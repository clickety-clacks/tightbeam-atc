"""Operator-login transport with synthetic credentials and mocked responses."""

import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import LiveService
from atc_upstream import Gateway, PackagedCLI


def page(rows, more=False):
    return {"schemaVersion": 1, "items": rows, "page": {
        "hasMoreBefore": False, "hasMoreAfter": more, "newestCursor": "next cursor"}}


class OperatorLoginTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.login = Path(self.scratch.name) / "gateway.json"
        self.login.write_text(json.dumps({"cliToken": "synthetic-operator-login"}))
        self.cli = PackagedCLI("/package/bin/tightbeam", self.scratch.name, "test reader+one")
        self.gateway = Gateway("http://example.invalid", self.cli)

    @patch("atc_upstream.urlopen")
    def test_every_http_read_uses_operator_login_and_user(self, urlopen):
        def response(request, **kwargs):
            parts = urlsplit(request.full_url)
            self.assertEqual("Bearer synthetic-operator-login", request.get_header("Authorization"))
            self.assertEqual([self.cli.user], parse_qs(parts.query)["asUser"])
            if parts.path == "/version":
                value = {"version": "test", "build": 1, "sha": "synthetic"}
            elif parts.path.endswith("/entry"):
                value = {"schemaVersion": 1, "item": {"id": "entry"}}
            else:
                value = page([{"id": "entry"}])
            return io.StringIO(json.dumps(value))
        urlopen.side_effect = response
        with patch.dict("os.environ", {"TIGHTBEAM_TOKEN": "ignored-ambient-login"}):
            for resource in ("work-items", "sessions", "decision-requests"):
                self.assertTrue(self.gateway.read(resource).complete)
            for resource in ("turns", "wakes", "messages"):
                self.assertTrue(self.gateway.read(resource, "session / one").complete)
            for resource in ("work-items", "sessions", "decision-requests", "artifacts", "turns", "wakes", "messages"):
                self.assertTrue(self.gateway.detail(resource, "entry", "session / one").complete)
            self.assertEqual("test", self.gateway.version()["version"])
        self.assertEqual(14, urlopen.call_count)

    @patch("atc_upstream.urlopen")
    def test_pagination_preserves_cursor_and_user_on_every_page(self, urlopen):
        urlopen.side_effect = [io.StringIO(json.dumps(page([{"id": "a"}], True))),
                               io.StringIO(json.dumps(page([{"id": "b"}])))]
        result = self.gateway.read("turns", "session / one")
        self.assertTrue(result.complete)
        self.assertEqual(["a", "b"], [r["id"] for r in result.rows])
        first, second = [urlsplit(c.args[0].full_url) for c in urlopen.call_args_list]
        self.assertEqual("/api/sessions/session%20%2F%20one/turns", first.path)
        self.assertEqual({"limit": ["500"], "tail": ["false"], "asUser": [self.cli.user]},
                         parse_qs(first.query))
        self.assertEqual(["next cursor"], parse_qs(second.query)["after"])
        self.assertNotIn("tail", parse_qs(second.query))
        self.assertEqual([self.cli.user], parse_qs(second.query)["asUser"])

    @patch("atc_upstream.urlopen")
    def test_refused_page_remains_partial_without_exposing_response(self, urlopen):
        urlopen.side_effect = [io.StringIO(json.dumps(page([{"id": "retained"}], True))),
                               OSError("private response synthetic-operator-login")]
        result = self.gateway.read("wakes", "session")
        self.assertFalse(result.complete)
        self.assertEqual([{"id": "retained"}], result.rows)
        self.assertEqual("OSError", result.error)
        self.assertNotIn("private response", str(result))
        self.assertNotIn("synthetic-operator-login", str(result))

    @patch("atc_upstream.urlopen")
    def test_missing_login_is_incomplete_without_another_auth_route(self, urlopen):
        self.login.unlink()
        with patch.dict("os.environ", {"TIGHTBEAM_TOKEN": "ignored-ambient-login"}):
            result = self.gateway.read("work-items")
        self.assertFalse(result.complete)
        self.assertEqual([], result.rows)
        self.assertEqual("FileNotFoundError", result.error)
        urlopen.assert_not_called()

    @patch("atc_upstream.urlopen")
    def test_login_is_consumed_in_place_again_after_rotation(self, urlopen):
        urlopen.side_effect = lambda *args, **kwargs: io.StringIO(json.dumps(page([])))
        self.gateway.read("sessions")
        self.login.write_text(json.dumps({"cliToken": "synthetic-rotated-login"}))
        self.gateway.read("sessions")
        connection = Mock()
        self.gateway.authenticate_feed(connection)
        self.assertEqual("Bearer synthetic-rotated-login", urlopen.call_args.args[0].get_header("Authorization"))
        self.assertEqual("synthetic-rotated-login", json.loads(connection.send.call_args.args[0])["token"])
        self.assertNotIn("synthetic-rotated-login", repr(self.gateway))
        self.assertNotIn("synthetic-rotated-login", repr(self.cli))

    def run_feed(self, accepted):
        service = LiveService(self.gateway)
        self.gateway.version = Mock(return_value={"version": "test", "build": 1, "sha": "synthetic"})
        connection = Mock()
        replies = iter([{"type": "auth_result", "success": accepted}, {"type": "subscription_ready"}])
        def receive(**kwargs):
            value = next(replies)
            if not accepted or value["type"] == "subscription_ready":
                service.stop_event.set()
            return json.dumps(value)
        connection.recv.side_effect = receive
        connect = Mock()
        connect.return_value.__enter__ = Mock(return_value=connection)
        connect.return_value.__exit__ = Mock(return_value=False)
        modules = {name: ModuleType(name) for name in ("websockets", "websockets.sync", "websockets.sync.client")}
        modules["websockets.sync.client"].connect = connect
        with patch.dict(sys.modules, modules):
            service._feed()
        self.assertEqual("ws://example.invalid/ws/changes?protocolVersion=2", connect.call_args.args[0])
        frames = [json.loads(c.args[0]) for c in connection.send.call_args_list]
        self.assertEqual({"type": "auth", "token": "synthetic-operator-login", "asUser": self.cli.user}, frames[0])
        self.assertNotIn("synthetic-operator-login", json.dumps(service.snapshot()))
        return service, frames

    def test_feed_authenticates_as_user_then_subscribes_org_wide(self):
        service, frames = self.run_feed(True)
        self.assertEqual({"type": "subscribe", "subscriptionId": "atc", "protocolVersion": 2, "filters": {}}, frames[1])
        self.assertEqual("ready", service.events.get_nowait()[0])

    def test_gateway_refusal_enters_existing_feed_lost_path(self):
        service, frames = self.run_feed(False)
        self.assertEqual(1, len(frames))
        self.assertEqual(("lost", "feed disconnected: ValueError"), service.events.get_nowait())
