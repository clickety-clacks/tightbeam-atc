"""Retired holders retain item facts without retaining session histories."""
import json
import shutil
import subprocess
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

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for viewer tests')
    def test_published_item_outside_browser_window_keeps_retired_activity(self):
        now = 40 * 86400000
        with patch('atc_service.now_ms', return_value=now):
            gateway = self.gateway(False)
            warm = self.load(gateway)
            gateway.rows['sessions', None][0]['state'] = 'retired'
            warm.rebuild(recovery=True)
            self.drain(warm)
            warm._publish()
            cold = self.load(self.gateway(True))
            self.assertEqual(warm.snapshot()['items'], cold.snapshot()['items'])
            item = cold.snapshot()['items'][0]
            self.assertEqual(700000, item['latestActivityAt'])
            self.assertEqual(700000, item['finishedAt'])
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run([shutil.which('node'), '--input-type=module', '-e',
                "import {itemInWindow} from './web/work-item-window.mjs';"
                "const item=JSON.parse(process.argv[1]);"
                "console.log(JSON.stringify([1,30].map(days=>itemInWindow(item,days,Number(process.argv[2])))));",
                json.dumps(item), str(now)], cwd=root, capture_output=True, text=True, check=True)
            self.assertEqual([False, False], json.loads(result.stdout))
            self.assertEqual([], cold.snapshot()['agents'])
            self.assertNotIn(('turns', 'agent s_author'), cold.model.inventories)

    def test_recovery_refreshes_retired_item_activity(self):
        with patch('atc_service.now_ms', return_value=1000000):
            gateway = self.gateway(True)
            service = self.load(gateway)
            gateway.rows['turns', 'agent s_author'][0]['endedAt'] = 900000
            service.rebuild(recovery=True)
            self.drain(service)
            service._publish()
            self.assertEqual(900000, service.snapshot()['items'][0]['latestActivityAt'])
            self.assertEqual(900000, service.snapshot()['items'][0]['finishedAt'])

    def test_partial_retired_history_retries_without_claiming_complete(self):
        with patch('atc_service.now_ms', return_value=1000000):
            gateway = self.gateway(True)
            gateway.partial.add(('turns', 'agent s_author'))
            service = self.load(gateway)
            self.assertIsNotNone(service.cycle)
            self.assertIn(('retired-turns', None, None), service.read_retries)
            self.assertEqual(700000, service.snapshot()['items'][0]['latestActivityAt'])
            gateway.partial.clear()
            gateway.rows['turns', 'agent s_author'][0]['endedAt'] = 800000
            service.read_retries['retired-turns', None, None] = (0, 0)
            service._retry_incomplete_reads()
            self.drain(service)
            service._publish()
            self.assertIsNone(service.cycle)
            self.assertEqual(800000, service.snapshot()['items'][0]['latestActivityAt'])
            self.assertNotIn(('turns', 'agent s_author'), service.model.inventories)

    def test_retired_turn_notice_folds_and_session_reappearance_rereads(self):
        with patch('atc_service.now_ms', return_value=1000000):
            gateway = self.gateway(True)
            service = self.load(gateway)
            gateway.calls.clear()
            for resource in ('messages', 'wakes'):
                self.assertFalse(service.change(dict(resource=resource,
                    refs={'sessionKey': 'agent s_author'}, payload={})))
            self.assertEqual([], gateway.calls)
            gateway.rows['turns', 'agent s_author'][0]['endedAt'] = 800000
            service.change(dict(resource='turns', refs={'sessionKey': 'agent s_author', 'turnSeq': 1},
                                payload={'sessionKey': 'agent s_author'}))
            self.drain(service)
            service._publish()
            self.assertEqual(800000, service.snapshot()['items'][0]['latestActivityAt'])
            self.assertNotIn(('turns', 'agent s_author'), service.model.inventories)
            gateway.rows['sessions', None][0]['state'] = 'active'
            gateway.rows['assignments', None][0]['state'] = 'open'
            gateway.calls.clear()
            service.rebuild(recovery=True)
            self.drain(service)
            service._publish()
            self.assertEqual(1, len(service.snapshot()['agents']))
            self.assertIn(('read', 'turns', 'agent s_author'), gateway.calls)
            fresh = self.load(gateway)
            self.assertEqual(fresh.snapshot()['items'], service.snapshot()['items'])
            self.assertEqual(fresh.snapshot()['agents'], service.snapshot()['agents'])

    def test_reconnect_catches_retired_activity_missed_during_outage(self):
        with patch('atc_service.now_ms', return_value=1000000):
            gateway = self.gateway(True)
            service = self.load(gateway)
            service._handle('lost', 'test outage')
            gateway.rows['turns', 'agent s_author'][0]['endedAt'] = 900000
            service._handle('ready', {'version': '0.1.11', 'build': 1, 'sha': 'test'})
            self.drain(service)
            service._publish()
            self.assertEqual(900000, service.snapshot()['items'][0]['latestActivityAt'])

    def test_retired_discovery_releases_each_page_before_the_next_session(self):
        import gc
        import queue
        import weakref
        from unittest.mock import Mock
        from atc_reads import ReadQueue
        from atc_upstream import Gateway

        class Row(dict):
            pass

        held, paths = [], []
        gateway = Gateway('http://example.invalid', Mock())
        def request(path):
            gc.collect()
            self.assertTrue(all(ref() is None for ref in held))
            paths.append(path)
            second = 'after=' in path
            session = 'first' if '/first/' in path else 'second'
            row = Row(seq=2 if second else 1, assignmentId=session,
                      endedAt=800000 if second else 700000, prompt='historical text')
            held.append(weakref.ref(row))
            return {'schemaVersion': 1, 'items': [row], 'page': {
                'hasMoreBefore': second, 'hasMoreAfter': not second,
                'newestCursor': 'next' if not second else 'end'}}
        gateway.request = request
        reads = ReadQueue(gateway, queue.Queue(), 1)
        self.addCleanup(reads.close)
        result = reads._read(('retired-turns', None, None),
                             ('marker', ('first', 'second'), {'first': 'work', 'second': 'work'}))
        self.assertTrue(result.complete, result.error)
        self.assertEqual(('marker', {'work': 800000}), result.folded)
        self.assertEqual([], result.rows)
        self.assertEqual(4, len(paths))
        self.assertTrue(all('/first/' in p for p in paths[:2]))
        self.assertTrue(all('/second/' in p for p in paths[2:]))
        gc.collect()
        self.assertTrue(all(ref() is None for ref in held))

    def test_new_session_reads_only_its_scopes_without_retired_rescan(self):
        from atc_payloads import PAYLOAD_POLICIES, SESSION_FIELDS
        with patch('atc_service.now_ms', return_value=1000000):
            gateway = self.gateway(True)
            service = self.load(gateway)
            service.policies = PAYLOAD_POLICIES
            marker = service.retired_activity_marker
            row = dict.fromkeys(SESSION_FIELDS) | dict(
                sessionKey='agent s_new', state='active', rowVersion=100,
                displayName='New', archetype='coder', capabilities={})
            gateway.rows['sessions', None].append(row)
            for resource in ('turns', 'wakes', 'messages'):
                gateway.rows[resource, 'agent s_new'] = []
            gateway.calls.clear()
            service.change(dict(resource='sessions', op='upsert',
                                **{'class': 'session.spawned'},
                                refs={'sessionKey': 'agent s_new'}, payload=row))
            self.drain(service)
            service._publish()
            self.assertEqual(marker, service.retired_activity_marker)
            self.assertIsNone(service.cycle)
            self.assertEqual({('read', resource, 'agent s_new')
                              for resource in ('turns', 'wakes', 'messages')},
                             set(gateway.calls))
            self.assertEqual(700000, service.snapshot()['items'][0]['latestActivityAt'])
            self.assertNotIn(('turns', 'agent s_author'), service.model.inventories)
