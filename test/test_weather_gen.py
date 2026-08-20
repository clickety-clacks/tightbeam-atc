#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "bin" / "tb-weather-gen"


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


if __name__ == "__main__":
    unittest.main()
