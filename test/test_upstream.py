import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_upstream import Gateway


def page(rows, more=False, cursor=None):
    return {"schemaVersion": 1, "items": rows, "page": {
        "hasMoreBefore": False, "hasMoreAfter": more, "newestCursor": cursor}}


class ReadCompletionTest(unittest.TestCase):
    def setUp(self):
        self.gateway = Gateway("http://example.invalid", "test-device", "test-org", "test")

    def test_walks_every_page_before_claiming_complete(self):
        self.gateway.request = Mock(side_effect=[
            page([{"id": "a"}], True, "cursor"), page([{"id": "b"}])])
        result = self.gateway.collection("/api/work-items")
        self.assertTrue(result.complete)
        self.assertEqual(["a", "b"], [r["id"] for r in result.rows])
        self.assertIn("after=cursor", self.gateway.request.call_args.args[0])

    def test_failed_second_page_keeps_first_page_partial(self):
        self.gateway.request = Mock(side_effect=[
            page([{"id": "a"}], True, "cursor"), OSError("connection lost")])
        result = self.gateway.collection("/api/work-items")
        self.assertFalse(result.complete)
        self.assertEqual([{"id": "a"}], result.rows)

    def test_missing_or_repeated_cursor_cannot_certify_absence(self):
        self.gateway.request = Mock(side_effect=[
            page([{"id": "a"}], True, "cursor"),
            page([{"id": "b"}], True, "cursor")])
        self.assertFalse(self.gateway.collection("/api/work-items").complete)

    def test_read_adapter_cannot_dispatch_mutation_commands(self):
        self.gateway.request = Mock()
        with self.assertRaises(ValueError):
            self.gateway.command("work-item-close", work_item_id="a")
        self.gateway.request.assert_not_called()

    def test_first_page_with_older_rows_missing_is_partial_even_with_next_page(self):
        first = page([{"id": "b"}], True, "cursor")
        first["page"]["hasMoreBefore"] = True
        self.gateway.request = Mock(return_value=first)
        result = self.gateway.collection("/api/work-items")
        self.assertFalse(result.complete)
        self.assertEqual([{"id": "b"}], result.rows)
        self.gateway.request.assert_called_once()

    def test_unknown_schema_cannot_establish_complete_or_empty_state(self):
        unknown = dict(page([]), schemaVersion=2)
        self.gateway.request = Mock(return_value=unknown)
        self.assertFalse(self.gateway.collection("/api/work-items").complete)
        self.assertFalse(self.gateway.detail("work-items", "work").complete)
