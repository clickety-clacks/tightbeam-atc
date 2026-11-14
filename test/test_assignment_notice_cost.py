"""Ordinary assignment changes must not fan out over released item history."""
from copy import deepcopy
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_service import LiveService
from test_item_assignment_retry import ItemReads
from publication_assertions import assert_publication_equal, assert_replay_current


class AssignmentNoticeCostTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_service.now_ms', return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def load(self, gateway):
        service = LiveService(gateway, workers=1)
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

    def assert_no_member_fanout(self, calls, changed):
        unrelated = [call for call in calls
                     if (call[0] == 'detail' and call[1] == 'assignments'
                         and call[-1] != changed)
                     or (call[:2] == ('read', 'attests') and call[-1] != changed)]
        self.assertEqual([], unrelated, calls)
        self.assertFalse([call for call in calls if call[0] == 'membership'], calls)

    def test_reduced_detail_pin_does_not_change_attest_source_eligibility(self):
        gateway = ItemReads()
        service = self.load(gateway)
        closed = service.model.closed_items['work']
        self.assertTrue(closed['attestsRead'])
        # Compact reductions retain source facts but cannot regenerate the
        # discarded detail text. That is not a source-skip transition.
        closed['detailsKnown'] = False
        gateway.calls.clear()
        with patch.object(service, '_request_item_assignments') as item_read:
            service._reconcile_item_attests(service.model.work_items()['work'])
            item_read.assert_not_called()
        self.assertEqual([], gateway.calls)
        self.assertEqual(set(), service.model.item_assignment_pending)

    def test_new_and_held_close_preserve_released_facts_without_member_fanout(self):
        gateway = ItemReads()
        service = self.load(gateway)
        self.assertTrue(service.model.closed_sources_released)
        row = dict(id='new-current', workItemId='work', holderKey='author',
                   state='open', effectKind='code', openedAt=30,
                   closedAt=None, commitRefCorrections=[])
        gateway.rows['assignments', None].insert(0, row)
        gateway.rows['attests', row['id']] = []
        for state in ('open', 'closed'):
            with self.subTest(state=state):
                row.update(state=state, closedAt=40 if state == 'closed' else None)
                baseline = deepcopy(service.current)
                gateway.calls.clear()
                service.change(dict(resource='assignments', op='upsert',
                    refs={'assignmentId': row['id'], 'workItemId': 'work'},
                    payload=deepcopy(row),
                    **{'class': 'assignment.opened' if state == 'open' else 'assignment.closed'}))
                self.settle(service)
                self.assert_no_member_fanout(gateway.calls, row['id'])
                fresh_gateway = ItemReads()
                fresh_gateway.rows = deepcopy(gateway.rows)
                fresh = self.load(fresh_gateway)
                assert_publication_equal(self, fresh.current, service.current,
                                         'b4-ordinary-' + state)
                assert_replay_current(self, service, baseline, 'b4-ordinary-' + state + '-replay')
