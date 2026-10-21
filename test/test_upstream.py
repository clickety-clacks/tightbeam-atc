import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_upstream import Gateway, PackagedCLI


def page(rows, more=False, cursor=None):
    return {"schemaVersion": 1, "items": rows, "page": {
        "hasMoreBefore": False, "hasMoreAfter": more, "newestCursor": cursor}}


class ReadCompletionTest(unittest.TestCase):
    def setUp(self):
        self.gateway = Gateway("http://example.invalid", Mock())

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


class PackagedCLITest(unittest.TestCase):
    def setUp(self):
        self.cli = PackagedCLI("/package/bin/tightbeam", "/scratch/base", "reader")

    @patch("atc_upstream.subprocess.run")
    def test_exact_read_arguments_and_base_without_ambient_endpoint(self, run):
        run.return_value = Mock(returncode=0, stdout='{"roles": []}')
        cases = [
            ("assignments", {"state": "all"}, ["assignments", "--state", "all"]),
            ("attests", {"assignment_id": "assignment"}, ["attests", "assignment"]),
            ("assignment-get", {"assignment_id": "assignment"}, ["assignment-get", "assignment"]),
            ("role-list", {}, ["list"]),
            ("artifacts", {}, ["artifacts"]),
        ]
        with patch.dict("os.environ", {"TIGHTBEAM_URL": "wrong-endpoint",
                                      "TIGHTBEAM_TOKEN": "ambient-test-token",
                                      "TIGHTBEAM_BASE_DIR": "/wrong/base"}):
            for verb, params, expected in cases:
                self.cli.read(verb, **params)
                self.assertEqual(["/package/bin/tightbeam", *expected, "--as-user", "reader"],
                                 run.call_args.args[0])
                options = run.call_args.kwargs
                self.assertEqual("/", options["cwd"])
                self.assertEqual("/scratch/base", options["env"]["TIGHTBEAM_BASE_DIR"])
                self.assertNotIn("TIGHTBEAM_TOKEN", options["env"])
                self.assertNotIn("TIGHTBEAM_URL", options["env"])
                self.assertNotIn("shell", options)

    @patch("atc_upstream.subprocess.run")
    def test_mutations_and_non_inventory_filters_cannot_reach_cli(self, run):
        for verb, params in [("work-item-close", {}), ("assignments", {"state": "open"}),
                             ("artifacts", {"session": "someone"})]:
            with self.assertRaises(ValueError):
                self.cli.read(verb, **params)
        run.assert_not_called()

    @patch("atc_upstream.subprocess.run")
    def test_cli_failure_is_partial_and_does_not_expose_diagnostics(self, run):
        run.return_value = Mock(returncode=1, stdout="not a result", stderr="private diagnostic")
        result = Gateway("http://example.invalid", self.cli).read("assignments")
        self.assertFalse(result.complete)
        self.assertEqual([], result.rows)
        self.assertEqual("ValueError", result.error)
        self.assertNotIn("private", str(result))

    @patch("atc_upstream.subprocess.run")
    def test_assignment_corrections_survive_cli_projection(self, run):
        run.return_value = Mock(returncode=0, stdout=(
            '{"id":"assignment","commitRefCorrections":'
            '[{"actorRef":"author","commitRefs":[{"commit":"abc"}]}]}'))
        result = Gateway("http://example.invalid", self.cli).detail(
            "assignments", "assignment")
        self.assertTrue(result.complete)
        self.assertEqual("author", result.rows[0]["commitRefCorrections"][0]["actorRef"])

    def test_missing_explicit_target_is_not_an_ambient_base_fallback(self):
        with self.assertRaises(ValueError):
            PackagedCLI("tightbeam", "/scratch/base", "reader")
        with self.assertRaises(ValueError):
            PackagedCLI("/package/bin/tightbeam", "relative-base", "reader")
