"""One upstream feed, recoverable read model, and snapshot/delta subscriptions."""

from collections import deque
from copy import deepcopy
import json
import logging
import queue
import threading
import time
import uuid

from atc_model import EvidenceModel
from atc_evidence import combined_evidence
from atc_reads import ReadQueue
from atc_reconciliation import ActiveBackstop, RETRY_MS
from atc_upstream import ATTEST_FIELDS, attest_row, now_ms


# Per-resource field and ordering policies come from the audited published build.
# Runtime applies them across Gateway builds, with an explicit warning when the
# connected identity differs; this is not runtime/feed acceptance evidence.
from atc_payloads import (
    WORK_ITEM_FIELDS, CLASSES, required_fields, policies_for, payload_policy_warning,
)

BASE_SCOPES = (("work-items", None), ("sessions", None), ("assignments", None),
               ("roles", None), ("decision-requests", None), ("artifacts", None))
HISTORIES = {"attests", "turns", "wakes", "messages"}
PUBLICATION_INTERVAL = 0.25
EVENT_BATCH_LIMIT = 128

COLLECTIONS = ("agents", "items", "links", "wakes", "messages")
LOG = logging.getLogger("atc.recovery")

RESOURCE_REFS = {
    "work-items": "workItemId", "assignments": "assignmentId",
    "attests": "attestId", "sessions": "sessionKey", "turns": "turnSeq",
    "wakes": "wakeId", "roles": "role", "artifacts": "artifactId",
    "messages": "messageId", "decision requests": "decisionRequestId",
}


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
        self.generation, self.revision = uuid.uuid4().hex, 0
        self.rebuild_interval, self.workers = rebuild_interval, workers
        self.events = queue.Queue()
        self.reads = ReadQueue(gateway, self.events, workers)
        self.backstop = ActiveBackstop()
        self.scope_budget = scope_budget
        self.rotation = deque()
        self.cycle = None
        self.last_sweep = None
        self.sweep_remaining = set()
        self.read_failures = set()
        self.read_retries = {}
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
        self.merge_pending = {}
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
        self.delta = None
        self.delta_base = None
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
        """All subscribers share the immutable bytes encoded at publication."""
        with self.condition:
            self.condition.wait_for(
                lambda: self.revision != revision or self.stop_event.is_set(), timeout)
            if self.revision == revision:
                return None
            body = self.delta if revision == self.delta_base else self.event
            return self.revision, body

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
        # Model work does not hold the lock serving committed SSE/snapshots.
        # During coalescing, retry instead of labelling newer rows with an old
        # revision. The next publication makes the page and its pin consistent.
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
            item = work.rows.get(item_id)
            if item is None:
                return (404 if work.complete else 503), {"error": "item-unavailable", **pin}
            assignments = self.model.inventory("assignments")
            owned = [a for a in assignments.rows.values() if a.get("workItemId") == item_id]
            scopes = [self.model.inventory("attests", a["id"]) for a in owned]
            facts = combined_evidence(work.evidence(), assignments.evidence(),
                                      *(scope.evidence() for scope in scopes))
            rows = sorted((row for scope in scopes for row in scope.rows.values()),
                          key=lambda row: (row.get("ts") or 0, row["id"]))
            end = offset + limit
            return 200, deepcopy({
                **pin, "itemId": item_id, "item": item, "assignments": owned,
                "records": [dict(row, bySession=row.get("bySession"), byUser=row.get("byUser"),
                                 note=row.get("note")) for row in rows[offset:end]],
                "total": len(rows), "nextOffset": end if end < len(rows) else None,
                "complete": facts["complete"], "freshness": facts,
            })

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
            targets = (self._worker, self._git_worker)
        else:
            targets = (self._worker, self._feed, self._git_worker)
        for target in targets:
            threading.Thread(target=target, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.events.put(("stop", None))
        self.merge_jobs.put(None)
        self.reads.close()
        with self.condition:
            self.condition.notify_all()

    def _state(self, state, reason=None):
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
        if not classified:
            self._update_backstop(at)
        data = deepcopy(self.model.snapshot(at))
        self._retain_unproven_removals(data, at)
        old_agents = {row["id"]: row for row in self.current.get("agents", [])}
        for agent in data["agents"]:
            fact = agent["evidence"]["idleFor"]
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
            return False
        previous = self.current
        revision = self.revision + 1
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
        delta = self._encode("delta", dict(metadata, baseRevision=self.revision, changes=changes))
        with self.condition:
            previous = self.current
            self.delta_base = self.revision
            self.revision = revision
            self.current = current
            self._comparison = comparison
            self._published_details = self.model.details_revision
            self._full_event = None
            self.delta = delta
            self.condition.notify_all()
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
        scopes = [("attests", a["id"])
                for a in self.model.inventory("assignments").rows.values()] + [
                    (resource, session["sessionKey"])
                    for session in self.model.active_sessions()
                    for resource in ("turns", "wakes", "messages")]
        # Previously visible sessions remain in terminal reconciliation after
        # retirement; losing visibility does not establish historical absence.
        scopes.extend((resource, scope) for resource, scope in self.model.inventories
                      if resource in {"turns", "wakes", "messages"} and scope is not None)
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
        histories = len(self._history_scopes())
        terminal = len(self.backstop.history)
        return {"mode": cycle["mode"] if cycle else None,
                "pendingScopes": len(cycle["pending"]) if cycle else 0,
                "totalScopes": len(cycle["seen"]) if cycle else 0,
                "scopeBudget": self.scope_budget,
                "historyScopes": histories,
                "estimatedSweepMs": ((terminal + self.scope_budget - 1) // self.scope_budget
                                     * self.rebuild_interval * 1000),
                "lastSweepAt": self.last_sweep,
                "readDiagnostics": self._read_diagnostics(),
                **self.backstop.metadata(self.model, self.connected)}

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
                          "active": len(self.reads.active)},
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

    def rebuild(self, *, recovery=True):
        """Schedule work, never perform upstream I/O on the model worker.

        Bootstrap and a lost feed require all scopes. Normal intervals rotate
        through a bounded number of histories and always read base inventories.
        A collection still establishes absence only after every page succeeds.
        """
        if self.cycle is not None:
            if recovery:
                self.cycle["mode"] = "recovery"
                self.cycle["expanded"] = False
                self.model.invalidate()
                for resource, scope in self.model.inventories:
                    self.reads.touch(resource, scope)
                self._cycle_add(BASE_SCOPES, repeat=True)
            return
        mode = ("bootstrap" if self.metrics["bootstrapReads"] is None else "recovery") if recovery else "interval"
        self.cycle = {"mode": mode, "pending": set(), "seen": set(),
                      "expanded": False, "started": time.monotonic(),
                      "reads": self.gateway.read_count, "complete": True}
        if recovery:
            self.model.invalidate()
        self._state("rebuilding", "reading published state: " + mode)
        self._cycle_add(BASE_SCOPES)
        self.metrics["rebuilds"] += 1
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
        scopes = self._history_scopes()
        if cycle["mode"] != "interval":
            self._cycle_add(scopes, repeat=True)
            return
        needed = set(scopes)
        self.backstop.classify(self.model, scopes, now_ms())
        terminal = self.backstop.history
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
        if cycle["pending"] or not cycle["expanded"]:
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
            self.last_complete = self.last_sweep = now_ms()
            self.sweep_remaining = set(self._history_scopes())
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
                self._refresh(resource, found, None if resource == "attests" else key)
            else:
                self._state("stale", "uncorrelated " + resource + " change")
            return True
        if key is None:
            self.model.accept(resource, scope, result)
        else:
            self.model.detail(resource, scope, result)
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
            known_sessions = self.model.inventory("sessions").rows
            for assignment in self.model.inventory("assignments").rows.values():
                holder = assignment.get("holderKey")
                if assignment.get("state") == "open" and holder and holder not in known_sessions:
                    self._refresh("sessions", key=holder)
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
            refs = item["commitRefs"]
            previous = self.model.merges.get(item["id"])
            if item["code"] is not True or not refs:
                continue
            if previous and previous["refs"] == refs and now_ms() - previous["observedAt"] < self.rebuild_interval * 1000:
                continue
            if self.merge_pending.get(item["id"]) == refs:
                continue
            self.merge_pending[item["id"]] = deepcopy(refs)
            self.merge_jobs.put((item["id"], deepcopy(refs)))

    def _git_worker(self):
        while not self.stop_event.is_set():
            job = self.merge_jobs.get()
            if job is None:
                return
            wid, refs = job
            try:
                value = self.resolver.resolve(refs)
            except Exception:
                value = None
            self.events.put(("merged", (wid, refs, value, now_ms())))

    def _refresh(self, resource, scope=None, key=None):
        self.metrics["targetedReads"] += 1
        self.reads.schedule(resource, scope, key, priority=True)

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
            scope = payload.get("assignmentId")
        elif resource in {"turns", "wakes", "messages"}:
            scope = payload.get("sessionKey") or refs.get("sessionKey")
        else:
            scope = None
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
        required = required_fields(resource, payload, ATTEST_FIELDS)
        applied = False
        if (policy and notice.get("op") == "upsert" and required is not None
                and required <= payload.keys() and notice.get("class") in CLASSES.get(resource, ())
                and str(payload.get(inventory.key)) == str(key)):
            row = attest_row(payload) if resource == "attests" else payload
            previous = inventory.rows.get(str(row[inventory.key]))
            applied = inventory.payload(row, at=now_ms(), source="feed:" + notice["class"],
                                        policy=policy)
            if applied:
                self.metrics["payloads"] += 1
                if resource in {"work-items", "attests"} and previous != inventory.rows.get(str(row[inventory.key])):
                    self.model.details_revision += 1
        if not applied:
            if resource in {"roles", "attests"}:
                self._refresh(normalized, scope)
            elif key is not None:
                self._refresh(normalized, scope, key)
            else:
                inventory.invalidate()
                self._state("stale", "change missing primary reference")
        if resource == "sessions" and applied:
            self._discover_histories()
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
            self.rebuild()
        elif kind == "lost":
            was_connected = self.connected
            self.connected = False
            changed = self._state("stale", value) or was_connected
            if was_connected:
                if self.cycle is None:
                    self.model.invalidate()
                    for resource, scope in self.model.inventories:
                        self.reads.touch(resource, scope)
                # Start recovery once per outage. Repeated lost notifications
                # can follow local read-state changes; they must not invalidate
                # epochs or bypass an incomplete read's retry deadline.
                self.rebuild()
                self._state("stale", value)
            return changed
        elif kind == "read":
            return self._read_done(value)
        elif kind == "change":
            if (isinstance(value, tuple) and len(value) == 2
                    and isinstance(value[0], dict)):
                notice, received_at = value
                self._record_feed_processed(received_at)
                return self.change(notice)
            return self.change(value)
        elif kind == "merged":
            wid, refs, merged, at = value
            if self.merge_pending.get(wid) != refs:
                return False
            self.merge_pending.pop(wid)
            self.model.merges[wid] = {"value": merged, "observedAt": at, "refs": refs}
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
        expired = self.model.next_expiry is not None and self.model.next_expiry <= at
        self._dirty = self._update_backstop(at) or self._dirty or expired
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
        if self.model.next_expiry is not None:
            deadlines.append((self.model.next_expiry - now_ms()) / 1000)
        backstop_deadline = self.backstop.deadline(self.model, self.reads, now_ms(), self.connected)
        if backstop_deadline is not None:
            deadlines.append((backstop_deadline - now_ms()) / 1000)
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
