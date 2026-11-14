"""Current provenance retains published facts and releases absent contributors."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_contributions import AssignmentContribution, ItemContributions
from atc_model import ClosedAssignmentFold
from atc_evidence import HistoryInventory
from atc_upstream import Read
from test_closed_folding import ClosedAssignmentFoldTest


class ItemContributionsTest(unittest.TestCase):
    def entries(self):
        model = ClosedAssignmentFoldTest().model()
        return {key: AssignmentContribution.capture(row, model.inventory('attests', key), index)
                for index, (key, row) in enumerate(model.inventory('assignments').rows.items())}

    def test_partial_cannot_erase_other_contributors_complete_and_item_exit_release(self):
        entries = self.entries()
        store = ItemContributions()
        self.assertTrue(store.replace_item('work', entries, complete=True))
        before = dict(store.entries)
        self.assertFalse(store.replace_item('work', {'first': entries['first']}, complete=False))
        self.assertEqual(before, store.entries)
        self.assertTrue(store.replace_item('work', {'first': entries['first']}, complete=True))
        self.assertEqual({'first'}, set(store.entries))
        store.retain_items(set())
        self.assertEqual({}, store.entries)

    def test_projection_preserves_counts_authors_refs_corrections_without_source_text(self):
        entries = self.entries()
        first, last = entries['first'], entries['last']
        self.assertIsInstance(first, tuple)
        self.assertFalse(hasattr(first, '__dict__'))
        self.assertEqual(1, first.attests.count)
        self.assertEqual((('author', 10),), first.attests.authors)
        self.assertEqual(({'commit': 'first'},), first.attests.refs)
        self.assertEqual(({'commitRefs': [{'commit': 'corrected'}], 'id': 'correction'},),
                         last.corrections)
        self.assertNotIn('discard', repr(entries))
        self.assertEqual(60, last.closed_at)
        self.assertEqual('other', last.holder)

    def test_assignment_close_and_reopen_keep_existing_attest_and_correction_facts(self):
        original = self.entries()['last']
        closed = original.assignment_update(dict(
            state='closed', closedAt=75, subject='not a current contribution'))
        self.assertEqual(75, closed.closed_at)
        self.assertIs(original.attests, closed.attests)
        self.assertIs(original.corrections, closed.corrections)
        self.assertEqual(original.evidence, closed.evidence)
        self.assertEqual(original.detail_fingerprint, closed.detail_fingerprint)
        self.assertNotIn('not a current contribution', repr(closed))
        # Explicit null terminal time on reopening clears the assignment fact,
        # while omitted fields in a notice do not clear unrelated contributions.
        reopened = closed.assignment_update(dict(state='open', closedAt=None))
        self.assertEqual('open', reopened.state)
        self.assertIsNone(reopened.closed_at)
        self.assertIs(original.attests, reopened.attests)
        self.assertEqual(original.order, reopened.order)
        self.assertEqual(original.holder, reopened.holder)
        self.assertEqual(original.corrections, reopened.corrections)

    def test_assignment_source_correction_replaces_only_current_correction_field(self):
        original = self.entries()['last']
        changed = original.assignment_update(dict(commitRefCorrections=[dict(
            id='new-correction', commitRefs=[{'commit': 'new'}],
            note='discarded text', reason='discarded reason')]), order=4)
        self.assertEqual(({'id': 'new-correction', 'commitRefs': [{'commit': 'new'}]},),
                         changed.corrections)
        self.assertEqual(4, changed.order)
        self.assertIs(original.attests, changed.attests)
        self.assertEqual((), changed.assignment_update(
            {'commitRefCorrections': []}).corrections)
        self.assertEqual(original.corrections,
                         original.assignment_update({'closedAt': 90}).corrections)

    def test_summary_skipped_contribution_does_not_claim_an_attest_read(self):
        inventory = HistoryInventory('attests')
        contribution = AssignmentContribution.capture(
            dict(id='skipped', workItemId='work', state='closed'), inventory, 0)
        fold = ClosedAssignmentFold()
        fold.add_contribution(contribution)
        self.assertFalse(fold.part['attestsRead'])
        self.assertFalse(fold.part['detailsKnown'])
        self.assertFalse(fold.part['facts']['complete'])

    def test_reduction_keeps_order_corrections_and_authors_after_contributor_removal(self):
        entries = self.entries()
        store = ItemContributions()
        store.replace_item('work', entries, complete=True)

        def reduced():
            fold = ClosedAssignmentFold()
            # Iteration order must not change the published reference order.
            for value in reversed(tuple(store.entries.values())):
                if value.state != 'open':
                    fold.add_contribution(value)
            return fold.part

        part = reduced()
        self.assertEqual(2, part['count'])
        self.assertEqual(60, part['closedAt'])
        self.assertEqual(2, part['summary']['count'])
        self.assertEqual({'progress': 2}, part['summary']['kinds'])
        self.assertEqual({'author': 10, 'other': 30}, part['summary']['authors'])
        self.assertEqual([{'commit': 'first'}, {'commit': 'corrected'}],
                         [ref for _, _, ref in sorted(part['refs'], key=lambda entry: entry[:2])])
        self.assertEqual([(2, 0, {'commitRefs': [{'commit': 'corrected'}], 'id': 'correction'})],
                         part['corrections'])
        self.assertFalse(part['detailsKnown'])
        self.assertTrue(part['attestsRead'])
        # Partial omission preserves the other author's timing and correction.
        store.replace_item('work', {'first': entries['first']}, complete=False)
        self.assertEqual(part['corrections'], reduced()['corrections'])
        self.assertEqual(part['summary']['authors'], reduced()['summary']['authors'])
        # Complete removal releases those contributions; it is not a max-only merge.
        store.replace_item('work', {'first': entries['first']}, complete=True)
        remaining = reduced()
        self.assertEqual(1, remaining['count'])
        self.assertEqual(50, remaining['closedAt'])
        self.assertEqual({'author': 10}, remaining['summary']['authors'])
        self.assertEqual([], remaining['corrections'])

    def test_close_reduction_keeps_old_closed_facts_and_reopen_removes_only_its_part(self):
        entries = self.entries()

        def reduced():
            fold = ClosedAssignmentFold()
            for value in entries.values():
                if value.state != 'open':
                    fold.add_contribution(value)
            return fold.part

        before = reduced()
        entries['open'] = entries['open'].assignment_update(
            {'state': 'closed', 'closedAt': 100})
        closed = reduced()
        self.assertEqual(3, closed['count'])
        self.assertEqual(3, closed['summary']['count'])
        self.assertEqual(100, closed['closedAt'])
        self.assertEqual(before['corrections'], closed['corrections'])
        self.assertEqual({'author': 20, 'other': 30}, closed['summary']['authors'])
        self.assertEqual([{'commit': 'first'}, {'commit': 'open'}, {'commit': 'corrected'}],
                         [ref for _, _, ref in sorted(closed['refs'], key=lambda x: x[:2])])
        entries['last'] = entries['last'].assignment_update(
            {'state': 'open', 'closedAt': None})
        reopened = reduced()
        self.assertEqual(2, reopened['count'])
        self.assertEqual(2, reopened['summary']['count'])
        self.assertEqual({'author': 20}, reopened['summary']['authors'])
        self.assertEqual([], reopened['corrections'])
        self.assertEqual([{'commit': 'first'}, {'commit': 'open'}],
                         [ref for _, _, ref in sorted(reopened['refs'], key=lambda x: x[:2])])
        # Reopened source is still current; its correction belongs to the open
        # contribution, rather than being deleted or counted twice as closed.
        self.assertEqual(({'id': 'correction', 'commitRefs': [{'commit': 'corrected'}]},),
                         entries['last'].corrections)
        self.assertFalse(reopened['detailsKnown'])

    def test_model_disposal_preserves_contributions_and_item_exit_drops_them(self):
        model = ClosedAssignmentFoldTest().model()
        before = model.snapshot(1000)
        model.fold_closed_assignments()
        self.assertEqual({'first', 'open', 'last'}, set(model.item_contributions.entries))
        self.assertEqual({'open'}, set(model.inventory('assignments').rows))
        self.assertEqual(before, model.snapshot(1000))
        previous = dict(model.item_contributions.entries)
        model.accept('assignments', None, Read([], False, 1100, 'partial'))
        self.assertEqual(previous, model.item_contributions.entries)
        model.accept('work-items', None, Read([], True, 1200, 'complete'))
        self.assertEqual({}, model.item_contributions.entries)

    def test_newly_closed_raw_member_merges_with_previously_released_closed_members(self):
        from copy import deepcopy
        model = ClosedAssignmentFoldTest().model()
        baseline = deepcopy(model)
        model.fold_closed_assignments()
        old_pin = model.closed_items['work']['detailFingerprint']
        for current in (baseline, model):
            row = dict(current.inventory('assignments').rows['open'],
                       state='closed', closedAt=120)
            current.detail('assignments', None, Read([row], True, 1100, 'detail'))
        expected = baseline.snapshot(1200)
        model.fold_closed_assignments()
        self.assertEqual(expected, model.snapshot(1200))
        part = model.closed_items['work']
        self.assertEqual(3, part['count'])
        self.assertEqual(3, part['summary']['count'])
        self.assertEqual(120, part['closedAt'])
        self.assertEqual(90, part['turnActivity'])
        self.assertTrue(part['attestsRead'])
        self.assertFalse(part['detailsKnown'])
        self.assertEqual(old_pin, part['detailFingerprint'])
        self.assertEqual({}, model.inventory('assignments').rows)
        self.assertEqual({'first', 'open', 'last'}, set(model.item_contributions.entries))

    def test_new_close_does_not_clear_released_contributors_outage_evidence(self):
        from copy import deepcopy
        model = ClosedAssignmentFoldTest().model()
        baseline = deepcopy(model)
        model.fold_closed_assignments()
        for current in (baseline, model):
            current.mark_unconfirmed(1100)
            row = dict(current.inventory('assignments').rows['open'],
                       state='closed', closedAt=120)
            current.detail('assignments', None, Read([row], True, 1150, 'detail'))
        model.fold_closed_assignments()
        self.assertEqual(baseline.snapshot(1200), model.snapshot(1200))
        self.assertEqual('unconfirmed', model.closed_items['work']['facts']['freshness'])
        self.assertEqual(1100, model.closed_items['work']['facts']['unconfirmedSince'])

    def test_released_closed_assignment_projection_preserves_complete_publication(self):
        from copy import deepcopy
        for outage in (False, True):
            with self.subTest(outage=outage):
                model = ClosedAssignmentFoldTest().model()
                baseline = deepcopy(model)
                model.fold_closed_assignments()
                if outage:
                    model.mark_unconfirmed(1100)
                    baseline.mark_unconfirmed(1100)
                old = model.item_contributions.entries['last']
                pin = model.closed_items['work']['detailFingerprint']
                revision = model.details_revision
                # The ordinary projection omits corrections and attest rows.
                projection = dict(id='last', workItemId='work', state='closed',
                                  holderKey='other', closedAt=150, effectKind='code')
                source = dict(baseline.inventory('assignments').rows['last'], **projection)
                baseline.detail('assignments', None, Read([source], True, 1150, 'detail'))
                self.assertTrue(model.update_closed_assignment_contribution(projection))
                self.assertEqual(baseline.snapshot(1200), model.snapshot(1200))
                current = model.item_contributions.entries['last']
                self.assertIs(old.attests, current.attests)
                self.assertIs(old.corrections, current.corrections)
                self.assertEqual(150, model.closed_items['work']['closedAt'])
                self.assertEqual(90, model.closed_items['work']['turnActivity'])
                self.assertEqual(pin, model.closed_items['work']['detailFingerprint'])
                self.assertFalse(model.closed_items['work']['detailsKnown'])
                self.assertEqual(revision + 1, model.details_revision)
                self.assertNotIn('last', model.inventory('assignments').rows)
                self.assertNotIn(('attests', 'last'), model.inventories)
                self.assertTrue(model.update_closed_assignment_contribution(projection))
                self.assertEqual(revision + 1, model.details_revision)

    def test_closed_scoped_confirmation_preserves_other_contributors_warnings(self):
        from copy import deepcopy
        from publication_assertions import assert_publication_equal
        for warning in ('none', 'unconfirmed', 'stale'):
            with self.subTest(warning=warning):
                model = ClosedAssignmentFoldTest().model()
                baseline = deepcopy(model)
                model.fold_closed_assignments()
                for current in (model, baseline):
                    if warning == 'unconfirmed':
                        current.mark_unconfirmed(1100)
                    elif warning == 'stale':
                        current.invalidate()
                old = model.item_contributions.entries['first']
                pin = model.closed_items['work']['detailFingerprint']
                rows = list(baseline.inventory('attests', 'first').rows.values())
                rows.append(dict(id='new', kind='progress', ts=1200,
                                 bySession='author', commitRefs=[{'commit': 'new'}]))
                result = Read(rows, True, 1250, 'fixture')
                baseline.accept('attests', 'first', deepcopy(result))
                self.assertTrue(model.accept_closed_attest_read('first', result, connected=True))
                current = model.item_contributions.entries['first']
                self.assertEqual(2, current.attests.count)
                self.assertEqual('live', current.evidence.freshness)
                self.assertEqual(old.corrections, current.corrections)
                self.assertEqual(old.closed_at, current.closed_at)
                self.assertEqual(pin, model.closed_items['work']['detailFingerprint'])
                self.assertFalse(model.closed_items['work']['detailsKnown'])
                self.assertNotIn(('attests', 'first'), model.inventories)
                self.assertEqual('live' if warning == 'none' else warning,
                                 model.closed_items['work']['facts']['freshness'])
                assert_publication_equal(self, baseline.snapshot(1300), model.snapshot(1300),
                                         'r28-closed-scope-confirmation-' + warning)
                # Partial data must neither replace live facts nor certify
                # this closed scope. Its caller owns the existing retry path.
                before = dict(model.item_contributions.entries)
                self.assertFalse(model.accept_closed_attest_read(
                    'first', Read([], False, 1350, 'fixture'), connected=True))
                self.assertEqual(before, model.item_contributions.entries)

    def test_released_projection_cannot_override_pending_or_different_item_source(self):
        from copy import deepcopy
        model = ClosedAssignmentFoldTest().model()
        model.fold_closed_assignments()
        before = deepcopy(model.closed_items)
        contributions = dict(model.item_contributions.entries)
        projection = dict(id='last', workItemId='work', state='closed', closedAt=150)
        self.assertFalse(model.update_closed_assignment_contribution(
            dict(projection, workItemId='not-published')))
        self.assertFalse(model.update_closed_assignment_contribution(
            dict(projection, state='open', closedAt=None)))
        model.hold_item_assignments('work')
        self.assertFalse(model.update_closed_assignment_contribution(projection))
        self.assertEqual(contributions, model.item_contributions.entries)
        self.assertEqual(before, model.closed_items)

    def test_released_attest_notice_keeps_full_publication_pins_and_outage(self):
        from copy import deepcopy
        from publication_assertions import assert_publication_equal
        for outage in (False, True):
            with self.subTest(outage=outage):
                model = ClosedAssignmentFoldTest().model()
                baseline = deepcopy(model)
                model.fold_closed_assignments()
                if outage:
                    model.mark_unconfirmed(1100)
                    baseline.mark_unconfirmed(1100)
                prior = model.item_contributions.entries['first']
                pin = model.closed_items['work']['detailFingerprint']
                revision = model.details_revision
                notice = dict(id='later', assignmentId='first', ts=1500, kind='progress',
                              bySession='other', commitRefs=[{'commit': 'additional'}],
                              note='not retained contribution text')
                inventory = baseline.inventory('attests', 'first')
                self.assertTrue(inventory.payload(notice, at=1600, source='feed', policy='immutable'))
                baseline.fold_activity_inventory('attests', 'first')
                self.assertTrue(model.update_closed_attest_contribution('first', notice))
                # The service's existing positive-activity path runs separately
                # from the item fold; the item helper must not invent a read.
                model.fold_session_activity('other', 1500, 'attest', 'first',
                    dict(inventory.evidence(), status='known', complete=True,
                         freshness='live', unconfirmedSince=None), owner='work')
                assert_publication_equal(self, baseline.snapshot(1700), model.snapshot(1700),
                                         'r26-released-attest-' + str(outage))
                current = model.item_contributions.entries['first']
                self.assertEqual(prior.evidence, current.evidence)
                self.assertEqual(prior.detail_fingerprint, current.detail_fingerprint)
                self.assertEqual(pin, model.closed_items['work']['detailFingerprint'])
                self.assertFalse(model.closed_items['work']['detailsKnown'])
                self.assertEqual(90, model.closed_items['work']['turnActivity'])
                self.assertEqual(revision + 1, model.details_revision)
                self.assertNotIn(('attests', 'first'), model.inventories)
                self.assertNotIn('not retained contribution text', repr(current))
                before = model.snapshot(1700)
                self.assertTrue(model.update_closed_attest_contribution('first', notice))
                self.assertEqual(revision + 1, model.details_revision)
                self.assertEqual(before, model.snapshot(1700))
                model.hold_item_assignments('work')
                # R27 does not suppress valid delivered notices during an
                # independently required read. Failure must retain this live
                # effect; success replays it onto the complete snapshot.
                self.assertTrue(model.update_closed_attest_contribution(
                    'first', dict(notice, id='newer', ts=1800)))
                self.assertEqual(current.attests.count + 1,
                                 model.item_contributions.entries['first'].attests.count)
                self.assertIn('work', model.item_assignment_pending)

    def test_assignment_projection_new_close_and_reopen_keep_full_model_parity(self):
        from copy import deepcopy
        model = ClosedAssignmentFoldTest().model()
        baseline = deepcopy(model)
        model.fold_closed_assignments()
        new = dict(id='new', workItemId='work', state='open', holderKey='author',
                   openedAt=40, closedAt=None, effectKind='code',
                   reviewsAssignmentId=None)

        def baseline_projection(row):
            inventory = baseline.inventory('assignments')
            merged = dict(inventory.rows.get(row['id'], {}), **row)
            baseline.detail('assignments', None, Read([merged], True, 1100, 'feed'))
            inventory.rows = dict(sorted(inventory.rows.items(), key=lambda pair: (
                pair[1].get('openedAt') is not None, pair[1].get('openedAt') or 0,
                pair[0]), reverse=True))

        baseline_projection(new)
        self.assertEqual(set(), model.apply_assignment_projection(new, at=1100, source='feed'))
        for current in (model, baseline):
            current.accept('attests', 'new', Read([], True, 1100, 'fixture'))
        self.assertEqual(baseline.snapshot(1200), model.snapshot(1200))
        self.assertEqual(0, model.item_contributions.entries['new'].order)

        close = dict(new, state='closed', closedAt=150)
        baseline_projection(close)
        released = model.apply_assignment_projection(close, at=1100, source='feed')
        self.assertEqual({('attests', 'new')}, released)
        self.assertEqual(baseline.snapshot(1200), model.snapshot(1200))
        self.assertNotIn('new', model.inventory('assignments').rows)
        self.assertNotIn(('attests', 'new'), model.inventories)

        # The reopened member has both old attest references and a correction.
        # Neither is supplied by the assignment projection itself.
        reopen = dict(id='last', workItemId='work', holderKey='other', state='open',
                      openedAt=None, closedAt=None, effectKind='spec',
                      reviewsAssignmentId=None)
        baseline_projection(reopen)
        prior = model.item_contributions.entries['last']
        self.assertEqual(set(), model.apply_assignment_projection(reopen, at=1100, source='feed'))
        self.assertEqual(baseline.snapshot(1200), model.snapshot(1200))
        restored = model.inventory('attests', 'last')
        self.assertEqual({}, restored.rows)
        self.assertEqual(dict(prior.attests.kinds), dict(restored.folded['kinds']))
        self.assertEqual(prior.attests.frontier, restored.attest_frontier)
        self.assertEqual(prior.detail_fingerprint, restored.fingerprint)
        self.assertEqual(list(prior.corrections),
                         model.inventory('assignments').rows['last']['commitRefCorrections'])
        self.assertEqual(2, model.closed_items['work']['count'])

    def test_assignment_projection_does_not_complete_a_partial_item_source(self):
        from copy import deepcopy
        model = ClosedAssignmentFoldTest().model()
        model.fold_closed_assignments()
        model.hold_item_assignments('work')
        before = deepcopy(model.snapshot(1200))
        self.assertIsNone(model.apply_assignment_projection(dict(
            id='open', workItemId='work', state='closed', closedAt=150),
            at=1100, source='feed'))
        self.assertEqual(before, model.snapshot(1200))
        self.assertIn('work', model.item_assignment_pending)

    def test_membership_refresh_cannot_erase_released_attests_and_partial_retry_recovers(self):
        model = ClosedAssignmentFoldTest().model()
        assignment_rows = list(model.inventory('assignments').rows.values())
        source_attests = list(model.inventory('attests', 'first').rows.values())
        model.fold_closed_assignments()
        prior = model.item_contributions.entries['first']
        model.accept('assignments', None, Read(assignment_rows, True, 1100, 'membership'))
        model.capture_assignment_contributions()
        current = model.item_contributions.entries['first']
        self.assertEqual(prior.attests, current.attests)
        self.assertEqual(prior.evidence, current.evidence)
        self.assertEqual(prior.detail_fingerprint, current.detail_fingerprint)

        model.accept('attests', 'first', Read([], False, 1200, 'partial', 'unavailable'))
        model.capture_assignment_contributions()
        incomplete = model.item_contributions.entries['first']
        self.assertEqual(prior.attests, incomplete.attests)
        self.assertFalse(incomplete.evidence.complete)
        self.assertEqual('stale', incomplete.evidence.freshness)

        model.accept('attests', 'first', Read(source_attests, True, 1300, 'confirmed'))
        model.capture_assignment_contributions()
        recovered = model.item_contributions.entries['first']
        self.assertEqual(prior.attests, recovered.attests)
        self.assertTrue(recovered.evidence.complete)
        self.assertEqual('live', recovered.evidence.freshness)
        self.assertEqual(1300, recovered.evidence.observed_at)

    def test_membership_without_corrections_keeps_released_current_correction(self):
        model = ClosedAssignmentFoldTest().model()
        rows = [dict(row) for row in model.inventory('assignments').rows.values()]
        model.fold_closed_assignments()
        prior = model.item_contributions.entries['last'].corrections
        for row in rows:
            row.pop('commitRefCorrections', None)
        model.accept('assignments', None, Read(rows, False, 1100, 'partial-membership'))
        model.capture_assignment_contributions()
        self.assertEqual(prior, model.item_contributions.entries['last'].corrections)
        for row in rows:
            row['commitRefCorrections'] = []
        model.accept('assignments', None, Read(rows, True, 1200, 'complete-source'))
        model.capture_assignment_contributions()
        self.assertEqual((), model.item_contributions.entries['last'].corrections)

    def test_complete_membership_reorders_existing_open_and_closed_contributions(self):
        model = ClosedAssignmentFoldTest().model()
        original_rows = list(model.inventory('assignments').rows.values())
        model.fold_closed_assignments()
        model.capture_assignment_contributions()
        self.assertEqual(1, model.item_contributions.entries['open'].order)
        inserted = dict(id='new-first', workItemId='work', holderKey='author',
                        state='open', effectKind='code', openedAt=200)
        # The complete source is authoritative for order; the former open-row
        # position cannot survive insertion of a preceding assignment.
        model.accept('assignments', None,
                     Read([inserted, *original_rows], True, 1100, 'membership'))
        model.capture_assignment_contributions()
        self.assertEqual({'new-first': 0, 'first': 1, 'open': 2, 'last': 3},
                         {key: value.order for key, value in
                          model.item_contributions.entries.items()})

    def test_partial_membership_and_released_subset_do_not_renumber_contributors(self):
        model = ClosedAssignmentFoldTest().model()
        last = model.inventory('assignments').rows['last']
        model.fold_closed_assignments()
        prior = {key: value.order for key, value in model.item_contributions.entries.items()}
        model.capture_assignment_contributions()
        self.assertEqual(prior, {key: value.order for key, value in
                                model.item_contributions.entries.items()})
        model.accept('assignments', None, Read([last], False, 1100, 'partial'))
        model.capture_assignment_contributions()
        self.assertEqual(prior, {key: value.order for key, value in
                                model.item_contributions.entries.items()})

    def test_published_older_item_keeps_contributors_until_complete_item_exit(self):
        model = ClosedAssignmentFoldTest().model()
        item = dict(model.work_items()['work'], state='closed')
        model.accept('work-items', None, Read([item], True, 1100, 'complete'))
        model.fold_closed_assignments()
        prior = dict(model.item_contributions.entries)
        before = model.snapshot(1200)
        model.fold_older_work_items()
        self.assertNotIn('work', model.inventory('work-items').rows)
        model.release_auxiliary_sources()
        self.assertEqual(prior, model.item_contributions.entries)
        self.assertEqual(before, model.snapshot(1200))
        model.accept('work-items', None, Read([], False, 1300, 'partial'))
        model.release_auxiliary_sources()
        self.assertEqual(prior, model.item_contributions.entries)
        model.accept('work-items', None, Read([], True, 1400, 'complete'))
        self.assertEqual({}, model.item_contributions.entries)
