"""One upstream feed, recoverable read model, and full-model subscriptions."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import queue
import threading
import time
import uuid

from atc_model import EvidenceModel
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
    def __init__(self, gateway, resolver=None, rebuild_interval=300, workers=8):
        self.gateway, self.resolver = gateway, resolver
        self.model = EvidenceModel()
        self.generation, self.revision = uuid.uuid4().hex, 0
        self.rebuild_interval, self.workers = rebuild_interval, workers
        self.events = queue.Queue()
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
        self._publish()

    def snapshot(self):
        with self.condition:
            return deepcopy(self.current)

    def next_snapshot(self, revision, timeout=15):
        with self.condition:
            self.condition.wait_for(
                lambda: self.revision != revision or self.stop_event.is_set(), timeout)
            return deepcopy(self.current) if self.revision != revision else None

    def start(self):
        # Fail startup visibly when the runtime dependency isn't installed.
        from websockets.sync.client import connect  # noqa: F401
        for target in (self._worker, self._feed, self._git_worker):
            threading.Thread(target=target, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.events.put(("stop", None))
        self.merge_jobs.put(None)
        with self.condition:
            self.condition.notify_all()

    def _state(self, state, reason=None):
        if (self.state, self.reason) != (state, reason):
            self.state, self.reason, self.since = state, reason, now_ms()

    def _publish(self):
        # A published revision is immutable even while the worker updates its
        # inventories outside the subscribers' lock.
        data = deepcopy(self.model.snapshot(now_ms()))
        with self.condition:
            self.revision += 1
            self.current = {
                "schemaVersion": 1, "generation": self.generation,
                "revision": self.revision, "generatedAt": now_ms(),
                "freshness": {
                    "state": self.state, "since": self.since, "reason": self.reason,
                    "rebuildIntervalMs": self.rebuild_interval * 1000,
                    "lastCompleteAt": self.last_complete,
                },
                "gatewayVersion": self.version, "metrics": dict(self.metrics), **data,
            }
            self.condition.notify_all()

    def _read_many(self, jobs):
        complete = True
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self.gateway.read, resource, scope): (resource, scope)
                       for resource, scope in jobs}
            for future in as_completed(futures):
                resource, scope = futures[future]
                result = future.result()
                self.model.accept(resource, scope, result)
                complete = complete and result.complete
        return complete

    def rebuild(self):
        started, reads = time.monotonic(), self.gateway.read_count
        self._state("rebuilding", "reading published state")
        self.model.invalidate()
        self._publish()
        complete = self._read_many([
            ("work-items", None), ("sessions", None), ("assignments", None),
            ("roles", None), ("decision-requests", None), ("artifacts", None),
        ])
        jobs = [("attests", a["id"]) for a in self.model.inventory("assignments").rows.values()]
        jobs.extend((resource, session["sessionKey"])
                    for session in self.model.active_sessions()
                    for resource in ("turns", "wakes", "messages"))
        complete = self._read_many(jobs) and complete
        self.metrics["rebuilds"] += 1
        if self.metrics["bootstrapReads"] is None:
            self.metrics["bootstrapReads"] = self.gateway.read_count - reads
            self.metrics["bootstrapMs"] = round((time.monotonic() - started) * 1000)
            self.metrics["bootstrapComplete"] = complete
        self.last_rebuild = time.monotonic()
        if complete:
            self.last_complete = now_ms()
        self._state("live" if complete and self.connected else "stale",
                    None if complete and self.connected else "incomplete read or disconnected feed")
        self._resolve_merges()

    def _resolve_merges(self):
        if self.resolver is None:
            return
        # The resolver owns its cache; the rendering path never starts Git.
        snapshot = self.model.snapshot(now_ms())
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
        if key is None:
            result = self.gateway.read(resource, scope)
            self.model.accept(resource, scope, result)
        else:
            result = self.gateway.detail(resource, key, scope=scope)
            self.model.detail(resource, scope, result)
        if not result.complete:
            self._state("stale", "targeted read failed")
        return result

    def change(self, notice):
        resource = notice.get("resource")
        if resource not in RESOURCE_REFS:
            # Observational events do not imply that every inventory changed.
            return
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
            # An actual correlation gap, not an excuse to refetch every row.
            if key is not None:
                detail = self.gateway.detail(normalized, key)
                if detail.complete:
                    payload = detail.rows[0]
                    scope = payload.get("assignmentId" if resource == "attests" else "sessionKey")
            if not scope:
                self._state("stale", "uncorrelated " + resource + " change")
                return
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
        if resource == "assignments" and key is not None:
            # An opened/reopened assignment may introduce a previously unseen
            # attest scope; an existing complete inventory needs no reread.
            if not self.model.inventory("attests", str(key)).complete:
                self._refresh("attests", str(key))
            assignment = self.model.inventory("assignments").rows.get(str(key), {})
            holder = assignment.get("holderKey")
            if assignment.get("state") == "open" and holder:
                if holder not in self.model.inventory("sessions").rows:
                    self._refresh("sessions", key=holder)
                self._session_histories(holder)
        if resource == "sessions" and key is not None:
            if any(s["sessionKey"] == str(key) for s in self.model.active_sessions()):
                self._session_histories(str(key))
        if resource in {"attests", "assignments", "work-items"}:
            self._resolve_merges()

    def _session_histories(self, key):
        # An assignment can bring a retired holder back into view without a
        # session notice. Previously unused or stale histories are actual gaps.
        for nested in ("turns", "wakes", "messages"):
            inventory = self.model.inventory(nested, key)
            if not inventory.complete or inventory.stale:
                self._refresh(nested, key)

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                kind, value = self.events.get(timeout=1)
                if kind == "stop":
                    return
                if kind == "ready":
                    self.connected = True
                    self.version = value
                    self.policies = PAYLOAD_BUILDS.get(
                        (value["version"], value["build"], value["sha"]), {})
                    self.rebuild()
                elif kind == "lost":
                    self.connected = False
                    self.model.invalidate()
                    self._state("stale", value)
                elif kind == "change":
                    self.change(value)
                elif kind == "merged":
                    wid, refs, merged, at = value
                    if self.merge_pending.get(wid) == refs:
                        self.merge_pending.pop(wid)
                        self.model.merges[wid] = {
                            "value": merged, "observedAt": at, "refs": refs}
            except queue.Empty:
                pass
            except Exception as error:
                self.model.invalidate()
                self._state("stale", "model update failed: " + type(error).__name__)
            if self.connected and time.monotonic() - self.last_rebuild >= self.rebuild_interval:
                try:
                    self.rebuild()
                except Exception as error:
                    self.last_rebuild = time.monotonic()
                    self.model.invalidate()
                    self._state("stale", "rebuild failed: " + type(error).__name__)
            self._publish()

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
