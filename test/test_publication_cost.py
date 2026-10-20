"""Burst publication and history reuse with synthetic, credential-free data."""

from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_model import EvidenceModel
from atc_reconciliation import ActiveBackstop
from atc_service import LiveService, PUBLICATION_INTERVAL
from atc_upstream import Read
from test_service import PublishedReads, work


class NoHistoryWalk(dict):
    def values(self):
        raise AssertionError("unchanged history was walked again")


class PublicationCostTest(unittest.TestCase):
    def test_unchanged_histories_are_not_walked_by_classification_or_projection(self):
        model = EvidenceModel()
        model.accept("work-items", None, Read([work()], True, 1, "fixture"))
        assignments = [{"id": str(n), "workItemId": "work", "state": "open",
                        "holderKey": "agent s_author", "effectKind": "code"} for n in range(200)]
        model.accept("assignments", None, Read(assignments, True, 1, "fixture"))
        model.accept("sessions", None, Read([], True, 1, "fixture"))
        scopes = [("attests", row["id"]) for row in assignments]
        for resource, scope in scopes:
            rows = [{"id": str(n), "kind": "progress", "ts": n,
                     "bySession": "agent s_author", "commitRefs": []} for n in range(50)]
            model.accept(resource, scope, Read(rows, True, 1, "fixture"))
        backstop = ActiveBackstop()
        backstop.classify(model, scopes, 100)
        before = model.snapshot(100)
        for scope in scopes:
            inventory = model.inventory(*scope)
            inventory.rows = NoHistoryWalk(inventory.rows)
        # A changed item does not require any history walk.
        model.accept("work-items", None, Read([work("Changed", 2)], True, 2, "fixture"))
        backstop.classify(model, scopes, 101)
        after = model.snapshot(101)
        self.assertEqual(10000, after["items"][0]["evidenceCounts"]["total"])
        self.assertEqual(before["items"][0]["stage"], after["items"][0]["stage"])
        # One authoritative history replacement rebuilds only that scope.
        model.accept("attests", "0", Read([], True, 3, "fixture"))
        backstop.classify(model, scopes, 102)
        self.assertEqual(9950, model.snapshot(102)["items"][0]["evidenceCounts"]["total"])
        # Partial reads retain evidence, but cannot certify the aggregate.
        model.accept("attests", "0", Read([], False, 4, "fixture"))
        self.assertIsNone(model.snapshot(103)["items"][0]["stage"])

    def test_burst_is_coalesced_and_final_payload_is_published(self):
        service = LiveService(PublishedReads())
        self.addCleanup(service.stop)
        service.policies = {"work-items": "strict-version"}
        service._next_maintenance = time.monotonic() + 60
        for version in range(1, 513):
            service.events.put(("change", {
                "resource": "work-items", "class": "work_item.updated", "op": "upsert",
                "refs": {"workItemId": "work"}, "payload": work(str(version), version)}))
        published = threading.Event()
        calls = []
        original = service._publish

        def publish(**kwargs):
            result = original(**kwargs)
            calls.append(time.monotonic())
            if service.current["items"][0]["title"] == "512":
                published.set()
            return result

        with patch.object(service, "_publish", side_effect=publish):
            worker = threading.Thread(target=service._worker)
            worker.start()
            try:
                self.assertTrue(published.wait(5), "final burst state was not published")
                self.assertEqual("512", service.snapshot()["items"][0]["title"])
                self.assertLess(len(calls), 512)
                for earlier, later in zip(calls, calls[1:]):
                    self.assertGreaterEqual(later - earlier, PUBLICATION_INTERVAL * .9)
            finally:
                service.events.put(("stop", None))
                worker.join(5)
                self.assertFalse(worker.is_alive())

    def test_committed_snapshot_remains_available_during_projection(self):
        service = LiveService(PublishedReads())
        self.addCleanup(service.stop)
        old = service.snapshot()
        entered, release = threading.Event(), threading.Event()
        original = service.model.snapshot

        def slow_snapshot(at):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(at)

        def publish():
            with service.model_lock:
                service._publish()

        with patch.object(service.model, "snapshot", side_effect=slow_snapshot):
            writer = threading.Thread(target=publish)
            writer.start()
            try:
                self.assertTrue(entered.wait(5))
                observed = []
                reader = threading.Thread(target=lambda: observed.append(service.snapshot()))
                reader.start()
                reader.join(1)
                self.assertFalse(reader.is_alive(), "committed readers waited for model construction")
                self.assertEqual([old], observed)
            finally:
                release.set()
                writer.join(5)

    def test_unpublished_evidence_is_not_labelled_with_committed_revision(self):
        service = LiveService(PublishedReads())
        self.addCleanup(service.stop)
        service.model.accept("work-items", None, Read([work()], True, 1, "fixture"))
        service._dirty = True
        self.assertEqual(409, service.evidence_page("work")[0])
        service._publish()
        status, page = service.evidence_page("work")
        self.assertEqual(200, status)
        self.assertEqual(service.revision, page["revision"])


if __name__ == "__main__":
    unittest.main()
