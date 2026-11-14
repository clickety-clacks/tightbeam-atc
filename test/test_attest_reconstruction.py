"""Membership reconstruction retains facts without canceling scoped discovery."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_contributions import AssignmentContribution
from atc_evidence import HistoryInventory
from atc_summary import HistoryFold
from atc_upstream import Read
from test_closed_folding import ClosedAssignmentFoldTest
from publication_assertions import assert_publication_equal


class AttestReconstructionTest(unittest.TestCase):
    def contribution(self, rows):
        inventory = HistoryInventory('attests')
        fold = HistoryFold('attests', 100, ())
        fold.add(rows)
        inventory.read_fold(fold, complete=True, at=100, source='cli:attests first')
        return AssignmentContribution.capture(dict(id='first', workItemId='work',
            state='closed', closedAt=50), inventory, 0)

    def test_retained_empty_and_nonempty_folds_do_not_complete_discovery(self):
        for rows in ([], [dict(id='old', kind='progress', ts=10, bySession='author',
                              commitRefs=[{'commit': 'old'}])]):
            with self.subTest(empty=not rows):
                contribution = self.contribution(rows)
                inventory = HistoryInventory('attests')
                self.assertTrue(inventory.restore_attest_contribution(contribution))
                self.assertTrue(inventory.needs_source_read)
                self.assertEqual(contribution.evidence.publication(), inventory.evidence())
                self.assertEqual(contribution.detail_fingerprint, inventory.fingerprint)
                live = dict(id='live', kind='progress', ts=200, bySession='author', commitRefs=[])
                self.assertTrue(inventory.payload(live, at=201, source='feed:attest.filed',
                                                  policy='immutable'))
                self.assertTrue(inventory.needs_source_read)
                self.assertEqual(len(rows) + 1, inventory.folded['count'])
                partial = HistoryFold('attests', 202, ())
                inventory.read_fold(partial, complete=False, at=202, source='cli:attests first')
                self.assertFalse(inventory.needs_source_read)
                self.assertFalse(inventory.complete)
                self.assertTrue(inventory.stale)
                self.assertEqual(len(rows) + 1, inventory.folded['count'])
                self.assertEqual(200, inventory.folded['authors']['author'])
                # A failed read is now owned by the existing retry path, not
                # discovery. Restoring older provenance cannot hide its failure.
                held = deepcopy(inventory.__dict__)
                self.assertFalse(inventory.restore_attest_contribution(contribution))
                self.assertEqual(held, inventory.__dict__)
                complete = HistoryFold('attests', 203, ())
                complete.add([*rows, live])
                inventory.read_fold(complete, complete=True, at=203, source='cli:attests first')
                self.assertEqual(len(rows) + 1, inventory.folded['count'])
                self.assertTrue(inventory.complete)
                self.assertFalse(inventory.stale)
                self.assertFalse(inventory.needs_source_read)

    def test_first_partial_read_cannot_erase_a_pre_read_live_notice(self):
        inventory = HistoryInventory('attests')
        inventory.payload(dict(id='live', kind='progress', ts=200, bySession='author'),
                          at=201, source='feed:attest.filed', policy='immutable')
        inventory.read_fold(HistoryFold('attests', 202, ()), complete=False,
                            at=202, source='cli:attests first')
        self.assertEqual(1, inventory.folded['count'])
        self.assertEqual(200, inventory.folded['authors']['author'])
        self.assertFalse(inventory.complete)

    def test_global_membership_restores_only_final_construction_scopes(self):
        for protected in ((), ('first',)):
            with self.subTest(protected=protected):
                model = ClosedAssignmentFoldTest().model()
                rows = deepcopy(list(model.inventory('assignments').rows.values()))
                model.fold_closed_assignments()
                before = model.snapshot(1000)
                model.accept_assignment_read_preserving(Read(rows, True, 100, 'fixture'), protected)
                model.restore_assignment_attests({'work'})
                assert_publication_equal(self, before, model.snapshot(1000),
                    'r29-reconstructed-membership-' + str(bool(protected)))
                self.assertFalse(model.inventory('attests', 'open').needs_source_read)
                for aid in ('first', 'last'):
                    if aid in protected:
                        self.assertNotIn(('attests', aid), model.inventories)
                    else:
                        inventory = model.inventory('attests', aid)
                        self.assertTrue(inventory.needs_source_read)
                        self.assertEqual(1, inventory.folded['count'])

    def test_excluded_source_is_not_restored_from_old_provenance(self):
        model = ClosedAssignmentFoldTest().model()
        rows = deepcopy(list(model.inventory('assignments').rows.values()))
        model.fold_closed_assignments()
        model.accept_assignment_read_preserving(Read(rows, True, 100, 'fixture'), ())
        model.restore_assignment_attests(set())
        self.assertNotIn(('attests', 'first'), model.inventories)

