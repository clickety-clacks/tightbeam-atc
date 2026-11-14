"""Ownership resolution preserves author maxima and forgets resolved owners."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import LiveService
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
        self.addCleanup(self.service.stop)
        self.addCleanup(self.gateway.release.set)

    def notice(self, assignment, author, timestamp, **fields):
        return self.service.change(dict(resource="attests", op="upsert",
            **{"class": "attest.created"}, refs={"assignmentId": assignment},
            payload=dict(id=author + str(timestamp), assignmentId=assignment,
                         kind="progress", bySession=author, ts=timestamp) | fields))

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
        self.gateway.fail = True
        self.notice("released", "author", 950000)
        service.reads.pump()
        with patch("atc_service.time.monotonic", return_value=100):
            self.finish()
        self.assertEqual({}, service.model.session_activity)
        self.assertIsNotNone(service.attest_activity_pending["released"]["failedAt"])
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
