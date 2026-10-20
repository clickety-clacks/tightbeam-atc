import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_model import EvidenceModel
from atc_upstream import Read


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.model = EvidenceModel()
        self.read("work-items", [{"id": "work", "title": "Work", "state": "open"}])
        self.read("assignments", [{"id": "assignment", "workItemId": "work",
                                  "holderKey": "agent s_author", "state": "closed",
                                  "closedAt": 10, "effectKind": "code",
                                  "reviewsAssignmentId": None}])
        self.read("attests", [
            {"id": "done", "assignmentId": "assignment", "kind": "completion",
             "ts": 1, "bySession": "agent s_author", "commitRefs": []},
            {"id": "review", "assignmentId": "assignment", "kind": "verdict",
             "verdictKind": "reviewed-clean", "ts": 2, "commitRefs": []},
        ], "assignment")
        self.read("sessions", [{"sessionKey": "agent s_author", "displayName": "Author",
                                "state": "active", "topologyParent": "agent s_missing",
                                "spawnedBy": "agent s_spawner", "archetype": "coder"}])
        self.read("roles", [])
        for kind in ("turns", "wakes", "messages"):
            self.read(kind, [], "agent s_author")

    def read(self, kind, rows, scope=None, complete=True):
        self.model.accept(kind, scope, Read(rows, complete, 10, "test-read"))

    def test_unknown_parent_is_not_invented(self):
        agent = self.model.snapshot(20)["agents"][0]
        self.assertIsNone(agent["parent"])
        self.assertEqual("agent s_missing", agent["parentSessionKey"])
        self.assertEqual("unknown", agent["evidence"]["parent"]["status"])

    def test_failed_attest_read_retains_item_but_cannot_establish_stage(self):
        before = self.model.snapshot(20)["items"][0]
        self.assertEqual(5, before["stage"])
        self.assertEqual("yellow", before["ring"])
        self.assertFalse(before["readyToMerge"])
        self.read("attests", [], "assignment", complete=False)
        after = self.model.snapshot(20)["items"][0]
        self.assertEqual(before["id"], after["id"])
        self.assertIsNone(after["stage"])
        self.assertIsNone(after["evidenceCounts"]["total"])
        self.assertEqual(2, len(self.model.inventory("attests", "assignment").rows))
        self.assertEqual("stale", after["evidence"]["stage"]["freshness"])

    def test_due_wake_is_not_a_queued_turn(self):
        self.read("wakes", [{"wakeId": "wake", "sessionKey": "agent s_author",
                             "state": "pending", "dueAt": 1}], "agent s_author")
        self.assertEqual("idle", self.model.snapshot(20)["agents"][0]["turn"])
        self.assertEqual({}, self.model.snapshot(20)["items"][0]["turns"])

    def test_unlinked_turn_uses_only_attests_written_during_that_turn(self):
        self.read("turns", [{"seq": 1, "sessionKey": "agent s_author",
                             "assignmentId": None, "startedAt": 1, "endedAt": None}],
                  "agent s_author")
        self.assertEqual({"s_author": "run"}, self.model.snapshot(20)["items"][0]["turns"])
        self.read("turns", [{"seq": 1, "sessionKey": "agent s_author",
                             "assignmentId": None, "startedAt": 3, "endedAt": None}],
                  "agent s_author")
        self.assertEqual({}, self.model.snapshot(20)["items"][0]["turns"])

    def test_corrected_refs_replace_historical_refs_without_rewriting_authorship(self):
        assignment = self.model.inventory("assignments").rows["assignment"]
        corrected = {"repo": "host:/checkout", "remote": "https://example.invalid/repo",
                     "ref": "refs/heads/main", "commit": "a" * 40}
        assignment["commitRefCorrections"] = [{"commitRefs": [corrected], "actorRef": "person"}]
        item = self.model.snapshot(20)["items"][0]
        self.assertEqual([corrected], item["commitRefs"])
        self.assertEqual("person", item["commitRefCorrections"][0]["actorRef"])
        self.assertNotIn("evidenceRecords", item)
        self.assertEqual("agent s_author",
                         self.model.inventory("attests", "assignment").rows["done"]["bySession"])

    def test_inventory_does_not_fill_in_missing_ladder_steps(self):
        self.read("attests", [{"id": "v", "assignmentId": "assignment", "kind": "verdict",
                              "verdictKind": "verified", "ts": 1, "commitRefs": []}],
                  "assignment")
        item = self.model.snapshot(20)["items"][0]
        self.assertEqual(["verdict"], item["attestKinds"])
        self.assertEqual(["verified"], item["verdictKinds"])
        self.assertEqual(4, item["stage"])

    def test_counts_and_effects_derive_from_the_same_assignment_inventory(self):
        old = dict(self.model.inventory("assignments").rows["assignment"], effectKind="coordination")
        self.read("assignments", [old])
        item = self.model.snapshot(20)["items"][0]
        self.assertIs(False, item["code"])
        self.assertEqual({"open": 0, "terminal": 1}, item["assignments"])
        added = dict(old, id="new", state="open", effectKind=None)
        self.read("assignments", [old, added])
        self.read("attests", [], "new")
        item = self.model.snapshot(20)["items"][0]
        self.assertIsNone(item["code"])
        self.assertEqual({"open": 1, "terminal": 1}, item["assignments"])
        self.read("assignments", [], complete=False)
        item = self.model.snapshot(20)["items"][0]
        self.assertEqual({"open": None, "terminal": None}, item["assignments"])

    def test_role_kind_comes_from_records_and_not_display_name(self):
        row = self.model.inventory("sessions").rows["agent s_author"]
        row["displayName"] = "Product Owner By Name Only"
        self.assertEqual("coder", self.model.snapshot(20)["agents"][0]["kind"])


if __name__ == "__main__":
    unittest.main()
