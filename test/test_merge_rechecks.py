import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import LiveService
from atc_git import GitResolver


class MergeRecheckTest(unittest.TestCase):
    def setUp(self):
        self.service = LiveService(object(), resolver=object())
        self.addCleanup(self.service.stop)
        self.refs = [{"repo": "example", "commit": "0123456789abcdef"}]
        self.service.current = {"items": [{
            "id": "work", "code": True, "commitRefs": self.refs,
        }]}
        self.service.model.merges["work"] = {
            "value": None, "observedAt": 100_000, "refs": self.refs,
        }
        self.service.cycle = {"mode": "recovery", "pending": {("messages", "session")}}
        self.service._dirty = False

    def run_maintenance(self, at):
        self.service._next_maintenance = 0
        with (patch("atc_service.now_ms", return_value=at),
              patch("atc_service.time.monotonic", return_value=1_000),
              patch.object(self.service, "_update_backstop", return_value=False),
              patch.object(self.service, "_publish") as publish):
            self.service._maintenance()
        return publish

    def test_unknown_merge_check_retries_at_expiry_without_another_model_change(self):
        before_expiry = self.run_maintenance(399_999)
        self.assertFalse(before_expiry.called)
        self.assertFalse(self.service._dirty)
        self.assertEqual(0, self.service.merge_jobs.qsize())

        due = self.run_maintenance(400_000)
        self.assertTrue(due.called)
        self.assertEqual({"work": self.refs}, self.service.merge_pending)
        self.assertEqual(("work", self.refs), self.service.merge_jobs.get_nowait())

    def test_known_merge_result_does_not_enter_unknown_retry_schedule(self):
        self.service.model.merges["work"]["value"] = False
        self.assertIsNone(self.service._next_merge_retry_at())

    def test_failed_worker_recheck_retains_known_merge(self):
        class FailingResolver:
            def resolve(self, refs):
                raise TimeoutError("Git unavailable")

        self.service.model.merges["work"].update(value=True, freshness="live")
        self.service.merge_pending["work"] = self.refs
        self.service.merge_jobs.put(("work", self.refs))
        self.service.merge_jobs.put(None)
        with patch("atc_service.now_ms", return_value=400_000):
            self.service._git_worker(FailingResolver())
        kind, event = self.service.events.get_nowait()
        self.assertTrue(self.service._handle(kind, event))
        retained = self.service.model.merges["work"]
        self.assertIs(True, retained["value"])
        self.assertEqual("stale", retained["freshness"])
        self.assertEqual(100_000, retained["observedAt"])
        self.assertEqual(400_000, retained["lastAttemptAt"])

    def test_definite_answer_replaces_retained_evidence(self):
        self.service.model.merges["work"].update(value=True, freshness="stale")
        for answer in (False, True):
            with self.subTest(answer=answer):
                self.service.merge_pending["work"] = self.refs
                self.assertTrue(self.service._handle(
                    "merged", ("work", self.refs, answer, 400_000)))
                result = self.service.model.merges["work"]
                self.assertIs(answer, result["value"])
                self.assertEqual("live", result["freshness"])
                self.assertEqual(400_000, result["observedAt"])

    def test_changed_refs_do_not_inherit_previous_merge(self):
        self.service.model.merges["work"].update(value=True, freshness="live")
        changed = [{"repo": "example", "commit": "abcdef0123456789"}]
        self.service.merge_pending["work"] = changed
        self.assertFalse(self.service._handle(
            "merged", ("work", self.refs, None, 400_000)))
        self.assertTrue(self.service._handle(
            "merged", ("work", changed, None, 400_001)))
        self.assertIsNone(self.service.model.merges["work"]["value"])
        self.assertEqual(changed, self.service.model.merges["work"]["refs"])

    def test_restart_partial_ref_attempt_cannot_erase_full_proof(self):
        proof = [{"repo": "example", "commit": f"{n:040x}"} for n in range(15)]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            seed = LiveService(object(), resolver=GitResolver(cache))
            self.addCleanup(seed.stop)
            seed._persist_merge_result("work", {
                "value": True, "observedAt": 100_000, "refs": proof,
            })
            service = LiveService(object(), resolver=GitResolver(cache))
            self.addCleanup(service.stop)
            for partial_answer in (None, False, True):
                service.merge_pending["work"] = proof[:1]
                service.merge_pending_complete["work"] = False
                service._handle("merged", ("work", proof[:1], partial_answer, 400_000))
                retained = service.model.merges["work"]
                self.assertIs(True, retained["value"])
                self.assertEqual(proof, retained["refs"])
                self.assertEqual(100_000, retained["observedAt"])
                self.assertEqual("stale", retained["freshness"])
            service._compact_merge_cache(service.merge_cache_path)
            restarted = LiveService(object(), resolver=GitResolver(cache))
            self.addCleanup(restarted.stop)
            restarted.current = {"items": [{"id": "work", "code": True,
                "commitRefs": proof, "commitRefsComplete": True}]}
            with patch("atc_service.now_ms", return_value=400_001):
                restarted._resolve_merges()
            self.assertEqual(("work", proof), restarted.merge_jobs.get_nowait())
            restarted._handle("merged", ("work", proof, None, 400_001))
            self.assertIs(True, restarted.model.merges["work"]["value"])
            # A later successful full-list check is fresh proof, not merely
            # the retained True from before the partial startup load.
            for answer, at in ((True, 700_000), (False, 1_000_000)):
                restarted.merge_pending["work"] = proof
                restarted.merge_pending_complete["work"] = True
                restarted._handle("merged", ("work", proof, answer, at))
                refreshed = restarted.model.merges["work"]
                self.assertIs(answer, refreshed["value"])
                self.assertEqual(at, refreshed["observedAt"])
                self.assertEqual(at, refreshed["lastAttemptAt"])
                self.assertEqual("live", refreshed["freshness"])
                saved = json.loads((cache / "merge-results.jsonl").read_text().splitlines()[-1])
                self.assertIs(answer, saved["value"])
                self.assertEqual(at, saved["observedAt"])
                self.assertEqual(proof, saved["refs"])

    def test_grown_refs_retain_proof_through_failure_compaction_and_restart(self):
        class FailingResolver:
            def resolve(self, refs):
                raise TimeoutError("Git unavailable")

        added = {"repo": "example", "commit": "b" * 40}
        grown = [added, *self.refs]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            service = LiveService(object(), resolver=GitResolver(cache))
            self.addCleanup(service.stop)
            service.current = {"items": [{"id": "work", "code": True, "commitRefs": grown}]}
            service.model.merges["work"] = {
                "value": True, "observedAt": 100_000, "refs": self.refs,
                "freshness": "live",
            }
            with patch("atc_service.now_ms", return_value=400_000):
                service._resolve_merges()
                service.merge_jobs.put(None)
                service._git_worker(FailingResolver())
            kind, event = service.events.get_nowait()
            self.assertTrue(service._handle(kind, event))
            retained = service.model.merges["work"]
            self.assertIs(True, retained["value"])
            self.assertEqual(self.refs, retained["refs"], "keep the original proof")
            self.assertEqual(grown, retained["lastAttemptRefs"])
            self.assertEqual(100_000, retained["observedAt"])
            self.assertEqual("stale", retained["freshness"])
            service._compact_merge_cache(service.merge_cache_path)
            restarted = LiveService(object(), resolver=GitResolver(cache))
            self.addCleanup(restarted.stop)
            restarted.current = service.current
            self.assertEqual(retained, restarted.model.merges["work"])
            with patch("atc_service.now_ms", return_value=699_999):
                restarted._resolve_merges()
            self.assertTrue(restarted.merge_jobs.empty())
            with patch("atc_service.now_ms", return_value=700_000):
                restarted._resolve_merges()
            self.assertEqual(("work", grown), restarted.merge_jobs.get_nowait())
            # Replacing any original proof ref invalidates it, even after a
            # retained result has been persisted for a larger attempted set.
            removed = [added]
            restarted.merge_pending["work"] = removed
            restarted._handle("merged", ("work", removed, None, 700_001))
            self.assertIsNone(restarted.model.merges["work"]["value"])

    def test_grown_refs_definite_answer_replaces_proof_and_negative_does_not_carry(self):
        grown = [*self.refs, {"repo": "example", "commit": "b" * 40}]
        for old, answer in ((True, False), (True, True), (False, None)):
            with self.subTest(old=old, answer=answer):
                self.service.model.merges["work"] = {
                    "value": old, "observedAt": 100_000, "refs": self.refs,
                    "freshness": "stale",
                }
                self.service.merge_pending["work"] = grown
                self.service._handle("merged", ("work", grown, answer, 400_000))
                result = self.service.model.merges["work"]
                self.assertIs(answer, result["value"])
                self.assertEqual(grown, result["refs"])
                self.assertEqual(400_000, result["observedAt"])

    def test_inconclusive_recheck_preserves_definite_result_and_attempt_time(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed), tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp) / "git-cache"
                service = LiveService(object(), resolver=GitResolver(cache))
                try:
                    service.current = {"items": [{
                        "id": "work", "code": True, "commitRefs": self.refs,
                    }]}
                    service.merge_pending["work"] = self.refs
                    service.model.merges["work"] = {
                        "value": confirmed, "observedAt": 100_000,
                        "lastAttemptAt": 100_000, "refs": self.refs,
                        "freshness": "live",
                    }

                    self.assertTrue(service._handle(
                        "merged", ("work", self.refs, None, 400_000)))
                    retained = service.model.merges["work"]
                    self.assertIs(confirmed, retained["value"])
                    self.assertEqual(100_000, retained["observedAt"])
                    self.assertEqual(400_000, retained["lastAttemptAt"])
                    self.assertEqual("stale", retained["freshness"])

                    record = json.loads((cache / "merge-results.jsonl").read_text().splitlines()[-1])
                    self.assertIs(confirmed, record["value"])
                    self.assertEqual(100_000, record["observedAt"])
                    self.assertEqual(400_000, record["lastAttemptAt"])

                    restarted = LiveService(object(), resolver=GitResolver(cache))
                    try:
                        restored = restarted.model.merges["work"]
                        self.assertIs(confirmed, restored["value"])
                        self.assertEqual(100_000, restored["observedAt"])
                        self.assertEqual(400_000, restored["lastAttemptAt"])
                        self.assertEqual("stale", restored["freshness"])
                        restarted.current = service.current
                        with patch("atc_service.now_ms", return_value=699_999):
                            restarted._resolve_merges()
                        self.assertEqual(0, restarted.merge_jobs.qsize())
                        with patch("atc_service.now_ms", return_value=700_000):
                            restarted._resolve_merges()
                        self.assertEqual(("work", self.refs),
                                         restarted.merge_jobs.get_nowait())
                    finally:
                        restarted.stop()
                finally:
                    service.stop()


if __name__ == "__main__":
    unittest.main()
