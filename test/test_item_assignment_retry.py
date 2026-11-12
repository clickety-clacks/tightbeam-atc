"""Partial assignment sources publish only complete per-item replacements."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_service import LiveService
from atc_upstream import Read
from test_closed_folding import ClosedAssignmentFoldTest
from test_service import PublishedReads


class ItemReads(PublishedReads):
    def __init__(self):
        super().__init__()
        model = ClosedAssignmentFoldTest().model()
        self.rows = {key: deepcopy(list(inv.rows.values())) for key, inv in model.inventories.items()}
        self.subset = None
        self.fail_member = None

    def read(self, resource, scope=None):
        result = super().read(resource, scope)
        if resource == 'assignments' and self.subset is not None:
            result.rows = [row for row in result.rows if row['id'] in self.subset]
            result.complete = False
        return result

    def detail(self, resource, key, scope=None):
        if resource == 'assignments' and key == self.fail_member:
            return Read([], False, 10, 'published-detail', 'TimeoutError')
        return super().detail(resource, key, scope)


class ItemAssignmentRetryTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_service.now_ms', return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def service(self, gateway=None):
        service = LiveService(gateway or ItemReads(), workers=1)
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

    def partial(self, service, subset):
        service.gateway.subset = subset
        service._refresh('assignments')
        service.reads.pump()
        service._handle(*service.events.get(timeout=5))
        service._publish()

    def facts(self, service):
        item = next(row for row in service.current['items'] if row['id'] == 'work')
        return ({key: item[key] for key in ('closedAt', 'commitRefs', 'commitRefCorrections',
                                           'assignments', 'latestActivityAt', 'finishedAt')},
                {row['sessionKey']: row['idleSince'] for row in service.current['agents']})

    def fresh(self, service):
        gateway = ItemReads()
        gateway.rows = deepcopy(service.gateway.rows)
        return self.service(gateway)

    def test_original_partial_cases_hold_then_match_complete_source(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                service = self.service()
                before = self.facts(service)
                first = service.gateway.rows['assignments', None][0]
                if changed:
                    first['closedAt'] = 200
                    first['commitRefCorrections'] = [dict(id='new-correction', commitRefs=[{'commit': 'new'}])]
                self.partial(service, {'first'})
                self.assertEqual(before, self.facts(service))
                self.assertFalse(service.current['items'][0]['evidence']['assignments']['complete'])
                self.assertNotIn('first', service.model.inventory('assignments').rows)
                self.settle(service)
                self.assertEqual(self.facts(self.fresh(service)), self.facts(service))
                self.assertFalse(service.model.inventory('assignments').complete)
                self.assertTrue(service.current['items'][0]['evidence']['assignments']['complete'])
                self.assertEqual({'open'}, service.model.inventory('assignments').rows.keys())
                self.assertNotIn(('attests', 'first'), service.model.inventories)
                self.assertEqual(set(), service.model.item_assignment_pending)

    def test_different_contributors_failure_and_targeted_retry(self):
        service = self.service()
        for aid, close, commit in [('first', 200, 'new-first'), ('last', 300, 'new-last')]:
            before = self.facts(service)
            row = next(row for row in service.gateway.rows['assignments', None] if row['id'] == aid)
            row['closedAt'] = close
            row['commitRefCorrections'] = [dict(id='correction-'+aid, commitRefs=[{'commit': commit}])]
            service.gateway.fail_member = 'last'
            self.partial(service, {aid})
            self.settle(service)
            self.assertEqual(before, self.facts(service))
            job = ('item-assignment-source', 'work', None)
            self.assertIn(job, service.read_retries)
            self.assertIn('work', service.model.item_assignment_pending)
            service.gateway.fail_member = None
            service.gateway.calls.clear()
            service.read_retries[job] = (0, 2)
            service._retry_incomplete_reads()
            self.settle(service)
            self.assertEqual(self.facts(self.fresh(service)), self.facts(service))
            self.assertIn(('membership', 'work'), service.gateway.calls)
            self.assertNotIn(('read', 'assignments', None), service.gateway.calls)

    def test_closed_notice_correlates_then_reads_only_affected_item(self):
        service = self.service()
        service.gateway.calls.clear()
        service.gateway.rows['attests', 'last'].append(
            dict(id='later', assignmentId='last', kind='progress', bySession='other', ts=500))
        service.change(dict(resource='attests', op='upsert', **{'class': 'attest.filed'},
                            refs={'attestId': 'later'}, payload={'assignmentId': 'last'}))
        self.settle(service)
        self.assertIn(('detail', 'assignments', None, 'last'), service.gateway.calls)
        self.assertIn(('membership', 'work'), service.gateway.calls)
        self.assertNotIn(('read', 'assignments', None), service.gateway.calls)
        self.assertEqual(self.facts(self.fresh(service)), self.facts(service))

