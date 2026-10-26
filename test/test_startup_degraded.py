import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from atc_service import LiveService
from atc_upstream import Read


class EmptyGateway:
    read_count = 0

    def read(self, resource, scope=None):
        self.read_count += 1
        return Read([], True, 1, "mock-read")

    def detail(self, resource, key, scope=None):
        self.read_count += 1
        return Read([], True, 1, "mock-detail")


class StartupDegradedTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "atc_startup_test", ROOT / "server" / "tb-atc-api.py")
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.temporary = tempfile.TemporaryDirectory()
        self.api.STATE_FILE = Path(self.temporary.name) / "annotations.json"
        self.server = None
        self.thread = None
        self.live = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
        if self.live is not None:
            self.live.stop()
        self.temporary.cleanup()

    def serve(self, live, warning):
        self.live = live
        self.api.live_service = live
        self.api.live_warning = warning
        self.server = self.api.ThreadingHTTPServer(("127.0.0.1", 0), self.api.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def request(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.url + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=3) as response:
                payload = response.read()
                status = response.status
        except HTTPError as error:
            payload = error.read()
            status = error.code
        content = payload.decode()
        if path == "/" or path.endswith(".html"):
            return status, content
        return status, json.loads(content)

    def assert_viewer_and_controls(self, live_available, warning_part):
        status, page = self.request("/")
        self.assertEqual(200, status)
        self.assertIn('id="mode"', page)

        status, startup = self.request("/api/status")
        self.assertEqual(200, status)
        self.assertEqual(live_available, startup["liveAvailable"])
        self.assertIn(warning_part, startup["warning"])

        self.assertEqual(200, self.request("/api/state")[0])
        self.assertEqual(200, self.request("/api/tags", "POST", {
            "tags": [{"target": "item", "text": "available"}]} )[0])
        self.assertEqual(200, self.request("/api/arrows", "POST", {
            "arrows": [{"from": "item", "to": "agent", "text": "related"}]} )[0])
        self.assertEqual(200, self.request("/api/filter", "POST", {
            "query": "status", "author": "mock"})[0])
        self.assertEqual(200, self.request("/api/select", "POST", {
            "add": ["item"]})[0])
        self.assertEqual(200, self.request("/api/focus", "POST", {
            "id": "item"})[0])
        self.assertEqual(200, self.request("/api/fit", "POST", {"on": True})[0])
        self.assertEqual(1, len(self.request("/api/searches")[1]))
        self.assertEqual(1, len(self.request("/api/tags")[1]))
        self.assertEqual(1, len(self.request("/api/arrows")[1]))

    def live_settings(self):
        return {
            "TB_ATC_GATEWAY": "http://example.invalid",
            "TB_ATC_CLI": "/package/bin/tightbeam",
            "TB_ATC_CLI_BASE_DIR": "/tmp/mock-gateway",
            "TB_ATC_USER": "mock-viewer",
            "TB_ATC_GIT_CACHE": str(Path(self.temporary.name) / "git-cache"),
        }

    def test_missing_gateway_disables_only_live_model(self):
        settings = self.live_settings()
        settings.pop("TB_ATC_GATEWAY")
        live, warning = self.api.build_live_service(settings)
        self.assertIsNone(live)
        self.serve(live, warning)
        self.assert_viewer_and_controls(False, "TB_ATC_GATEWAY")
        self.assertEqual(503, self.request("/api/model")[0])

    def test_missing_service_environment_file_does_not_block_unit(self):
        unit = (ROOT / "systemd" / "tb-atc.service").read_text()
        self.assertIn("EnvironmentFile=-%h/.config/tb-atc/service.env", unit)
        settings = {"TB_ATC_HOST": "127.0.0.1", "TB_ATC_PORT": "8787"}
        live, warning = self.api.build_live_service(settings)
        self.assertIsNone(live)
        self.serve(live, warning)
        self.assert_viewer_and_controls(False, "TB_ATC_GATEWAY")

    def test_missing_websockets_keeps_rest_model_and_controls_available(self):
        missing_modules = {name: None for name in (
            "websockets", "websockets.sync", "websockets.sync.client")}
        with patch.object(self.api, "Gateway", return_value=EmptyGateway()):
            with patch.dict(sys.modules, missing_modules):
                live, warning = self.api.build_live_service(self.live_settings())
        self.assertIsNotNone(live)
        self.serve(live, warning)
        self.assert_viewer_and_controls(True, "optional websockets library")
        status, model = self.request("/api/model")
        self.assertEqual(200, status)
        self.assertIn("items", model)

    def test_malformed_live_settings_warn_and_keep_model_available(self):
        settings = self.live_settings() | {
            "TB_ATC_HOST_ROUTES": "[]",
            "TB_ATC_REBUILD_SECONDS": "NaN",
        }
        with patch.object(self.api, "Gateway", return_value=EmptyGateway()):
            with patch.object(LiveService, "start"):
                live, warning = self.api.build_live_service(settings)
        self.assertIsNotNone(live)
        self.assertEqual({}, live.resolver.host_routes)
        self.assertEqual(300.0, live.rebuild_interval)
        self.serve(live, warning)
        self.assert_viewer_and_controls(True, "TB_ATC_HOST_ROUTES")
        self.assertIn("using the default 300-second interval", warning)
        self.assertEqual(200, self.request("/api/model")[0])


if __name__ == "__main__":
    unittest.main()
