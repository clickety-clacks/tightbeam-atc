#!/usr/bin/env python3
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "bin" / "tb-weather-gen"
SERVER = ROOT / "server" / "tb-atc-api.py"


class WeatherGeneratorReadinessTest(unittest.TestCase):
    def test_open_assignment_caps_ready_and_closed_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._make_state_db(base / "state.db")
            output = base / "weather.json"
            env = os.environ.copy()
            env.update({"TB_BASE_DIR": str(base), "TB_WEATHER_OUT": str(output)})
            env.pop("TB_WEATHER_DEST", None)
            env.pop("TB_WEATHER_REPO", None)

            subprocess.run(["python3", str(GENERATOR)], env=env, check=True,
                           capture_output=True, text=True)
            items = {item["id"]: item for item in json.loads(output.read_text())["items"]}

            self.assertEqual(4, items["wi_mixed_ready"]["stage"])
            self.assertEqual(4, items["wi_mixed_closed"]["stage"])
            self.assertEqual(5, items["wi_terminal_ready"]["stage"])
            self.assertEqual(6, items["wi_terminal_closed"]["stage"])

    def _make_state_db(self, path):
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE sessions (
                sessionKey TEXT PRIMARY KEY, displayName TEXT, state TEXT,
                spawnedBy TEXT, archetype TEXT, harness TEXT, model TEXT
            );
            CREATE TABLE work_items (
                id TEXT PRIMARY KEY, title TEXT, state TEXT, createdAt INTEGER
            );
            CREATE TABLE assignments (
                id TEXT PRIMARY KEY, workItemId TEXT, holderKey TEXT, state TEXT,
                closedAt INTEGER, reviewsAssignmentId TEXT
            );
            CREATE TABLE assignment_effects (assignmentId TEXT, effectKind TEXT);
            CREATE TABLE attests (
                assignmentId TEXT, bySession TEXT, ts INTEGER, kind TEXT,
                verdictKind TEXT, commitRefs TEXT
            );
            CREATE TABLE turns (
                sessionKey TEXT, assignmentId TEXT, wakeId TEXT, createdAt INTEGER,
                startedAt INTEGER, endedAt INTEGER
            );
            CREATE TABLE wakes (
                wakeId TEXT, creatorSessionKey TEXT, sessionKey TEXT, state TEXT,
                dueAt INTEGER, firedAt INTEGER, work_item_id TEXT
            );
            CREATE TABLE roles (name TEXT, boundSessionKey TEXT);
            CREATE TABLE messages (id TEXT, sessionKey TEXT, sender TEXT, timestamp INTEGER);
        """)
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("session:holder", "Coder", "active", None, "coder", "codex", "test"),
        )

        items = [
            ("wi_mixed_ready", "mixed ready", "open", 4),
            ("wi_mixed_closed", "mixed closed", "closed", 3),
            ("wi_terminal_ready", "terminal ready", "open", 2),
            ("wi_terminal_closed", "terminal closed", "closed", 1),
        ]
        con.executemany("INSERT INTO work_items VALUES (?, ?, ?, ?)", items)

        assignments = []
        for wid in ("wi_mixed_ready", "wi_mixed_closed"):
            assignments.append((f"asg_{wid}_open", wid, "session:holder", "open", None, None))
        for wid in ("wi_mixed_ready", "wi_mixed_closed", "wi_terminal_ready",
                    "wi_terminal_closed"):
            assignments.append((f"asg_{wid}_code", wid, "session:holder", "closed",
                                9_999_999_999_998, None))
            assignments.append((f"asg_{wid}_review", wid, "session:holder", "closed",
                                9_999_999_999_999,
                                f"asg_{wid}_code"))
        con.executemany("INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?)", assignments)
        con.executemany(
            "INSERT INTO assignment_effects VALUES (?, 'code')",
            [(f"asg_{wid}_code",) for wid, _, _, _ in items],
        )

        attests = []
        for wid, _, _, _ in items:
            code = f"asg_{wid}_code"
            review = f"asg_{wid}_review"
            attests.extend([
                (code, "session:holder", 100, "progress", None, None),
                (code, "session:holder", 200, "verdict", "tests-passed", None),
                (review, "session:holder", 300, "verdict", "reviewed-clean", None),
                (code, "session:holder", 301, "completion", None, None),
            ])
        con.executemany("INSERT INTO attests VALUES (?, ?, ?, ?, ?, ?)", attests)
        con.commit()
        con.close()


class WeatherGeneratorEngagedTest(unittest.TestCase):
    def test_engaged_flag_matches_open_assignment_or_pending_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._make_state_db(base / "state.db")
            output = base / "weather.json"
            env = os.environ.copy()
            env.update({"TB_BASE_DIR": str(base), "TB_WEATHER_OUT": str(output)})
            env.pop("TB_WEATHER_DEST", None)
            env.pop("TB_WEATHER_REPO", None)

            subprocess.run(["python3", str(GENERATOR)], env=env, check=True,
                           capture_output=True, text=True)
            agents = {a["id"]: a for a in json.loads(output.read_text())["agents"]}

            self.assertTrue(agents["s_holder"]["engaged"])   # open assignment
            self.assertTrue(agents["s_waiter"]["engaged"])   # pending wake, no card
            self.assertFalse(agents["s_idle"]["engaged"])    # neither
            self.assertFalse(agents["s_retired"]["engaged"]) # retired, would
                                                              # otherwise qualify

    def _make_state_db(self, path):
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE sessions (
                sessionKey TEXT PRIMARY KEY, displayName TEXT, state TEXT,
                spawnedBy TEXT, archetype TEXT, harness TEXT, model TEXT
            );
            CREATE TABLE work_items (
                id TEXT PRIMARY KEY, title TEXT, state TEXT, createdAt INTEGER
            );
            CREATE TABLE assignments (
                id TEXT PRIMARY KEY, workItemId TEXT, holderKey TEXT, state TEXT,
                closedAt INTEGER, reviewsAssignmentId TEXT
            );
            CREATE TABLE assignment_effects (assignmentId TEXT, effectKind TEXT);
            CREATE TABLE attests (
                assignmentId TEXT, bySession TEXT, ts INTEGER, kind TEXT,
                verdictKind TEXT, commitRefs TEXT
            );
            CREATE TABLE turns (
                sessionKey TEXT, assignmentId TEXT, wakeId TEXT, createdAt INTEGER,
                startedAt INTEGER, endedAt INTEGER
            );
            CREATE TABLE wakes (
                wakeId TEXT, creatorSessionKey TEXT, sessionKey TEXT, state TEXT,
                dueAt INTEGER, firedAt INTEGER, work_item_id TEXT
            );
            CREATE TABLE roles (name TEXT, boundSessionKey TEXT);
            CREATE TABLE messages (id TEXT, sessionKey TEXT, sender TEXT, timestamp INTEGER);
        """)
        con.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("s_holder", "Holder", "active", None, "coder", "codex", "test"),
                ("s_waiter", "Waiter", "active", None, "coder", "codex", "test"),
                ("s_idle", "Idle", "active", None, "coder", "codex", "test"),
                ("s_retired", "Retired", "retired", None, "coder", "codex", "test"),
            ],
        )
        con.execute("INSERT INTO work_items VALUES (?, ?, ?, ?)",
                    ("wi_holds_a_card", "holds a card", "open", 1))
        con.execute(
            "INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?)",
            ("asg_open", "wi_holds_a_card", "s_holder", "open", None, None),
        )
        # s_retired would qualify by open assignment alone; retired must win.
        con.execute(
            "INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?)",
            ("asg_open_retired", "wi_holds_a_card", "s_retired", "open", None, None),
        )
        # dueAt is real wall-clock time: the generator computes NOW from
        # time.time() itself, not from anything the fixture controls.
        due = int(time.time() * 1000) + 10_000
        con.execute(
            "INSERT INTO wakes VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("wake_1", "s_holder", "s_waiter", "pending", due, None, None),
        )
        con.commit()
        con.close()


