"""Retired holders retain item facts without retaining session histories."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_service import LiveService
from test_service import PublishedReads


class RetiredActivityTest(unittest.TestCase):
    def gateway(self, retired):
        gateway = PublishedReads()
        gateway.rows['work-items', None][0]['state'] = 'closed'
        gateway.rows['assignments', None][0].update(state='closed', openedAt=2, closedAt=20)
        gateway.rows['sessions', None][0]['state'] = 'retired' if retired else 'active'
        gateway.rows['turns', 'agent s_author'] = [dict(
            seq=1, sessionKey='agent s_author', assignmentId='assignment',
            createdAt=100, startedAt=101, endedAt=700000, prompt='discard me')]
        return gateway

    def drain(self, service):
        deadline = time.monotonic() + 5
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            kind, value = service.events.get(timeout=max(.01, deadline-time.monotonic()))
            self.assertEqual('read', kind)
            service._read_done(value)
            self.assertLess(time.monotonic(), deadline)

    def load(self, gateway):
        service = LiveService(gateway)
        self.addCleanup(service.stop)
        service.connected = True
        service.rebuild()
        self.drain(service)
        service._publish()
        return service

    def test_warm_and_fresh_retain_item_maxima_without_session_history(self):
        with patch('atc_service.now_ms', return_value=1000000):
            gateway = self.gateway(False)
            warm = self.load(gateway)
            gateway.rows['sessions', None][0]['state'] = 'retired'
            warm.rebuild(recovery=True)
            self.drain(warm)
            warm._publish()
            cold_gateway = self.gateway(True)
            cold = self.load(cold_gateway)
            for service in (warm, cold):
                self.assertEqual([], service.snapshot()['agents'])
                item = service.snapshot()['items'][0]
                self.assertEqual(700000, item['latestActivityAt'])
                self.assertEqual(700000, item['finishedAt'])
                self.assertTrue(item['finishedAtApproximate'])
                self.assertEqual({'work': 700000}, service.model.retired_item_activity)
                self.assertFalse(any(scope == 'agent s_author' for resource, scope in service.model.inventories))
                self.assertNotIn('agent s_author', service.model.inventory('sessions').rows)
            self.assertEqual(1, cold_gateway.calls.count(('read', 'turns', 'agent s_author')))
            cold_gateway.calls.clear()
            cold.rebuild(recovery=False)
            self.drain(cold)
            cold._publish()
            self.assertNotIn(('read', 'turns', 'agent s_author'), cold_gateway.calls)

    def test_unlinked_retired_session_is_not_read_and_exact_finish_wins(self):
        with patch('atc_service.now_ms', return_value=1000000):
            gateway = self.gateway(True)
            gateway.rows['sessions', None].append(dict(sessionKey='agent s_unlinked', state='retired'))
            gateway.rows['finish-time', 'work'] = [dict(id='work', state='closed', finishedAt=800000)]
            service = self.load(gateway)
            item = service.snapshot()['items'][0]
            self.assertEqual(700000, item['latestActivityAt'])
            self.assertEqual(800000, item['finishedAt'])
            self.assertFalse(item['finishedAtApproximate'])
            self.assertNotIn(('read', 'turns', 'agent s_unlinked'), gateway.calls)
