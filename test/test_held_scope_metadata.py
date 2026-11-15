"""R37 comparison-accounting units; not source transport or recovery proof."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from atc_assignment_conflicts import AssignmentHolds
from publication_assertions import held_scope_identities, held_scope_metadata


class HeldScopeMetadataTest(unittest.TestCase):
    def frame(self, holds, *, extra=(), unconfirmed=()):
        # Explicit classification inputs. These are synthetic scope graphs,
        # not claims that a production pending/failed read was exercised.
        active = {('turns', 'session'): 10}
        active.update({('attests', aid): None if aid in unconfirmed else 10
                       for aid in extra})
        history = {('messages', 'session'): 9}
        inventories = {scope: SimpleNamespace(confirmed_at=at)
                       for scope, at in (active | history).items()}
        def group(scopes):
            return dict(scopeCount=len(scopes),
                        unconfirmedScopes=sum(at is None for at in scopes.values()),
                        oldestConfirmedAt=min((at for at in scopes.values()
                                               if at is not None), default=None))
        metadata = dict(historyScopes=len(active) + len(history), active=group(active),
                        history=group(history), estimatedSweepMs=300000)
        return SimpleNamespace(assignment_holds=holds,
            model=SimpleNamespace(inventories=inventories),
            backstop=SimpleNamespace(active=set(active)),
            _history_scopes=lambda: list(active | history),
            _repeated_history_scopes=lambda: list(history),
            scope_budget=5, rebuild_interval=300,
            current={'freshness': {'reconciliation': metadata}})

    def shared(self, independent=True):
        holds = AssignmentHolds()
        for aid in ('A1', 'A2'):
            holds.hold(aid, 'work', None, at=20,
                       producer_recovery=True, producer='P')
        if independent:
            holds.hold('A1', 'work', None, at=20, incomplete_contribution=True)
        return holds

    def assert_delta(self, holds, extra, expected, label):
        fresh = self.frame(AssignmentHolds())
        warm = self.frame(holds, extra=extra)
        permitted, deltas = held_scope_metadata(self, fresh, warm, label)
        self.assertEqual(expected, deltas['historyScopes'])
        self.assertEqual(expected, deltas['active.scopeCount'])
        self.assertEqual(0, deltas['active.unconfirmedScopes'])
        self.assertEqual(0, deltas['history.scopeCount'])
        self.assertEqual(0, deltas['history.unconfirmedScopes'])
        self.assertEqual(10, permitted['active.oldestConfirmedAt'])
        self.assertEqual(9, permitted['history.oldestConfirmedAt'])
        self.assertEqual(300000, permitted['estimatedSweepMs'])

    def test_shared_producer_and_independent_assignment_count_two_then_one_then_zero(self):
        holds = self.shared()
        self.assertEqual({('attests', 'P'), ('attests', 'A1')},
                         held_scope_identities(self.frame(holds)))
        self.assert_delta(holds, ('P', 'A1'), 2, 'r37-unit-two-scopes')
        # Resolving one reference does not remove the shared P cause.
        holds.resolve_producer('A1')
        self.assert_delta(holds, ('P', 'A1'), 2, 'r37-unit-one-p-reference')
        holds.resolve_producer('A2')
        self.assert_delta(holds, ('A1',), 1, 'r37-unit-independent-remains')
        self.assertTrue(holds.assignments['A1'].incomplete_contribution)
        holds.resolve_contribution('A1')
        self.assert_delta(holds, (), 0, 'r37-unit-resolved')
        self.assertFalse(holds.items)

    def test_shared_producer_and_duplicate_cause_never_multiply_scope(self):
        holds = self.shared(independent=False)
        self.assert_delta(holds, ('P',), 1, 'r37-unit-shared-only')
        holds.hold('P', 'work', None, at=20)
        self.assertEqual({('attests', 'P')}, held_scope_identities(self.frame(holds)))
        self.assert_delta(holds, ('P',), 1, 'r37-unit-identical-independent-scope')

    def test_unknown_or_absent_producer_gives_no_scope_offset(self):
        holds = AssignmentHolds()
        holds.hold('A', 'work', None, at=20, producer_recovery=True)
        self.assertEqual(set(), held_scope_identities(self.frame(holds)))
        self.assert_delta(holds, (), 0, 'r37-unit-unidentified')
        holds.hold('B', 'work', None, at=20, producer_recovery=True, producer='absent')
        self.assert_delta(holds, (), 0, 'r37-unit-no-retained-scope')
        # Naming a producer never grants an allowance for a different scope.
        with self.assertRaises(AssertionError):
            self.assert_delta(holds, ('unrelated',), 1, 'r37-unit-reject-proxy')

    def test_same_scope_confirmation_difference_is_exactly_attributed(self):
        holds = self.shared(independent=False)
        fresh = self.frame(AssignmentHolds(), extra=('P',))
        warm = self.frame(holds, extra=('P',), unconfirmed=('P',))
        _permitted, deltas = held_scope_metadata(self, fresh, warm, 'r37-unit-confirmation')
        self.assertEqual(0, deltas['historyScopes'])
        self.assertEqual(0, deltas['active.scopeCount'])
        self.assertEqual(1, deltas['active.unconfirmedScopes'])
        warm.current['freshness']['reconciliation']['active']['unconfirmedScopes'] = 2
        with self.assertRaises(AssertionError):
            held_scope_metadata(self, fresh, warm, 'r37-unit-reject-invented-confirmation')
