"""Published older items release source detail and reread it under stable pins."""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'server'))
from atc_service import LiveService
from atc_upstream import Read
from test_service import PublishedReads, work


class OlderItemServiceTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_service.now_ms', return_value=40*86400000)
        clock.start()
        self.addCleanup(clock.stop)
        self.gateway = PublishedReads()
        self.row = dict(work(), state='closed', body='Older source detail')
        self.gateway.rows['work-items', None] = [self.row]
        self.gateway.rows['assignments', None] = []
        self.gateway.rows['sessions', None] = []
        self.service = LiveService(self.gateway)
        self.addCleanup(self.service.stop)
        self.service.connected = True
        self.service.rebuild()
        self.drain()
        self.service._publish()

    def drain(self):
        deadline = time.monotonic()+5
        while self.service.reads.pending or self.service.reads.active:
            self.service.reads.pump()
            kind, value = self.service.events.get(timeout=max(.01, deadline-time.monotonic()))
            self.assertEqual('read', kind)
            self.service._read_done(value)
            self.assertLess(time.monotonic(), deadline)

    def test_full_board_and_source_detail_survive_without_retained_older_rows(self):
        self.assertEqual(['work'], [row['id'] for row in self.service.snapshot()['items']])
        self.assertEqual({}, self.service.model.inventory('work-items').rows)
        self.assertEqual({}, self.service.model.inventory('work-items').observations)
        self.assertNotIn('body', self.service.model.older_items['work']['fields'])
        status, detail = self.service.evidence_page('work')
        self.assertEqual(200, status)
        self.assertEqual(self.row, detail['item'])
        self.assertIn(('detail', 'work-items', None, 'work'), self.gateway.calls)
        self.assertEqual({}, self.service.model.inventory('work-items').rows)
        self.assertEqual([], detail['records'])

    def test_changed_detail_reconciles_before_new_pin_and_failed_read_keeps_board(self):
        status, first = self.service.evidence_page('work')
        self.assertEqual(200, status)
        before = self.service.snapshot()
        self.gateway.rows['work-items', None] = []
        status, unavailable = self.service.evidence_page('work')
        self.assertEqual(503, status)
        self.assertEqual('item-unavailable', unavailable['error'])
        self.assertEqual(before['items'], self.service.snapshot()['items'])
        self.gateway.rows['work-items', None] = [dict(self.row, body='Changed source detail')]
        status, mismatch = self.service.evidence_page('work')
        self.assertEqual(409, status)
        event = self.service.events.get_nowait()
        self.assertEqual(('detail-item-reload', 'work'), event)
        self.service._handle(*event)
        self.drain()
        self.service._publish()
        self.assertGreater(self.service.revision, first['revision'])
        status, _ = self.service.evidence_page('work', generation=first['generation'], revision=first['revision'])
        self.assertEqual(409, status)
        status, latest = self.service.evidence_page('work')
        self.assertEqual(200, status)
        self.assertEqual('Changed source detail', latest['item']['body'])
        self.assertEqual({}, self.service.model.inventory('work-items').rows)

    def test_older_version_duplicate_conflict_and_reopen_preserve_feed_policy(self):
        self.service.policies = {'work-items': 'strict-version'}
        def notice(row):
            self.service.change(dict(resource='work-items', op='upsert',
                **{'class': 'work_item.updated'}, refs={'workItemId': 'work'}, payload=row))
            self.service._publish()
        initial = deepcopy(self.service.snapshot()['items'])
        revision = self.service.model.details_revision
        notice(dict(self.row, rowVersion=0, title='Old version'))
        self.assertEqual(initial, self.service.snapshot()['items'])
        self.assertEqual(revision, self.service.model.details_revision)
        notice(self.row)
        self.assertEqual(revision, self.service.model.details_revision)
        self.assertEqual({}, self.service.model.inventory('work-items').rows)
        conflict = dict(self.row, title='Read required')
        self.gateway.rows['work-items', None] = [conflict]
        notice(conflict)
        self.assertIn(('work-items', None, 'work'), self.service.reads.pending)
        self.assertEqual('First', self.service.model.work_items()['work']['title'])
        self.drain(); self.service._publish()
        self.assertEqual('Read required', self.service.snapshot()['items'][0]['title'])
        reopened = dict(conflict, state='open', rowVersion=2)
        notice(reopened)
        self.assertNotIn('work', self.service.model.older_items)
        self.assertEqual('open', self.service.snapshot()['items'][0]['state'])
        self.assertEqual(reopened, self.service.model.inventory('work-items').rows['work'])
