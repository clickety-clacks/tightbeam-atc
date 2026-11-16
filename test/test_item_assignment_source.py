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
from atc_reads import AttestReadFlight, ReadQueue
from atc_service import LiveService
from atc_upstream import Gateway, Read
from atc_summary import ItemActivityFold
from test_closed_folding import ClosedAssignmentFoldTest
from atc_contributions import AttestFrontier
from atc_assignment_conflicts import AssignmentReadConflicts, assignment_projection_fingerprint
from atc_payloads import ASSIGNMENT_FIELDS
from publication_assertions import assert_publication_equal, assert_replay_current


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
        self.assertEqual(['work-item-get', 'work-item-trace', 'assignment-get',
                          'assignment-get', 'assignment-get', 'attests', 'attests', 'attests'],
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

    def test_complete_item_snapshot_preserves_new_open_or_closed_contribution(self):
        for state in ('open', 'closed'):
            with self.subTest(state=state):
                snapshot = self.result()
                model = deepcopy(self.model)
                model.fold_closed_assignments()
                new = dict(id='new', workItemId='work', holderKey='author', state='open',
                           openedAt=40, closedAt=None, effectKind='code', files=[],
                           commitRefCorrections=[])
                model.apply_assignment_projection(new, at=100, source='feed')
                note = dict(id='new-attest', assignmentId='new', kind='progress',
                            bySession='author', ts=700, commitRefs=[{'commit': 'new'}])
                self.assignments.insert(0, new)
                self.attests['new'] = [note]
                try:
                    model.accept('attests', 'new', self.gateway.read_current('attests', 'new'))
                    model.capture_assignment_contributions()
                    if state == 'closed':
                        new.update(state='closed', closedAt=800)
                        model.apply_assignment_projection(new, at=100, source='feed')
                    calls = list(self.calls)
                    snapshot.folded.preserve_assignments(model, set(), arrivals={'new'})
                    self.assertEqual(calls, self.calls)
                    self.assertEqual(0, snapshot.folded.contributions['new'].order)
                    self.assertEqual(state, snapshot.folded.contributions['new'].state)
                    self.assertEqual(1, snapshot.folded.contributions['new'].attests.count)
                    self.assertNotIn('new', snapshot.folded.assignment_fingerprints)
                    expected = deepcopy(model)
                    expected.replace_item_assignments('work', self.result())
                    model.replace_item_assignments('work', snapshot)
                    assert_publication_equal(self, expected.snapshot(1000), model.snapshot(1000),
                                             'r29-item-created-after-snapshot-' + state)
                finally:
                    self.assignments.pop(0)
                    self.attests.pop('new')

    def test_selected_turns_finish_and_release_raw_results_before_first_attest(self):
        order, raw_refs = [], []
        flight = AttestReadFlight()
        self.addCleanup(flight.discard)
        class TurnRow(dict):
            pass
        def turns(session, mapping):
            self.assertEqual({}, flight.buffers)
            order.append(('turns', session, dict(mapping)))
            rows = [TurnRow(seq=1, sessionKey=session, assignmentId=next(iter(mapping)),
                            createdAt=70, startedAt=80, endedAt=90)]
            raw_refs.extend(weakref.ref(row) for row in rows)
            fold = ItemActivityFold(mapping)
            fold.add(rows)
            return Read(rows, True, 100, 'turn-source', folded=fold)
        original = self.gateway.read_current
        def read_current(resource, scope, **kwargs):
            if resource == 'attests':
                gc.collect()
                self.assertTrue(raw_refs)
                self.assertTrue(all(ref() is None for ref in raw_refs))
                self.assertIn(scope, flight.buffers)
                self.assertEqual(['author', 'other'],
                                 [entry[1] for entry in order if entry[0] == 'turns'])
                order.append(('attests', scope))
            return original(resource, scope, **kwargs)
        self.gateway.read_item_activity = turns
        self.gateway.read_current = read_current
        result = self.reads._item_assignments('work',
            dict(liveIds=set(), attests=True, visibleSessions=set()), flight)
        self.assertTrue(result.complete, result.error)
        self.assertEqual([('turns', 'author', {'first': 'work', 'open': 'work'}),
                          ('turns', 'other', {'last': 'work'}),
                          ('attests', 'first'), ('attests', 'open'), ('attests', 'last')], order)
        self.assertEqual(90, result.folded.activity)
        self.assertEqual({'first', 'open', 'last'}, flight.buffers.keys())
        self.assertFalse(flight.closed)
        flight.apply(result.folded.replay_attests)
        self.assertEqual({}, flight.buffers)

    def test_failed_turn_portion_never_starts_attest_portion_or_returns_partial_stage(self):
        self.gateway.read_item_activity = lambda session, mapping: Read(
            [], False, 100, 'turn-source', 'fixture-failure')
        result = self.reads._read(('item-assignment-source', 'work', None),
            dict(liveIds=set(), attests=True, visibleSessions=set()))
        self.assertFalse(result.complete)
        self.assertEqual('fixture-failure', result.error)
        self.assertIsNone(result.folded)
        self.assertFalse(any(verb == 'attests' for verb, _ in self.calls))

    def test_turn_first_stage_does_not_roll_back_newer_published_turn_activity(self):
        fold = ItemActivityFold({'first': 'work'})
        fold.activity['work'] = 90
        self.gateway.read_item_activity = lambda session, mapping: Read(
            [], True, 100, 'turn-source', folded=fold)
        result = self.reads._read(('item-assignment-source', 'work', None),
            dict(liveIds=set(), attests=True, visibleSessions={'other'}))
        self.assertTrue(result.complete, result.error)
        self.model.fold_closed_assignments()
        self.model.closed_items['work']['turnActivity'] = 200
        self.model.closed_items['work']['activity'] = 200
        self.model.hold_item_assignments('work')
        self.model.replace_item_assignments('work', result)
        self.assertEqual(200, self.model.closed_items['work']['turnActivity'])
        self.assertEqual(200, self.model.item_activity()['work'])

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

    def test_closed_read_projection_can_be_compared_without_retaining_its_row(self):
        result = self.result()
        self.assertTrue(result.complete, result.error)
        stage = result.folded
        self.assertNotIn('first', stage.open_rows)
        self.assertEqual({'first', 'open', 'last'}, set(stage.assignment_fingerprints))
        row = dict.fromkeys(ASSIGNMENT_FIELDS) | deepcopy(self.assignments[0])
        self.assertEqual(assignment_projection_fingerprint(row), stage.assignment_fingerprints['first'])
        flight = AssignmentReadConflicts()
        row.update(state='open', closedAt=None)
        flight.observe(dict(resource='assignments', op='upsert',
            refs={'assignmentId': 'first'}, payload=row, **{'class': 'assignment.reopened'}),
            delivery=12)
        calls = list(self.calls)
        self.assertEqual({'first': 12}, flight.conflict_fingerprints(
            stage.assignment_fingerprints.items(), complete=True))
        self.assertIsNone(flight.conflict_fingerprints(
            stage.assignment_fingerprints.items(), complete=False))
        self.assertEqual(calls, self.calls)
        self.assertTrue(all(isinstance(value, bytes) and len(value) == 32
                            for value in stage.assignment_fingerprints.values()))
        self.model.hold_item_assignments('work')
        self.model.replace_item_assignments('work', result)
        self.assertEqual({}, stage.assignment_fingerprints)
        self.assertFalse(hasattr(self.model, 'assignment_fingerprints'))

    def test_conflicted_item_projection_keeps_new_attests_and_full_publication(self):
        # A held closed projection can conflict with a reopened snapshot, and
        # a held open projection can conflict with a closed snapshot. Neither
        # conflict authorizes discarding the independent complete attest read.
        for aid in ('first', 'open'):
            with self.subTest(assignment=aid):
                original_rows = deepcopy(self.assignments)
                original_attests = deepcopy(self.attests)
                current = deepcopy(self.model)
                current.fold_closed_assignments()
                old = current.item_contributions.entries[aid]
                row = next(row for row in self.assignments if row['id'] == aid)
                row.update(state='open' if old.state == 'closed' else 'closed',
                           closedAt=None if old.state == 'closed' else 500)
                snapshot = self.result()
                self.assertTrue(snapshot.complete, snapshot.error)
                read_pin = snapshot.folded.contributions[aid].detail_fingerprint
                notice = dict(id='new-' + aid, assignmentId=aid, kind='progress',
                              ts=600, bySession='other', commitRefs=[{'commit': 'new-' + aid}])
                snapshot.folded.replay_attests({aid: [notice]})
                read_attests = snapshot.folded.contributions[aid].attests
                before_live = deepcopy(current.snapshot(1000))
                calls = list(self.calls)
                self.assertTrue(snapshot.folded.preserve_assignments(current, {aid}))
                self.assertEqual(calls, self.calls)
                self.assertEqual(before_live, current.snapshot(1000))
                protected = snapshot.folded.contributions[aid]
                self.assertEqual(old.state, protected.state)
                self.assertEqual(old.closed_at, protected.closed_at)
                self.assertEqual(old.corrections, protected.corrections)
                self.assertEqual(read_attests, protected.attests)
                self.assertEqual(read_pin, protected.detail_fingerprint)
                self.assertEqual(2, protected.attests.count)
                self.assertEqual(600, snapshot.folded.session_activity['other'])
                if old.state == 'open':
                    self.assertEqual(read_pin, snapshot.folded.open_attests[aid].fingerprint)
                    self.assertEqual(read_attests.summary(), snapshot.folded.open_attests[aid].folded)
                else:
                    self.assertNotIn(aid, snapshot.folded.open_rows)
                    self.assertNotIn(aid, snapshot.folded.open_attests)
                self.assertFalse(snapshot.folded.closed['detailsKnown'])

                # Expected mixed source uses the established mutable projection
                # and the actual new immutable attest. This is not an upstream
                # fresh-equality claim while R29's publication hold is active.
                self.assignments = original_rows
                self.attests[aid].append(notice)
                expected = self.result()
                self.assertTrue(expected.complete, expected.error)
                self.assert_staged_replay_publication(snapshot, expected,
                                                     'r29-item-preserved-' + aid)
                self.attests = original_attests

    def test_invalid_protected_member_leaves_entire_item_stage_and_model_unchanged(self):
        result = self.result()
        self.assertTrue(result.complete, result.error)
        self.model.fold_closed_assignments()
        stage = result.folded
        before_stage = deepcopy(stage)
        before_live = deepcopy(self.model.snapshot(1000))
        with self.assertRaisesRegex(ValueError, 'present source contributor'):
            stage.preserve_assignments(self.model, ['first', 'missing'])
        self.assertEqual(before_stage, stage)
        self.assertEqual(before_live, self.model.snapshot(1000))
        self.assertFalse(stage.preserve_assignments(self.model, set()))
        self.assertEqual(before_stage, stage)

    def test_first_discovered_contribution_needs_no_invented_prior_stage(self):
        new = dict(self.assignments[0], id='discovered', state='open', closedAt=None)
        self.assignments.append(new)
        self.attests['discovered'] = []
        result = self.result()
        self.assertTrue(result.complete, result.error)
        self.model.fold_closed_assignments()
        stage = result.folded
        original = deepcopy(stage)
        self.assertFalse(stage.preserve_assignments(self.model, {'discovered'}))
        self.assertEqual(original, stage)
        self.assertNotIn('discovered', self.model.item_contributions.entries)

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

    def assert_staged_replay_publication(self, snapshot, fresh_result, label):
        """Exercise publication/replay separately from the unwired flight owner."""
        with patch('atc_service.now_ms', return_value=1000):
            warm = LiveService(self.gateway)
            fresh = LiveService(self.gateway)
            self.addCleanup(warm.stop)
            self.addCleanup(fresh.stop)
            warm.model, fresh.model = deepcopy(self.model), deepcopy(self.model)
            warm._publish()
            baseline = deepcopy(warm.current)
            for service, result in ((warm, snapshot), (fresh, fresh_result)):
                service.model.hold_item_assignments('work')
                service.model.replace_item_assignments('work', result)
                service._publish()
            assert_publication_equal(self, fresh.current, warm.current, label)
            assert_replay_current(self, warm, baseline, label + '-replay')

    def test_overlap_histories_have_distinct_exact_item_counts_and_full_replay(self):
        # R27 H1/H2: the same snapshot must not be added to the old warm
        # count or replaced by only the maximum-timestamp notice.
        member = next(row for row in self.assignments if row['id'] == 'open')
        member.update(effectKind='code', reviewsAssignmentId=None)
        def row(identity, timestamp):
            return dict(id=identity, kind='progress', ts=timestamp,
                        bySession='author', commitRefs=[])
        m, a, b, c = [row(key, at) for key, at in
                      [('M', 1000), ('A', 2000), ('B', 3000), ('C', 4000)]]
        for buffered, expected, count in (([b, c], [m, a, b, c], 4),
                                           ([a, c], [m, a, c], 3)):
            with self.subTest(count=count):
                self.attests['open'] = [m, a]
                snapshot = self.result()
                self.assertTrue(snapshot.complete, snapshot.error)
                calls = list(self.calls)
                snapshot.folded.replay_attests({'open': buffered})
                self.assertEqual(calls, self.calls)
                self.assertEqual(count, snapshot.folded.contributions['open'].attests.count)
                self.assertEqual(4000, snapshot.folded.session_activity['author'])
                self.attests['open'] = expected
                fresh = self.result()
                self.assertTrue(fresh.complete, fresh.error)
                self.assert_staged_replay_publication(snapshot, fresh, 'r27-stage-count-' + str(count))

    def test_same_time_buffer_replay_preserves_both_item_reference_histories(self):
        for left, right, expected in ((['A'], ['B'], ['A', 'C', 'B']),
                                       ([], ['A', 'B'], ['C', 'A', 'B'])):
            with self.subTest(left=left):
                def row(identity, refs):
                    return dict(id=identity, kind='progress', ts=2000,
                        bySession='author', commitRefs=[{'commit': ref} for ref in refs])
                b, c, d = row('b', left), row('c', ['C']), row('d', right)
                self.attests['first'] = [b, d]
                snapshot = self.result()
                self.assertTrue(snapshot.complete, snapshot.error)
                snapshot.folded.replay_attests({'first': [b, c, c, d]})
                contribution = snapshot.folded.contributions['first'].attests
                self.assertEqual(3, contribution.count)
                self.assertEqual(expected, [ref['commit'] for ref in contribution.refs])
                self.attests['first'] = [b, c, d]
                fresh = self.result()
                self.assertTrue(fresh.complete, fresh.error)
                self.assert_staged_replay_publication(snapshot, fresh,
                    'r27-stage-groups-' + ('nonempty' if left else 'empty'))

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
