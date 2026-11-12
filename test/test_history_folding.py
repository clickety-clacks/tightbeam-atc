"""History pages preserve board facts without retaining display payloads."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_summary import summarize, recent_marks


class HistoryFoldingTest(unittest.TestCase):
    def test_attest_pages_preserve_counts_authors_refs_and_latest(self):
        rows = [
            {"kind": "progress", "ts": 1, "bySession": "author"},
            {"kind": "verdict", "verdictKind": "reviewed-clean", "ts": 2,
             "bySession": "author", "commitRefs": ["abc"]},
            {"kind": "completion", "ts": 3},
        ]
        folded = summarize("attests", rows[:1])
        self.assertIs(folded, summarize("attests", rows[1:], into=folded))
        self.assertEqual(summarize("attests", rows), folded)
        self.assertEqual(3, folded["count"])
        self.assertEqual({"author": 2}, folded["authors"])

    def test_message_projection_expires_but_preserves_activity(self):
        rows = [{"id": "old", "at": 1, "content": "old payload"},
                {"id": "visible", "at": 300_002, "sessionKey": "target",
                 "sender": "user:example", "content": "private payload"}]
        folded = summarize("messages", rows, now=600_001)
        self.assertEqual(["visible"], [r["id"] for r in folded["marks"]])
        self.assertNotIn("content", folded["marks"][0])
        self.assertEqual(300_002, folded["activity"])
        self.assertEqual([], list(recent_marks(folded, "at", 600_002)))
        summarize("messages", [], into=folded, now=600_002)
        self.assertEqual([], folded["marks"])
        self.assertEqual(2, folded["count"])

    def test_live_turn_and_wake_keep_only_fields_used_by_board(self):
        turn = {"seq": 1, "sessionKey": "target", "assignmentId": "task",
                "wakeId": "wake", "startedAt": 10, "endedAt": None,
                "prompt": "large payload"}
        live = summarize("turns", [turn])["live"][0]
        self.assertNotIn("prompt", live)
        self.assertEqual("wake", live["wakeId"])
        wake = {"wakeId": "wake", "sessionKey": "target", "state": "fired",
                "firedAt": 10, "creatorSessionKey": "source", "prompt": "large"}
        mark = summarize("wakes", [wake], now=20)["marks"][0]
        self.assertNotIn("prompt", mark)
        self.assertEqual("source", mark["creatorSessionKey"])
        self.assertEqual([], summarize("turns", [dict(turn, endedAt=20)])["live"])

    def test_old_attests_are_counted_without_rows_or_observations(self):
        from atc_model import EvidenceModel
        from atc_upstream import Read
        model = EvidenceModel()
        rows = [{"id": str(n), "kind": "progress", "ts": n,
                 "bySession": "author", "note": "payload" * 100} for n in range(1000)]
        model.accept("attests", "task", Read(rows, True, 1_000_000, "fixture"))
        inventory = model.inventory("attests", "task")
        self.assertEqual({}, inventory.rows)
        self.assertEqual({}, inventory.observations)
        self.assertEqual(1000, model.summary("attests", "task")["count"])
        model.accept("attests", "task", Read([], False, 1_000_001, "fixture"))
        self.assertEqual(1000, model.summary("attests", "task")["count"])
        self.assertFalse(inventory.complete)
        model.accept("attests", "task", Read(rows, True, 1_000_002, "fixture"))
        self.assertEqual(1000, model.summary("attests", "task")["count"])
        self.assertFalse(inventory.payload(rows[0], at=1_000_003, source="feed", policy="immutable"))
        self.assertEqual(1000, inventory.folded["count"])

    def test_paged_history_folds_before_requesting_the_next_page(self):
        from atc_upstream import Gateway
        from unittest.mock import Mock, patch
        gateway = Gateway("http://example.invalid", Mock())
        pages = iter([
            {"schemaVersion": 1, "items": [{"id": "old", "at": 1, "content": "large"}],
             "page": {"hasMoreBefore": False, "hasMoreAfter": True, "newestCursor": "next"}},
            {"schemaVersion": 1, "items": [{"id": "new", "at": 999_999}],
             "page": {"hasMoreBefore": True, "hasMoreAfter": False}},
        ])
        gateway.request = lambda path: next(pages)
        with patch("atc_upstream.now_ms", return_value=1_000_000):
            result = gateway.read_current("messages", "session")
        self.assertTrue(result.complete)
        self.assertEqual([], result.rows)
        self.assertEqual(2, result.folded.summary["count"])
        self.assertEqual({"new"}, set(result.folded.rows))
        self.assertEqual(["new"], [m["id"] for m in result.folded.summary["marks"]])

    def test_old_wake_needed_by_live_turn_survives_raw_expiry(self):
        from atc_model import EvidenceModel
        from atc_upstream import Read
        model = EvidenceModel()
        model.accept("turns", "session", Read([
            {"seq": 1, "sessionKey": "session", "createdAt": 1,
             "startedAt": 2, "endedAt": None, "wakeId": "old", "prompt": "large"}],
            True, 1_000_000, "fixture"))
        model.accept("wakes", "session", Read([
            {"wakeId": "old", "sessionKey": "session", "createdAt": 1,
             "firedAt": 2, "state": "fired", "creatorSessionKey": "source", "prompt": "large"}],
            True, 1_000_000, "fixture"))
        wakes = model.inventory("wakes", "session")
        self.assertEqual("source", wakes.rows["old"]["creatorSessionKey"])
        self.assertNotIn("prompt", wakes.rows["old"])
        self.assertEqual([], wakes.folded["marks"])
        model.accept("turns", "session", Read([], True, 1_000_001, "fixture"))
        model.snapshot(1_000_001)
        self.assertEqual({}, wakes.rows)

    def test_expired_note_change_changes_fingerprint_without_retaining_note(self):
        from atc_model import EvidenceModel
        from atc_upstream import Read
        model = EvidenceModel()
        row = {"id": "old", "kind": "progress", "ts": 1, "note": "first"}
        model.accept("attests", "task", Read([row], True, 1_000_000, "fixture"))
        before = model.details_revision
        model.accept("attests", "task", Read([dict(row)], True, 1_000_001, "fixture"))
        self.assertEqual(before, model.details_revision)
        model.accept("attests", "task", Read([dict(row, note="changed")], True, 1_000_002, "fixture"))
        self.assertGreater(model.details_revision, before)
        self.assertEqual({}, model.inventory("attests", "task").rows)

    def test_partial_page_can_finish_old_live_turn_without_retaining_payload(self):
        from atc_model import EvidenceModel
        from atc_upstream import Read
        model = EvidenceModel()
        turn = {"seq": 1, "createdAt": 1, "startedAt": 2, "endedAt": None,
                "sessionKey": "session", "prompt": "large", "rowVersion": 1}
        model.accept("turns", "session", Read([turn], True, 1_000_000, "fixture"))
        model.accept("turns", "session", Read([dict(turn, endedAt=100, rowVersion=2)],
                                               False, 1_000_001, "fixture"))
        inventory = model.inventory("turns", "session")
        self.assertEqual([], inventory.folded["live"])
        self.assertEqual({}, inventory.rows)
        self.assertEqual(100, inventory.folded["activity"])
        self.assertFalse(inventory.complete)

    def test_partial_page_adds_four_minute_mark_without_raw_row(self):
        from atc_model import EvidenceModel
        from atc_upstream import Read
        model = EvidenceModel()
        model.accept("messages", "session", Read([], True, 1_000_000, "fixture"))
        model.accept("messages", "session", Read([
            {"id": "visible", "at": 760_000, "sessionKey": "session", "content": "large"}],
            False, 1_000_001, "fixture"))
        inventory = model.inventory("messages", "session")
        self.assertEqual({}, inventory.rows)
        self.assertEqual(["visible"], [r["id"] for r in inventory.folded["marks"]])
        self.assertFalse(inventory.complete)

    def test_recent_wake_required_by_live_turn_drops_payload_immediately(self):
        from atc_model import EvidenceModel
        from atc_upstream import Read
        model = EvidenceModel()
        model.accept("turns", "session", Read([
            {"seq": 1, "createdAt": 1, "startedAt": 2, "endedAt": None, "wakeId": "wake"}],
            True, 1_000_000, "fixture"))
        model.accept("wakes", "session", Read([
            {"wakeId": "wake", "createdAt": 999_999, "state": "fired",
             "firedAt": 999_999, "prompt": "large", "creatorSessionKey": "source"}],
            True, 1_000_000, "fixture"))
        inventory = model.inventory("wakes", "session")
        self.assertNotIn("prompt", inventory.rows["wake"])
        self.assertEqual({}, inventory.observations)

    def test_idle_worker_releases_completed_event_payload(self):
        import gc
        import queue
        import threading
        import time
        import weakref
        from unittest.mock import patch
        from atc_service import LiveService
        from test_service import PublishedReads

        class Payload:
            pass

        class ObservedQueue(queue.Queue):
            def __init__(self):
                super().__init__()
                self.calls = 0
                self.idle = threading.Event()

            def get(self, block=True, timeout=None):
                if block:
                    self.calls += 1
                    if self.calls >= 2:
                        self.idle.set()
                return super().get(block, timeout)

        service = LiveService(PublishedReads())
        self.addCleanup(service.stop)
        service.events = ObservedQueue()
        service._next_maintenance = time.monotonic() + 60
        payload = Payload()
        reference = weakref.ref(payload)
        service.events.put(('fixture', payload))
        del payload
        with patch.object(service, '_handle', new=lambda kind, value: False):
            worker = threading.Thread(target=service._worker)
            worker.start()
            try:
                self.assertTrue(service.events.idle.wait(5))
                gc.collect()
                self.assertIsNone(reference())
            finally:
                service.events.put(('stop', None))
                worker.join(5)
                self.assertFalse(worker.is_alive())
