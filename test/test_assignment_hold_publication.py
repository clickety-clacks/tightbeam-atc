"""R29 publication retention; source-result merge is checked separately."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_assignment_conflicts import AssignmentReadConflicts
from atc_payloads import ASSIGNMENT_FIELDS
from atc_service import LiveService
from test_item_assignment_retry import ItemReads
from publication_assertions import assert_replay_current


class AssignmentHoldPublicationTest(unittest.TestCase):
    def setUp(self):
        clock = patch('atc_service.now_ms', return_value=1000)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.gateway = ItemReads()
        other = deepcopy(self.gateway.rows['work-items', None][0])
        other.update(id='unaffected-item', title='Unaffected')
        self.gateway.rows['work-items', None].append(other)
        self.service = LiveService(self.gateway, workers=1)
        self.addCleanup(self.service.stop)
        self.service._handle('ready', {'version': '0.1.9', 'build': 2908, 'sha': '8433d22f'})
        while self.service.reads.pending or self.service.reads.active:
            self.service.reads.pump()
            self.service._handle(*self.service.events.get(timeout=5))
        self.service._publish()
        self.opened = dict.fromkeys(ASSIGNMENT_FIELDS) | deepcopy(next(
            row for row in self.gateway.rows['assignments', None] if row['id'] == 'open'))
        self.closed = dict(self.opened, state='closed', closedAt=5000, outcome='completed')

    def notice(self, row):
        return dict(resource='assignments', op='upsert', refs={'assignmentId': row['id']},
                    payload=row, **{'class': 'assignment.closed' if row['state'] == 'closed'
                                    else 'assignment.reopened'})

    def item(self, frame, identity='work'):
        return next(row for row in frame['items'] if row['id'] == identity)

    def assert_retained(self, original, actual, since):
        expected = deepcopy(original)
        for fact in expected['evidence'].values():
            if fact['freshness'] != 'stale':
                fact['freshness'] = 'unconfirmed'
            prior = fact.get('unconfirmedSince')
            fact['unconfirmedSince'] = since if prior is None else min(prior, since)
        self.assertEqual(expected, actual)

    def test_hold_shares_actual_last_publication_without_changing_other_collections(self):
        before_close = deepcopy(self.service.current)
        self.clock.return_value = 6000
        flight = AssignmentReadConflicts()
        flight.observe(self.notice(self.closed), delivery=1)
        self.assertTrue(self.service.change(self.notice(self.closed)))
        self.service._publish()
        published = self.service.current
        retained = self.item(published)
        original = deepcopy(retained)
        self.assertNotEqual(self.item(before_close), retained)
        calls = list(self.gateway.calls)
        conflicts = self.service._hold_assignment_conflicts(
            [self.opened], flight, complete=True, at=6000)
        self.assertEqual({'open'}, conflicts)
        self.assertIs(retained, self.service.assignment_holds.items['work'])
        self.service._publish()
        self.assert_retained(original, self.item(self.service.current), 6000)
        self.assertEqual(original, retained)  # Earlier full/replay frames remain immutable.
        self.assertEqual(self.item(published, 'unaffected-item'),
                         self.item(self.service.current, 'unaffected-item'))
        for name in ('agents', 'links', 'wakes', 'messages'):
            self.assertEqual(published[name], self.service.current[name], name)
        self.assertEqual(calls, self.gateway.calls)
        assert_replay_current(self, self.service, published, 'r29-held-item-publication')

    def test_queued_establisher_and_feed_drain_do_not_clear_hold(self):
        flight = AssignmentReadConflicts()
        flight.observe(self.notice(self.closed), delivery=10)
        baseline = self.service.current
        self.clock.return_value = 2000
        self.service._hold_assignment_conflicts([self.opened], flight, complete=True, at=2000)
        self.service._publish()
        held = deepcopy(self.item(self.service.current))
        self.assertFalse(self.service.assignment_holds.resolve_notice('open', 10))
        # Nothing about another publication, zero queued events, or a partial
        # read establishes a resolver. No read is scheduled by this hold.
        self.clock.return_value = 3000
        self.assertEqual(set(), self.service._hold_assignment_conflicts(
            [self.closed], flight, complete=False, at=3000))
        self.service._publish()
        self.assertEqual(held, self.item(self.service.current))
        self.assertFalse(self.service.reads.pending)
        self.assertFalse(self.service.reads.active)
        assert_replay_current(self, self.service, baseline, 'r29-feed-drain-hold')

    def test_already_consumed_later_notice_is_not_a_retroactive_resolver(self):
        flight = AssignmentReadConflicts()
        flight.observe(self.notice(self.closed), delivery=5)
        self.service.assignment_delivery_consumed = 8
        self.clock.return_value = 2000
        self.service._hold_assignment_conflicts([self.opened], flight, complete=True, at=2000)
        self.assertFalse(self.service.assignment_holds.resolve_notice('open', 5))
        self.assertFalse(self.service.assignment_holds.resolve_notice('open', 8))
        self.assertTrue(self.service.assignment_holds.resolve_notice('open', 9))
        self.service._publish()
        self.assertEqual({}, self.service.assignment_holds.items)
