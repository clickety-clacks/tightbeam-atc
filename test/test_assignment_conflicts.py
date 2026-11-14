"""R29 equality conflicts and current unresolved publication ownership."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_assignment_conflicts import AssignmentReadConflicts, AssignmentHolds
from atc_payloads import ASSIGNMENT_FIELDS


class AssignmentConflictsTest(unittest.TestCase):
    def row(self, state='open', **changes):
        row = dict.fromkeys(ASSIGNMENT_FIELDS)
        row.update(id='a', workItemId='item', openedAt=100, state=state,
                   closedAt=200 if state == 'closed' else None, files=[],
                   subject='not retained as a source row')
        row.update(changes)
        return row

    def notice(self, row):
        return dict(resource='assignments', op='upsert',
                    **{'class': 'assignment.closed' if row['state'] == 'closed'
                       else 'assignment.reopened'},
                    refs={'assignmentId': row['id']}, payload=row)

    def test_ambiguous_projection_conflicts_without_timestamp_precedence(self):
        flight = AssignmentReadConflicts()
        opened = self.row(rowVersion=100)
        closed = self.row('closed', rowVersion=200)
        self.assertTrue(flight.observe(self.notice(closed)))
        before = deepcopy(opened)
        self.assertEqual({'a'}, flight.conflicting_rows([opened], complete=True))
        self.assertEqual(before, opened)
        # These inputs are shared by before-close and after-reopen histories.
        # Complete service sequences must exercise both application orders.
        self.assertNotIn('not retained', repr(flight.notices))

    def test_same_shared_projection_is_clean_without_feed_only_fields(self):
        flight = AssignmentReadConflicts()
        row = self.row('closed', derivedStatus='abandoned', rowVersion=200)
        flight.observe(self.notice(row))
        snapshot = {key: value for key, value in row.items()
                    if key not in {'derivedStatus', 'rowVersion'}}
        snapshot['commitRefCorrections'] = [{'id': 'correction'}]
        self.assertEqual(set(), flight.conflicting_rows([snapshot], complete=True))

    def test_reopen_does_not_erase_an_earlier_conflict_in_the_same_flight(self):
        flight = AssignmentReadConflicts()
        for row in (self.row('closed'), self.row(), self.row('closed'), self.row()):
            flight.observe(self.notice(row))
        self.assertEqual(1, len(flight.notices))
        self.assertEqual({'a'}, flight.conflicting_rows([self.row()], complete=True))
        self.assertEqual({'a'}, flight.conflicting_rows([self.row('closed')], complete=True))
        self.assertIsNone(flight.conflicting_rows([self.row()], complete=False))
        self.assertEqual(set(), flight.conflicting_rows([self.row(id='unaffected')], complete=True))
        flight.discard()
        self.assertEqual({}, flight.notices)
        self.assertFalse(flight.observe(self.notice(self.row())))
        with self.assertRaises(ValueError):
            flight.conflicting_rows([], complete=True)

    def test_unsupported_or_mismatched_notice_cannot_certify_a_projection(self):
        flight = AssignmentReadConflicts()
        malformed = self.notice(self.row())
        malformed['refs']['assignmentId'] = 'different'
        self.assertFalse(flight.observe(malformed))
        missing = self.notice({'id': 'a', 'state': 'closed'})
        self.assertFalse(flight.observe(missing))
        opened = self.notice(self.row())
        opened['class'] = 'assignment.opened'
        self.assertFalse(flight.observe(opened))
        self.assertEqual({}, flight.notices)

    def test_hold_uses_last_publication_and_resolves_assignments_individually(self):
        holds = AssignmentHolds()
        published = {'id': 'item', 'closedAt': 200, 'evidence': {'assignments': {'complete': True}}}
        later = {'id': 'item', 'closedAt': None}
        untouched = deepcopy(published)
        self.assertTrue(holds.hold('a', 'item', published, at=300))
        self.assertFalse(holds.hold('a', 'item', later, at=400))
        self.assertTrue(holds.hold('b', 'item', later, at=500))
        self.assertIs(published, holds.items['item'])
        self.assertEqual(300, holds.since('item'))
        self.assertTrue(holds.resolve('a'))
        self.assertIs(published, holds.items['item'])
        self.assertEqual(500, holds.since('item'))
        self.assertTrue(holds.resolve('b'))
        self.assertEqual({}, holds.items)
        self.assertEqual({}, holds.assignments)
        self.assertEqual(untouched, published)

    def test_scope_exit_and_restart_keep_no_completed_hold_ledger(self):
        holds = AssignmentHolds()
        holds.hold('a', 'one', {'id': 'one'}, at=100)
        holds.hold('b', 'two', {'id': 'two'}, at=200)
        holds.retain_items({'two'})
        self.assertEqual({'b'}, set(holds.assignments))
        self.assertEqual({'two'}, set(holds.items))
        restarted = AssignmentHolds()
        self.assertEqual({}, restarted.assignments)
        self.assertEqual({}, restarted.items)
        with self.assertRaises(ValueError):
            holds.hold('new', 'unknown', None, at=300)
