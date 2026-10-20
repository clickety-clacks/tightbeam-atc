import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from atc_service import LiveService
from atc_upstream import Read


class HttpTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("atc_api_test", ROOT / "server" / "tb-atc-api.py")
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.temporary = tempfile.TemporaryDirectory()
        self.api.STATE_FILE = Path(self.temporary.name) / "annotations.json"
        self.live = LiveService(None)
        self.api.live_service = self.live
        self.server = self.api.ThreadingHTTPServer(("127.0.0.1", 0), self.api.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.live.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.url + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_stream_carries_full_recovery_models_and_retains_partial_rows(self):
        self.live.model.accept("work-items", None, Read(
            [{"id": "item", "title": "Visible", "state": "open"}], True, 1, "test-read"))
        self.live._publish()
        with urlopen(self.url + "/api/events", timeout=3) as response:
            self.assertEqual(b"event: model\n", response.readline())
            first = json.loads(response.readline()[6:])
            self.assertEqual(b"\n", response.readline())
            self.assertEqual("item", first["items"][0]["id"])
            self.live.model.accept("work-items", None, Read([], False, 2, "test-read"))
            self.live._state("stale", "partial read")
            self.live._publish()
            self.assertEqual(b"event: model\n", response.readline())
            second = json.loads(response.readline()[6:])
            self.assertGreater(second["revision"], first["revision"])
            self.assertEqual(first["generation"], second["generation"])
            self.assertEqual("item", second["items"][0]["id"])
            self.assertEqual("stale", second["freshness"]["state"])
            self.assertEqual("stale", second["items"][0]["evidence"]["identity"]["freshness"])
        self.assertEqual(second["generation"], self.request("/data.json")[1]["generation"])

    def test_an_agent_cannot_remove_another_authors_annotation(self):
        status, result = self.request("/api/tags", "POST", {
            "tags": [{"target": "item", "text": "keep", "author": "other"}]})
        self.assertEqual(200, status)
        tag = result["created"][0]["tagId"]
        self.assertEqual(403, self.request("/api/tags/" + tag + "?author=caller", "DELETE")[0])
        self.assertEqual(403, self.request("/api/tags?all=true", "DELETE")[0])
        self.assertEqual(400, self.request("/api/tags/clear", "POST", {})[0])
        self.assertEqual(1, len(self.request("/api/tags")[1]))
        self.assertEqual(200, self.request("/api/tags/" + tag + "?author=other", "DELETE")[0])

    def test_saved_card_edits_preserve_the_original_author(self):
        _, result = self.request("/api/searches", "POST", {
            "searches": [{"query": "work", "author": "original"}]})
        card = result["searches"][0]["searchId"]
        body = {"searches": [{"searchId": card, "label": "changed", "author": "different"}]}
        self.assertEqual(403, self.request("/api/searches", "POST", body)[0])
        body["searches"][0]["source"] = "user"
        status, result = self.request("/api/searches", "POST", body)
        self.assertEqual(200, status)
        self.assertEqual("original", result["searches"][0]["author"])
        persisted = json.loads(self.api.STATE_FILE.read_text())
        self.assertEqual("original", persisted["searches"][0]["author"])

    def test_unknown_author_survives_persistence(self):
        self.request("/api/arrows", "POST", {"arrows": [{"from": "one", "to": "two"}]})
        self.assertIsNone(json.loads(self.api.STATE_FILE.read_text())["arrows"][0]["author"])
        self.api.state["arrows"].clear()
        self.api.load_tags()
        self.assertIsNone(self.request("/api/arrows")[1][0]["author"])
