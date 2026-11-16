"""Read bookkeeping stays sparse without changing dispatch or invalidation."""
from collections import OrderedDict
from pathlib import Path
from queue import Queue
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_reads import ReadEpochs, ReadQueue
from atc_service import LiveService
from atc_upstream import Read


class EmptyReads:
    def __init__(self):
        self.calls = []

    def read(self, resource, scope=None):
        self.calls.append((resource, scope))
        return Read([], True, 10, 'fixture')


class ReadQueueStorageTest(unittest.TestCase):
    def queue(self):
        gateway, events = EmptyReads(), Queue()
        reads = ReadQueue(gateway, events, workers=1)
        self.addCleanup(reads.close)
        return reads, events, gateway

    def result(self, reads, events):
        reads.pump()
        kind, (job, epoch, result) = events.get(timeout=5)
        self.assertEqual('read', kind)
        self.assertTrue(result.complete)
        return job, epoch

    def test_completed_untouched_scopes_leave_no_epoch_entries_or_queue_capacity(self):
        reads, events, gateway = self.queue()
        pending, epochs = reads.pending, reads.epochs
        jobs = [('messages', 'session-' + str(i), None) for i in range(64)]
        for job in jobs:
            self.assertEqual(0, epochs[job[:2]])
            reads.schedule(*job)
        self.assertEqual({}, epochs)
        for expected in jobs:
            job, epoch = self.result(reads, events)
            self.assertEqual(expected, job)
            self.assertEqual(0, epoch)
            self.assertTrue(reads.finish(job, epoch))
        self.assertEqual([job[:2] for job in jobs], gateway.calls)
        self.assertIs(pending, reads.pending)
        self.assertIs(epochs, reads.epochs)
        self.assertFalse(pending)
        self.assertFalse(epochs)
        self.assertFalse(reads.active)
        self.assertEqual(0, reads.epoch_discards)
        self.assertEqual(sys.getsizeof(OrderedDict()), sys.getsizeof(pending))
        self.assertEqual(sys.getsizeof(ReadEpochs()), sys.getsizeof(epochs))

    def test_queued_result_invalidation_retains_generation_and_retry_priority(self):
        reads, events, gateway = self.queue()
        job = ('messages', 'session', None)
        reads.schedule(*job, priority=True)
        first, epoch = self.result(reads, events)
        self.assertEqual(job, first)
        self.assertEqual(0, epoch)
        reads.touch(*job[:2])
        reads.touch(*job[:2])
        self.assertFalse(reads.finish(job, epoch))
        self.assertEqual({job[:2]: 2}, reads.epochs)
        self.assertEqual({job: 2}, reads.pending)
        second, epoch = self.result(reads, events)
        self.assertEqual(job, second)
        self.assertEqual(2, epoch)
        reads.schedule(*job, priority=True)  # Same active generation coalesces.
        self.assertFalse(reads.pending)
        self.assertTrue(reads.finish(job, epoch))
        self.assertEqual([job[:2], job[:2]], gateway.calls)
        self.assertEqual(1, reads.epoch_discards)
        self.assertEqual({job[:2]: 2}, reads.epochs)

    def test_fourth_dispatch_still_admits_background_and_preserves_queue_order(self):
        reads, events, _gateway = self.queue()
        background = ('messages', 'background', None)
        priorities = [('messages', 'priority-' + str(i), None) for i in range(4)]
        reads.schedule(*background)
        for job in priorities:
            reads.schedule(*job, priority=True)
        expected = [priorities[3], priorities[2], priorities[1], background, priorities[0]]
        for wanted in expected:
            job, epoch = self.result(reads, events)
            self.assertEqual(wanted, job)
            self.assertTrue(reads.finish(job, epoch))
        self.assertEqual({}, reads.epochs)
        self.assertEqual(sys.getsizeof(OrderedDict()), sys.getsizeof(reads.pending))

    def test_scope_exit_keeps_active_generation_then_releases_empty_storage(self):
        service = LiveService(EmptyReads(), workers=1)
        self.addCleanup(service.stop)
        reads = service.reads
        pending, epochs = reads.pending, reads.epochs
        scopes = [('messages', 'session-' + str(i)) for i in range(64)]
        for scope in scopes:
            reads.touch(*scope)
            reads.schedule(*scope)
        job, epoch = self.result(reads, service.events)
        service._forget_scopes(set(scopes))
        self.assertFalse(reads.pending)
        self.assertEqual({job[:2]: 1}, reads.epochs)
        self.assertTrue(reads.finish(job, epoch))
        service._forget_scopes({job[:2]})
        self.assertIs(pending, reads.pending)
        self.assertIs(epochs, reads.epochs)
        self.assertFalse(epochs)
        self.assertEqual(0, epochs[job[:2]])
        self.assertFalse(epochs)  # A released scope stays absent after lookup.
        self.assertEqual(sys.getsizeof(OrderedDict()), sys.getsizeof(pending))
        self.assertEqual(sys.getsizeof(ReadEpochs()), sys.getsizeof(epochs))
