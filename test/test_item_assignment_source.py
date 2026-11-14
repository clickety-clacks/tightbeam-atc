"""Complete item reads include corrections and attests before releasing rows."""
from copy import deepcopy
import gc
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import patch
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_reads import ReadQueue
from atc_service import LiveService
from atc_upstream import Gateway
from test_closed_folding import ClosedAssignmentFoldTest
from atc_contributions import AttestFrontier
from publication_assertions import assert_publication_equal


class ItemAssignmentSourceTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_upstream.now_ms', return_value=100)
        clock.start()
        self.addCleanup(clock.stop)
        self.model = ClosedAssignmentFoldTest().model()
        # Turn-to-item maxima are applied by the model, independently of this
        # assignment-source read. Keep this comparison on the read's scope.
        ClosedAssignmentFoldTest.read(self.model, 'turns', [], 'author')
        self.assignments = deepcopy(list(self.model.inventory('assignments').rows.values()))
        for row in self.assignments:
            row.setdefault('commitRefCorrections', [])
            row.setdefault('files', ['src/' + row['id']])
        self.attests = {row['id']: deepcopy(list(self.model.inventory('attests', row['id']).rows.values()))
                        for row in self.assignments}
        self.calls = []
        self.failure = None
        self.gateway = Gateway('http://example.invalid', self)
        self.reads = ReadQueue(self.gateway, queue.Queue(), 1)
        self.addCleanup(self.reads.close)

    def read(self, verb, **params):
        self.calls.append((verb, params))
        if self.failure == (verb, params.get('assignment_id')):
            raise ValueError('fixture read failure')
        if verb == 'work-item-get':
            # The existing membership route does not supply corrections.
            return dict(workItem={'id': 'work'}, assignments=[
                {k: v for k, v in row.items() if k not in ('commitRefCorrections', 'files')}
                for row in self.assignments])
        if verb == 'work-item-trace':
            return dict(workItem={'id': 'work'}, assignments=deepcopy(self.assignments))
        if verb == 'assignments':
            return {'assignments': deepcopy(self.assignments)}
        if verb == 'assignment-get':
            row = deepcopy(next(row for row in self.assignments if row['id'] == params['assignment_id']))
            row.pop('files')
            row['reopenings'] = []
            return row
        if verb == 'attests':
            return {'attests': deepcopy(self.attests[params['assignment_id']])}
        raise AssertionError(verb)

    def result(self):
        return self.reads._read(('item-assignment-source', 'work', None), {'first'})

    def test_complete_read_matches_full_source_fold_and_order(self):
        result = self.result()
        self.assertTrue(result.complete, result.error)
        self.assertEqual([], result.rows)
        self.assertEqual(['work-item-get', 'work-item-trace', 'assignment-get', 'attests',
                          'assignment-get', 'attests', 'assignment-get', 'attests'],
                         [verb for verb, _ in self.calls])
        staged = result.folded
        self.model.accept('assignments', None, self.gateway.read('assignments'))
        for row in self.assignments:
            self.model.accept('attests', row['id'], self.gateway.read_current('attests', row['id']))
        self.model.fold_closed_assignments()
        self.assertEqual(self.model.closed_items['work'], staged.closed)
        self.assertEqual({'open'}, staged.open_rows.keys())
        self.assertEqual({'open'}, staged.open_attests.keys())
        self.assertEqual({'open': 1}, staged.open_order)
        self.assertEqual({'first': 'work'}, staged.live_items)
        self.assertEqual(['first', 'corrected'], [ref['commit'] for _, _, ref in staged.closed['refs']])
        self.assertEqual(60, staged.closed['closedAt'])
        self.assertEqual({'author': 10, 'other': 30}, staged.closed['summary']['authors'])

    def test_complete_membership_reports_only_missing_published_winners(self):
        plan = dict(liveIds=set(), attests=True, activityWinners={"first", "missing"})
        result = self.reads._read(('item-assignment-source', 'work', None), plan)
        self.assertTrue(result.complete, result.error)
        self.assertEqual({"missing"}, result.folded.absent_activity_winners)
        self.assertEqual({"first", "missing"}, plan['activityWinners'])
        self.assertNotIn('assignments', [verb for verb, _ in self.calls])
        self.failure = ('attests', 'last')
        failed = self.reads._read(('item-assignment-source', 'work', None), plan)
        self.assertFalse(failed.complete)
        self.assertIsNone(failed.folded)

    def test_any_incomplete_member_discards_staging_and_retry_rebuilds_all_members(self):
        for verb, aid in [('work-item-get', None), ('work-item-trace', None),
                          ('assignment-get', 'last'), ('attests', 'last')]:
            with self.subTest(verb=verb):
                self.failure = (verb, aid)
                failed = self.result()
                self.assertFalse(failed.complete)
                self.assertIsNone(failed.folded)
                self.assertEqual([], failed.rows)
                self.failure = None
                self.assertTrue(self.result().complete)

    def test_missing_corrections_or_member_identity_cannot_certify_completeness(self):
        original = self.gateway.detail
        for change in ('corrections', 'identity', 'item', 'empty'):
            with self.subTest(change=change):
                def detail(resource, aid, scope=None):
                    result = original(resource, aid, scope)
                    if aid == 'last':
                        if change == 'corrections':
                            result.rows[0].pop('commitRefCorrections')
                        elif change == 'identity':
                            result.rows[0]['id'] = 'another'
                        elif change == 'item':
                            result.rows[0]['workItemId'] = 'another'
                        else:
                            result.rows.clear()
                    return result
                self.gateway.detail = detail
                result = self.result()
                self.assertFalse(result.complete)
                self.assertIsNone(result.folded)

    def test_empty_complete_membership_is_a_complete_replacement(self):
        self.assignments.clear()
        result = self.result()
        self.assertTrue(result.complete)
        self.assertIsNone(result.folded.closed)
        self.assertEqual({}, result.folded.open_rows)

    def test_replayed_stage_matches_fresh_open_closed_corrections_and_authors(self):
        snapshot = self.result()
        self.assertTrue(snapshot.complete, snapshot.error)
        stage = snapshot.folded
        old_closed_pin = stage.closed['detailFingerprint']
        old_open_pin = stage.open_attests['open'].fingerprint
        notices = {
            'first': [dict(id='new-first', kind='progress', ts=200,
                           bySession='other', commitRefs=[{'commit': 'later-first'}])],
            'open': [dict(id='new-open', kind='verdict', verdictKind='reviewed-clean',
                          ts=300, bySession='author', commitRefs=[])],
            'last': [dict(id='new-last', kind='progress', ts=400,
                         bySession='other', commitRefs=[{'commit': 'still-corrected'}])],
        }
        # No global/item/member read is performed by staging replay.
        before_calls = list(self.calls)
        self.assertTrue(stage.replay_attests(notices))
        self.assertEqual(before_calls, self.calls)
        self.assertEqual(old_closed_pin, stage.closed['detailFingerprint'])
        self.assertFalse(stage.closed['detailsKnown'])
        self.assertEqual(old_open_pin, stage.open_attests['open'].fingerprint)
        self.assertEqual(2, stage.open_attests['open'].folded['count'])
        self.assertEqual({'author': 300, 'other': 400}, stage.session_activity)
        self.assertEqual(['first', 'later-first', 'corrected'],
                         [ref['commit'] for _, _, ref in stage.closed['refs']])
        before_duplicate = deepcopy(stage)
        self.assertFalse(stage.replay_attests(notices))
        self.assertEqual(before_duplicate.contributions, stage.contributions)
        self.assertEqual(before_duplicate.closed, stage.closed)

        for aid, rows in notices.items():
            self.attests[aid].extend(rows)
        fresh_result = self.result()
        self.assertTrue(fresh_result.complete, fresh_result.error)
        fresh = deepcopy(self.model)
        for model, result in ((fresh, fresh_result), (self.model, snapshot)):
            model.hold_item_assignments('work')
            model.replace_item_assignments('work', result)
        assert_publication_equal(self, fresh.snapshot(1000), self.model.snapshot(1000),
                                 'r27-replayed-stage-open-closed')

    def test_invalid_second_buffer_does_not_partially_change_staging(self):
        result = self.result()
        self.assertTrue(result.complete, result.error)
        stage = result.folded
        value = stage.contributions['last']
        stage.contributions['last'] = value._replace(
            attests=value.attests._replace(frontier=AttestFrontier()))
        before = deepcopy(stage)
        with self.assertRaisesRegex(ValueError, 'no usable frontier'):
            stage.replay_attests({
                'first': [dict(id='new', kind='progress', ts=200, bySession='author')],
                'last': [],
            })
        self.assertEqual(before.contributions, stage.contributions)
        self.assertEqual(before.closed, stage.closed)
        self.assertEqual(before.session_activity, stage.session_activity)
        self.assertEqual(before.open_attests['open'].folded, stage.open_attests['open'].folded)

    def test_detail_page_preserves_collection_fields_before_and_after_targeted_reread(self):
        self.model.accept('assignments', None, self.gateway.read('assignments'))
        for row in self.assignments:
            self.model.accept('attests', row['id'], self.gateway.read_current('attests', row['id']))
        self.model.fold_closed_assignments()
        service = LiveService(self.gateway)
        self.addCleanup(service.stop)
        service.model = self.model
        for targeted in (False, True):
            with self.subTest(targeted=targeted):
                if targeted:
                    self.model.hold_item_assignments('work')
                    result = self.result()
                    self.assertTrue(result.complete, result.error)
                    self.model.replace_item_assignments('work', result)
                service._publish()
                self.calls.clear()
                status, body = service.evidence_page('work')
                self.assertEqual(200, status, body)
                self.assertEqual(self.assignments, body['assignments'])
                self.assertNotIn('assignments', [verb for verb, _ in self.calls])
        self.failure = ('work-item-trace', None)
        status, body = service.evidence_page('work')
        self.assertEqual(503, status)
        self.assertTrue(service.events.empty())

    def test_changed_member_between_membership_and_detail_does_not_publish_mixed_source(self):
        original = self.gateway.detail
        def detail(resource, aid, scope=None):
            result = original(resource, aid, scope)
            result.rows[0]['closedAt'] = 999
            return result
        self.gateway.detail = detail
        result = self.result()
        self.assertFalse(result.complete)
        self.assertEqual('assignment-source-changed', result.error)
        self.assertIsNone(result.folded)

    def test_closed_raw_rows_are_released_between_members(self):
        class Row(dict):
            pass
        original = self.gateway.detail
        held = []
        def detail(resource, aid, scope=None):
            gc.collect()
            self.assertTrue(all(ref() is None for ref in held))
            result = original(resource, aid, scope)
            if result.rows[0]['state'] != 'open':
                result.rows[0] = Row(result.rows[0])
                held.append(weakref.ref(result.rows[0]))
            return result
        self.gateway.detail = detail
        result = self.result()
        self.assertTrue(result.complete, result.error)
        gc.collect()
        self.assertTrue(all(ref() is None for ref in held))

