import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import LiveService


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


if __name__ == "__main__":
    unittest.main()
