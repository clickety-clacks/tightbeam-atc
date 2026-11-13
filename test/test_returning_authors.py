"""Returning sessions recover attest activity without a global rebuild."""
from copy import deepcopy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_service import LiveService
from atc_payloads import SESSION_FIELDS, PAYLOAD_POLICIES
from atc_upstream import Read
from test_service import PublishedReads, summarized_work
from test_assignment_source_failure import publication


class ReturningReads(PublishedReads):
    def __init__(self):
        super().__init__()
        self.block = False
        self.fail = False
        self.fail_item = False
        self.entered, self.release = threading.Event(), threading.Event()
        self.active_attests = self.max_active_attests = 0
        self.rows['work-items', None][0].update(state='closed', createdAt=1, closedAt=20)
        self.rows['assignments', None][0].update(state='closed', closedAt=20)
        self.rows['sessions', None][0]['state'] = 'retired'
        for key in ('guest-one', 'guest-two'):
            self.rows['sessions', None].append(dict.fromkeys(SESSION_FIELDS) | dict(
                sessionKey=key, state='retired', rowVersion=10, displayName=key,
                archetype='reviewer-code', capabilities={}))
        self.rows['attests', 'assignment'] = [
            dict(id='first', assignmentId='assignment', kind='progress',
                 bySession='guest-one', ts=900000),
            dict(id='second', assignmentId='assignment', kind='progress',
                 bySession='guest-two', ts=800000)]

    def assignment_membership(self, wid, *, include_rows=False):
        result = super().assignment_membership(wid, include_rows=include_rows)
        if include_rows and self.fail_item:
            result.complete = False
            result.error = 'TimeoutError'
        return result

    def read(self, resource, scope=None):
        if resource != 'attests':
            return super().read(resource, scope)
        self.active_attests += 1
        self.max_active_attests = max(self.max_active_attests, self.active_attests)
        try:
            result = super().read(resource, scope)
            if self.block:
                self.entered.set()
                if not self.release.wait(5):
                    raise TimeoutError('test did not release attest read')
            if self.fail:
                result.complete = False
                result.error = 'TimeoutError'
            return result
        finally:
            self.active_attests -= 1


class ReturningAuthorTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_service.now_ms', return_value=5000000000)
        clock.start()
        self.addCleanup(clock.stop)

    def load(self, gateway):
        service = LiveService(gateway, workers=4)
        service.connected = True
        self.addCleanup(service.stop)
        self.addCleanup(gateway.release.set)
        service.rebuild()
        self.drain(service)
        return service

    def drain(self, service):
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            service._handle(*service.events.get(timeout=5))
        service._publish()

    def returns(self, service, keys):
        for row in service.gateway.rows['sessions', None]:
            if row['sessionKey'] in keys:
                row.update(state='active', rowVersion=11)
        service._refresh('sessions')
        service.reads.pump()
        service._handle(*service.events.get(timeout=5))
        service._publish()

    def fresh(self, service):
        gateway = ReturningReads()
        gateway.rows = deepcopy(service.gateway.rows)
        return self.load(gateway)

    def test_nonholder_authors_coalesce_and_match_fresh_full_publication(self):
        gateway = ReturningReads()
        service = self.load(gateway)
        self.assertEqual({}, service.model.closed_items['work']['summary']['authors'])
        gateway.calls.clear()
        with patch.object(service, 'rebuild', side_effect=AssertionError('global recovery')):
            self.returns(service, {'guest-one', 'guest-two'})
            self.assertEqual({'guest-one': None, 'guest-two': None}, service.returning_authors)
            self.assertTrue(all(row['idleSince'] is None for row in service.current['agents']))
            self.drain(service)
        self.assertEqual([], [c for c in gateway.calls if c[0] == 'membership'])
        self.assertEqual(1, gateway.calls.count(('read', 'assignments', None)))
        self.assertEqual(1, gateway.calls.count(('read', 'attests', 'assignment')))
        self.assertNotIn(('read', 'turns', 'agent s_author'), gateway.calls)
        self.assertEqual({'guest-one': 900000, 'guest-two': 800000},
                         {a['sessionKey']: a['idleSince'] for a in service.current['agents']})
        self.assertEqual({}, service.returning_authors)
        self.assertEqual(1, gateway.max_active_attests)
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))
        self.assertNotIn(('attests', 'assignment'), service.model.inventories)
        self.assertEqual({}, service.model.inventory('assignments').rows)

    def test_partial_recovery_keeps_last_value_then_retries(self):
        gateway = ReturningReads()
        service = self.load(gateway)
        gateway.fail = True
        self.returns(service, {'guest-one'})
        self.drain(service)
        self.assertIsNone(service.current['agents'][0]['idleSince'])
        self.assertFalse(service.current['agents'][0]['evidence']['idleFor']['complete'])
        self.assertEqual({}, service.model.closed_items['work']['summary']['authors'])
        job = ('returning-authors', None, None)
        self.assertIn(job, service.read_retries)
        gateway.fail = False
        service.read_retries[job] = (0, 2)
        service._retry_incomplete_reads()
        self.drain(service)
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))
        self.assertNotIn(job, service.read_retries)

    def test_return_during_pass_waits_for_its_own_complete_coverage(self):
        gateway = ReturningReads()
        service = self.load(gateway)
        gateway.block = True
        self.returns(service, {'guest-one'})
        service.reads.pump()
        self.assertTrue(gateway.entered.wait(2))
        # Session payloads continue to publish while the attest read is blocked.
        returned = next(row for row in gateway.rows['sessions', None] if row['sessionKey'] == 'guest-two')
        returned.update(state='active', rowVersion=11)
        service.policies = PAYLOAD_POLICIES
        service.change(dict(resource='sessions', op='upsert', **{'class': 'session.updated'},
            refs={'sessionKey': 'guest-two'}, payload=deepcopy(returned)))
        service._publish()
        self.assertEqual(2, len(service.current['agents']))
        self.assertTrue(all(row['idleSince'] is None for row in service.current['agents']))
        gateway.block = False
        gateway.release.set()
        self.drain(service)
        # Align the session identity observation source with the fresh collection.
        service._refresh('sessions')
        self.drain(service)
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))
        self.assertEqual(1, gateway.max_active_attests)
        self.assertEqual(2, gateway.calls.count(('read', 'assignments', None)))

    def test_null_item_assignment_matches_unchanged_fresh_scope(self):
        gateway = ReturningReads()
        gateway.rows['assignments', None].append(dict(id='outside', workItemId=None,
            holderKey='agent s_author', state='closed', effectKind='code'))
        gateway.rows['attests', 'outside'] = [dict(id='external', assignmentId='outside',
            kind='progress', bySession='guest-one', ts=950000)]
        service = self.load(gateway)
        gateway.calls.clear()
        self.returns(service, {'guest-one'})
        self.drain(service)
        self.assertEqual(1, gateway.calls.count(('read', 'attests', 'outside')))
        self.assertEqual(950000, service.current['agents'][0]['idleSince'])
        self.assertEqual(2, service.returning_author_result['assignments'])
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))

    def test_returning_live_turn_restores_closed_assignment_and_authored_attachment(self):
        for assignment in ('assignment', None):
            with self.subTest(assignment=assignment):
                gateway = ReturningReads()
                service = self.load(gateway)
                gateway.rows['turns', 'guest-one'] = [dict(seq=3, sessionKey='guest-one',
                    assignmentId=assignment, createdAt=700000, startedAt=750000, endedAt=None)]
                with patch.object(service, 'rebuild', side_effect=AssertionError('global recovery')):
                    self.returns(service, {'guest-one'})
                    self.drain(service)
                fresh = self.fresh(service)
                self.assertEqual(publication(fresh.current), publication(service.current))
                self.assertNotIn(('attests', 'assignment'), service.model.inventories)

    def test_summary_ineligible_attests_are_not_added_by_return_recovery(self):
        gateway = ReturningReads()
        gateway.rows['work-items', None] = [summarized_work(holders=[])]
        service = self.load(gateway)
        gateway.calls.clear()
        self.returns(service, {'guest-one'})
        self.drain(service)
        self.assertNotIn(('membership', 'work'), gateway.calls)
        self.assertNotIn(('read', 'attests', 'assignment'), gateway.calls)
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))

    def test_retirement_discards_recovered_author_facts(self):
        gateway = ReturningReads()
        service = self.load(gateway)
        self.returns(service, {'guest-one'})
        self.drain(service)
        for row in gateway.rows['sessions', None]:
            if row['sessionKey'] == 'guest-one':
                row['state'] = 'retired'
        service._refresh('sessions')
        self.drain(service)
        self.assertEqual({}, service.returning_authors)
        self.assertEqual(set(), service.returning_authors_ready)
        part = service.model.closed_items['work']
        self.assertEqual({}, part['summary']['authors'])
        self.assertEqual({}, part['authorFacts'])

    def test_complete_source_read_invalidates_an_inflight_author_pass(self):
        gateway = ReturningReads()
        service = self.load(gateway)
        gateway.block = True
        self.returns(service, {'guest-one'})
        service.reads.pump()
        self.assertTrue(gateway.entered.wait(2))
        gateway.rows['attests', 'assignment'][0]['ts'] = 950000
        # An independent reconciliation observes the source after the pass
        # captured its old rows, without requiring a feed notice.
        service._refresh('work-items')
        service.reads.pump()
        while True:
            kind, value = service.events.get(timeout=5)
            service._handle(kind, value)
            if kind == 'read' and value[0] == ('work-items', None, None):
                break
            service.reads.pump()
        service._publish()
        self.assertIsNone(service.current['agents'][0]['idleSince'])
        gateway.block = False
        gateway.release.set()
        self.drain(service)
        self.assertGreater(service.reads.epoch_discards_by_resource['returning-authors'], 0)
        self.assertEqual(950000, service.current['agents'][0]['idleSince'])
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))

    def test_new_author_activity_and_item_replace_in_one_complete_pass(self):
        gateway = ReturningReads()
        service = self.load(gateway)
        gateway.rows['attests', 'assignment'][0]['ts'] = 950000
        gateway.fail = True
        gateway.calls.clear()
        self.returns(service, {'guest-one'})
        self.drain(service)
        self.assertIsNone(service.current['agents'][0]['idleSince'])
        self.assertEqual(900000, service.current['items'][0]['latestActivityAt'])
        job = ('returning-authors', None, None)
        self.assertIn(job, service.read_retries)
        self.assertEqual(1, gateway.calls.count(('read', 'attests', 'assignment')))
        gateway.fail = False
        service.read_retries[job] = (0, 2)
        service._retry_incomplete_reads()
        self.drain(service)
        self.assertEqual(950000, service.current['agents'][0]['idleSince'])
        self.assertEqual(950000, service.current['items'][0]['latestActivityAt'])
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))
        self.assertEqual({}, service.returning_author_items)
        # One failed pass and one successful pass each read the attest once.
        # Item reconciliation consumes the same fold without an extra read.
        self.assertEqual(2, gateway.calls.count(('read', 'attests', 'assignment')))

    def test_failed_assignment_scope_discovery_reads_no_attests(self):
        gateway = ReturningReads()
        service = self.load(gateway)
        gateway.calls.clear()
        original = gateway.read
        def fail_collection(resource, scope=None):
            result = original(resource, scope)
            if resource == 'assignments':
                result.rows = []
                result.complete = False
                result.error = 'TimeoutError'
            return result
        with patch.object(gateway, 'read', side_effect=fail_collection):
            self.returns(service, {'guest-one'})
            self.drain(service)
        self.assertFalse(any(c[:2] == ('read', 'attests') for c in gateway.calls))
        self.assertIsNone(service.current['agents'][0]['idleSince'])
        job = ('returning-authors', None, None)
        self.assertIn(job, service.read_retries)
        service.read_retries[job] = (0, 2)
        service._retry_incomplete_reads()
        self.drain(service)
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))
