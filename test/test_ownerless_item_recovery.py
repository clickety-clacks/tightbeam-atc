"""Owner resolution supplies activity, never an incomplete item contribution."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_payloads import ASSIGNMENT_FIELDS
from atc_upstream import Read
from publication_assertions import assert_publication_equal, assert_replay_current
from test_assignment_hold_publication import AssignmentHoldPublicationTest


class OwnerlessItemRecoveryTest(unittest.TestCase):
    setUp = AssignmentHoldPublicationTest.setUp
    settle = AssignmentHoldPublicationTest.settle
    fresh = AssignmentHoldPublicationTest.fresh
    item = AssignmentHoldPublicationTest.item
    notice = AssignmentHoldPublicationTest.notice
    assert_retained = AssignmentHoldPublicationTest.assert_retained
    assert_held_publication = AssignmentHoldPublicationTest.assert_held_publication

    def establish(self, *, unpublished=False):
        baseline = deepcopy(self.service.current)
        wid = 'new-item' if unpublished else 'work'
        if unpublished:
            row = dict(self.gateway.rows['work-items', None][0], id=wid, title='New')
            self.gateway.rows['work-items', None].append(row)
            self.service._refresh('work-items')
            self.service.reads.pump()
            kind, value = self.service.events.get(timeout=5)
            self.assertEqual('read', kind)
            self.service._read_done(value)  # no publication between this batch's sources
        row = dict.fromkeys(ASSIGNMENT_FIELDS) | dict(
            id='new-owner', workItemId=wid, holderKey='author', state='closed',
            openedAt=100, closedAt=200, effectKind='code', subject='New contribution')
        self.gateway.rows['assignments', None].append(row)
        attests = [dict(id='new-evidence', assignmentId=row['id'], kind='progress',
                        ts=950000, bySession='author', commitRefs=[])]
        self.gateway.rows['attests', row['id']] = attests
        self.gateway.calls.clear()
        epochs = dict(self.service.reads.epochs)
        notice = dict(resource='attests', op='upsert', **{'class': 'attest.created'},
                      refs={'attestId': 'new-evidence', 'assignmentId': row['id']},
                      payload=deepcopy(attests[0]))
        self.assertTrue(self.service.change(notice))
        self.assertTrue(self.service.change(notice))
        self.service.reads.pump()
        kind, value = self.service.events.get(timeout=5)
        self.assertEqual(('attest-owner', row['id'], None), value[0])
        self.assertEqual('read', kind)
        self.service._read_done(value)
        self.assertEqual([], value[2].rows)
        self.assertEqual([('detail', 'assignments', None, row['id'])], self.gateway.calls)
        self.assertEqual(epochs, self.service.reads.epochs)
        self.assertEqual({}, self.service.reads.pending)
        self.assertNotIn(row['id'], self.service.model.inventory('assignments').rows)
        self.assertNotIn(row['id'], self.service.model.item_contributions.entries)
        self.assertEqual(950000, self.service.model.session_activity['author'])
        self.assertEqual({}, self.service.attest_activity_pending)
        self.assertEqual(1, self.service.reads.attest_owner_attempts)
        self.service._publish()
        held = self.service.assignment_holds.assignments[row['id']]
        self.assertTrue(held.incomplete_contribution)
        self.assertFalse(held.conflict)
        if unpublished:
            self.assertIsNone(self.service.assignment_holds.items[wid])
            self.assertNotIn(wid, {value['id'] for value in self.service.current['items']})
        else:
            self.assert_retained(self.item(baseline), self.item(self.service.current), 1000)
        self.assertEqual(self.item(baseline, 'unaffected-item'),
                         self.item(self.service.current, 'unaffected-item'))
        differences, _fresh = self.assert_held_publication(baseline, 'r32-full-hold-' + wid)
        # This case retains no unresolved history scope. R31 is not a blanket
        # metadata allowance for a missing-contribution hold.
        self.assertEqual({'historyScopes': 0, 'active.scopeCount': 0}, differences)
        assert_replay_current(self, self.service, baseline, 'r32-owner-hold-' + wid)
        return baseline, row

    def test_lookup_and_incomplete_notice_do_not_start_item_read(self):
        for unpublished in (False, True):
            with self.subTest(unpublished=unpublished):
                if unpublished:
                    self.service.stop()
                    self.setUp()
                _baseline, row = self.establish(unpublished=unpublished)
                before = deepcopy(self.service.current)
                calls = list(self.gateway.calls)
                for incomplete in (row, dict(row, state='open', closedAt=None)):
                    self.assertTrue(self.service.change(self.notice(incomplete)))
                    self.service._publish()
                self.assertEqual(calls, self.gateway.calls)
                self.assertEqual({}, self.service.reads.pending)
                self.assertNotIn(row['id'], self.service.model.item_contributions.entries)
                assert_publication_equal(self, before, self.service.current,
                                         'r32-incomplete-notice-' + row['workItemId'])

    def test_existing_covering_read_resolves_published_and_absent_items(self):
        for unpublished in (False, True):
            with self.subTest(unpublished=unpublished):
                if unpublished:
                    self.service.stop()
                    self.setUp()
                baseline, row = self.establish(unpublished=unpublished)
                # Independent existing item work, not scheduled by the owner
                # result. Exercise the canonical complete-item consumer.
                self.service._request_item_assignments(row['workItemId'])
                self.settle()
                self.assertNotIn(row['id'], self.service.assignment_holds.assignments)
                self.assertEqual(1, self.gateway.calls.count(('membership', row['workItemId'])))
                assert_publication_equal(self, self.fresh().current, self.service.current,
                                         'r32-covering-read-' + row['workItemId'])
                assert_replay_current(self, self.service, baseline,
                                      'r32-covering-read-replay-' + row['workItemId'])

    def test_membership_and_partial_history_do_not_resolve_until_complete(self):
        baseline, row = self.establish()
        self.gateway.partial.add(('attests', row['id']))
        # This is the existing periodic source sequence. No per-item timer or
        # extra recovery read is introduced by the unresolved condition.
        self.service.rebuild()
        self.settle()
        self.assertTrue(self.service.assignment_holds.assignments[row['id']].incomplete_contribution)
        self.assert_retained(self.item(baseline), self.item(self.service.current), 1000)
        self.gateway.partial.clear()
        self.service._request_item_assignments(row['workItemId'])
        self.settle()
        self.assertNotIn(row['id'], self.service.assignment_holds.assignments)
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r32-partial-complete-recovery')

    def test_existing_rebuild_discovers_history_and_resolves_without_item_chain(self):
        baseline, row = self.establish()
        self.service.rebuild()
        self.settle()
        self.assertNotIn(row['id'], self.service.assignment_holds.assignments)
        self.assertNotIn(('membership', row['workItemId']), self.gateway.calls)
        self.assertIn(('read', 'attests', row['id']), self.gateway.calls)
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r32-scheduled-source-recovery')
        assert_replay_current(self, self.service, baseline, 'r32-scheduled-source-replay')

    def test_notice_with_existing_complete_history_resolves_without_read(self):
        for unpublished in (False, True):
            with self.subTest(unpublished=unpublished):
                if unpublished:
                    self.service.stop()
                    self.setUp()
                baseline, row = self.establish(unpublished=unpublished)
                # A previously scheduled complete history is already held at
                # the model source seam; the next full assignment notice can
                # now supply the complete contribution through its normal flow.
                self.service.model.accept('attests', row['id'], Read(
                    deepcopy(self.gateway.rows['attests', row['id']]), True, 10, 'published-read'))
                opened = dict(row, state='open', closedAt=None)
                self.gateway.rows['assignments', None][-1] = opened
                calls = list(self.gateway.calls)
                self.assertTrue(self.service.change(self.notice(opened)))
                self.service._publish()
                self.assertNotIn(row['id'], self.service.assignment_holds.assignments)
                self.assertEqual(calls, self.gateway.calls)
                self.assertEqual({}, self.service.reads.pending)
                assert_publication_equal(self, self.fresh().current, self.service.current,
                                         'r32-complete-notice-' + row['workItemId'])
                assert_replay_current(self, self.service, baseline,
                                      'r32-complete-notice-replay-' + row['workItemId'])

    def test_restart_uses_complete_fresh_facts_without_transferring_hold(self):
        for unpublished in (False, True):
            with self.subTest(unpublished=unpublished):
                if unpublished:
                    self.service.stop()
                    self.setUp()
                _baseline, row = self.establish(unpublished=unpublished)
                restarted = self.fresh()
                self.assertEqual({}, restarted.assignment_holds.assignments)
                self.assertEqual({}, restarted.assignment_holds.items)
                self.assertIn(row['workItemId'], {value['id'] for value in restarted.current['items']})
                self.assertEqual(950000, restarted.model.session_activity['author'])
