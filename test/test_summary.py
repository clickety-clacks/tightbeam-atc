import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_model import EvidenceModel
from atc_summary import summarize
from atc_upstream import Read


class SummaryTest(unittest.TestCase):
    def test_jobref_only_turns_count_as_work_item_activity(self):
        summary = summarize("turns", [
            {"seq": 1, "jobRef": "work", "startedAt": 10, "endedAt": 20},
            {"seq": 2, "jobRef": "work", "createdAt": 30},
            {"seq": 3, "jobRef": "running", "startedAt": 40},
            {"seq": 4, "startedAt": 50, "endedAt": 60},
        ])

        self.assertEqual({"work": 30, "running": 40}, summary["jobRefActivity"])
        self.assertEqual({}, summary["assignmentActivity"])

    def test_assignment_linked_turns_keep_existing_activity_path(self):
        summary = summarize("turns", [
            {"seq": 1, "assignmentId": "assignment", "jobRef": "work",
             "startedAt": 10, "endedAt": 20},
        ])

        self.assertEqual({"assignment": 20}, summary["assignmentActivity"])
        self.assertEqual({}, summary["jobRefActivity"])

    def test_jobref_only_turn_updates_latest_activity_without_attachment(self):
        model = EvidenceModel()
        item = {
            "id": "work", "title": "Work", "state": "open",
            "createdAt": 10, "rowVersion": 10, "holders": [], "started": False,
            "attestCounts": {}, "verdictCounts": {}, "effectiveCommitRefs": [], "landings": [],
        }
        model.accept("work-items", None, Read([item], True, 10, "test-read"))
        model.accept("assignments", None, Read([], True, 10, "test-read"))
        model.accept("sessions", None, Read([
            {"sessionKey": "agent s_author", "state": "active"},
        ], True, 10, "test-read"))
        model.accept("roles", None, Read([], True, 10, "test-read"))

        before = model.snapshot(100)["items"][0]
        model.accept("turns", "agent s_author", Read([
            {"seq": 1, "sessionKey": "agent s_author", "jobRef": "work",
             "startedAt": 50, "endedAt": 80},
        ], True, 80, "test-read"))
        after = model.snapshot(100)["items"][0]

        self.assertEqual(10, before["latestActivityAt"])
        self.assertEqual(80, after["latestActivityAt"])
        self.assertEqual("open", after["state"])
        self.assertEqual([], after["holders"])
        self.assertFalse(after["started"])
        self.assertEqual(0, after["assignments"]["open"])
        self.assertEqual({}, after["turns"], "jobRef activity is not an attached turn")

    def test_jobref_activity_cache_updates_and_removes_with_its_turn_scope(self):
        model = EvidenceModel()
        model.accept("assignments", None, Read([], True, 10, "test-read"))
        model.accept("turns", "agent s_author", Read([
            {"seq": 1, "jobRef": "work", "endedAt": 20},
        ], True, 20, "test-read"))
        self.assertEqual({"work": 20}, model.item_activity())

        model.accept("turns", "agent s_author", Read([], False, 30, "partial-read"))
        self.assertEqual({"work": 20}, model.item_activity())
        model.accept("turns", "agent s_author", Read([
            {"seq": 1, "jobRef": "work", "endedAt": 40},
        ], True, 40, "test-read"))
        self.assertEqual({"work": 40}, model.item_activity())
        model.accept("turns", "agent s_author", Read([], True, 50, "test-read"))
        self.assertEqual({}, model.item_activity())


if __name__ == "__main__":
    unittest.main()