class WeatherGeneratorDeskTest(unittest.TestCase):
    def test_decisions_feed_matches_operator_open_rows_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._make_state_db(base / "state.db")
            output = base / "weather.json"
            env = os.environ.copy()
            env.update({"TB_BASE_DIR": str(base), "TB_WEATHER_OUT": str(output)})
            env.pop("TB_WEATHER_DEST", None)
            env.pop("TB_WEATHER_REPO", None)
            env.pop("TB_ATC_API", None)   # no live API in this test: feed-only

            subprocess.run(["python3", str(GENERATOR)], env=env, check=True,
                           capture_output=True, text=True)
            decisions = {d["id"]: d for d in json.loads(output.read_text())["decisions"]}

            # effort-kind and ruled rows are not desk items
            self.assertNotIn("dr_effort", decisions)
            self.assertNotIn("dr_ruled", decisions)
            # open, not-yet-expired
            d = decisions["dr_open"]
            self.assertEqual("wi_linked", d["workItemId"])
            self.assertEqual("s_raiser", d["raiserAgentId"])
            self.assertEqual(["yes", "no"], d["options"])
            self.assertEqual("a note", d["note"])
            # a request past its deadline but still status='open' still shows
            # (the desk strip greys it; only the board arrows/tags drop it)
            self.assertIn("dr_expired", decisions)

    def test_missing_decision_requests_table_degrades_to_empty_feed(self):
        # decision_requests is a 0.1.8+ table. An older gateway, or any
        # fixture that predates it (like the readiness/engaged tests' own),
        # must not crash generation over a table that simply is not there.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            WeatherGeneratorReadinessTest()._make_state_db(base / "state.db")
            output = base / "weather.json"
            env = os.environ.copy()
            env.update({"TB_BASE_DIR": str(base), "TB_WEATHER_OUT": str(output)})
            env.pop("TB_WEATHER_DEST", None)
            env.pop("TB_WEATHER_REPO", None)
            env.pop("TB_ATC_API", None)

            subprocess.run(["python3", str(GENERATOR)], env=env, check=True,
                           capture_output=True, text=True)
            self.assertEqual([], json.loads(output.read_text())["decisions"])

    def _make_state_db(self, path):
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE sessions (
                sessionKey TEXT PRIMARY KEY, displayName TEXT, state TEXT,
                spawnedBy TEXT, archetype TEXT, harness TEXT, model TEXT
            );
            CREATE TABLE work_items (
                id TEXT PRIMARY KEY, title TEXT, state TEXT, createdAt INTEGER
            );
            CREATE TABLE assignments (
                id TEXT PRIMARY KEY, workItemId TEXT, holderKey TEXT, state TEXT,
                closedAt INTEGER, reviewsAssignmentId TEXT
            );
            CREATE TABLE assignment_effects (assignmentId TEXT, effectKind TEXT);
            CREATE TABLE attests (
                assignmentId TEXT, bySession TEXT, ts INTEGER, kind TEXT,
                verdictKind TEXT, commitRefs TEXT
            );
            CREATE TABLE turns (
                sessionKey TEXT, assignmentId TEXT, wakeId TEXT, createdAt INTEGER,
                startedAt INTEGER, endedAt INTEGER
            );
            CREATE TABLE wakes (
                wakeId TEXT, creatorSessionKey TEXT, sessionKey TEXT, state TEXT,
                dueAt INTEGER, firedAt INTEGER, work_item_id TEXT
            );
            CREATE TABLE roles (name TEXT, boundSessionKey TEXT);
            CREATE TABLE messages (id TEXT, sessionKey TEXT, sender TEXT, timestamp INTEGER);
            CREATE TABLE decision_requests (
                id TEXT PRIMARY KEY, kind TEXT, raiserId TEXT, raiserSessionKey TEXT,
                ownerUserId TEXT, assignmentId TEXT, raisedAt INTEGER, deadlineAt INTEGER,
                question TEXT, options TEXT, context TEXT, status TEXT
            );
        """)
        con.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("s_raiser", "Raiser", "active", None, "coder", "codex", "test"))
        con.execute("INSERT INTO work_items VALUES (?, ?, ?, ?)",
                    ("wi_linked", "linked item", "open", 1))
        con.execute("INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?)",
                    ("asg_linked", "wi_linked", "s_raiser", "open", None, None))
        now = int(time.time() * 1000)
        rows = [
            ("dr_open", "operator", "agent:tester", "s_raiser", "george", "asg_linked",
             now, now + 22 * 3600_000, "should we ship it?",
             '[{"label":"yes"},{"label":"no"}]', '{"note":"a note","supersedes":null}', "open"),
            ("dr_expired", "operator", "agent:tester", "s_raiser", "george", None,
             now - 100_000, now - 1_000, "expired but still open",
             '[{"label":"ok"}]', '{}', "open"),
            ("dr_ruled", "operator", "agent:tester", "s_raiser", "george", None,
             now, now + 3600_000, "already ruled",
             '[{"label":"ok"}]', '{}', "ruled"),
            ("dr_effort", "effort", "process:tightbeam", None, "george", "asg_linked",
             now, now + 3600_000, "effort check-in", None, '{}', "open"),
        ]
        con.executemany("INSERT INTO decision_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()


class WeatherGeneratorDeskSyncTest(unittest.TestCase):
    """The riskiest part of the Desk layer: sync_desk's idempotent, author-
    scoped diff against the ATC API. This spins up a REAL, disposable
    tb-atc-api.py instance on a random local port — never the actual running
    deployment — so the test exercises the real server logic (author
    persistence, delete-by-id, search edit-in-place), not a hand-simplified
    stand-in for it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.web = self.base / "web"
        self.web.mkdir()
        self.state_file = self.base / "tags.json"
        self.port = self._free_port()
        self.api = f"http://127.0.0.1:{self.port}/api"
        env = os.environ.copy()
        env.update({"TB_ATC_HOST": "127.0.0.1", "TB_ATC_PORT": str(self.port),
                    "TB_ATC_WEB": str(self.web), "TB_ATC_STATE": str(self.state_file)})
        self.server = subprocess.Popen(["python3", str(SERVER)], env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_server()

    def tearDown(self):
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)
        self.tmp.cleanup()

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

    def _api_get(self, path):
        with urllib.request.urlopen(self.api + path, timeout=2) as r:
            return json.loads(r.read())

    def _api_post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.api + path, data=data, method="POST",
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read())

    def _run_gen(self, base):
        env = os.environ.copy()
        env.update({"TB_BASE_DIR": str(base), "TB_WEATHER_OUT": str(base / "weather.json"),
                    "TB_ATC_API": self.api})
        env.pop("TB_WEATHER_DEST", None)
        env.pop("TB_WEATHER_REPO", None)
        subprocess.run(["python3", str(GENERATOR)], env=env, check=True,
                        capture_output=True, text=True)

    def _make_db(self, base, deadline_ms, status="open"):
        con = sqlite3.connect(base / "state.db")
        con.executescript("""
            CREATE TABLE sessions (
                sessionKey TEXT PRIMARY KEY, displayName TEXT, state TEXT,
                spawnedBy TEXT, archetype TEXT, harness TEXT, model TEXT
            );
            CREATE TABLE work_items (id TEXT PRIMARY KEY, title TEXT, state TEXT, createdAt INTEGER);
            CREATE TABLE assignments (
                id TEXT PRIMARY KEY, workItemId TEXT, holderKey TEXT, state TEXT,
                closedAt INTEGER, reviewsAssignmentId TEXT
            );
            CREATE TABLE assignment_effects (assignmentId TEXT, effectKind TEXT);
            CREATE TABLE attests (
                assignmentId TEXT, bySession TEXT, ts INTEGER, kind TEXT,
                verdictKind TEXT, commitRefs TEXT
            );
            CREATE TABLE turns (
                sessionKey TEXT, assignmentId TEXT, wakeId TEXT, createdAt INTEGER,
                startedAt INTEGER, endedAt INTEGER
            );
            CREATE TABLE wakes (
                wakeId TEXT, creatorSessionKey TEXT, sessionKey TEXT, state TEXT,
                dueAt INTEGER, firedAt INTEGER, work_item_id TEXT
            );
            CREATE TABLE roles (name TEXT, boundSessionKey TEXT);
            CREATE TABLE messages (id TEXT, sessionKey TEXT, sender TEXT, timestamp INTEGER);
            CREATE TABLE decision_requests (
                id TEXT PRIMARY KEY, kind TEXT, raiserId TEXT, raiserSessionKey TEXT,
                ownerUserId TEXT, assignmentId TEXT, raisedAt INTEGER, deadlineAt INTEGER,
                question TEXT, options TEXT, context TEXT, status TEXT
            );
        """)
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                    ("s_raiser", "Raiser", "active", None, "coder", "codex", "test"))
        con.execute("INSERT INTO work_items VALUES (?,?,?,?)",
                    ("wi_linked", "linked item", "open", 1))
        con.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?)",
                    ("asg_linked", "wi_linked", "s_raiser", "open", None, None))
        now = int(time.time() * 1000)
        con.execute("INSERT INTO decision_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dr_sync01", "operator", "agent:tester", "s_raiser", "george", "asg_linked",
             now, deadline_ms, "ship it?", '[{"label":"yes"},{"label":"no"}]',
             '{"note":null,"supersedes":null}', status))
        con.commit()
        con.close()

    def _set_decision(self, base, **fields):
        con = sqlite3.connect(base / "state.db")
        cols = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE decision_requests SET {cols} WHERE id='dr_sync01'",
                    list(fields.values()))
        con.commit()
        con.close()

    def _desk(self, state):
        return {"arrows": [a for a in state["arrows"] if a.get("author") == "atc:desk"],
                "tags": [t for t in state["tags"] if t.get("author") == "atc:desk"],
                "searches": [s for s in state["searches"] if s.get("author") == "atc:desk"]}

    def test_full_sync_cycle(self):
        base = self.base
        now = int(time.time() * 1000)
        self._make_db(base, now + 22 * 3600_000)

        # 1. creation
        self._run_gen(base)
        desk = self._desk(self._api_get("/state"))
        self.assertEqual(1, len(desk["arrows"]))
        self.assertEqual("s_raiser", desk["arrows"][0]["from"])
        self.assertEqual("wi_linked", desk["arrows"][0]["to"])
        self.assertEqual(1, len(desk["tags"]))
        self.assertEqual("s_raiser", desk["tags"][0]["target"])
        self.assertIn("needs George", desk["tags"][0]["text"])
        self.assertEqual(1, len(desk["searches"]))
        self.assertEqual("where George is needed", desk["searches"][0]["label"])
        arrow_id = desk["arrows"][0]["arrowId"]
        tag_id = desk["tags"][0]["tagId"]
        search_id = desk["searches"][0]["searchId"]

        # 2. idempotency: an unchanged decision re-run must not duplicate
        self._run_gen(base)
        desk = self._desk(self._api_get("/state"))
        self.assertEqual(1, len(desk["arrows"]))
        self.assertEqual(arrow_id, desk["arrows"][0]["arrowId"])
        self.assertEqual(1, len(desk["tags"]))
        self.assertEqual(tag_id, desk["tags"][0]["tagId"])
        self.assertEqual(1, len(desk["searches"]))
        self.assertEqual(search_id, desk["searches"][0]["searchId"])

        # 3. tag text updates IN PLACE (new id, still exactly one) when the
        # countdown crosses an hour bucket — must not accumulate
        self._set_decision(base, deadlineAt=now + 2 * 3600_000)
        self._run_gen(base)
        desk = self._desk(self._api_get("/state"))
        self.assertEqual(1, len(desk["tags"]))
        self.assertNotEqual(tag_id, desk["tags"][0]["tagId"])
        self.assertNotIn("22h", desk["tags"][0]["text"])
        # the search edits in place: same id even though the tag churned
        self.assertEqual(1, len(desk["searches"]))
        self.assertEqual(search_id, desk["searches"][0]["searchId"])

        # 4. ruled: every atc:desk-authored thing is removed
        self._set_decision(base, status="ruled")
        self._run_gen(base)
        desk = self._desk(self._api_get("/state"))
        self.assertEqual([], desk["arrows"])
        self.assertEqual([], desk["tags"])
        self.assertEqual([], desk["searches"])

    def test_expired_but_open_drops_annotations_but_stays_in_feed(self):
        base = self.base
        now = int(time.time() * 1000)
        self._make_db(base, now - 60_000)   # already past deadline, still 'open'

        self._run_gen(base)
        desk = self._desk(self._api_get("/state"))
        self.assertEqual([], desk["arrows"])
        self.assertEqual([], desk["tags"])
        self.assertEqual([], desk["searches"])

        decisions = json.loads((base / "weather.json").read_text())["decisions"]
        self.assertEqual(["dr_sync01"], [d["id"] for d in decisions])

    def test_never_touches_another_authors_annotations(self):
        # The issue-#10-class regression the review flagged: the author-
        # scoped diff must be provably incapable of sweeping up anything it
        # did not itself create — pre-seed a different author's annotations
        # and confirm they survive both a sync AND a subsequent cleanup.
        base = self.base
        self._api_post("/tags", {"tags": [{"target": "s_someone_else", "text": "unrelated",
                                            "color": "blue", "author": "someone-else"}]})
        self._api_post("/arrows", {"arrows": [{"from": "s_a", "to": "wi_b", "text": "unrelated",
                                                "color": "blue", "author": "someone-else"}]})
        self._api_post("/searches", {"searches": [{"ids": ["s_a"], "label": "unrelated search",
                                                    "author": "someone-else"}]})
        before = self._api_get("/state")
        self.assertEqual(1, len(before["tags"]))
        self.assertEqual(1, len(before["arrows"]))
        self.assertEqual(1, len(before["searches"]))

        now = int(time.time() * 1000)
        self._make_db(base, now + 22 * 3600_000)
        self._run_gen(base)   # creates its own atc:desk annotations alongside

        after = self._api_get("/state")
        others = lambda coll: [x for x in after[coll] if x["author"] == "someone-else"]
        self.assertEqual(1, len(others("tags")))
        self.assertEqual("unrelated", others("tags")[0]["text"])
        self.assertEqual(1, len(others("arrows")))
        self.assertEqual(1, len(others("searches")))
        desk = self._desk(after)
        self.assertEqual(1, len(desk["tags"]))
        self.assertEqual(1, len(desk["arrows"]))
        self.assertEqual(1, len(desk["searches"]))

        # rule the decision: atc:desk annotations vanish, the OTHER author's
        # remain completely untouched — the actual regression this guards
        self._set_decision(base, status="ruled")
        self._run_gen(base)
        after = self._api_get("/state")
        desk = self._desk(after)
        self.assertEqual([], desk["tags"])
        self.assertEqual([], desk["arrows"])
        self.assertEqual([], desk["searches"])
        self.assertEqual(1, len([t for t in after["tags"] if t["author"] == "someone-else"]))
        self.assertEqual(1, len([a for a in after["arrows"] if a["author"] == "someone-else"]))
        self.assertEqual(1, len([s for s in after["searches"] if s["author"] == "someone-else"]))


if __name__ == "__main__":
    unittest.main()
