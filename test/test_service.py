"""Deterministic service recovery; real gateway proof is recorded separately."""

from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import LiveService, WORK_ITEM_FIELDS
from atc_payloads import policies_for
from atc_git import GitResolver
from atc_upstream import Read


def work(title="First", version=1):
    return dict.fromkeys(WORK_ITEM_FIELDS) | {
        "id": "work", "title": title, "state": "open", "createdAt": 1,
        "rowVersion": version,
    }


def summarized_work(title="First", version=1, *, attest_counts=None, verdict_counts=None,
                   holders=None, started=True, refs=None, landings=None):
    return work(title, version) | {
        "holders": ["agent s_author"] if holders is None else holders,
        "started": started,
        "attestCounts": {"progress": 1} if attest_counts is None else attest_counts,
        "verdictCounts": {} if verdict_counts is None else verdict_counts,
        "effectiveCommitRefs": [] if refs is None else refs,
        "landings": [] if landings is None else landings,
    }


class PublishedReads:
    def __init__(self):
        self.read_count = 0
        self.calls = []
        self.partial = set()
        self.rows = {
            ("work-items", None): [work()],
            ("assignments", None): [{
                "id": "assignment", "holderKey": "agent s_author",
                "workItemId": "work", "state": "open", "effectKind": "code",
            }],
            ("sessions", None): [{
                "sessionKey": "agent s_author", "state": "active",
                "displayName": "Author", "topologyParent": None,
            }],
            ("attests", "assignment"): [{
                "id": "progress", "assignmentId": "assignment", "kind": "progress",
                "ts": 1, "bySession": "agent s_author", "commitRefs": [],
            }],
        }

    def read(self, resource, scope=None):
        self.read_count += 1
        self.calls.append(("read", resource, scope))
        return Read(deepcopy(self.rows.get((resource, scope), [])),
                    (resource, scope) not in self.partial, 10, "published-read")

    def detail(self, resource, key, scope=None):
        self.read_count += 1
        self.calls.append(("detail", resource, scope, key))
        identity = "sessionKey" if resource == "sessions" else "seq" if resource == "turns" else "id"
        rows = [r for r in self.rows.get((resource, scope), []) if r.get(identity) == key]
        rows = deepcopy(rows)
        if resource == "assignments":
            for row in rows:
                row.setdefault("commitRefCorrections", [])
        return Read(rows, bool(rows), 11, "published-detail")

    def assignment_membership(self, wid, *, include_rows=False):
        self.read_count += 1
        self.calls.append(("membership", wid))
        return Read([deepcopy(row) if include_rows else {"id": row["id"]} for row in self.rows['assignments', None]
                     if row.get("workItemId") == wid], True, 10, "published-membership")


class LocalCanonicalGitResolver(GitResolver):
    """Use real Git against a local bare remote for resolver interaction tests."""
    def __init__(self, cache, remotes):
        super().__init__(cache)
        self.remotes = remotes

    def _run(self, args):
        mapped = [str(self.remotes.get(arg, arg)) for arg in args]
        return super()._run(mapped)


