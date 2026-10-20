import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import FeedCursor


class FeedRecoveryTest(unittest.TestCase):
    def test_missing_next_change_is_a_gap(self):
        cursor = FeedCursor(0)
        with self.assertRaises(ValueError):
            cursor.accept({"type": "change", "schemaVersion": 2, "seq": 2}, 1)

    def test_heartbeat_can_include_frames_queued_for_delivery(self):
        cursor = FeedCursor(0)
        cursor.accept({"type": "heartbeat", "seq": 2}, 1)
        cursor.accept({"type": "change", "schemaVersion": 2, "seq": 1}, 2)
        cursor.accept({"type": "change", "schemaVersion": 2, "seq": 2}, 3)
        self.assertIsNone(cursor.pending_since)
        self.assertGreater(cursor.remaining(4), 0)

    def test_changes_do_not_conceal_missing_heartbeat(self):
        cursor = FeedCursor(0)
        cursor.accept({"type": "change", "schemaVersion": 2, "seq": 1}, 40)
        with self.assertRaises(TimeoutError):
            cursor.remaining(46)

    def test_heartbeat_does_not_conceal_stalled_allocated_frames(self):
        cursor = FeedCursor(0)
        cursor.accept({"type": "heartbeat", "seq": 2}, 1)
        cursor.accept({"type": "heartbeat", "seq": 2}, 30)
        with self.assertRaises(TimeoutError):
            cursor.remaining(47)
