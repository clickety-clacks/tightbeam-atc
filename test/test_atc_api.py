#!/usr/bin/env python3
"""Tests for the ruling and follow-up endpoints in server/tb-atc-api.py.

Spins up a REAL, disposable tb-atc-api.py instance on a random local port —
never the actual running deployment — with a fake `tightbeam` executable on
PATH that logs its args instead of touching any real gateway or waking any
real agent. This is the only place these endpoints are exercised end to end;
everything here is safety-critical (the interim path actually rules a real
decision and sends a real wake once deployed), so it is tested against the
real server code, not a reimplementation of it.
"""
import json
import os
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server" / "tb-atc-api.py"


class AtcApiDecisionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.web = self.base / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<html><body>test page</body></html>")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.state_db_dir = self.base / "tbstate"
        self.state_db_dir.mkdir()
        self.stub_log = self.base / "stub-calls.log"
        self._write_stub()
        self._make_state_db()

        self.port = self._free_port()
        self.api = f"http://127.0.0.1:{self.port}/api"
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env.update({
            "TB_ATC_HOST": "127.0.0.1", "TB_ATC_PORT": str(self.port),
            "TB_ATC_WEB": str(self.web),
            "TB_ATC_STATE": str(self.base / "tags.json"),
            "TB_BASE_DIR": str(self.state_db_dir),
            "TB_ATC_TOKEN_FILE": str(self.base / "operator.token"),
            "TB_ATC_AUDIT_LOG": str(self.base / "atc-rulings.log"),
        })
        self.env = env
        self.server = subprocess.Popen(["python3", str(SERVER)], env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_server()
        self.token = (self.base / "operator.token").read_text().strip()

    def tearDown(self):
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)
        self.tmp.cleanup()

    def _write_stub(self):
        stub = self.bin / "tightbeam"
        # NUL-separate args and record-separate (ASCII RS, 0x1e) calls. One
        # call's args (the wake prompt) legitimately contain embedded
        # newlines, so splitting on newlines — within or between calls —
        # cannot recover argv boundaries; NUL cannot appear in a shell arg.
        stub.write_text(
            "#!/bin/bash\n"
            "{ printf '%s\\0' \"$@\"; printf '\\x1e'; } >> \"" + str(self.stub_log) + "\"\n"
            'echo \'{"ok":true,"stub":true}\'\n'
            "exit 0\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _free_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _wait_for_server(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.api + "/state", timeout=0.5)
                return
            except OSError:
                time.sleep(0.05)
        self.fail("test tb-atc-api.py instance never came up")

    def _make_state_db(self):
        con = sqlite3.connect(self.state_db_dir / "state.db")
        con.executescript("""
            CREATE TABLE decision_requests (
                id TEXT PRIMARY KEY, kind TEXT, raiserId TEXT, raiserSessionKey TEXT,
                ownerUserId TEXT, assignmentId TEXT, raisedAt INTEGER, deadlineAt INTEGER,
                question TEXT, options TEXT, context TEXT, status TEXT
            );
        """)
        now = int(time.time() * 1000)
        con.execute("INSERT INTO decision_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dr_open01", "operator", "agent:tester", "s_raiser01", "george", None,
             now, now + 22 * 3600_000, "ship it?", '[{"label":"yes"},{"label":"no"}]',
             '{"note":null,"supersedes":null}', "open"))
        con.execute("INSERT INTO decision_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dr_ruled01", "operator", "agent:tester", "s_raiser01", "george", None,
             now, now + 3600_000, "already done", '[{"label":"yes"}]', '{}', "ruled"))
        con.commit()
        con.close()

    def _get(self, path):
        with urllib.request.urlopen(self.api + path, timeout=2) as r:
            return json.loads(r.read())

    def _post(self, path, body, token=None, cookie=None):
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-ATC-Operator"] = token
        if cookie is not None:
            headers["Cookie"] = f"atc_operator={cookie}"
        req = urllib.request.Request(self.api + path, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get_page(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="GET")
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, r.headers.get("Set-Cookie")

    def _stub_calls(self):
        """One entry per invocation, each the exact argv list it received.
        NUL-separated args, record-separated (0x1e) calls — a wake's prompt
        legitimately contains newlines within a single arg, so nothing here
        may split on newlines to recover boundaries."""
        if not self.stub_log.exists():
            return []
        raw = self.stub_log.read_bytes()
        calls = []
        for rec in raw.split(b"\x1e"):
            if not rec:
                continue
            args = [a.decode() for a in rec.split(b"\0") if a != b""]
            if args:
                calls.append(args)
        return calls

    # ---------- token ----------

    def test_token_generated_with_mode_600(self):
        tokfile = self.base / "operator.token"
        self.assertTrue(tokfile.exists())
        mode = tokfile.stat().st_mode & 0o777
        self.assertEqual(0o600, mode)
        self.assertGreaterEqual(len(self.token), 32)

    def test_missing_or_wrong_token_is_403(self):
        code, body = self._post("/decisions/dr_open01/rule",
                                 {"decision": "yes", "rationale": "Because it is ready."})
        self.assertEqual(403, code)
        code, body = self._post("/decisions/dr_open01/rule",
                                 {"decision": "yes", "rationale": "Because it is ready."},
                                 token="wrong-token")
        self.assertEqual(403, code)
        self.assertEqual([], self._stub_calls())   # never reached the gateway

    def test_page_load_sets_operator_cookie(self):
        code, set_cookie = self._get_page()
        self.assertEqual(200, code)
        self.assertIsNotNone(set_cookie)
        self.assertIn(f"atc_operator={self.token}", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Path=/api", set_cookie)

    def test_rule_accepts_cookie_with_no_header(self):
        code, body = self._post("/decisions/dr_open01/rule",
                                 {"decision": "yes", "rationale": "Because it is ready."},
                                 cookie=self.token)
        self.assertEqual(200, code)
        self.assertTrue(body.get("ok"))

    def test_rule_rejects_wrong_or_missing_cookie(self):
        code, body = self._post("/decisions/dr_open01/rule",
                                 {"decision": "yes", "rationale": "Because it is ready."},
                                 cookie="wrong-token")
        self.assertEqual(403, code)
        self.assertEqual([], self._stub_calls())   # never reached the gateway

    # ---------- rule: validation ----------

    def test_rule_requires_rationale(self):
        code, body = self._post("/decisions/dr_open01/rule", {"decision": "yes"}, self.token)
        self.assertEqual(400, code)
        self.assertIn("rationale", body["error"])
        self.assertEqual([], self._stub_calls())

    def test_rule_rejects_label_not_in_options(self):
        code, body = self._post("/decisions/dr_open01/rule",
            {"decision": "not-a-real-option", "rationale": "Because it seemed plausible."}, self.token)
        self.assertEqual(400, code)
        self.assertEqual([], self._stub_calls())

    def test_rule_rejects_non_open_row(self):
        code, body = self._post("/decisions/dr_ruled01/rule",
            {"decision": "yes", "rationale": "Because it needs to happen now."}, self.token)
        self.assertEqual(409, code)

    def test_rule_rejects_unknown_row(self):
        code, body = self._post("/decisions/dr_does_not_exist/rule",
            {"decision": "yes", "rationale": "Because it needs to happen now."}, self.token)
        self.assertEqual(404, code)

    def test_rule_rejects_short_response_with_no_rationale(self):
        code, body = self._post("/decisions/dr_open01/rule", {"response": "ok"}, self.token)
        self.assertEqual(400, code)

    # ---------- rule: success paths ----------

    def test_rule_with_label_and_rationale_calls_gateway_once_and_logs(self):
        code, body = self._post("/decisions/dr_open01/rule",
            {"decision": "yes", "rationale": "Because the tests pass and it is ready."}, self.token)
        self.assertEqual(200, code)
        self.assertTrue(body.get("ok"))
        calls = self._stub_calls()
        self.assertEqual(1, len(calls))
        args = calls[0]
        self.assertEqual(["--as-user", "george", "operator-rule", "dr_open01",
                           "--decision", "yes",
                           "--rationale", "Because the tests pass and it is ready."], args)
        audit = (self.base / "atc-rulings.log").read_text()
        self.assertIn("ruled dr_open01 yes by operator via atc at", audit)

    def test_rule_with_long_response_uses_it_as_its_own_rationale(self):
        code, body = self._post("/decisions/dr_open01/rule",
            {"response": "Let's hold off until the migration lands next week."}, self.token)
        self.assertEqual(200, code)
        calls = self._stub_calls()
        self.assertEqual(1, len(calls))
        args = calls[0]
        self.assertEqual(["--as-user", "george", "operator-rule", "dr_open01",
                           "--response", "Let's hold off until the migration lands next week.",
                           "--rationale", "Let's hold off until the migration lands next week."], args)

    # ---------- ask ----------

    def test_ask_requires_question(self):
        code, body = self._post("/decisions/dr_open01/ask", {}, self.token)
        self.assertEqual(400, code)
        self.assertEqual([], self._stub_calls())

    def test_ask_rejects_non_open_row(self):
        code, body = self._post("/decisions/dr_ruled01/ask", {"question": "why?"}, self.token)
        self.assertEqual(409, code)

    def test_ask_sends_one_wake_and_records_followup(self):
        code, body = self._post("/decisions/dr_open01/ask", {"question": "give me the ELI5"}, self.token)
        self.assertEqual(200, code)
        calls = self._stub_calls()
        self.assertEqual(1, len(calls))
        args = calls[0]
        self.assertEqual(["--as-user", "george", "wake", "--session", "s_raiser01",
                          "--prompt"], args[:6])
        prompt = args[6]
        self.assertIn("dr_open01", prompt)
        self.assertIn("give me the ELI5", prompt)
        self.assertIn("--supersedes dr_open01", prompt)

        state = self._get("/state")
        followups = {f["drId"]: f for f in state["followups"]}
        self.assertIn("dr_open01", followups)
        self.assertEqual("give me the ELI5", followups["dr_open01"]["question"])
        self.assertEqual("ship it?", followups["dr_open01"]["originalQuestion"])

    def test_followup_persists_across_restart(self):
        code, _ = self._post("/decisions/dr_open01/ask", {"question": "persist me"}, self.token)
        self.assertEqual(200, code)
        self.server.terminate()
        self.server.wait(timeout=5)
        self.server = subprocess.Popen(["python3", str(SERVER)], env=self.env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_server()
        state = self._get("/state")
        followups = {f["drId"]: f for f in state["followups"]}
        self.assertIn("dr_open01", followups)
        self.assertEqual("persist me", followups["dr_open01"]["question"])
        # the token must also be stable across a restart — same file, same value
        self.assertEqual(self.token, (self.base / "operator.token").read_text().strip())


if __name__ == "__main__":
    unittest.main()
