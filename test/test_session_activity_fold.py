"""Fixed session activity evidence uses the same winner for every arrival order."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_model import EvidenceModel


class SessionActivityFoldTest(unittest.TestCase):
    @staticmethod
    def facts(**changes):
        return dict(status="known", freshness="live", observedAt=10,
                    source="delivery", complete=True, unconfirmedSince=None) | changes

    def test_equal_maximum_uses_canonical_lexical_winner_in_either_order(self):
        records = []
        for scopes in (("z", "a"), ("a", "z")):
            model = EvidenceModel()
            for scope in scopes:
                model.fold_session_activity("author", 950000, "attest", scope, self.facts())
            records.append((model.session_activity, model.session_activity_evidence))
        self.assertEqual(records[0], records[1])
        self.assertEqual(950000, records[0][0]["author"])
        self.assertEqual(["attest", "cli:attests a"], records[0][1]["author"]["source"])

    def test_older_and_repeated_contributions_keep_only_winner_and_kinds(self):
        model = EvidenceModel()
        model.fold_session_activity("author key", 950000, "attest", "winner", self.facts())
        for index in range(100):
            model.fold_session_activity("author key", 900000, "attest", str(index), self.facts())
        model.fold_session_activity("author key", 800000, "turn", "author key", self.facts())
        self.assertEqual({"author key": 950000}, model.session_activity)
        self.assertEqual({"author key"}, model.session_activity_evidence.keys())
        self.assertEqual(["attest", "cli:attests winner", "turn"],
                         model.session_activity_evidence["author key"]["source"])
        model.fold_session_activity("author key", 960000, "turn", "author key", self.facts())
        self.assertEqual(["/api/sessions/author%20key/turns", "attest", "turn"],
                         model.session_activity_evidence["author key"]["source"])

    def test_winner_item_exit_retains_observation_until_newer_activity(self):
        for remaining in (None, 950000):
            with self.subTest(remaining=remaining):
                model = EvidenceModel()
                if remaining is not None:
                    model.fold_session_activity("author", remaining, "attest", "null", self.facts())
                model.fold_session_activity("author", 1000000, "attest", "winner", self.facts(), owner="item")
                model.fold_session_activity("unaffected", 800000, "attest", "other", self.facts())
                model.retain_exited_activity(set(), 1200000)
                self.assertEqual(1000000, model.session_activity["author"])
                facts = model.session_activity_facts("author", self.facts())
                self.assertEqual(["attest", "cli:attests winner"], facts["source"])
                self.assertFalse(facts["complete"])
                self.assertEqual("unconfirmed", facts["freshness"])
                self.assertEqual(1200000, facts["unconfirmedSince"])
                self.assertNotIn("_retainedAt", model.session_activity_evidence["unaffected"])
                model.fold_session_activity("author", 1100000, "attest", "next", self.facts())
                self.assertNotIn("_retainedAt", model.session_activity_evidence["author"])
                self.assertEqual(1100000, model.session_activity["author"])
                self.assertTrue(model.session_activity_facts("author", self.facts())["complete"])

    def test_turn_only_retirement_removes_record_and_attest_winner_keeps_value(self):
        model = EvidenceModel()
        model.fold_session_activity("turn-only", 950000, "turn", "turn-only", self.facts())
        model.fold_session_activity("mixed", 900000, "turn", "mixed", self.facts())
        model.fold_session_activity("mixed", 950000, "attest", "winner", self.facts())
        model.release_turn_activity("turn-only")
        model.release_turn_activity("mixed")
        self.assertNotIn("turn-only", model.session_activity)
        self.assertNotIn("turn-only", model.session_activity_evidence)
        self.assertEqual(950000, model.session_activity["mixed"])
        self.assertEqual(["attest", "cli:attests winner"],
                         model.session_activity_evidence["mixed"]["source"])

    def test_folding_does_not_erase_incomplete_or_stale_evidence(self):
        model = EvidenceModel()
        model.fold_session_activity("author", 950000, "attest", "winner", self.facts(
            status="unknown", complete=False, freshness="stale", unconfirmedSince=5))
        model.fold_session_activity("author", 960000, "turn", "author", self.facts(observedAt=20))
        record = model.session_activity_evidence["author"]
        self.assertEqual("unknown", record["status"])
        self.assertFalse(record["complete"])
        self.assertEqual("stale", record["freshness"])
        self.assertEqual(10, record["observedAt"])
        self.assertEqual(5, record["unconfirmedSince"])


if __name__ == "__main__":
    unittest.main()
