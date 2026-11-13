"""Closed source disposal through complete service read/publication cycles."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_service import LiveService
from test_service import PublishedReads, summarized_work
from test_assignment_source_failure import publication


class ClosedSourceServiceTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_service.now_ms', return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def service(self, gateway):
        service = LiveService(gateway)
        service.connected = True
        self.addCleanup(service.stop)
        service.rebuild()
        self.settle(service)
        return service

    def settle(self, service):
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            service._handle(*service.events.get(timeout=5))
        service._publish()

    def test_closed_notice_reloads_facts_and_matches_fresh_after_source_disposal(self):
        gateway = PublishedReads()
        gateway.rows['assignments', None][0]['state'] = 'closed'
        service = self.service(gateway)
        self.assertEqual({}, service.model.inventory('assignments').rows)
        self.assertNotIn(('attests', 'assignment'), service.model.inventories)
        self.assertEqual(3, service.current['freshness']['reconciliation']['active']['scopeCount'])
        self.assertEqual(0, service.current['freshness']['reconciliation']['history']['scopeCount'])
        for scopes in (service.backstop.active, service.backstop.history,
                       service.reads.epochs, service.rotation, service.read_failures):
            self.assertNotIn(('attests', 'assignment'), scopes)
        row = dict(id='later', assignmentId='assignment', kind='progress', ts=50,
                   bySession='agent s_author', note='Fresh detail')
        gateway.rows['attests', 'assignment'].append(row)
        service.change(dict(resource='attests', op='upsert', **{'class': 'attest.filed'},
                            refs={'attestId': 'later'}, payload=row))
        self.settle(service)
        fresh_gateway = PublishedReads()
        fresh_gateway.rows = deepcopy(gateway.rows)
        fresh = self.service(fresh_gateway)
        for key in ('agents', 'items', 'links', 'wakes', 'messages'):
            self.assertEqual(fresh.current[key], service.current[key])
        self.assertEqual(50, service.current['items'][0]['latestActivityAt'])
        self.assertEqual({}, service.model.inventory('assignments').rows)
        self.assertNotIn(('attests', 'assignment'), service.model.inventories)

    def test_summary_backed_detail_pin_does_not_expand_background_attest_eligibility(self):
        gateway = PublishedReads()
        gateway.rows['assignments', None][0]['state'] = 'closed'
        gateway.rows['work-items', None] = [summarized_work(holders=[])]
        service = self.service(gateway)
        self.assertNotIn(('read', 'attests', 'assignment'), gateway.calls)
        status, mismatch = service.evidence_page('work')
        self.assertEqual(409, status)
        service._handle(*service.events.get_nowait())
        gateway.calls.clear()
        self.settle(service)
        self.assertNotIn(('read', 'attests', 'assignment'), gateway.calls)
        status, page = service.evidence_page('work')
        self.assertEqual(200, status)
        self.assertEqual(1, page['total'])
        self.assertTrue(page['complete'])
        self.assertNotIn(('attests', 'assignment'), service.model.inventories)

    def test_closed_review_count_change_reads_only_its_item_and_matches_fresh(self):
        gateway = PublishedReads()
        gateway.rows['assignments', None][0]['state'] = 'closed'
        gateway.rows['work-items', None] = [summarized_work(holders=[],
            attest_counts={'completion': 1, 'verdict': 2},
            verdict_counts={'reviewed-clean': 1, 'tests-passed': 1})]
        gateway.rows['attests', 'assignment'] = [
            dict(id='done', assignmentId='assignment', kind='completion', ts=10),
            dict(id='tested', assignmentId='assignment', kind='verdict',
                 verdictKind='tests-passed', ts=20),
            dict(id='review', assignmentId='assignment', kind='verdict',
                 verdictKind='reviewed-clean', ts=30)]
        service = self.service(gateway)
        self.assertEqual(5, service.current['items'][0]['stage'])
        self.assertEqual({}, service.model.inventory('assignments').rows)
        gateway.rows['attests', 'assignment'].append(dict(id='progress',
            assignmentId='assignment', kind='progress', ts=40, bySession='agent s_author'))
        item = gateway.rows['work-items', None][0]
        item['attestCounts']['progress'] = 1
        item['rowVersion'] = 2
        gateway.calls.clear()
        with patch.object(service, 'rebuild', side_effect=AssertionError('unrelated recovery')):
            service._refresh('work-items', key='work')
            self.settle(service)
        self.assertEqual([('membership', 'work')],
                         [call for call in gateway.calls if call[0] == 'membership'])
        self.assertFalse(any(call[0] == 'read' and call[1] in
                             ('assignments', 'sessions', 'turns', 'wakes', 'messages')
                             for call in gateway.calls))
        fresh_gateway = PublishedReads()
        fresh_gateway.rows = deepcopy(gateway.rows)
        fresh = self.service(fresh_gateway)
        self.assertEqual(publication(fresh.current), publication(service.current))
        self.assertEqual({}, service.model.inventory('assignments').rows)
        self.assertNotIn(('attests', 'assignment'), service.model.inventories)

    def test_closed_nonreview_count_change_preserves_summary_attest_eligibility(self):
        gateway = PublishedReads()
        gateway.rows['assignments', None][0]['state'] = 'closed'
        gateway.rows['work-items', None] = [summarized_work(holders=[])]
        service = self.service(gateway)
        gateway.rows['work-items', None][0]['attestCounts']['progress'] = 2
        gateway.rows['work-items', None][0]['rowVersion'] = 2
        gateway.calls.clear()
        with patch.object(service, 'rebuild', side_effect=AssertionError('unrelated recovery')):
            service._refresh('work-items', key='work')
            self.settle(service)
        self.assertEqual([('membership', 'work')],
                         [call for call in gateway.calls if call[0] == 'membership'])
        self.assertNotIn(('read', 'attests', 'assignment'), gateway.calls)
        self.assertNotIn(('read', 'assignments', None), gateway.calls)
        fresh_gateway = PublishedReads()
        fresh_gateway.rows = deepcopy(gateway.rows)
        fresh = self.service(fresh_gateway)
        self.assertEqual(publication(fresh.current), publication(service.current))
