"""One upstream feed, recoverable read model, and snapshot/delta subscriptions."""

from collections import Counter, deque
from copy import copy, deepcopy
import json
import logging
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import uuid

from atc_git import GitResolver, reference_digest
from atc_model import EvidenceModel, activity_time, latest_time
from atc_evidence import combined_evidence, merge_value_for_refs
from atc_reads import ReadQueue, ItemAssignmentSource
from atc_reconciliation import ActiveBackstop, RETRY_MS
from atc_upstream import ATTEST_FIELDS, Read, attest_row, now_ms


# Per-resource field and ordering policies come from the audited published build.
# Runtime applies them across Gateway builds, with an explicit warning when the
# connected identity differs; this is not runtime/feed acceptance evidence.
from atc_payloads import (
    WORK_ITEM_FIELDS, WORK_ITEM_SUMMARY_FIELDS, ASSIGNMENT_FIELDS, CLASSES, has_work_item_summary,
    required_fields, policies_for, payload_policy_warning, supports_work_item_summary,
)

BASE_SCOPES = (("work-items", None), ("sessions", None), ("assignments", None),
               ("roles", None), ("decision-requests", None), ("artifacts", None))
HISTORIES = {"attests", "turns", "wakes", "messages"}
PUBLICATION_INTERVAL = 0.25
EVENT_BATCH_LIMIT = 128
MAX_DELTA_HISTORY_COUNT = 64
MAX_DELTA_HISTORY_BYTES = 16 * 1024 * 1024
MERGE_WORKER_COUNT = 4
MERGE_CACHE_COMPACT_AFTER = 8192
MAX_MERGE_CACHE_ENTRIES = 20000
MERGE_CACHE_NAME = "merge-results.jsonl"

COLLECTIONS = ("agents", "items", "links", "wakes", "messages")
LOG = logging.getLogger("atc.recovery")

RESOURCE_REFS = {
    "work-items": "workItemId", "assignments": "assignmentId",
    "attests": "attestId", "sessions": "sessionKey", "turns": "turnSeq",
    "wakes": "wakeId", "roles": "role", "artifacts": "artifactId",
    "messages": "messageId", "decision requests": "decisionRequestId",
}


def _resource_counts(jobs):
    counts = Counter(job[0] for job in jobs)
    return {resource: counts[resource] for resource in sorted(counts)}


class FeedCursor:
    """Sequence continuity and heartbeat liveness on one subscription."""
    def __init__(self, now, timeout=45):
        self.sequence = 0
        self.heartbeat_at = now
        self.timeout = timeout
        self.watermark = 0
        self.pending_since = None

    def remaining(self, now):
        deadlines = [self.heartbeat_at + self.timeout]
        if self.pending_since is not None:
            deadlines.append(self.pending_since + self.timeout)
        remaining = min(deadlines) - now
        if remaining <= 0:
            raise TimeoutError("feed heartbeat or queued delivery missing")
        return remaining

    def accept(self, message, now):
        kind = message.get("type")
        if kind == "change":
            if message.get("schemaVersion") != 2 or message.get("seq") != self.sequence + 1:
                raise ValueError("feed sequence gap or unsupported schema")
            self.sequence += 1
            if self.sequence >= self.watermark:
                self.pending_since = None
            elif self.pending_since is not None:
                self.pending_since = now
        elif kind == "heartbeat":
            sequence = message.get("seq")
            if not isinstance(sequence, int) or sequence < self.sequence:
                raise ValueError("feed heartbeat sequence regressed")
            self.heartbeat_at = now
            self.watermark = sequence
            # Hub.sequence includes allocated frames still in its delivery
            # queue. A heartbeat ahead of the last delivered frame alone does
            # not prove loss. Missing next frames or a stalled queue do.
            if sequence > self.sequence and self.pending_since is None:
                self.pending_since = now
        elif kind == "error":
            raise ValueError("feed error")


