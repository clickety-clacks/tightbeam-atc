"""Current provenance retains published facts and releases absent contributors."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_contributions import AssignmentContribution, ItemContributions
from atc_model import ClosedAssignmentFold
from atc_upstream import Read
from test_closed_folding import ClosedAssignmentFoldTest


class ItemContributionsTest(unittest.TestCase):
    def entries(self):
        model = ClosedAssignmentFoldTest().model()
        return {key: AssignmentContribution.capture(row, model.inventory('attests', key), index)
                for index, (key, row) in enumerate(model.inventory('assignments').rows.items())}

    def test_partial_cannot_erase_other_contributors_complete_and_item_exit_release(self):
        entries = self.entries()
        store = ItemContributions()
        self.assertTrue(store.replace_item('work', entries, complete=True))
        before = dict(store.entries)
        self.assertFalse(store.replace_item('work', {'first': entries['first']}, complete=False))
        self.assertEqual(before, store.entries)
        self.assertTrue(store.replace_item('work', {'first': entries['first']}, complete=True))
        self.assertEqual({'first'}, set(store.entries))
        store.retain_items(set())
        self.assertEqual({}, store.entries)

    def test_projection_preserves_counts_authors_refs_corrections_without_source_text(self):
        entries = self.entries()
        first, last = entries['first'], entries['last']
        self.assertIsInstance(first, tuple)
        self.assertFalse(hasattr(first, '__dict__'))
        self.assertEqual(1, first.attests.count)
        self.assertEqual((('author', 10),), first.attests.authors)
        self.assertEqual(({'commit': 'first'},), first.attests.refs)
        self.assertEqual(({'commitRefs': [{'commit': 'corrected'}], 'id': 'correction'},),
                         last.corrections)
        self.assertNotIn('discard', repr(entries))
        self.assertEqual(60, last.closed_at)
        self.assertEqual('other', last.holder)

    def test_reduction_keeps_order_corrections_and_authors_after_contributor_removal(self):
        entries = self.entries()
        store = ItemContributions()
        store.replace_item('work', entries, complete=True)

        def reduced():
            fold = ClosedAssignmentFold()
            # Iteration order must not change the published reference order.
            for value in reversed(tuple(store.entries.values())):
                if value.state != 'open':
                    fold.add_contribution(value)
            return fold.part

        part = reduced()
        self.assertEqual(2, part['count'])
        self.assertEqual(60, part['closedAt'])
        self.assertEqual(2, part['summary']['count'])
        self.assertEqual({'progress': 2}, part['summary']['kinds'])
        self.assertEqual({'author': 10, 'other': 30}, part['summary']['authors'])
        self.assertEqual([{'commit': 'first'}, {'commit': 'corrected'}],
                         [ref for _, _, ref in sorted(part['refs'], key=lambda entry: entry[:2])])
        self.assertEqual([(2, 0, {'commitRefs': [{'commit': 'corrected'}], 'id': 'correction'})],
                         part['corrections'])
        self.assertFalse(part['detailsKnown'])
        # Partial omission preserves the other author's timing and correction.
        store.replace_item('work', {'first': entries['first']}, complete=False)
        self.assertEqual(part['corrections'], reduced()['corrections'])
        self.assertEqual(part['summary']['authors'], reduced()['summary']['authors'])
        # Complete removal releases those contributions; it is not a max-only merge.
        store.replace_item('work', {'first': entries['first']}, complete=True)
        remaining = reduced()
        self.assertEqual(1, remaining['count'])
        self.assertEqual(50, remaining['closedAt'])
        self.assertEqual({'author': 10}, remaining['summary']['authors'])
        self.assertEqual([], remaining['corrections'])

    def test_model_disposal_preserves_contributions_and_item_exit_drops_them(self):
        model = ClosedAssignmentFoldTest().model()
        before = model.snapshot(1000)
        model.fold_closed_assignments()
        self.assertEqual({'first', 'open', 'last'}, set(model.item_contributions.entries))
        self.assertEqual({'open'}, set(model.inventory('assignments').rows))
        self.assertEqual(before, model.snapshot(1000))
        previous = dict(model.item_contributions.entries)
        model.accept('assignments', None, Read([], False, 1100, 'partial'))
        self.assertEqual(previous, model.item_contributions.entries)
        model.accept('work-items', None, Read([], True, 1200, 'complete'))
        self.assertEqual({}, model.item_contributions.entries)

    def test_membership_refresh_cannot_erase_released_attests_and_partial_retry_recovers(self):
        model = ClosedAssignmentFoldTest().model()
        assignment_rows = list(model.inventory('assignments').rows.values())
        source_attests = list(model.inventory('attests', 'first').rows.values())
        model.fold_closed_assignments()
        prior = model.item_contributions.entries['first']
        model.accept('assignments', None, Read(assignment_rows, True, 1100, 'membership'))
        model.capture_assignment_contributions()
        current = model.item_contributions.entries['first']
        self.assertEqual(prior.attests, current.attests)
        self.assertEqual(prior.evidence, current.evidence)
        self.assertEqual(prior.detail_fingerprint, current.detail_fingerprint)

        model.accept('attests', 'first', Read([], False, 1200, 'partial', 'unavailable'))
        model.capture_assignment_contributions()
        incomplete = model.item_contributions.entries['first']
        self.assertEqual(prior.attests, incomplete.attests)
        self.assertFalse(incomplete.evidence.complete)
        self.assertEqual('stale', incomplete.evidence.freshness)

        model.accept('attests', 'first', Read(source_attests, True, 1300, 'confirmed'))
        model.capture_assignment_contributions()
        recovered = model.item_contributions.entries['first']
        self.assertEqual(prior.attests, recovered.attests)
        self.assertTrue(recovered.evidence.complete)
        self.assertEqual('live', recovered.evidence.freshness)
        self.assertEqual(1300, recovered.evidence.observed_at)

    def test_complete_membership_reorders_existing_open_and_closed_contributions(self):
        model = ClosedAssignmentFoldTest().model()
        original_rows = list(model.inventory('assignments').rows.values())
        model.fold_closed_assignments()
        model.capture_assignment_contributions()
        self.assertEqual(1, model.item_contributions.entries['open'].order)
        inserted = dict(id='new-first', workItemId='work', holderKey='author',
                        state='open', effectKind='code', openedAt=200)
        # The complete source is authoritative for order; the former open-row
        # position cannot survive insertion of a preceding assignment.
        model.accept('assignments', None,
                     Read([inserted, *original_rows], True, 1100, 'membership'))
        model.capture_assignment_contributions()
        self.assertEqual({'new-first': 0, 'first': 1, 'open': 2, 'last': 3},
                         {key: value.order for key, value in
                          model.item_contributions.entries.items()})

    def test_partial_membership_and_released_subset_do_not_renumber_contributors(self):
        model = ClosedAssignmentFoldTest().model()
        last = model.inventory('assignments').rows['last']
        model.fold_closed_assignments()
        prior = {key: value.order for key, value in model.item_contributions.entries.items()}
        model.capture_assignment_contributions()
        self.assertEqual(prior, {key: value.order for key, value in
                                model.item_contributions.entries.items()})
        model.accept('assignments', None, Read([last], False, 1100, 'partial'))
        model.capture_assignment_contributions()
        self.assertEqual(prior, {key: value.order for key, value in
                                model.item_contributions.entries.items()})

    def test_published_older_item_keeps_contributors_until_complete_item_exit(self):
        model = ClosedAssignmentFoldTest().model()
        item = dict(model.work_items()['work'], state='closed')
        model.accept('work-items', None, Read([item], True, 1100, 'complete'))
        model.fold_closed_assignments()
        prior = dict(model.item_contributions.entries)
        before = model.snapshot(1200)
        model.fold_older_work_items()
        self.assertNotIn('work', model.inventory('work-items').rows)
        model.release_auxiliary_sources()
        self.assertEqual(prior, model.item_contributions.entries)
        self.assertEqual(before, model.snapshot(1200))
        model.accept('work-items', None, Read([], False, 1300, 'partial'))
        model.release_auxiliary_sources()
        self.assertEqual(prior, model.item_contributions.entries)
        model.accept('work-items', None, Read([], True, 1400, 'complete'))
        self.assertEqual({}, model.item_contributions.entries)
