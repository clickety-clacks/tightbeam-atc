"""Current provenance retains published facts and releases absent contributors."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_contributions import AssignmentContribution, ItemContributions
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
        self.assertEqual(({'commitRefs': [{'commit': 'corrected'}]},), last.corrections)
        self.assertNotIn('discard', repr(entries))
        self.assertEqual(60, last.closed_at)
        self.assertEqual('other', last.holder)
