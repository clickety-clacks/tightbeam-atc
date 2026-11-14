"""R29 publication retention; source-result merge is checked separately."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_assignment_conflicts import AssignmentReadConflicts
from atc_payloads import ASSIGNMENT_FIELDS
from atc_service import LiveService
from atc_upstream import ATTEST_FIELDS
from test_item_assignment_retry import ItemReads
from publication_assertions import assert_publication_equal, assert_replay_current


class AssignmentHoldPublicationTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_service.now_ms', return_value=1000)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.gateway = ItemReads()
        other = deepcopy(self.gateway.rows['work-items', None][0])
        other.update(id='unaffected-item', title='Unaffected')
        self.gateway.rows['work-items', None].append(other)
        self.service = LiveService(self.gateway, workers=1)
        self.addCleanup(self.service.stop)
        self.service._handle('ready', {'version': '0.1.9', 'build': 2908, 'sha': '8433d22f'})
        while self.service.reads.pending or self.service.reads.active:
            self.service.reads.pump()
            self.service._handle(*self.service.events.get(timeout=5))
        self.service._publish()
        self.opened = dict.fromkeys(ASSIGNMENT_FIELDS) | deepcopy(next(
            row for row in self.gateway.rows['assignments', None] if row['id'] == 'open'))
        self.closed = dict(self.opened, state='closed', closedAt=5000, outcome='completed')

    def notice(self, row):
        return dict(resource='assignments', op='upsert', refs={'assignmentId': row['id']},
                    payload=row, **{'class': 'assignment.closed' if row['state'] == 'closed'
                                    else 'assignment.reopened'})

    def item(self, frame, identity='work'):
        return next(row for row in frame['items'] if row['id'] == identity)

    def assert_retained(self, original, actual, since):
        expected = deepcopy(original)
        for fact in expected['evidence'].values():
            if fact['freshness'] != 'stale':
                fact['freshness'] = 'unconfirmed'
            prior = fact.get('unconfirmedSince')
            fact['unconfirmedSince'] = since if prior is None else min(prior, since)
        self.assertEqual(expected, actual)

    def queued_source(self, resource):
        if resource == 'assignments':
            self.service._refresh(resource)
        else:
            self.service._request_item_assignments('work')
        self.service.reads.pump()
        event = self.service.events.get(timeout=5)
        self.assertEqual('read', event[0])
        self.assertEqual(resource, event[1][0][0])
        self.assertTrue(event[1][2].complete, event[1][2].error)
        return event

    def settle(self, service=None):
        service = service or self.service
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            service._handle(*service.events.get(timeout=5))
        service._publish()

    def fresh(self):
        gateway = ItemReads()
        gateway.rows = deepcopy(self.gateway.rows)
        service = LiveService(gateway, workers=1)
        self.addCleanup(service.stop)
        service._handle('ready', {'version': '0.1.9', 'build': 2908, 'sha': '8433d22f'})
        self.settle(service)
        return service

    def set_source_assignment(self, row):
        rows = self.gateway.rows['assignments', None]
        index = next(i for i, value in enumerate(rows) if value['id'] == row['id'])
        rows[index] = deepcopy(row)

    def test_item_queued_establisher_is_not_applied_and_clean_source_resolves(self):
        baseline = deepcopy(self.service.current)
        event = self.queued_source('item-assignment-source')
        stage = event[1][2].folded
        self.set_source_assignment(self.closed)
        self.service._queue_feed_change(self.notice(self.closed))
        calls = list(self.gateway.calls)
        self.clock.return_value = 6000
        self.service._handle(*event)
        self.service._publish()
        self.assert_retained(self.item(baseline), self.item(self.service.current), 6000)
        self.assertEqual({}, stage.assignment_fingerprints)
        self.assertEqual('open', self.service.model.item_contributions.entries['open'].state)
        self.service._handle(*self.service.events.get(timeout=5))
        self.service._publish()
        self.assertEqual('open', self.service.model.item_contributions.entries['open'].state)
        self.assertIn('open', self.service.assignment_holds.assignments)
        self.assertEqual(calls, self.gateway.calls)
        self.assertFalse(self.service.reads.pending)
        self.assertEqual(0, self.service.reads.epoch_discards)
        assert_replay_current(self, self.service, baseline, 'r29-item-queued-establisher')

        # No new timer/read is introduced by the hold. This explicitly invokes
        # the existing complete item source as the later clean resolver.
        clean = self.queued_source('item-assignment-source')
        self.service._handle(*clean)
        self.settle()
        self.assertFalse(self.service.assignment_holds.assignments)
        self.assertEqual('closed', self.service.model.item_contributions.entries['open'].state)
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r29-item-clean-resolver')
        assert_replay_current(self, self.service, baseline, 'r29-item-clean-resolver-replay')

    def test_item_next_already_queued_reopen_resolves_after_establishing_close(self):
        baseline = deepcopy(self.service.current)
        event = self.queued_source('item-assignment-source')
        self.service._queue_feed_change(self.notice(self.closed))
        self.service._queue_feed_change(self.notice(self.opened))
        self.service._handle(*event)
        self.service._publish()
        self.assertIn('open', self.service.assignment_holds.assignments)
        calls = list(self.gateway.calls)
        self.service._handle(*self.service.events.get(timeout=5))
        self.assertIn('open', self.service.assignment_holds.assignments)
        self.service._handle(*self.service.events.get(timeout=5))
        self.service._publish()
        self.assertFalse(self.service.assignment_holds.assignments)
        self.assertFalse(self.service.reads.pending)
        self.assertEqual(calls, self.gateway.calls)
        self.assertEqual(0, self.service.reads.epoch_discards)
        self.set_source_assignment(self.opened)
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r29-item-next-queued-reopen')
        assert_replay_current(self, self.service, baseline, 'r29-item-next-queued-reopen-replay')

    def test_item_replay_is_committed_behind_held_publication_and_not_lost_on_resolution(self):
        baseline = deepcopy(self.service.current)
        event = self.queued_source('item-assignment-source')
        before = self.service.model.item_contributions.entries['open'].attests.count
        row = dict.fromkeys(ATTEST_FIELDS) | dict(id='new-attest', assignmentId='open',
            kind='progress', bySession='author', ts=7000, commitRefs=[{'commit': 'new'}])
        self.gateway.rows['attests', 'open'].append(deepcopy(row))
        self.service.change(dict(resource='attests', op='upsert',
            refs={'assignmentId': 'open', 'attestId': row['id']}, payload=row,
            **{'class': 'attest.filed'}))
        self.service._queue_feed_change(self.notice(self.closed))
        self.service._queue_feed_change(self.notice(self.opened))
        self.service._handle(*event)
        self.service._publish()
        self.assert_retained(self.item(baseline), self.item(self.service.current), 1000)
        self.assertEqual(before + 1, self.service.model.item_contributions.entries['open'].attests.count)
        self.assertEqual(7000, self.service.model.session_activity['author'])
        self.service._handle(*self.service.events.get(timeout=5))
        self.service._handle(*self.service.events.get(timeout=5))
        self.settle()
        self.set_source_assignment(self.opened)
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r29-item-attest-replay-with-mutable-conflict')
        assert_replay_current(self, self.service, baseline, 'r29-item-replay-hold-resolution')

    def test_global_conflict_hold_resolves_only_after_later_clean_collection(self):
        baseline = deepcopy(self.service.current)
        event = self.queued_source('assignments')
        self.set_source_assignment(self.closed)
        self.service._queue_feed_change(self.notice(self.closed))
        self.clock.return_value = 6000
        self.service._handle(*event)
        self.service._handle(*self.service.events.get(timeout=5))
        self.settle()
        self.assert_retained(self.item(baseline), self.item(self.service.current), 6000)
        self.assertIn('open', self.service.assignment_holds.assignments)
        self.assertEqual(0, self.service.reads.epoch_discards)
        assert_replay_current(self, self.service, baseline, 'r29-global-conflict-hold')

        clean = self.queued_source('assignments')
        self.service._handle(*clean)
        self.settle()
        self.assertFalse(self.service.assignment_holds.assignments)
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r29-global-clean-resolver')
        assert_replay_current(self, self.service, baseline, 'r29-global-clean-resolver-replay')

    def test_repeat_item_conflict_and_failed_source_keep_original_hold(self):
        baseline = deepcopy(self.service.current)
        for timestamp in (2000, 3000):
            event = self.queued_source('item-assignment-source')
            self.service._queue_feed_change(self.notice(self.closed))
            self.clock.return_value = timestamp
            self.service._handle(*event)
            self.service._handle(*self.service.events.get(timeout=5))
            self.service._publish()
            self.assert_retained(self.item(baseline), self.item(self.service.current), 2000)
        self.gateway.fail_member = 'last'
        self.service._request_item_assignments('work')
        self.service.reads.pump()
        event = self.service.events.get(timeout=5)
        self.assertFalse(event[1][2].complete)
        self.service._handle(*event)
        self.service._publish()
        self.assert_retained(self.item(baseline), self.item(self.service.current), 2000)
        self.assertIn('open', self.service.assignment_holds.assignments)
        self.assertFalse(self.service.reads.item_flights)
        self.assertFalse(self.service.reads.assignment_reads.flights)
        assert_replay_current(self, self.service, baseline, 'r29-repeat-conflict-failed-item')

    def test_hold_shares_actual_last_publication_without_changing_other_collections(self):
        before_close = deepcopy(self.service.current)
        self.clock.return_value = 6000
        flight = AssignmentReadConflicts()
        flight.observe(self.notice(self.closed), delivery=1)
        self.assertTrue(self.service.change(self.notice(self.closed)))
        self.service._publish()
        published = self.service.current
        retained = self.item(published)
        original = deepcopy(retained)
        self.assertNotEqual(self.item(before_close), retained)
        calls = list(self.gateway.calls)
        conflicts = self.service._hold_assignment_conflicts(
            [self.opened], flight, complete=True, at=6000)
        self.assertEqual({'open'}, conflicts)
        self.assertIs(retained, self.service.assignment_holds.items['work'])
        self.service._publish()
        self.assert_retained(original, self.item(self.service.current), 6000)
        self.assertEqual(original, retained)  # Earlier full/replay frames remain immutable.
        self.assertEqual(self.item(published, 'unaffected-item'),
                         self.item(self.service.current, 'unaffected-item'))
        for name in ('agents', 'links', 'wakes', 'messages'):
            self.assertEqual(published[name], self.service.current[name], name)
        self.assertEqual(calls, self.gateway.calls)
        assert_replay_current(self, self.service, published, 'r29-held-item-publication')

    def test_queued_establisher_and_feed_drain_do_not_clear_hold(self):
        flight = AssignmentReadConflicts()
        flight.observe(self.notice(self.closed), delivery=10)
        baseline = self.service.current
        self.clock.return_value = 2000
        self.service._hold_assignment_conflicts([self.opened], flight, complete=True, at=2000)
        self.service._publish()
        held = deepcopy(self.item(self.service.current))
        self.assertFalse(self.service.assignment_holds.resolve_notice('open', 10))
        # Nothing about another publication, zero queued events, or a partial
        # read establishes a resolver. No read is scheduled by this hold.
        self.clock.return_value = 3000
        self.assertEqual(set(), self.service._hold_assignment_conflicts(
            [self.closed], flight, complete=False, at=3000))
        self.service._publish()
        self.assertEqual(held, self.item(self.service.current))
        self.assertFalse(self.service.reads.pending)
        self.assertFalse(self.service.reads.active)
        assert_replay_current(self, self.service, baseline, 'r29-feed-drain-hold')

    def test_already_consumed_later_notice_is_not_a_retroactive_resolver(self):
        flight = AssignmentReadConflicts()
        flight.observe(self.notice(self.closed), delivery=5)
        self.service.assignment_delivery_consumed = 8
        self.clock.return_value = 2000
        self.service._hold_assignment_conflicts([self.opened], flight, complete=True, at=2000)
        self.assertFalse(self.service.assignment_holds.resolve_notice('open', 5))
        self.assertFalse(self.service.assignment_holds.resolve_notice('open', 8))
        self.assertTrue(self.service.assignment_holds.resolve_notice('open', 9))
        self.service._publish()
        self.assertEqual({}, self.service.assignment_holds.items)
