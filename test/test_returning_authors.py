"""Session activity reuses current source folds without return recovery reads."""
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

    def outside(self, gateway):
        gateway.rows['assignments', None].append(dict(id='outside', workItemId=None,
            holderKey='agent s_author', state='closed', effectKind='code'))
        gateway.rows['attests', 'outside'] = [dict(id='external', assignmentId='outside',
            kind='progress', bySession='guest-one', ts=950000)]

    def assert_no_return_reads(self, gateway):
        self.assertFalse([c for c in gateway.calls if c[:2] in {
            ('read', 'assignments'), ('read', 'attests'), ('membership', 'work')}], gateway.calls)

    def test_nonholder_and_null_item_return_use_existing_fold(self):
        gateway = ReturningReads()
        self.outside(gateway)
        service = self.load(gateway)
        self.assertEqual(950000, service.model.session_activity['guest-one'])
        gateway.calls.clear()
        with patch.object(service, 'rebuild', side_effect=AssertionError('return rebuild')):
            self.returns(service, {'guest-one', 'guest-two'})
            self.drain(service)
        self.assert_no_return_reads(gateway)
        self.assertEqual({'guest-one': 950000, 'guest-two': 800000},
                         {a['sessionKey']: a['idleSince'] for a in service.current['agents']})
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))
        self.assertNotIn(('attests', 'outside'), service.model.inventories)
        self.assertEqual({}, service.model.inventory('assignments').rows)

    def test_retirement_and_time_do_not_expire_authored_activity(self):
        gateway = ReturningReads()
        self.outside(gateway)
        service = self.load(gateway)
        self.returns(service, {'guest-one'})
        self.drain(service)
        for row in gateway.rows['sessions', None]:
            if row['sessionKey'] == 'guest-one':row.update(state='retired', rowVersion=12)
        service._refresh('sessions');self.drain(service)
        with patch('atc_service.now_ms', return_value=900000000000):
            service._publish()
        self.assertEqual(950000, service.model.session_activity['guest-one'])
        self.assertFalse(service.current['agents'])
        self.assertFalse(any(resource in {'turns','wakes','messages'} and scope=='guest-one'
                             for resource,scope in service.model.inventories))

    def test_complete_replacement_lowers_then_removes_without_ledger(self):
        gateway = ReturningReads()
        self.outside(gateway)
        service = self.load(gateway)
        self.returns(service, {'guest-one'});self.drain(service)
        gateway.rows['attests', 'outside'] = []
        service.rebuild();self.drain(service)
        self.assertEqual(900000, service.model.session_activity['guest-one'])
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))
        gateway.rows['attests', 'assignment'] = [r for r in gateway.rows['attests','assignment']
                                               if r['bySession']!='guest-one']
        service.rebuild();self.drain(service)
        self.assertNotIn('guest-one', service.model.session_activity)
        self.assertEqual(publication(self.fresh(service).current), publication(service.current))

    def test_selected_turns_only_and_no_wake_message_activity(self):
        gateway = ReturningReads()
        gateway.rows['attests','assignment'] = []
        gateway.rows['sessions',None][-2].update(state='active')
        gateway.rows['turns','guest-one'] = [dict(seq=3,sessionKey='guest-one',
            createdAt=700000,startedAt=800000,endedAt=750000)]
        service=self.load(gateway)
        self.assertEqual(750000, service.model.session_activity['guest-one'])
        gateway.rows['turns','guest-one'] = []
        service._refresh('turns','guest-one');self.drain(service)
        self.assertNotIn('guest-one',service.model.session_activity)
        self.assertEqual(publication(self.fresh(service).current),publication(service.current))

    def test_summary_skip_does_not_add_authored_activity(self):
        gateway=ReturningReads()
        gateway.rows['work-items',None]=[summarized_work(holders=[])]
        service=self.load(gateway)
        gateway.calls.clear()
        self.returns(service,{'guest-one'});self.drain(service)
        self.assert_no_return_reads(gateway)
        self.assertNotIn('guest-one',service.model.session_activity)
        self.assertEqual(publication(self.fresh(service).current),publication(service.current))

    def test_live_attests_before_retire_and_return_include_null_item(self):
        gateway=ReturningReads();self.outside(gateway)
        saved={key:deepcopy(gateway.rows[key]) for key in [('attests','assignment'),('attests','outside')]}
        for key in saved:gateway.rows[key]=[]
        service=self.load(gateway)
        service.policies=PAYLOAD_POLICIES
        for key,rows in saved.items():
            gateway.rows[key]=rows
            for row in rows:
                service.change(dict(resource='attests',op='upsert',refs={'assignmentId':key[1],'id':row['id']},payload=deepcopy(row)))
            self.drain(service)
        self.assertEqual(950000,service.model.session_activity['guest-one'])
        gateway.calls.clear()
        self.returns(service,{'guest-one'});self.drain(service)
        self.assert_no_return_reads(gateway)
        self.assertEqual(950000,service.current['agents'][0]['idleSince'])
        self.assertEqual(publication(self.fresh(service).current),publication(service.current))

    def test_returning_live_turn_keeps_existing_attachment_parity(self):
        for assignment in ('assignment',None):
            with self.subTest(assignment=assignment):
                gateway=ReturningReads();service=self.load(gateway)
                gateway.rows['turns','guest-one']=[dict(seq=3,sessionKey='guest-one',assignmentId=assignment,
                    createdAt=700000,startedAt=750000,endedAt=None)]
                self.returns(service,{'guest-one'});self.drain(service)
                self.assertEqual(publication(self.fresh(service).current),publication(service.current))
