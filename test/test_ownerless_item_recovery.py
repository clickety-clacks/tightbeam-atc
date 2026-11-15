"""Owner resolution supplies activity, never an incomplete item contribution."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_payloads import ASSIGNMENT_FIELDS
from atc_service import LiveService
from atc_upstream import ATTEST_FIELDS, Read
from publication_assertions import (assert_publication_equal, assert_replay_current,
                                    assert_winner_exit_publication)
from test_assignment_hold_publication import AssignmentHoldPublicationTest
from test_item_assignment_retry import ItemReads
from test_service import summarized_work


class OwnerlessItemRecoveryTest(unittest.TestCase):
    setUp = AssignmentHoldPublicationTest.setUp
    bootstrap = AssignmentHoldPublicationTest.bootstrap
    settle = AssignmentHoldPublicationTest.settle
    item = AssignmentHoldPublicationTest.item
    notice = AssignmentHoldPublicationTest.notice
    assert_retained = AssignmentHoldPublicationTest.assert_retained
    assert_held_publication = AssignmentHoldPublicationTest.assert_held_publication
    queued_source = AssignmentHoldPublicationTest.queued_source
    set_source_assignment = AssignmentHoldPublicationTest.set_source_assignment

    def fresh(self):
        # R7/R32 matched source inputs; custodian agreement art_85f110ed.
        # The implicit-summary case intentionally has no advertised version.
        gateway = ItemReads()
        gateway.rows = deepcopy(self.gateway.rows)
        service = LiveService(gateway, workers=1, scope_budget=self.service.scope_budget,
                              rebuild_interval=self.service.rebuild_interval)
        self.addCleanup(service.stop)
        service._handle('ready', deepcopy(self.service.version))
        self.settle(service)
        self.assertEqual(self.service.version, service.version)
        return service

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
        notice = dict(resource='attests', op='upsert', **{'class': 'attest.filed'},
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
        # This case retains no unresolved history scope. R34 is not a blanket
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
                baseline, row = self.establish(unpublished=unpublished)
                before = deepcopy(self.service.current)
                calls = list(self.gateway.calls)
                for incomplete in (row, dict(row, state='open', closedAt=None)):
                    self.set_source_assignment(incomplete)
                    self.assertTrue(self.service.change(self.notice(incomplete)))
                    self.assertEqual(incomplete,
                        self.service.model.inventory('assignments').rows[row['id']])
                    self.service._publish()
                    self.assertTrue(self.service.assignment_holds.assignments[row['id']].incomplete_contribution)
                    self.assert_held_publication(baseline,
                        'r32-incomplete-notice-' + row['workItemId'] + '-' + incomplete['state'])
                self.assertEqual(calls, self.gateway.calls)
                self.assertEqual({}, self.service.reads.pending)
                # Membership can now contribute internally, but cannot claim
                # complete attest evidence or release the published-item hold.
                contribution = self.service.model.item_contributions.entries.get(row['id'])
                if contribution is not None:
                    self.assertFalse(contribution.evidence.complete)
                assert_replay_current(self, self.service, before,
                                      'r32-incomplete-notice-replay-' + row['workItemId'])

    def test_incomplete_next_assignment_notice_resolves_only_conflict_cause(self):
        baseline, row = self.establish()
        self.service.assignment_holds.hold(row['id'], row['workItemId'],
            self.item(baseline), at=1000,
            after_delivery=self.service.assignment_delivery_consumed)
        self.assertTrue(self.service.assignment_holds.assignments[row['id']].conflict)
        opened = dict(row, state='open', closedAt=None)
        self.set_source_assignment(opened)
        calls = list(self.gateway.calls)
        self.assertTrue(self.service.change(self.notice(opened)))
        held = self.service.assignment_holds.assignments[row['id']]
        self.assertFalse(held.conflict)
        self.assertTrue(held.incomplete_contribution)
        self.assertEqual(opened, self.service.model.inventory('assignments').rows[row['id']])
        self.service._publish()
        self.assertEqual(calls, self.gateway.calls)
        self.assertEqual({}, self.service.reads.pending)
        self.assert_held_publication(baseline, 'r32-next-notice-clears-only-conflict')

    def test_held_open_and_closed_scope_metadata_is_exactly_explained_until_resolution(self):
        for state in ('open', 'closed'):
            for conflict in (False, True):
                with self.subTest(state=state, conflict=conflict):
                    if state != 'open' or conflict:
                        self.service.stop()
                        self.setUp()
                    # Three confirmed inactive histories put the unchanged
                    # sweep calculation exactly on its batch boundary. Their
                    # old confirmation also prevents an invented minimum for A.
                    self.service.scope_budget = 3
                    self.gateway.rows['sessions', None].append(dict(
                        sessionKey='quiet', state='active', displayName='Quiet'))
                    self.service._refresh('sessions')
                    self.settle()
                    self.assertEqual(3, len(self.service._repeated_history_scopes()))
                    baseline, row = self.establish()
                    if conflict:
                        self.service.assignment_holds.hold(row['id'], row['workItemId'],
                            self.item(baseline), at=1000,
                            after_delivery=self.service.assignment_delivery_consumed)
                    row = dict(row, state=state, closedAt=None if state == 'open' else 200)
                    self.set_source_assignment(row)
                    calls = list(self.gateway.calls)
                    self.assertTrue(self.service.change(self.notice(row)))
                    self.service._publish()
                    held = self.service.assignment_holds.assignments[row['id']]
                    self.assertFalse(held.conflict)
                    self.assertTrue(held.incomplete_contribution)
                    self.assertEqual(calls, self.gateway.calls)
                    self.assertEqual({}, self.service.reads.pending)
                    label = 'r34-' + state + '-conflict-' + str(conflict)
                    _, fresh = self.assert_held_publication(baseline, label)
                    warm_meta = self.service.current['freshness']['reconciliation']
                    fresh_meta = fresh.current['freshness']['reconciliation']
                    if state == 'open':
                        self.assertEqual(fresh_meta['historyScopes'], warm_meta['historyScopes'])
                        self.assertEqual(fresh_meta['active']['scopeCount'], warm_meta['active']['scopeCount'])
                        self.assertEqual(fresh_meta['active']['unconfirmedScopes'] + 1,
                                         warm_meta['active']['unconfirmedScopes'])
                        self.assertEqual(fresh_meta['active']['oldestConfirmedAt'],
                                         warm_meta['active']['oldestConfirmedAt'])
                        self.assertEqual(fresh_meta['estimatedSweepMs'], warm_meta['estimatedSweepMs'])
                    else:
                        self.assertEqual(fresh_meta['historyScopes'] + 1, warm_meta['historyScopes'])
                        self.assertEqual(fresh_meta['history']['scopeCount'] + 1,
                                         warm_meta['history']['scopeCount'])
                        self.assertEqual(fresh_meta['history']['unconfirmedScopes'] + 1,
                                         warm_meta['history']['unconfirmedScopes'])
                        self.assertEqual(fresh_meta['history']['oldestConfirmedAt'],
                                         warm_meta['history']['oldestConfirmedAt'])
                        self.assertEqual(300000, fresh_meta['estimatedSweepMs'])
                        self.assertEqual(600000, warm_meta['estimatedSweepMs'])
                    assert_replay_current(self, self.service, baseline, label + '-held-replay')
                    # Use the already-authorized complete covering consumer;
                    # no hold-driven read or new resolver is introduced.
                    self.service._request_item_assignments(row['workItemId'])
                    self.settle()
                    self.assertNotIn(row['id'], self.service.assignment_holds.assignments)
                    self.assertEqual(1, self.gateway.calls.count(('membership', row['workItemId'])))
                    assert_publication_equal(self, self.fresh().current, self.service.current,
                                             label + '-resolved')
                    assert_replay_current(self, self.service, baseline, label + '-resolved-replay')

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

    def test_failed_covering_read_keeps_hold_until_its_existing_retry(self):
        for unpublished in (False, True):
            with self.subTest(unpublished=unpublished):
                if unpublished:
                    self.service.stop()
                    self.setUp()
                baseline, row = self.establish(unpublished=unpublished)
                self.gateway.fail_member = row['id']
                self.service._request_item_assignments(row['workItemId'])
                self.service._publish()
                self.assertIn(row['id'], self.service.assignment_holds.assignments)
                self.settle()
                held = self.service.assignment_holds.assignments[row['id']]
                self.assertTrue(held.incomplete_contribution)
                self.assertFalse(held.conflict)
                if unpublished:
                    self.assertNotIn(row['workItemId'], {item['id'] for item in self.service.current['items']})
                else:
                    self.assert_retained(self.item(baseline), self.item(self.service.current), 1000)
                job = ('item-assignment-source', row['workItemId'], None)
                self.assertIn(job, self.service.read_retries)
                calls = list(self.gateway.calls)
                self.service.change(self.notice(row))
                self.service._publish()
                self.assertEqual(calls, self.gateway.calls)
                self.assertEqual({}, self.service.reads.pending)
                self.gateway.fail_member = None
                _deadline, priority = self.service.read_retries[job]
                self.service.read_retries[job] = (0, priority)
                self.service._retry_incomplete_reads()
                self.settle()
                self.assertNotIn(row['id'], self.service.assignment_holds.assignments)
                self.assertEqual(2, self.gateway.calls.count(('membership', row['workItemId'])))
                self.assertEqual(0, self.service.reads.epoch_discards)
                assert_publication_equal(self, self.fresh().current, self.service.current,
                                         'r32-existing-retry-' + row['workItemId'])
                assert_replay_current(self, self.service, baseline,
                                      'r32-existing-retry-replay-' + row['workItemId'])

    def test_complete_item_resolves_contribution_but_not_concurrent_order_conflict(self):
        for unpublished in (False, True):
            with self.subTest(unpublished=unpublished):
                if unpublished:
                    self.service.stop()
                    self.setUp()
                baseline, row = self.establish(unpublished=unpublished)
                event = self.queued_source('item-assignment-source', row['workItemId'])
                # The source can have closed both before and after an observed
                # reopen. No generation orders that read against either notice.
                # Both events are queued; only the later close may resolve.
                self.service._queue_feed_change(self.notice(dict(row, state='open', closedAt=None)))
                self.service._queue_feed_change(self.notice(row))
                self.service._handle(*event)
                self.service._publish()
                held = self.service.assignment_holds.assignments[row['id']]
                self.assertFalse(held.incomplete_contribution)
                self.assertTrue(held.conflict)
                self.assert_held_publication(baseline, 'r32-complete-with-conflict-' + row['workItemId'])
                self.service._handle(*self.service.events.get(timeout=5))
                self.service._publish()
                self.assertTrue(self.service.assignment_holds.assignments[row['id']].conflict)
                before_close_calls = list(self.gateway.calls)
                self.service._handle(*self.service.events.get(timeout=5))
                self.settle()
                # R36 section 3 / att_e33b4539; owner mapping art_637a203a.
                # The next close resolves ordering but has no identified P.
                held = self.service.assignment_holds.assignments[row['id']]
                self.assertFalse(held.conflict)
                self.assertFalse(held.incomplete_contribution)
                self.assertTrue(held.producer_recovery)
                self.assertIsNone(held.producer)
                self.assertEqual(before_close_calls, self.gateway.calls)
                if unpublished:
                    self.assertNotIn(row['workItemId'],
                                     {item['id'] for item in self.service.current['items']})
                else:
                    self.assert_retained(self.item(baseline),
                                         self.item(self.service.current), 1000)
                # An existing covering refresh, not a notice-driven item chain.
                self.service._refresh('assignments')
                self.settle()
                self.assertNotIn(row['id'], self.service.assignment_holds.assignments)
                self.assertEqual(1, self.gateway.calls.count(('membership', row['workItemId'])))
                self.assertEqual(0, self.service.reads.epoch_discards)
                assert_publication_equal(self, self.fresh().current, self.service.current,
                                         'r32-conflict-next-notice-' + row['workItemId'])
                assert_replay_current(self, self.service, baseline,
                                      'r32-conflict-next-notice-replay-' + row['workItemId'])

    def test_ownerless_notice_replays_into_queued_first_contribution_snapshot(self):
        self.assert_first_snapshot_replay()

    def test_refs_owner_notice_replays_into_queued_first_contribution_snapshot(self):
        self.assert_first_snapshot_replay(owner_field='refs')

    def test_payload_owner_notice_replays_into_queued_first_contribution_snapshot(self):
        self.assert_first_snapshot_replay(owner_field='payload')

    def assert_first_snapshot_replay(self, *, owner_field=None):
        baseline, row = self.establish()
        event = self.queued_source('item-assignment-source', row['workItemId'])
        late = dict.fromkeys(ATTEST_FIELDS) | dict(
            id='after-first-snapshot', assignmentId=row['id'], kind='progress',
            bySession='author', ts=960000, commitRefs=[])
        self.gateway.rows['attests', row['id']].append(deepcopy(late))
        self.assertNotIn(row['id'], self.service.model.item_contributions.entries)
        notice = dict(resource='attests', op='upsert', **{'class': 'attest.filed'},
            refs={'attestId': late['id'], 'assignmentId': row['id']}, payload=deepcopy(late))
        if owner_field is not None:
            notice[owner_field]['workItemId'] = row['workItemId']
        self.assertTrue(self.service.change(notice))
        self.assertEqual(1, self.service.reads.item_flights[row['workItemId']].notice_count)
        self.assertNotIn(row['id'], self.service.model.item_contributions.entries)
        self.assertTrue(self.service.assignment_holds.assignments[row['id']].incomplete_contribution)
        self.service._handle(*event)
        self.assertEqual(2, self.service.model.item_contributions.entries[row['id']].attests.count)
        self.settle()
        self.assertNotIn(row['id'], self.service.assignment_holds.assignments)
        self.assertEqual(1, self.gateway.calls.count(('membership', row['workItemId'])))
        self.assertEqual(2 if owner_field is None else 1, self.service.reads.attest_owner_attempts)
        self.assertEqual(0, self.service.reads.epoch_discards)
        self.assertEqual({}, self.service.reads.item_flights)
        self.assertEqual(960000, self.service.model.session_activity['author'])
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r32-first-snapshot-replay-' + str(owner_field))
        assert_replay_current(self, self.service, baseline, 'r32-first-snapshot-sse-' + str(owner_field))

    def test_summary_eligibility_change_resolves_without_requiring_skipped_history(self):
        # Exercise the existing implicit summary mode, not a simulated change
        # to the pinned upstream version. Initially neither item has a summary.
        # R7/R32 matched handshake; custodian mapping art_637a203a.
        self.service._handle('ready', None)
        self.settle()
        baseline, row = self.establish()
        original = deepcopy(self.gateway.rows['work-items', None])
        self.gateway.rows['work-items', None] = [
            summarized_work(holders=['author'] if item['id'] == 'work' else [],
                            attest_counts={'progress': 4} if item['id'] == 'work' else {},
                            started=item['id'] == 'work') | dict(
                id=item['id'], title=item['title'], rowVersion=item['rowVersion'] + 1)
            for item in original]
        self.gateway.calls.clear()
        self.service._refresh('work-items')
        self.settle()
        self.assertFalse(self.service._attest_activity_eligible('work'))
        self.assertNotIn(row['id'], self.service.assignment_holds.assignments)
        self.assertNotIn(('read', 'attests', row['id']), self.gateway.calls)
        fresh = self.fresh()
        # The existing R21 winner-exit exception covers exactly these retained
        # session observations. R32 adds no item exception after resolution.
        assert_winner_exit_publication(self, fresh.current, self.service.current,
                                      'r32-summary-source-exit', {'author', 'other'})
        assert_replay_current(self, self.service, baseline, 'r32-summary-source-exit-replay')
