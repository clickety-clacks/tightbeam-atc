"""Current maximum IDs and reference groups preserve exact counted evidence."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_contributions import AttestFrontier, AssignmentContribution, ItemContributions
from atc_evidence import HistoryInventory
from atc_summary import HistoryFold
from atc_model import EvidenceModel
from atc_upstream import Read
from publication_assertions import assert_publication_equal
from test_service import work


class AttestFrontierTest(unittest.TestCase):
    @staticmethod
    def row(identity, timestamp, **values):
        return dict(id=identity, ts=timestamp, kind='progress', bySession='author') | values

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
        self.assertEqual(AttestFrontier(2000, (('Y', 0, 0), ('Z', 0, 0))),
                         inventory.attest_frontier)
        contribution = AssignmentContribution.capture(
            dict(id='assignment', workItemId='item', state='closed'), inventory, 0)
        self.assertIs(contribution.attests.frontier, inventory.attest_frontier)
        self.assertEqual(3, contribution.attests.count)
        self.assertNotIn('X', contribution.attests.frontier.ids)

    def test_contract_older_equal_new_and_advance_decisions(self):
        frontier = AttestFrontier(2000, (('Y', 0, 0),))
        self.assertTrue(frontier.includes(self.row('X', 1000)))
        self.assertTrue(frontier.includes(self.row('Y', 2000)))
        self.assertFalse(frontier.includes(self.row('Z', 2000)))
        frontier = frontier.observe(self.row('Z', 2000))
        self.assertTrue(frontier.includes(self.row('Z', 2000)))
        self.assertEqual(frozenset({'Y', 'Z'}), frontier.ids)
        frontier = frontier.observe(self.row('W', 3000))
        self.assertEqual(AttestFrontier(3000, (('W', 0, 0),)), frontier)
        self.assertTrue(frontier.includes(self.row('Z', 2000)))
        self.assertFalse(AttestFrontier().includes(self.row('first', 0)))
        self.assertEqual(AttestFrontier(0, (('first', 0, 0),)),
                         AttestFrontier().observe(self.row('first', 0)))

    def test_partial_read_preserves_counted_frontier_complete_retry_replaces(self):
        inventory = HistoryInventory('attests')
        initial = [self.row('X', 1000), self.row('Y', 2000)]
        self.read(inventory, initial)
        self.read(inventory, [self.row('W', 3000)], complete=False, at=1000100)
        self.assertFalse(inventory.complete)
        self.assertEqual(2, inventory.folded['count'])
        self.assertEqual(AttestFrontier(2000, (('Y', 0, 0),)),
                         inventory.attest_frontier)
        self.read(inventory, initial + [self.row('W', 3000)], at=1000200)
        self.assertTrue(inventory.complete)
        self.assertEqual(3, inventory.folded['count'])
        self.assertEqual(AttestFrontier(3000, (('W', 0, 0),)),
                         inventory.attest_frontier)
        self.read(inventory, [], at=1000300)
        self.assertEqual(AttestFrontier(), inventory.attest_frontier)
        self.assertEqual(0, inventory.folded['count'])

    def test_interposed_reference_groups_match_both_required_fresh_publications(self):
        def model(rows):
            value = EvidenceModel()
            value.accept('work-items', None, Read([work()], True, 1000000, 'fixture'))
            value.accept('sessions', None, Read([
                dict(sessionKey='author', state='active', displayName='Author')],
                True, 1000000, 'fixture'))
            value.accept('roles', None, Read([], True, 1000000, 'fixture'))
            value.accept('assignments', None, Read([dict(id='assignment', workItemId='work',
                holderKey='author', state='open', effectKind='code', openedAt=1)],
                True, 1000000, 'fixture'))
            for resource in ('turns', 'wakes', 'messages'):
                value.accept(resource, 'author', Read([], True, 1000000, 'fixture'))
            value.accept('attests', 'assignment', Read(rows, True, 1000000, 'fixture'))
            return value

        for first, last, expected in ((['A'], ['B'], ['A', 'C', 'B']),
                                      ([], ['A', 'B'], ['C', 'A', 'B'])):
            with self.subTest(first=first):
                def row(identity, refs):
                    return self.row(identity, 2000, assignmentId='assignment', kind='verdict',
                        verdictKind='verification-failed', commitRefs=[{'commit': ref} for ref in refs])
                initial, notice = [row('b', first), row('d', last)], row('c', ['C'])
                warm = model(initial)
                inventory = warm.inventory('attests', 'assignment')
                contribution = AssignmentContribution.capture(
                    warm.inventory('assignments').rows['assignment'], inventory, 0)
                self.assertEqual(['A', 'B'], [ref['commit'] for ref in inventory.folded['refs']])
                self.assertTrue(inventory.payload(notice, at=1000100, source='feed', policy='immutable'))
                warm.fold_activity_inventory('attests', 'assignment')
                fresh = model([initial[0], notice, initial[1]])
                assert_publication_equal(self, fresh.snapshot(1000200), warm.snapshot(1000200),
                                         'r26-interposed-' + ('nonempty' if first else 'empty'))
                self.assertEqual(expected, [ref['commit'] for ref in warm.snapshot(1000200)['items'][0]['commitRefs']])
                self.assertEqual(3, inventory.folded['count'])
                self.assertEqual(frozenset({'b', 'c', 'd'}), inventory.attest_frontier.ids)
                self.assertEqual({}, inventory.rows)
                updated = contribution.attests.notice(notice)
                self.assertEqual(inventory.attest_frontier, updated.frontier)
                self.assertEqual(expected, [ref['commit'] for ref in updated.refs])
                self.assertEqual(3, updated.count)
                self.assertIs(updated, updated.notice(notice))
                before = warm.snapshot(1000200)
                self.assertTrue(inventory.payload(notice, at=1000150, source='feed', policy='immutable'))
                self.assertEqual(before, warm.snapshot(1000200))

    def test_frontier_group_reset_retains_prefix_and_releases_old_boundaries(self):
        inventory = HistoryInventory('attests')
        self.read(inventory, [self.row('old', 1000, commitRefs=[{'commit': 'prefix'}]),
                              self.row('b', 2000, commitRefs=[]),
                              self.row('d', 2000, commitRefs=[{'commit': 'A'}, {'commit': 'B'}])])
        self.assertEqual((('b', 1, 0), ('d', 1, 2)), inventory.attest_frontier.groups)
        for identity, timestamp, refs in [('c', 2000, ['C']), ('w', 3000, []),
                                           ('v', 3000, ['V']), ('z', 3000, ['Z'])]:
            self.assertTrue(inventory.payload(self.row(identity, timestamp,
                commitRefs=[{'commit': ref} for ref in refs]), at=1000100,
                source='feed', policy='immutable'))
        self.assertEqual(['prefix', 'C', 'A', 'B', 'V', 'Z'],
                         [ref['commit'] for ref in inventory.folded['refs']])
        self.assertEqual((('v', 4, 1), ('w', 5, 0), ('z', 5, 1)), inventory.attest_frontier.groups)
        self.assertEqual(7, inventory.folded['count'])
        self.assertFalse(inventory.attest_frontier.ids & {'old', 'b', 'c', 'd'})
        self.read(inventory, [], at=1000200)
        self.assertEqual((), inventory.attest_frontier.groups)
        self.assertEqual([], inventory.folded['refs'])

    def test_partial_raw_overlap_counts_once_and_advances_fold_revision(self):
        inventory = HistoryInventory('attests')
        first = self.row('b', 1000000, commitRefs=[{'commit': 'B'}])
        later = self.row('c', 1000100, commitRefs=[{'commit': 'C'}])
        self.read(inventory, [first])
        self.read(inventory, [first, later], complete=False, at=1000200)
        self.assertIn('c', inventory.rows)
        self.assertEqual(1, inventory.folded['count'])
        revision = inventory.data_revision
        self.assertTrue(inventory.payload(later, at=1000300, source='feed', policy='immutable'))
        self.assertGreater(inventory.data_revision, revision)
        self.assertFalse(inventory.complete)
        self.assertEqual(2, inventory.folded['count'])
        self.assertEqual([{'commit': 'B'}, {'commit': 'C'}], inventory.folded['refs'])
        revision = inventory.data_revision
        self.assertTrue(inventory.payload(later, at=1000400, source='feed', policy='immutable'))
        self.assertEqual(revision, inventory.data_revision)
        self.read(inventory, [first, later], at=1000500)
        self.assertTrue(inventory.complete)
        self.assertEqual(2, inventory.folded['count'])

    def test_contribution_and_item_exit_release_frontier_groups_only_when_complete(self):
        inventory = HistoryInventory('attests')
        self.read(inventory, [self.row('b', 2000, commitRefs=[{'commit': 'B'}])])
        contribution = AssignmentContribution.capture(
            dict(id='assignment', workItemId='item', state='closed'), inventory, 0)
        store = ItemContributions()
        store.replace_item('item', {'assignment': contribution}, complete=True)
        self.assertFalse(store.replace_item('item', {}, complete=False))
        self.assertEqual((('b', 0, 1),), store.entries['assignment'].attests.frontier.groups)
        store.replace_item('item', {}, complete=True)
        self.assertEqual({}, store.entries)
        store.replace_item('item', {'assignment': contribution}, complete=True)
        store.retain_items(set())
        self.assertEqual({}, store.entries)
