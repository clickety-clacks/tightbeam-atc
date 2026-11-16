"""Older source disposal preserves full item state and later replacements."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'server'))
from atc_model import EvidenceModel
from atc_upstream import Read


class OlderItemFoldingTest(unittest.TestCase):
    def fixture(self):
        model = EvidenceModel()
        items = [
            dict(id='old', title='Older displayed work', state='closed', createdAt=1,
                 closedAt=8, bodyUpdatedAt=12, rowVersion=13, body='Detail only',
                 specRefName='detail-spec', holders=[], started=True,
                 attestCounts={}, verdictCounts={}, effectiveCommitRefs=[], landings=[]),
            dict(id='current', title='Current', state='open', createdAt=2, rowVersion=14),
            dict(id='ice', title='Iceboxed', state='iceboxed', createdAt=3,
                 iceboxedAt=11, rowVersion=15, body='Other detail'),
            dict(id='failed', title='Failed but unaccepted', state='failed', createdAt=4,
                 rowVersion=16, body='Still current'),
        ]
        model.accept('work-items', None, Read(items, True, 20, 'items'))
        for resource in ('assignments', 'sessions', 'roles', 'artifacts'):
            model.accept(resource, None, Read([], True, 20, resource))
        return model, items

    def test_fold_preserves_every_field_and_source_order_outside_window(self):
        model, items = self.fixture()
        before = model.snapshot(40*86400000)
        revision = model.details_revision
        model.fold_older_work_items()
        self.assertEqual(before, model.snapshot(40*86400000))
        self.assertEqual([row['id'] for row in items], list(model.work_items()))
        self.assertEqual(revision, model.details_revision)
        self.assertEqual({'current', 'failed'}, set(model.inventory('work-items').rows))
        self.assertEqual({'current', 'failed'}, set(model.inventory('work-items').observations))
        for item in model.older_items.values():
            self.assertNotIn('body', item['fields'])
            self.assertNotIn('specRefName', item['fields'])
        old = next(row for row in before['items'] if row['id'] == 'old')
        self.assertEqual(8, old['finishedAt'])
        self.assertEqual(13, old['latestActivityAt'])

    def test_partial_updates_keep_absent_item_facts_and_order(self):
        model, items = self.fixture()
        source = deepcopy(model)
        model.fold_older_work_items()
        for row in (items[0], dict(items[2], title='Changed icebox')):
            for target in (source, model):
                target.accept('work-items', None, Read([row], False, 30, 'partial'))
            self.assertEqual(source.snapshot(40*86400000), model.snapshot(40*86400000))
        # Both older rows have reappeared; source order still must not move them
        # behind current rows merely because the compact map is now empty.
        self.assertEqual([row['id'] for row in items], list(model.work_items()))
        model.fold_older_work_items()
        self.assertEqual(source.snapshot(40*86400000), model.snapshot(40*86400000))

    def test_body_only_change_invalidates_detail_revision_and_reopen_replaces(self):
        model, items = self.fixture()
        model.fold_older_work_items()
        revision = model.details_revision
        model.detail('work-items', None, Read([items[0]], True, 30, 'detail'))
        self.assertEqual(revision, model.details_revision)
        model.fold_older_work_items()
        changed = dict(items[0], body='New full detail, same board fields')
        model.detail('work-items', None, Read([changed], True, 31, 'detail'))
        self.assertGreater(model.details_revision, revision)
        model.fold_older_work_items()
        reopened = dict(changed, state='open', rowVersion=32)
        model.detail('work-items', None, Read([reopened], True, 32, 'detail'))
        model.fold_older_work_items()
        self.assertNotIn('old', model.older_items)
        self.assertEqual(reopened, model.work_items()['old'])
        self.assertEqual([row['id'] for row in items], list(model.work_items()))

    def test_complete_replacement_removes_absent_items_and_outage_facts_match(self):
        model, items = self.fixture()
        source = deepcopy(model)
        model.fold_older_work_items()
        for target in (source, model):
            target.mark_unconfirmed(30)
        self.assertEqual(source.snapshot(40*86400000), model.snapshot(40*86400000))
        for target in (source, model):
            target.invalidate()
        self.assertEqual(source.snapshot(40*86400000), model.snapshot(40*86400000))
        for target in (source, model):
            target.accept('work-items', None, Read([items[3], items[2]], True, 40, 'replacement'))
        model.fold_older_work_items()
        self.assertEqual(source.snapshot(40*86400000), model.snapshot(40*86400000))
        self.assertEqual(['failed', 'ice'], list(model.work_items()))
        self.assertNotIn('old', model.older_items)
