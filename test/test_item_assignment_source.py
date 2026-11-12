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
from atc_upstream import Gateway
from test_closed_folding import ClosedAssignmentFoldTest


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
                {k: v for k, v in row.items() if k != 'commitRefCorrections'}
                for row in self.assignments])
        if verb == 'assignments':
            return {'assignments': deepcopy(self.assignments)}
        if verb == 'assignment-get':
            return deepcopy(next(row for row in self.assignments if row['id'] == params['assignment_id']))
        if verb == 'attests':
            return {'attests': deepcopy(self.attests[params['assignment_id']])}
        raise AssertionError(verb)

    def result(self):
        return self.reads._read(('item-assignment-source', 'work', None), {'first'})

    def test_complete_read_matches_full_source_fold_and_order(self):
        result = self.result()
        self.assertTrue(result.complete, result.error)
        self.assertEqual([], result.rows)
        self.assertEqual(['work-item-get', 'assignment-get', 'attests',
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

    def test_any_incomplete_member_discards_staging_and_retry_rebuilds_all_members(self):
        for verb, aid in [('work-item-get', None), ('assignment-get', 'last'), ('attests', 'last')]:
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

