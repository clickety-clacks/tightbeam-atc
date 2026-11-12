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
from test_service import PublishedReads, summarized_work


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
                fresh = self.fresh(service)
                self.assertEqual(self.facts(fresh), self.facts(service))
                for name in ('agents', 'items', 'links', 'wakes', 'messages'):
                    self.assertEqual(fresh.current[name], service.current[name], name)
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
        fresh = self.fresh(service)
        for name in ('agents', 'items', 'links', 'wakes', 'messages'):
            self.assertEqual(fresh.current[name], service.current[name], name)

    def test_item_completion_does_not_clear_later_outage_evidence(self):
        service = self.service()
        self.partial(service, {'first'})
        self.settle(service)
        service.model.mark_unconfirmed(2000)
        fact = service.model.assignment_evidence('work')
        self.assertEqual('unconfirmed', fact['freshness'])
        self.assertEqual(2000, fact['unconfirmedSince'])
        service.model.invalidate()
        self.assertEqual('stale', service.model.assignment_evidence('work')['freshness'])

    def test_unrelated_fold_keeps_complete_item_reference_order(self):
        service = self.service()
        self.partial(service, {'first'})
        self.settle(service)
        model = service.model
        expected = self.facts(service)[0]['commitRefs']
        # A different item acquires closed construction rows during a complete
        # source cycle. Existing per-item order must survive that item's fold.
        model.inventory('assignments').complete = True
        model.detail('assignments', None, Read([dict(id='other-closed', workItemId='other-work',
                     state='closed', holderKey='other', effectKind='spec')], True, 10, 'published-detail'))
        model.accept('attests', 'other-closed', Read([], True, 10, 'published-read'))
        model.fold_closed_assignments()
        item = next(row for row in model.snapshot(1000)['items'] if row['id'] == 'work')
        self.assertEqual(expected, item['commitRefs'])

    def test_targeted_replacement_keeps_retired_holder_activity_in_full_board(self):
        gateway = ItemReads()
        for session in gateway.rows['sessions', None]:
            if session['sessionKey'] == 'other':
                session['state'] = 'retired'
        gateway.rows['turns', 'other'] = [dict(seq=3, sessionKey='other',
                                             assignmentId='last', endedAt=700000)]
        service = self.service(gateway)
        gateway.rows['turns', 'other'][0]['endedAt'] = 800000
        self.partial(service, {'last'})
        self.settle(service)
        fresh = self.fresh(service)
        self.assertEqual(800000, self.facts(service)[0]['latestActivityAt'])
        for name in ('agents', 'items', 'links', 'wakes', 'messages'):
            self.assertEqual(fresh.current[name], service.current[name], name)
        self.assertNotIn(('turns', 'other'), service.model.inventories)

    def test_detail_completeness_tracks_item_retry_and_preserves_pin(self):
        service = self.service(PublishedReads())
        status, before = service.evidence_page('work')
        self.assertEqual(200, status)
        self.assertTrue(before['complete'])
        service._request_item_assignments('work')
        service._publish()
        status, pending = service.evidence_page('work')
        self.assertEqual(200, status)
        self.assertFalse(pending['complete'])
        self.assertEqual(before['records'], pending['records'])
        self.assertTrue(service.model.inventory('assignments').complete)
        self.assertEqual(409, service.evidence_page('work', generation=before['generation'],
                                                  revision=before['revision'])[0])
        self.settle(service)
        status, complete = service.evidence_page('work')
        self.assertEqual(200, status)
        self.assertTrue(complete['complete'])
        self.assertEqual(before['records'], complete['records'])

    def test_summary_update_keeps_title_live_and_assignment_values_held_until_retry(self):
        gateway = ItemReads()
        gateway.rows['work-items', None] = [summarized_work(holders=['author'],
            refs=[{'commit': 'old'}], attest_counts={'progress': 3})]
        service = self.service(gateway)
        before = deepcopy(service.current['items'][0])
        gateway.fail_member = 'last'
        self.partial(service, {'first'})
        self.settle(service)
        epoch = service.reads.epochs['item-assignment-source', 'work']
        gateway.rows['work-items', None] = [summarized_work('New title', 2, holders=['other'],
            refs=[{'commit': 'new'}], attest_counts={'progress': 4})]
        # Both lifetimes use the same work-item source and confirmation time;
        # the complete-source comparison retains every evidence field.
        service._refresh('work-items')
        self.settle(service)
        held = service.current['items'][0]
        self.assertEqual('New title', held['title'])
        for key in ('commitRefs', 'commitRefCorrections', 'stage', 'assignments', 'closedAt'):
            self.assertEqual(before[key], held[key], key)
        self.assertFalse(held['evidence']['assignments']['complete'])
        self.assertGreater(service.reads.epochs['item-assignment-source', 'work'], epoch)
        gateway.fail_member = None
        job = ('item-assignment-source', 'work', None)
        service.read_retries[job] = (0, 2)
        service._retry_incomplete_reads()
        self.settle(service)
        fresh = self.fresh(service)
        for name in ('agents', 'items', 'links', 'wakes', 'messages'):
            self.assertEqual(fresh.current[name], service.current[name], name)
        self.assertEqual({}, service.model.item_assignment_summary)

    def test_item_removal_discards_failed_retry_and_held_facts(self):
        service = self.service()
        service.gateway.fail_member = 'last'
        self.partial(service, {'first'})
        self.settle(service)
        self.assertIn('work', service.model.item_assignment_pending)
        service.gateway.rows['work-items', None] = []
        service._refresh('work-items')
        self.settle(service)
        self.assertEqual([], service.current['items'])
        self.assertNotIn('work', service.model.item_assignment_pending)
        self.assertNotIn('work', service.model.item_assignment_facts)
        self.assertNotIn('work', service.model.item_assignment_summary)
        self.assertNotIn(('item-assignment-source', 'work', None), service.read_retries)

    def test_partial_summary_does_not_replace_assignment_holder_evidence(self):
        gateway = ItemReads()
        gateway.rows['work-items', None][0]['holders'] = None
        service = self.service(gateway)
        self.assertEqual({'author', 'other'},
                         {row['sessionKey'] for row in service.current['agents']})
        before = self.facts(service)
        self.partial(service, {'first'})
        self.settle(service)
        self.assertEqual(before, self.facts(service))

    def test_complete_item_retry_updates_observation_without_confirming_global_scope(self):
        service = self.service()
        original = service.gateway.read
        def later_read(resource, scope=None):
            result = original(resource, scope)
            result.at = 2000
            return result
        with patch.object(service.gateway, 'read', side_effect=later_read):
            self.partial(service, {'first'})
            self.assertEqual(10, service.model.assignment_evidence('work')['observedAt'])
            self.settle(service)
        self.assertEqual(2000, service.model.assignment_evidence('work')['observedAt'])
        self.assertFalse(service.model.inventory('assignments').complete)
        self.assertEqual(10, service.model.inventory('assignments').confirmed_at)