class ServiceRecoveryTest(unittest.TestCase):
    def setUp(self):
        clock = patch("atc_service.now_ms", return_value=20)
        clock.start()
        self.addCleanup(clock.stop)
        self.gateway = PublishedReads()
        self.service = LiveService(self.gateway)
        self.service.connected = True
        self.service.rebuild()
        self.drain()
        self.service._update_backstop(20)

    def tearDown(self):
        self.service.stop()

    def drain(self, service=None):
        service = service or self.service
        deadline = time.monotonic() + 5
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            kind, value = service.events.get(timeout=max(0.01, deadline - time.monotonic()))
            self.assertEqual("read", kind)
            service._read_done(value)
            self.assertLess(time.monotonic(), deadline)

    def item(self):
        return self.service.model.snapshot(20)["items"][0]

    def test_reconciliation_names_pending_scopes_and_queue_work_by_resource(self):
        self.service.cycle = {
            "mode": "recovery",
            "pending": {("messages", "agent-one"), ("messages", "agent-two"),
                        ("wakes", "agent-one")},
            "seen": {("messages", "agent-one"), ("messages", "agent-two"),
                     ("wakes", "agent-one")},
        }
        self.service.reads.schedule("messages", "agent-one")
        self.service.reads.schedule("messages", "agent-two", "message-one")
        self.service.reads.schedule("wakes", "agent-one")
        self.service.reads.active[("messages", "agent-three")] = (
            ("messages", "agent-three", None), 0)

        reconciliation = self.service._reconciliation()
        self.assertEqual(3, reconciliation["pendingScopes"])
        self.assertEqual({"messages": 2, "wakes": 1},
                         reconciliation["pendingByResource"])
        queue = reconciliation["readDiagnostics"]["readQueue"]
        self.assertEqual({"messages": 2, "wakes": 1}, queue["pendingByResource"])
        self.assertEqual({"messages": 1}, queue["activeByResource"])

    def test_bootstrap_covers_assignments_attests_and_visible_session_histories(self):
        self.assertEqual("live", self.service.state)
        self.assertEqual(1, self.item()["stage"])
        self.assertEqual({"open": 1, "terminal": 0}, self.item()["assignments"])
        for resource in ("turns", "wakes", "messages"):
            self.assertIn(("read", resource, "agent s_author"), self.gateway.calls)
        self.assertTrue(self.service.metrics["bootstrapComplete"])

    def test_incomplete_bootstrap_retries_failed_scope_without_restarting_cycle(self):
        gateway = PublishedReads()
        gateway.partial.add(("work-items", None))
        service = LiveService(gateway)
        service.connected = True
        self.addCleanup(service.stop)
        service.rebuild()
        cycle = service.cycle

        deadline = time.monotonic() + 5
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            kind, value = service.events.get(timeout=max(0.01, deadline - time.monotonic()))
            self.assertEqual("read", kind)
            service._read_done(value)
            self.assertLess(time.monotonic(), deadline)

        retry = ("work-items", None, None)
        self.assertIs(service.cycle, cycle)
        self.assertEqual({("work-items", None)}, cycle["pending"])
        self.assertIsNone(service.metrics["bootstrapReads"])
        self.assertIn(retry, service.read_retries)

        gateway.partial.clear()
        _, priority = service.read_retries[retry]
        service.read_retries[retry] = (time.monotonic() - 1, priority)
        service._retry_incomplete_reads()
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            kind, value = service.events.get(timeout=max(0.01, deadline - time.monotonic()))
            self.assertEqual("read", kind)
            service._read_done(value)
            self.assertLess(time.monotonic(), deadline)

        self.assertIsNone(service.cycle)
        self.assertTrue(service.metrics["bootstrapComplete"])
        self.assertEqual("live", service.state)

    def test_connected_incomplete_scope_keeps_staleness_local(self):
        self.gateway.partial.add(("work-items", None))
        self.service.reads.schedule("work-items", None, None)
        self.service.reads.pump()
        kind, value = self.service.events.get(timeout=2)
        self.assertEqual("read", kind)
        self.service._read_done(value)
        self.service._publish()

        snapshot = self.service.snapshot()
        self.assertEqual("rebuilding", snapshot["freshness"]["state"])
        self.assertEqual("published read incomplete", snapshot["freshness"]["reason"])
        self.assertEqual("stale", snapshot["items"][0]["evidence"]["identity"]["freshness"])
        self.assertEqual("live", snapshot["agents"][0]["evidence"]["identity"]["freshness"])
        self.assertIn(("work-items", None, None), self.service.read_retries)

    def test_disconnect_resumes_pending_bootstrap_without_restarting_completed_scopes(self):
        gateway = PublishedReads()
        gateway.partial.add(("work-items", None))
        service = LiveService(gateway)
        service.connected = True
        self.addCleanup(service.stop)
        service.rebuild()
        self.drain(service)

        retry = ("work-items", None, None)
        self.assertIn(retry, service.read_retries)
        self.assertIsNone(service.metrics["bootstrapReads"])
        cycle = service.cycle
        retry_state = service.read_retries[retry]
        completed = {scope for scope in cycle["seen"] if scope != retry[:2]}
        self.assertTrue(completed)
        self.assertTrue(all(service.model.inventory(*scope).complete and
                            not service.model.inventory(*scope).stale for scope in completed))

        reads_before_disconnect = gateway.read_count
        epochs_before_disconnect = dict(service.reads.epochs)
        service._handle("lost", "feed disconnected: ConnectionClosedError (4008)")
        self.assertIs(cycle, service.cycle)
        self.assertEqual("recovery", service.cycle["mode"])
        self.assertEqual("feed disconnected: ConnectionClosedError (4008)", service.reason)
        gateway.partial.clear()
        self.drain(service)
        self.assertEqual(reads_before_disconnect, gateway.read_count)
        self.assertEqual(retry_state, service.read_retries[retry])

        service._handle("ready", {"version": "0.1.11", "build": 1, "sha": "test"})
        self.assertTrue(service.connected)
        self.assertEqual(reads_before_disconnect, gateway.read_count)
        self.assertEqual(retry_state, service.read_retries[retry])
        self.assertEqual(epochs_before_disconnect, dict(service.reads.epochs))

        _, priority = retry_state
        service.read_retries[retry] = (time.monotonic() - 1, priority)
        service._retry_incomplete_reads()
        self.drain(service)
        self.assertIsNone(service.cycle)
        self.assertTrue(service.metrics["bootstrapComplete"])
        self.assertIsNotNone(service.last_complete)
        self.assertEqual("live", service.state)

    def test_completed_scopes_keep_credit_and_show_outage_until_validated(self):
        completed = {
            scope: (deepcopy(inventory.rows), inventory.confirmed_at)
            for scope, inventory in self.service.model.inventories.items()
            if inventory.complete
        }
        self.assertTrue(completed)
        reads_before_outage = self.gateway.read_count
        last_complete_before = self.service.last_complete
        outage_at = 1000

        with patch("atc_service.now_ms", return_value=outage_at):
            self.service._handle("lost", "feed disconnected: transport closed")
        self.assertIsNotNone(self.service.cycle)
        self.assertEqual(set(), self.service.cycle["pending"])
        self.assertEqual(reads_before_outage, self.gateway.read_count)
        for scope, (rows, confirmed_at) in completed.items():
            inventory = self.service.model.inventory(*scope)
            self.assertEqual(rows, inventory.rows)
            self.assertTrue(inventory.complete)
            self.assertFalse(inventory.stale)
            self.assertEqual(confirmed_at, inventory.confirmed_at)
            self.assertEqual(outage_at, inventory.unconfirmed_since)
            self.assertEqual("unconfirmed", inventory.evidence()["freshness"])

        with patch("atc_service.now_ms", return_value=1100):
            self.service._handle(
                "ready", {"version": "0.1.11", "build": 1, "sha": "test"})
            self.service._publish()
        self.assertTrue(self.service.connected)
        self.assertIsNone(self.service.cycle)
        self.assertTrue(self.service.metrics["bootstrapComplete"])
        self.assertNotEqual(last_complete_before, self.service.last_complete)
        self.assertEqual(1100, self.service.last_complete)
        self.assertEqual("live", self.service.state)
        self.assertEqual(reads_before_outage, self.gateway.read_count)
        item = self.service.snapshot()["items"][0]
        self.assertEqual("unconfirmed", item["evidence"]["identity"]["freshness"])
        self.assertEqual(outage_at, item["evidence"]["identity"]["unconfirmedSince"])
        self.assertEqual("unconfirmed", item["evidence"]["stage"]["freshness"])
        status, page = self.service.evidence_page("work")
        self.assertEqual(200, status)
        self.assertEqual("unconfirmed", page["freshness"]["freshness"])
        self.assertEqual(outage_at, page["freshness"]["unconfirmedSince"])

        work_rows = list(self.service.model.inventory("work-items").rows.values())
        self.service.model.accept("work-items", None,
                                  Read(work_rows, True, 1200, "validating-read"))
        with patch("atc_service.now_ms", return_value=1200):
            self.service._publish()
        item = self.service.snapshot()["items"][0]
        self.assertEqual("live", item["evidence"]["identity"]["freshness"])
        self.assertIsNone(item["evidence"]["identity"]["unconfirmedSince"])
        self.assertEqual("unconfirmed", item["evidence"]["stage"]["freshness"])
        self.assertEqual(outage_at,
                         self.service.model.inventory("assignments").unconfirmed_since)

    def test_reconnected_partial_recovery_keeps_stale_on_genuinely_incomplete_scope(self):
        self.gateway.partial.add(("attests", "assignment"))
        self.service.model.inventory("attests", "assignment").invalidate()
        self.service._handle("lost", "feed disconnected: transport closed")
        self.assertEqual("stale", self.service.state)
        self.assertIsNotNone(self.service.cycle)

        self.service._handle("ready", {"version": "0.1.11", "build": 1, "sha": "test"})
        self.assertTrue(self.service.connected)
        self.assertEqual("rebuilding", self.service.state)

        self.drain()
        self.assertEqual("rebuilding", self.service.state)
        # Bound R32/R10, test-owner agreement art_6fa7dbb4: a scoped
        # failure retains complete facts and retries that same scope. It
        # does not identify an incomplete assignment-membership source.
        self.assertEqual({("attests", "assignment")}, self.service.cycle["pending"])
        self.assertEqual(set(), self.service.model.item_assignment_pending)
        self.assertIn(("attests", "assignment", None), self.service.read_retries)
        self.assertNotIn(("item-assignment-source", "work", None), self.service.read_retries)
        self.assertEqual(1, self.service.model.summary("attests", "assignment")["count"])
        self.assertFalse(self.service.model.inventory("work-items").stale)
        self.assertFalse(self.service.model.inventory("assignments").stale)
        self.assertTrue(self.service.model.inventory("attests", "assignment").stale)
        self.assertEqual("unconfirmed", self.service.model.inventory("work-items").evidence()["freshness"])
        self.assertEqual("unconfirmed", self.service.model.inventory("assignments").evidence()["freshness"])
        self.assertIsNotNone(self.service.model.inventory("attests", "assignment").unconfirmed_since)

        self.service._publish()
        reconciliation = self.service.snapshot()["freshness"]["reconciliation"]
        self.assertGreater(reconciliation["totalScopes"], reconciliation["pendingScopes"])
        self.assertEqual(1, reconciliation["pendingScopes"])
        self.assertEqual("stale", self.service.snapshot()["items"][0]["evidence"]["stage"]["freshness"])
        self.assertIsNotNone(self.service.snapshot()["items"][0]["evidence"]["stage"]["unconfirmedSince"])

        deadline, _ = self.service.read_retries["attests", "assignment", None]
        self.gateway.partial.clear()
        self.gateway.calls.clear()
        with patch("atc_service.time.monotonic", return_value=deadline):
            self.service._retry_incomplete_reads()
        self.drain()
        self.service._publish()
        self.assertEqual([("read", "attests", "assignment")], self.gateway.calls)
        self.assertNotIn(("attests", "assignment", None), self.service.read_retries)
        self.assertIsNone(self.service.cycle)
        self.assertEqual("live", self.service.state)
        self.assertEqual(1, self.item()["evidenceCounts"]["total"])
        # R32 / art_680506dc: this retry confirms attests only. The other
        # retained outage observations above are not silently reconfirmed.
        self.assertEqual("live", self.service.model.inventory("attests", "assignment").evidence()["freshness"])
        self.assertEqual("unconfirmed", self.service.model.inventory("work-items").evidence()["freshness"])
        self.assertEqual("unconfirmed", self.service.model.inventory("assignments").evidence()["freshness"])
        self.assertEqual("unconfirmed", self.item()["evidence"]["stage"]["freshness"])

    def test_repeated_lost_events_do_not_rebuild_after_disconnect_transition(self):
        self.service._handle("lost", "feed disconnected: ConnectionClosedError (4008)")
        cycle = self.service.cycle
        self.assertIsNotNone(cycle)
        self.drain()
        self.assertIs(cycle, self.service.cycle,
                      "an offline recovery keeps completion credit but waits to settle until reconnect")
        rebuilds = self.service.metrics["rebuilds"]
        reads = self.gateway.read_count
        epochs = dict(self.service.reads.epochs)

        for reason in ("feed disconnected: retry 1", "feed disconnected: retry 2"):
            self.service._handle("lost", reason)
            self.assertEqual(reason, self.service.reason)
            self.assertIs(cycle, self.service.cycle)
            self.assertEqual(rebuilds, self.service.metrics["rebuilds"])
            self.assertEqual(epochs, dict(self.service.reads.epochs))
            self.assertEqual(reads, self.gateway.read_count)
        self.assertEqual(reads, self.gateway.read_count)

        self.service._handle("ready", {"version": "0.1.11", "build": 1, "sha": "test"})
        self.drain()
        self.assertIsNone(self.service.cycle)
        self.assertTrue(self.service.metrics["bootstrapComplete"])
        self.assertEqual("live", self.service.state)

    def test_incomplete_read_retry_deadline_runs_while_feed_is_disconnected(self):
        gateway = PublishedReads()
        gateway.partial.add(("work-items", None))
        service = LiveService(gateway)
        service.connected = True
        service.last_rebuild = time.monotonic()
        self.addCleanup(service.stop)
        service.reads.schedule("work-items", None, None)
        service.reads.pump()
        kind, value = service.events.get(timeout=2)
        self.assertEqual("read", kind)
        service._read_done(value)
        retry = ("work-items", None, None)
        self.assertIn(retry, service.read_retries)

        service.connected = False
        gateway.partial.clear()
        retry_delay = service._wait_timeout()
        self.assertIsNotNone(retry_delay)
        self.assertGreater(retry_delay, 0)
        self.assertLessEqual(retry_delay, 60)
        _, priority = service.read_retries[retry]
        service.read_retries[retry] = (time.monotonic() - 1, priority)
        service._retry_incomplete_reads()
        self.assertIn(retry, service.reads.pending)
        self.drain(service)
        self.assertEqual(1, service.retry_counters["completed"])
        self.assertTrue(service.last_retry_outcome["complete"])

    def test_repeated_lost_events_preserve_offline_retry_and_rebuild_cadence(self):
        gateway = PublishedReads()
        gateway.partial.add(("work-items", None))
        service = LiveService(gateway)
        service.connected = True
        self.addCleanup(service.stop)
        service.rebuild()
        self.drain(service)

        retry = ("work-items", None, None)
        self.assertIsNotNone(service.cycle)
        self.assertIn(retry[:2], service.cycle["pending"])
        self.assertIn(retry, service.read_retries)
        service._handle("lost", "feed disconnected: ConnectionClosedError (4008)")
        self.assertEqual("recovery", service.cycle["mode"])
        self.drain(service)

        offline_cycle = service.cycle
        self.assertIsNotNone(offline_cycle)
        self.assertIn(retry[:2], offline_cycle["pending"])
        self.assertIn(retry, service.read_retries)
        retry_state = service.read_retries[retry]
        self.assertGreater(retry_state[0], time.monotonic())
        epochs = dict(service.reads.epochs)
        reads = gateway.read_count
        cycle_state = {
            "mode": offline_cycle["mode"],
            "pending": set(offline_cycle["pending"]),
            "seen": set(offline_cycle["seen"]),
            "expanded": offline_cycle["expanded"],
        }

        # Feed loss is reported repeatedly. Local read-state transitions can
        # change the stale reason between notifications without starting a
        # fresh full recovery cycle or consuming the bounded retry delay.
        for reason in ("feed disconnected: retry 1", "feed disconnected: retry 2"):
            service._handle("lost", reason)
            self.assertEqual(reason, service.reason)
            self.assertIs(offline_cycle, service.cycle)
            self.assertEqual(cycle_state["mode"], offline_cycle["mode"])
            self.assertEqual(cycle_state["pending"], offline_cycle["pending"])
            self.assertEqual(cycle_state["seen"], offline_cycle["seen"])
            self.assertEqual(cycle_state["expanded"], offline_cycle["expanded"])
            self.assertEqual(epochs, dict(service.reads.epochs))
            self.assertEqual(retry_state, service.read_retries[retry])
            self.assertFalse(service.reads.pending)
            self.assertGreater(retry_state[0], time.monotonic())
        self.assertEqual(reads, gateway.read_count)

        # Its scheduled retry still runs while disconnected. The result remains
        # stale, so reconnect must resume that scope before settling the cycle.
        gateway.partial.clear()
        service.read_retries[retry] = (time.monotonic() - 1, retry_state[1])
        service._retry_incomplete_reads()
        self.assertIn(retry, service.reads.pending)
        self.drain(service)
        self.assertIs(offline_cycle, service.cycle)
        self.assertEqual(1, service.retry_counters["completed"])
        self.assertTrue(service.last_retry_outcome["complete"])
        self.assertEqual("stale", service.state)
        self.assertTrue(service.model.inventory(*retry[:2]).stale)

        reads_before_reconnect = gateway.read_count
        service._handle("ready", {"version": "0.1.11", "build": 1, "sha": "test"})
        self.assertEqual(reads_before_reconnect, gateway.read_count)
        pending_scopes = {job[:2] for job in service.reads.pending if job[2] is None}
        stale_scopes = {scope for scope in offline_cycle["seen"]
                        if (not service.model.inventory(*scope).complete
                            or service.model.inventory(*scope).stale
                            or service.model.inventory(*scope).budget_stale)}
        self.assertEqual(stale_scopes, pending_scopes)
        self.assertIn(retry[:2], pending_scopes)
        self.drain(service)
        self.assertIsNone(service.cycle)
        self.assertFalse(service.model.inventory(*retry[:2]).stale)
        self.assertEqual("live", service.state)

    def test_status_exposes_sanitized_page_retry_epoch_and_feed_lag_details(self):
        job = ("work-items", None, None)
        self.service.reads.schedule(*job)
        self.gateway.read = lambda resource, scope: Read(
            [], False, 25, "/api/work-items?after=private-cursor", "TimeoutError", 3)
        self.service.reads.pump()
        kind, value = self.service.events.get(timeout=2)
        self.assertEqual("read", kind)
        self.service._read_done(value)

        before = time.monotonic() - 0.25
        self.service._queue_feed_change({"resource": "unknown"}, received_at=before)
        kind, value = self.service.events.get(timeout=2)
        self.assertEqual("change", kind)
        self.service._handle(kind, value)

        self.service.reads.schedule("roles", None, None)
        self.service.reads.pump()
        self.service.reads.touch("roles", None)
        kind, value = self.service.events.get(timeout=2)
        self.assertEqual("read", kind)
        self.assertFalse(self.service._read_done(value))

        diagnostic = self.service.snapshot()["freshness"]["reconciliation"]["readDiagnostics"]
        self.assertEqual({"resource": "work-items", "readKind": "collection", "page": 3,
                          "complete": False, "error": "TimeoutError", "at": 25},
                         diagnostic["lastIncompleteRead"])
        self.assertEqual(1, diagnostic["retry"]["waiting"])
        self.assertEqual(1, diagnostic["feed"]["received"])
        self.assertEqual(1, diagnostic["feed"]["processed"])
        self.assertGreaterEqual(diagnostic["feed"]["lastReceiveToProcessMs"], 200)
        self.assertEqual({"total": 1, "byResource": {"roles": 1}}, diagnostic["epochDiscards"])
        self.assertNotIn("private-cursor", json.dumps(diagnostic))

    def test_failed_rebuild_retains_rows_and_recovery_can_establish_absence(self):
        self.gateway.rows[("work-items", None)] = []
        self.gateway.partial.add(("work-items", None))
        self.service.rebuild()
        self.drain()
        self.assertEqual("work", self.item()["id"])
        self.assertEqual("stale", self.item()["evidence"]["identity"]["freshness"])
        self.assertEqual("rebuilding", self.service.state)
        self.gateway.partial.clear()
        self.service.rebuild()
        self.drain()
        self.assertEqual([], self.service.model.snapshot(20)["items"])
        self.assertEqual("live", self.service.state)

    def test_unannounced_correction_is_recovered_by_interval_read_scope(self):
        self.gateway.rows[("assignments", None)][0]["commitRefCorrections"] = [{
            "actorRef": "author", "commitRefs": [{"commit": "a" * 40, "repo": "repo"}],
        }]
        self.service.rebuild()
        self.drain()
        self.assertEqual("author", self.item()["commitRefCorrections"][0]["actorRef"])
        self.assertEqual("agent s_author",
                         self.service.model.inventory("attests", "assignment").rows["progress"]["bySession"])

    def test_verified_payload_applies_without_reads_and_does_not_roll_back(self):
        # Exercise ordering with synthetic rows. This is not real-feed proof.
        self.service.policies = {"work-items": "strict-version"}
        before = self.gateway.read_count
        notice = {"resource": "work-items", "class": "work_item.updated",
                  "op": "upsert", "refs": {"workItemId": "work"},
                  "payload": work("New", 3)}
        self.service.change(notice)
        self.service.change(dict(notice, payload=work("Older", 2)))
        self.assertEqual("New", self.item()["title"])
        self.assertEqual(before, self.gateway.read_count)

    def test_partial_or_unverified_payload_uses_only_targeted_read(self):
        self.service.policies = {"work-items": "strict-version"}
        self.gateway.rows[("work-items", None)] = [work("Current", 3)]
        self.gateway.calls.clear()
        self.service.change({
            "resource": "work-items", "class": "work_item.updated", "op": "upsert",
            "refs": {"workItemId": "work"}, "payload": {"id": "work", "rowVersion": 4},
        })
        self.drain()
        self.assertEqual("Current", self.item()["title"])
        self.assertEqual([("detail", "work-items", None, "work")], self.gateway.calls)

    def test_publication_does_not_change_an_already_observed_revision(self):
        self.service._publish()
        old = self.service.snapshot()
        self.service.model.inventory("work-items").rows["work"]["title"] = "Changed"
        self.assertEqual("First", old["items"][0]["title"])
        self.assertEqual("First", self.service.snapshot()["items"][0]["title"])
        self.service._publish()
        self.assertGreater(self.service.snapshot()["revision"], old["revision"])
        self.assertEqual("Changed", self.service.snapshot()["items"][0]["title"])

    def test_idle_time_and_diagnostic_counters_do_not_publish_a_new_revision(self):
        with patch("atc_service.now_ms", return_value=10_000):
            self.service._publish()
        before = self.service.snapshot()
        frame = self.service.next_event(-1)
        self.service.metrics["targetedReads"] += 1
        with patch("atc_service.now_ms", return_value=20_000):
            self.assertFalse(self.service._publish())
        self.assertEqual(before, self.service.snapshot())
        self.assertIs(frame[1], self.service.next_event(-1)[1])
        self.assertIsNone(self.service.next_event(before["revision"], timeout=0))
        self.assertEqual(1, before["agents"][0]["idleSince"])

    def test_raw_attest_history_does_not_grow_the_viewer_frame(self):
        self.service._publish()
        baseline = len(self.service.event)
        rows = [{"id": str(i), "assignmentId": "assignment", "kind": "progress",
                 "ts": 1, "bySession": "agent s_author", "commitRefs": [],
                 "note": "private history " * 1000} for i in range(2000)]
        self.service.model.accept("attests", "assignment", Read(rows, True, 10, "published-read"))
        self.service._publish()
        self.assertLess(len(self.service.event), baseline + 100)
        self.assertNotIn(b"private history", self.service.event)
        item = self.service.snapshot()["items"][0]
        self.assertNotIn("evidenceRecords", item)
        self.assertEqual({"total": 2000, "byKind": {"progress": 2000}, "byVerdict": {}},
                         item["evidenceCounts"])
        self.assertEqual(rows[0], self.service.model.inventory("attests", "assignment").rows["0"])

    def test_recent_mark_expires_without_one_second_publications(self):
        self.service.model.accept("messages", "agent s_author", Read([{
            "id": "message", "sessionKey": "agent s_author", "sender": "user:person",
            "at": 1000,
        }], True, 1000, "published-read"))
        with patch("atc_service.now_ms", return_value=1000):
            self.service._publish()
        self.assertEqual(301_000, self.service.model.next_expiry)
        with patch("atc_service.now_ms", return_value=300_999):
            self.assertFalse(self.service._publish())
        with patch("atc_service.now_ms", return_value=301_000):
            self.assertTrue(self.service._publish())
        self.assertEqual([], self.service.snapshot()["messages"])
        self.assertIsNone(self.service.model.next_expiry)

    def test_freshness_change_is_published_even_when_rows_are_unchanged(self):
        self.service._publish()
        revision = self.service.revision
        self.service.model.invalidate()
        self.service._state("stale", "feed disconnected")
        self.assertTrue(self.service._publish())
        self.assertGreater(self.service.revision, revision)
        self.assertEqual("work", self.service.snapshot()["items"][0]["id"])
        self.assertEqual("stale", self.service.snapshot()["items"][0]["evidence"]["identity"]["freshness"])

    def test_unrelated_feed_notice_does_not_rebuild_the_snapshot(self):
        self.service.events.put(("change", {"resource": "config"}))
        self.service.events.put(("stop", None))
        with patch.object(self.service.model, "snapshot") as snapshot:
            with patch("atc_service.time.monotonic", return_value=self.service.last_rebuild):
                self.service._worker()
        snapshot.assert_not_called()

    def test_idle_worker_waits_until_rebuild_or_mark_expiry(self):
        self.service.last_rebuild = 100
        with patch("atc_service.time.monotonic", return_value=100):
            self.assertEqual(300, self.service._wait_timeout())
            self.service.model.next_expiry = 16_000
            with patch("atc_service.now_ms", return_value=1000):
                self.assertEqual(15, self.service._wait_timeout())
        self.service.connected = False
        self.service.model.next_expiry = None
        self.service.last_rebuild = time.monotonic()
        # Confirmation-age and recent-activity transitions remain visible offline.
        self.assertGreater(self.service._wait_timeout(), 0)

    def test_new_assignment_fills_histories_of_previously_inactive_holder(self):
        self.gateway.rows[("sessions", None)].append({
            "sessionKey": "agent s_retired", "state": "retired",
            "displayName": "Retired", "topologyParent": None,
        })
        self.service.rebuild()
        self.drain()
        self.gateway.rows[("assignments", None)].append({
            "id": "new", "holderKey": "agent s_retired", "workItemId": "work",
            "state": "open", "effectKind": "code",
        })
        self.gateway.calls.clear()
        self.service.change({
            "resource": "assignments", "op": "upsert", "class": "assignment.opened",
            "refs": {"assignmentId": "new"}, "payload": {},
        })
        self.drain()
        self.assertCountEqual([
            ("detail", "assignments", None, "new"), ("read", "attests", "new"),
            ("read", "turns", "agent s_retired"), ("read", "wakes", "agent s_retired"),
            ("read", "messages", "agent s_retired"),
        ], self.gateway.calls)


    def test_interval_rotates_bounded_histories(self):
        for number in range(12):
            self.gateway.rows[("assignments", None)].append({
                "id": "extra" + str(number), "workItemId": "work",
                "state": "closed", "holderKey": "agent s_author",
            })
        self.service.rebuild()
        self.drain()
        self.service.scope_budget = 2
        seen = set()
        for _ in range(8):
            self.gateway.calls.clear()
            self.service.rebuild(recovery=False)
            # Interval refresh does not make every known inventory stale.
            self.assertFalse(self.service.model.inventory("attests", "assignment").stale)
            self.drain()
            histories = {(r, scope) for kind, r, scope in self.gateway.calls
                         if r in {"attests", "turns", "wakes", "messages"}}
            self.assertLessEqual(len(histories), 2)
            seen.update(histories)
        self.assertEqual(self.service.backstop.history, seen)

    def test_unplaced_change_stays_stale_after_interval_without_full_sweep(self):
        self.gateway.rows[("assignments", None)][0]["state"] = "closed"
        self.service.rebuild()
        self.drain()
        self.service._update_backstop(20)
        self.assertIn(("attests", "assignment"), self.service.backstop.history)

        self.service.scope_budget = 1
        self.service.change({"resource": "messages", "refs": {"messageId": "unplaced"}})
        gap = ("messages", "unplaced")
        self.assertIn(gap, self.service.correlation_gaps)
        self.assertEqual("stale", self.service.state)

        sweep = set(self.service.sweep_remaining)
        self.assertTrue(sweep)
        self.service.rebuild(recovery=False)
        self.drain()

        self.assertIsNone(self.service.cycle)
        self.assertTrue(self.service.sweep_remaining)
        self.assertLess(len(self.service.sweep_remaining), len(sweep))
        self.assertIn(gap, self.service.correlation_gaps)
        self.assertEqual("stale", self.service.state)
        self.assertEqual("uncorrelated messages change", self.service.reason)

    def test_unplaced_change_survives_idle_reconnect_until_correlated_read(self):
        self.service.change({"resource": "messages", "refs": {"messageId": "unplaced"}})
        gap = ("messages", "unplaced")
        self.assertIn(gap, self.service.correlation_gaps)
        self.assertEqual("stale", self.service.state)
        reads_before_outage = self.gateway.read_count

        self.service._handle("lost", "feed disconnected: transport closed")
        cycle = self.service.cycle
        self.assertIsNotNone(cycle)
        self.assertTrue(cycle["resume"])
        self.assertEqual(set(), cycle["pending"])
        self.service._handle("ready", {"version": "0.1.11", "build": 1, "sha": "test"})

        self.assertIsNone(self.service.cycle)
        self.assertEqual(reads_before_outage, self.gateway.read_count,
                         "idle reconnect should retain completed scope credit")
        self.assertIn(gap, self.service.correlation_gaps)
        self.assertEqual("stale", self.service.state)
        self.assertEqual("uncorrelated messages change", self.service.reason)

        self.gateway.rows[("messages", None)] = [{
            "id": "unplaced", "sessionKey": "agent s_author",
        }]
        self.service.reads.schedule("messages", None, "unplaced")
        self.drain()

        self.assertIsNone(self.service.cycle)
        self.assertNotIn(gap, self.service.correlation_gaps)
        self.assertTrue(self.service.model.inventory("messages", "agent s_author").stale,
                        "a detail read correlates the event but does not validate the full scope")

    def test_background_history_read_does_not_block_targeted_change(self):
        entered, release = threading.Event(), threading.Event()
        original = self.gateway.read

        def blocked(resource, scope=None):
            if resource == "attests":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release history")
            return original(resource, scope)

        self.gateway.read = blocked
        self.service._refresh("attests", "assignment")
        self.service.reads.pump()
        self.assertTrue(entered.wait(2))
        try:
            self.gateway.rows[("work-items", None)] = [work("While history blocked", 4)]
            self.service.change({"resource": "work-items", "refs": {"workItemId": "work"}})
            self.service.reads.pump()
            kind, value = self.service.events.get(timeout=2)
            self.assertEqual("read", kind)
            self.assertEqual("work-items", value[0][0])
            self.service._read_done(value)
            self.assertEqual("While history blocked", self.item()["title"])
        finally:
            release.set()
            self.drain()

    def test_read_started_before_notice_cannot_replace_verified_newer_payload(self):
        entered, release = threading.Event(), threading.Event()
        original = self.gateway.read

        def captured(resource, scope=None):
            result = original(resource, scope)
            if resource == "work-items":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release read")
            return result

        self.gateway.read = captured
        self.service._refresh("work-items")
        self.service.reads.pump()
        self.assertTrue(entered.wait(2))
        self.service.policies = {"work-items": "strict-version"}
        self.service.change({"resource": "work-items", "class": "work_item.updated",
                             "op": "upsert", "refs": {"workItemId": "work"},
                             "payload": work("Newer", 8)})
        self.gateway.rows[("work-items", None)] = [work("Newer", 8)]
        release.set()
        kind, value = self.service.events.get(timeout=2)
        self.assertEqual("read", kind)
        self.assertFalse(self.service._read_done(value))
        self.assertEqual("Newer", self.item()["title"])
        self.drain()
        self.assertEqual("Newer", self.item()["title"])

    def test_partial_rotating_read_retains_rows_and_does_not_certify_sweep(self):
        # Open assignments use the active refresh deadline, not terminal rotation.
        self.gateway.rows[("assignments", None)][0]["state"] = "closed"
        self.gateway.partial.add(("attests", "assignment"))
        self.gateway.rows[("attests", "assignment")] = []
        before = self.service.last_sweep
        previous_stage = self.item()["stage"]
        previous_complete = self.item()["evidence"]["stage"]["complete"]
        self.service.rebuild(recovery=False)
        self.drain()
        # R32/R10 / art_6fa7dbb4: retain the prior complete scoped facts
        # until this same read recovers; do not inject an item reread.
        self.assertIn(("attests", "assignment"), self.service.model.inventories)
        self.assertEqual(1, self.service.model.summary("attests", "assignment")["count"])
        self.assertEqual(previous_stage, self.item()["stage"])
        self.assertEqual(1, self.item()["evidenceCounts"]["total"])
        self.assertEqual(previous_complete, self.item()["evidence"]["stage"]["complete"])
        self.assertEqual("rebuilding", self.service.state)
        self.assertEqual("stale", self.item()["evidence"]["attests"]["freshness"])
        self.assertEqual(before, self.service.last_sweep)
        self.gateway.partial.clear()
        job = ("attests", "assignment", None)
        self.assertIn(job, self.service.read_retries)
        self.assertNotIn(("item-assignment-source", "work", None), self.service.read_retries)
        deadline, _ = self.service.read_retries[job]
        self.gateway.calls.clear()
        with patch("atc_service.time.monotonic", return_value=deadline):
            self.service._retry_incomplete_reads()
        self.drain()
        self.assertEqual([("read", "attests", "assignment")], self.gateway.calls)
        self.assertNotIn(job, self.service.read_retries)
        self.assertEqual(0, self.item()["stage"])
        self.assertEqual(0, self.item()["evidenceCounts"]["total"])

    def test_assignment_notice_recovers_a_holder_missing_from_session_inventory(self):
        self.gateway.rows[("sessions", None)].append({
            "sessionKey": "agent s_new", "state": "active", "displayName": "New",
        })
        self.gateway.rows[("assignments", None)].append({
            "id": "new", "holderKey": "agent s_new", "workItemId": "work", "state": "open",
        })
        self.service.change({"resource": "assignments", "refs": {"assignmentId": "new"}})
        self.drain()
        self.assertIn(("detail", "sessions", None, "agent s_new"), self.gateway.calls)
        self.assertTrue(self.service.model.inventory("turns", "agent s_new").complete)

    def test_reconnect_upgrades_an_in_progress_interval_to_full_recovery(self):
        self.service.scope_budget = 1
        self.service.rebuild(recovery=False)
        self.service.rebuild(recovery=True)
        self.assertEqual("recovery", self.service.cycle["mode"])
        self.gateway.calls.clear()
        self.drain()
        histories = {(r, scope) for kind, r, scope in self.gateway.calls
                     if r in {"attests", "turns", "wakes", "messages"}}
        self.assertEqual(set(self.service._history_scopes()), histories)
        self.assertEqual("live", self.service.state)


    def test_delta_replaces_rows_and_complete_read_can_remove_them(self):
        self.service._publish()
        baseline = self.service.snapshot()
        self.service.model.accept("work-items", None, Read([work("Updated", 3)], True, 30, "read"))
        self.service._publish()
        revision, frame = self.service.next_event(baseline["revision"], timeout=0)
        self.assertTrue(frame.startswith(b"event: delta\n"))
        delta = json.loads(frame.split(b"data: ", 1)[1])
        self.assertEqual(baseline["revision"], delta["baseRevision"])
        self.assertEqual(revision, delta["revision"])
        self.assertEqual("Updated", delta["changes"]["items"]["upsert"][0]["title"])
        self.assertEqual([], delta["changes"]["items"]["remove"])
        for name in ("freshness", "gatewayVersion", "metrics", "generation", "generatedAt"):
            self.assertEqual(self.service.snapshot()[name], delta[name])
        self.service.model.accept("work-items", None, Read([], True, 40, "read"))
        self.service._publish()
        final_revision, frame = self.service.next_event(revision, timeout=0)
        delta = json.loads(frame.split(b"data: ", 1)[1])
        self.assertEqual(["work"], delta["changes"]["items"]["remove"])
        # A subscriber still within the bounded history receives each delta in order.
        _, frame = self.service.next_event(baseline["revision"], timeout=0)
        frames = [part for part in frame.split(b"\n\n") if part]
        self.assertEqual(2, len(frames))
        catch_up = [json.loads(part.split(b"data: ", 1)[1]) for part in frames]
        self.assertEqual(baseline["revision"], catch_up[0]["baseRevision"])
        self.assertEqual(revision, catch_up[0]["revision"])
        self.assertEqual(revision, catch_up[1]["baseRevision"])
        self.assertEqual(final_revision, catch_up[1]["revision"])

    def test_status_delta_has_no_clock_only_agent_upserts(self):
        self.service._publish()
        revision = self.service.revision
        self.service._state("rebuilding", "interval read")
        self.service._publish()
        _, frame = self.service.next_event(revision, timeout=0)
        delta = json.loads(frame.split(b"data: ", 1)[1])
        self.assertEqual("rebuilding", delta["freshness"]["state"])
        self.assertTrue(all(not change["upsert"] and not change["remove"]
                            for change in delta["changes"].values()))

    def test_stale_idle_age_does_not_advance_on_unrelated_updates_or_resync(self):
        with patch("atc_service.now_ms", return_value=10000):
            self.service._publish()
        self.service._state("stale", "lost")
        self.service.model.invalidate()
        self.service._publish()
        frozen = self.service.snapshot()["agents"][0]["idleFor"]
        self.service.model.inventory("work-items").rows["work"]["title"] = "Other change"
        with patch("atc_service.now_ms", return_value=50000):
            self.service._publish()
        self.assertEqual(frozen, self.service.snapshot()["agents"][0]["idleFor"])

    def test_partial_retirement_read_does_not_remove_projected_agent(self):
        assignments = deepcopy(self.gateway.rows[("assignments", None)])
        assignments[0]["state"] = "closed"
        self.service.model.accept("assignments", None, Read(assignments, True, 20, "read"))
        self.service._publish()
        revision = self.service.revision
        sessions = deepcopy(self.gateway.rows[("sessions", None)])
        sessions[0]["state"] = "retired"
        self.service.model.accept("sessions", None, Read(sessions, False, 30, "read"))
        self.service._publish()
        _, frame = self.service.next_event(revision, timeout=0)
        delta = json.loads(frame.split(b"data: ", 1)[1])
        self.assertEqual([], delta["changes"]["agents"]["remove"])
        self.assertEqual("unknown", self.service.snapshot()["agents"][0]["evidence"]["identity"]["status"])
        revision = self.service.revision
        self.service.model.accept("sessions", None, Read(sessions, True, 40, "read"))
        self.service._publish()
        _, frame = self.service.next_event(revision, timeout=0)
        delta = json.loads(frame.split(b"data: ", 1)[1])
        self.assertEqual(["s_author"], delta["changes"]["agents"]["remove"])

    def test_published_build_policy_applies_full_attest_without_reading_history(self):
        from atc_payloads import PAYLOAD_POLICIES
        from atc_upstream import ATTEST_FIELDS
        self.service.policies = PAYLOAD_POLICIES
        row = dict.fromkeys(ATTEST_FIELDS) | {
            "id": "new-progress", "assignmentId": "assignment", "kind": "progress",
            "note": "Original note", "bySession": "agent s_author", "ts": 15,
            "commitRefs": [], "producer": "compiler", "producerCommand": "build",
        }
        notice = {"resource": "attests", "op": "upsert", "class": "attest.filed",
                  "refs": {"attestId": row["id"]}, "payload": row}
        self.gateway.calls.clear()
        self.service.change(notice)
        self.service.change(notice)
        self.assertEqual({}, self.service.reads.pending)
        self.assertEqual([], self.gateway.calls)
        self.assertEqual(row, self.service.model.inventory("attests", "assignment").rows[row["id"]])
        self.assertTrue(self.service.model.inventory("attests", "assignment").complete)

    def _producer_recovery_fixture(self, *, identified=True, published=True, workers=1,
                                   second_review=False):
        from atc_payloads import ASSIGNMENT_FIELDS
        review = dict.fromkeys(ASSIGNMENT_FIELDS) | dict(
            id='assignment', workItemId='work', holderKey='agent s_author',
            state='open', openedAt=1, files=[], effectKind='review',
            reviewsAssignmentId='producer' if identified else None)
        producer = dict.fromkeys(ASSIGNMENT_FIELDS) | dict(
            id='producer', workItemId='work', holderKey='agent s_author',
            state='open', openedAt=0, files=[], effectKind='code')
        self.gateway.rows['assignments', None] = [review, producer]
        if second_review:
            other = dict(review, id='second-review', openedAt=2)
            self.gateway.rows['assignments', None].insert(0, other)
            self.gateway.rows['attests', 'second-review'] = []
        self.gateway.rows['attests', 'producer'] = []
        self.service.stop()
        self.service = LiveService(self.gateway, workers=workers)
        version = {'version': '0.1.9', 'build': 2908, 'sha': '8433d22f'}
        self.service._handle('ready', version)
        self.drain()
        if published:
            self.service._publish()
        self.gateway.calls.clear()
        return review, producer

    @staticmethod
    def _assignment_close_notice(row):
        return dict(resource='assignments', op='upsert',
                    refs={'assignmentId': row['id']}, payload=deepcopy(row),
                    **{'class': 'assignment.closed'})

    def _assert_producer_recovery_fresh_and_replayed(self, baseline, label):
        from publication_assertions import assert_publication_equal, assert_replay_current
        gateway = PublishedReads()
        gateway.rows = deepcopy(self.gateway.rows)
        fresh = LiveService(gateway)
        self.addCleanup(fresh.stop)
        fresh._handle('ready', self.service.version)
        self.drain(fresh)
        fresh._publish()
        assert_publication_equal(self, fresh.current, self.service.current, label)
        assert_replay_current(self, self.service, baseline, label + '-replay')

    def test_completion_payload_reads_only_assignment_side_effect(self):
        # Record45 owner mapping art_762b0bdd; PO att_e33b4539, R23/R36:
        # completion attest alone grants no read. Its real associated close,
        # with no identified P, holds prior publication until a covering read.
        from atc_upstream import ATTEST_FIELDS
        review, _producer = self._producer_recovery_fixture(identified=False)
        baseline = deepcopy(self.service.current)
        row = dict.fromkeys(ATTEST_FIELDS) | {
            'id': 'completed', 'assignmentId': 'assignment', 'kind': 'completion',
            'ts': 15, 'bySession': 'agent s_author', 'commitRefs': [],
        }
        self.gateway.rows['attests', 'assignment'].append(deepcopy(row))
        self.service.change(dict(resource='attests', op='upsert',
            refs={'attestId': row['id'], 'assignmentId': 'assignment'}, payload=row,
            **{'class': 'attest.filed'}))
        self.assertFalse(self.service.reads.pending)
        self.assertEqual([], self.gateway.calls)
        review.update(state='closed', closedAt=15, closingAttestId=row['id'])
        self.assertTrue(self.service.change(self._assignment_close_notice(review)))
        self.service._publish()
        self.assertFalse(self.service.reads.pending)
        self.assertEqual([], self.gateway.calls)
        held = self.service.assignment_holds.assignments['assignment']
        self.assertTrue(held.producer_recovery)
        self.assertIsNone(held.producer)
        expected = deepcopy(baseline['items'])
        for item in expected:
            for fact in item.get('evidence', {}).values():
                if fact['freshness'] != 'stale':
                    fact['freshness'] = 'unconfirmed'
                fact['unconfirmedSince'] = min(fact.get('unconfirmedSince') or 20, 20)
        self.assertEqual(expected, self.service.current['items'])
        # This is an explicitly supplied already-scheduled covering read,
        # not a new schedule introduced by either delivered notice.
        self.service._refresh('assignments')
        self.drain()
        self.service._publish()
        self.assertFalse(self.service.assignment_holds.assignments)
        self.assertFalse([call for call in self.gateway.calls if call[0] in {'detail', 'membership'}])
        contribution = self.service.model.item_contributions.entries['assignment']
        self.assertEqual('closed', contribution.state)
        self.assertEqual(2, contribution.attests.count)
        self._assert_producer_recovery_fresh_and_replayed(baseline, 'r36-no-producer')

    def test_gateway_payload_policy_preserves_topology_and_order(self):
        from atc_payloads import PAYLOAD_POLICIES, SESSION_FIELDS
        self.service.policies = PAYLOAD_POLICIES
        row = dict.fromkeys(SESSION_FIELDS) | {
            "sessionKey": "agent s_author", "displayName": "Old", "state": "active",
            "rowVersion": 10, "capabilities": {},
        }
        self.service.model.accept("sessions", None, Read([row], True, 10, "read"))
        changed = dict(row, displayName="New", rowVersion=11, topologyParent="agent s_parent")
        notice = {"resource": "sessions", "op": "upsert", "class": "session.updated",
                  "refs": {"sessionKey": row["sessionKey"]}, "payload": changed}
        self.gateway.calls.clear()
        self.service.change(notice)
        self.service.change(dict(notice, payload=row))
        current = self.service.model.inventory("sessions").rows[row["sessionKey"]]
        self.assertEqual(changed, current)
        self.assertEqual({}, self.service.reads.pending)
        self.assertEqual([], self.gateway.calls)

    def test_review_attest_reads_producer_assignment_without_recrawling_evidence(self):
        # Record48: verdict-only is a negative permission check. The actual
        # assignment.closed notice identifies P. Owner art_762b0bdd agrees
        # this explicit mapping under PO att_e33b4539, R23 and bound R36.
        from atc_upstream import ATTEST_FIELDS
        review, producer = self._producer_recovery_fixture()
        baseline = deepcopy(self.service.current)
        row = dict.fromkeys(ATTEST_FIELDS) | {
            'id': 'review', 'assignmentId': 'assignment', 'kind': 'verdict',
            'verdictKind': 'reviewed-clean', 'ts': 15, 'bySession': 'agent s_author',
            'commitRefs': [],
        }
        self.gateway.rows['attests', 'assignment'].append(deepcopy(row))
        self.service.change(dict(resource='attests', op='upsert',
            refs={'attestId': row['id'], 'assignmentId': 'assignment'}, payload=row,
            **{'class': 'attest.filed'}))
        self.assertFalse(self.service.reads.pending)
        self.assertEqual([], self.gateway.calls)
        producer.update(state='closed', closedAt=16)
        review.update(state='closed', closedAt=17, closingAttestId='review')
        self.assertTrue(self.service.change(self._assignment_close_notice(review)))
        self.assertEqual({('reviewed-producer', 'producer', None)}, set(self.service.reads.pending))
        self.drain()
        self.service._publish()
        self.assertEqual([('detail', 'assignments', None, 'producer')], self.gateway.calls)
        self.assertFalse(self.service.assignment_holds.assignments)
        self.assertFalse(self.service.reads.assignment_reads.flights)
        self.assertEqual('closed', self.service.model.item_contributions.entries['producer'].state)
        self.assertEqual(16, self.service.model.item_contributions.entries['producer'].closed_at)
        self.assertNotIn('producer', self.service.model.inventory('assignments').rows)
        self._assert_producer_recovery_fresh_and_replayed(baseline, 'r36-known-producer')

    def test_attest_side_effect_invalidates_an_older_assignment_read(self):
        # Record44 retains the original blocked keyed read of A. R29 compares
        # its actual old row with the close; R36 does not permit a shared epoch
        # restart. Owner art_762b0bdd / PO att_e33b4539 / R23 / R36.
        review, producer = self._producer_recovery_fixture()
        baseline = deepcopy(self.service.current)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.gateway.detail
        def captured(resource, key, scope=None):
            result = original(resource, key, scope)
            if key == 'assignment':
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release read')
            return result
        self.gateway.detail = captured
        self.service._refresh('assignments', key='assignment')
        self.service.reads.pump()
        self.assertTrue(entered.wait(2))
        epochs = dict(self.service.reads.epochs)
        from atc_upstream import ATTEST_FIELDS
        completion = dict.fromkeys(ATTEST_FIELDS) | dict(id='completed',
            assignmentId='assignment', kind='completion', ts=15,
            bySession='agent s_author', commitRefs=[])
        self.gateway.rows['attests', 'assignment'].append(deepcopy(completion))
        self.service.change(dict(resource='attests', op='upsert',
            refs={'attestId': 'completed', 'assignmentId': 'assignment'},
            payload=completion, **{'class': 'attest.filed'}))
        self.assertFalse(self.service.reads.pending)
        producer.update(state='closed', closedAt=16)
        review.update(state='closed', closedAt=17)
        self.assertEqual('producer', self.service.model.inventory('assignments').rows[
            'assignment']['reviewsAssignmentId'])
        self.service.change(self._assignment_close_notice(review))
        self.assertEqual(epochs.get(('assignments', None), 0),
                         self.service.reads.epochs.get(('assignments', None), 0))
        release.set()
        kind, value = self.service.events.get(timeout=2)
        self.assertEqual('read', kind)
        self.assertTrue(self.service._read_done(value))
        self.assertTrue(self.service.assignment_holds.assignments['assignment'].conflict)
        self.drain()
        self.service._publish()
        self.assertEqual(0, self.service.reads.epoch_discards)
        self.assertEqual([('detail', 'assignments', None, 'assignment'),
                          ('detail', 'assignments', None, 'producer')], self.gateway.calls)
        self.assertEqual('closed', self.service.model.item_contributions.entries['assignment'].state)
        self.assertEqual('closed', self.service.model.item_contributions.entries['producer'].state)
        self.assertTrue(self.service.assignment_holds.assignments['assignment'].conflict)
        self.assertFalse(self.service.assignment_holds.assignments['assignment'].producer_recovery)
        self.assertEqual(baseline['items'][0]['assignments'], self.service.current['items'][0]['assignments'])
        self.service._refresh('assignments')
        self.drain()
        self.service._publish()
        self.assertFalse(self.service.assignment_holds.assignments)
        self.assertFalse([call for call in self.gateway.calls if call[0] == 'membership'])
        self._assert_producer_recovery_fresh_and_replayed(baseline, 'r36-older-keyed-read')

    def _assert_producer_hold_publication(self, baseline, label):
        from publication_assertions import (assert_publication_equal,
            assert_replay_current, held_scope_metadata)
        gateway = PublishedReads()
        gateway.rows = deepcopy(self.gateway.rows)
        fresh = LiveService(gateway, scope_budget=self.service.scope_budget,
                            rebuild_interval=self.service.rebuild_interval)
        self.addCleanup(fresh.stop)
        fresh._handle('ready', self.service.version)
        self.drain(fresh)
        fresh._publish()
        expected = deepcopy(fresh.current)
        permitted, deltas = held_scope_metadata(self, fresh, self.service, label)
        for path, value in permitted.items():
            bits = path.split('.')
            target = expected['freshness']['reconciliation']
            if len(bits) == 2:
                target = target[bits[0]]
            target[bits[-1]] = value
        holds = self.service.assignment_holds
        for wid, prior in holds.items.items():
            if prior is None:
                self.assertNotIn(wid, {item['id'] for item in baseline['items']})
                expected['items'] = [item for item in expected['items'] if item['id'] != wid]
                continue
            self.assertEqual(next(item for item in baseline['items'] if item['id'] == wid), prior)
            retained = deepcopy(prior)
            for fact in retained.get('evidence', {}).values():
                if fact['freshness'] != 'stale':
                    fact['freshness'] = 'unconfirmed'
                previous = fact.get('unconfirmedSince')
                since = holds.since(wid, producer_items=self.service._held_producer_items())
                fact['unconfirmedSince'] = since if previous is None else min(previous, since)
            expected['items'] = [retained if item['id'] == wid else item
                                 for item in expected['items']]
        assert_publication_equal(self, expected, self.service.current, label)
        assert_replay_current(self, self.service, baseline, label + '-replay')
        return deltas, fresh

    def test_shared_producer_scope_pending_failed_and_existing_resolvers(self):
        from publication_assertions import held_scope_identities, assert_publication_equal
        for resolver in ('targeted', 'covering', 'restart'):
            with self.subTest(resolver=resolver):
                review, producer = self._producer_recovery_fixture(second_review=True)
                other = next(row for row in self.gateway.rows['assignments', None]
                             if row['id'] == 'second-review')
                baseline = deepcopy(self.service.current)
                producer.update(state='closed', closedAt=16)
                review.update(state='closed', closedAt=17)
                self.assertTrue(self.service.change(self._assignment_close_notice(review)))
                self.service._publish()
                first, _fresh = self._assert_producer_hold_publication(
                    baseline, 'r37-single-pending-' + resolver)
                self.assertEqual(1, first['historyScopes'])
                self.assertEqual(1, first['active.scopeCount'])
                other.update(state='closed', closedAt=18)
                self.assertTrue(self.service.change(self._assignment_close_notice(other)))
                self.service._publish()
                shared, _fresh = self._assert_producer_hold_publication(
                    baseline, 'r37-shared-pending-' + resolver)
                self.assertEqual(first, shared)
                self.assertEqual({('attests', 'producer')}, held_scope_identities(self.service))
                self.assertEqual({('reviewed-producer', 'producer', None)},
                                 set(self.service.reads.pending))
                original = self.gateway.detail
                def result(resource, key, scope=None):
                    read = original(resource, key, scope)
                    if resolver != 'targeted' and key == 'producer':
                        return Read([], False, read.at, read.source, 'TimeoutError')
                    return read
                with patch.object(self.gateway, 'detail', side_effect=result):
                    self.drain()
                self.service._publish()
                self.assertEqual([('detail', 'assignments', None, 'producer')], self.gateway.calls)
                if resolver == 'targeted':
                    self.assertFalse(self.service.assignment_holds.assignments)
                    self._assert_producer_recovery_fresh_and_replayed(
                        baseline, 'r37-shared-targeted-resolved')
                    continue
                failed, fresh = self._assert_producer_hold_publication(
                    baseline, 'r37-shared-failed-' + resolver)
                self.assertEqual(shared, failed)
                self.assertIn(('reviewed-producer', 'producer', None), self.service.read_retries)
                if resolver == 'restart':
                    # Restart is a separate normal bootstrap, not a mutation
                    # of the held server or a fabricated source confirmation.
                    restarted = LiveService(self.gateway)
                    self.addCleanup(restarted.stop)
                    restarted._handle('ready', self.service.version)
                    self.drain(restarted)
                    restarted._publish()
                    self.assertFalse(restarted.assignment_holds.assignments)
                    assert_publication_equal(self, fresh.current, restarted.current,
                                             'r37-shared-restart-resolved')
                else:
                    # Explicit already-scheduled covering source, not a new
                    # timer/read chain caused by the failed producer lookup.
                    self.service._refresh('assignments')
                    self.drain()
                    self.service._publish()
                    self.assertFalse(self.service.assignment_holds.assignments)
                    self.assertFalse([call for call in self.gateway.calls if call[0] == 'membership'])
                    self._assert_producer_recovery_fresh_and_replayed(
                        baseline, 'r37-shared-covering-resolved')

    def test_shared_producer_and_independent_assignment_scope_resolve_two_one_zero(self):
        from publication_assertions import held_scope_identities
        for failed in (False, True):
            with self.subTest(failed=failed):
                review, producer = self._producer_recovery_fixture(workers=2, second_review=True)
                other = next(row for row in self.gateway.rows['assignments', None]
                             if row['id'] == 'second-review')
                baseline = deepcopy(self.service.current)
                producer.update(state='closed', closedAt=16)
                review.update(state='closed', closedAt=17)
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                original = self.gateway.detail
                def captured(resource, key, scope=None):
                    result = original(resource, key, scope)
                    if key == 'producer':
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError('fixture did not release producer snapshot')
                        if failed:
                            return Read([], False, result.at, result.source, 'TimeoutError')
                    return result
                with patch.object(self.gateway, 'detail', side_effect=captured):
                    self.service.change(self._assignment_close_notice(review))
                    self.service.reads.pump()
                    self.assertTrue(entered.wait(2))
                    # Reopening is a real full projection. The earlier P hold
                    # remains independent; a later A read/close conflict cannot
                    # certify P or choose the mutable source ordering.
                    review.update(state='open', closedAt=None)
                    reopened = self._assignment_close_notice(review)
                    reopened['class'] = 'assignment.reopened'
                    self.assertTrue(self.service.change(reopened))
                    self.service._refresh('assignments', key='assignment')
                    self.service.reads.pump()
                    old = self.service.events.get(timeout=5)
                    self.assertEqual(('assignments', None, 'assignment'), old[1][0])
                    review.update(state='closed', closedAt=19)
                    self.service._queue_feed_change(self._assignment_close_notice(review))
                    self.service._handle(*old)
                    self.service._handle(*self.service.events.get(timeout=5))
                    self.assertTrue(self.service.assignment_holds.assignments['assignment'].conflict)
                    self.assertTrue(self.service.assignment_holds.assignments['assignment'].producer_recovery)
                    self.assertEqual('open', self.service.model.inventory('assignments').rows[
                        'assignment']['state'])
                    other.update(state='closed', closedAt=18)
                    self.service.change(self._assignment_close_notice(other))
                    self.service._publish()
                    self.assertEqual({('attests', 'producer'), ('attests', 'assignment')},
                                     held_scope_identities(self.service))
                    delta, _fresh = self._assert_producer_hold_publication(
                        baseline, 'r37-two-held-pending-' + str(failed))
                    self.assertEqual(2, delta['historyScopes'])
                    self.assertEqual(2, delta['active.scopeCount'])
                    self.assertFalse(self.service.reads.pending)
                    release.set()
                    self.service._handle(*self.service.events.get(timeout=5))
                self.service._publish()
                self.assertEqual(1, self.gateway.calls.count(
                    ('detail', 'assignments', None, 'producer')))
                if failed:
                    delta, _fresh = self._assert_producer_hold_publication(
                        baseline, 'r37-two-held-failed')
                    self.assertEqual(2, delta['historyScopes'])
                    self.assertEqual(2, delta['active.scopeCount'])
                    job = ('reviewed-producer', 'producer', None)
                    deadline, _priority = self.service.read_retries[job]
                    # Advance only the existing retry clock, preserving its
                    # selected transport/deadline and no item escalation.
                    with patch('atc_service.time.monotonic', return_value=deadline):
                        self.service._retry_incomplete_reads()
                    self.drain()
                    self.service._publish()
                self.assertTrue(self.service.assignment_holds.assignments['assignment'].conflict)
                self.assertFalse(self.service.assignment_holds.assignments['assignment'].producer_recovery)
                self.assertNotIn('second-review', self.service.assignment_holds.assignments)
                delta, _fresh = self._assert_producer_hold_publication(
                    baseline, 'r37-one-held-after-producer-' + str(failed))
                self.assertEqual(1, delta['historyScopes'])
                self.assertEqual(1, delta['active.scopeCount'])
                self.assertEqual('closed', self.service.model.item_contributions.entries['producer'].state)
                review.update(state='open', closedAt=None)
                resolver = self._assignment_close_notice(review)
                resolver['class'] = 'assignment.reopened'
                self.service.change(resolver)
                self.drain()
                self.service._publish()
                self.assertFalse(self.service.assignment_holds.assignments)
                self.assertEqual(2 if failed else 1, self.gateway.calls.count(
                    ('detail', 'assignments', None, 'producer')))
                self.assertFalse([call for call in self.gateway.calls if call[0] == 'membership'])
                self.assertEqual(0, self.service.reads.epoch_discards)
                self._assert_producer_recovery_fresh_and_replayed(
                    baseline, 'r37-zero-held-after-assignment-' + str(failed))

    def test_synthetic_held_only_producer_selector_branch(self):
        # PDO disposition1083: SYNTHETIC selector input only. The pinned
        # ordinary close publisher retains reviewsAssignmentId; null here is
        # not a source-shaped transition or full-recovery parity evidence.
        # Pair with test_reviewed_producer_service_coalesces_active_and_queued_lookup_and_discards_rows.
        review, _producer = self._producer_recovery_fixture()
        self.assertEqual('producer', self.service.model.inventory('assignments').rows[
            'assignment']['reviewsAssignmentId'])
        synthetic = dict(review, state='closed', closedAt=17, reviewsAssignmentId=None)
        with patch.object(self.service, '_request_reviewed_producer',
                          wraps=self.service._request_reviewed_producer) as requested:
            self.assertTrue(self.service.change(self._assignment_close_notice(synthetic)))
        requested.assert_called_once_with('assignment', 'work', 'producer')
        self.assertEqual({('reviewed-producer', 'producer', None)},
                         set(self.service.reads.pending))
        self.assertEqual([], self.gateway.calls)
        self.assertEqual('producer', self.service.assignment_holds.assignments[
            'assignment'].producer)
        # Deliberately do not claim publication parity for the artificial null.

    def test_source_shaped_producer_close_requires_complete_direct_projection(self):
        from atc_payloads import ASSIGNMENT_FIELDS
        review, producer = self._producer_recovery_fixture()
        baseline = deepcopy(self.service.current)
        producer.update(state='closed', closedAt=16)
        review.update(state='closed', closedAt=17)
        self.assertTrue(ASSIGNMENT_FIELDS <= review.keys())
        missing = deepcopy(review)
        del missing['reviewsAssignmentId']
        # A missing required field is not an exclusive notice-identity fixture.
        self.assertFalse(self.service._apply_assignment_notice(
            self._assignment_close_notice(missing), 'assignment', missing))
        self.assertEqual([], self.gateway.calls)
        self.assertFalse(self.service.reads.pending)
        with patch.object(self.service, '_request_reviewed_producer',
                          wraps=self.service._request_reviewed_producer) as requested:
            self.assertTrue(self.service.change(self._assignment_close_notice(review)))
        requested.assert_called_once_with('assignment', 'work', 'producer')
        self.drain()
        self.service._publish()
        self.assertEqual([('detail', 'assignments', None, 'producer')], self.gateway.calls)
        self.assertFalse(self.service.assignment_holds.assignments)
        self._assert_producer_recovery_fresh_and_replayed(baseline, 'r37-source-shaped-close')

    def test_unapplied_terminal_notice_without_producer_holds_without_item_read(self):
        # B6 / R30-R32, lane245; custodian agreement art_cb3f8f6b.
        # This is an unapplied first discovery, not F1's applied ordinary close.
        for published in (True, False):
            with self.subTest(published=published):
                review, _producer = self._producer_recovery_fixture(published=published)
                baseline = deepcopy(self.service.current)
                discovered = dict(review, id='discovered-terminal', openedAt=12,
                    state='closed', closedAt=17, effectKind='code', reviewsAssignmentId=None)
                self.gateway.rows['assignments', None].append(discovered)
                self.gateway.rows['attests', discovered['id']] = []
                epochs = dict(self.service.reads.epochs)
                with patch.object(self.service, '_request_reviewed_producer',
                                  wraps=self.service._request_reviewed_producer) as requested:
                    self.assertTrue(self.service.change(self._assignment_close_notice(discovered)))
                requested.assert_not_called()
                self.assertEqual([], self.gateway.calls)
                self.assertFalse(self.service.reads.pending)
                self.assertFalse(self.service.reads.active)
                self.assertEqual(epochs, self.service.reads.epochs)
                self.assertNotIn(discovered['id'], self.service.model.inventory('assignments').rows)
                self.assertNotIn(discovered['id'], self.service.model.item_contributions.entries)
                held = self.service.assignment_holds.assignments[discovered['id']]
                self.assertTrue(held.incomplete_contribution)
                self.assertFalse(held.producer_recovery)
                self.assertFalse(held.conflict)
                self.service._publish()
                label = 'b6-first-discovery-no-producer-' + str(published)
                self._assert_producer_hold_publication(baseline, label)
                if not published:
                    self.assertIsNone(self.service.assignment_holds.items['work'])
                    self.assertFalse(self.service.current['items'])

                # R38 keeps the R32 completeness rule: a next mutable notice
                # can apply, but cannot certify the missing attest history.
                discovered.update(state='open', closedAt=None)
                reopened = self._assignment_close_notice(discovered)
                reopened['class'] = 'assignment.reopened'
                self.assertTrue(self.service.change(reopened))
                self.service._publish()
                self.assertTrue(self.service.assignment_holds.assignments[
                    discovered['id']].incomplete_contribution)
                self.assertEqual([], self.gateway.calls)
                self.assertFalse(self.service.reads.pending)
                self._assert_producer_hold_publication(baseline, label + '-incomplete-next-notice')

                # Explicit fixture delivery of an independently scheduled
                # covering source; the notice must not schedule this read.
                self.service._request_item_assignments('work')
                self.drain()
                self.service._publish()
                self.assertEqual(1, len([call for call in self.gateway.calls
                                         if call[0] == 'membership']))
                self.assertFalse(self.service.assignment_holds.assignments)
                self._assert_producer_recovery_fresh_and_replayed(baseline, label + '-resolved')

    def test_applied_incomplete_close_reads_producer_and_keeps_contribution_hold(self):
        # R20 establishes an eligible missing-contribution hold without keeping
        # its lookup row. The later full close can then apply under R32/R36.
        # This is separate from the unapplied first-discovery F2 case.
        from atc_upstream import ATTEST_FIELDS
        review, producer = self._producer_recovery_fixture()
        baseline = deepcopy(self.service.current)
        discovered = dict(review, id='incomplete-review', openedAt=12,
                          state='closed', closedAt=17)
        self.gateway.rows['assignments', None].append(discovered)
        progress = dict.fromkeys(ATTEST_FIELDS) | dict(id='incomplete-progress',
            assignmentId=discovered['id'], kind='progress', ts=15,
            bySession='agent s_author', commitRefs=[])
        self.gateway.rows['attests', discovered['id']] = [progress]
        returned = []
        original = self.gateway.detail
        def captured(resource, key, scope=None):
            result = original(resource, key, scope)
            if resource == 'assignments':
                result.rows[0].pop('files', None)
                result.rows[0]['reopenings'] = []
                returned.append(result)
            return result
        self.gateway.detail = captured
        self.assertTrue(self.service.change(dict(resource='attests', op='upsert',
            refs={'attestId': progress['id'], 'assignmentId': discovered['id']},
            payload=deepcopy(progress), **{'class': 'attest.filed'})))
        self.drain()
        self.assertEqual([('detail', 'assignments', None, discovered['id'])], self.gateway.calls)
        self.assertEqual([], returned[0].rows)
        self.assertNotIn(discovered['id'], self.service.model.inventory('assignments').rows)
        self.assertNotIn(discovered['id'], self.service.model.item_contributions.entries)
        self.assertTrue(self.service.assignment_holds.assignments[discovered['id']].incomplete_contribution)

        # The hidden producer closure belongs only to this qualifying applied
        # close case. No relation is erased from an established A projection.
        producer.update(state='closed', closedAt=16)
        with patch.object(self.service, '_request_reviewed_producer',
                          wraps=self.service._request_reviewed_producer) as requested:
            self.assertTrue(self.service.change(self._assignment_close_notice(discovered)))
        requested.assert_called_once_with(discovered['id'], 'work', 'producer')
        self.assertEqual(discovered,
                         self.service.model.inventory('assignments').rows[discovered['id']])
        self.assertEqual({('reviewed-producer', 'producer', None)}, set(self.service.reads.pending))
        self.drain()
        self.assertEqual([('detail', 'assignments', None, discovered['id']),
                          ('detail', 'assignments', None, 'producer')], self.gateway.calls)
        self.assertTrue(all(not result.rows for result in returned))
        self.assertNotIn('producer', self.service.model.inventory('assignments').rows)
        self.assertEqual('closed', self.service.model.item_contributions.entries['producer'].state)
        held = self.service.assignment_holds.assignments[discovered['id']]
        self.assertTrue(held.incomplete_contribution)
        self.assertFalse(held.producer_recovery)
        self.service._publish()
        self._assert_producer_hold_publication(baseline, 'r37-applied-incomplete-producer-resolved')
        self.assertFalse(self.service.reads.pending)

        self.service._request_item_assignments('work')  # existing scheduled covering route
        self.drain()
        self.service._publish()
        self.assertFalse(self.service.assignment_holds.assignments)
        self.assertEqual(1, len([call for call in self.gateway.calls if call[0] == 'membership']))
        self._assert_producer_recovery_fresh_and_replayed(baseline, 'r37-applied-incomplete-covered')

    def test_first_discovery_close_supplies_producer_identity_only_in_notice(self):
        # Source-shaped first discovery, separately labeled from fully loaded
        # recovery. No prior relation is erased to manufacture this case.
        # R38 / PO att_3533a567: P recovery does not complete unapplied A.
        review, producer = self._producer_recovery_fixture()
        baseline = deepcopy(self.service.current)
        discovered = dict(review, id='discovered-review', openedAt=12,
                          state='closed', closedAt=17)
        self.gateway.rows['assignments', None].append(discovered)
        self.gateway.rows['attests', 'discovered-review'] = []
        producer.update(state='closed', closedAt=16)
        self.assertNotIn('discovered-review', self.service.model.inventory('assignments').rows)
        self.assertNotIn('discovered-review', self.service.model.item_contributions.entries)
        with patch.object(self.service, '_request_reviewed_producer',
                          wraps=self.service._request_reviewed_producer) as requested:
            self.assertTrue(self.service.change(self._assignment_close_notice(discovered)))
        requested.assert_called_once_with('discovered-review', 'work', 'producer')
        self.assertIn(('reviewed-producer', 'producer', None), self.service.reads.pending)
        self.drain()
        self.service._publish()
        self.assertEqual(1, self.gateway.calls.count(('detail', 'assignments', None, 'producer')))
        self.assertFalse([call for call in self.gateway.calls if call[0] == 'membership'])
        held = self.service.assignment_holds.assignments['discovered-review']
        self.assertTrue(held.incomplete_contribution)
        self.assertFalse(held.producer_recovery)
        self._assert_producer_hold_publication(baseline, 'r38-first-discovery-P-resolved-A-held')
        self.assertFalse(self.service.reads.pending)
        self.service._request_item_assignments('work')  # explicit existing covering source
        self.drain()
        self.service._publish()
        self.assertFalse(self.service.assignment_holds.assignments)
        self._assert_producer_recovery_fresh_and_replayed(
            baseline, 'r37-first-discovery-notice-only')

    def test_first_discovery_shared_producer_on_distinct_item_recovers_independently(self):
        # R38 / PO att_3533a567; owner agreement art_f1c12430.
        # Actual source-shaped A notices, with no prior A relation removed.
        from publication_assertions import assert_publication_equal
        cases = ((True, 'closed', 'targeted'), (False, 'open', 'targeted'),
                 (True, 'closed', 'retry'), (False, 'closed', 'covering'),
                 (True, 'closed', 'restart'))
        for published, state, resolver in cases:
            with self.subTest(published=published, state=state, resolver=resolver):
                self.gateway = PublishedReads()
                review, producer = self._producer_recovery_fixture()
                item = dict(self.gateway.rows['work-items', None][0],
                            id='review-item', title='Review item')
                self.gateway.rows['work-items', None].append(item)
                self.service._refresh('work-items')
                self.drain()
                if published:
                    self.service._publish()
                baseline = deepcopy(self.service.current)
                self.assertEqual(published, any(item['id'] == 'review-item'
                                               for item in baseline['items']))
                discovered = [dict(review, id=aid, workItemId='review-item',
                    openedAt=12, state='closed', closedAt=17)
                    for aid in ('discovered-one', 'discovered-two')]
                for row in discovered:
                    self.gateway.rows['assignments', None].append(row)
                    self.gateway.rows['attests', row['id']] = []
                producer.update(state=state, closedAt=16 if state == 'closed' else None)
                self.gateway.calls.clear()
                targeted_results = []
                original = self.gateway.detail
                def captured(resource, key, scope=None):
                    result = original(resource, key, scope)
                    if key == 'producer':
                        result.rows[0].pop('files', None)
                        result.rows[0]['reopenings'] = []
                    return result
                self.gateway.detail = captured
                original_read = self.service.reads._read
                def capture_targeted(job, keep_ids=(), flight=None):
                    result = original_read(job, keep_ids, flight)
                    if job == ('reviewed-producer', 'producer', None):
                        targeted_results.append(result)
                    return result
                def drain_capturing_targeted():
                    # PDO 1105 / owner art_6fc45e6e: ordinary item detail
                    # results have a different ownership/disposal boundary.
                    with patch.object(self.service.reads, '_read', side_effect=capture_targeted):
                        self.drain()
                for row in discovered:
                    self.assertTrue(self.service.change(self._assignment_close_notice(row)))
                    self.assertNotIn(row['id'], self.service.model.inventory('assignments').rows)
                    self.assertNotIn(row['id'], self.service.model.item_contributions.entries)
                if resolver == 'retry':
                    # A later incomplete reopen supplies A1's mutable facts,
                    # not its attest completeness. The source then closes A1
                    # again before that next close is delivered. Its retained
                    # scope and shared P are two distinct held scopes.
                    discovered[0].update(state='open', closedAt=None)
                    reopened = self._assignment_close_notice(discovered[0])
                    reopened['class'] = 'assignment.reopened'
                    self.assertTrue(self.service.change(reopened))
                    self.assertTrue(self.service.assignment_holds.assignments[
                        discovered[0]['id']].incomplete_contribution)
                    discovered[0].update(state='closed', closedAt=18)
                job = ('reviewed-producer', 'producer', None)
                self.assertEqual({job}, set(self.service.reads.pending))
                self.assertEqual([], self.gateway.calls)
                self.service._publish()
                label = 'r38-distinct-' + str(published) + '-' + state + '-' + resolver
                deltas, _fresh = self._assert_producer_hold_publication(baseline, label + '-pending')
                expected_scopes = (1 if state == 'closed' else 0) + (1 if resolver == 'retry' else 0)
                self.assertEqual(expected_scopes, deltas['historyScopes'])
                self.assertEqual(expected_scopes, deltas['active.scopeCount'])
                self.assertIn('work', self.service.assignment_holds.items)
                self.assertIn('review-item', self.service.assignment_holds.items)
                if resolver != 'targeted':
                    def failed(resource, key, scope=None):
                        result = captured(resource, key, scope)
                        if key == 'producer':
                            result.complete = False
                            result.error = 'TimeoutError'
                        return result
                    with patch.object(self.gateway, 'detail', side_effect=failed):
                        drain_capturing_targeted()
                    self.service._publish()
                    failed_deltas, fresh = self._assert_producer_hold_publication(baseline, label + '-failed')
                    self.assertEqual(deltas, failed_deltas)
                    self.assertIn(job, self.service.read_retries)
                    self.assertEqual([('detail', 'assignments', None, 'producer')], self.gateway.calls)
                    self.assertEqual(1, len(targeted_results))
                    self.assertTrue(all(not value.rows for value in targeted_results))
                    if resolver == 'restart':
                        restarted = LiveService(self.gateway)
                        self.addCleanup(restarted.stop)
                        restarted._handle('ready', self.service.version)
                        self.drain(restarted)
                        restarted._publish()
                        self.assertFalse(restarted.assignment_holds.assignments)
                        assert_publication_equal(self, fresh.current, restarted.current, label + '-restart')
                        continue
                    if resolver == 'covering':
                        # An existing independently scheduled P-item source
                        # resolves P, but is not a complete source for A's item.
                        self.service._request_item_assignments('work')
                    else:
                        _, priority = self.service.read_retries[job]
                        self.service.read_retries[job] = (time.monotonic() - 1, priority)
                        self.service._retry_incomplete_reads()
                drain_capturing_targeted()
                self.service._publish()
                self.assertEqual(2 if resolver == 'retry' else 1, len(targeted_results))
                self.assertTrue(all(not value.rows for value in targeted_results))
                for row in discovered:
                    held = self.service.assignment_holds.assignments[row['id']]
                    self.assertTrue(held.incomplete_contribution)
                    self.assertFalse(held.producer_recovery)
                self.assertNotIn('work', self.service.assignment_holds.items)
                self.assertIn('review-item', self.service.assignment_holds.items)
                self.assertEqual(state, self.service.model.item_contributions.entries['producer'].state)
                self.assertEqual(1 if resolver == 'covering' else 0,
                                 len([call for call in self.gateway.calls if call[0] == 'membership']))
                remaining, _fresh = self._assert_producer_hold_publication(
                    baseline, label + '-P-resolved-A-held')
                self.assertEqual(1 if resolver == 'retry' else 0, remaining['historyScopes'])
                self.assertEqual(1 if resolver == 'retry' else 0, remaining['active.scopeCount'])
                self.service._request_item_assignments('review-item')  # existing A covering source
                self.drain()
                self.service._publish()
                self.assertFalse(self.service.assignment_holds.assignments)
                self.assertFalse(self.service.assignment_holds.items)
                self._assert_producer_recovery_fresh_and_replayed(baseline, label + '-all-resolved')

    def test_unapplied_no_producer_hold_survives_partial_failed_covering_read(self):
        from publication_assertions import assert_replay_current
        for published in (True, False):
            with self.subTest(published=published):
                self.gateway = PublishedReads()
                review, _producer = self._producer_recovery_fixture(published=published)
                baseline = deepcopy(self.service.current)
                discovered = dict(review, id='discovered-terminal', openedAt=12,
                    state='closed', closedAt=17, effectKind='code', reviewsAssignmentId=None)
                self.gateway.rows['assignments', None].append(discovered)
                self.gateway.rows['attests', discovered['id']] = []
                self.assertTrue(self.service.change(self._assignment_close_notice(discovered)))
                self.service._publish()
                label = 'r38-a-covering-failure-' + str(published)
                self._assert_producer_hold_publication(baseline, label + '-held')
                prior = deepcopy(self.service.current['items'])
                self.assertEqual([], self.gateway.calls)
                self.assertFalse(self.service.reads.pending)
                original = self.gateway.assignment_membership
                job = ('item-assignment-source', 'work', None)
                # This source is independently scheduled; neither the notice
                # nor failure may invent a read or change the retry's scope.
                self.service._request_item_assignments('work')
                for failure in ('partial', 'timeout'):
                    def incomplete(wid, *, include_rows=False):
                        result = original(wid, include_rows=include_rows)
                        if failure == 'timeout':
                            raise TimeoutError('fixture membership timeout')
                        return Read(result.rows, False, result.at, result.source, 'partial')
                    with patch.object(self.gateway, 'assignment_membership', side_effect=incomplete):
                        self.drain()
                    self.service._publish()
                    self.assertEqual(prior, self.service.current['items'])
                    held = self.service.assignment_holds.assignments[discovered['id']]
                    self.assertTrue(held.incomplete_contribution)
                    self.assertFalse(held.producer_recovery)
                    self.assertIsNone(held.producer)
                    self.assertNotIn(discovered['id'], self.service.model.inventory('assignments').rows)
                    self.assertNotIn(discovered['id'], self.service.model.item_contributions.entries)
                    self.assertIn(job, self.service.read_retries)
                    self.assertFalse(self.service.reads.assignment_reads.flights)
                    self.assertFalse(self.service.reads.pending)
                    assert_replay_current(self, self.service, baseline, label + '-' + failure)
                    _deadline, priority = self.service.read_retries[job]
                    self.service.read_retries[job] = (0, priority)
                    self.service._retry_incomplete_reads()
                    self.assertEqual({job}, set(self.service.reads.pending))
                self.drain()
                self.service._publish()
                self.assertEqual(3, self.gateway.calls.count(('membership', 'work')))
                self.assertFalse(self.service.assignment_holds.assignments)
                self.assertFalse(self.service.assignment_holds.items)
                self.assertNotIn(job, self.service.read_retries)
                self.assertEqual(0, self.service.reads.epoch_discards)
                self._assert_producer_recovery_fresh_and_replayed(baseline, label + '-resolved')

    def test_unapplied_review_recovery_survives_older_global_or_producer_item_read(self):
        # R38 first discovery extends the existing R29 selective-merge seam.
        # Block only delivery of an actual complete result, not its contents.
        for resource, partial_producer in (('assignments', False),
                                           ('item-assignment-source', False),
                                           ('item-assignment-source', True)):
            with self.subTest(resource=resource, partial_producer=partial_producer):
                self.gateway = PublishedReads()
                review, producer = self._producer_recovery_fixture(workers=2)
                item = dict(self.gateway.rows['work-items', None][0],
                            id='review-item', title='Review item')
                self.gateway.rows['work-items', None].append(item)
                self.service._refresh('work-items')
                self.drain()
                self.service._publish()
                baseline = deepcopy(self.service.current)
                discovered = dict(review, id='unapplied-review', workItemId='review-item',
                                  state='open', closedAt=None, openedAt=12)
                self.gateway.rows['assignments', None].append(discovered)
                self.gateway.rows['attests', discovered['id']] = []
                self.gateway.calls.clear()
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                original = self.service.reads._read
                older = ('assignments', None, None) if resource == 'assignments' else (
                    'item-assignment-source', 'work', None)
                def delayed(job, keep_ids=(), flight=None):
                    result = original(job, keep_ids, flight)
                    if job == older:
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError('fixture did not release older complete source')
                    if partial_producer and job == ('reviewed-producer', 'producer', None):
                        return Read(result.rows, False, result.at, result.source, 'partial')
                    return result
                with patch.object(self.service.reads, '_read', side_effect=delayed):
                    if resource == 'assignments':
                        self.service._refresh('assignments')
                    else:
                        self.service._request_item_assignments('work')
                    self.service.reads.pump()
                    self.assertTrue(entered.wait(2))
                    old_flight = self.service.reads.assignment_reads.flights[older]
                    before = list(self.gateway.calls)
                    epochs = dict(self.service.reads.epochs)
                    producer.update(state='closed', closedAt=16)
                    discovered.update(state='closed', closedAt=17)
                    self.assertTrue(self.service.change(self._assignment_close_notice(discovered)))
                    self.assertNotIn(discovered['id'], self.service.model.inventory('assignments').rows)
                    self.assertEqual({discovered['id']: 'producer'}, old_flight.producer_effects)
                    self.service.reads.pump()
                    kind, value = self.service.events.get(timeout=5)
                    self.assertEqual('read', kind)
                    self.assertEqual(('reviewed-producer', 'producer', None), value[0])
                    self.service._read_done(value)
                    self.assertEqual([], value[2].rows)
                    self.assertEqual('open' if partial_producer else 'closed',
                        self.service.model.item_contributions.entries['producer'].state)
                    held = self.service.assignment_holds.assignments[discovered['id']]
                    self.assertEqual(partial_producer, held.producer_recovery)
                    # Success clears A's producer hold identity; failure must
                    # retain it. Either way this attempt still owns the link.
                    self.assertEqual('producer' if partial_producer else None, held.producer)
                    self.assertEqual({discovered['id']: 'producer'}, old_flight.producer_effects)
                    self.assertEqual(1, self.gateway.calls.count(('detail', 'assignments', None, 'producer'))
                                     - before.count(('detail', 'assignments', None, 'producer')))
                    release.set()
                    kind, value = self.service.events.get(timeout=5)
                    self.assertEqual(older, value[0])
                    self.service._read_done(value)
                    self.assertEqual({}, old_flight.producer_effects)
                    self.assertTrue(old_flight.closed)
                    self.drain()
                self.service._publish()
                label = 'r38-older-' + resource + '-' + str(partial_producer)
                if partial_producer:
                    self.assertTrue(self.service.assignment_holds.assignments[
                        discovered['id']].producer_recovery)
                    producer_job = ('reviewed-producer', 'producer', None)
                    self.assertIn(producer_job, self.service.read_retries)
                    self._assert_producer_hold_publication(baseline, label + '-still-pending')
                    # Only the existing same-scope retry can confirm P here.
                    _deadline, priority = self.service.read_retries[producer_job]
                    self.service.read_retries[producer_job] = (0, priority)
                    self.service._retry_incomplete_reads()
                    self.drain()
                    self.service._publish()
                self.assertEqual('closed', self.service.model.item_contributions.entries['producer'].state)
                self.assertEqual(16, self.service.model.item_contributions.entries['producer'].closed_at)
                # Discovery may create a zero-valued epoch key; no scope may
                # advance its epoch and restart the independently required read.
                for scope in set(epochs) | set(self.service.reads.epochs):
                    self.assertEqual(epochs.get(scope, 0), self.service.reads.epochs.get(scope, 0))
                self.assertEqual(0, self.service.reads.epoch_discards)
                self.assertEqual(before.count(('membership', 'work')),
                                 self.gateway.calls.count(('membership', 'work')))
                if resource == 'assignments':
                    self.assertTrue(self.service.assignment_holds.assignments[
                        discovered['id']].conflict)
                self._assert_producer_hold_publication(baseline, label)
                self.service._request_item_assignments('review-item')  # existing A covering source
                self.drain()
                self.service._publish()
                self.assertFalse(self.service.assignment_holds.assignments)
                self._assert_producer_recovery_fresh_and_replayed(
                    baseline, label + '-resolved')

    def test_reviewed_producer_service_coalesces_active_and_queued_lookup_and_discards_rows(self):
        review, producer = self._producer_recovery_fixture()
        baseline = deepcopy(self.service.current)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.gateway.detail
        def captured(resource, key, scope=None):
            result = original(resource, key, scope)
            if key == 'producer':
                result.rows[0]['reopenings'] = [{'reason': 'do not retain lookup history'}]
                result.rows[0].pop('files', None)  # actual assignment-get shape
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('fixture did not release producer lookup')
            return result
        self.gateway.detail = captured
        producer.update(state='closed', closedAt=16)
        review.update(state='closed', closedAt=17)
        notice = self._assignment_close_notice(review)
        self.service.change(notice)
        self.service.reads.pump()
        self.assertTrue(entered.wait(2))
        self.service.change(notice)
        self.assertFalse(self.service.reads.pending)
        release.set()
        event = self.service.events.get(timeout=2)
        result = event[1][2]
        self.service.change(notice)  # result queued, still coalesced
        self.assertFalse(self.service.reads.pending)
        self.service._handle(*event)
        self.service._publish()
        self.assertEqual([], result.rows)
        self.assertFalse(self.service.assignment_holds.assignments)
        self.assertFalse(self.service.reads.assignment_reads.flights)
        self.assertEqual([('detail', 'assignments', None, 'producer')], self.gateway.calls)
        self.assertNotIn('reopenings', repr(self.service.model.inventories))
        self.assertNotIn('do not retain lookup history', repr(self.service.model.item_contributions.entries))
        self._assert_producer_recovery_fresh_and_replayed(baseline, 'r36-coalesced-producer')

    def test_failed_or_partial_producer_lookup_holds_until_scheduled_covering_read(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                review, producer = self._producer_recovery_fixture()
                baseline = deepcopy(self.service.current)
                original = self.gateway.detail
                def incomplete(resource, key, scope=None):
                    result = original(resource, key, scope)
                    if key == 'producer':
                        return Read(result.rows if partial else [], False, result.at,
                                    result.source, 'partial' if partial else 'TimeoutError')
                    return result
                producer.update(state='closed', closedAt=16)
                review.update(state='closed', closedAt=17)
                with patch.object(self.gateway, 'detail', side_effect=incomplete):
                    self.service.change(self._assignment_close_notice(review))
                    self.drain()
                self.service._publish()
                self.assertEqual([('detail', 'assignments', None, 'producer')], self.gateway.calls)
                self.assertEqual('open', self.service.model.item_contributions.entries['producer'].state)
                self.assertTrue(self.service.assignment_holds.assignments['assignment'].producer_recovery)
                job = ('reviewed-producer', 'producer', None)
                self.assertIn(job, self.service.read_retries)
                self.assertFalse(self.service.reads.pending)
                self.assertFalse(self.service.reads.assignment_reads.flights)
                self.assertEqual(baseline['items'][0]['assignments'],
                                 self.service.current['items'][0]['assignments'])
                self.assertEqual('unconfirmed', self.service.current['items'][0]['evidence']['assignments']['freshness'])
                # No retry route escalation. A separately scheduled covering
                # read can resolve without waiting for or repeating the lookup.
                self.service._refresh('assignments')
                self.drain()
                self.service._publish()
                self.assertNotIn(job, self.service.read_retries)
                self.assertNotIn(job[:2], self.service.read_failures)
                self.assertFalse(self.service.assignment_holds.assignments)
                self.assertEqual('closed', self.service.model.item_contributions.entries['producer'].state)
                self.assertFalse([call for call in self.gateway.calls if call[0] == 'membership'])
                self._assert_producer_recovery_fresh_and_replayed(
                    baseline, 'r36-producer-incomplete-' + str(partial))

    def test_clean_covering_item_resolves_only_matching_producer_failure(self):
        # N2: a genuinely covering item read clears P's resolved retry, while
        # an unrelated failed source still prevents global live certification.
        for unrelated_failure in (False, True):
            with self.subTest(unrelated_failure=unrelated_failure):
                self.gateway = PublishedReads()
                review, producer = self._producer_recovery_fixture()
                baseline = deepcopy(self.service.current)
                producer.update(state='closed', closedAt=16)
                review.update(state='closed', closedAt=17)
                original = self.gateway.detail
                def partial(resource, key, scope=None):
                    result = original(resource, key, scope)
                    if key == 'producer':
                        return Read(result.rows, False, result.at, result.source, 'partial')
                    return result
                with patch.object(self.gateway, 'detail', side_effect=partial):
                    self.assertTrue(self.service.change(self._assignment_close_notice(review)))
                    self.drain()
                producer_job = ('reviewed-producer', 'producer', None)
                self.assertIn(producer_job[:2], self.service.read_failures)
                self.assertIn(producer_job, self.service.read_retries)
                self.assertTrue(self.service.assignment_holds.assignments['assignment'].producer_recovery)
                other_scope = ('turns', 'agent s_author')
                if unrelated_failure:
                    self.gateway.partial.add(other_scope)
                    self.service._refresh(*other_scope)
                    self.drain()
                    self.assertIn(other_scope, self.service.read_failures)
                # Existing independently scheduled complete covering source.
                self.service._request_item_assignments('work')
                self.drain()
                self.service._publish()
                self.assertNotIn(producer_job[:2], self.service.read_failures)
                self.assertNotIn(producer_job, self.service.read_retries)
                self.assertFalse(self.service.assignment_holds.assignments)
                self.assertEqual('closed', self.service.model.item_contributions.entries['producer'].state)
                self.assertEqual(1, self.gateway.calls.count(('membership', 'work')))
                if unrelated_failure:
                    self.assertIn(other_scope, self.service.read_failures)
                    other_job = other_scope + (None,)
                    self.assertIn(other_job, self.service.read_retries)
                    self.assertNotEqual('live', self.service.current['freshness']['state'])
                    self.gateway.partial.remove(other_scope)
                    _deadline, priority = self.service.read_retries[other_job]
                    self.service.read_retries[other_job] = (0, priority)
                    self.service._retry_incomplete_reads()
                    self.drain()
                    self.service._publish()
                self.assertEqual('live', self.service.current['freshness']['state'])
                self.assertIsNone(self.service.current['freshness']['reason'])
                self._assert_producer_recovery_fresh_and_replayed(
                    baseline, 'r38-n2-covering-' + str(unrelated_failure))

    def test_unidentified_producer_keeps_never_published_item_absent_until_covering_read(self):
        review, _producer = self._producer_recovery_fixture(identified=False, published=False)
        self.assertFalse(self.service.current.get('items'))
        review.update(state='closed', closedAt=17)
        self.service.change(self._assignment_close_notice(review))
        self.service._publish()
        self.assertEqual([], self.service.current['items'])
        self.assertEqual([], self.gateway.calls)
        self.assertFalse(self.service.reads.pending)
        self.assertIsNone(self.service.assignment_holds.items['work'])
        baseline = deepcopy(self.service.current)
        self.service._refresh('assignments')
        self.drain()
        self.service._publish()
        self.assertEqual(['work'], [item['id'] for item in self.service.current['items']])
        self.assertFalse(self.service.assignment_holds.assignments)
        self._assert_producer_recovery_fresh_and_replayed(baseline, 'r36-absent-producer')

    def test_recovered_producer_survives_an_older_global_or_item_assignment_snapshot(self):
        for item_source in (False, True):
            with self.subTest(item_source=item_source):
                review, producer = self._producer_recovery_fixture(workers=2)
                baseline = deepcopy(self.service.current)
                if item_source:
                    self.service._request_item_assignments('work')
                else:
                    self.service._refresh('assignments')
                self.service.reads.pump()
                older = self.service.events.get(timeout=5)
                self.assertEqual('read', older[0])
                self.assertTrue(older[1][2].complete)
                original_calls = list(self.gateway.calls)
                # The source snapshot is already complete but queued. Its
                # old A/P rows cannot undo the later targeted P recovery.
                producer.update(state='closed', closedAt=16)
                review.update(state='closed', closedAt=17)
                self.service.change(self._assignment_close_notice(review))
                self.service.reads.pump()
                recovered = self.service.events.get(timeout=5)
                self.assertEqual(('reviewed-producer', 'producer', None), recovered[1][0])
                self.service._handle(*recovered)
                self.assertEqual('closed', self.service.model.item_contributions.entries['producer'].state)
                self.service._handle(*older)
                self.drain()
                self.service._publish()
                self.assertEqual(original_calls + [('detail', 'assignments', None, 'producer')],
                                 self.gateway.calls)
                self.assertEqual(0, self.service.reads.epoch_discards)
                self.assertEqual('closed', self.service.model.item_contributions.entries['producer'].state)
                self.assertEqual(16, self.service.model.item_contributions.entries['producer'].closed_at)
                self.assertTrue(self.service.assignment_holds.assignments['assignment'].conflict)
                self.assertEqual(baseline['items'][0]['assignments'],
                                 self.service.current['items'][0]['assignments'])
                self.service._refresh('assignments')
                self.drain()
                self.service._publish()
                self.assertFalse(self.service.assignment_holds.assignments)
                self._assert_producer_recovery_fresh_and_replayed(
                    baseline, 'r36-producer-older-source-' + str(item_source))

    def test_decision_and_artifact_payloads_require_versioned_complete_projections(self):
        from atc_payloads import PAYLOAD_POLICIES, DECISION_FIELDS, ARTIFACT_FIELDS
        self.service.policies = PAYLOAD_POLICIES
        decision = dict.fromkeys(DECISION_FIELDS) | {
            "id": "decision", "kind": "statute", "status": "open", "rowVersion": 1,
            "question": "Ship?", "options": [], "context": {}, "deadlineAt": 100,
            "lineageRung": 0, "effortGeneration": 0,
        }
        artifact = dict.fromkeys(ARTIFACT_FIELDS) | {
            "artifactId": "artifact", "kind": "report", "state": "in-workspace",
            "title": "Report", "rowVersion": 1,
        }
        self.service.change({"resource": "decision requests", "op": "upsert",
                             "class": "decision_request.opened", "refs": {"decisionRequestId": "decision"},
                             "payload": decision})
        self.service.change({"resource": "artifacts", "op": "upsert", "class": "artifact.recorded",
                             "refs": {"artifactId": "artifact"}, "payload": artifact})
        self.assertEqual({}, self.service.reads.pending)
        self.assertEqual(decision, self.service.model.inventory("decision-requests").rows["decision"])
        self.assertEqual(artifact, self.service.model.inventory("artifacts").rows["artifact"])
        # The legacy operator projection does not have the closed R7 guarantee.
        self.service.change({"resource": "decision requests", "op": "upsert",
                             "class": "decision_request.ruled", "refs": {"decisionRequestId": "decision"},
                             "payload": dict(decision, kind="operator", rowVersion=2)})
        self.assertIn(("decision-requests", None, "decision"), self.service.reads.pending)
        # A CLI artifact baseline has no version floor. Reading this named row
        # is an ordering gap, not a whole-inventory notice refetch.
        self.service.model.accept("artifacts", None, Read([
            {k: v for k, v in artifact.items() if k != "rowVersion"}], True, 10, "cli:artifacts"))
        self.service.change({"resource": "artifacts", "op": "upsert", "class": "artifact.archived",
                             "refs": {"artifactId": "artifact"}, "payload": dict(artifact, rowVersion=2)})
        self.assertIn(("artifacts", None, "artifact"), self.service.reads.pending)

    def test_payloads_apply_across_gateway_versions_with_warning_and_targeted_fallback(self):
        from atc_payloads import BUILD_ID, PAYLOAD_POLICIES, policies_for
        audited = dict(zip(("version", "build", "sha"), BUILD_ID))
        versions = [audited, audited | {"version": "0.1.11"}, audited | {"build": 2909},
                    audited | {"sha": "different"}, None]
        for index, version in enumerate(versions, start=2):
            self.gateway.rows[("work-items", None)] = [work(version=1)]
            self.assertEqual(PAYLOAD_POLICIES, policies_for(version))
            self.service._handle("ready", version)
            self.drain()
            self.assertEqual(PAYLOAD_POLICIES, self.service.policies)
            before = self.gateway.read_count
            title = f"Version {index}"
            payload = work(title, index)
            self.service.change({"resource": "work-items", "op": "upsert",
                                 "class": "work_item.updated", "refs": {"workItemId": "work"},
                                 "payload": payload})
            self.assertEqual(title, self.item()["title"])
            self.assertEqual(before, self.gateway.read_count)
            self.assertFalse(self.service.reads.pending)
            self.service._publish()
            published = self.service.snapshot()
            self.assertEqual(version, published["gatewayVersion"])
            if version == audited:
                self.assertIsNone(published["payloadWarning"])
            else:
                self.assertIn("unverified", published["payloadWarning"])

        self.service.change({"resource": "work-items", "op": "upsert", "class": "unknown.class",
                             "refs": {"workItemId": "work"}, "payload": work("Invalid class", 99)})
        self.assertIn(("work-items", None, "work"), self.service.reads.pending)
        self.assertEqual("Version 6", self.item()["title"])

    def test_summary_payload_supplies_item_facts_and_skips_git_resolution(self):
        self.service.version = {"version": "0.1.15", "build": 3866, "sha": "b09a64a9"}
        self.service.policies = policies_for(self.service.version)
        ref = {"repo": "example/repo", "commit": "a" * 40}
        landing = {"repository": "example/repo", "branch": "main", "commit": "a" * 40,
                   "headCommit": "b" * 40, "pullRequest": 14, "observedAt": 90}
        payload = summarized_work("Summary", 2, attest_counts={"progress": 1},
                                  refs=[ref], landings=[landing])
        applied = self.service.change({
            "resource": "work-items", "op": "upsert", "class": "work_item.updated",
            "refs": {"workItemId": "work"}, "payload": payload,
        })
        self.assertTrue(applied)
        result = self.item()
        self.assertEqual(["s_author"], result["holders"])
        self.assertTrue(result["started"])
        self.assertEqual({"total": 1, "byKind": {"progress": 1}, "byVerdict": {}},
                         result["evidenceCounts"])
        self.assertEqual([ref], result["commitRefs"])
        self.assertEqual([landing], result["landings"])
        self.assertTrue(result["merged"])
        self.assertEqual(["work-items"], result["evidence"]["merged"]["source"])

        resolver = type("Resolver", (), {"resolve": lambda _self, _refs: self.fail("Git lookup used")})()
        self.service.resolver = resolver
        self.service.current = {"items": [result]}
        self.service._resolve_merges()
        self.assertTrue(self.service.merge_jobs.empty())
        self.assertIsNone(self.service._next_merge_retry_at())

    def test_empty_summary_landings_uses_shipping_line_git_result(self):
        self.service.version = {"version": "0.1.15", "build": 3866, "sha": "b09a64a9"}
        self.service.policies = policies_for(self.service.version)
        remote_name = "git@github.com:clickety-clacks/tightbeam.git"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote = root / "source", root / "canonical.git"
            subprocess.run(["git", "init", "-b", "main", str(source)], check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email",
                            "test@example.com"], check=True)
            (source / "content.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(source), "add", "content.txt"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", "base"], check=True,
                           capture_output=True, text=True)
            base = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "-C", str(source), "switch", "-c", "0.1.14", base], check=True,
                           capture_output=True, text=True)
            (source / "content.txt").write_text("release line\n")
            subprocess.run(["git", "-C", str(source), "commit", "-am", "release line"], check=True,
                           capture_output=True, text=True)
            release_commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                                            check=True, capture_output=True,
                                            text=True).stdout.strip()
            subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)],
                           check=True)
            subprocess.run(["git", "-C", str(source), "push", "origin", "main", "0.1.14"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD",
                            "refs/heads/main"], check=True, capture_output=True, text=True)

            ref = {"repo": "retired:/tightbeam", "remote": remote_name,
                   "ref": "refs/heads/0.1.14", "commit": release_commit}
            payload = summarized_work("Historical item", 2, refs=[ref], landings=[])
            self.assertTrue(self.service.change({
                "resource": "work-items", "op": "upsert", "class": "work_item.updated",
                "refs": {"workItemId": "work"}, "payload": payload,
            }))

            result = self.item()
            self.assertEqual([], result["landings"])
            self.assertIsNone(result["merged"])

            # Empty summaries enqueue effective refs; resolve the release-only
            # commit through real Git and consume the result as the worker does.
            self.service.resolver = LocalCanonicalGitResolver(root / "cache", {remote_name: remote})
            self.service.current = {"items": [result]}
            self.service._resolve_merges()
            job = self.service.merge_jobs.get_nowait()
            self.assertEqual(("work", [ref]), job)
            value = self.service.resolver.resolve(job[1])
            self.assertIs(value, True)
            self.assertTrue(self.service._handle("merged", (job[0], job[1], value, 30)))

            resolved = self.item()
            self.assertTrue(resolved["merged"])
            self.assertEqual([], resolved["landings"])
            self.assertEqual("git", resolved["evidence"]["merged"]["source"])

    def test_git_merge_result_survives_service_restart_as_stale_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "git-cache"
            ref = {"repo": "retired:/tightbeam", "remote":
                   "git@github.com:clickety-clacks/tightbeam.git",
                   "ref": "refs/heads/0.1.14", "commit": "a" * 40}
            first = LiveService(PublishedReads(), GitResolver(cache))
            first.merge_pending["work"] = [ref]
            self.assertTrue(first._handle("merged", ("work", [ref], True, 100)))

            restarted = LiveService(PublishedReads(), GitResolver(cache))
            try:
                result = restarted.model.merges["work"]
                self.assertTrue(result["value"])
                self.assertEqual([ref], result["refs"])
                self.assertEqual(100, result["observedAt"])
                self.assertEqual("stale", result["freshness"])
                self.assertTrue((cache / "merge-results.jsonl").is_file())
            finally:
                first.stop()
                restarted.stop()

    def test_unavailable_merge_lookup_does_not_block_an_independent_job(self):
        slow_started = threading.Event()
        release_slow = threading.Event()
        fast_finished = threading.Event()

        class BlockingResolver:
            def resolve(self, refs):
                if refs[0]["commit"] == "slow":
                    slow_started.set()
                    release_slow.wait(2)
                else:
                    fast_finished.set()
                return True

        service = LiveService(PublishedReads())
        service.resolver = BlockingResolver()
        workers = service._merge_resolvers()
        threads = [threading.Thread(target=service._git_worker, args=(resolver,), daemon=True)
                   for resolver in workers]
        for thread in threads:
            thread.start()
        try:
            service.merge_pending["slow-item"] = [{"commit": "slow"}]
            service.merge_jobs.put(("slow-item", [{"commit": "slow"}]))
            self.assertTrue(slow_started.wait(1))
            service.merge_pending["fast-item"] = [{"commit": "fast"}]
            service.merge_jobs.put(("fast-item", [{"commit": "fast"}]))
            self.assertTrue(fast_finished.wait(1),
                            "a blocked lookup must not hold up another queued item")
        finally:
            release_slow.set()
            service.stop_event.set()
            for _ in threads:
                service.merge_jobs.put(None)
            for thread in threads:
                thread.join(timeout=2)
            service.stop()

    def test_summary_bootstrap_skips_redundant_attest_reads(self):
        gateway = PublishedReads()
        gateway.rows[("work-items", None)] = [summarized_work()]
        service = LiveService(gateway)
        service.version = {"version": "0.1.15", "build": 3866, "sha": "b09a64a9"}
        service.connected = True
        self.addCleanup(service.stop)
        service.rebuild()
        self.drain(service)

        self.assertNotIn(("read", "attests", "assignment"), gateway.calls)
        self.assertIsNone(service.model.inventory("attests", "assignment").observed_at)
        self.assertTrue(service.metrics["bootstrapComplete"])
        self.assertEqual(1, service.model.snapshot(20)["items"][0]["evidenceCounts"]["total"])

    def test_summary_reads_attest_detail_when_review_order_controls_stage(self):
        gateway = PublishedReads()
        gateway.rows[("work-items", None)] = [summarized_work(
            attest_counts={"completion": 1, "verdict": 2},
            verdict_counts={"reviewed-clean": 1, "tests-passed": 1},
            holders=[])]
        gateway.rows[("assignments", None)] = [{
            "id": "assignment", "holderKey": "agent s_author", "workItemId": "work",
            "state": "closed", "effectKind": "code",
        }]
        gateway.rows[("attests", "assignment")] = [
            {"id": "done", "assignmentId": "assignment", "kind": "completion",
             "ts": 10, "bySession": "agent s_author", "commitRefs": []},
            {"id": "tested", "assignmentId": "assignment", "kind": "verdict",
             "verdictKind": "tests-passed", "ts": 100, "bySession": "agent s_author",
             "commitRefs": []},
            {"id": "reviewed", "assignmentId": "assignment", "kind": "verdict",
             "verdictKind": "reviewed-clean", "ts": 200, "bySession": "agent s_author",
             "commitRefs": []},
        ]
        service = LiveService(gateway)
        service.version = {"version": "0.1.15", "build": 3866, "sha": "b09a64a9"}
        service.connected = True
        self.addCleanup(service.stop)
        service.rebuild()
        self.drain(service)

        self.assertIn(("read", "attests", "assignment"), gateway.calls)
        self.assertEqual(5, service.model.snapshot(20)["items"][0]["stage"])

    def test_summary_count_gap_requests_only_the_matching_attest_scopes(self):
        self.service.version = {"version": "0.1.15", "build": 3866, "sha": "b09a64a9"}
        self.service.policies = policies_for(self.service.version)
        payload = summarized_work("Count changed", 2, attest_counts={"progress": 2})
        self.assertTrue(self.service.change({
            "resource": "work-items", "op": "upsert", "class": "work_item.updated",
            "refs": {"workItemId": "work"}, "payload": payload,
        }))
        self.assertIn(("attests", "assignment", None), self.service.reads.pending)

    def test_0_1_15_stops_terminal_attest_rotation_but_legacy_keeps_it(self):
        self.service.backstop.history = {("attests", "assignment"), ("messages", "session")}
        self.service.version = {"version": "0.1.15"}
        item = summarized_work("No review-order dependency", 2)
        self.service.model.accept("work-items", None, Read([item], True, 30, "work-items"))
        self.assertEqual({("messages", "session")}, self.service._repeated_history_scopes())

        item = summarized_work("Review pending", 3, attest_counts={"completion": 1, "verdict": 2},
                               verdict_counts={"reviewed-clean": 1, "tests-passed": 1}, holders=[])
        self.service.model.accept("work-items", None, Read([item], True, 30, "work-items"))
        self.assertEqual({("attests", "assignment"), ("messages", "session")},
                         self.service._repeated_history_scopes())

        self.service.version = {"version": "0.1.14"}
        self.assertEqual({("attests", "assignment"), ("messages", "session")},
                         self.service._repeated_history_scopes())

    def test_active_budget_expiry_publishes_stale_without_clock_only_deltas(self):
        from atc_reconciliation import ACTIVE_BUDGET_MS
        self.service._publish()
        before = self.service.snapshot()
        meta = before["freshness"]["reconciliation"]
        self.assertEqual(4, meta["active"]["scopeCount"])
        self.assertEqual(10, meta["active"]["oldestConfirmedAt"])
        self.assertEqual(0, meta["active"]["overBudgetScopes"])
        with patch("atc_service.now_ms", return_value=1000):
            self.assertFalse(self.service._publish())
        with patch("atc_service.now_ms", return_value=10 + ACTIVE_BUDGET_MS):
            self.assertTrue(self.service._publish())
        stale = self.service.snapshot()
        self.assertEqual("stale", stale["freshness"]["state"])
        self.assertEqual("stale", stale["items"][0]["evidence"]["stage"]["freshness"])
        self.assertEqual(4, stale["freshness"]["reconciliation"]["active"]["overBudgetScopes"])
        self.assertEqual(before["agents"][0]["idleFor"], stale["agents"][0]["idleFor"])
        for scope in self.service.backstop.active:
            inventory = self.service.model.inventory(*scope)
            self.service.model.accept(*scope, Read(list(inventory.rows.values()), True,
                                                  20 + ACTIVE_BUDGET_MS, "read"))
        with patch("atc_service.now_ms", return_value=20 + ACTIVE_BUDGET_MS):
            self.service._publish()
        self.assertEqual("live", self.service.snapshot()["freshness"]["state"])
