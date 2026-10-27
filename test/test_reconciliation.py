"""Active backstop semantics; synthetic coverage, not gateway load evidence."""

from pathlib import Path
import queue
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_model import EvidenceModel
from atc_reads import ReadQueue
from atc_reconciliation import ActiveBackstop, ACTIVE_BUDGET_MS, ACTIVE_REFRESH_MS, ACTIVE_BATCH_LIMIT
from atc_upstream import Read


class ActiveBackstopTest(unittest.TestCase):
    def setUp(self):
        self.model = EvidenceModel()
        self.backstop = ActiveBackstop()
        self.reads = ReadQueue(None, queue.Queue(), 2)
        self.addCleanup(self.reads.close)

    def accept(self, resource, scope, rows=(), at=100, complete=True):
        self.model.accept(resource, scope, Read(list(rows), complete, at, "read"))

    def test_active_and_terminal_scopes_follow_recorded_activity(self):
        now = 10_000_000
        self.accept("assignments", None, [
            {"id": "open", "state": "open", "holderKey": "holder"},
            {"id": "closed", "state": "closed", "holderKey": "idle"},
        ])
        scopes = [("attests", "open"), ("attests", "closed")]
        for session in ("holder", "running", "recent", "idle", "unknown"):
            for resource in ("turns", "wakes", "messages"):
                scopes.append((resource, session))
                self.accept(resource, session, complete=session != "unknown")
        for scope in scopes[:2]:
            self.accept(*scope)
        self.accept("turns", "running", [{"seq": 1, "startedAt": 1, "endedAt": None}])
        self.accept("messages", "recent", [{"id": "message", "at": now - 1000}])
        self.backstop.classify(self.model, scopes, now)
        self.assertIn(("attests", "open"), self.backstop.active)
        self.assertIn(("attests", "closed"), self.backstop.history)
        for session in ("holder", "running", "recent", "unknown"):
            self.assertTrue(all((resource, session) in self.backstop.active
                                for resource in ("turns", "wakes", "messages")))
        self.assertIn(("turns", "idle"), self.backstop.history)
        # Recent activity expires, but an old running turn and open holder do not.
        self.backstop.classify(self.model, scopes, now + ACTIVE_BUDGET_MS)
        self.assertIn(("messages", "recent"), self.backstop.history)
        self.assertIn(("turns", "running"), self.backstop.active)
        self.assertIn(("turns", "holder"), self.backstop.active)

    def test_only_complete_scope_reads_reset_confirmation_age(self):
        scope = ("attests", "open")
        self.accept("assignments", None, [{"id": "open", "state": "open"}])
        self.accept(*scope, [{"id": "first"}])
        inventory = self.model.inventory(*scope)
        inventory.payload({"id": "second"}, at=200, source="feed", policy="immutable")
        self.model.detail(*scope, Read([{"id": "third"}], True, 300, "detail"))
        self.accept(*scope, [{"id": "fourth"}], at=400, complete=False)
        self.assertEqual(100, inventory.confirmed_at)
        self.assertEqual(4, len(inventory.rows))
        self.backstop.classify(self.model, [scope], 100 + ACTIVE_BUDGET_MS)
        self.assertTrue(inventory.budget_stale)
        self.assertEqual("stale", inventory.evidence()["freshness"])
        self.assertFalse(inventory.complete)
        self.assertEqual(1, self.backstop.metadata(self.model, True)["active"]["overBudgetScopes"])
        self.accept(*scope, [], at=500 + ACTIVE_BUDGET_MS)
        self.backstop.classify(self.model, [scope], 500 + ACTIVE_BUDGET_MS)
        self.assertFalse(inventory.budget_stale)
        self.assertEqual("live", inventory.evidence()["freshness"])

    def test_active_refresh_is_bounded_and_does_not_wait_for_terminal_rotation(self):
        assignments = [{"id": "active" + str(i), "state": "open"} for i in range(100)]
        assignments += [{"id": "old", "state": "closed"}]
        self.accept("assignments", None, assignments)
        scopes = [("attests", a["id"]) for a in assignments]
        for scope in scopes:
            self.accept(*scope)
        self.reads.schedule("attests", "old")
        self.backstop.classify(self.model, scopes, 100 + ACTIVE_REFRESH_MS)
        self.backstop.schedule(self.model, self.reads, 100 + ACTIVE_REFRESH_MS)
        self.assertEqual(ACTIVE_BATCH_LIMIT, sum(priority == 1 for priority in self.reads.pending.values()))
        self.assertEqual(0, self.reads.pending["attests", "old", None])
        # No busy deadline while the bounded active queue is full; read results
        # will provide the next opportunity to schedule the remaining scopes.
        deadline = self.backstop.deadline(self.model, self.reads, 100 + ACTIVE_REFRESH_MS, True)
        self.assertEqual(100 + ACTIVE_BUDGET_MS, deadline)
        self.backstop.schedule(self.model, self.reads, 101 + ACTIVE_REFRESH_MS)
        self.assertEqual(ACTIVE_BATCH_LIMIT + 1, len(self.reads.pending))

    def test_partial_read_does_not_demote_a_last_known_open_scope(self):
        self.accept("assignments", None, [{"id": "open", "state": "open"}])
        self.accept("attests", "open")
        self.backstop.classify(self.model, [("attests", "open")], 100)
        self.accept("assignments", None, [{"id": "open", "state": "closed"}], complete=False)
        self.backstop.classify(self.model, [("attests", "open")], 200)
        self.assertIn(("attests", "open"), self.backstop.active)
        self.accept("assignments", None, [{"id": "open", "state": "closed"}])
        self.backstop.classify(self.model, [("attests", "open")], 300)
        self.assertIn(("attests", "open"), self.backstop.history)

    def test_terminal_ages_and_unknown_confirmation_remain_distinct(self):
        self.accept("assignments", None, [{"id": "old", "state": "closed"},
                                           {"id": "missing", "state": "closed"}])
        self.accept("attests", "old")
        self.accept("attests", "missing", complete=False)
        self.backstop.classify(self.model, [("attests", "old"), ("attests", "missing")], 10**9)
        metadata = self.backstop.metadata(self.model, False)
        self.assertEqual({"scopeCount": 2, "unconfirmedScopes": 1, "oldestConfirmedAt": 100},
                         metadata["history"])
        self.assertEqual(0, metadata["active"]["scopeCount"])
        self.assertEqual("stale", metadata["announcedChanges"])
        self.assertFalse(self.model.inventory("attests", "old").budget_stale)

    def test_active_confirmation_precedes_queued_terminal_history_and_yields_to_notice(self):
        class Gateway:
            def read(self, resource, scope):
                return Read([], True, 100, "read")

            def detail(self, resource, key, scope=None):
                return Read([], True, 100, "detail")

        self.reads.gateway = Gateway()
        self.reads.workers = 1
        self.reads.schedule("attests", "old")
        self.reads.schedule("attests", "open", active=True)
        self.reads.schedule("assignments", key="notice", priority=True)
        dispatched = []
        while self.reads.pending:
            self.reads.pump()
            _, (job, epoch, _) = self.reads.events.get(timeout=2)
            dispatched.append(job)
            self.reads.finish(job, epoch)
        self.assertEqual([("assignments", None, "notice"), ("attests", "open", None),
                          ("attests", "old", None)], dispatched)

    def test_epoch_retry_preserves_dispatch_priority(self):
        class Gateway:
            def read(self, resource, scope):
                return Read([], True, 100, "read")

            def detail(self, resource, key, scope=None):
                return Read([], True, 100, "detail")

        self.reads.gateway = Gateway()
        self.reads.workers = 1
        for expected, options in ((2, {"priority": True}), (1, {"active": True})):
            scope = f"retry-{expected}"
            self.reads.schedule("attests", scope, **options)
            self.reads.pump()
            kind, (job, epoch, _) = self.reads.events.get(timeout=2)
            self.assertEqual("read", kind)
            self.reads.touch(*job[:2])
            self.assertFalse(self.reads.finish(job, epoch))
            self.assertEqual(expected, self.reads.pending[job])
            self.reads.pending.clear()
