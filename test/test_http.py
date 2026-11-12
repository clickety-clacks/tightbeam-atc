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

    def test_javascript_module_has_browser_module_mime_type(self):
        with urlopen(self.url + "/viewer-semantics.mjs", timeout=3) as response:
            self.assertEqual(200, response.status)
            self.assertEqual("text/javascript", response.headers.get_content_type())
            self.assertIn(b"export function renderModelStatusElement", response.read())

    def test_stream_starts_full_then_sends_delta_and_retains_partial_rows(self):
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
            self.assertEqual(b"event: delta\n", response.readline())
            second = json.loads(response.readline()[6:])
            self.assertGreater(second["revision"], first["revision"])
            self.assertEqual(first["generation"], second["generation"])
            self.assertEqual(first["revision"], second["baseRevision"])
            self.assertEqual("item", second["changes"]["items"]["upsert"][0]["id"])
            self.assertEqual([], second["changes"]["items"]["remove"])
            self.assertEqual("stale", second["freshness"]["state"])
            self.assertEqual("stale", second["changes"]["items"]["upsert"][0]["evidence"]["identity"]["freshness"])
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


    def test_evidence_pages_pin_revision_and_preserve_notes_and_unknown_authors(self):
        self.live.model.accept("work-items", None, Read([
            {"id": "item", "title": "Visible", "state": "open"}], True, 1, "test-read"))
        self.live.model.accept("assignments", None, Read([
            {"id": "assignment", "workItemId": "item", "state": "closed"}], True, 1, "test-read"))
        rows = [{"id": str(i), "assignmentId": "assignment", "kind": "progress",
                 "ts": i, "note": "Note " + str(i), "byUser": None, "bySession": None}
                for i in range(3)]
        from unittest.mock import Mock
        self.live.gateway = Mock(read_count=0)
        self.live.gateway.read.side_effect = lambda resource, scope: Read(rows, True, 1, "test-read")
        self.live.model.accept("attests", "assignment", Read(rows, True, 1, "test-read"))
        self.live._publish()
        status, first = self.request("/api/items/item/evidence?limit=2")
        self.assertEqual(200, status)
        self.assertTrue(first["complete"])
        self.assertEqual(3, first["total"])
        self.assertEqual(2, first["nextOffset"])
        self.assertEqual("Note 0", first["records"][0]["note"])
        self.assertIsNone(first["records"][0]["byUser"])
        pin = "&generation=" + first["generation"] + "&revision=" + str(first["revision"])
        status, last = self.request("/api/items/item/evidence?offset=2&limit=2" + pin)
        self.assertEqual(200, status)
        self.assertEqual(["2"], [r["id"] for r in last["records"]])
        self.assertIsNone(last["nextOffset"])
        # Only long detail text changes; the next page must still invalidate.
        rows[0]["note"] = "Changed note"
        status, mismatch = self.request("/api/items/item/evidence?offset=2&limit=2" + pin)
        self.assertEqual(409, status)
        self.assertEqual(first["revision"], mismatch["revision"])
        event = self.live.events.get_nowait()
        self.assertEqual("detail-history", event[0])
        self.live._handle(*event)
        self.live._publish()
        status, mismatch = self.request("/api/items/item/evidence?offset=2&limit=2" + pin)
        self.assertEqual(409, status)
        self.assertEqual("revision-mismatch", mismatch["error"])
        self.assertGreater(mismatch["revision"], first["revision"])
        self.assertEqual(400, self.request("/api/items/item/evidence?offset=1")[0])
        self.assertEqual(400, self.request("/api/items/item/evidence?limit=101")[0])
        self.assertEqual(404, self.request("/api/items/missing/evidence")[0])
        status, restarted = self.request("/api/items/item/evidence?limit=2")
        self.assertEqual(200, status)
        self.assertEqual("Changed note", restarted["records"][0]["note"])
        self.assertNotIn("Changed note", json.dumps(self.request("/api/model")[1]))

    def test_evidence_page_orders_across_assignments_and_reports_partial_reads(self):
        from unittest.mock import Mock
        self.live.model.accept("work-items", None, Read([
            {"id": "item", "title": "Visible", "state": "open"}], True, 1, "test-read"))
        self.live.model.accept("assignments", None, Read([
            {"id": scope, "workItemId": "item", "state": "closed"} for scope in ('a', 'b')],
            True, 1, "test-read"))
        rows = {'a': [{'id': '1', 'ts': 1}, {'id': '4', 'ts': 4}],
                'b': [{'id': '2', 'ts': 2}, {'id': '3', 'ts': 3}]}
        for scope, records in rows.items():
            self.live.model.accept("attests", scope, Read(records, True, 1, "test-read"))
        self.live.gateway = Mock(read_count=0)
        self.live.gateway.read.side_effect = lambda resource, scope: Read(rows[scope], scope == 'a', 1, "test-read")
        self.live._publish()
        status, first = self.request('/api/items/item/evidence?limit=2')
        self.assertEqual(200, status)
        self.assertEqual(['1', '2'], [row['id'] for row in first['records']])
        self.assertEqual(4, first['total'])
        self.assertFalse(first['complete'])
        pin = '&generation=' + first['generation'] + '&revision=' + str(first['revision'])
        status, last = self.request('/api/items/item/evidence?offset=2&limit=2' + pin)
        self.assertEqual(200, status)
        self.assertEqual(['3', '4'], [row['id'] for row in last['records']])
        self.assertIsNone(last['nextOffset'])

    def test_reconnect_ignores_last_event_id_and_starts_with_current_full_model(self):
        request = Request(self.url + "/api/events", headers={"Last-Event-ID": "old"})
        with urlopen(request, timeout=3) as response:
            self.assertEqual(b"event: model\n", response.readline())
            model = json.loads(response.readline()[6:])
            self.assertEqual(self.live.snapshot(), model)

    def test_folded_closed_details_keep_order_notes_pins_and_partial_evidence(self):
        from unittest.mock import Mock
        assignments = [dict(id=scope, workItemId='item', state=state, holderKey='author',
                            subject='Detail '+scope)
                       for scope, state in (('a', 'closed'), ('b', 'open'), ('c', 'closed'))]
        rows = {scope: [dict(id=scope, ts=at, note='Note '+scope)]
                for scope, at in (('a', 3), ('b', 1), ('c', 2))}
        self.live.model.accept('work-items', None, Read([
            dict(id='item', title='Visible', state='open')], True, 1, 'test-read'))
        self.live.model.accept('assignments', None, Read(assignments, True, 1, 'test-read'))
        for scope, records in rows.items():
            self.live.model.accept('attests', scope, Read(records, True, 1, 'test-read'))
        partial = set()
        self.live.gateway = Mock(read_count=0)
        self.live.gateway.read.side_effect = lambda resource, scope=None: Read(
            assignments if resource == 'assignments' else rows[scope],
            (resource, scope) not in partial, 1, 'test-read')
        self.live._publish()
        before = self.request('/api/items/item/evidence?limit=2')
        self.live.model.fold_closed_assignments()
        for scope in ('a', 'c'):
            self.live.reads.epochs.pop(('attests', scope), None)
        self.assertEqual(before, self.request('/api/items/item/evidence?limit=2'))
        first = before[1]
        self.assertEqual(['b', 'c'], [row['id'] for row in first['records']])
        pin = '&generation=' + first['generation'] + '&revision=' + str(first['revision'])
        partial.update({('assignments', None), ('attests', 'c')})
        status, last = self.request('/api/items/item/evidence?offset=2&limit=2'+pin)
        self.assertEqual(200, status)
        self.assertFalse(last['complete'])
        self.assertEqual(['a'], [row['id'] for row in last['records']])
        self.assertEqual('Note a', last['records'][0]['note'])
        self.assertEqual({'b'}, set(self.live.model.inventory('assignments').rows))
        for scope in ('a', 'c'):
            self.assertNotIn(('attests', scope), self.live.model.inventories)
            self.assertNotIn(('attests', scope), self.live.reads.epochs)

    def test_changed_folded_detail_reloads_before_serving_a_new_pin(self):
        from test_service import PublishedReads
        gateway = PublishedReads()
        gateway.rows['assignments', None][0]['state'] = 'closed'
        self.live.gateway = self.live.reads.gateway = gateway
        self.live.connected = True

        def settle():
            while self.live.reads.pending or self.live.reads.active:
                self.live.reads.pump()
                self.live._handle(*self.live.events.get(timeout=5))
            self.live._publish()

        self.live.rebuild()
        settle()
        self.live.model.fold_closed_assignments()
        status, first = self.request('/api/items/work/evidence?limit=1')
        self.assertEqual(200, status)
        gateway.rows['attests', 'assignment'][0]['note'] = 'Changed detail only'
        status, mismatch = self.request('/api/items/work/evidence?limit=1')
        self.assertEqual(409, status)
        self.assertEqual(first['revision'], mismatch['revision'])
        event = self.live.events.get_nowait()
        self.assertEqual('detail-reload', event[0])
        self.assertNotIn(('attests', 'assignment'), self.live.model.inventories)
        self.live._handle(*event)
        settle()
        self.live.model.fold_closed_assignments()
        pin = '&generation=' + first['generation'] + '&revision=' + str(first['revision'])
        self.assertEqual(409, self.request('/api/items/work/evidence?limit=1'+pin)[0])
        status, fresh = self.request('/api/items/work/evidence?limit=1')
        self.assertEqual(200, status)
        self.assertGreater(fresh['revision'], first['revision'])
        self.assertEqual('Changed detail only', fresh['records'][0]['note'])
        self.assertNotIn(('attests', 'assignment'), self.live.model.inventories)
        gateway.rows['assignments', None][0]['subject'] = 'Changed assignment detail only'
        self.assertEqual(409, self.request('/api/items/work/evidence?limit=1')[0])
