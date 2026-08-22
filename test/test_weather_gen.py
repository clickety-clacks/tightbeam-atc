#!/usr/bin/env python3
import json
import os
import shutil
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


class WeatherGeneratorCanonicalMergeTest(unittest.TestCase):
    def test_canonical_remote_proves_landed_equivalent_absent_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, landed, equivalent, absent = self._make_remote(root)
            missing = "f" * 40
            self._make_state_db(root / "state.db", {
                "wi_landed": [landed],
                "wi_equivalent": [equivalent],
                "wi_absent": [absent],
                "wi_missing": [missing],
                "wi_mixed": [absent, missing],
            })
            items = self._run(root, remote)

            self.assertIs(True, items["wi_landed"]["merged"])
            self.assertIs(True, items["wi_equivalent"]["merged"])
            self.assertIs(False, items["wi_absent"]["merged"])
            self.assertIsNone(items["wi_missing"]["merged"])
            self.assertIsNone(items["wi_mixed"]["merged"])

    def test_canonical_lookup_failure_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, landed, _, _ = self._make_remote(root)
            self._make_state_db(root / "state.db", {"wi_lookup_failure": [landed]})
            items = self._run(root, remote, fail_fetch=True)

            self.assertIsNone(items["wi_lookup_failure"]["merged"])

    def test_canonical_lookup_timeout_is_unknown_and_writes_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, landed, _, _ = self._make_remote(root)
            self._make_state_db(root / "state.db", {"wi_lookup_timeout": [landed]})
            items = self._run(root, remote, hang_fetch=True)

            self.assertIsNone(items["wi_lookup_timeout"]["merged"])

    def _run(self, base, remote, fail_fetch=False, hang_fetch=False):
        output = base / "weather.json"
        wrapper = base / "git-wrapper" / "git"
        wrapper.parent.mkdir()
        wrapper.write_text("""#!/usr/bin/env python3
import os
import sys
import time

args = sys.argv[1:]
if "fetch" in args and os.environ.get("TEST_GIT_FAIL") == "1":
    raise SystemExit(1)
if "fetch" in args and os.environ.get("TEST_GIT_HANG") == "1":
    time.sleep(60)
args = [os.environ["TEST_GIT_REMOTE"] if arg ==
        "git@github.com:clickety-clacks/tightbeam-atc.git" else arg
        for arg in args]
os.execv(os.environ["TEST_REAL_GIT"], [os.environ["TEST_REAL_GIT"], *args])
""")
        wrapper.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "PATH": str(wrapper.parent) + os.pathsep + env["PATH"],
            "TB_BASE_DIR": str(base),
            "TB_WEATHER_OUT": str(output),
            "TEST_GIT_REMOTE": str(remote),
            "TEST_REAL_GIT": shutil.which("git"),
            "TEST_GIT_FAIL": "1" if fail_fetch else "0",
            "TEST_GIT_HANG": "1" if hang_fetch else "0",
        })
        env.pop("TB_WEATHER_DEST", None)
        subprocess.run(["python3", str(GENERATOR)], env=env, check=True,
                       capture_output=True, text=True, timeout=8)
        return {item["id"]: item for item in json.loads(output.read_text())["items"]}

    def _make_remote(self, root):
        source = root / "source"
        remote = root / "canonical.git"
        subprocess.run(["git", "init", "-b", "main", str(source)], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"],
                       check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email",
                        "test@example.com"], check=True)
        (source / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(source), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "base"],
                       check=True, capture_output=True, text=True)
        base = self._rev(source, "HEAD")

        (source / "same.txt").write_text("landed\n")
        subprocess.run(["git", "-C", str(source), "add", "same.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "landed"],
                       check=True, capture_output=True, text=True)
        landed = self._rev(source, "HEAD")

        subprocess.run(["git", "-C", str(source), "switch", "-c", "equivalent", base],
                       check=True, capture_output=True, text=True)
        (source / "same.txt").write_text("landed\n")
        subprocess.run(["git", "-C", str(source), "add", "same.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "equivalent"],
                       check=True, capture_output=True, text=True)
        equivalent = self._rev(source, "HEAD")

        subprocess.run(["git", "-C", str(source), "switch", "-c", "absent", base],
                       check=True, capture_output=True, text=True)
        (source / "absent.txt").write_text("not landed\n")
        subprocess.run(["git", "-C", str(source), "add", "absent.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "absent"],
                       check=True, capture_output=True, text=True)
        absent = self._rev(source, "HEAD")

        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)],
                       check=True)
        subprocess.run(["git", "-C", str(source), "push", "origin",
                        "main", "equivalent", "absent"], check=True,
                       capture_output=True, text=True)
        return remote, landed, equivalent, absent

    def _make_state_db(self, path, commits_by_item):
        WeatherGeneratorReadinessTest()._make_state_db(path)
        con = sqlite3.connect(path)
        for created_at, (wid, commits) in enumerate(commits_by_item.items(), 1):
            assignment = "asg_" + wid
            con.execute("INSERT INTO work_items VALUES (?, ?, 'open', ?)",
                        (wid, wid, created_at))
            con.execute("INSERT INTO assignments VALUES (?, ?, ?, 'closed', ?, NULL)",
                        (assignment, wid, "session:holder", 9_999_999_999_999))
            con.execute("INSERT INTO assignment_effects VALUES (?, 'code')", (assignment,))
            refs = [{"repo": "gibson:/deleted/session-workdir", "commit": commit}
                    for commit in commits]
            con.execute("INSERT INTO attests VALUES (?, ?, ?, 'progress', NULL, ?)",
                        (assignment, "session:holder", 100, json.dumps(refs)))
        con.commit()
        con.close()

    def _rev(self, repo, ref):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True,
                              capture_output=True, text=True).stdout.strip()


if __name__ == "__main__":
    unittest.main()
