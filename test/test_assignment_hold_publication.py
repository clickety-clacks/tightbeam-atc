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

    def queued_source(self, resource, item='work'):
        if resource == 'assignments':
            self.service._refresh(resource)
        else:
            self.service._request_item_assignments(item)
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

    def assert_first_discovery_hold(self, baseline, wid, unpublished, label):
        expected = deepcopy(self.fresh().current)
        if unpublished:
            expected['items'] = [item for item in expected['items'] if item['id'] != wid]
            self.assertIsNone(self.service.assignment_holds.items[wid])
        else:
            retained = deepcopy(self.item(baseline, wid))
            since = self.service.assignment_holds.since(wid)
            for fact in retained['evidence'].values():
                if fact['freshness'] != 'stale':
                    fact['freshness'] = 'unconfirmed'
                prior = fact.get('unconfirmedSince')
                fact['unconfirmedSince'] = since if prior is None else min(prior, since)
            expected['items'] = [retained if item['id'] == wid else item
                                 for item in expected['items']]
        # R30 permits only the named item's publication/absence. In
        # particular do not omit agents, links, board health or other items.
        assert_publication_equal(self, expected, self.service.current, label)

    def first_discovery(self, resource, *, unpublished, resolution='notice'):
        baseline = deepcopy(self.service.current)
        wid = 'discovered-item' if unpublished else 'work'
        if unpublished:
            item = dict(self.gateway.rows['work-items', None][0], id=wid, title='New item')
            self.gateway.rows['work-items', None].append(item)
            self.service._refresh('work-items')
            self.service.reads.pump()
            event = self.service.events.get(timeout=5)
            self.assertEqual(('work-items', None, None), event[1][0])
            self.service._handle(*event)
            # Deliberately do not publish between source messages in this
            # model-worker batch: this item has no committed frame yet.
            self.assertNotIn(wid, {row['id'] for row in self.service.current['items']})
        row = dict(self.opened, id='discovered', workItemId=wid, openedAt=90,
                   reviewsAssignmentId=None, effectKind='spec')
        self.gateway.rows['assignments', None].append(deepcopy(row))
        self.gateway.rows['attests', 'discovered'] = []
        self.assertNotIn('discovered', self.service.model.item_contributions.entries)
        self.assertNotIn('discovered', self.service.model.inventory('assignments').rows)
        self.gateway.calls.clear()
        event = self.queued_source(resource, wid)
        # Both source-consistent histories can expose an open read and a
        # queued close. A following reopen is already queued, but is not a
        # resolver until it is consumed after the hold is established.
        closed = dict(row, state='closed', closedAt=95)
        self.service._queue_feed_change(self.notice(closed))
        if resolution == 'notice':
            self.service._queue_feed_change(self.notice(row))
        else:
            self.set_source_assignment(closed)
        self.service._handle(*event)
        self.service._publish()
        self.assertIn('discovered', self.service.assignment_holds.assignments)
        if unpublished:
            self.assertIsNone(self.service.assignment_holds.items[wid])
            self.assertNotIn(wid, {item['id'] for item in self.service.current['items']})
        else:
            self.assert_retained(self.item(baseline), self.item(self.service.current), 1000)
        self.assertEqual(0, self.service.reads.epoch_discards)
        self.assertNotIn(('item-assignment-source', wid, None), self.service.read_retries)
        self.assertNotIn(('item-assignment-source', wid, None), self.service.reads.pending)
        calls = list(self.gateway.calls)
        self.service._handle(*self.service.events.get(timeout=5))
        self.service._publish()
        self.assertIn('discovered', self.service.assignment_holds.assignments)
        self.assertEqual(calls, self.gateway.calls)
        label = 'r30-' + self._testMethodName
        assert_replay_current(self, self.service, baseline, label + '-held-replay')

        if resolution == 'notice':
            self.service._handle(*self.service.events.get(timeout=5))
        else:
            # Finish ordinary discovery already scheduled by the global
            # source. Feed drain and completion of attest reads do not resolve
            # a mutable assignment hold.
            self.settle()
            self.assertIn('discovered', self.service.assignment_holds.assignments)
            self.assert_first_discovery_hold(baseline, wid, unpublished, label + '-full-held')
            if resolution == 'held':
                return baseline, row, wid, label
            self.assertEqual('clean', resolution)
            clean = self.queued_source(resource, wid)
            self.service._handle(*clean)
        self.assertNotIn('discovered', self.service.assignment_holds.assignments)
        self.settle()
        self.assertIn(wid, {item['id'] for item in self.service.current['items']})
        attempts = 1 if resolution == 'notice' else 2
        self.assertEqual(attempts if resource == 'item-assignment-source' else 0,
                         sum(call[0] == 'membership' for call in self.gateway.calls))
        self.assertEqual(attempts if resource == 'assignments' else 0,
                         self.gateway.calls.count(('read', 'assignments', None)))
        self.assertEqual(0, self.service.reads.epoch_discards)
        assert_publication_equal(self, self.fresh().current, self.service.current, label)
        assert_replay_current(self, self.service, baseline, label + '-resolved-replay')

    def test_global_first_discovery_holds_prior_published_item(self):
        self.first_discovery('assignments', unpublished=False)

    def test_item_first_discovery_holds_prior_published_item(self):
        self.first_discovery('item-assignment-source', unpublished=False)

    def test_global_first_discovery_holds_prior_absence(self):
        self.first_discovery('assignments', unpublished=True)

    def test_item_first_discovery_holds_prior_absence(self):
        self.first_discovery('item-assignment-source', unpublished=True)

    def test_global_first_discovery_clean_read_resolves_published_item(self):
        self.first_discovery('assignments', unpublished=False, resolution='clean')

    def test_item_first_discovery_clean_read_resolves_published_item(self):
        self.first_discovery('item-assignment-source', unpublished=False, resolution='clean')

    def test_global_first_discovery_clean_read_resolves_prior_absence(self):
        self.first_discovery('assignments', unpublished=True, resolution='clean')

    def test_item_first_discovery_clean_read_resolves_prior_absence(self):
        self.first_discovery('item-assignment-source', unpublished=True, resolution='clean')

    def first_discovery_repeat_and_retry(self, resource, *, unpublished):
        baseline, opened, wid, label = self.first_discovery(
            resource, unpublished=unpublished, resolution='held')
        original = deepcopy(self.service.assignment_holds.items[wid])
        repeat = self.queued_source(resource, wid)
        self.service._queue_feed_change(self.notice(opened))
        self.service._handle(*repeat)
        self.service._handle(*self.service.events.get(timeout=5))
        self.settle()
        self.assertEqual(original, self.service.assignment_holds.items[wid])
        self.assert_first_discovery_hold(baseline, wid, unpublished, label + '-repeat')
        if resource == 'assignments':
            self.gateway.subset = set()
            self.service._refresh('assignments')
            job = ('assignments', None, None)
        else:
            self.gateway.fail_member = 'discovered'
            self.service._request_item_assignments(wid)
            job = ('item-assignment-source', wid, None)
        self.service.reads.pump()
        failed = self.service.events.get(timeout=5)
        self.assertEqual(job, failed[1][0])
        self.assertFalse(failed[1][2].complete)
        self.service._handle(*failed)
        self.service._publish()
        self.assertEqual(original, self.service.assignment_holds.items[wid])
        self.assertIn('discovered', self.service.assignment_holds.assignments)
        self.assertIn(job, self.service.read_retries)
        if unpublished:
            self.assertNotIn(wid, {item['id'] for item in self.service.current['items']})
        else:
            self.assert_retained(self.item(baseline, wid), self.item(self.service.current, wid), 1000)
        assert_replay_current(self, self.service, baseline, label + '-failed')

        self.gateway.subset = None
        self.gateway.fail_member = None
        _deadline, priority = self.service.read_retries[job]
        self.service.read_retries[job] = (0, priority)
        self.service._retry_incomplete_reads()
        self.settle()
        self.assertFalse(self.service.assignment_holds.assignments)
        self.assertFalse(self.service.assignment_holds.items)
        self.assertNotIn(job, self.service.read_retries)
        self.assertEqual(0, self.service.reads.epoch_discards)
        self.assertEqual(4 if resource == 'item-assignment-source' else 0,
                         sum(call[0] == 'membership' for call in self.gateway.calls))
        self.assertEqual(4 if resource == 'assignments' else 0,
                         self.gateway.calls.count(('read', 'assignments', None)))
        assert_publication_equal(self, self.fresh().current, self.service.current, label + '-retry')
        assert_replay_current(self, self.service, baseline, label + '-retry-replay')

    def test_global_first_discovery_repeat_partial_and_retry_published(self):
        self.first_discovery_repeat_and_retry('assignments', unpublished=False)

    def test_global_first_discovery_repeat_partial_and_retry_absent(self):
        self.first_discovery_repeat_and_retry('assignments', unpublished=True)

    def test_item_first_discovery_repeat_failure_and_retry_published(self):
        self.first_discovery_repeat_and_retry('item-assignment-source', unpublished=False)

    def test_item_first_discovery_repeat_failure_and_retry_absent(self):
        self.first_discovery_repeat_and_retry('item-assignment-source', unpublished=True)

    def test_restart_releases_first_discovery_absence_through_fresh_load(self):
        baseline, _row, wid, label = self.first_discovery(
            'item-assignment-source', unpublished=True, resolution='held')
        self.service.stop()
        restarted = self.fresh()
        self.assertEqual({}, restarted.assignment_holds.assignments)
        self.assertEqual({}, restarted.assignment_holds.items)
        self.assertEqual(1, sum(item['id'] == wid for item in restarted.current['items']))
        self.assertEqual(self.item(baseline, 'unaffected-item'),
                         self.item(restarted.current, 'unaffected-item'))
        assert_publication_equal(self, self.fresh().current, restarted.current, label + '-restart')

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

    def test_applied_close_during_item_read_is_retained_without_epoch_restart(self):
        baseline = deepcopy(self.service.current)
        event = self.queued_source('item-assignment-source')
        job = event[1][0]
        epoch = self.service.reads.epochs[job[:2]]
        calls = list(self.gateway.calls)
        self.clock.return_value = 6000
        self.set_source_assignment(self.closed)
        self.assertTrue(self.service.change(self.notice(self.closed)))
        self.service._publish()
        published_close = deepcopy(self.item(self.service.current))
        self.assertEqual('closed', self.service.model.item_contributions.entries['open'].state)
        self.assertEqual(epoch, self.service.reads.epochs[job[:2]])
        self.assertFalse(self.service.reads.pending)
        self.assertEqual(calls, self.gateway.calls)
        self.service._handle(*event)
        self.service._publish()
        self.assert_retained(published_close, self.item(self.service.current), 6000)
        self.assertIn('open', self.service.assignment_holds.assignments)
        self.assertEqual(0, self.service.reads.epoch_discards)
        self.set_source_assignment(self.opened)
        self.service.change(self.notice(self.opened))
        self.settle()
        self.assertFalse(self.service.assignment_holds.assignments)
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r29-applied-close-during-item-read')
        assert_replay_current(self, self.service, baseline, 'r29-applied-close-item-replay')

    def test_applied_close_during_global_read_keeps_its_actual_publication(self):
        baseline = deepcopy(self.service.current)
        event = self.queued_source('assignments')
        epoch = self.service.reads.epochs['assignments', None]
        self.clock.return_value = 6000
        self.set_source_assignment(self.closed)
        self.service.change(self.notice(self.closed))
        self.service._publish()
        published_close = deepcopy(self.item(self.service.current))
        self.assertEqual(epoch, self.service.reads.epochs['assignments', None])
        self.assertFalse(self.service.reads.pending)
        self.service._handle(*event)
        self.settle()
        self.assert_retained(published_close, self.item(self.service.current), 6000)
        self.assertIn('open', self.service.assignment_holds.assignments)
        self.assertEqual('closed', self.service.model.item_contributions.entries['open'].state)
        self.assertEqual(0, self.service.reads.epoch_discards)
        clean = self.queued_source('assignments')
        self.service._handle(*clean)
        self.settle()
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r29-applied-close-during-global-read')
        assert_replay_current(self, self.service, baseline, 'r29-applied-close-global-replay')

    def test_delayed_creation_cannot_reopen_a_loaded_closed_projection(self):
        self.service.change(self.notice(self.closed))
        self.service._publish()
        baseline = deepcopy(self.service.current)
        contribution = self.service.model.item_contributions.entries['open']
        revision = self.service.model.details_revision
        calls = list(self.gateway.calls)
        notice = self.notice(self.opened)
        notice['class'] = 'assignment.opened'
        self.assertTrue(self.service.change(notice))
        self.service._publish()
        self.assertEqual(contribution, self.service.model.item_contributions.entries['open'])
        self.assertEqual(revision, self.service.model.details_revision)
        self.assertEqual(calls, self.gateway.calls)
        self.assertFalse(self.service.reads.pending)
        assert_publication_equal(self, baseline, self.service.current, 'r29-delayed-creation')

    def test_new_member_and_close_during_item_read_keep_existing_scoped_confirmation(self):
        baseline = deepcopy(self.service.current)
        event = self.queued_source('item-assignment-source')
        epoch = self.service.reads.epochs['item-assignment-source', 'work']
        new = dict(self.opened, id='new', effectKind='code', reviewsAssignmentId=None,
                   openedAt=40, closedAt=None, state='open')
        self.gateway.rows['assignments', None].insert(0, deepcopy(new))
        self.gateway.rows['attests', 'new'] = []
        notice = self.notice(new)
        notice['class'] = 'assignment.opened'
        self.service.change(notice)
        scoped = ('attests', 'new', None)
        self.assertIn(scoped, self.service.reads.pending)
        close = dict(new, state='closed', closedAt=5000, outcome='completed')
        self.set_source_assignment(close)
        self.service.change(self.notice(close))
        self.assertIn(scoped, self.service.reads.pending)
        self.assertEqual('closed', self.service.model.item_contributions.entries['new'].state)
        self.assertIsNone(self.service.model.item_contributions.entries['new'].evidence.observed_at)
        # This eligible progress was written while open and delivered after
        # closure, before the already scheduled first attest confirmation.
        live = dict.fromkeys(ATTEST_FIELDS) | dict(id='new-live', assignmentId='new',
            kind='progress', bySession='author', ts=4000, commitRefs=[])
        self.gateway.rows['attests', 'new'].append(deepcopy(live))
        pending, calls = dict(self.service.reads.pending), list(self.gateway.calls)
        attest = dict(resource='attests', op='upsert',
            refs={'attestId': live['id'], 'assignmentId': 'new'}, payload=live,
            **{'class': 'attest.filed'})
        self.assertTrue(self.service.change(attest))
        self.assertTrue(self.service.change(attest))
        current = self.service.model.item_contributions.entries['new']
        self.assertEqual(1, current.attests.count)
        self.assertFalse(current.evidence.complete)
        self.assertEqual(pending, self.service.reads.pending)
        self.assertEqual(calls, self.gateway.calls)
        self.assertEqual(epoch, self.service.reads.epochs['item-assignment-source', 'work'])
        self.service._handle(*event)
        self.assertIn('new', self.service.model.item_contributions.entries)
        self.settle()
        self.assertTrue(self.service.model.item_contributions.entries['new'].evidence.complete)
        self.assertEqual(1, sum(call[0] == 'membership' for call in self.gateway.calls))
        self.assertEqual(1, sum(call == ('read', 'attests', 'new') for call in self.gateway.calls))
        self.assertEqual(0, self.service.reads.epoch_discards)
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r29-created-closed-during-item-read')
        assert_replay_current(self, self.service, baseline, 'r29-created-closed-item-replay')

    def test_global_completion_neither_restarts_item_read_nor_resolves_its_held_scope(self):
        flight = AssignmentReadConflicts()
        flight.observe(self.notice(self.closed), delivery=1)
        self.service._hold_assignment_conflicts([self.opened], flight, complete=True, at=1000)
        item_event = self.queued_source('item-assignment-source')
        epoch = self.service.reads.epochs['item-assignment-source', 'work']
        # The first worker has finished, but its item result is still queued
        # for application. Admit another dispatch without consuming that result.
        self.service.reads.workers = 2
        global_event = self.queued_source('assignments')
        self.service._handle(*global_event)
        self.assertEqual(epoch, self.service.reads.epochs['item-assignment-source', 'work'])
        self.assertIn('open', self.service.assignment_holds.assignments)
        self.assertNotIn(('item-assignment-source', 'work', None), self.service.reads.pending)
        self.service._handle(*item_event)
        self.settle()
        self.assertFalse(self.service.assignment_holds.assignments)
        self.assertEqual(0, self.service.reads.epoch_discards)
        self.assertEqual(1, sum(call[0] == 'membership' for call in self.gateway.calls))
        assert_publication_equal(self, self.fresh().current, self.service.current,
                                 'r29-global-completion-with-pending-item')

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
