"""Complete snapshot replay preserves counts and normal contribution effects."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_contributions import AttestContribution, AttestFrontier
from atc_summary import HistoryFold


class AttestReplayTest(unittest.TestCase):
    @staticmethod
    def row(identity, timestamp, **fields):
        return dict(id=identity, ts=timestamp, kind='progress', bySession='author') | fields

    @staticmethod
    def snapshot(rows):
        fold = HistoryFold('attests', 1000000)
        fold.add(rows)
        return AttestContribution.capture(fold.summary, fold.attest_frontier)

    def test_distinct_flights_replay_from_read_not_collapsed_warm_counts(self):
        m, a, b, c = [self.row(key, at) for key, at in
                      [('M', 1000), ('A', 2000), ('B', 3000), ('C', 4000)]]
        read = self.snapshot([m, a])
        for buffered, fresh, count in (([b, c], [m, a, b, c], 4),
                                        ([a, c], [m, a, c], 3)):
            with self.subTest(count=count):
                result = read.replay_complete(buffered)
                self.assertEqual(self.snapshot(fresh), result)
                self.assertEqual(count, result.count)
                self.assertEqual(2, read.count)

    def test_same_timestamp_groups_and_all_attest_effects_match_fresh(self):
        for first, last, expected in ((['A'], ['B'], ['A', 'C', 'B']),
                                      ([], ['A', 'B'], ['C', 'A', 'B'])):
            with self.subTest(first=first):
                def row(key, refs):
                    return self.row(key, 2000, kind='verdict', verdictKind='reviewed-clean',
                                    bySession='author-' + key,
                                    commitRefs=[{'commit': value} for value in refs])
                b, c, d = row('b', first), row('c', ['C']), row('d', last)
                read = self.snapshot([b, d])
                replayed = read.replay_complete([b, c, c, d])
                fresh = self.snapshot([b, c, d])
                # Author-map insertion order is not a published ordering.
                self.assertEqual(fresh.count, replayed.count)
                self.assertEqual(dict(fresh.authors), dict(replayed.authors))
                self.assertEqual(fresh.author_ids, replayed.author_ids)
                self.assertEqual(fresh.kinds, replayed.kinds)
                self.assertEqual(fresh.verdicts, replayed.verdicts)
                self.assertEqual(fresh.latest, replayed.latest)
                self.assertEqual(fresh.activity, replayed.activity)
                self.assertEqual(fresh.frontier, replayed.frontier)
                self.assertEqual(fresh.refs, replayed.refs)
                self.assertEqual(expected, [ref['commit'] for ref in replayed.refs])

    def test_empty_read_initializes_and_pre_read_notices_are_superseded(self):
        first = self.row('first', 1000)
        later = self.row('later', 2000)
        empty = self.snapshot([])
        self.assertEqual(self.snapshot([first, later]), empty.replay_complete([first, later]))
        self.assertIs(empty, empty.replay_complete([]))
        complete = self.snapshot([first, later])
        self.assertIs(complete, complete.replay_complete([first, later]))

    def test_unusable_complete_max_does_not_mutate_live_contribution(self):
        live = self.snapshot([self.row('live', 4000)])
        read = self.snapshot([self.row('read', 1000)])
        invalid = read._replace(frontier=AttestFrontier())
        with self.assertRaisesRegex(ValueError, 'no usable frontier'):
            invalid.replay_complete([self.row('later', 5000)])
        self.assertEqual(self.snapshot([self.row('live', 4000)]), live)
