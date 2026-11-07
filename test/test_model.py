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

    def test_partial_attest_load_keeps_full_persisted_proof_visible(self):
        proof = [{"repo": "example", "commit": f"{n:040x}"} for n in range(15)]
        self.model.merges["work"] = {
            "value": True, "refs": proof, "observedAt": 5, "freshness": "stale",
        }
        self.read("attests", [{"id": "done", "kind": "completion",
                  "assignmentId": "assignment", "ts": 1,
                  "commitRefs": proof[:1]}], "assignment", complete=False)
        item = self.model.snapshot(20)["items"][0]
        self.assertFalse(item["commitRefsComplete"])
        self.assertTrue(item["merged"])
        self.assertEqual("stale", item["evidence"]["merged"]["freshness"])
        self.read("attests", [{"id": "done", "kind": "completion",
                  "assignmentId": "assignment", "ts": 1,
                  "commitRefs": proof}], "assignment")
        item = self.model.snapshot(20)["items"][0]
        self.assertTrue(item["commitRefsComplete"])
        self.assertTrue(item["merged"])

    def test_grown_merge_proof_stays_visible_until_proving_refs_are_removed(self):
        proof = [{"repo": "example", "commit": "a" * 40},
                 {"repo": "example", "commit": "b" * 40}]
        added = {"repo": "example", "commit": "c" * 40}
        for summary in (False, True):
            with self.subTest(summary=summary):
                self.model.merges["work"] = {
                    "value": True, "observedAt": 5, "refs": proof,
                    "freshness": "live",
                }
                for refs, expected in (([added, *proof], True),
                                       ([proof[0], added], None),
                                       ([], None), (list(reversed(proof)), True)):
                    row = {"id": "work", "title": "Work", "state": "closed"}
                    if summary:
                        row.update(holders=[], started=True, attestCounts={},
                                   verdictCounts={}, effectiveCommitRefs=refs, landings=[])
                    self.read("work-items", [row])
                    self.read("attests", [{"id": "done", "kind": "completion",
                              "assignmentId": "assignment", "ts": 1,
                              "commitRefs": refs}], "assignment")
                    item = self.model.snapshot(20)["items"][0]
                    self.assertIs(expected, item["merged"])
                    self.assertEqual("stale", item["evidence"]["merged"]["freshness"])
                    if expected:
                        self.assertEqual(5, item["evidence"]["merged"]["observedAt"])

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
        snapshot = self.model.snapshot(20)
        self.assertEqual({"s_author": "run"}, snapshot["items"][0]["turns"])
        turns_evidence = snapshot["items"][0]["evidence"]["turns"]
        self.assertEqual(["turns"], turns_evidence["source"])
        self.assertIsNone(turns_evidence["observedAt"])
        self.assertEqual("test-read", snapshot["agents"][0]["evidence"]["turn"]["source"])
        self.assertEqual(10, snapshot["agents"][0]["evidence"]["turn"]["observedAt"])
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

    def test_work_item_summary_drives_counts_effective_refs_and_observed_landing(self):
        model = EvidenceModel()
        ref = {"repo": "example/repo", "ref": "refs/heads/0.1.14", "commit": "a" * 40}
        landing = {"repository": "example/repo", "branch": "main",
                   "commit": "a" * 40, "headCommit": "b" * 40,
                   "pullRequest": 12, "observedAt": 100}
        item = {"id": "work", "title": "Work", "state": "closed", "rowVersion": 4,
                "holders": [], "started": True,
                "attestCounts": {"completion": 1, "verdict": 1},
                "verdictCounts": {"reviewed-clean": 1},
                "effectiveCommitRefs": [ref], "landings": [landing]}
        assignment = {"id": "assignment", "workItemId": "work", "holderKey": "agent s_author",
                      "state": "closed", "closedAt": 20, "effectKind": "code",
                      "reviewsAssignmentId": None,
                      "commitRefCorrections": [{"commitRefs": [ref], "actorRef": "person"}]}
        model.accept("work-items", None, Read([item], True, 1000, "work-items"))
        model.accept("assignments", None, Read([assignment], True, 1000, "assignments"))
        model.accept("attests", "assignment", Read([
            {"id": "done", "assignmentId": "assignment", "kind": "completion",
             "ts": 10, "bySession": "agent s_author", "commitRefs": [{"repo": "old/repo", "commit": "c" * 40}]},
            {"id": "review", "assignmentId": "assignment", "kind": "verdict",
             "verdictKind": "reviewed-clean", "ts": 20, "commitRefs": []},
        ], True, 1000, "attests"))
        model.accept("sessions", None, Read([], True, 1000, "sessions"))
        model.accept("roles", None, Read([], True, 1000, "roles"))

        # An observed landing takes precedence over a cached negative Git result.
        model.merges["work"] = {"value": False, "observedAt": 90, "refs": [ref]}
        result = model.snapshot(1000)["items"][0]
        self.assertTrue(result["started"])
        self.assertEqual([], result["holders"])
        self.assertEqual({"total": 2, "byKind": {"completion": 1, "verdict": 1},
                          "byVerdict": {"reviewed-clean": 1}}, result["evidenceCounts"])
        self.assertEqual([ref], result["commitRefs"], "effective refs must retain corrections")
        self.assertEqual([landing], result["landings"])
        self.assertTrue(result["merged"])
        self.assertEqual("known", result["evidence"]["merged"]["status"])
        self.assertEqual(100, result["evidence"]["merged"]["observedAt"])
        self.assertEqual(["work-items"], result["evidence"]["merged"]["source"])

        item["landings"] = []
        item["rowVersion"] = 5
        model.accept("work-items", None, Read([item], True, 2000, "work-items"))
        model.merges.clear()
        absent = model.snapshot(2000)["items"][0]
        self.assertIsNone(absent["merged"], "no observed landing is not a negative merge result")
        self.assertEqual("unknown", absent["evidence"]["merged"]["status"])
        self.assertFalse(absent["evidence"]["merged"]["complete"])

        # With no observed landing, a matching resolver result for a release-line
        # commit supplies the merged state.
        model.merges["work"] = {"value": True, "observedAt": 1500, "refs": [ref]}
        resolved = model.snapshot(2000)["items"][0]
        self.assertEqual([], resolved["landings"])
        self.assertTrue(resolved["merged"])
        self.assertEqual("git", resolved["evidence"]["merged"]["source"])
        self.assertEqual(1500, resolved["evidence"]["merged"]["observedAt"])

        # Persisted evidence survives a process restart but remains visibly stale
        # until the resolver confirms it again.
        model.merges["work"]["freshness"] = "stale"
        resumed = model.snapshot(2000)["items"][0]
        self.assertTrue(resumed["merged"])
        self.assertEqual("stale", resumed["evidence"]["merged"]["freshness"])

    def test_summary_does_not_guess_stage_when_attest_counts_exceed_retained_detail(self):
        model = EvidenceModel()
        model.accept("work-items", None, Read([{
            "id": "work", "title": "Work", "state": "open", "rowVersion": 1,
            "holders": [], "started": True,
            "attestCounts": {"completion": 1, "verdict": 2},
            "verdictCounts": {"reviewed-clean": 1, "tests-passed": 1},
            "effectiveCommitRefs": [], "landings": [],
        }], True, 100, "work-items"))
        model.accept("assignments", None, Read([{
            "id": "assignment", "workItemId": "work", "holderKey": "agent s_author",
            "state": "closed", "closedAt": 10, "effectKind": "code",
        }], True, 100, "assignments"))
        model.accept("attests", "assignment", Read([{
            "id": "done", "assignmentId": "assignment", "kind": "completion",
            "ts": 10, "bySession": "agent s_author", "commitRefs": [],
        }], True, 100, "attests"))
        model.accept("sessions", None, Read([], True, 100, "sessions"))
        model.accept("roles", None, Read([], True, 100, "roles"))

        result = model.snapshot(100)["items"][0]
        self.assertIsNone(result["stage"])
        self.assertEqual("unknown", result["evidence"]["stage"]["status"])
        self.assertEqual(3, result["evidenceCounts"]["total"], "summary counts remain available")

    def test_summary_stage_timestamp_order_does_not_outlive_stale_detail(self):
        model = EvidenceModel()
        model.accept("work-items", None, Read([{
            "id": "work", "title": "Work", "state": "open", "rowVersion": 1,
            "holders": [], "started": True,
            "attestCounts": {"completion": 1, "verdict": 2},
            "verdictCounts": {"reviewed-clean": 1, "tests-passed": 1},
            "effectiveCommitRefs": [], "landings": [],
        }], True, 100, "work-items"))
        model.accept("assignments", None, Read([{
            "id": "assignment", "workItemId": "work", "holderKey": "agent s_author",
            "state": "closed", "closedAt": 10, "effectKind": "code",
        }], True, 100, "assignments"))
        model.accept("attests", "assignment", Read([
            {"id": "done", "assignmentId": "assignment", "kind": "completion",
             "ts": 10, "bySession": "agent s_author", "commitRefs": []},
            {"id": "tested", "assignmentId": "assignment", "kind": "verdict",
             "verdictKind": "tests-passed", "ts": 20, "bySession": "agent s_author",
             "commitRefs": []},
            {"id": "reviewed", "assignmentId": "assignment", "kind": "verdict",
             "verdictKind": "reviewed-clean", "ts": 30, "bySession": "agent s_author",
             "commitRefs": []},
        ], True, 100, "attests"))
        model.inventory("attests", "assignment").mark_unconfirmed(101)
        model.accept("sessions", None, Read([], True, 100, "sessions"))
        model.accept("roles", None, Read([], True, 100, "roles"))

        result = model.snapshot(102)["items"][0]
        self.assertIsNone(result["stage"])
        self.assertEqual("unknown", result["evidence"]["stage"]["status"])
        self.assertEqual("unconfirmed", result["evidence"]["stage"]["freshness"])

    def test_summary_refs_do_not_invent_non_code_without_producer_attribution(self):
        model = EvidenceModel()
        ref = {"repo": "example/repo", "commit": "a" * 40}
        model.accept("work-items", None, Read([{
            "id": "work", "title": "Work", "state": "open", "rowVersion": 1,
            "holders": [], "started": True,
            "attestCounts": {"completion": 1, "verdict": 1},
            "verdictCounts": {"reviewed-clean": 1},
            "effectiveCommitRefs": [ref], "landings": [],
        }], True, 100, "work-items"))
        model.accept("assignments", None, Read([{
            "id": "review", "workItemId": "work", "holderKey": "agent s_reviewer",
            "state": "closed", "closedAt": 10, "effectKind": "review",
            "reviewsAssignmentId": "producer",
        }], True, 100, "assignments"))
        model.accept("sessions", None, Read([], True, 100, "sessions"))
        model.accept("roles", None, Read([], True, 100, "roles"))

        result = model.snapshot(100)["items"][0]
        self.assertEqual([ref], result["commitRefs"])
        self.assertEqual(5, result["stage"])
        self.assertIsNone(result["code"])
        self.assertIsNone(result["ring"])
        self.assertEqual("unknown", result["evidence"]["ring"]["status"])


if __name__ == "__main__":
    unittest.main()
