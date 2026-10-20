"""One upstream feed, recoverable read model, and full-model subscriptions."""

from collections import deque
from copy import deepcopy
import json
import queue
import threading
import time
import uuid

from atc_model import EvidenceModel
from atc_reads import ReadQueue
from atc_upstream import ATTEST_FIELDS, attest_row, now_ms


# Enabled only after source review and real-feed verification of this build.
# A different gateway build uses targeted reads until its guarantees are known.
PAYLOAD_BUILDS = {}
WORK_ITEM_FIELDS = frozenset((
    "id", "title", "specRefName", "specRefSha256", "isBug", "ownerUserId",
    "state", "failReason", "routingWakeId", "slateWakeId", "createdByUser",
    "createdBySession", "createdInTurnSeq", "createdContextKnown", "createdAt",
    "priority", "rowVersion",
))
BASE_SCOPES = (("work-items", None), ("sessions", None), ("assignments", None),
               ("roles", None), ("decision-requests", None), ("artifacts", None))
HISTORIES = {"attests", "turns", "wakes", "messages"}

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
        self.scope_budget = scope_budget
        self.rotation = deque()
        self.cycle = None
        self.last_sweep = None
        self.sweep_remaining = set()
        self.read_failures = set()
        self.correlation_gaps = set()
        self.merge_jobs = queue.Queue()
        self.merge_pending = {}
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.connected = False
        self.state, self.since, self.reason = "stale", now_ms(), "initial state unavailable"
        self.last_complete, self.last_rebuild = None, 0
        self.version = None
        self.policies = {}
        self.metrics = {"bootstrapReads": None, "bootstrapMs": None, "payloads": 0,
                        "bootstrapComplete": None, "targetedReads": 0, "rebuilds": 0}
        self.current = {}
        self._comparison = None
        self.event = None
        self._publish()

    def snapshot(self):
        with self.condition:
            return deepcopy(self.current)

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
            return (self.revision, self.event) if self.revision != revision else None

    def start(self):
        # Fail startup visibly when the runtime dependency isn't installed.
        from websockets.sync.client import connect  # noqa: F401
        for target in (self._worker, self._feed, self._git_worker):
            threading.Thread(target=target, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.events.put(("stop", None))
        self.merge_jobs.put(None)
        self.reads.close()
        with self.condition:
            self.condition.notify_all()

    def _state(self, state, reason=None):
        if (self.state, self.reason) != (state, reason):
            self.state, self.reason, self.since = state, reason, now_ms()
            return True
        return False

    def _publish(self):
        # A published revision is immutable even while the worker updates its
        # inventories outside the subscribers' lock.
        at = now_ms()
        data = deepcopy(self.model.snapshot(at))
        candidate = {
            "schemaVersion": 1, "generation": self.generation,
            "freshness": {
                "state": self.state, "since": self.since, "reason": self.reason,
                "rebuildIntervalMs": self.rebuild_interval * 1000,
                "lastCompleteAt": self.last_complete,
                "reconciliation": self._reconciliation(),
            },
            "gatewayVersion": self.version, **data,
        }
        # idleFor is sampled at generatedAt; viewers advance it from idleSince.
        # A ticking clock or diagnostic counter alone is not a model change.
        comparison = dict(candidate, agents=[
            {key: value for key, value in agent.items() if key != "idleFor"}
            for agent in data["agents"]])
        if comparison == self._comparison:
            return False
        with self.condition:
            self.revision += 1
            self.current = dict(candidate, revision=self.revision, generatedAt=at,
                                metrics=dict(self.metrics))
            self._comparison = comparison
            body = json.dumps(self.current, separators=(",", ":"))
            self.event = ("event: model\ndata: " + body + "\n\n").encode()
            self.condition.notify_all()
        return True

    def _history_scopes(self):
        return [("attests", a["id"])
                for a in self.model.inventory("assignments").rows.values()] + [
                    (resource, session["sessionKey"])
                    for session in self.model.active_sessions()
                    for resource in ("turns", "wakes", "messages")]

    def _reconciliation(self):
        cycle = self.cycle
        histories = len(self._history_scopes())
        return {"mode": cycle["mode"] if cycle else None,
                "pendingScopes": len(cycle["pending"]) if cycle else 0,
                "totalScopes": len(cycle["seen"]) if cycle else 0,
                "scopeBudget": self.scope_budget,
                "historyScopes": histories,
                "estimatedSweepMs": ((histories + self.scope_budget - 1) // self.scope_budget
                                     * self.rebuild_interval * 1000),
                "lastSweepAt": self.last_sweep}

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
                self.reads.schedule(resource, scope)

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
        self.rotation = deque(s for s in self.rotation if s in needed)
        scheduled = set(self.rotation)
        self.rotation.extend(s for s in scopes if s not in scheduled)
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
        if cycle["mode"] != "interval" and complete:
            self.last_complete = self.last_sweep = now_ms()
            self.sweep_remaining = set(self._history_scopes())
            self.correlation_gaps.clear()
        elif cycle["mode"] == "interval" and not self.sweep_remaining and complete:
            self.last_complete = self.last_sweep = now_ms()
            self.correlation_gaps.clear()
        self.cycle = None
        complete = complete and not self.correlation_gaps
        self._state("live" if complete and self.connected else "stale",
                    None if complete and self.connected else "incomplete read or disconnected feed")

    def _read_done(self, value):
        job, epoch, result = value
        resource, scope, key = job
        if not self.reads.finish(job, epoch):
            return False
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
                self.sweep_remaining.discard(job[:2])
        else:
            self.read_failures.add(job[:2])
            self._state("stale", "published read incomplete")
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
            self.cycle["pending"].discard(job[:2])
            self.cycle["complete"] = self.cycle["complete"] and result.complete
            self._settle_cycle()
        elif not self.cycle and self.connected and not self.read_failures and not self.correlation_gaps:
            required = list(BASE_SCOPES) + self._history_scopes()
            if all(self.model.inventory(*s).complete and not self.model.inventory(*s).stale
                   for s in required):
                self._state("live")
        return True

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
        required = {
            "work-items": WORK_ITEM_FIELDS,
            "attests": set(ATTEST_FIELDS),
            "messages": {"id", "sessionKey", "sender", "at"},
        }.get(resource, set())
        applied = False
        if policy and notice.get("op") == "upsert" and required <= payload.keys():
            row = attest_row(payload) if resource == "attests" else payload
            applied = inventory.payload(row, at=now_ms(), source="feed:" + notice["class"],
                                        policy=policy)
            if applied:
                self.metrics["payloads"] += 1
        if not applied:
            if resource in {"roles", "attests"}:
                self._refresh(normalized, scope)
            elif key is not None:
                self._refresh(normalized, scope, key)
            else:
                inventory.invalidate()
                self._state("stale", "change missing primary reference")
        return True

    def _worker(self):
        while not self.stop_event.is_set():
            update = True
            try:
                kind, value = self.events.get(timeout=self._wait_timeout())
                if kind == "stop":
                    return
                if kind == "ready":
                    self.connected = True
                    self.version = value
                    self.policies = PAYLOAD_BUILDS.get(
                        (value["version"], value["build"], value["sha"]), {})
                    self.rebuild()
                elif kind == "lost":
                    update = self.connected
                    self.connected = False
                    update = self._state("stale", value) or update
                    if update:
                        self.model.invalidate()
                        for resource, scope in self.model.inventories:
                            self.reads.touch(resource, scope)
                elif kind == "read":
                    update = self._read_done(value)
                elif kind == "change":
                    update = self.change(value)
                elif kind == "merged":
                    wid, refs, merged, at = value
                    update = self.merge_pending.get(wid) == refs
                    if update:
                        self.merge_pending.pop(wid)
                        self.model.merges[wid] = {
                            "value": merged, "observedAt": at, "refs": refs}
            except queue.Empty:
                pass
            except Exception as error:
                self.model.invalidate()
                self._state("stale", "model update failed: " + type(error).__name__)
            if self.connected and self.cycle is None and time.monotonic() - self.last_rebuild >= self.rebuild_interval:
                update = True
                try:
                    self.rebuild(recovery=False)
                except Exception as error:
                    self.last_rebuild = time.monotonic()
                    self.model.invalidate()
                    self._state("stale", "rebuild failed: " + type(error).__name__)
            self.reads.pump()
            if update:
                self._publish()
                self._resolve_merges()

    def _wait_timeout(self):
        deadlines = []
        if self.connected and self.cycle is None:
            deadlines.append(self.last_rebuild + self.rebuild_interval - time.monotonic())
        if self.model.next_expiry is not None:
            deadlines.append((self.model.next_expiry - now_ms()) / 1000)
        return max(0, min(deadlines)) if deadlines else None

    def _feed(self):
        from websockets.sync.client import connect
        while not self.stop_event.is_set():
            try:
                version = self.gateway.version()
                url = self.gateway.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                with connect(url + "/ws/changes?protocolVersion=2", proxy=None,
                             ping_interval=None, max_size=None) as connection:
                    connection.send(json.dumps({"type": "auth", "token": self.gateway.device_token}))
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
                            self.events.put(("change", message))
            except Exception as error:
                # Exception bodies can contain server responses. The category
                # and close code suffice for recovery and never expose tokens.
                code = getattr(getattr(error, "rcvd", None), "code", None)
                reason = "feed disconnected: " + type(error).__name__
                if code is not None:
                    reason += " (" + str(code) + ")"
                self.events.put(("lost", reason))
            self.stop_event.wait(1)
