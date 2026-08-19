#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
