"""Selective complete-source replacement without choosing conflicted rows."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_upstream import Read
from test_closed_folding import ClosedAssignmentFoldTest
from publication_assertions import assert_publication_equal


class AssignmentConflictSourceTest(unittest.TestCase):
    def models(self, *, with_other_item=False):
        reference = ClosedAssignmentFoldTest().model()
        if with_other_item:
            items = deepcopy(list(reference.inventory('work-items').rows.values()))
            items.append(dict(items[0], id='other-item', title='Independent item'))
            reference.accept('work-items', None, Read(items, True, 100, 'fixture'))
            assignments = deepcopy(list(reference.inventory('assignments').rows.values()))
            assignments.append(dict(id='independent', workItemId='other-item',
                                    holderKey='other', state='closed', effectKind='spec',
                                    openedAt=1, closedAt=40))
            reference.accept('assignments', None, Read(assignments, True, 100, 'fixture'))
            reference.accept('attests', 'independent', Read([], True, 100, 'fixture'))
        current = deepcopy(reference)
        current.fold_closed_assignments()
        return current, reference

    def replace(self, current, reference, incoming, protected, label):
        # The expected source contains the established projection only for
        # explicitly conflicted assignments. It is not an upstream warm/fresh
        # equality claim during R29's temporary item-publication exception.
        old_rows = reference.inventory('assignments').rows
        expected_rows = [deepcopy(old_rows[row['id']] if row['id'] in protected else row)
                         for row in incoming]
        saved_input = deepcopy(incoming)
        current.accept_assignment_read_preserving(Read(incoming, True, 100, 'fixture'), protected)
        reference.accept('assignments', None, Read(expected_rows, True, 100, 'fixture'))
        self.assertEqual(saved_input, incoming)
        # Supply the same existing complete attest inventories for the other
        # members. This model fixture does not simulate transport or restore
        # history from rows that HistoryInventory has already discarded.
        # The protected closed member remains compact without a raw rebuild.
        for row in incoming:
            aid = row['id']
            if aid in protected:
                continue
            source = reference.inventory('attests', aid)
            current.inventories['attests', aid] = deepcopy(source)
            current.summaries.pop(('attests', aid), None)
        current.fold_closed_assignments()
        reference.fold_closed_assignments()
        assert_publication_equal(self, reference.snapshot(1000), current.snapshot(1000), label)

    def test_closed_conflict_keeps_its_contribution_and_accepts_other_changed_rows(self):
        current, reference = self.models()
        before = current.item_contributions.entries['first']
        pin = current.closed_items['work']['detailFingerprint']
        incoming = deepcopy(list(reference.inventory('assignments').rows.values()))
        incoming[0].update(state='open', closedAt=None)
        incoming[-1].update(closedAt=600, commitRefCorrections=[{
            'id': 'new-correction', 'commitRefs': [{'commit': 'new-last'}]}])
        self.replace(current, reference, incoming, {'first'}, 'r29-selective-closed-source')
        self.assertNotIn('first', current.inventory('assignments').rows)
        self.assertEqual(before, current.item_contributions.entries['first'])
        self.assertEqual(600, current.item_contributions.entries['last'].closed_at)
        self.assertEqual('new-correction', current.item_contributions.entries['last'].corrections[0]['id'])
        self.assertEqual('work', current.live_assignment_items['first'])
        self.assertEqual(pin, current.closed_items['work']['detailFingerprint'])
        self.assertFalse(current.closed_items['work']['detailsKnown'])

    def test_open_conflict_does_not_prevent_complete_absence_of_another_member(self):
        current, reference = self.models()
        opened = deepcopy(current.inventory('assignments').rows['open'])
        incoming = [deepcopy(row) for aid, row in reference.inventory('assignments').rows.items()
                    if aid != 'first']
        incoming[0].update(state='closed', closedAt=600)
        self.replace(current, reference, incoming, {'open'}, 'r29-selective-open-and-absence')
        self.assertEqual(opened, current.inventory('assignments').rows['open'])
        self.assertNotIn('first', current.item_contributions.entries)
        self.assertNotIn('first', current.live_assignment_items)
        self.assertEqual({'last'}, {aid for aid, value in current.item_contributions.entries.items()
                                   if value.state != 'open'})

    def test_conflicting_closed_member_survives_when_every_other_member_reopens(self):
        current, reference = self.models()
        incoming = deepcopy(list(reference.inventory('assignments').rows.values()))
        for row in incoming:
            row.update(state='open', closedAt=None)
        self.replace(current, reference, incoming, {'first'}, 'r29-selective-last-closed-member')
        self.assertEqual(1, current.closed_items['work']['count'])
        self.assertEqual(50, current.closed_items['work']['closedAt'])
        self.assertEqual({'open', 'last'}, set(current.inventory('assignments').rows))
        self.assertEqual({'first', 'open', 'last'}, set(current.item_contributions.entries))

    def test_held_member_does_not_retain_removed_members_turn_maximum(self):
        current, reference = self.models()
        incoming = [deepcopy(row) for aid, row in reference.inventory('assignments').rows.items()
                    if aid != 'last']
        incoming[0].update(state='open', closedAt=None)
        self.replace(current, reference, incoming, {'first'}, 'held-member-removed-turn-winner')
        self.assertNotIn('last', current.item_contributions.entries)
        self.assertEqual(80, current.closed_items['work']['turnActivity'])
        self.assertEqual(80, current.item_activity()['work'])

    def test_conflict_does_not_freeze_another_items_complete_replacement(self):
        current, reference = self.models(with_other_item=True)
        incoming = deepcopy(list(reference.inventory('assignments').rows.values()))
        for row in incoming:
            if row['id'] == 'first':
                row.update(state='open', closedAt=None)
            elif row['id'] == 'independent':
                row.update(closedAt=777)
        self.replace(current, reference, incoming, {'first'}, 'r29-unaffected-item-source')
        self.assertEqual(777, current.closed_items['other-item']['closedAt'])
        self.assertEqual(50, current.item_contributions.entries['first'].closed_at)

    def test_partial_or_absent_comparison_cannot_mutate_current_source(self):
        for complete, rows, protected in (
                (False, [{'id': 'first', 'state': 'open'}], {'first'}),
                (True, [], {'first'})):
            with self.subTest(complete=complete, rows=rows):
                current, _ = self.models()
                before = deepcopy(current.snapshot(1000))
                contributions = deepcopy(current.item_contributions.entries)
                with self.assertRaises(ValueError):
                    current.accept_assignment_read_preserving(
                        Read(rows, complete, 100, 'fixture'), protected)
                self.assertEqual(before, current.snapshot(1000))
                self.assertEqual(contributions, current.item_contributions.entries)

    def test_first_discovery_retains_actual_complete_source_for_later_resolution(self):
        current, reference = self.models()
        incoming = deepcopy(list(reference.inventory('assignments').rows.values()))
        discovered = dict(incoming[0], id='discovered', state='open', closedAt=None)
        incoming.append(discovered)
        original = deepcopy(incoming)
        current.accept_assignment_read_preserving(
            Read(incoming, True, 100, 'fixture'), {'discovered'})
        # Publication retention belongs to the service. This seam does not
        # fabricate an old row, choose a conflicting notice, or lose the
        # actual source facts needed by a later resolving notice.
        self.assertEqual(discovered, current.inventory('assignments').rows['discovered'])
        self.assertEqual(original, incoming)
        self.assertTrue(current.inventory('assignments').complete)

    def test_clean_empty_complete_read_keeps_existing_absence_semantics(self):
        current, _ = self.models()
        reference = deepcopy(current)
        current.accept_assignment_read_preserving(Read([], True, 100, 'fixture'), set())
        reference.accept('assignments', None, Read([], True, 100, 'fixture'))
        self.assertEqual({}, current.item_contributions.entries)
        self.assertEqual({}, current.closed_items)
        assert_publication_equal(self, reference.snapshot(1000), current.snapshot(1000),
                                 'r29-clean-empty-source')

    def test_complete_snapshot_preserves_delivered_creation_open_or_already_closed(self):
        for state in ('open', 'closed'):
            with self.subTest(state=state):
                current, reference = self.models()
                incoming = deepcopy(list(reference.inventory('assignments').rows.values()))
                new = dict(id='new', workItemId='work', holderKey='author', state='open',
                           effectKind='code', openedAt=40, closedAt=None)
                current.apply_assignment_projection(new, at=100, source='feed')
                reference.detail('assignments', None, Read([new], True, 100, 'feed'))
                attest = dict(id='new-attest', assignmentId='new', kind='progress',
                              bySession='author', ts=500, commitRefs=[{'commit': 'new'}])
                for model in (current, reference):
                    model.accept('attests', 'new', Read([attest], True, 100, 'fixture'))
                current.capture_assignment_contributions()
                if state == 'closed':
                    new.update(state='closed', closedAt=600)
                    current.apply_assignment_projection(new, at=100, source='feed')
                reference.accept('assignments', None,
                    Read([deepcopy(new), *deepcopy(incoming)], True, 100, 'fixture'))
                current.accept_assignment_read_preserving(
                    Read(incoming, True, 100, 'fixture'), set(), arrivals={'new'})
                for row in incoming:
                    aid = row['id']
                    current.inventories['attests', aid] = deepcopy(reference.inventory('attests', aid))
                    current.summaries.pop(('attests', aid), None)
                for model in (current, reference):
                    model.fold_closed_assignments()
                self.assertEqual(state, current.item_contributions.entries['new'].state)
                self.assertEqual(1, current.item_contributions.entries['new'].attests.count)
                self.assertEqual(0, current.item_contributions.entries['new'].order)
                self.assertEqual({'first', 'open', 'last'}, {row['id'] for row in incoming})
                assert_publication_equal(self, reference.snapshot(1000), current.snapshot(1000),
                                         'r29-global-created-after-snapshot-' + state)

    def test_creation_preservation_cannot_replace_a_present_or_unknown_member(self):
        for incoming, arrivals in (([{'id': 'first'}], {'first'}), ([], {'unknown'})):
            current, _ = self.models()
            before = deepcopy(current.snapshot(1000))
            with self.assertRaises(ValueError):
                current.accept_assignment_read_preserving(
                    Read(incoming, True, 100, 'fixture'), set(), arrivals=arrivals)
            self.assertEqual(before, current.snapshot(1000))
