"""The R25 frontier retains only IDs at the current counted maximum."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_contributions import AttestFrontier, AssignmentContribution
from atc_evidence import HistoryInventory
from atc_summary import HistoryFold


class AttestFrontierTest(unittest.TestCase):
    @staticmethod
    def row(identity, timestamp, **values):
        return dict(id=identity, ts=timestamp, kind='progress', bySession='author',
                    **values)

    def read(self, inventory, rows, *, complete=True, at=1000000):
        fold = HistoryFold('attests', at)
        # Fold across pages; raw rows expire independently of maximum IDs.
        for row in rows:
            fold.add([row])
        inventory.read_fold(fold, complete=complete, at=at, source='fixture')

    def test_complete_fold_releases_older_ids_and_keeps_all_maximum_ties(self):
        inventory = HistoryInventory('attests')
        self.read(inventory, [self.row('X', 1000), self.row('Y', 2000),
                              self.row('Z', 2000)])
        self.assertEqual({}, inventory.rows)
        self.assertEqual(3, inventory.folded['count'])
        self.assertEqual(AttestFrontier(2000, frozenset({'Y', 'Z'})),
                         inventory.attest_frontier)
        contribution = AssignmentContribution.capture(
            dict(id='assignment', workItemId='item', state='closed'), inventory, 0)
        self.assertIs(contribution.attests.frontier, inventory.attest_frontier)
        self.assertEqual(3, contribution.attests.count)
        self.assertNotIn('X', contribution.attests.frontier.ids)

    def test_contract_older_equal_new_and_advance_decisions(self):
        frontier = AttestFrontier(2000, frozenset({'Y'}))
        self.assertTrue(frontier.includes(self.row('X', 1000)))
        self.assertTrue(frontier.includes(self.row('Y', 2000)))
        self.assertFalse(frontier.includes(self.row('Z', 2000)))
        frontier = frontier.observe(self.row('Z', 2000))
        self.assertTrue(frontier.includes(self.row('Z', 2000)))
        self.assertEqual(frozenset({'Y', 'Z'}), frontier.ids)
        frontier = frontier.observe(self.row('W', 3000))
        self.assertEqual(AttestFrontier(3000, frozenset({'W'})), frontier)
        self.assertTrue(frontier.includes(self.row('Z', 2000)))
        self.assertFalse(AttestFrontier().includes(self.row('first', 0)))
        self.assertEqual(AttestFrontier(0, frozenset({'first'})),
                         AttestFrontier().observe(self.row('first', 0)))

    def test_partial_read_preserves_counted_frontier_complete_retry_replaces(self):
        inventory = HistoryInventory('attests')
        initial = [self.row('X', 1000), self.row('Y', 2000)]
        self.read(inventory, initial)
        self.read(inventory, [self.row('W', 3000)], complete=False, at=1000100)
        self.assertFalse(inventory.complete)
        self.assertEqual(2, inventory.folded['count'])
        self.assertEqual(AttestFrontier(2000, frozenset({'Y'})),
                         inventory.attest_frontier)
        self.read(inventory, initial + [self.row('W', 3000)], at=1000200)
        self.assertTrue(inventory.complete)
        self.assertEqual(3, inventory.folded['count'])
        self.assertEqual(AttestFrontier(3000, frozenset({'W'})),
                         inventory.attest_frontier)
        self.read(inventory, [], at=1000300)
        self.assertEqual(AttestFrontier(), inventory.attest_frontier)
        self.assertEqual(0, inventory.folded['count'])
