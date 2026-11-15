"""Closed assignment contributions survive release as per-item facts."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_model import EvidenceModel
from atc_upstream import Read
from test_service import work, summarized_work


class ClosedAssignmentFoldTest(unittest.TestCase):
    def model(self, summary=False):
        model = EvidenceModel()
        self.read(model, 'work-items', [summarized_work(attest_counts={'progress': 3},
                  holders=['author'], refs=[{'commit': 'canonical'}]) if summary else work()])
        self.read(model, 'sessions', [dict(sessionKey=key, state='active', displayName=key)
                                     for key in ('author', 'other')])
        self.read(model, 'roles', [])
        assignments = [dict(id='first', workItemId='work', holderKey='author', state='closed',
                            effectKind='code', openedAt=1, closedAt=50, subject='discard first'),
                       dict(id='open', workItemId='work', holderKey='author', state='open',
                            effectKind='review', reviewsAssignmentId='first'),
                       dict(id='last', workItemId='work', holderKey='other', state='closed',
                            effectKind='spec', closedAt=60, subject='discard last',
                            commitRefCorrections=[dict(commitRefs=[{'commit': 'corrected'}],
                                                       note='discard correction note', id='correction')])]
        self.read(model, 'assignments', assignments)
        for i, row in enumerate(assignments):
            self.read(model, 'attests', [dict(id='note-'+row['id'], kind='progress', ts=10*(i+1),
                      bySession=row['holderKey'], note='discard note', commitRefs=[{'commit': row['id']}])], row['id'])
        for session in ('author', 'other'):
            for resource in ('wakes', 'messages', 'turns'):
                self.read(model, resource, [], session)
        self.read(model, 'turns', [dict(seq=1, sessionKey='author', assignmentId='first',
                  createdAt=70, startedAt=80, endedAt=None),
                  dict(seq=2, sessionKey='author', assignmentId='last', createdAt=85, endedAt=90)], 'author')
        return model

    @staticmethod
    def read(model, resource, rows, scope=None, complete=True):
        model.accept(resource, scope, Read(rows, complete, 100, 'fixture'))

    def test_fold_preserves_all_fields_order_live_attribution_and_freshness(self):
        for summary in (False, True):
            with self.subTest(summary=summary):
                folded = self.model(summary)
                baseline = deepcopy(folded)
                before = baseline.snapshot(1000)
                self.assertEqual({('attests', 'first'), ('attests', 'last')}, folded.fold_closed_assignments())
                self.assertEqual(before, folded.snapshot(1000))
                self.assertEqual({'open'}, set(folded.inventory('assignments').rows))
                self.assertEqual({'open'}, set(folded.inventory('assignments').observations))
                self.assertNotIn(('attests', 'first'), folded.inventories)
                self.assertNotIn(('attests', 'last'), folded.inventories)
                self.assertEqual(2, folded.closed_items['work']['count'])
                self.assertEqual({'first': 'work'}, folded.live_assignment_items)
                self.assertEqual(set(), folded.fold_closed_assignments())
                self.assertEqual(before, folded.snapshot(1000))
                for model in (baseline, folded):
                    model.mark_unconfirmed(1001)
                self.assertEqual(baseline.snapshot(1002), folded.snapshot(1002))
                for model in (baseline, folded):
                    model.invalidate()
                self.assertEqual(baseline.snapshot(1003), folded.snapshot(1003))

    def test_authoritative_assignment_removal_discards_folded_contributions(self):
        folded = self.model()
        baseline = deepcopy(folded)
        folded.fold_closed_assignments()
        for model in (baseline, folded):
            self.read(model, 'assignments', [])
        self.assertEqual({}, folded.closed_items)
        self.assertEqual({}, folded.live_assignment_items)
        self.assertEqual(baseline.snapshot(1000), folded.snapshot(1000))

    def test_membership_refresh_preserves_current_turn_timing_and_removal_releases_it(self):
        folded = self.model()
        baseline = deepcopy(folded)
        members = deepcopy(list(baseline.inventory('assignments').rows.values()))
        folded.fold_closed_assignments()
        for model in (baseline, folded):
            self.read(model, 'assignments', deepcopy(members))
        self.assertEqual(baseline.snapshot(1000), folded.snapshot(1000))
        folded.fold_closed_assignments()
        self.assertEqual(90, folded.item_activity()['work'])
        self.assertEqual(baseline.snapshot(1000), folded.snapshot(1000))
        # The maximum belongs to last, not permanently to the whole item.
        for model in (baseline, folded):
            self.read(model, 'assignments', [deepcopy(row) for row in members
                                             if row['id'] != 'last'])
        folded.fold_closed_assignments()
        self.assertNotIn('last', folded.item_contributions.entries)
        self.assertEqual(80, folded.item_activity()['work'])
        self.assertEqual(baseline.snapshot(1000), folded.snapshot(1000))

    def test_closed_assignment_live_turn_finish_advances_item_after_source_release(self):
        for complete in (False, True):
            with self.subTest(complete=complete):
                folded = self.model()
                baseline = deepcopy(folded)
                folded.fold_closed_assignments()
                for model in (baseline, folded):
                    self.read(model, 'turns', [dict(seq=1, sessionKey='author',
                              assignmentId='first', createdAt=70, startedAt=80,
                              endedAt=700000)], 'author', complete=complete)
                self.assertEqual(baseline.snapshot(800000), folded.snapshot(800000))
                self.assertEqual(700000, folded.item_activity()['work'])
                self.assertEqual({}, folded.live_assignment_items)
                self.assertNotIn('first', folded.summary('turns', 'author')['assignmentActivity'])
                self.assertEqual(baseline.snapshot(800001), folded.snapshot(800001))

    def test_retirement_preserves_current_authored_activity_and_item_facts(self):
        folded = self.model()
        folded.fold_closed_assignments()
        self.read(folded, 'sessions', [dict(sessionKey='author', state='active', displayName='author'),
                                      dict(sessionKey='other', state='retired', displayName='other')])
        before = folded.snapshot(1000)
        folded.release_session_histories({'author'})
        self.assertEqual(before, folded.snapshot(1000))
        part = folded.closed_items['work']
        self.assertEqual({'author': 10, 'other': 30}, part['summary']['authors'])
        self.assertIn('other', part['summary']['authors'])
        self.assertEqual(part['summary']['authors']['other'], folded.session_activity['other'])
        self.assertEqual(90, folded.item_activity()['work'])

    def test_closed_review_timing_and_unknown_code_remain_exact(self):
        for tested_at in (30, 400000):
            for effect in (None, 'review'):
                with self.subTest(tested_at=tested_at, effect=effect):
                    folded = self.model()
                    self.read(folded, 'assignments', [dict(id='terminal', workItemId='work',
                              state='closed', holderKey='author', effectKind=effect)])
                    self.read(folded, 'attests', [dict(id='done', kind='completion', ts=10),
                              dict(id='clean', kind='verdict', verdictKind='reviewed-clean', ts=20),
                              dict(id='tested', kind='verdict', verdictKind='tests-passed', ts=tested_at)], 'terminal')
                    before = folded.snapshot(500000)
                    folded.fold_closed_assignments()
                    self.assertEqual(before, folded.snapshot(500000))
