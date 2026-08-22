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


def make_state_db(path):
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
    return con


class WeatherGeneratorEvidenceTest(unittest.TestCase):
    def test_emits_only_durable_evidence_and_canonical_merge_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            helper = WeatherGeneratorCanonicalMergeTest()
            remote, merged, _, not_merged = helper._make_remote(base)
            self._make_state_db(base / "state.db", merged, not_merged)
            snapshot = helper._run_snapshot(base, remote)
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

            incomplete = items["wi_code_incomplete"]
            self.assertIsNone(incomplete["code"])
            self.assertIsNone(incomplete["merged"])

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
            self.assertEqual("refs/heads/main", snapshot["mergeSource"]["branch"])
            self.assertEqual("git@github.com:clickety-clacks/tightbeam-atc.git",
                             snapshot["mergeSource"]["remote"])

    def _make_state_db(self, path, merged, not_merged):
        con = make_state_db(path)

        items = [
            ("wi_later_verdict", "later verdict", "open", 7),
            ("wi_non_code_closed", "closed non-code", "closed", 6),
            ("wi_code_true_closed", "closed code merged", "closed", 5),
            ("wi_code_false_closed", "closed code not merged", "closed", 4),
            ("wi_code_unknown_closed", "closed code unknown", "closed", 3),
            ("wi_code_incomplete", "incomplete effect coverage", "open", 2),
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
            ("asg_incomplete", "wi_code_incomplete", "session:holder", "closed", 9_999_999_999_994, None),
            ("asg_incomplete_review", "wi_code_incomplete", "session:holder", "closed", 9_999_999_999_994, "asg_incomplete"),
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
            ("asg_incomplete", "session:holder", 131, "verdict", "tests-passed", None),
            ("asg_incomplete_review", "session:holder", 132, "verdict",
             "reviewed-clean", refs(not_merged)),
            ("asg_counts_done", "session:holder", 140, "completion", None, None),
            ("asg_counts_revoked", "session:holder", 150, "verdict", "verified", None),
            ("asg_ready", "session:holder", 160, "verdict", "ready-to-merge",
             refs(not_merged)),
        ]
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
        snapshot = self._run_snapshot(base, remote, fail_fetch, hang_fetch)
        return {item["id"]: item for item in snapshot["items"]}

    def _run_snapshot(self, base, remote, fail_fetch=False, hang_fetch=False):
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
        return json.loads(output.read_text())

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
        con = make_state_db(path)
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
