"""R29 equality conflicts and current unresolved publication ownership."""
from copy import deepcopy
from pathlib import Path
import queue
import sys
from threading import Event
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_assignment_conflicts import AssignmentReadConflicts, AssignmentHolds, AssignmentReadRegistry
from atc_payloads import ASSIGNMENT_FIELDS
from atc_reads import ReadQueue
from atc_upstream import Read


class AssignmentConflictsTest(unittest.TestCase):
    def row(self, state='open', **changes):
        row = dict.fromkeys(ASSIGNMENT_FIELDS)
        row.update(id='a', workItemId='item', openedAt=100, state=state,
                   closedAt=200 if state == 'closed' else None, files=[],
                   subject='not retained as a source row')
        row.update(changes)
        return row

    def notice(self, row):
        return dict(resource='assignments', op='upsert',
                    **{'class': 'assignment.closed' if row['state'] == 'closed'
                       else 'assignment.reopened'},
                    refs={'assignmentId': row['id']}, payload=row)

    def test_ambiguous_projection_conflicts_without_timestamp_precedence(self):
        flight = AssignmentReadConflicts()
        opened = self.row(rowVersion=100)
        closed = self.row('closed', rowVersion=200)
        self.assertTrue(flight.observe(self.notice(closed)))
        before = deepcopy(opened)
        self.assertEqual({'a'}, flight.conflicting_rows([opened], complete=True))
        self.assertEqual(before, opened)
        # These inputs are shared by before-close and after-reopen histories.
        # Complete service sequences must exercise both application orders.
        self.assertNotIn('not retained', repr(flight.notices))

    def test_same_shared_projection_is_clean_without_feed_only_fields(self):
        flight = AssignmentReadConflicts()
        row = self.row('closed', derivedStatus='abandoned', rowVersion=200)
        flight.observe(self.notice(row))
        snapshot = {key: value for key, value in row.items()
                    if key not in {'derivedStatus', 'rowVersion'}}
        snapshot['commitRefCorrections'] = [{'id': 'correction'}]
        self.assertEqual(set(), flight.conflicting_rows([snapshot], complete=True))

    def test_reopen_does_not_erase_an_earlier_conflict_in_the_same_flight(self):
        flight = AssignmentReadConflicts()
        for row in (self.row('closed'), self.row(), self.row('closed'), self.row()):
            flight.observe(self.notice(row))
        self.assertEqual(1, len(flight.notices))
        self.assertEqual({'a'}, flight.conflicting_rows([self.row()], complete=True))
        self.assertEqual({'a'}, flight.conflicting_rows([self.row('closed')], complete=True))
        self.assertIsNone(flight.conflicting_rows([self.row()], complete=False))
        self.assertEqual(set(), flight.conflicting_rows([self.row(id='unaffected')], complete=True))
        flight.discard()
        self.assertEqual({}, flight.notices)
        self.assertFalse(flight.observe(self.notice(self.row())))
        with self.assertRaises(ValueError):
            flight.conflicting_rows([], complete=True)

    def test_unsupported_or_mismatched_notice_cannot_certify_a_projection(self):
        flight = AssignmentReadConflicts()
        malformed = self.notice(self.row())
        malformed['refs']['assignmentId'] = 'different'
        self.assertFalse(flight.observe(malformed))
        missing = self.notice({'id': 'a', 'state': 'closed'})
        self.assertFalse(flight.observe(missing))
        opened = self.notice(self.row())
        opened['class'] = 'assignment.opened'
        self.assertFalse(flight.observe(opened))
        self.assertEqual({}, flight.notices)

    def test_hold_uses_last_publication_and_resolves_assignments_individually(self):
        holds = AssignmentHolds()
        published = {'id': 'item', 'closedAt': 200, 'evidence': {'assignments': {'complete': True}}}
        later = {'id': 'item', 'closedAt': None}
        untouched = deepcopy(published)
        self.assertTrue(holds.hold('a', 'item', published, at=300))
        self.assertFalse(holds.hold('a', 'item', later, at=400))
        self.assertTrue(holds.hold('b', 'item', later, at=500))
        self.assertIs(published, holds.items['item'])
        self.assertEqual(300, holds.since('item'))
        self.assertTrue(holds.resolve('a'))
        self.assertIs(published, holds.items['item'])
        self.assertEqual(500, holds.since('item'))
        self.assertTrue(holds.resolve('b'))
        self.assertEqual({}, holds.items)
        self.assertEqual({}, holds.assignments)
        self.assertEqual(untouched, published)

    def test_scope_exit_and_restart_keep_no_completed_hold_ledger(self):
        holds = AssignmentHolds()
        holds.hold('a', 'one', {'id': 'one'}, at=100)
        holds.hold('b', 'two', {'id': 'two'}, at=200)
        holds.retain_items({'two'})
        self.assertEqual({'b'}, set(holds.assignments))
        self.assertEqual({'two'}, set(holds.items))
        restarted = AssignmentHolds()
        self.assertEqual({}, restarted.assignments)
        self.assertEqual({}, restarted.items)
        with self.assertRaises(ValueError):
            holds.hold('new', 'unknown', None, at=300)

    def test_establishing_delivery_cannot_resolve_but_next_queued_notice_can(self):
        registry = AssignmentReadRegistry()
        job = ('assignments', None, None)
        registry.start(job)
        queued = []
        close = registry.admit(self.notice(self.row('closed')), queued.append)
        reopen = registry.admit(self.notice(self.row()), queued.append)
        holds = AssignmentHolds()
        published = {'id': 'item', 'closedAt': None}

        def apply(flight):
            conflicts = flight.conflict_deliveries([self.row()], complete=True)
            self.assertEqual({'a': close}, conflicts)
            holds.hold('a', 'item', published, at=300,
                       after_delivery=conflicts['a'])
        registry.consume(job, apply)
        self.assertEqual([close, reopen], queued)
        self.assertFalse(holds.resolve_notice('a', queued[0]))
        self.assertIs(published, holds.items['item'])
        self.assertTrue(holds.resolve_notice('a', queued[1]))
        self.assertEqual({}, holds.assignments)
        self.assertEqual({}, registry.flights)

    def test_first_matching_notice_does_not_hide_later_conflict(self):
        flight = AssignmentReadConflicts()
        flight.observe(self.notice(self.row('closed')), delivery=10)
        flight.observe(self.notice(self.row()), delivery=11)
        flight.observe(self.notice(self.row('closed')), delivery=12)
        self.assertEqual({'a': 11}, flight.conflict_deliveries(
            [self.row('closed')], complete=True))
        self.assertEqual({'a': 10}, flight.conflict_deliveries(
            [self.row()], complete=True))
        holds = AssignmentHolds()
        original = {'id': 'item', 'closedAt': 200}
        holds.hold('a', 'item', original, at=300, after_delivery=10)
        self.assertFalse(holds.hold('a', 'item', {'id': 'item'},
                                    at=500, after_delivery=11))
        self.assertFalse(holds.resolve_notice('a', 11))
        self.assertEqual(300, holds.since('item'))
        self.assertIs(original, holds.items['item'])
        self.assertTrue(holds.resolve_notice('a', 12))

    def test_read_tracking_excludes_pre_read_notices_and_unrelated_scopes(self):
        registry = AssignmentReadRegistry()
        registry.admit(self.notice(self.row('closed')))
        global_job = ('assignments', None, None)
        item_job = ('item-assignment-source', 'item', None)
        other_job = ('item-assignment-source', 'other', None)
        detail_job = ('assignments', None, 'different')
        for job in (global_job, item_job, other_job, detail_job):
            registry.start(job)
            self.assertEqual({}, registry.flights[job].notices)
        delivered = registry.admit(self.notice(self.row('closed')))
        for job in (global_job, item_job):
            self.assertEqual({'a': delivered}, registry.flights[job].conflict_deliveries(
                [self.row()], complete=True))
        for job in (other_job, detail_job):
            self.assertEqual({}, registry.flights[job].notices)
        # The retained summary cannot refer back to the caller's mutable row.
        payload = self.row()
        registry.admit(self.notice(payload))
        payload['subject'] = 'changed after admission'
        self.assertNotIn('changed after admission', repr(registry.flights[item_job].notices))
        registry.close()
        self.assertEqual({}, registry.flights)

    def test_queue_insertion_observes_admission_and_failed_consumer_releases_attempt(self):
        registry = AssignmentReadRegistry()
        job = ('assignments', None, None)
        registry.start(job)
        original = registry.flights[job]
        deliveries = []
        def enqueue(delivery):
            self.assertEqual({'a': delivery}, original.conflict_deliveries(
                [self.row()], complete=True))
            deliveries.append(delivery)
        registry.admit(self.notice(self.row('closed')), enqueue)
        self.assertEqual(1, len(deliveries))
        def failed_consumer(flight):
            self.assertIs(original, flight)
            self.assertIsNone(flight.conflict_deliveries([], complete=False))
            raise ValueError('incomplete source is not a hold resolver')
        with self.assertRaisesRegex(ValueError, 'incomplete source'):
            registry.consume(job, failed_consumer)
        self.assertTrue(original.closed)
        self.assertEqual({}, original.notices)
        self.assertEqual({}, registry.flights)
        registry.start(job)
        self.assertEqual({}, registry.flights[job].notices)
        # Item exit/cancellation drops protection without retaining an ID ledger.
        registry.discard(job)
        self.assertEqual({}, registry.flights)

    def test_assignment_result_queued_before_close_still_tracks_unconsumed_notice(self):
        started, release = Event(), Event()
        snapshot = self.row()
        calls = []
        class Gateway:
            def read(self, resource, scope):
                calls.append((resource, scope))
                started.set()
                if not release.wait(5):
                    raise AssertionError('fixture did not release assignment read')
                return Read([deepcopy(snapshot)], True, 100, 'assignment-snapshot')
        events = queue.Queue()
        reads = ReadQueue(Gateway(), events, 1)
        self.addCleanup(reads.close)
        self.addCleanup(release.set)
        job = ('assignments', None, None)
        # An earlier delivery is not part of this read's protected interval.
        reads.assignment_reads.admit(self.notice(self.row('closed')))
        reads.schedule(*job)
        reads.pump()
        self.assertTrue(started.wait(5))
        flight = reads.assignment_reads.flights[job]
        self.assertEqual({}, flight.notices)
        release.set()
        kind, value = events.get(timeout=5)
        self.assertEqual('read', kind)
        self.assertTrue(value[2].complete)
        # The worker is finished, but its result has not been consumed. The
        # subsequently queued close must participate in the R29 comparison.
        notice = self.notice(self.row('closed'))
        delivery = reads.assignment_reads.admit(notice, lambda ticket:
            events.put(('change', (notice, ticket))))
        queued_kind, queued_value = events.get(timeout=5)
        self.assertEqual('change', queued_kind)
        self.assertEqual(delivery, queued_value[1])
        def apply(summary):
            self.assertTrue(reads.finish(value[0], value[1]))
            self.assertEqual({'a': delivery}, summary.conflict_deliveries(
                value[2].rows, complete=value[2].complete))
        reads.assignment_reads.consume(job, apply)
        self.assertEqual([('assignments', None)], calls)
        self.assertEqual({}, reads.assignment_reads.flights)
        self.assertTrue(flight.closed)
        self.assertFalse(reads.pending)
        self.assertEqual(0, reads.epoch_discards)
