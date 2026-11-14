"""Ordinary assignment changes must not fan out over released item history."""
from copy import deepcopy
from threading import Event
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_service import LiveService
from atc_payloads import ASSIGNMENT_FIELDS
from atc_upstream import ATTEST_FIELDS, Read
from test_item_assignment_retry import ItemReads
from test_service import summarized_work
from publication_assertions import assert_publication_equal, assert_replay_current


class AssignmentNoticeCostTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_service.now_ms', return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def load(self, gateway, version=None):
        service = LiveService(gateway, workers=1)
        self.addCleanup(service.stop)
        # Exercise the same version/payload-policy setup as the follower. A
        # direct rebuild alone leaves all payload policies unset.
        service._handle('ready', version or {
            'version': '0.1.9', 'build': 2908, 'sha': '8433d22f'})
        self.settle(service)
        return service

    def settle(self, service):
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            service._handle(*service.events.get(timeout=5))
        service._publish()

    def assert_no_member_fanout(self, calls, changed):
        unrelated = [call for call in calls
                     if (call[0] == 'detail' and call[1] == 'assignments'
                         and call[-1] != changed)
                     or (call[:2] == ('read', 'attests') and call[-1] != changed)]
        self.assertEqual([], unrelated, calls)
        self.assertFalse([call for call in calls if call[0] == 'membership'], calls)

    def test_known_attest_during_queued_global_read_and_recovery_is_zero_read(self):
        for recovery in (False, True):
            for aid in ('first', 'open'):
                with self.subTest(recovery=recovery, assignment=aid):
                    gateway = ItemReads()
                    service = self.load(gateway)
                    baseline = deepcopy(service.current)
                    gateway.calls.clear()
                    service._refresh('assignments')
                    service.reads.pump()
                    event = service.events.get(timeout=5)
                    self.assertEqual(('assignments', None), event[1][0][:2])
                    self.assertTrue(event[1][2].complete)
                    # The real complete membership result is queued. Recovery
                    # may be requested before the model worker consumes it.
                    if recovery:
                        service.rebuild()
                        self.assertIsNotNone(service.cycle)
                    self.assertTrue(service.model.closed_sources_released)
                    original = service.model.item_contributions.entries[aid]
                    row = dict.fromkeys(ATTEST_FIELDS) | dict(id='during-membership',
                        assignmentId=aid, kind='progress', bySession='author',
                        ts=4000, commitRefs=[])
                    gateway.rows['attests', aid].append(deepcopy(row))
                    notice = dict(resource='attests', op='upsert',
                        refs={'attestId': row['id'], 'assignmentId': aid}, payload=row,
                        **{'class': 'attest.filed'})
                    calls = list(gateway.calls)
                    pending = dict(service.reads.pending)
                    epochs = dict(service.reads.epochs)
                    for _ in range(2):
                        self.assertTrue(service.change(notice))
                        self.assertEqual(calls, gateway.calls)
                        self.assertEqual(pending, service.reads.pending)
                        self.assertEqual(epochs, dict(service.reads.epochs))
                        service._publish()
                    current = service.model.item_contributions.entries[aid]
                    self.assertEqual(original.attests.count + 1, current.attests.count)
                    self.assertEqual(original.evidence, current.evidence)
                    self.assertEqual(original.detail_fingerprint, current.detail_fingerprint)
                    self.assertFalse(service.model.item_assignment_pending)
                    service._handle(*event)
                    self.settle(service)
                    self.assertEqual(0, service.reads.epoch_discards)
                    self.assertFalse([call for call in gateway.calls if call[0] == 'membership'])
                    fresh_gateway = ItemReads()
                    fresh_gateway.rows = deepcopy(gateway.rows)
                    fresh = self.load(fresh_gateway)
                    label = 'b4-attest-global-' + aid + '-' + str(recovery)
                    assert_publication_equal(self, fresh.current, service.current, label)
                    assert_replay_current(self, service, baseline, label + '-replay')

    def test_attest_burst_replays_queued_complete_item_result_without_restart(self):
        cases = [(aid, ids, count) for aid in ('first', 'open')
                 for ids, count in ((('b', 'c'), 4), (('a', 'c'), 3))]
        for aid, replay_ids, expected in cases:
            with self.subTest(assignment=aid, replay_ids=replay_ids):
                gateway = ItemReads()
                def row(key, timestamp):
                    return dict.fromkeys(ATTEST_FIELDS) | dict(id=key, assignmentId=aid,
                        kind='progress', bySession='author', ts=timestamp, commitRefs=[])
                initial = [row('m', 1000), row('a', 2000)]
                gateway.rows['attests', aid] = deepcopy(initial)
                service = self.load(gateway)
                baseline = deepcopy(service.current)
                started, release = Event(), Event()
                self.addCleanup(release.set)
                original = gateway.read
                def read(resource, scope=None):
                    result = original(resource, scope)
                    if (resource, scope) == ('attests', aid):
                        started.set()
                        if not release.wait(5):
                            raise AssertionError('fixture did not release captured snapshot')
                    return result
                gateway.calls.clear()
                with patch.object(gateway, 'read', side_effect=read):
                    service._request_item_assignments('work')
                    service.reads.pump()
                    self.assertTrue(started.wait(5))
                    flight = service.reads.item_flights['work']
                    self.assertIn(aid, flight.buffers)
                    epoch = service.reads.epochs['item-assignment-source', 'work']
                    for identity in replay_ids:
                        notice_row = row(identity, {'a': 2000, 'b': 3000, 'c': 4000}[identity])
                        if identity != 'a':
                            gateway.rows['attests', aid].append(deepcopy(notice_row))
                        self.assertTrue(service.change(dict(resource='attests', op='upsert',
                            refs={'attestId': identity, 'assignmentId': aid},
                            payload=notice_row, **{'class': 'attest.filed'})))
                        service._publish()
                    self.assertEqual(expected,
                        service.model.item_contributions.entries[aid].attests.count)
                    self.assertEqual(epoch, service.reads.epochs['item-assignment-source', 'work'])
                    self.assertEqual(2, flight.notice_count)
                    self.assertFalse(service.reads.pending)
                    release.set()
                    kind, result = service.events.get(timeout=5)
                    # The complete result is queued, not yet locally applied.
                    self.assertEqual('read', kind)
                    self.assertTrue(result[2].complete, result[2].error)
                    self.assertFalse(flight.closed)
                    self.assertEqual(2, flight.notice_count)
                    service._handle(kind, result)
                    self.settle(service)
                self.assertTrue(flight.closed)
                self.assertEqual({}, flight.buffers)
                self.assertNotIn('work', service.reads.item_flights)
                self.assertEqual(0, service.reads.epoch_discards)
                self.assertEqual(1, sum(call[0] == 'membership' for call in gateway.calls))
                self.assertEqual(1, sum(call[:2] == ('read', 'attests') and call[-1] == aid
                                        for call in gateway.calls))
                fresh_gateway = ItemReads()
                fresh_gateway.rows = deepcopy(gateway.rows)
                fresh = self.load(fresh_gateway)
                assert_publication_equal(self, fresh.current, service.current,
                                         'b4-flight-count-' + aid + '-' + str(expected))
                assert_replay_current(self, service, baseline,
                                     'b4-flight-count-' + aid + '-' + str(expected) + '-replay')

    def test_reconstructed_closed_scope_keeps_live_notice_through_read_and_retry(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                gateway = ItemReads()
                service = self.load(gateway)
                baseline = deepcopy(service.current)
                prior = service.model.item_contributions.entries['first']
                gateway.calls.clear()
                service._refresh('assignments')
                service.reads.pump()
                service._handle(*service.events.get(timeout=5))
                inventory = service.model.inventory('attests', 'first')
                self.assertFalse(service.model.closed_sources_released)
                self.assertTrue(inventory.needs_source_read)
                self.assertEqual(prior.attests.count, inventory.folded['count'])
                self.assertEqual(prior.detail_fingerprint, inventory.fingerprint)
                self.assertIn(('attests', 'first', None), service.reads.pending)
                # The required read also discovers an eligible append whose
                # notice was missed. Its count must enter current provenance
                # before the other construction scopes finish.
                gateway.rows['attests', 'first'].append(dict.fromkeys(ATTEST_FIELDS) | dict(
                    id='read-only-append', assignmentId='first', kind='progress',
                    bySession='author', ts=2000, commitRefs=[]))
                started, release = Event(), Event()
                self.addCleanup(release.set)
                original = gateway.read
                def read(resource, scope=None):
                    result = original(resource, scope)
                    if (resource, scope) == ('attests', 'first'):
                        started.set()
                        if not release.wait(5):
                            raise AssertionError('fixture did not release reconstructed scope')
                        if partial:
                            return Read([], False, result.at, result.source, 'fixture-partial')
                    return result
                with patch.object(gateway, 'read', side_effect=read):
                    # This prioritizes the already discovered scope; it does
                    # not add a second read or change its epoch.
                    service.reads.schedule('attests', 'first', priority=True)
                    service.reads.pump()
                    self.assertTrue(started.wait(5))
                    row = dict.fromkeys(ATTEST_FIELDS) | dict(id='reconstructed-live',
                        assignmentId='first', kind='progress', bySession='author',
                        ts=4000, commitRefs=[])
                    gateway.rows['attests', 'first'].append(deepcopy(row))
                    notice = dict(resource='attests', op='upsert',
                        refs={'attestId': row['id'], 'assignmentId': 'first'},
                        payload=row, **{'class': 'attest.filed'})
                    pending, epochs = dict(service.reads.pending), dict(service.reads.epochs)
                    calls = list(gateway.calls)
                    for _ in range(2):
                        self.assertTrue(service.change(notice))
                    self.assertEqual(prior.attests.count + 1, inventory.folded['count'])
                    self.assertEqual(prior.attests.count + 1,
                        service.model.item_contributions.entries['first'].attests.count)
                    self.assertEqual(pending, service.reads.pending)
                    self.assertEqual(epochs, dict(service.reads.epochs))
                    self.assertEqual(calls, gateway.calls)
                    service._publish()
                    release.set()
                    service._handle(*service.events.get(timeout=5))
                expected = prior.attests.count + (1 if partial else 2)
                self.assertEqual(expected, inventory.folded['count'])
                self.assertEqual(expected,
                    service.model.item_contributions.entries['first'].attests.count)
                self.assertFalse(inventory.needs_source_read)
                if partial:
                    self.assertFalse(inventory.complete)
                    self.assertTrue(inventory.stale)
                    service._publish()
                    deadline = service.read_retries['attests', 'first', None][0]
                    with patch('atc_service.time.monotonic', return_value=deadline + 1):
                        service._retry_incomplete_reads()
                self.settle(service)
                self.assertEqual(prior.attests.count + 2,
                    service.model.item_contributions.entries['first'].attests.count)
                self.assertEqual(0, service.reads.epoch_discards)
                self.assertEqual(2 if partial else 1,
                    sum(call[:2] == ('read', 'attests') and call[-1] == 'first'
                        for call in gateway.calls))
                self.assertFalse([call for call in gateway.calls if call[0] == 'membership'])
                fresh_gateway = ItemReads()
                fresh_gateway.rows = deepcopy(gateway.rows)
                fresh = self.load(fresh_gateway)
                label = 'b4-reconstructed-scope-' + str(partial)
                assert_publication_equal(self, fresh.current, service.current, label)
                assert_replay_current(self, service, baseline, label + '-replay')

    def test_failed_item_read_releases_buffer_without_erasing_live_attest(self):
        gateway = ItemReads()
        service = self.load(gateway)
        started, release = Event(), Event()
        self.addCleanup(release.set)
        original = gateway.read
        def read(resource, scope=None):
            result = original(resource, scope)
            if (resource, scope) == ('attests', 'first'):
                started.set()
                if not release.wait(5):
                    raise AssertionError('fixture did not release failed snapshot')
                return Read(result.rows, False, result.at, result.source, 'fixture-partial')
            return result
        with patch.object(gateway, 'read', side_effect=read):
            service._request_item_assignments('work')
            service.reads.pump()
            self.assertTrue(started.wait(5))
            flight = service.reads.item_flights['work']
            row = dict.fromkeys(ATTEST_FIELDS) | dict(id='live', assignmentId='first',
                kind='progress', bySession='author', ts=4000, commitRefs=[])
            gateway.rows['attests', 'first'].append(deepcopy(row))
            service.change(dict(resource='attests', op='upsert',
                refs={'attestId': 'live', 'assignmentId': 'first'}, payload=row,
                **{'class': 'attest.filed'}))
            live = deepcopy(service.model.item_contributions.entries['first'])
            self.assertEqual(2, live.attests.count)
            self.assertEqual(1, flight.notice_count)
            release.set()
            service._handle(*service.events.get(timeout=5))
        self.assertEqual(live, service.model.item_contributions.entries['first'])
        self.assertEqual(4000, service.model.session_activity['author'])
        self.assertTrue(flight.closed)
        self.assertEqual({}, flight.buffers)
        self.assertNotIn('work', service.reads.item_flights)
        self.assertIn(('item-assignment-source', 'work', None), service.read_retries)
        self.assertIn('work', service.model.item_assignment_pending)
        service._publish()
        self.assertFalse(service.current['items'][0]['evidence']['assignments']['complete'])
        # Retry is a distinct attempt and starts from the now-complete source.
        service.read_retries.clear()
        service.reads.schedule('item-assignment-source', 'work', priority=True)
        self.settle(service)
        fresh_gateway = ItemReads()
        fresh_gateway.rows = deepcopy(gateway.rows)
        fresh = self.load(fresh_gateway)
        assert_publication_equal(self, fresh.current, service.current, 'b4-flight-failure-retry')

    def test_failed_scoped_read_keeps_close_and_live_attest_through_bounded_retry(self):
        for failure in ('partial', 'timeout'):
            with self.subTest(failure=failure):
                gateway = ItemReads()
                gateway.rows['assignments', None] = [dict.fromkeys(ASSIGNMENT_FIELDS) | row
                    for row in gateway.rows['assignments', None]]
                service = self.load(gateway)
                baseline = deepcopy(service.current)
                assignment = next(row for row in gateway.rows['assignments', None]
                                  if row['id'] == 'open')
                started, release = Event(), Event()
                self.addCleanup(release.set)
                original = gateway.read
                def read(resource, scope=None):
                    result = original(resource, scope)
                    if (resource, scope) == ('attests', 'open'):
                        started.set()
                        if not release.wait(5):
                            raise AssertionError('fixture did not release failed scoped read')
                        if failure == 'timeout':
                            raise TimeoutError('fixture timeout')
                        return Read(result.rows, False, result.at, result.source, 'fixture-partial')
                    return result
                gateway.calls.clear()
                with patch.object(gateway, 'read', side_effect=read):
                    service.reads.schedule('attests', 'open')
                    service.reads.pump()
                    self.assertTrue(started.wait(5))
                    flight = service.reads.attest_flights['open']
                    epoch = service.reads.epochs['attests', 'open']
                    assignment.update(state='closed', closedAt=100)
                    service.change(dict(resource='assignments', op='upsert',
                        refs={'assignmentId': 'open', 'workItemId': 'work'},
                        payload=deepcopy(assignment), **{'class': 'assignment.closed'}))
                    notice = dict.fromkeys(ATTEST_FIELDS) | dict(id='during-failure',
                        assignmentId='open', kind='progress', bySession='author',
                        ts=4000, commitRefs=[{'commit': 'after-close'}])
                    gateway.rows['attests', 'open'].append(deepcopy(notice))
                    service.change(dict(resource='attests', op='upsert', payload=notice,
                        refs={'attestId': notice['id'], 'assignmentId': 'open'},
                        **{'class': 'attest.filed'}))
                    service._publish()
                    before_failure = service.model.item_contributions.entries['open']
                    self.assertEqual('closed', before_failure.state)
                    self.assertEqual(2, before_failure.attests.count)
                    self.assertEqual(epoch, service.reads.epochs['attests', 'open'])
                    self.assertEqual(1, flight.notice_count)
                    release.set()
                    service._handle(*service.events.get(timeout=5))
                self.assertTrue(flight.closed)
                self.assertEqual({}, flight.buffers)
                self.assertEqual({}, service.reads.attest_flights)
                self.assertEqual(before_failure, service.model.item_contributions.entries['open'])
                self.assertEqual(4000, service.model.session_activity['author'])
                self.assertEqual(0, service.reads.epoch_discards)
                self.assertIn('work', service.model.item_assignment_pending)
                # Existing partial-source recovery may read the item. Its
                # first failure must retain the same live effects and use B3,
                # not retry immediately or retain either attempt's buffer.
                gateway.fail_member = 'last'
                self.settle(service)
                self.assertEqual({}, service.reads.item_flights)
                self.assertEqual(before_failure, service.model.item_contributions.entries['open'])
                self.assertFalse(service.current['items'][0]['evidence']['assignments']['complete'])
                retry_job = ('item-assignment-source', 'work', None)
                deadline, _ = service.read_retries[retry_job]
                calls_before_retry = list(gateway.calls)
                with patch('atc_service.time.monotonic', return_value=deadline - 0.001):
                    service._retry_incomplete_reads()
                self.assertNotIn(retry_job, service.reads.pending)
                self.assertEqual(calls_before_retry, gateway.calls)
                gateway.fail_member = None
                with patch('atc_service.time.monotonic', return_value=deadline):
                    service._retry_incomplete_reads()
                self.assertIn(retry_job, service.reads.pending)
                self.settle(service)
                self.assertNotIn(retry_job, service.read_retries)
                self.assertEqual(set(), service.model.item_assignment_pending)
                self.assertEqual(2, gateway.calls.count(('membership', 'work')))
                self.assertEqual(2, gateway.calls.count(('read', 'attests', 'open')))
                self.assertEqual(2, service.model.item_contributions.entries['open'].attests.count)
                self.assertNotIn(('attests', 'open'), service.model.inventories)
                fresh_gateway = ItemReads()
                fresh_gateway.rows = deepcopy(gateway.rows)
                fresh = self.load(fresh_gateway)
                label = 'r28-scoped-failure-close-retry-' + failure
                assert_publication_equal(self, fresh.current, service.current, label)
                assert_replay_current(self, service, baseline, label + '-replay')

    def test_scoped_attest_snapshot_replays_without_item_fanout_or_epoch_restart(self):
        for initial_ids, replay_ids, expected in (
                (('m', 'a'), ('b', 'c'), 4),
                (('m', 'a'), ('a', 'c'), 3),
                ((), ('c',), 1)):
            with self.subTest(initial=initial_ids, replay=replay_ids):
                gateway = ItemReads()
                def row(key):
                    return dict.fromkeys(ATTEST_FIELDS) | dict(id=key, assignmentId='open',
                        kind='progress', bySession='author', commitRefs=[],
                        ts={'m': 1000, 'a': 2000, 'b': 3000, 'c': 4000}[key])
                gateway.rows['attests', 'open'] = [row(key) for key in initial_ids]
                service = self.load(gateway)
                baseline = deepcopy(service.current)
                started, release = Event(), Event()
                self.addCleanup(release.set)
                original = gateway.read
                def read(resource, scope=None):
                    result = original(resource, scope)
                    if (resource, scope) == ('attests', 'open'):
                        started.set()
                        if not release.wait(5):
                            raise AssertionError('fixture did not release scoped snapshot')
                    return result
                gateway.calls.clear()
                with patch.object(gateway, 'read', side_effect=read):
                    service.reads.schedule('attests', 'open')
                    service.reads.pump()
                    self.assertTrue(started.wait(5))
                    flight = service.reads.attest_flights['open']
                    epoch = service.reads.epochs['attests', 'open']
                    for identity in replay_ids:
                        notice_row = row(identity)
                        if identity not in initial_ids:
                            gateway.rows['attests', 'open'].append(deepcopy(notice_row))
                        service.change(dict(resource='attests', op='upsert',
                            refs={'attestId': identity, 'assignmentId': 'open'},
                            payload=notice_row, **{'class': 'attest.filed'}))
                        service._publish()
                    self.assertEqual(epoch, service.reads.epochs['attests', 'open'])
                    self.assertEqual(set(), service.model.item_assignment_pending)
                    self.assertEqual(expected, service.model.inventory('attests', 'open').folded['count'])
                    release.set()
                    event = service.events.get(timeout=5)
                    self.assertFalse(flight.closed)
                    self.assertEqual(len(replay_ids), flight.notice_count)
                    service._handle(*event)
                    self.settle(service)
                self.assertEqual([('read', 'attests', 'open')], gateway.calls)
                self.assertEqual(expected, service.model.inventory('attests', 'open').folded['count'])
                self.assertEqual(expected, service.model.item_contributions.entries['open'].attests.count)
                self.assertEqual({}, service.reads.attest_flights)
                self.assertEqual({}, flight.buffers)
                self.assertEqual(0, service.reads.epoch_discards)
                fresh_gateway = ItemReads()
                fresh_gateway.rows = deepcopy(gateway.rows)
                fresh = self.load(fresh_gateway)
                assert_publication_equal(self, fresh.current, service.current,
                                         'b4-scoped-flight-' + str(expected))
                assert_replay_current(self, service, baseline,
                                     'b4-scoped-flight-' + str(expected) + '-replay')

    def test_scoped_attest_read_survives_close_and_reopen_without_member_reads(self):
        for states in (('closed',), ('closed', 'open'), ('closed', 'open', 'closed')):
            with self.subTest(states=states):
                gateway = ItemReads()
                gateway.rows['assignments', None] = [dict.fromkeys(ASSIGNMENT_FIELDS) | row
                    for row in gateway.rows['assignments', None]]
                service = self.load(gateway)
                baseline = deepcopy(service.current)
                assignment = next(row for row in gateway.rows['assignments', None]
                                  if row['id'] == 'open')
                started, release = Event(), Event()
                self.addCleanup(release.set)
                original = gateway.read
                def read(resource, scope=None):
                    result = original(resource, scope)
                    if (resource, scope) == ('attests', 'open'):
                        started.set()
                        if not release.wait(5):
                            raise AssertionError('fixture did not release closing scope read')
                    return result
                gateway.calls.clear()
                with patch.object(gateway, 'read', side_effect=read):
                    service.reads.schedule('attests', 'open')
                    service.reads.pump()
                    self.assertTrue(started.wait(5))
                    flight = service.reads.attest_flights['open']
                    epoch = service.reads.epochs['attests', 'open']
                    for position, state in enumerate(states):
                        assignment.update(state=state, closedAt=100 + position if state == 'closed' else None)
                        payload = deepcopy(assignment)
                        payload.pop('commitRefCorrections', None)
                        service.change(dict(resource='assignments', op='upsert',
                            refs={'assignmentId': 'open', 'workItemId': 'work'}, payload=payload,
                            **{'class': 'assignment.closed' if state == 'closed' else 'assignment.reopened'}))
                        service._publish()
                        self.assertEqual(set(), service.model.item_assignment_pending)
                        self.assertEqual(epoch, service.reads.epochs['attests', 'open'])
                    notice = dict.fromkeys(ATTEST_FIELDS) | dict(id='after-transition',
                        assignmentId='open', kind='progress', bySession='author',
                        ts=4000, commitRefs=[{'commit': 'new-ref'}])
                    gateway.rows['attests', 'open'].append(deepcopy(notice))
                    service.change(dict(resource='attests', op='upsert', payload=notice,
                        refs={'attestId': notice['id'], 'assignmentId': 'open'},
                        **{'class': 'attest.filed'}))
                    service._publish()
                    self.assertEqual(2, service.model.item_contributions.entries['open'].attests.count)
                    self.assertEqual(1, flight.notice_count)
                    release.set()
                    service._handle(*service.events.get(timeout=5))
                    self.settle(service)
                self.assertEqual([('read', 'attests', 'open')], gateway.calls)
                self.assertEqual(0, service.reads.epoch_discards)
                self.assertEqual({}, service.reads.attest_flights)
                contribution = service.model.item_contributions.entries['open']
                self.assertEqual(states[-1], contribution.state)
                self.assertEqual(2, contribution.attests.count)
                if states[-1] == 'closed':
                    self.assertNotIn(('attests', 'open'), service.model.inventories)
                    self.assertNotIn('open', service.model.inventory('assignments').rows)
                fresh_gateway = ItemReads()
                fresh_gateway.rows = deepcopy(gateway.rows)
                fresh = self.load(fresh_gateway)
                label = 'b4-scoped-transition-' + '-'.join(states)
                assert_publication_equal(self, fresh.current, service.current, label)
                assert_replay_current(self, service, baseline, label + '-replay')

    def test_scoped_result_cannot_revive_exited_item_or_contributor(self):
        for exit_kind in ('item', 'contributor'):
            for queued in (False, True):
                with self.subTest(exit_kind=exit_kind, queued=queued):
                    gateway = ItemReads()
                    service = self.load(gateway)
                    baseline = deepcopy(service.current)
                    replacement = None
                    if exit_kind == 'contributor':
                        # Prepare an actual complete membership/detail/fold
                        # result. Apply its model commit while the independent
                        # scoped result is delayed; no direct store deletion.
                        rows = gateway.rows['assignments', None]
                        gateway.rows['assignments', None] = [row for row in rows if row['id'] != 'first']
                        replacement = service.reads._item_assignments(
                            'work', service._read_plan('item-assignment-source', 'work'))
                        gateway.rows['assignments', None] = rows
                        self.assertTrue(replacement.complete)
                    started, release = Event(), Event()
                    self.addCleanup(release.set)
                    original = gateway.read
                    def read(resource, scope=None):
                        result = original(resource, scope)
                        if (resource, scope) == ('attests', 'first'):
                            started.set()
                            if not release.wait(5):
                                raise AssertionError('fixture did not release exited scope')
                        return result
                    gateway.calls.clear()
                    with patch.object(gateway, 'read', side_effect=read):
                        service.reads.schedule('attests', 'first')
                        service.reads.pump()
                        self.assertTrue(started.wait(5))
                        flight = service.reads.attest_flights['first']
                        self.assertEqual(('work',), flight.owner)
                        notice = dict.fromkeys(ATTEST_FIELDS) | dict(id='before-exit',
                            assignmentId='first', kind='progress', bySession='author',
                            ts=4000, commitRefs=[])
                        service.change(dict(resource='attests', op='upsert', payload=notice,
                            refs={'attestId': notice['id'], 'assignmentId': 'first'},
                            **{'class': 'attest.filed'}))
                        self.assertEqual(1, flight.notice_count)
                        event = None
                        if queued:
                            release.set()
                            event = service.events.get(timeout=5)
                            self.assertFalse(flight.closed)
                        if exit_kind == 'item':
                            service.model.accept('work-items', None,
                                                 Read([], True, 5000, 'published-read'))
                        else:
                            service.model.replace_item_assignments('work', replacement)
                        service._publish()
                        self.assertTrue(flight.closed)
                        self.assertEqual({}, flight.buffers)
                        self.assertEqual({}, flight.opened_at)
                        self.assertEqual({}, flight.read_completed_at)
                        self.assertEqual((), flight.owner)
                        after_exit = deepcopy(service.current)
                        release.set()
                        service._handle(*(event if event is not None else service.events.get(timeout=5)))
                        self.settle(service)
                    self.assertEqual([('read', 'attests', 'first')], gateway.calls)
                    self.assertEqual({}, service.reads.attest_flights)
                    self.assertNotIn('first', service.model.item_contributions.entries)
                    self.assertNotIn(('attests', 'first'), service.model.inventories)
                    self.assertNotIn(('attests', 'first', None), service.read_retries)
                    self.assertEqual(set(), service.model.item_assignment_pending)
                    label = 'r28-scope-exit-' + exit_kind + '-queued-' + str(queued)
                    assert_publication_equal(self, after_exit, service.current, label)
                    assert_replay_current(self, service, baseline, label + '-replay')

    def test_item_exit_releases_protection_before_delayed_read_result(self):
        gateway = ItemReads()
        service = self.load(gateway)
        started, release = Event(), Event()
        self.addCleanup(release.set)
        original = gateway.read
        def read(resource, scope=None):
            result = original(resource, scope)
            if (resource, scope) == ('attests', 'first'):
                started.set()
                if not release.wait(5):
                    raise AssertionError('fixture did not release abandoned snapshot')
            return result
        with patch.object(gateway, 'read', side_effect=read):
            service._request_item_assignments('work')
            service.reads.pump()
            self.assertTrue(started.wait(5))
            flight = service.reads.item_flights['work']
            row = dict.fromkeys(ATTEST_FIELDS) | dict(id='live', assignmentId='first',
                kind='progress', bySession='author', ts=4000, commitRefs=[])
            service.change(dict(resource='attests', op='upsert',
                refs={'attestId': 'live', 'assignmentId': 'first'}, payload=row,
                **{'class': 'attest.filed'}))
            self.assertEqual(1, flight.notice_count)
            service.model.accept('work-items', None, Read([], True, 1000, 'published-read'))
            service._publish()
            self.assertTrue(flight.closed)
            self.assertEqual({}, flight.buffers)
            self.assertNotIn('work', service.reads.item_flights)
            release.set()
            service._handle(*service.events.get(timeout=5))
        self.assertNotIn(('item-assignment-source', 'work', None), service.read_retries)
        self.assertNotIn('work', service.model.item_assignment_pending)
        self.assertEqual([], service.current['items'])

    def test_reduced_detail_pin_does_not_change_attest_source_eligibility(self):
        gateway = ItemReads()
        service = self.load(gateway)
        closed = service.model.closed_items['work']
        self.assertTrue(closed['attestsRead'])
        # Compact reductions retain source facts but cannot regenerate the
        # discarded detail text. That is not a source-skip transition.
        closed['detailsKnown'] = False
        gateway.calls.clear()
        with patch.object(service, '_request_item_assignments') as item_read:
            service._reconcile_item_attests(service.model.work_items()['work'])
            item_read.assert_not_called()
        self.assertEqual([], gateway.calls)
        self.assertEqual(set(), service.model.item_assignment_pending)

    def test_new_and_held_close_preserve_released_facts_without_member_fanout(self):
        gateway = ItemReads()
        service = self.load(gateway)
        self.assertTrue(service.model.closed_sources_released)
        row = dict.fromkeys(ASSIGNMENT_FIELDS) | dict(id='new-current', workItemId='work', holderKey='author',
                   state='open', effectKind='code', openedAt=30,
                   closedAt=None, commitRefCorrections=[], files=[])
        gateway.rows['assignments', None].insert(0, row)
        gateway.rows['attests', row['id']] = []
        for state in ('open', 'closed'):
            with self.subTest(state=state):
                row.update(state=state, closedAt=40 if state == 'closed' else None)
                baseline = deepcopy(service.current)
                gateway.calls.clear()
                service.change(dict(resource='assignments', op='upsert',
                    refs={'assignmentId': row['id'], 'workItemId': 'work'},
                    payload=deepcopy(row),
                    **{'class': 'assignment.opened' if state == 'open' else 'assignment.closed'}))
                self.settle(service)
                self.assert_no_member_fanout(gateway.calls, row['id'])
                fresh_gateway = ItemReads()
                fresh_gateway.rows = deepcopy(gateway.rows)
                fresh = self.load(fresh_gateway)
                assert_publication_equal(self, fresh.current, service.current,
                                         'b4-ordinary-' + state)
                assert_replay_current(self, service, baseline, 'b4-ordinary-' + state + '-replay')

    def test_reopen_uses_existing_fold_without_item_member_reads(self):
        gateway = ItemReads()
        # Membership and notices share the pinned complete public projection.
        gateway.rows['assignments', None] = [dict.fromkeys(ASSIGNMENT_FIELDS) | row
                                            for row in gateway.rows['assignments', None]]
        service = self.load(gateway)
        row = next(row for row in gateway.rows['assignments', None] if row['id'] == 'last')
        row.update(state='open', closedAt=None)
        before = deepcopy(service.current)
        gateway.calls.clear()
        payload = deepcopy(row)
        payload.pop('commitRefCorrections', None)
        service.change(dict(resource='assignments', op='upsert',
            refs={'assignmentId': row['id'], 'workItemId': 'work'}, payload=payload,
            **{'class': 'assignment.reopened'}))
        self.settle(service)
        self.assertEqual([], gateway.calls)
        fresh_gateway = ItemReads()
        fresh_gateway.rows = deepcopy(gateway.rows)
        fresh = self.load(fresh_gateway)
        assert_publication_equal(self, fresh.current, service.current, 'b4-reopen')
        assert_replay_current(self, service, before, 'b4-reopen-replay')
        self.assertEqual(set(), service.model.item_assignment_pending)
        self.assertEqual({}, service.model.inventory('attests', 'last').rows)

    def test_released_attest_uses_current_ownership_and_zero_reads(self):
        for explicit_owner in (False, True):
            with self.subTest(explicit_owner=explicit_owner):
                gateway = ItemReads()
                assignment = next(row for row in gateway.rows['assignments', None]
                                  if row['id'] == 'first')
                assignment.update(state='open', closedAt=None)
                service = self.load(gateway)
                row = dict.fromkeys(ATTEST_FIELDS) | dict(id='later', assignmentId='first',
                    kind='progress', bySession='author', ts=500, commitRefs=[], note='released notice')
                gateway.rows['attests', 'first'].append(deepcopy(row))
                assignment.update(state='closed', closedAt=600)
                # An independently required assignment observation can learn
                # the later closure before the earlier attest notice reaches
                # the model. The progress was filed by its holder while open,
                # not invented as a new progress write on a closed assignment.
                released = service.model.apply_assignment_projection(
                    assignment, at=1000, source='published-detail')
                self.assertIsNotNone(released)
                service._forget_scopes(released)
                service._publish()
                before = deepcopy(service.current)
                pin = service.model.closed_items['work']['detailFingerprint']
                revision = service.model.details_revision
                refs = {'attestId': row['id'], 'assignmentId': 'first'}
                if explicit_owner:
                    refs['workItemId'] = 'work'
                notice = dict(resource='attests', op='upsert', refs=refs, payload=deepcopy(row),
                              **{'class': 'attest.filed'})
                gateway.calls.clear()
                self.assertTrue(service.change(notice))
                self.settle(service)
                self.assertEqual([], gateway.calls)
                self.assertEqual(set(), service.model.item_assignment_pending)
                self.assertEqual({}, service.attest_activity_pending)
                self.assertNotIn(('attests', 'first'), service.model.inventories)
                self.assertEqual(pin, service.model.closed_items['work']['detailFingerprint'])
                self.assertEqual(revision + 1, service.model.details_revision)
                fresh_gateway = ItemReads()
                fresh_gateway.rows = deepcopy(gateway.rows)
                fresh = self.load(fresh_gateway)
                assert_publication_equal(self, fresh.current, service.current,
                                         'b4-released-attest-' + str(explicit_owner))
                assert_replay_current(self, service, before,
                                      'b4-released-attest-replay-' + str(explicit_owner))
                self.assertTrue(service.change(notice))
                self.settle(service)
                self.assertEqual([], gateway.calls)
                self.assertEqual(revision + 1, service.model.details_revision)
                assert_publication_equal(self, fresh.current, service.current,
                                         'b4-released-attest-duplicate-' + str(explicit_owner))

    def test_summary_backed_duplicate_counts_keep_review_timing_stage_and_ring_exact(self):
        gateway = ItemReads()
        item = summarized_work(attest_counts={'completion': 1, 'verdict': 2},
            verdict_counts={'tests-passed': 1, 'reviewed-clean': 1}, holders=[], refs=[])
        producer = dict.fromkeys(ASSIGNMENT_FIELDS) | dict(id='producer', workItemId='work',
            holderKey='author', state='closed', effectKind='code', openedAt=1, closedAt=100,
            reviewsAssignmentId=None, commitRefCorrections=[], files=[])
        review = dict.fromkeys(ASSIGNMENT_FIELDS) | dict(id='review', workItemId='work',
            holderKey='other', state='closed', effectKind='review', openedAt=110, closedAt=200,
            reviewsAssignmentId='producer', commitRefCorrections=[], files=[])

        def attest(identity, assignment, author, timestamp, kind, verdict=None):
            return dict.fromkeys(ATTEST_FIELDS) | dict(id=identity, assignmentId=assignment,
                bySession=author, ts=timestamp, kind=kind, verdictKind=verdict, commitRefs=[])

        tested = attest('tested', 'producer', 'author', 10, 'verdict', 'tests-passed')
        done = attest('done', 'producer', 'author', 100, 'completion')
        clean = attest('clean', 'review', 'other', 200, 'verdict', 'reviewed-clean')
        gateway.rows = {
            ('work-items', None): [item], ('assignments', None): [review, producer],
            ('roles', None): [],
            ('sessions', None): [dict(sessionKey=key, state='active', displayName=key)
                                 for key in ('author', 'other')],
            ('attests', 'producer'): [tested, done], ('attests', 'review'): [clean],
            **{(resource, key): [] for resource in ('turns', 'wakes', 'messages')
               for key in ('author', 'other')},
        }
        version = {'version': '0.1.15', 'build': 3866, 'sha': 'b09a64a9'}
        service = self.load(gateway, version)
        before = deepcopy(service.current)
        current = before['items'][0]
        self.assertEqual(5, current['stage'])
        self.assertEqual('yellow', current['ring'])
        self.assertEqual('known', current['evidence']['stage']['status'])
        self.assertTrue(current['evidence']['stage']['complete'])
        self.assertEqual({'total': 3, 'byKind': {'completion': 1, 'verdict': 2},
                          'byVerdict': {'tests-passed': 1, 'reviewed-clean': 1}},
                         current['evidenceCounts'])
        self.assertTrue(service._item_attest_counts_match(item, require_fresh=True))
        self.assertNotIn(('attests', 'producer'), service.model.inventories)
        self.assertNotIn(('attests', 'review'), service.model.inventories)
        revision = service.model.details_revision
        gateway.calls.clear()
        # The tested notice is older than its current maximum. Done and clean
        # are already-counted frontier IDs; all repeats must remain exact.
        for index, row in enumerate((tested, done, clean, tested, clean)):
            with self.subTest(index=index, identity=row['id']):
                self.assertTrue(service.change(dict(resource='attests', op='upsert',
                    refs={'attestId': row['id'], 'assignmentId': row['assignmentId']},
                    payload=deepcopy(row), **{'class': 'attest.filed'})))
                self.settle(service)
                self.assertEqual([], gateway.calls)
                self.assertEqual(revision, service.model.details_revision)
                self.assertTrue(service._item_attest_counts_match(item, require_fresh=True))
                self.assertEqual(3, service.model.closed_items['work']['summary']['count'])
                assert_publication_equal(self, before, service.current,
                                         'r25-summary-duplicate-' + str(index))
        fresh_gateway = ItemReads()
        fresh_gateway.rows = deepcopy(gateway.rows)
        fresh = self.load(fresh_gateway, version)
        assert_publication_equal(self, fresh.current, service.current, 'r25-summary-duplicates-fresh')
        assert_replay_current(self, service, before, 'r25-summary-duplicates-replay')
