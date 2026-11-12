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
