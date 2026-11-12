"""Closed source disposal through complete service read/publication cycles."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_service import LiveService
from test_service import PublishedReads, summarized_work


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