class LiveService:
    def __init__(self, gateway, resolver=None, rebuild_interval=300, workers=8, scope_budget=32):
        if rebuild_interval <= 0 or scope_budget <= 0 or workers <= 0:
            raise ValueError("read intervals, workers and scope budget must be positive")
        self.gateway, self.resolver = gateway, resolver
        self.model = EvidenceModel()
        self.merge_cache_path = self._cache_path(resolver)
        self.merge_cache_records = 0
        self.merge_cache_lock = threading.Lock()
        self.model.merges.update(self._load_merge_cache())
        self.generation, self.revision = uuid.uuid4().hex, 0
        self.rebuild_interval, self.workers = rebuild_interval, workers
        self.events = queue.Queue()
        self.reads = ReadQueue(gateway, self.events, workers, self._read_plan)
        self.retired_activity_marker = None
        self.retired_activity_pending = False
        self.backstop = ActiveBackstop()
        self.scope_budget = scope_budget
        self.rotation = deque()
        self.cycle = None
        self.last_sweep = None
        self.sweep_remaining = set()
        self.read_failures = set()
        self.assignment_source_unavailable = False
        self.read_retries = {}
        # Transient unresolved notices, folded by author rather than retained
        # as rows. A completed resolution leaves no assignment ownership cache.
        self.attest_activity_pending = {}
        self.attest_activity_counts = {"triggered": 0, "coalesced": 0,
                                       "completed": 0, "failures": 0}
        self.retry_in_flight = set()
        self.retry_counters = {"scheduled": 0, "completed": 0, "incomplete": 0,
                               "epochDiscarded": 0}
        self.last_incomplete_read = None
        self.last_retry_outcome = None
        self.feed_lock = threading.Lock()
        self.feed_received = 0
        self.feed_processed = 0
        self.feed_queued = 0
        self.feed_last_lag_ms = None
        self.feed_max_lag_ms = None
        self.correlation_gaps = set()
        self.merge_jobs = queue.Queue()
        self.finish_trace_versions = {}
        self.merge_pending = {}
        self.merge_pending_complete = {}
        self.merge_workers_started = 0
        self.condition = threading.Condition()
        self.model_lock = threading.RLock()
        self._dirty = False
        self._next_maintenance = 0
        self._last_publication = 0
        self.stop_event = threading.Event()
        self.connected = False
        self.feed_available = True
        self.feed_warning = None
        self.state, self.since, self.reason = "stale", now_ms(), "initial state unavailable"
        self.last_complete, self.last_rebuild = None, 0
        self.version = None
        self.payload_warning = None
        self.policies = {}
        self.metrics = {"bootstrapReads": None, "bootstrapMs": None, "payloads": 0,
                        "bootstrapComplete": None, "targetedReads": 0, "rebuilds": 0}
        self.current = {}
        self._comparison = None
        self._published_details = -1
        self._full_event = None
        self.delta_history = deque()
        self.delta_history_bytes = 0
        self._publish()

    def snapshot(self):
        with self.condition:
            value = deepcopy(self.current)
        value["freshness"]["reconciliation"]["readDiagnostics"] = self._read_diagnostics()
        return value

    def next_snapshot(self, revision, timeout=15):
        with self.condition:
            self.condition.wait_for(
                lambda: self.revision != revision or self.stop_event.is_set(), timeout)
            return deepcopy(self.current) if self.revision != revision else None

    def next_event(self, revision, timeout=15):
        """Return retained delta frames in order, or a full snapshot on a gap."""
        with self.condition:
            self.condition.wait_for(
                lambda: self.revision != revision or self.stop_event.is_set(), timeout)
            if self.revision == revision:
                return None
            history = {base: (next_revision, body)
                       for base, next_revision, body in self.delta_history}
            cursor = revision
            frames = []
            while cursor < self.revision:
                retained = history.get(cursor)
                if retained is None:
                    break
                next_revision, body = retained
                if next_revision != cursor + 1 or next_revision > self.revision:
                    break
                frames.append(body)
                cursor = next_revision
            body = b"".join(frames) if cursor == self.revision else self.event
            return self.revision, body

    def _retain_delta(self, base_revision, revision, body):
        if len(body) > MAX_DELTA_HISTORY_BYTES:
            # Keep even one unusually large publication from exceeding the cap.
            self.delta_history.clear()
            self.delta_history_bytes = 0
            return
        self.delta_history.append((base_revision, revision, body))
        self.delta_history_bytes += len(body)
        while (len(self.delta_history) > MAX_DELTA_HISTORY_COUNT or
               self.delta_history_bytes > MAX_DELTA_HISTORY_BYTES):
            _, _, expired = self.delta_history.popleft()
            self.delta_history_bytes -= len(expired)

    @property
    def event(self):
        """Encode full state only for a new subscriber or an explicit resync."""
        with self.condition:
            if self._full_event is None:
                self._full_event = self._encode("model", self.current)
            return self._full_event

    @staticmethod
    def _encode(kind, value):
        return ("event: " + kind + "\ndata: " +
                json.dumps(value, separators=(",", ":")) + "\n\n").encode()

    @staticmethod
    def _comparable(collection, row):
        return {k: v for k, v in row.items() if k != "idleFor"} if collection == "agents" else row

    def evidence_page(self, item_id, offset=0, limit=100, generation=None, revision=None):
        from heapq import nsmallest
        from itertools import chain
        from atc_summary import HistoryFold, DetailFingerprint
        from atc_evidence import Inventory

        # Capture only this item's current facts. Network reads must not hold
        # the model worker's lock; a publication during I/O invalidates the pin.
        with self.model_lock:
            pin = {"generation": self.generation, "revision": self.revision}
            if self._dirty:
                return 409, {"error": "revision-mismatch", **pin}
            if (offset < 0 or not 1 <= limit <= 100 or
                    (offset > 0 and (generation is None or revision is None)) or
                    ((generation is None) != (revision is None))):
                return 400, {"error": "invalid-pagination"}
            if generation is not None and (generation != self.generation or revision != self.revision):
                return 409, {"error": "revision-mismatch", **pin}
            work = self.model.inventory("work-items")
            item = self.model.work_items().get(item_id)
            if item is None:
                return (404 if work.complete else 503), {"error": "item-unavailable", **pin}
            item = deepcopy(item)
            work_facts = deepcopy(work.evidence())
            older = self.model.older_items.get(item_id) if item_id not in work.rows else None
            older_fingerprint = older["fingerprint"] if older else None
            owned = deepcopy([a for a in self.model.inventory("assignments").rows.values()
                              if a.get("workItemId") == item_id])
            closed = self.model.closed_item(item_id, owned)
            expected_closed = closed["detailFingerprint"] if closed else None
            assignment_facts = deepcopy(self.model.assignment_evidence(item_id))
            if item_id in self.model.item_assignment_pending:
                assignment_facts.update(status="unknown", complete=False, freshness="stale")
            attest_pins = {a["id"]: (self.reads.epochs["attests", a["id"]],
                                    self.model.inventory("attests", a["id"]).fingerprint)
                           for a in owned}

        def finish(status, body, event=None):
            with self.model_lock:
                current_pin = {"generation": self.generation, "revision": self.revision}
                if self._dirty or current_pin != pin:
                    return 409, {"error": "revision-mismatch", **current_pin}
                if event is not None:
                    self._dirty = True
                    self.events.put(event)
                return status, body

        def reload_details(observed=None):
            return finish(409, {"error": "revision-mismatch", **pin},
                          ("detail-reload", (item_id, observed)))

        if older_fingerprint is not None:
            result = self.gateway.detail("work-items", item_id)
            if (not result.complete or len(result.rows) != 1
                    or result.rows[0].get("id") != item_id):
                return finish(503, {"error": "item-unavailable", **pin})
            if self.model.item_fingerprint(result.rows[0]) != older_fingerprint:
                return finish(409, {"error": "revision-mismatch", **pin},
                              ("detail-item-reload", item_id))
            item = result.rows[0]
            del result
        closed_fingerprint = DetailFingerprint() if expected_closed is not None else None
        assignment_fingerprint = DetailFingerprint() if closed_fingerprint else None
        if closed_fingerprint is not None:
            # Existing per-item membership includes original assignment rows in
            # source order. Do not materialize the all-time global collection.
            result = self.gateway.assignment_membership(item_id, include_rows=True)
            if not result.complete and not result.rows:
                return finish(503, {"error": "item-unavailable", **pin})
            fresh_owned = result.rows
            if [a for a in fresh_owned if a.get("state") == "open"] != owned:
                return reload_details()
            evidence = Inventory()
            evidence.read((), complete=result.complete, at=result.at, source=result.source)
            assignment_facts = combined_evidence(assignment_facts, evidence.evidence())
            owned = fresh_owned
            del result, fresh_owned
        scopes, rows, total = [], [], 0
        end = offset + limit
        for position, assignment in enumerate(owned):
            scope = assignment["id"]
            folded_assignment = closed_fingerprint is not None and assignment.get("state") != "open"
            result = self.gateway.read("attests", scope)
            fold = HistoryFold("attests", result.at)
            fold.add(result.rows)
            if folded_assignment:
                closed_fingerprint.add(assignment, fold.fingerprint.hexdigest(), position)
                assignment_fingerprint.add(assignment, None, position)
            else:
                epoch, fingerprint = attest_pins[scope]
                if fingerprint != fold.fingerprint.hexdigest():
                    return finish(409, {"error": "revision-mismatch", **pin},
                                  ("detail-history", (scope, epoch, Read(
                                      [], result.complete, result.at, result.source,
                                      result.error, result.page, fold))))
            evidence = Inventory()
            evidence.read((), complete=result.complete, at=result.at, source=result.source)
            scopes.append(evidence.evidence())
            total += len(result.rows)
            rows = nsmallest(end, chain(rows, result.rows),
                             key=lambda row: (row.get("ts") or 0, row["id"]))
            del result, fold
        if closed_fingerprint is not None and closed_fingerprint.hexdigest() != expected_closed:
            return reload_details((expected_closed, closed_fingerprint.hexdigest(),
                                   assignment_fingerprint.hexdigest()))
        facts = combined_evidence(work_facts, assignment_facts, *scopes)
        return finish(200, deepcopy({
            **pin, "itemId": item_id, "item": item, "assignments": owned,
            "records": [dict(row, bySession=row.get("bySession"), byUser=row.get("byUser"),
                             note=row.get("note")) for row in rows[offset:end]],
            "total": total, "nextOffset": end if end < total else None,
            "complete": facts["complete"], "freshness": facts,
        }))

    def start(self):
        try:
            from websockets.sync.client import connect  # noqa: F401
        except ImportError:
            # The feed is optional. Keep published reads and the viewer alive;
            # periodic snapshots still provide a useful, explicitly stale map.
            self.feed_available = False
            self.feed_warning = (
                "Live feed is unavailable because the optional websockets library "
                "is missing; ATC will use periodic gateway snapshots.")
            self.rebuild()
            targets = (self._worker,)
        else:
            targets = (self._worker, self._feed)
        for target in targets:
            threading.Thread(target=target, daemon=True).start()
        if self.resolver is not None:
            resolvers = self._merge_resolvers()
            self.merge_workers_started = len(resolvers)
            for index, resolver in enumerate(resolvers):
                threading.Thread(
                    target=self._git_worker, args=(resolver,), daemon=True,
                    name=f"atc-git-{index}").start()

    def stop(self):
        self.stop_event.set()
        self.events.put(("stop", None))
        for _ in range(self.merge_workers_started):
            self.merge_jobs.put(None)
        self.reads.close()
        if isinstance(self.resolver, GitResolver):
            self.resolver.acquisitions.close()
        with self.condition:
            self.condition.notify_all()

    @staticmethod
    def _cache_path(resolver):
        cache = getattr(resolver, "cache", None)
        return Path(cache) / MERGE_CACHE_NAME if cache is not None else None

    def _load_merge_cache(self):
        path = self.merge_cache_path
        if path is None:
            return {}
        loaded = {}
        try:
            with path.open(encoding="utf-8") as source:
                for line in source:
                    self.merge_cache_records += 1
                    try:
                        record = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    item_id, refs = record.get("id"), record.get("refs")
                    value, observed_at = record.get("value"), record.get("observedAt")
                    attempted_at = record.get("lastAttemptAt", observed_at)
                    attempted_refs = record.get("lastAttemptRefs", refs)
                    if (not isinstance(item_id, str) or not item_id
                            or not isinstance(refs, list)
                            or not all(isinstance(ref, dict) for ref in refs)
                            or not isinstance(attempted_refs, list)
                            or not all(isinstance(ref, dict) for ref in attempted_refs)
                            or (value is not None and not isinstance(value, bool))
                            or not isinstance(observed_at, (int, float))
                            or not isinstance(attempted_at, (int, float))):
                        continue
                    loaded[item_id] = {
                        "value": value, "observedAt": observed_at,
                        "lastAttemptAt": attempted_at, "lastAttemptRefs": attempted_refs,
                        "lastAttemptComplete": record.get("lastAttemptComplete", True),
                        "refs": refs, "freshness": "stale",
                        **({"landingAt": record["landingAt"]} if record.get("landingAt") is not None else {}),
                    }
        except FileNotFoundError:
            return {}
        except OSError as error:
            LOG.warning("could not read persisted merge evidence: %s", error)
            return {}
        if len(loaded) > MAX_MERGE_CACHE_ENTRIES:
            loaded = dict(sorted(
                loaded.items(), key=lambda entry: entry[1]["observedAt"]
            )[-MAX_MERGE_CACHE_ENTRIES:])
        return loaded

    def _persist_merge_result(self, item_id, result):
        path = self.merge_cache_path
        if path is None:
            return
        record = {"id": item_id, "refs": result["refs"],
                  "lastAttemptRefs": result.get("lastAttemptRefs", result["refs"]),
                  "lastAttemptComplete": result.get("lastAttemptComplete", True),
                  "value": result["value"], "observedAt": result["observedAt"],
                  "landingAt": result.get("landingAt"),
                  "lastAttemptAt": result.get("lastAttemptAt", result["observedAt"])}
        try:
            with self.merge_cache_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(record, separators=(",", ":"),
                                            sort_keys=True) + "\n")
                self.merge_cache_records += 1
                if self.merge_cache_records >= MERGE_CACHE_COMPACT_AFTER:
                    self._compact_merge_cache(path)
        except OSError as error:
            LOG.warning("could not persist merge evidence: %s", error)

    def _compact_merge_cache(self, path):
        entries = sorted(
            self.model.merges.items(), key=lambda entry: entry[1]["observedAt"],
            reverse=True,
        )[:MAX_MERGE_CACHE_ENTRIES]
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=path.parent,
                    prefix=path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = output.name
                for item_id, result in entries:
                    output.write(json.dumps({
                        "id": item_id, "refs": result["refs"],
                        "lastAttemptRefs": result.get("lastAttemptRefs", result["refs"]),
                        "lastAttemptComplete": result.get("lastAttemptComplete", True),
                        "value": result["value"], "observedAt": result["observedAt"],
                  "landingAt": result.get("landingAt"),
                        "lastAttemptAt": result.get("lastAttemptAt", result["observedAt"]),
                    }, separators=(",", ":"), sort_keys=True) + "\n")
            os.replace(temporary, path)
            self.merge_cache_records = len(entries)
        except OSError as error:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            LOG.warning("could not compact persisted merge evidence: %s", error)

    def _merge_resolvers(self):
        resolver = self.resolver
        if not isinstance(resolver, GitResolver):
            return [resolver] * MERGE_WORKER_COUNT
        workers = [resolver]
        for index in range(1, MERGE_WORKER_COUNT):
            worker = copy(resolver)
            worker.cache = Path(resolver.cache) / "workers" / f"worker-{index}"
            worker.results, worker.origins = {}, {}
            worker.branches, worker.targets = {}, {}
            workers.append(worker)
        return workers

    def _state(self, state, reason=None):
        if self.assignment_source_unavailable:
            state = "stale"
            confirmed = self.model.inventory("assignments").confirmed_at
            reason = "assignments unavailable; last complete read: " + str(confirmed)
        if state == "live" and self.model.item_assignment_pending:
            state, reason = "rebuilding", "reading complete item assignment source"
        if (state in {"live", "rebuilding"} and self.connected and
                any(self.model.inventory(*s).budget_stale for s in self.backstop.active)):
            state, reason = "stale", "active reconciliation budget exceeded"
        if (self.state, self.reason) != (state, reason):
            self.state, self.reason, self.since = state, reason, now_ms()
            return True
        return False

    def _incomplete_state(self, reason):
        # A connected feed with a failed read is still rebuilding; the
        # affected inventories carry stale evidence individually. Disconnects
        # and over-budget active scopes remain a global stale condition. An
        # uncorrelated change has no inventory to mark stale, so retain its
        # global warning until a full sweep or recovery resolves the gap.
        if self.correlation_gaps:
            resource = min(resource for resource, _ in self.correlation_gaps)
            self._state("stale", "uncorrelated " + resource + " change")
            return
        self._state("rebuilding" if self.connected else "stale", reason)

    def _publish(self, *, classified=False):
        # Only the model worker writes. Build the next immutable frame without
        # holding the condition used by subscribers of the committed frame.
        at = now_ms()
        visible = {row["sessionKey"] for row in self.model.active_sessions()}
        removed = self.model.item_assignment_pending - self.model.work_items().keys()
        self._forget_scopes({("item-assignment-source", wid) for wid in removed})
        for wid in removed:
            self.model.item_assignment_pending.discard(wid)
            self.model.item_assignment_facts.pop(wid, None)
            self.model.item_assignment_summary.pop(wid, None)
        if self._fold_closed_sources():
            classified = False
        if not classified:
            self._update_backstop(at)
        if self.model.inventory("work-items").complete:
            eligible_activity_items = {wid for wid, item in self.model.work_items().items()
                if not (self._summary_contract_active() and has_work_item_summary(item)
                        and not self._summary_needs_review_timing(item))}
            self.model.retain_exited_activity(eligible_activity_items, at)
        data = deepcopy(self.model.snapshot(
            at, assignment_source_unavailable=self.assignment_source_unavailable))
        self._retain_unproven_removals(data, at)
        old_agents = {row["id"]: row for row in self.current.get("agents", [])}
        unresolved_activity = {}
        for pending in self.attest_activity_pending.values():
            if pending["failedAt"] is not None:
                for author in pending["authors"]:
                    unresolved_activity[author] = min(
                        unresolved_activity.get(author, pending["failedAt"]), pending["failedAt"])
        for agent in data["agents"]:
            key = agent["sessionKey"]
            fact = agent["evidence"]["idleFor"]
            if key in unresolved_activity:
                if fact["freshness"] != "stale":
                    fact["freshness"] = "unconfirmed"
                fact["unconfirmedSince"] = min(
                    fact.get("unconfirmedSince") or unresolved_activity[key], unresolved_activity[key])
            previous = old_agents.get(agent["id"])
            if (previous and previous.get("idleSince") == agent.get("idleSince") and
                    (self.state != "live" or fact["freshness"] != "live" or not fact["complete"])):
                agent["idleFor"] = previous.get("idleFor")
        candidate = {
            "schemaVersion": 1, "generation": self.generation,
            "freshness": {
                "state": self.state, "since": self.since, "reason": self.reason,
                "rebuildIntervalMs": self.rebuild_interval * 1000,
                "lastCompleteAt": self.last_complete,
                "reconciliation": self._reconciliation(),
            },
            "gatewayVersion": self.version, "payloadWarning": self.payload_warning, **data,
        }
        # idleFor is sampled at generatedAt; viewers advance it from idleSince.
        # A ticking clock or diagnostic counter alone is not a model change.
        comparison = dict(candidate, agents=[
            {key: value for key, value in agent.items() if key != "idleFor"}
            for agent in data["agents"]])
        comparison["freshness"] = dict(candidate["freshness"])
        comparison["freshness"]["reconciliation"] = dict(
            candidate["freshness"]["reconciliation"])
        comparison["freshness"]["reconciliation"].pop("readDiagnostics", None)
        self._dirty = False
        self._last_publication = time.monotonic()
        if comparison == self._comparison and self.model.details_revision == self._published_details:
            self._release_retired_histories()
            self.model.release_auxiliary_sources()
            self.model.fold_older_work_items()
            return False
        previous = self.current
        base_revision = self.revision
        revision = base_revision + 1
        current = dict(candidate, revision=revision, generatedAt=at, metrics=dict(self.metrics))
        changes = {}
        for name in COLLECTIONS:
            before = {row["id"]: row for row in previous.get(name, [])}
            after = {row["id"]: row for row in data[name]}
            changes[name] = {
                "upsert": [row for key, row in after.items() if key not in before or
                           self._comparable(name, row) != self._comparable(name, before[key])],
                "remove": [key for key in before if key not in after],
            }
        metadata = {key: value for key, value in current.items() if key not in COLLECTIONS}
        delta = self._encode("delta", dict(metadata, baseRevision=base_revision, changes=changes))
        with self.condition:
            previous = self.current
            # Publish the revision and its catch-up path atomically to subscribers.
            self._retain_delta(base_revision, revision, delta)
            self.revision = revision
            self.current = current
            self._comparison = comparison
            self._published_details = self.model.details_revision
            self._full_event = None
            self.condition.notify_all()
        self._release_retired_histories()
        self.model.release_auxiliary_sources()
        self.model.fold_older_work_items()
        return True

    def _release_retired_histories(self):
        if not (self.model.inventory("sessions").complete
                and self.model.inventory("assignments").complete):
            return
        visible = {row["sessionKey"] for row in self.current.get("agents", [])}
        released = self.model.release_session_histories(visible, at=now_ms())
        self._forget_scopes(released)
        sessions = self.model.inventory("sessions")
        for key in list(sessions.rows):
            if key not in visible:
                del sessions.rows[key]
                sessions.observations.pop(key, None)

    def _fold_closed_sources(self):
        # In-flight reads still need their construction rows. Once the cycle
        # settles, publish only the per-item facts and current live lookups.
        if (self.cycle or self.retired_activity_pending
                or not all(self.model.inventory(*scope).complete for scope in BASE_SCOPES)):
            return False
        assignments = self.model.inventory("assignments")
        if not assignments.complete:
            return False
        eligible = set(self._history_scopes())
        closed = {("attests", row["id"]) for row in assignments.rows.values()
                  if row.get("state") != "open"}
        if any(scope in self.reads.active or any(job[:2] == scope for job in self.reads.pending)
               or (scope in eligible and not self.model.inventory(*scope).complete)
               for scope in closed):
            return False
        released = self.model.fold_closed_assignments()
        self._forget_scopes(released)
        return bool(released)

    def _forget_scopes(self, released):
        for key in list(self.reads.pending):
            if key[:2] in released:
                del self.reads.pending[key]
        for key in list(self.reads.epochs):
            if key in released and key not in self.reads.active:
                del self.reads.epochs[key]
        for key in list(self.read_retries):
            if key[:2] in released:
                del self.read_retries[key]
        self.retry_in_flight = {job for job in self.retry_in_flight if job[:2] not in released}
        self.read_failures.difference_update(released)
        self.sweep_remaining.difference_update(released)
        self.rotation = deque(key for key in self.rotation if key not in released)
        self.backstop.active.difference_update(released)
        self.backstop.history.difference_update(released)
        for key in released:
            self.backstop.attempted.pop(key, None)
        if self.cycle:
            self.cycle["pending"].difference_update(released)
            self.cycle["seen"].difference_update(released)

    def _ensure_retired_activity(self):
        if self.model.closed_sources_released:
            return
        if not all(self.model.inventory(resource).complete
                   for resource in ("work-items", "assignments", "sessions")):
            return
        marker, sessions, _assignments = self.model.retired_activity_plan()
        if self.retired_activity_pending or marker == self.retired_activity_marker:
            return
        if ("retired-turns", None, None) in self.read_retries:
            return
        if not sessions:
            self.model.fold_retired_activity({})
            self.retired_activity_marker = marker
            return
        self.retired_activity_pending = True
        self.reads.schedule("retired-turns")

    def _retired_activity_done(self, job, result):
        self.retired_activity_pending = False
        self.retry_in_flight.discard(job)
        marker, activity = result.folded if result.folded is not None else (None, {})
        self.model.fold_retired_activity(activity)
        if result.complete:
            self.retired_activity_marker = marker
            self.read_failures.discard(job[:2])
            self.read_retries.pop(job, None)
            self._ensure_retired_activity()
        else:
            self.read_failures.add(job[:2])
            self.read_retries[job] = (time.monotonic() + RETRY_MS / 1000, 0)
            self.last_incomplete_read = self._read_outcome(*job, result)
            self._incomplete_state("published read incomplete")
        if self.cycle:
            self._settle_cycle()
        return True

    def _retain_unproven_removals(self, data, at):
        # A partial read may contain a changed row (for example a retirement)
        # but cannot certify disappearance from a cross-inventory projection.
        bases = self.model.inventory("sessions").complete and self.model.inventory("assignments").complete
        complete = {"items": self.model.inventory("work-items").complete, "agents": bases}
        for name, sources in (("links", {"turns", "wakes"}), ("wakes", {"wakes"}),
                              ("messages", {"messages"})):
            complete[name] = bases and all(inventory.complete for (resource, _), inventory
                                          in self.model.inventories.items() if resource in sources)
        complete["messages"] = complete["messages"] and self.model.inventory("roles").complete
        for name in COLLECTIONS:
            if complete[name]:
                continue
            present = {row["id"] for row in data[name]}
            for old in self.current.get(name, []):
                if old["id"] in present:
                    continue
                if name in {"wakes", "messages"}:
                    expiry = self.model.recent_expiries.get((name, old["id"]))
                    if expiry is None or expiry <= at:
                        continue  # Timed projection expiry, not a source deletion.
                    self.model._expires(expiry)
                retained = deepcopy(old)
                for fact in retained.get("evidence", {}).values():
                    fact.update(status="unknown", complete=False, freshness="stale")
                if name == "agents":
                    retained["turn"] = None
                if name == "links":
                    retained["state"] = None
                data[name].append(retained)

    def _history_scopes(self):
        summary_active = self._summary_contract_active()
        work_items = self.model.work_items()
        attest_scopes = []
        for assignment in self.model.inventory("assignments").rows.values():
            item = work_items.get(assignment.get("workItemId"))
            if (summary_active and has_work_item_summary(item)
                    and not self._summary_needs_review_timing(item)):
                continue
            attest_scopes.append(("attests", assignment["id"]))
        scopes = attest_scopes + [
                    (resource, session["sessionKey"])
                    for session in self.model.active_sessions()
                    for resource in ("turns", "wakes", "messages")]
        return list(dict.fromkeys(scopes))

    def _update_backstop(self, at):
        before = (self.state, self.reason, self.backstop.active, self.backstop.history,
                  {s for s in self.backstop.active if self.model.inventory(*s).budget_stale})
        scopes = self._history_scopes()
        self.backstop.classify(self.model, scopes, at)
        overdue = any(self.model.inventory(*s).budget_stale for s in self.backstop.active)
        if overdue and self.connected:
            self._state("stale", "active reconciliation budget exceeded")
        elif self.reason == "active reconciliation budget exceeded":
            required = list(BASE_SCOPES) + scopes
            complete = all(self.model.inventory(*s).complete and not self.model.inventory(*s).stale
                           for s in required)
            if self.cycle:
                self._state("rebuilding", "reading published state: " + self.cycle["mode"])
            elif complete and self.connected and not self.read_failures and not self.correlation_gaps:
                self._state("live")
            else:
                self._incomplete_state("incomplete read or disconnected feed")
        after = (self.state, self.reason, self.backstop.active, self.backstop.history,
                 {s for s in self.backstop.active if self.model.inventory(*s).budget_stale})
        return before != after

    def _reconciliation(self):
        cycle = self.cycle
        item_reads = {("item-assignment-source", wid) for wid in self.model.item_assignment_pending}
        pending = (cycle["pending"] if cycle else set()) | item_reads
        seen = (cycle["seen"] if cycle else set()) | item_reads
        histories = len(self._history_scopes())
        repeated_history = self._repeated_history_scopes()
        terminal = len(repeated_history)
        backstop = self.backstop.metadata(self.model, self.connected)
        backstop["history"] = self.backstop.group(self.model, repeated_history)
        return {"mode": cycle["mode"] if cycle else None,
                "pendingScopes": len(pending),
                "pendingByResource": _resource_counts(pending),
                "totalScopes": len(seen),
                "scopeBudget": self.scope_budget,
                "historyScopes": histories,
                "estimatedSweepMs": ((terminal + self.scope_budget - 1) // self.scope_budget
                                     * self.rebuild_interval * 1000),
                "lastSweepAt": self.last_sweep,
                "readDiagnostics": self._read_diagnostics(),
                **backstop}

    def _summary_contract_active(self):
        if supports_work_item_summary(self.version):
            return True
        if isinstance(self.version, dict) and self.version.get("version") is not None:
            return False
        rows = self.model.work_items().values()
        rows = list(rows)
        return bool(rows) and all(has_work_item_summary(row) for row in rows)

    def _repeated_history_scopes(self):
        scopes = set(self.backstop.history)
        if self._summary_contract_active():
            # Summary-backed attest detail stays in the backstop only for
            # stage timestamp ordering or unresolved summary rows.
            required = set(self._history_scopes())
            scopes = {scope for scope in scopes
                      if scope[0] != "attests" or scope in required}
        return scopes

    @staticmethod
    def _summary_needs_review_timing(item):
        """Whether retained attest timestamps can change the canonical stage."""
        if not has_work_item_summary(item) or item["state"] == "closed" or item["holders"]:
            return False
        return (item["attestCounts"].get("completion", 0) > 0
                and item["verdictCounts"].get("reviewed-clean", 0) > 0
                and item["verdictCounts"].get("tests-passed", 0) > 0)

    def _item_attest_counts_match(self, item, *, require_fresh=False):
        assignments = self.model.inventory("assignments")
        if (not assignments.complete or not has_work_item_summary(item)
                or (require_fresh and assignments.evidence()["freshness"] != "live")):
            return False
        owned = [row for row in assignments.rows.values()
                 if row.get("workItemId") == item["id"]]
        inventories = [self.model.inventory("attests", row["id"]) for row in owned]
        if any(not inventory.complete or (require_fresh
               and inventory.evidence()["freshness"] != "live") for inventory in inventories):
            return False
        kinds, verdicts = Counter(), Counter()
        for row in owned:
            summary = self.model.summary("attests", row["id"])
            kinds.update({kind: count for kind, count in summary["kinds"].items() if kind is not None})
            verdicts.update(summary["verdicts"])
        closed = self.model.closed_item(item["id"], owned)
        if closed:
            facts = closed["facts"]
            if not facts["complete"] or (require_fresh and facts["freshness"] != "live"):
                return False
            kinds.update({kind: count for kind, count in closed["summary"]["kinds"].items()
                          if kind is not None})
            verdicts.update(closed["summary"]["verdicts"])
        return dict(kinds) == item["attestCounts"] and dict(verdicts) == item["verdictCounts"]

    def _reconcile_item_attests(self, item):
        self._schedule_finish_time(item)
        if item["id"] in self.model.item_assignment_pending:
            return
        assignments = self.model.inventory("assignments")
        timing_needed = self._summary_needs_review_timing(item)
        summary_active = self._summary_contract_active() and has_work_item_summary(item)
        skip = summary_active and not timing_needed
        owned = [row for row in assignments.rows.values()
                 if row.get("workItemId") == item["id"]]
        for row in owned:
            inventory = self.model.inventories.get(("attests", row["id"]))
            if inventory is None:
                continue
            was_eligible = getattr(inventory, "activity_eligible", True)
            inventory.activity_eligible = not skip
            if not skip and not was_eligible:
                # Re-entering the fresh-load source set requires the existing
                # scoped read; observations from before the skip cannot
                # certify activity delivered while that scope was omitted.
                self._refresh("attests", row["id"])
        closed = self.model.closed_item(item["id"], owned)
        if assignments.complete and closed is not None and closed["attestsRead"] == skip:
            # A change into or out of the existing summary-skip set changes
            # activity eligibility even when the published counts are equal.
            # Released closed identities need the item's complete read path.
            # Retained assignment identities use the matching attest scopes
            # below; summary validity alone does not require a membership read.
            self._request_item_assignments(item["id"], changed=True)
            return
        if not summary_active:
            return
        if (not assignments.complete
                or (timing_needed and assignments.evidence()["freshness"] != "live")):
            if timing_needed and self.connected:
                job = ("assignments", None, None)
                pending = job[:2] in {key[:2] for key in self.reads.pending}
                active = any(key[:2] == job[:2] for key, _epoch in self.reads.active.values())
                if not pending and not active:
                    self._refresh("assignments")
            return
        details_complete = all(
            self.model.inventory("attests", row["id"]).complete for row in owned)
        details_match = self._item_attest_counts_match(item, require_fresh=timing_needed)
        if (not timing_needed and not details_complete) or details_match:
            return
        if self.model.closed_item(item["id"], owned):
            # Released assignment identities belong to this item's complete
            # source read. A changed count does not invalidate other items or
            # require rediscovering every retired holder's turn history.
            self._request_item_assignments(item["id"], changed=True)
            return
        for row in assignments.rows.values():
            if row.get("workItemId") != item["id"]:
                continue
            job = ("attests", row["id"], None)
            pending = job[:2] in {key[:2] for key in self.reads.pending}
            active = any(key[:2] == job[:2] for key, _epoch in self.reads.active.values())
            if not pending and not active:
                self._refresh(*job)

    def _reconcile_work_item_attests(self):
        for item in self.model.work_items().values():
            self._reconcile_item_attests(item)

    def _read_diagnostics(self):
        with self.feed_lock:
            feed = {"received": self.feed_received, "processed": self.feed_processed,
                    "queued": self.feed_queued,
                    "lastReceiveToProcessMs": self.feed_last_lag_ms,
                    "maxReceiveToProcessMs": self.feed_max_lag_ms}
        return {
            "lastIncompleteRead": deepcopy(self.last_incomplete_read),
            "lastRetryOutcome": deepcopy(self.last_retry_outcome),
            "gatewayReadRequests": getattr(self.gateway, "read_count", None),
            "readQueue": {"pending": len(self.reads.pending),
                          "pendingByResource": _resource_counts(self.reads.pending),
                          "active": len(self.reads.active),
                          "activeByResource": _resource_counts(
                              job for job, _epoch in self.reads.active.values())},
            "retry": {**self.retry_counters, "waiting": len(self.read_retries),
                      "inProgress": len(self.retry_in_flight)},
            "epochDiscards": {"total": self.reads.epoch_discards,
                              "byResource": dict(self.reads.epoch_discards_by_resource)},
            "feed": feed,
        }

    def _record_feed_processed(self, received_at):
        lag_ms = max(0, round((time.monotonic() - received_at) * 1000))
        with self.feed_lock:
            self.feed_processed += 1
            self.feed_queued = max(0, self.feed_queued - 1)
            self.feed_last_lag_ms = lag_ms
            self.feed_max_lag_ms = (lag_ms if self.feed_max_lag_ms is None
                                    else max(self.feed_max_lag_ms, lag_ms))

    def _queue_feed_change(self, message, received_at=None):
        received_at = time.monotonic() if received_at is None else received_at
        with self.feed_lock:
            self.feed_received += 1
            self.feed_queued += 1
        self.events.put(("change", (message, received_at)))

    def rebuild(self, *, recovery=True, resume=False):
        """Schedule work, never perform upstream I/O on the model worker.

        A new bootstrap reads every scope. A resumed recovery keeps completed
        scopes, including those marked unconfirmed after feed loss, and adds
        newly discovered or incomplete scopes. Normal intervals rotate through
        bounded history scopes. A collection establishes absence only after
        every page succeeds.
        """
        if recovery and not resume:
            self.retired_activity_marker = None
            self.model.begin_activity_refresh()
        if self.cycle is not None:
            if recovery:
                self.cycle["mode"] = "recovery"
                self.cycle["expanded"] = False
                self.cycle["resume"] = resume
                if resume:
                    if self.connected:
                        self._expand_cycle()
                        self._resume_cycle()
                        self._settle_cycle()
                        if self.cycle:
                            self._incomplete_state("reading published state: " + self.cycle["mode"])
                else:
                    self.model.invalidate()
                    for resource, scope in self.model.inventories:
                        self.reads.touch(resource, scope)
                    self._cycle_add(BASE_SCOPES, repeat=True)
                    if self.connected:
                        self._incomplete_state("reading published state: " + self.cycle["mode"])
            return
        mode = ("bootstrap" if self.metrics["bootstrapReads"] is None else "recovery") if recovery else "interval"
        completed = ({scope for scope, inventory in self.model.inventories.items()
                      if inventory.complete} if recovery and resume else set())
        self.cycle = {"mode": mode, "pending": set(), "seen": completed,
                      "expanded": False, "started": time.monotonic(),
                      "reads": self.gateway.read_count, "complete": True,
                      "resume": resume}
        if recovery and not resume:
            self.model.invalidate()
        self._state("rebuilding", "reading published state: " + mode)
        self._cycle_add(BASE_SCOPES)
        self.metrics["rebuilds"] += 1
        if resume and self.connected:
            self._expand_cycle()
            self._resume_cycle()
            self._settle_cycle()
            if self.cycle:
                self._incomplete_state("reading published state: " + self.cycle["mode"])
        # Tick from start, not completion. An unfinished cycle cannot overlap.
        self.last_rebuild = time.monotonic()

    def _cycle_add(self, scopes, repeat=False):
        for resource, scope in scopes:
            inventory = resource, scope
            if repeat or inventory not in self.cycle["seen"]:
                self.cycle["seen"].add(inventory)
                self.cycle["pending"].add(inventory)
                self.read_retries.pop((resource, scope, None), None)
                self.reads.schedule(resource, scope)

    def _resume_cycle(self):
        """Requeue only unfinished cycle scopes, respecting existing retry deadlines."""
        cycle = self.cycle
        if not cycle:
            return
        waiting_retries = {job[:2] for job in self.read_retries if job[2] is None}
        queued = {job[:2] for job in self.reads.pending if job[2] is None}
        active = {job[:2] for job, _ in self.reads.active.values() if job[2] is None}

        # A successful read while disconnected is retained as data but marked
        # stale. Re-add those scopes here; completed connected reads stay done.
        for inventory in cycle["seen"]:
            state = self.model.inventory(*inventory)
            if not state.complete or state.stale or state.budget_stale:
                cycle["pending"].add(inventory)

        for inventory in tuple(cycle["pending"]):
            if inventory in waiting_retries or inventory in queued or inventory in active:
                continue
            state = self.model.inventory(*inventory)
            if state.complete and not state.stale and not state.budget_stale:
                cycle["pending"].discard(inventory)
                continue
            self.reads.schedule(*inventory)

    def _retry_incomplete_reads(self):
        now = time.monotonic()
        for job, (deadline, priority) in list(self.read_retries.items()):
            if deadline > now:
                continue
            self.read_retries.pop(job, None)
            self.retry_in_flight.add(job)
            self.reads.schedule(*job, priority=priority == 2, active=priority == 1)

    def _expand_cycle(self):
        cycle = self.cycle
        if cycle["expanded"] or any(s in cycle["pending"] for s in BASE_SCOPES):
            return
        cycle["expanded"] = True
        self._ensure_retired_activity()
        scopes = self._history_scopes()
        if cycle["mode"] != "interval":
            self._cycle_add(scopes, repeat=not cycle.get("resume", False))
            return
        needed = set(scopes)
        self.backstop.classify(self.model, scopes, now_ms())
        terminal = self._repeated_history_scopes()
        self.rotation = deque(s for s in self.rotation if s in terminal)
        scheduled = set(self.rotation)
        self.rotation.extend(s for s in scopes if s in terminal and s not in scheduled)
        # Newly discovered scopes are genuine data gaps, not interval recrawls.
        missing = [s for s in scopes if self.model.inventory(*s).observed_at is None]
        self._cycle_add(missing)
        for _ in range(min(self.scope_budget, len(self.rotation))):
            scope = self.rotation.popleft()
            self.rotation.append(scope)
            self._cycle_add([scope])
        if not self.sweep_remaining:
            self.sweep_remaining = needed
        else:
            self.sweep_remaining.intersection_update(needed)

    def _settle_cycle(self):
        if not self.cycle:
            return
        self._expand_cycle()
        cycle = self.cycle
        # A cycle already in progress when the feed drops stays open to resume
        # its unfinished scopes. New offline snapshots can still settle and
        # keep the existing periodic-read behavior.
        if ((not self.connected and cycle.get("resume")) or
                cycle["pending"] or not cycle["expanded"] or self.retired_activity_pending
                or self.model.item_assignment_pending
                or ("retired-turns", None) in self.read_failures):
            return
        # A failing history remains stale until its next successful rotation;
        # a successful unrelated batch never declares the whole map complete.
        required = list(BASE_SCOPES) + self._history_scopes()
        complete = all(self.model.inventory(*s).complete and not self.model.inventory(*s).stale
                       for s in required)
        if self.metrics["bootstrapReads"] is None:
            self.metrics["bootstrapReads"] = self.gateway.read_count - cycle["reads"]
            self.metrics["bootstrapMs"] = round((time.monotonic() - cycle["started"]) * 1000)
            self.metrics["bootstrapComplete"] = complete
        elif complete:
            # A failed first pass must not permanently latch the bootstrap as
            # incomplete after a later recovery cycle finishes successfully.
            self.metrics["bootstrapComplete"] = True
        if cycle["mode"] != "interval" and complete:
            self.model.finish_activity_refresh()
            self.last_complete = self.last_sweep = now_ms()
            self.sweep_remaining = set(self._history_scopes())
            # A resumed recovery can settle without reading anything when all
            # retained scopes are complete. That preserves their completion
            # credit, but cannot resolve an unplaced feed change, whose only
            # evidence is the global correlation-gap warning. A later full
            # interval sweep (or a correlated read) clears that warning.
            if not cycle.get("resume", False):
                self.correlation_gaps.clear()
        elif cycle["mode"] == "interval" and not self.sweep_remaining and complete:
            self.last_complete = self.last_sweep = now_ms()
            self.correlation_gaps.clear()
        self.cycle = None
        complete = complete and not self.correlation_gaps
        if complete and self.connected:
            self._state("live")
        else:
            self._incomplete_state("incomplete read or disconnected feed")

    def _schedule_finish_time(self, item):
        wid = item["id"]
        marker = (item.get("state"), item.get("rowVersion"))
        if self.finish_trace_versions.get(wid) == marker:
            return
        self.finish_trace_versions[wid] = marker
        self.reads.touch("finish-time", wid)
        self.model.inventory("finish-time", wid).rows.clear()
        # A disposition advances rowVersion. An older version cannot hide a
        # finish inside the selector's largest (30-day) window. Unknown bounds
        # remain eligible; unrelated recent edits may conservatively over-read.
        version_at = latest_time(item.get("rowVersion"))
        could_be_visible = version_at is None or version_at >= now_ms() - 30 * 86_400_000
        if item.get("state") in {"closed", "iceboxed"} and could_be_visible:
            self.reads.schedule("finish-time", wid)

    def _request_item_assignments(self, wid, *, changed=False):
        if not self.model.hold_item_assignments(wid):
            return False
        # The held values are current facts, not a license to keep old closed
        # source text throughout repeated failures of the replacement read.
        self._forget_scopes(self.model.fold_closed_assignments(items={wid}))
        job = ("item-assignment-source", wid, None)
        if changed:
            self.reads.touch(*job[:2])
        if job not in self.read_retries:
            self.reads.schedule(*job, priority=True)
        self._incomplete_state("reading complete item assignment source")
        return True

    def _read_plan(self, resource, scope):
        if resource == "attest-owner":
            return ()
        if (resource == "turns" and self.model.closed_sources_released
                and scope not in {row["sessionKey"] for row in self.model.active_sessions()}):
            # Correlation needs no retained turn inventory. Creating one here
            # would also let retirement cleanup discard a failed detail retry.
            return ()
        current = self.model.current_ids(resource, scope)
        if resource != "item-assignment-source":
            return current
        item = self.model.work_items().get(scope, {})
        winners = {source[len("cli:attests "):] for facts in self.model.session_activity_evidence.values()
                   if facts.get("_owner") == scope for source in facts["source"]
                   if source.startswith("cli:attests ")}
        return {"liveIds": current, "activityWinners": winners,
                "visibleSessions": {row["sessionKey"] for row in self.model.active_sessions()},
                "attests": not (self._summary_contract_active() and has_work_item_summary(item)
                                 and not self._summary_needs_review_timing(item))}

    def _queue_attest_activity(self, assignment_id, author, timestamp):
        """Stage an already eligible-kind notice until ownership is known."""
        timestamp = activity_time(timestamp)
        if not assignment_id or not author or timestamp is None:
            return False
        pending = self.attest_activity_pending.get(assignment_id)
        if pending is not None:
            self.attest_activity_counts["coalesced"] += 1
        else:
            pending = {"authors": {}, "observedAt": now_ms(), "failedAt": None}
            self.attest_activity_pending[assignment_id] = pending
            self.attest_activity_counts["triggered"] += 1
        pending["authors"][author] = max(pending["authors"].get(author, 0), timestamp)
        job = ("attest-owner", assignment_id, None)
        if job not in self.read_retries:
            # Do not touch the epoch: notices arriving during this read share
            # its ownership result and retain their own authors/timestamps.
            self.reads.schedule(*job, priority=True)
        return True

    def _attest_activity_eligible(self, owner):
        if owner is None:
            return True
        item = self.model.work_items().get(owner)
        return item is not None and not (
            self._summary_contract_active() and has_work_item_summary(item)
            and not self._summary_needs_review_timing(item))

    def _attest_activity_owner_done(self, job, result):
        assignment_id = job[1]
        pending = self.attest_activity_pending.get(assignment_id)
        self.retry_in_flight.discard(job)
        if pending is None:
            result.rows.clear()
            self._forget_scopes({job[:2]})
            return False
        row = result.rows[0] if result.complete and len(result.rows) == 1 else None
        usable = (row is not None and row.get("id") == assignment_id
                  and "workItemId" in row
                  and (row["workItemId"] is None or
                       (isinstance(row["workItemId"], str) and bool(row["workItemId"]))))
        if not usable:
            if pending["failedAt"] is None:
                pending["failedAt"] = now_ms()
            self.attest_activity_counts["failures"] += 1
            self.read_retries[job] = (time.monotonic() + RETRY_MS / 1000, 2)
            self.retry_counters["scheduled"] += 1
            result.rows.clear()
            return True
        owner = row["workItemId"]
        item = self.model.work_items().get(owner)
        eligible = self._attest_activity_eligible(owner)
        # No read row escapes the resolution, including into an inventory.
        result.rows.clear()
        del row
        if eligible:
            facts = dict(status="known", freshness="live" if self.connected else "stale", complete=True,
                         observedAt=pending["observedAt"], unconfirmedSince=None,
                         source="cli:attests " + str(assignment_id))
            for author, timestamp in pending["authors"].items():
                self.model.fold_session_activity(author, timestamp, "attest", assignment_id, facts,
                                                 owner=owner)
        del self.attest_activity_pending[assignment_id]
        self.attest_activity_counts["completed"] += 1
        self._forget_scopes({job[:2]})
        if owner is not None and item is not None:
            # The ordinary item-source path still refreshes its board facts;
            # ownership resolution is not an item-publication substitute.
            self._request_item_assignments(owner, changed=True)
        return True

    def _item_assignments_done(self, job, result):
        self.retry_in_flight.discard(job)
        wid = job[1]
        if wid not in self.model.work_items():
            self.model.item_assignment_pending.discard(wid)
            self.model.item_assignment_facts.pop(wid, None)
            self.model.item_assignment_summary.pop(wid, None)
            self._forget_scopes({job[:2]})
            return True
        if result.complete:
            released = self.model.replace_item_assignments(wid, result)
            self._forget_scopes(released | {job[:2]})
            if not self.connected:
                self.model.item_assignment_facts[wid]["freshness"] = "stale"
            self._discover_assignment_sessions(result.folded.open_rows.values())
            self._discover_histories()
        else:
            self.read_failures.add(job[:2])
            self.read_retries[job] = (time.monotonic() + RETRY_MS / 1000, 2)
            self.retry_counters["scheduled"] += 1
            self.last_incomplete_read = self._read_outcome(*job, result)
            self._incomplete_state("item assignment source incomplete")
        if self.cycle:
            self._settle_cycle()
        elif self.connected and not self.read_failures and not self.model.item_assignment_pending:
            required = list(BASE_SCOPES) + self._history_scopes()
            if all(self.model.inventory(*s).complete and not self.model.inventory(*s).stale
                   for s in required):
                self._state("live")
        return True

    def _read_done(self, value):
        job, epoch, result = value
        resource, scope, key = job
        priority = self.reads.active_priorities.get(job[:2], 0)
        retry = job in self.retry_in_flight
        if not self.reads.finish(job, epoch):
            if retry:
                self.retry_counters["epochDiscarded"] += 1
                self.last_retry_outcome = {
                    "resource": resource, "readKind": self._read_kind(scope, key),
                    "result": "epoch-discarded", "at": now_ms(),
                }
            return False
        if resource == "retired-turns":
            return self._retired_activity_done(job, result)
        if resource == "attest-owner":
            return self._attest_activity_owner_done(job, result)
        if resource == "item-assignment-source":
            return self._item_assignments_done(job, result)
        assignments = self.model.inventory("assignments")
        if resource == "assignments" and key is None:
            if not result.complete and not result.rows:
                # No data identifies an item. Keep the complete source facts
                # intact and expose availability through existing board health.
                self.assignment_source_unavailable = True
                self.read_failures.add(job[:2])
                self.last_incomplete_read = self._read_outcome(*job, result)
                self.read_retries[job] = (time.monotonic() + RETRY_MS / 1000, priority)
                self.retry_counters["scheduled"] += 1
                if retry:
                    self.retry_in_flight.discard(job)
                    self.retry_counters["incomplete"] += 1
                    self.last_retry_outcome = self.last_incomplete_read
                LOG.warning("assignments source unavailable error=%s", self._error_category(result.error))
                self._state("stale")
                return True
            if result.complete and self.assignment_source_unavailable:
                self.assignment_source_unavailable = False
                LOG.info("assignments source recovered")
        if (resource == "assignments" and key is not None and self.model.closed_sources_released
                and (key not in assignments.rows or assignments.rows[key].get("workItemId") is not None)):
            row = result.rows[0] if result.complete and len(result.rows) == 1 else None
            wid = (row or assignments.rows.get(key, {})).get("workItemId")
            if wid and self._request_item_assignments(wid, changed=True):
                self.read_retries.pop(job, None)
                self.retry_in_flight.discard(job)
            elif wid and self.model.inventory("work-items").complete:
                # A successfully correlated assignment outside the current
                # server item set has no pending item source to repair.
                self.read_retries.pop(job, None)
                self.retry_in_flight.discard(job)
            elif row is not None and "workItemId" in row and row["workItemId"] is None:
                self.read_retries.pop(job, None)
                self.retry_in_flight.discard(job)
            else:
                # Discarded IDs are correlated through this existing detail
                # route; an unavailable owner is retried, never globally scanned.
                self.read_retries[job] = (time.monotonic() + RETRY_MS / 1000, priority)
                self.last_incomplete_read = self._read_outcome(*job, result)
            return True
        if resource == "attests" and scope is not None:
            owner = assignments.rows.get(scope, {}).get("workItemId")
            if owner and (not result.complete or owner in self.model.item_assignment_pending):
                self.last_incomplete_read = self._read_outcome(*job, result)
                self._request_item_assignments(owner, changed=True)
                if (resource, scope) in self.model.inventories:
                    self.model.inventories[resource, scope].invalidate()
                # The complete item read replaces this scope too. Its retry
                # owns the incompleteness, without retaining the partial rows.
                self._forget_scopes({job[:2]})
                return True
            if self.model.closed_sources_released and scope not in assignments.rows:
                self._refresh("assignments", key=scope)
                return True
        if resource == "assignments" and key is None and not result.complete:
            # Unidentified items retain their last complete current facts; only
            # the contributors present in this partial response need rereads.
            for wid in self.model.work_items():
                self.model.item_assignment_facts.setdefault(wid, assignments.evidence())
            affected = {row.get("workItemId") for row in result.rows}
            for wid in affected:
                self._request_item_assignments(wid, changed=True)
            # Record the global failure independently. None of this partial
            # collection may overwrite an item's last complete source facts.
            result.rows = []
        elif resource == "assignments" and key is None and result.complete:
            for wid in tuple(self.model.item_assignment_pending):
                self._request_item_assignments(wid, changed=True)
        if (resource == "assignments" and key is not None and result.rows
                and result.rows[0].get("workItemId") in self.model.item_assignment_pending):
            self._request_item_assignments(result.rows[0]["workItemId"])
            return True
        if (resource == "artifacts" and key is not None
                and self.model.inventory(resource, scope).released):
            self._refresh(resource, scope)
            return True
        if (resource in {"turns", "wakes", "messages"} and scope is not None
                and self.model.inventory("sessions").complete
                and self.model.inventory("assignments").complete
                and scope not in {row["sessionKey"] for row in self.model.active_sessions()}):
            # A read dispatched before retirement cannot recreate its scope.
            if resource == "turns" and key is not None and self.model.closed_sources_released:
                if not result.complete:
                    self.read_failures.add(job[:2])
                    self.read_retries[job] = (time.monotonic() + RETRY_MS / 1000, priority)
                    self.last_incomplete_read = self._read_outcome(*job, result)
                    self._incomplete_state("retired turn correlation incomplete")
                    return True
                # The turn detail carries the assignment identity even after
                # its raw assignment row was released. Resolve only that
                # assignment, then use the owning item's complete read path.
                for row in result.rows:
                    if row.get("assignmentId"):
                        self._refresh("assignments", key=row["assignmentId"])
                self.read_retries.pop(job, None)
                self.retry_in_flight.discard(job)
                self.read_failures.discard(job[:2])
                self.reads.epochs.pop(job[:2], None)
                return True
            if resource == "turns":
                from atc_summary import ItemActivityFold
                mapping = {row["id"]: row.get("workItemId") for row in
                           self.model.inventory("assignments").rows.values()}
                if result.folded is not None:
                    activity = {}
                    for aid, at in result.folded.summary["assignmentActivity"].items():
                        wid = mapping.get(aid)
                        if wid:
                            activity[wid] = max(activity.get(wid, 0), at)
                else:
                    fold = ItemActivityFold(mapping)
                    fold.add(result.rows)
                    activity = fold.activity
                self.model.fold_retired_activity(activity)
            if not result.complete:
                # Partial late reads carry positive facts, but cannot certify
                # the final activity of the retired holder.
                self.retired_activity_marker = None
                if job[:2] in self.model.inventories:
                    self.model.inventories[job[:2]].invalidate()
                self._ensure_retired_activity()
            self.reads.epochs.pop(job[:2], None)
            self.read_retries.pop(job, None)
            self.retry_in_flight.discard(job)
            self.read_failures.discard(job[:2])
            if self.cycle:
                self.cycle["pending"].discard(job[:2])
                self.cycle["seen"].discard(job[:2])
                self._settle_cycle()
            return True
        if resource == "finish-time":
            # Optional enrichment: a missing route uses the labeled activity
            # fallback rather than blocking the core inventory's freshness.
            self.model.accept(resource, scope, result)
            return True
        if resource == "work-items" and supports_work_item_summary(self.version):
            invalid = [row for row in result.rows if not has_work_item_summary(row)]
            if invalid:
                result = Read(
                    [row for row in result.rows if has_work_item_summary(row)], False,
                    result.at, result.source, "work-item-summary-incomplete", result.page)
        if retry:
            self.retry_in_flight.discard(job)
            self.retry_counters["completed" if result.complete else "incomplete"] += 1
            self.last_retry_outcome = self._read_outcome(resource, scope, key, result)
        if not result.complete:
            self.last_incomplete_read = self._read_outcome(resource, scope, key, result)
            LOG.warning("published read incomplete resource=%s readKind=%s page=%s error=%s",
                        resource, self._read_kind(scope, key), result.page,
                        self._error_category(result.error))
        if resource in HISTORIES and scope is None:
            # Correlation reads establish where the scoped read belongs. They
            # never establish completeness of an unknown history inventory.
            field = "assignmentId" if resource == "attests" else "sessionKey"
            found = result.rows[0].get(field) if result.complete and result.rows else None
            if found:
                self.correlation_gaps.discard((resource, key))
                self.reads.touch(resource, found)
                if resource == "attests":
                    self._refresh(resource, found)
                else:
                    self.reads.schedule(resource, found, key, priority=True)
            else:
                self._state("stale", "uncorrelated " + resource + " change")
            return True
        summary_was_active = self._summary_contract_active() if resource == "work-items" else None
        if key is None:
            activity_eligible = True
            if resource == "attests":
                assignment = assignments.rows.get(scope)
                activity_eligible = (assignment is not None and "workItemId" in assignment
                                     and self._attest_activity_eligible(assignment["workItemId"]))
            self.model.accept(resource, scope, result, activity_eligible=activity_eligible)
        else:
            self.model.detail(resource, scope, result)
        if resource == "turns" and scope is not None and result.complete:
            if self.model.closed_sources_released:
                for turn in self.model.summary("turns", scope)["live"]:
                    aid = turn.get("assignmentId")
                    if (aid and aid not in self.model.inventory("assignments").rows
                            and aid not in self.model.live_assignment_items):
                        self._refresh("assignments", key=aid)
            wakes = self.model.inventory("wakes", scope)
            if self.model.current_ids("wakes", scope) - wakes.rows.keys():
                self._refresh("wakes", scope)
        if resource == "work-items" and result.complete:
            for row in result.rows:
                if row.get("id") in self.model.item_assignment_pending:
                    self._request_item_assignments(row["id"], changed=True)
            summary_changed = summary_was_active != self._summary_contract_active()
            if key is not None and summary_changed:
                # An implicit contract change affects the full item scope.
                # Confirm that collection so the changed item's provenance
                # and the other items' scope describe the same fresh read.
                self._refresh("work-items")
            if key is None or summary_changed:
                # Without a reported version, summary mode depends on every
                # item's validity. One detail can change other items' scope.
                self._reconcile_work_item_attests()
                self._discover_histories()
            else:
                row = next((row for row in result.rows if row.get("id") == key), None)
                if row:
                    self._reconcile_item_attests(row)
        if not self.connected:
            self.model.inventory(resource, scope).invalidate()
        if result.complete:
            self.read_failures.discard(job[:2])
            if key is None:
                self.read_retries.pop(job, None)
                self.sweep_remaining.discard(job[:2])
        else:
            self.read_failures.add(job[:2])
            self._incomplete_state("published read incomplete")
            if key is None:
                # Keep a failed collection in the current recovery cycle and
                # retry it after a bounded pause instead of waiting for the
                # next full sweep to revisit the scope.
                self.read_retries[job] = (time.monotonic() + RETRY_MS / 1000, priority)
                self.retry_counters["scheduled"] += 1
        if ((resource == "assignments" and self.model.inventory("sessions").complete)
                or (resource == "sessions" and key is None and result.complete)):
            self._discover_assignment_sessions(self.model.inventory("assignments").rows.values())
        if resource in {"assignments", "sessions"} and (
                not self.cycle or self.cycle["expanded"]):
            # Assignment creation or a newly active session can introduce
            # scopes before the next interval. Existing complete scopes stay.
            for nested, holder in self._history_scopes():
                inventory = self.model.inventory(nested, holder)
                if inventory.observed_at is None:
                    if self.cycle:
                        self._cycle_add([(nested, holder)])
                    else:
                        self._refresh(nested, holder)
        if resource == "assignments" and result.complete:
            self._reconcile_work_item_attests()
        if resource in {"work-items", "assignments", "sessions"} and result.complete:
            self._ensure_retired_activity()
        if self.cycle and key is None:
            if result.complete or self.cycle["mode"] == "interval":
                self.cycle["pending"].discard(job[:2])
            self.cycle["complete"] = self.cycle["complete"] and result.complete
            self._settle_cycle()
        elif not self.cycle and self.connected and not self.read_failures and not self.correlation_gaps:
            required = list(BASE_SCOPES) + self._history_scopes()
            if all(self.model.inventory(*s).complete and not self.model.inventory(*s).stale
                   for s in required):
                self._state("live")
        return True

    def _discover_assignment_sessions(self, assignments):
        # The pending collection already reads these identities. Its completion
        # checks missing holders again; queuing details now duplicates the read
        # and can overwrite collection evidence with an older response.
        collection = ("sessions", None, None)
        active = self.reads.active.get(collection[:2])
        if collection in self.reads.pending or (active and active[0] == collection):
            return
        known = self.model.inventory("sessions").rows
        for assignment in assignments:
            holder = assignment.get("holderKey")
            if assignment.get("state") == "open" and holder and holder not in known:
                self._refresh("sessions", key=holder)

    @staticmethod
    def _read_kind(scope, key):
        if key is not None:
            return "detail"
        return "scoped-history" if scope is not None else "collection"

    @staticmethod
    def _error_category(error):
        return error if isinstance(error, str) and error.isidentifier() else "read-failed"

    def _read_outcome(self, resource, scope, key, result):
        return {"resource": resource, "readKind": self._read_kind(scope, key),
                "page": result.page, "complete": result.complete,
                "error": self._error_category(result.error), "at": result.at}

    def _resolve_merges(self):
        if self.resolver is None:
            return
        # The resolver owns its cache; the rendering path never starts Git.
        snapshot = self.current
        for item in snapshot["items"]:
            if item.get("landings"):
                continue
            refs = item["commitRefs"]
            previous = self.model.merges.get(item["id"])
            if item["code"] is not True or not refs:
                continue
            last_attempt = (previous.get("lastAttemptAt", previous["observedAt"])
                            if previous else None)
            if (previous and previous.get("lastAttemptRefs", previous["refs"]) == refs
                    and previous.get("lastAttemptComplete", True) == item.get("commitRefsComplete", True)
                    and now_ms() - last_attempt < self.rebuild_interval * 1000):
                continue
            pending = item["id"] in self.merge_pending
            # One queued/running check per item. Updates replace its desired
            # inventory; the worker/result handler reconciles to that inventory.
            self.merge_pending[item["id"]] = deepcopy(refs)
            self.merge_pending_complete[item["id"]] = item.get("commitRefsComplete", True)
            if not pending:
                self.merge_jobs.put((item["id"], deepcopy(refs)))

    def _next_merge_retry_at(self):
        """Return when an unresolved merge observation can be checked again."""
        if self.resolver is None:
            return None
        deadlines = []
        for item in self.current.get("items", []):
            if item.get("landings"):
                continue
            if item.get("code") is not True or not item.get("commitRefs"):
                continue
            item_id, refs = item["id"], item["commitRefs"]
            if self.merge_pending.get(item_id) == refs:
                continue
            previous = self.model.merges.get(item_id)
            if (previous and previous.get("refs") == refs
                    and previous.get("value") is None
                    and isinstance(previous.get("observedAt"), (int, float))):
                deadlines.append(previous["observedAt"] + self.rebuild_interval * 1000)
        return min(deadlines, default=None)

    def _git_worker(self, resolver=None):
        resolver = resolver or self.resolver
        while not self.stop_event.is_set():
            job = self.merge_jobs.get()
            if job is None:
                return
            wid, refs = job
            with self.model_lock:
                if wid not in self.merge_pending:
                    continue
                refs = deepcopy(self.merge_pending[wid])
            started = time.monotonic()
            error_class = None
            try:
                value = resolver.resolve(refs)
            except Exception as error:
                error_class = type(error).__name__[:80]
                value = None
            if value is None:
                summary = resolver.diagnostic_summary() if isinstance(resolver, GitResolver) else {
                    "refs": [], "omittedRefs": len(refs)}
                data = {"item": str(wid)[:120], "worker": threading.current_thread().name[:80],
                        "elapsedMs": round((time.monotonic() - started) * 1000, 3),
                        "error": error_class, "inputCount": len(refs),
                        "inputDigest": reference_digest(refs), **summary}
                line = json.dumps(data, separators=(",", ":"))
                # Include a count when details are omitted. Keep the entire
                # warning well below the service journal's stream line limit.
                while len(line.encode("utf-8")) > 4000 and data["refs"]:
                    data["refs"].pop()
                    data["omittedRefs"] += 1
                    line = json.dumps(data, separators=(",", ":"))
                LOG.warning("merge check inconclusive %s", line)
            if isinstance(resolver, GitResolver):
                resolver.clear_diagnostics()
            landing_at = getattr(resolver, "landing_at", None)
            if not isinstance(landing_at, (int, float)) or isinstance(landing_at, bool):
                landing_at = None
            self.events.put(("merged", (wid, refs, value, now_ms(), landing_at)))

    def _refresh(self, resource, scope=None, key=None):
        self.metrics["targetedReads"] += 1
        if resource in HISTORIES and scope is not None:
            key = None  # Discarded identities require an authoritative scoped fold.
        if resource == "artifacts" and self.model.inventory(resource, scope).released:
            key = None
        self.reads.schedule(resource, scope, key, priority=True)

    def _apply_assignment_notice(self, notice, key, payload):
        if (notice.get("op") != "upsert" or not ASSIGNMENT_FIELDS <= payload.keys()
                or str(payload["id"]) != str(key)
                or notice.get("class") not in {
                    "assignment.opened", "assignment.closed", "assignment.reopened"}
                or not self.model.closed_sources_released or self.cycle):
            return False
        wid = payload["workItemId"]
        # A mutable assignment's timestamp-derived version is not strict
        # across reopening. This path applies ordered feed projections only
        # when no independently scheduled source result can overwrite them.
        # Concurrent item replacement still uses its held publication path.
        if (wid in self.model.item_assignment_pending
                or ("assignments", None) in self.reads.active
                or ("item-assignment-source", wid) in self.reads.active
                or ("attests", str(key)) in self.reads.active
                or any(job[:2] == ("attests", str(key))
                       for job in (*self.reads.pending, *self.read_retries))):
            return False
        released = self.model.apply_assignment_projection(
            payload, at=now_ms(), source="feed:" + notice["class"])
        if released is None:
            return False
        self._forget_scopes(released)
        self.metrics["payloads"] += 1
        self._discover_assignment_sessions((payload,))
        self._discover_histories()
        self._ensure_retired_activity()
        return True

    def _apply_released_attest_notice(self, notice, key, scope, payload):
        """Update established current provenance without re-reading members."""
        if (self.cycle or notice.get("op") != "upsert"
                or notice.get("class") not in CLASSES["attests"]
                or self.policies.get("attests") != "immutable"
                or not set(ATTEST_FIELDS) <= payload.keys()
                or str(payload["id"]) != str(key)
                or str(payload["assignmentId"]) != str(scope)):
            return False
        contribution = self.model.item_contributions.entries.get(str(scope))
        if contribution is None or contribution.state == "open":
            return False
        owner = contribution.item
        refs = notice.get("refs") or {}
        if any("workItemId" in value and value["workItemId"] != owner
               for value in (payload, refs)):
            return False
        if (owner in self.model.item_assignment_pending
                or ("assignments", None) in self.reads.active
                or ("item-assignment-source", owner) in self.reads.active
                or ("attests", str(scope)) in self.reads.active
                or any(job[:2] == ("attests", str(scope))
                       for job in (*self.reads.pending, *self.read_retries))):
            return False
        if not self._attest_activity_eligible(owner):
            # This source is excluded by the same complete item-summary
            # contract as fresh load. Its notice cannot make it eligible.
            return True
        if contribution.evidence.observed_at is None:
            return False
        if not self.model.update_closed_attest_contribution(str(scope), attest_row(payload)):
            return False
        self.metrics["payloads"] += 1
        return True

    def change(self, notice):
        resource = notice.get("resource")
        if resource not in RESOURCE_REFS:
            # Observational events do not imply that every inventory changed.
            return False
        payload, refs = notice.get("payload") or {}, notice.get("refs") or {}
        key = refs.get(RESOURCE_REFS[resource])
        normalized = "decision-requests" if resource == "decision requests" else resource
        policy = self.policies.get(resource)
        if resource == "attests":
            scope = payload.get("assignmentId") or refs.get("assignmentId")
        elif resource in {"turns", "wakes", "messages"}:
            scope = payload.get("sessionKey") or refs.get("sessionKey")
        else:
            scope = None
        if resource == "assignments" and self._apply_assignment_notice(notice, key, payload):
            return True
        if (resource == "attests" and notice.get("op") == "upsert"
                and payload.get("kind") and payload.get("bySession")
                and activity_time(payload.get("ts")) is not None and scope):
            held = self.model.inventory("assignments").rows.get(str(scope))
            contribution = self.model.item_contributions.entries.get(str(scope))
            if "workItemId" in payload:
                activity_owner, owner_known = payload["workItemId"], True
            elif "workItemId" in refs:
                activity_owner, owner_known = refs["workItemId"], True
            elif held is not None and "workItemId" in held:
                activity_owner, owner_known = held["workItemId"], True
            elif contribution is not None:
                # Raw disposal did not discard this current contributor's
                # established ownership. R20 resolution still handles an ID
                # absent from both current source forms.
                activity_owner, owner_known = contribution.item, True
            else:
                activity_owner, owner_known = None, False
            if not owner_known:
                return self._queue_attest_activity(str(scope), payload["bySession"], payload["ts"])
            if self._attest_activity_eligible(activity_owner):
                self.model.fold_session_activity(payload["bySession"], payload["ts"], "attest", scope,
                    dict(status="known", freshness="live" if self.connected else "stale", complete=True,
                         observedAt=now_ms(), unconfirmedSince=None,
                         source="cli:attests " + str(scope)), owner=activity_owner)
            if activity_owner is None and held is None:
                return True
        if resource == "attests" and self._apply_released_attest_notice(notice, key, scope, payload):
            return True
        owner = (payload.get("workItemId") or refs.get("workItemId") or
                 self.model.inventory("assignments").rows.get(
                     key if resource == "assignments" else scope, {}).get("workItemId"))
        if (resource in {"assignments", "attests"}
                and owner in self.model.item_assignment_pending):
            self._request_item_assignments(owner, changed=True)
            return True
        if (self.model.closed_sources_released and
                ((resource == "assignments" and
                  (key not in self.model.inventory("assignments").rows
                   or payload.get("state") not in {None, "open"}))
                 or (resource == "attests" and
                     scope not in self.model.inventory("assignments").rows))):
            if not self._request_item_assignments(owner, changed=True):
                aid = key if resource == "assignments" else scope
                if aid is not None:
                    self.reads.touch("assignments", None)
                    self._refresh("assignments", key=aid)
                elif key is not None:
                    self.correlation_gaps.add((resource, key))
                    self._refresh(resource, key=key)
            return True
        if (resource in {"turns", "wakes", "messages"} and scope is not None
                and self.model.inventory("sessions").complete
                and self.model.inventory("assignments").complete
                and scope not in {row["sessionKey"] for row in self.model.active_sessions()}):
            if resource == "turns" and self.model.closed_sources_released:
                if payload.get("assignmentId"):
                    self._refresh("assignments", key=payload["assignmentId"])
                elif key is not None:
                    # A detail read establishes assignment ownership; reading
                    # the whole session cannot recover released identities.
                    self.reads.touch(normalized, scope)
                    self.reads.schedule(normalized, scope, key, priority=True)
                else:
                    self.correlation_gaps.add((normalized, key))
                    self._state("stale", "uncorrelated turns change")
                return True
            linked = any(row.get("holderKey") == scope and row.get("workItemId") in
                         self.model.work_items() for row in
                         self.model.inventory("assignments").rows.values())
            if resource != "turns" or not linked:
                return False
        if resource in {"attests", "turns", "wakes", "messages"} and not scope:
            self.correlation_gaps.add((normalized, key))
            # No global scan for an actual correlation gap. Messages have no
            # global detail route; a missing session stays explicitly unknown.
            if key is not None and resource != "messages":
                self.reads.touch(normalized, None)
                self._refresh(normalized, key=key)
            self._state("stale", "uncorrelated " + resource + " change")
            return True
        self.reads.touch(normalized, scope)
        inventory = self.model.inventory(normalized, scope)
        required = required_fields(resource, payload, ATTEST_FIELDS, self.version)
        if (resource == "work-items"
                and (supports_work_item_summary(self.version)
                     or any(field in payload for field in WORK_ITEM_SUMMARY_FIELDS))
                and not has_work_item_summary(payload)):
            required = None
        applied = False
        if (policy and notice.get("op") == "upsert" and required is not None
                and required <= payload.keys() and notice.get("class") in CLASSES.get(resource, ())
                and str(payload.get(inventory.key)) == str(key)):
            row = attest_row(payload) if resource == "attests" else payload
            previous_revision = inventory.data_revision
            if resource == "work-items":
                summary_was_active = self._summary_contract_active()
                applied = self.model.payload_work_item(
                    row, at=now_ms(), source="feed:" + notice["class"], policy=policy)
            else:
                applied = inventory.payload(row, at=now_ms(), source="feed:" + notice["class"],
                                            policy=policy)
            if applied:
                if resource == "turns":
                    self.model.fold_activity_inventory(resource, scope)
                self.metrics["payloads"] += 1
                if resource == "attests" and previous_revision != inventory.data_revision:
                    self.model.details_revision += 1
                if resource == "work-items":
                    if str(row["id"]) in self.model.item_assignment_pending:
                        self._request_item_assignments(str(row["id"]), changed=True)
                    if summary_was_active != self._summary_contract_active():
                        self._refresh("work-items")
                        self._reconcile_work_item_attests()
                        self._discover_histories()
                    else:
                        self._reconcile_item_attests(self.model.work_items()[str(row[inventory.key])])
        if not applied:
            if resource == "roles":
                self._refresh(normalized, scope)
            elif resource == "attests":
                assignment = self.model.inventory("assignments").rows.get(str(scope))
                item = (self.model.work_items().get(assignment.get("workItemId"))
                        if assignment else None)
                if (not self._summary_contract_active()
                        or not has_work_item_summary(item or {})
                        or self._summary_needs_review_timing(item)):
                    self._refresh(normalized, scope)
            elif key is not None:
                self._refresh(normalized, scope, key)
            else:
                inventory.invalidate()
                self._state("stale", "change missing primary reference")
        if resource == "sessions" and applied:
            self._discover_histories()
        if resource in {"sessions", "assignments", "work-items"} and applied:
            self._ensure_retired_activity()
        if resource == "attests":
            assignment = self.model.inventory("assignments").rows.get(str(scope))
            # Completion can close the holder, and a review verdict can settle
            # its producer. The attest projection contains neither new row.
            affected = set()
            if assignment is None or payload.get("kind") in {None, "completion"}:
                affected.add(str(scope))
            if payload.get("kind") in {None, "verdict"} and assignment and assignment.get("reviewsAssignmentId"):
                affected.add(assignment["reviewsAssignmentId"])
            if affected:
                self.reads.touch("assignments", None)
                for assignment_id in affected:
                    self._refresh("assignments", key=assignment_id)
        return True

    def _discover_histories(self):
        for resource, scope in self._history_scopes():
            if self.model.inventory(resource, scope).observed_at is None:
                if self.cycle:
                    self._cycle_add([(resource, scope)])
                else:
                    self._refresh(resource, scope)

    def _handle(self, kind, value):
        if kind == "ready":
            self.connected = True
            self.version = value
            self.policies = policies_for(value)
            self.payload_warning = payload_policy_warning(value)
            # Released assignment identities are no longer a usable discovery
            # source after an outage. Reload them before resolving missed work.
            self.rebuild(resume=self.cycle is not None and not self.model.closed_sources_released)
            self._ensure_retired_activity()
        elif kind == "lost":
            was_connected = self.connected
            self.connected = False
            changed = self._state("stale", value) or was_connected
            if was_connected:
                if self.model.retired_item_activity:
                    self.retired_activity_marker = None
                self.model.mark_unconfirmed(now_ms())
                # Reject only snapshots already in flight. Completed scopes
                # retain their data and completion credit, with the outage
                # timestamp carried independently of freshness and cleared by
                # their next complete read.
                for resource, scope in tuple(self.reads.active):
                    self.reads.touch(resource, scope)
                    if resource != "attest-owner":
                        self.model.inventory(resource, scope).invalidate()
                # Start or resume one recovery cycle. Repeated lost events do
                # not reset epochs or bypass retry deadlines.
                self.rebuild(resume=True)
                self._state("stale", value)
            return changed
        elif kind == "read":
            return self._read_done(value)
        elif kind == "detail-item-reload":
            self.reads.touch("work-items", None)
            self._refresh("work-items", key=value)
        elif kind == "detail-history":
            scope, epoch, result = value
            owner = self.model.inventory("assignments").rows.get(scope, {}).get("workItemId")
            if (self.model.closed_sources_released
                    and scope not in self.model.inventory("assignments").rows):
                self._refresh("assignments", key=scope)
            elif owner and (not result.complete or owner in self.model.item_assignment_pending):
                self._request_item_assignments(owner, changed=True)
            elif epoch == self.reads.epochs["attests", scope]:
                self.model.accept("attests", scope, result)
            else:
                self._refresh("attests", scope)
        elif kind == "detail-reload":
            wid, observed = value
            if observed is not None:
                previous, fingerprint, assignments = observed
                closed = self.model.closed_items.get(wid)
                if closed and closed["detailFingerprint"] == previous:
                    closed["detailFingerprint"] = fingerprint
                    closed["assignmentFingerprint"] = assignments
                    self.model.details_revision += 1
            self._request_item_assignments(wid, changed=True)
        elif kind == "change":
            if (isinstance(value, tuple) and len(value) == 2
                    and isinstance(value[0], dict)):
                notice, received_at = value
                self._record_feed_processed(received_at)
                return self.change(notice)
            return self.change(value)
        elif kind == "merged":
            wid, refs, merged, at = value[:4]
            landing_at = value[4] if len(value) > 4 else None
            if self.merge_pending.get(wid) != refs:
                if wid in self.merge_pending:
                    # The obsolete attempt has finished. Only now queue the
                    # latest inventory, so variants never occupy two workers.
                    self.merge_jobs.put((wid, deepcopy(self.merge_pending[wid])))
                return False
            self.merge_pending.pop(wid)
            refs_complete = self.merge_pending_complete.pop(wid, True)
            previous = self.model.merges.get(wid)
            if (previous and previous.get("value") is True and not refs_complete):
                merged = None
            if (merged is None and previous
                    and isinstance(merge_value_for_refs(previous, refs, refs_complete), bool)):
                # An unavailable recheck is not new merge evidence. Keep the
                # last definite answer and its observation time, but remember
                # the failed attempt separately so feed activity cannot cause
                # an immediate retry loop. The retained answer is visibly stale.
                stale = dict(previous, freshness="stale", lastAttemptAt=at,
                             lastAttemptRefs=deepcopy(refs), lastAttemptComplete=refs_complete)
                self.model.merges[wid] = stale
                self._persist_merge_result(wid, stale)
                return (previous.get("freshness") != "stale"
                        or previous.get("lastAttemptComplete", True) != refs_complete)
            if (merged is True and landing_at is None and previous
                    and previous.get("value") is True and previous.get("refs") == refs):
                landing_at = previous.get("landingAt")
            result = {"value": merged, "observedAt": at, "lastAttemptAt": at,
                      "refs": refs, "freshness": "live", "lastAttemptComplete": refs_complete,
                      **({"landingAt": landing_at} if merged is True and landing_at is not None else {})}
            self.model.merges[wid] = result
            self._persist_merge_result(wid, result)
        return True

    def _worker(self):
        while not self.stop_event.is_set():
            # Deadlines are recomputed once per maintenance pass, not per
            # event. A busy feed still reaches maintenance after a bounded batch.
            timeout = max(0, self._next_maintenance - time.monotonic())
            try:
                event = self.events.get(timeout=timeout)
            except queue.Empty:
                event = None
            with self.model_lock:
                for index in range(EVENT_BATCH_LIMIT):
                    if event is None:
                        break
                    kind, value = event
                    if kind == "stop":
                        return
                    try:
                        changed = self._handle(kind, value)
                        self._dirty = changed or self._dirty
                        if changed:
                            self._next_maintenance = min(self._next_maintenance,
                                                         self._last_publication + PUBLICATION_INTERVAL)
                    except Exception as error:
                        self._dirty = True
                        self._next_maintenance = min(self._next_maintenance,
                                                     self._last_publication + PUBLICATION_INTERVAL)
                        self.model.invalidate()
                        self._state("stale", "model update failed: " + type(error).__name__)
                    finally:
                        # A quiet worker must not keep its last Read (or feed
                        # payload) alive until the next event arrives.
                        event = value = None
                    if index == EVENT_BATCH_LIMIT - 1:
                        break
                    try:
                        event = self.events.get_nowait()
                    except queue.Empty:
                        event = None
                self._maintenance()
                self.reads.pump()

    def _maintenance(self):
        clock = time.monotonic()
        if clock < self._next_maintenance:
            return
        self._retry_incomplete_reads()
        if self.cycle is None and clock - self.last_rebuild >= self.rebuild_interval:
            self.rebuild(recovery=self.metrics["bootstrapReads"] is None)
            self._dirty = True
        at = now_ms()
        expired = any(expiry is not None and expiry <= at for expiry in (
            self.model.next_expiry, self.model.next_raw_expiry))
        merge_retry_at = self._next_merge_retry_at()
        merge_retry_due = merge_retry_at is not None and merge_retry_at <= at
        self._dirty = self._update_backstop(at) or self._dirty or expired or merge_retry_due
        if self.connected:
            self.backstop.schedule(self.model, self.reads, at)
        if self._dirty:
            self._publish(classified=True)
            self._resolve_merges()
        delay = self._wait_timeout()
        # Coalesce bursts and read completions, including bootstrap/recovery.
        # Freshness/mark deadlines remain visible even without a feed event.
        self._next_maintenance = time.monotonic() + max(
            PUBLICATION_INTERVAL, delay if delay is not None else self.rebuild_interval)

    def _wait_timeout(self):
        deadlines = []
        if self.cycle is None:
            deadlines.append(self.last_rebuild + self.rebuild_interval - time.monotonic())
        for expiry in (self.model.next_expiry, self.model.next_raw_expiry):
            if expiry is not None:
                deadlines.append((expiry - now_ms()) / 1000)
        backstop_deadline = self.backstop.deadline(self.model, self.reads, now_ms(), self.connected)
        if backstop_deadline is not None:
            deadlines.append((backstop_deadline - now_ms()) / 1000)
        merge_retry_at = self._next_merge_retry_at()
        if merge_retry_at is not None:
            deadlines.append((merge_retry_at - now_ms()) / 1000)
        deadlines.extend(due - time.monotonic()
                         for due, _ in self.read_retries.values())
        return max(0, min(deadlines)) if deadlines else None

    def _feed(self):
        from websockets.sync.client import connect
        while not self.stop_event.is_set():
            try:
                version = self.gateway.version()
                url = self.gateway.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                with connect(url + "/ws/changes?protocolVersion=2", proxy=None,
                             ping_interval=None, max_size=None) as connection:
                    self.gateway.authenticate_feed(connection)
                    auth = json.loads(connection.recv(timeout=30))
                    if auth.get("type") != "auth_result" or auth.get("success") is not True:
                        raise ValueError("feed authentication rejected")
                    connection.send(json.dumps({
                        "type": "subscribe", "subscriptionId": "atc",
                        "protocolVersion": 2, "filters": {},
                    }))
                    ready = json.loads(connection.recv(timeout=30))
                    if ready.get("type") != "subscription_ready":
                        raise ValueError("feed subscription rejected")
                    cursor = FeedCursor(time.monotonic())
                    self.events.put(("ready", version))
                    while not self.stop_event.is_set():
                        message = json.loads(connection.recv(timeout=cursor.remaining(time.monotonic())))
                        kind = message.get("type")
                        cursor.accept(message, time.monotonic())
                        if kind == "change":
                            self._queue_feed_change(message)
            except Exception as error:
                # Exception bodies can contain server responses. The category
                # and close code suffice for recovery and never expose tokens.
                code = getattr(getattr(error, "rcvd", None), "code", None)
                reason = "feed disconnected: " + type(error).__name__
                if code is not None:
                    reason += " (" + str(code) + ")"
                self.events.put(("lost", reason))
            self.stop_event.wait(1)
