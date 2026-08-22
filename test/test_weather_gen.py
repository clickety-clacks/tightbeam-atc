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


class WeatherGeneratorEvidenceTest(unittest.TestCase):
    def test_emits_only_durable_evidence_and_canonical_merge_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            merged, not_merged = self._make_repo(base / "repo")
            self._make_state_db(base / "state.db", merged, not_merged)
            output = base / "weather.json"
            Path(str(output) + ".fetch").touch()
            env = os.environ.copy()
            env.update({
                "TB_BASE_DIR": str(base),
                "TB_WEATHER_OUT": str(output),
                "TB_WEATHER_REPO": str(base / "repo"),
                "TB_WEATHER_REMOTE": "origin",
                "TB_WEATHER_BRANCH": "origin/main",
            })
            env.pop("TB_WEATHER_DEST", None)

            subprocess.run(["python3", str(GENERATOR)], env=env, check=True,
                           capture_output=True, text=True)
            snapshot = json.loads(output.read_text())
            items = {item["id"]: item for item in snapshot["items"]}

            later = items["wi_later_verdict"]
            self.assertEqual("open", later["state"])
            self.assertEqual(["verdict"], later["attestKinds"])
            self.assertEqual(["reviewed-clean"], later["verdictKinds"])

            non_code = items["wi_non_code_closed"]
            self.assertEqual("closed", non_code["state"])
            self.assertFalse(non_code["code"])
            self.assertIsNone(non_code["merged"])

            self.assertTrue(items["wi_code_true_closed"]["merged"])
            self.assertFalse(items["wi_code_false_closed"]["merged"])
            self.assertIsNone(items["wi_code_unknown_closed"]["merged"])

            counts = items["wi_assignment_counts"]
            self.assertEqual({"open": 1, "terminal": 2}, counts["assignments"])
            self.assertEqual(["completion", "verdict"], counts["attestKinds"])
            self.assertEqual(["verified"], counts["verdictKinds"])

            ready = items["wi_explicit_ready"]
            self.assertTrue(ready["readyToMerge"])
            self.assertFalse(ready["merged"])

            agent = next(a for a in snapshot["agents"]
                         if a["name"] == "Product Owner By Name Only")
            self.assertEqual("coder", agent["kind"])
            self.assertEqual("coder", agent["archetype"])
            self.assertEqual("origin/main", snapshot["mergeSource"]["branch"])
            self.assertEqual(str(base / "repo"), snapshot["mergeSource"]["remote"])

            # Retaining origin/main locally must not turn a failed canonical
            # refresh into a false absence claim.
            missing_remote = base / "missing-origin"
            self._git(base / "repo", "remote", "set-url", "origin",
                      str(missing_remote))
            failed_output = base / "failed-weather.json"
            env["TB_WEATHER_OUT"] = str(failed_output)
            subprocess.run(["python3", str(GENERATOR)], env=env, check=True,
                           capture_output=True, text=True)
            failed = {item["id"]: item for item in
                      json.loads(failed_output.read_text())["items"]}
            self.assertIsNone(failed["wi_code_false_closed"]["merged"])
            self.assertIsNone(failed["wi_code_true_closed"]["merged"])

    def _make_repo(self, path):
        path.mkdir()
        self._git(path, "init", "-q", "-b", "main")
        self._git(path, "config", "user.email", "atc-test@example.invalid")
        self._git(path, "config", "user.name", "ATC Test")
        (path / "evidence.txt").write_text("merged\n")
        self._git(path, "add", "evidence.txt")
        self._git(path, "commit", "-q", "-m", "merged fixture")
        merged = self._git(path, "rev-parse", "HEAD").stdout.strip()
        self._git(path, "remote", "add", "origin", str(path))
        self._git(path, "update-ref", "refs/remotes/origin/main", merged)
        self._git(path, "switch", "-q", "-c", "topic")
        (path / "evidence.txt").write_text("not merged\n")
        self._git(path, "commit", "-qam", "unmerged fixture")
        not_merged = self._git(path, "rev-parse", "HEAD").stdout.strip()
        return merged, not_merged

    def _git(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True)

    def _make_state_db(self, path, merged, not_merged):
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
            ("session:holder", "Product Owner By Name Only", "active", None,
             "coder", "codex", "test"),
        )

        items = [
            ("wi_later_verdict", "later verdict", "open", 7),
            ("wi_non_code_closed", "closed non-code", "closed", 6),
            ("wi_code_true_closed", "closed code merged", "closed", 5),
            ("wi_code_false_closed", "closed code not merged", "closed", 4),
            ("wi_code_unknown_closed", "closed code unknown", "closed", 3),
            ("wi_assignment_counts", "assignment counts", "open", 2),
            ("wi_explicit_ready", "explicit ready", "open", 1),
        ]
        con.executemany("INSERT INTO work_items VALUES (?, ?, ?, ?)", items)

        assignments = [
            ("asg_later", "wi_later_verdict", "session:holder", "closed", 9_999_999_999_990, None),
            ("asg_non_code", "wi_non_code_closed", "session:holder", "closed", 9_999_999_999_991, None),
            ("asg_true", "wi_code_true_closed", "session:holder", "closed", 9_999_999_999_992, None),
            ("asg_false", "wi_code_false_closed", "session:holder", "closed", 9_999_999_999_993, None),
            ("asg_unknown", "wi_code_unknown_closed", "session:holder", "closed", 9_999_999_999_994, None),
            ("asg_counts_open", "wi_assignment_counts", "session:holder", "open", None, None),
            ("asg_counts_done", "wi_assignment_counts", "session:holder", "closed", 9_999_999_999_995, None),
            ("asg_counts_revoked", "wi_assignment_counts", "session:holder", "closed", 9_999_999_999_996, None),
            ("asg_ready", "wi_explicit_ready", "session:holder", "open", None, None),
        ]
        con.executemany("INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?)", assignments)
        con.executemany(
            "INSERT INTO assignment_effects VALUES (?, ?)",
            [("asg_non_code", "coordination"), ("asg_true", "code"),
             ("asg_false", "code"), ("asg_unknown", "code"),
             ("asg_ready", "code")],
        )

        refs = lambda sha: json.dumps([{"repo": "fixture:repo", "commit": sha}])
        attests = [
            ("asg_later", "session:holder", 100, "verdict", "reviewed-clean", None),
            ("asg_true", "session:holder", 110, "progress", None, refs(merged)),
            ("asg_false", "session:holder", 120, "progress", None, refs(not_merged)),
            ("asg_unknown", "session:holder", 130, "progress", None, refs("f" * 40)),
            ("asg_counts_done", "session:holder", 140, "completion", None, None),
            ("asg_counts_revoked", "session:holder", 150, "verdict", "verified", None),
            ("asg_ready", "session:holder", 160, "verdict", "ready-to-merge",
             refs(not_merged)),
        ]
        con.executemany("INSERT INTO attests VALUES (?, ?, ?, ?, ?, ?)", attests)
        con.commit()
        con.close()


if __name__ == "__main__":
    unittest.main()
