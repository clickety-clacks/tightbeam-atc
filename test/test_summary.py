import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_model import EvidenceModel
from atc_summary import ItemActivityFold, summarize
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
        item = {
            "id": "work", "title": "Work", "state": "open",
            "createdAt": 10, "rowVersion": 10, "holders": [], "started": False,
            "attestCounts": {}, "verdictCounts": {}, "effectiveCommitRefs": [], "landings": [],
        }
        model.accept("work-items", None, Read([item], True, 10, "test-read"))
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

    def test_partial_turn_read_advances_jobref_activity(self):
        model = EvidenceModel()
        item = {
            "id": "work", "title": "Work", "state": "open",
            "createdAt": 10, "rowVersion": 10, "holders": [], "started": False,
            "attestCounts": {}, "verdictCounts": {}, "effectiveCommitRefs": [], "landings": [],
        }
        model.accept("work-items", None, Read([item], True, 10, "test-read"))
        model.accept("assignments", None, Read([], True, 10, "test-read"))
        live = {"seq": 1, "sessionKey": "author", "jobRef": "work",
                "createdAt": 10, "startedAt": 20, "endedAt": None}
        model.accept("turns", "author", Read([live], True, 25, "test-read"))
        self.assertEqual({"work": 20}, model.item_activity())

        model.accept("turns", "author", Read([
            dict(live, endedAt=50, rowVersion=2),
        ], False, 50, "partial-read"))
        self.assertEqual({"work": 50}, model.item_activity())

    def test_jobref_survives_compaction_of_old_live_turn(self):
        model = EvidenceModel()
        model.accept("turns", "author", Read([
            {"seq": 1, "sessionKey": "author", "jobRef": "work",
             "createdAt": 1, "startedAt": 2, "endedAt": None},
        ], True, 1_000_000, "test-read"))

        inventory = model.inventory("turns", "author")
        self.assertEqual("work", inventory.rows["1"]["jobRef"])
        self.assertEqual({"work": 2}, inventory.folded["jobRefActivity"])

    def test_retired_turn_fold_keeps_jobref_activity_without_attachment(self):
        fold = ItemActivityFold({"assignment": "assigned-item"})
        fold.add([
            {"assignmentId": "assignment", "jobRef": "other-item", "endedAt": 70},
            {"jobRef": "jobref-item", "createdAt": 30, "endedAt": 80},
            {"assignmentId": "missing", "jobRef": "not-jobref-only", "endedAt": 90},
        ])

        self.assertEqual({"assigned-item": 70, "jobref-item": 80}, fold.activity)

    def test_retiring_session_preserves_jobref_item_activity(self):
        model = EvidenceModel()
        item = {
            "id": "work", "title": "Work", "state": "open",
            "createdAt": 10, "rowVersion": 10, "holders": [], "started": False,
            "attestCounts": {}, "verdictCounts": {}, "effectiveCommitRefs": [], "landings": [],
        }
        model.accept("work-items", None, Read([item], True, 10, "test-read"))
        model.accept("assignments", None, Read([], True, 10, "test-read"))
        model.accept("turns", "author", Read([
            {"seq": 1, "sessionKey": "author", "jobRef": "work", "endedAt": 80},
        ], True, 80, "test-read"))

        model.release_session_histories(set(), at=90)

        self.assertEqual({"work": 80}, model.retired_item_activity)
        self.assertEqual({"work": 80}, model.item_activity())


if __name__ == "__main__":
    unittest.main()
