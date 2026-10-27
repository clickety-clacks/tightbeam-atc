"""Deterministic service recovery; real gateway proof is recorded separately."""

from copy import deepcopy
from pathlib import Path
import sys
import json
import time
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import LiveService, WORK_ITEM_FIELDS
from atc_upstream import Read


def work(title="First", version=1):
    return dict.fromkeys(WORK_ITEM_FIELDS) | {
        "id": "work", "title": title, "state": "open", "createdAt": 1,
        "rowVersion": version,
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
        identity = "sessionKey" if resource == "sessions" else "id"
        rows = [r for r in self.rows.get((resource, scope), []) if r.get(identity) == key]
        return Read(deepcopy(rows), bool(rows), 11, "published-detail")


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

    def test_disconnect_restarts_pending_bootstrap_and_reconnect_can_complete_it(self):
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

        reads_before_disconnect = gateway.read_count
        service._handle("lost", "feed disconnected: ConnectionClosedError (4008)")
        self.assertEqual("recovery", service.cycle["mode"])
        self.assertEqual("feed disconnected: ConnectionClosedError (4008)", service.reason)
        gateway.partial.clear()
        self.drain(service)

        self.assertIsNone(service.cycle)
        self.assertFalse(service.metrics["bootstrapComplete"])
        self.assertEqual("stale", service.state)
        self.assertGreater(gateway.read_count, reads_before_disconnect)

        service._handle("ready", {"version": "0.1.11", "build": 1, "sha": "test"})
        self.drain(service)
        self.assertTrue(service.metrics["bootstrapComplete"])
        self.assertIsNotNone(service.last_complete)
        self.assertEqual("live", service.state)

    def test_repeated_lost_after_healthy_offline_settle_waits_for_interval(self):
        rebuilds = self.service.metrics["rebuilds"]
        self.service._handle("lost", "feed disconnected: ConnectionClosedError (4008)")
        self.drain()
        self.assertIsNone(self.service.cycle)
        self.assertTrue(self.service.metrics["bootstrapComplete"])
        rebuilds += 1
        reads = self.gateway.read_count
        epochs = dict(self.service.reads.epochs)

        for reason in ("feed disconnected: retry 1", "feed disconnected: retry 2"):
            self.service._handle("lost", reason)
            self.assertEqual(reason, self.service.reason)
            self.assertIsNone(self.service.cycle)
            self.assertEqual(rebuilds, self.service.metrics["rebuilds"])
            self.assertEqual(epochs, dict(self.service.reads.epochs))
        self.assertEqual(reads, self.gateway.read_count)

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

        # Its scheduled retry still runs while disconnected; the model remains
        # stale until the feed reconnects and can establish live freshness.
        gateway.partial.clear()
        service.read_retries[retry] = (time.monotonic() - 1, retry_state[1])
        service._retry_incomplete_reads()
        self.assertIn(retry, service.reads.pending)
        self.drain(service)
        self.assertIsNone(service.cycle)
        self.assertEqual(1, service.retry_counters["completed"])
        self.assertTrue(service.last_retry_outcome["complete"])
        self.assertEqual("stale", service.state)

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
        self.service.rebuild(recovery=False)
        self.drain()
        self.assertIn("progress", self.service.model.inventory("attests", "assignment").rows)
        self.assertIsNone(self.item()["stage"])
        self.assertEqual("rebuilding", self.service.state)
        self.assertTrue(self.service.model.inventory("attests", "assignment").stale)
        self.assertEqual(before, self.service.last_sweep)
        self.gateway.partial.clear()
        self.service.rebuild(recovery=False)
        self.drain()
        self.assertEqual({}, self.service.model.inventory("attests", "assignment").rows)
        self.assertEqual(0, self.item()["stage"])

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
        _, frame = self.service.next_event(revision, timeout=0)
        delta = json.loads(frame.split(b"data: ", 1)[1])
        self.assertEqual(["work"], delta["changes"]["items"]["remove"])
        # Falling behind more than the retained delta yields a complete resync.
        _, frame = self.service.next_event(baseline["revision"], timeout=0)
        self.assertTrue(frame.startswith(b"event: model\n"))
        self.assertEqual([], json.loads(frame.split(b"data: ", 1)[1])["items"])

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

    def test_completion_payload_reads_only_assignment_side_effect(self):
        from atc_payloads import PAYLOAD_POLICIES
        from atc_upstream import ATTEST_FIELDS
        self.service.policies = PAYLOAD_POLICIES
        row = dict.fromkeys(ATTEST_FIELDS) | {
            "id": "completed", "assignmentId": "assignment", "kind": "completion",
            "ts": 15, "bySession": "agent s_author", "commitRefs": [],
        }
        self.gateway.rows[("assignments", None)][0]["state"] = "closed"
        self.gateway.calls.clear()
        self.service.change({"resource": "attests", "class": "attest.filed", "op": "upsert",
                             "refs": {"attestId": "completed"}, "payload": row})
        self.drain()
        self.assertEqual([("detail", "assignments", None, "assignment")], self.gateway.calls)
        self.assertEqual("closed", self.service.model.inventory("assignments").rows["assignment"]["state"])
        self.assertIn("completed", self.service.model.inventory("attests", "assignment").rows)

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
        from atc_payloads import PAYLOAD_POLICIES
        from atc_upstream import ATTEST_FIELDS
        self.service.policies = PAYLOAD_POLICIES
        self.service.model.inventory("assignments").rows["assignment"]["reviewsAssignmentId"] = "producer"
        row = dict.fromkeys(ATTEST_FIELDS) | {
            "id": "review", "assignmentId": "assignment", "kind": "verdict",
            "verdictKind": "reviewed-clean", "ts": 15, "commitRefs": [],
        }
        self.service.change({"resource": "attests", "op": "upsert", "class": "attest.filed",
                             "refs": {"attestId": "review"}, "payload": row})
        self.assertEqual({("assignments", None, "producer")}, set(self.service.reads.pending))
        self.assertIn("review", self.service.model.inventory("attests", "assignment").rows)

    def test_attest_side_effect_invalidates_an_older_assignment_read(self):
        from atc_payloads import PAYLOAD_POLICIES
        from atc_upstream import ATTEST_FIELDS
        self.service.policies = PAYLOAD_POLICIES
        entered, release = threading.Event(), threading.Event()
        original = self.gateway.detail

        def captured(resource, key, scope=None):
            result = original(resource, key, scope)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release read")
            return result

        self.gateway.detail = captured
        self.service._refresh("assignments", key="assignment")
        self.service.reads.pump()
        self.assertTrue(entered.wait(2))
        row = dict.fromkeys(ATTEST_FIELDS) | {
            "id": "completed", "assignmentId": "assignment", "kind": "completion",
            "ts": 15, "commitRefs": [],
        }
        self.gateway.rows[("assignments", None)][0]["state"] = "closed"
        self.service.change({"resource": "attests", "op": "upsert", "class": "attest.filed",
                             "refs": {"attestId": "completed"}, "payload": row})
        release.set()
        kind, value = self.service.events.get(timeout=2)
        self.assertEqual("read", kind)
        self.assertFalse(self.service._read_done(value))
        self.drain()
        self.assertEqual("closed", self.service.model.inventory("assignments").rows["assignment"]["state"])

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
            self.assertEqual(PAYLOAD_POLICIES, policies_for(version))
            self.service._handle("ready", version)
            self.drain()
            self.assertEqual(PAYLOAD_POLICIES, self.service.policies)
            before = self.gateway.read_count
            title = f"Version {index}"
            self.service.change({"resource": "work-items", "op": "upsert",
                                 "class": "work_item.updated", "refs": {"workItemId": "work"},
                                 "payload": work(title, index)})
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
