"""Fixed session activity evidence uses the same winner for every arrival order."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_model import EvidenceModel
from atc_upstream import Read


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
        self.assertEqual(["cli:attests a"], records[0][1]["author"]["source"])

    def test_older_and_repeated_contributions_keep_only_winner(self):
        model = EvidenceModel()
        model.fold_session_activity("author key", 950000, "attest", "winner", self.facts())
        for index in range(100):
            model.fold_session_activity("author key", 900000, "attest", str(index), self.facts())
        model.fold_session_activity("author key", 800000, "turn", "author key", self.facts())
        self.assertEqual({"author key": 950000}, model.session_activity)
        self.assertEqual({"author key"}, model.session_activity_evidence.keys())
        self.assertEqual(["cli:attests winner"],
                         model.session_activity_evidence["author key"]["source"])
        model.fold_session_activity("author key", 960000, "turn", "author key", self.facts())
        self.assertEqual(["/api/sessions/author%20key/turns"],
                         model.session_activity_evidence["author key"]["source"])

    def test_null_assignment_source_is_singleton_with_or_without_losing_sources(self):
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                model = EvidenceModel()
                if mixed:
                    model.fold_session_activity('S', 900000, 'attest', 'A', self.facts(), owner='item')
                    model.fold_session_activity('S', 800000, 'turn', 'S', self.facts())
                model.fold_session_activity('S', 950000, 'attest', 'B', self.facts(), owner=None)
                self.assertEqual(950000, model.session_activity['S'])
                self.assertEqual(['cli:attests B'], model.session_activity_evidence['S']['source'])

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
                self.assertEqual(["cli:attests winner"], facts["source"])
                self.assertFalse(facts["complete"])
                self.assertEqual("unconfirmed", facts["freshness"])
                self.assertEqual(1200000, facts["unconfirmedSince"])
                self.assertNotIn("_retainedAt", model.session_activity_evidence["unaffected"])
                model.fold_session_activity("author", 1100000, "attest", "next", self.facts())
                self.assertNotIn("_retainedAt", model.session_activity_evidence["author"])
                self.assertEqual(1100000, model.session_activity["author"])
                self.assertTrue(model.session_activity_facts("author", self.facts())["complete"])

    def test_turn_winner_retirement_retains_record_and_losing_turn_exit_keeps_winner(self):
        model = EvidenceModel()
        model.fold_session_activity("turn-only", 950000, "turn", "turn-only", self.facts())
        model.fold_session_activity("mixed", 900000, "turn", "mixed", self.facts())
        model.fold_session_activity("mixed", 950000, "attest", "winner", self.facts())
        model.release_turn_activity("turn-only", 1000000)
        model.release_turn_activity("mixed", 1000000)
        self.assertEqual(950000, model.session_activity["turn-only"])
        self.assertEqual(1000000, model.session_activity_evidence["turn-only"]["_retainedAt"])
        self.assertEqual(950000, model.session_activity["mixed"])
        self.assertEqual(["cli:attests winner"],
                         model.session_activity_evidence["mixed"]["source"])

    def test_new_attest_after_turn_winner_exit_replaces_retained_winner(self):
        model = EvidenceModel()
        model.fold_session_activity("author", 950000, "attest", "null", self.facts())
        model.fold_session_activity("author", 1000000, "turn", "author", self.facts())
        model.release_turn_activity("author", 1050000)
        self.assertEqual(["/api/sessions/author/turns"],
                         model.session_activity_evidence["author"]["source"])
        model.fold_session_activity("author", 1100000, "attest", "null", self.facts())
        self.assertEqual(["cli:attests null"],
                         model.session_activity_evidence["author"]["source"])
        self.assertNotIn("_retainedAt", model.session_activity_evidence["author"])

    def test_equal_time_losing_attest_does_not_reconfirm_retained_turn_winner(self):
        model = EvidenceModel()
        model.fold_session_activity("author", 1000000, "turn", "author", self.facts())
        model.release_turn_activity("author", 1050000)
        for scope in ("first", "second", "first"):
            model.fold_session_activity("author", 1000000, "attest", scope, self.facts())
            record = model.session_activity_evidence["author"]
            self.assertEqual(1050000, record["_retainedAt"])
            self.assertEqual(["/api/sessions/author/turns"], record["source"])
            facts = model.session_activity_facts("author", self.facts())
            self.assertFalse(facts["complete"])
            self.assertEqual("unconfirmed", facts["freshness"])
            self.assertEqual(1050000, facts["unconfirmedSince"])
        # The next strictly newer observation replaces the retained winner.
        model.fold_session_activity("author", 1100000, "attest", "second", self.facts())
        fresh = EvidenceModel()
        for scope, timestamp in (("first", 1000000), ("second", 1100000)):
            fresh.fold_session_activity("author", timestamp, "attest", scope, self.facts())
        self.assertEqual(fresh.session_activity, model.session_activity)
        self.assertEqual(fresh.session_activity_evidence, model.session_activity_evidence)

    def test_newer_same_turn_source_reconfirms_retained_winner(self):
        model = EvidenceModel()
        model.fold_session_activity("author", 1000000, "turn", "author", self.facts())
        model.release_turn_activity("author", 1050000)
        model.fold_session_activity("author", 1100000, "turn", "author", self.facts())
        self.assertNotIn("_retainedAt", model.session_activity_evidence["author"])
        self.assertTrue(model.session_activity_facts("author", self.facts())["complete"])

    def test_complete_returning_winner_reconfirms_but_equal_other_source_does_not(self):
        model = EvidenceModel()
        model.fold_session_activity('author', 950000, 'attest', 'winner', self.facts(), owner='item')
        model.retain_exited_activity(set(), 1000000)
        model.fold_session_activity('author', 950000, 'attest', 'z-other', self.facts(),
                                    owner='other', reconfirmed=True)
        self.assertIn('_retainedAt', model.session_activity_evidence['author'])
        model.fold_session_activity('author', 950000, 'attest', 'winner',
                                    self.facts(complete=False), owner='item', reconfirmed=True)
        self.assertIn('_retainedAt', model.session_activity_evidence['author'])
        model.fold_session_activity('author', 950000, 'attest', 'winner', self.facts(),
                                    owner='item', reconfirmed=True)
        self.assertNotIn('_retainedAt', model.session_activity_evidence['author'])
        self.assertEqual(950000, model.session_activity['author'])
        self.assertEqual(['cli:attests winner'], model.session_activity_evidence['author']['source'])

    def test_only_complete_assignment_membership_can_establish_winner_exit(self):
        model = EvidenceModel()
        model.fold_session_activity("author", 1000000, "attest", "winner", self.facts())
        model.accept("assignments", None, Read([], False, 1100000, "assignments"))
        self.assertNotIn("_retainedAt", model.session_activity_evidence["author"])
        model.accept("assignments", None, Read([], True, 1200000, "assignments"))
        self.assertEqual(1000000, model.session_activity["author"])
        self.assertEqual(1200000, model.session_activity_evidence["author"]["_retainedAt"])
        self.assertEqual(["cli:attests winner"], model.session_activity_evidence["author"]["source"])

    def test_refresh_equal_losing_source_cannot_erase_retained_winner(self):
        model = EvidenceModel()
        model.fold_session_activity('author', 1000000, 'turn', 'author', self.facts())
        model.begin_activity_refresh()
        model.fold_session_activity('author', 1000000, 'attest', 'other', self.facts())
        model.release_turn_activity('author', 1100000)
        model.finish_activity_refresh()
        self.assertEqual(1000000, model.session_activity['author'])
        self.assertEqual(['/api/sessions/author/turns'],
                         model.session_activity_evidence['author']['source'])
        self.assertEqual(1100000, model.session_activity_evidence['author']['_retainedAt'])
        self.assertFalse(model.session_activity_facts('author', self.facts())['complete'])

    def test_resumed_refresh_does_not_reconfirm_a_pre_outage_record(self):
        model = EvidenceModel()
        model.begin_activity_refresh()
        model.fold_session_activity("author", 950000, "attest", "winner", self.facts())
        model.mark_unconfirmed(20)
        model.finish_activity_refresh()
        facts = model.session_activity_facts("author", self.facts())
        self.assertEqual(950000, model.session_activity["author"])
        self.assertEqual("unconfirmed", facts["freshness"])
        self.assertEqual(20, facts["unconfirmedSince"])
        # A subsequent full source reconstruction starts a new confirmation
        # fold; it is distinct from keeping pre-outage completion credit.
        model.begin_activity_refresh()
        model.fold_session_activity("author", 950000, "attest", "winner", self.facts(observedAt=30))
        model.finish_activity_refresh()
        facts = model.session_activity_facts("author", self.facts(observedAt=30))
        self.assertEqual("live", facts["freshness"])
        self.assertIsNone(facts["unconfirmedSince"])

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
