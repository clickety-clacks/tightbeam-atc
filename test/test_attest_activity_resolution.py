"""Ownership resolution preserves author maxima and forgets resolved owners."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import BASE_SCOPES, LiveService
from atc_upstream import Read
from atc_reconciliation import RETRY_MS
from test_service import PublishedReads, summarized_work


class OwnerReads(PublishedReads):
    def __init__(self):
        super().__init__()
        self.owner = None
        self.fail = False
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def detail(self, resource, key, scope=None):
        self.calls.append(("detail", resource, scope, key))
        self.entered.set()
        if self.block and not self.release.wait(5):
            raise TimeoutError("test ownership read was not released")
        return Read([dict(id=key, workItemId=self.owner)] if not self.fail else [],
                    not self.fail, 30, "cli:assignment-get " + key,
                    "TimeoutError" if self.fail else None)


class AttestActivityResolutionTest(unittest.TestCase):
    def setUp(self):
        self.gateway = OwnerReads()
        self.service = LiveService(self.gateway, workers=2)
        self.service.connected = True
        self.addCleanup(self.service.stop)
        self.addCleanup(self.gateway.release.set)

    def notice(self, assignment, author, timestamp, **fields):
        return self.service.change(dict(resource="attests", op="upsert",
            **{"class": "attest.created"}, refs={"assignmentId": assignment},
            payload=dict(id=author + str(timestamp), assignmentId=assignment,
                         kind="progress", bySession=author, ts=timestamp) | fields))

    def install_agents(self):
        for resource, scope in BASE_SCOPES:
            rows = ([dict(sessionKey=key, state="active") for key in ("author", "unaffected")]
                    if resource == "sessions" else [])
            self.service.model.accept(resource, scope, Read(rows, True, 10, resource))
        for session in ("author", "unaffected"):
            for resource in ("turns", "wakes", "messages"):
                self.service.model.accept(resource, session, Read([], True, 10, resource))
        self.service._publish()

    def finish(self):
        kind, result = self.service.events.get(timeout=5)
        self.assertEqual("read", kind)
        self.service._read_done(result)

    def test_inflight_notices_share_read_then_later_notice_resolves_again(self):
        service = self.service
        self.gateway.block = True
        self.notice("released", "first", 950000)
        service.reads.pump()
        self.assertTrue(self.gateway.entered.wait(2))
        self.notice("released", "second", 900000)
        self.notice("released", "first", 800000)
        self.assertEqual({}, service.model.session_activity)
        self.assertEqual({}, service.reads.pending)
        self.assertEqual(1, service.reads.attest_owner_attempts)
        self.gateway.release.set()
        self.finish()
        self.assertEqual({"first": 950000, "second": 900000}, service.model.session_activity)
        self.assertEqual({}, service.attest_activity_pending)
        self.assertNotIn("released", service.model.inventory("assignments").rows)
        self.notice("released", "first", 960000)
        service.reads.pump()
        self.finish()
        self.assertEqual(2, service.reads.attest_owner_attempts)
        self.assertEqual(960000, service.model.session_activity["first"])

    def test_failure_keeps_unapplied_maxima_and_retries_same_read(self):
        service = self.service
        self.install_agents()
        self.gateway.fail = True
        self.notice("released", "author", 950000)
        service.reads.pump()
        with patch("atc_service.time.monotonic", return_value=100):
            self.finish()
        self.assertEqual({}, service.model.session_activity)
        self.assertIsNotNone(service.attest_activity_pending["released"]["failedAt"])
        service._publish()
        agents = {row["sessionKey"]: row for row in service.current["agents"]}
        failed = agents["author"]["evidence"]["idleFor"]
        self.assertEqual("unconfirmed", failed["freshness"])
        self.assertIsNotNone(failed["unconfirmedSince"])
        self.assertEqual("live", agents["unaffected"]["evidence"]["idleFor"]["freshness"])
        self.assertIsNone(agents["author"]["idleSince"])
        self.assertEqual((100 + RETRY_MS / 1000, 2),
                         service.read_retries["attest-owner", "released", None])
        self.notice("released", "author", 960000)
        self.assertEqual({}, service.reads.pending)
        self.gateway.fail = False
        with patch("atc_service.time.monotonic", return_value=100 + RETRY_MS / 1000):
            service._retry_incomplete_reads()
        service.reads.pump()
        self.finish()
        self.assertEqual({"author": 960000}, service.model.session_activity)
        self.assertEqual({}, service.attest_activity_pending)
        self.assertEqual({}, service.read_retries)
        self.assertEqual(2, service.reads.attest_owner_attempts)
        service._publish()
        author = next(row for row in service.current["agents"] if row["sessionKey"] == "author")
        self.assertEqual(960000, author["idleSince"])
        self.assertEqual("live", author["evidence"]["idleFor"]["freshness"])
        self.assertIsNone(author["evidence"]["idleFor"]["unconfirmedSince"])

    def test_reference_only_assignment_id_resolves_through_existing_detail_route(self):
        self.service.change(dict(resource="attests", op="upsert",
            refs={"assignmentId": "released"},
            payload=dict(kind="progress", bySession="author", ts=950000)))
        self.service.reads.pump()
        self.finish()
        self.assertEqual({"author": 950000}, self.service.model.session_activity)
        self.assertEqual([("detail", "assignments", None, "released")], self.gateway.calls)
        self.assertNotIn(("attest-owner", "released"), self.service.model.inventories)

    def test_feed_loss_does_not_create_an_owner_inventory_or_apply_old_result(self):
        service = self.service
        self.gateway.block = True
        service.connected = True
        self.notice("released", "author", 950000)
        service.reads.pump()
        self.assertTrue(self.gateway.entered.wait(2))
        # Isolate feed-loss handling from unrelated bootstrap scheduling.
        with patch.object(service, "rebuild"):
            service._handle("lost", "synthetic disconnect")
        self.assertNotIn(("attest-owner", "released"), service.model.inventories)
        self.gateway.release.set()
        self.finish()
        self.assertEqual({}, service.model.session_activity)
        self.assertIn(("attest-owner", "released", None), service.reads.pending)
        service.reads.pump()
        self.finish()
        self.assertEqual({"author": 950000}, service.model.session_activity)
        self.assertEqual(2, service.reads.attest_owner_attempts)
        self.assertEqual({}, service.attest_activity_pending)
        self.assertEqual("stale", service.model.session_activity_evidence["author"]["freshness"])
        self.assertNotIn(("attest-owner", "released"), service.model.inventories)

    def test_explicit_null_and_held_owner_apply_without_resolution(self):
        self.notice("null-assignment", "null-author", 950000, workItemId=None)
        self.service.model.inventory("assignments").rows["held"] = {
            "id": "held", "workItemId": None, "state": "open", "holderKey": "other"}
        self.notice("held", "held-author", 900000)
        self.assertEqual({"null-author": 950000, "held-author": 900000},
                         self.service.model.session_activity)
        self.assertEqual(0, self.service.reads.attest_owner_attempts)
        self.assertEqual({}, self.service.attest_activity_pending)
        self.assertNotIn("other", self.service.model.session_activity)

    def test_unknown_and_summary_skipped_items_are_not_null(self):
        for owner in ("missing", "skipped"):
            with self.subTest(owner=owner):
                self.gateway.owner = owner
                self.service.model.accept("work-items", None,
                    Read([summarized_work() | dict(id="skipped")], True, 10, "work-items"))
                self.notice("released", "author", 950000)
                self.service.reads.pump()
                self.finish()
                self.assertEqual({}, self.service.model.session_activity)
                self.assertEqual({}, self.service.attest_activity_pending)


if __name__ == "__main__":
    unittest.main()
