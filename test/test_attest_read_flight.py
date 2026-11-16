"""Read protection lasts through application and keeps only replay fields."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_reads import AttestReadFlight


class AttestReadFlightTest(unittest.TestCase):
    def row(self, key='a', timestamp=100):
        return dict(id=key, assignmentId='assignment', kind='progress',
                    verdictKind=None, bySession='author', ts=timestamp,
                    commitRefs=[{'commit': key}], note='not retained',
                    producerCommand='not retained')

    def test_only_open_contributions_keep_arrival_ordered_projection(self):
        flight = AttestReadFlight()
        self.assertFalse(flight.notice('assignment', self.row('before')))
        flight.open('assignment')
        row = self.row('b', 200)
        self.assertTrue(flight.notice('assignment', row))
        self.assertFalse(flight.notice('another', self.row('unrelated')))
        self.assertTrue(flight.notice('assignment', self.row('a', 100)))
        row['commitRefs'][0]['commit'] = 'caller changed'
        observed = []
        def consume(buffers):
            observed.extend(buffers['assignment'])
            self.assertEqual(2, flight.notice_count)
        flight.apply(consume)
        self.assertEqual(['b', 'a'], [row['id'] for row in observed])
        self.assertEqual([{'commit': 'b'}], observed[0]['commitRefs'])
        self.assertTrue(all('note' not in row and 'producerCommand' not in row
                            for row in observed))
        self.assertEqual(2, flight.peak_notice_count)
        self.assertEqual({}, flight.buffers)
        self.assertEqual(0, flight.notice_count)
        self.assertFalse(flight.notice('assignment', self.row('after')))

    def test_read_return_keeps_protection_and_lifetime_includes_local_delay(self):
        flight = AttestReadFlight()
        with patch('atc_reads.time.monotonic', side_effect=[10.0, 12.0, 20.0]):
            flight.open('assignment')
            flight.read_completed('assignment')
            self.assertTrue(flight.notice('assignment', self.row()))
            self.assertFalse(flight.closed)
            self.assertEqual(12.0, flight.read_completed_at['assignment'])
            flight.apply(lambda buffers: self.assertEqual(1, len(buffers['assignment'])))
        self.assertEqual(2.0, flight.maximum_read_duration)
        self.assertEqual(10.0, flight.maximum_lifetime)
        self.assertEqual({}, flight.opened_at)
        self.assertEqual({}, flight.read_completed_at)

    def test_failed_application_or_abandonment_ends_attempt_not_future_retry(self):
        for failed_application in (False, True):
            with self.subTest(failed_application=failed_application):
                flight = AttestReadFlight()
                flight.open('assignment')
                flight.notice('assignment', self.row())
                if failed_application:
                    def fail(buffers):
                        raise ValueError('unusable complete snapshot')
                    with self.assertRaisesRegex(ValueError, 'unusable complete snapshot'):
                        flight.apply(fail)
                else:
                    flight.discard()
                self.assertTrue(flight.closed)
                self.assertEqual({}, flight.buffers)
                self.assertEqual({}, flight.opened_at)
                self.assertFalse(flight.notice('assignment', self.row('late')))
                with self.assertRaises(ValueError):
                    flight.open('assignment')
                retry = AttestReadFlight()
                self.assertFalse(retry.notice('assignment', self.row('pre-retry')))
                retry.open('assignment')
                self.assertEqual([], retry.buffers['assignment'])
                retry.discard()
