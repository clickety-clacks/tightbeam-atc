import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_evidence import Inventory, evidence_stage, merge_ring, turn_state


class EvidenceTest(unittest.TestCase):
    def test_partial_read_preserves_rows_and_does_not_prove_absence(self):
        inventory = Inventory()
        inventory.read([{"id": "one"}, {"id": "two"}],
                       complete=True, at=1, source="read")
        inventory.read([{"id": "one", "title": "changed"}],
                       complete=False, at=2, source="read")
        self.assertEqual({"one", "two"}, set(inventory.rows))
        self.assertEqual("unknown", inventory.evidence()["status"])
        self.assertEqual("stale", inventory.observations["two"]["freshness"])
        inventory.read([{"id": "one"}], complete=True, at=3, source="read")
        self.assertEqual({"one"}, set(inventory.rows))

    def test_buffered_old_payload_cannot_regress_a_newer_read(self):
        inventory = Inventory()
        inventory.read([{"id": "one", "rowVersion": 20, "state": "closed"}],
                       complete=True, at=1, source="read")
        inventory.payload({"id": "one", "rowVersion": 19, "state": "open"},
                          at=2, source="feed", policy="strict-version")
        self.assertEqual("closed", inventory.rows["one"]["state"])
        self.assertFalse(inventory.payload(
            {"id": "one", "rowVersion": 20, "state": "open"},
            at=3, source="feed", policy="strict-version"))
        self.assertTrue(inventory.stale)

    def test_one_payload_does_not_make_an_incomplete_inventory_complete(self):
        inventory = Inventory()
        inventory.payload({"id": "one", "kind": "progress"},
                          at=2, source="feed", policy="immutable")
        self.assertFalse(inventory.complete)

    def test_unknown_is_not_stage_zero(self):
        self.assertIsNone(evidence_stage({"state": "open"}, [], [], complete=False))
        self.assertEqual(0, evidence_stage({"state": "open"}, [], [], complete=True))
        self.assertEqual(4, evidence_stage({"state": "open"}, [], [
            {"kind": "verdict", "verdictKind": "verified", "ts": 1}], complete=True))

    def test_floor_does_not_manufacture_ready_to_merge(self):
        records = [
            {"kind": "completion", "ts": 1},
            {"kind": "verdict", "verdictKind": "reviewed-clean", "ts": 2},
        ]
        stage = evidence_stage({"state": "open"}, [], records, complete=True)
        self.assertEqual(5, stage)
        self.assertEqual("yellow", merge_ring(stage, None, False, True))
        self.assertEqual("green", merge_ring(stage, None, True, True))
        self.assertEqual("black", merge_ring(stage, True, True, True))
        self.assertEqual("light-gray", merge_ring(stage, None, False, False))
        self.assertIsNone(merge_ring(stage, False, False, True))
        self.assertIsNone(merge_ring(4, True, True, True))

    def test_review_window_is_measured_from_actual_attest_times(self):
        records = [
            {"kind": "completion", "ts": 1},
            {"kind": "verdict", "verdictKind": "reviewed-clean", "ts": 100},
            {"kind": "verdict", "verdictKind": "tests-passed", "ts": 300100},
        ]
        self.assertEqual(5, evidence_stage({"state": "open"}, [], records, complete=True))
        records[-1]["ts"] += 1
        self.assertEqual(4, evidence_stage({"state": "open"}, [], records, complete=True))

    def test_activity_requires_turn_evidence(self):
        self.assertIsNone(turn_state([], complete=False))
        self.assertEqual("idle", turn_state([], complete=True))
        self.assertEqual("wait", turn_state(
            [{"startedAt": None, "endedAt": None}], complete=True))
        self.assertEqual("run", turn_state(
            [{"startedAt": 1, "endedAt": None}], complete=True))


if __name__ == "__main__":
    unittest.main()
