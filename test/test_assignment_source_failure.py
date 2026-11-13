"""Whole-source outages preserve data and retry without item fanout."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_service import LiveService
from atc_reconciliation import RETRY_MS
from test_item_assignment_retry import ItemReads
from test_service import summarized_work


def publication(value, path=()):
    if isinstance(value, list):
        return [publication(row, path + (str(i),)) for i, row in enumerate(value)]
    if not isinstance(value, dict):
        return value
    omit = set()
    if not path:
        omit.update(('revision', 'generation', 'generatedAt', 'metrics'))
    if path == ('freshness',):
        omit.update(('since', 'lastCompleteAt'))
    if path == ('freshness', 'reconciliation'):
        omit.add('readDiagnostics')
    if len(path) == 2 and path[0] == 'agents':
        omit.add('idleFor')
    if 'evidence' in path:
        omit.update(('observedAt', 'confirmedAt', 'unconfirmedSince'))
    return {k: publication(v, path + (k,)) for k, v in value.items() if k not in omit}


class UnavailableAssignments(ItemReads):
    error = None

    def read(self, resource, scope=None):
        if resource == 'assignments' and self.error:
            self.calls.append(('read', resource, scope))
            raise self.error('synthetic unavailable source')
        return super().read(resource, scope)


class AssignmentSourceFailureTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_service.now_ms', return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)
        self.gateway = UnavailableAssignments()
        # A second published item must not be reread for the first item's
        # identified partial response, nor for an unavailable global source.
        second = deepcopy(self.gateway.rows['work-items', None][0])
        second['id'] = 'untouched'
        self.gateway.rows['work-items', None].append(second)
        self.gateway.rows['assignments', None].append(dict(
            id='unrelated', workItemId='untouched', holderKey='other',
            state='closed', effectKind='spec', closedAt=40))
        self.gateway.rows['attests', 'unrelated'] = []
        self.service = self.load(self.gateway)

    def load(self, gateway):
        service = LiveService(gateway, workers=1)
        service.connected = True
        self.addCleanup(service.stop)
        service.rebuild()
        self.settle(service)
        return service

    def settle(self, service=None):
        service = service or self.service
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            service._handle(*service.events.get(timeout=5))
        service._publish()

    @staticmethod
    def data(service):
        return {key: [{k: v for k, v in row.items() if k not in ('evidence', 'idleFor')}
                      for row in service.current[key]]
                for key in ('items', 'agents', 'links', 'wakes', 'messages')}

    def refresh(self):
        self.service._refresh('assignments')
        self.settle()

    def test_error_timeout_retry_and_recovery_preserve_all_source_fed_values(self):
        service = self.service
        before = self.data(service)
        keys = set(service.current)
        confirmed = service.model.inventory('assignments').confirmed_at
        self.gateway.calls.clear()
        with self.assertLogs('atc.recovery', level='INFO') as logs:
            for index, error in enumerate((OSError, TimeoutError)):
                self.gateway.error = error
                with patch('atc_service.time.monotonic', return_value=20 + index * 100):
                    if index == 0:
                        self.refresh()
                    else:
                        service._retry_incomplete_reads()
                        self.settle()
                    self.assertEqual(20 + index * 100 + RETRY_MS / 1000,
                                     service.read_retries['assignments', None, None][0])
                self.assertEqual(before, self.data(service))
                self.assertEqual(set(), service.model.item_assignment_pending)
                self.assertEqual('stale', service.current['freshness']['state'])
                self.assertEqual('assignments unavailable; last complete read: ' + str(confirmed),
                                 service.current['freshness']['reason'])
                self.assertEqual(keys, set(service.current))
                self.assertFalse(any(call[0] in ('membership', 'detail') for call in self.gateway.calls))
            self.assertEqual(2, self.gateway.calls.count(('read', 'assignments', None)))
            self.gateway.error = None
            self.gateway.rows['assignments', None][0]['closedAt'] = 500
            with patch('atc_service.time.monotonic', return_value=240):
                service._retry_incomplete_reads()
                self.settle()
        self.assertEqual(2, sum('source unavailable' in line for line in logs.output))
        self.assertEqual(1, sum('source recovered' in line for line in logs.output))
        self.assertFalse(service.assignment_source_unavailable)
        self.assertNotIn(('assignments', None, None), service.read_retries)
        self.assertEqual('live', service.current['freshness']['state'])
        self.assertFalse(any(call[0] == 'membership' for call in self.gateway.calls))
        fresh_gateway = UnavailableAssignments()
        fresh_gateway.rows = deepcopy(self.gateway.rows)
        fresh = self.load(fresh_gateway)
        self.assertEqual(publication(fresh.current), publication(service.current))

    def test_successful_empty_recovery_uses_normal_complete_empty_path(self):
        reference_gateway = UnavailableAssignments()
        reference_gateway.rows = deepcopy(self.gateway.rows)
        reference = self.load(reference_gateway)
        self.gateway.error = OSError
        with self.assertLogs('atc.recovery', level='INFO'):
            self.refresh()
            self.gateway.error = None
            self.gateway.rows['assignments', None] = []
            self.refresh()
        reference_gateway.rows['assignments', None] = []
        reference._refresh('assignments')
        self.settle(reference)
        self.assertEqual(self.data(reference), self.data(self.service))
        self.assertEqual(publication(reference.current), publication(self.service.current))
        self.assertTrue(self.service.model.inventory('assignments').complete)
        self.assertFalse(self.service.assignment_source_unavailable)
        self.assertEqual(set(), self.service.model.item_assignment_pending)
        self.assertEqual({}, self.service.model.inventory('assignments').rows)
        self.assertEqual('live', self.service.current['freshness']['state'])

    def test_identified_partial_response_targets_only_its_item(self):
        untouched = next(row for row in self.data(self.service)['items'] if row['id'] == 'untouched')
        self.gateway.subset = {'first'}
        self.gateway.calls.clear()
        self.service._refresh('assignments')
        self.service.reads.pump()
        self.service._handle(*self.service.events.get(timeout=5))
        self.service._publish()
        self.assertEqual({'work'}, self.service.model.item_assignment_pending)
        self.assertEqual(untouched, next(row for row in self.data(self.service)['items'] if row['id'] == 'untouched'))
        self.settle()
        self.assertEqual([('membership', 'work')], [c for c in self.gateway.calls if c[0] == 'membership'])
        self.assertFalse(self.service.model.inventory('assignments').complete)
        self.assertTrue(self.service.model.assignment_evidence('untouched')['complete'])

    def test_whole_source_failure_during_recovery_keeps_review_stage_and_ring(self):
        gateway = UnavailableAssignments()
        gateway.rows['work-items', None] = [summarized_work(holders=[],
            attest_counts={'completion': 1, 'verdict': 2},
            verdict_counts={'reviewed-clean': 1, 'tests-passed': 1})]
        gateway.rows['assignments', None] = [dict(id='reviewed', workItemId='work',
            holderKey='author', state='closed', effectKind='code')]
        gateway.rows['attests', 'reviewed'] = [
            dict(id='done', assignmentId='reviewed', kind='completion', ts=10),
            dict(id='tested', assignmentId='reviewed', kind='verdict',
                 verdictKind='tests-passed', ts=20),
            dict(id='review', assignmentId='reviewed', kind='verdict',
                 verdictKind='reviewed-clean', ts=30)]
        service = self.load(gateway)
        self.assertEqual(5, service.current['items'][0]['stage'])
        before = self.data(service)
        gateway.error = TimeoutError
        with self.assertLogs('atc.recovery', level='WARNING'):
            service.rebuild(recovery=True)
            self.settle(service)
        self.assertEqual(before, self.data(service))
        self.assertEqual(set(), service.model.item_assignment_pending)
