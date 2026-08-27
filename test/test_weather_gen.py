#!/usr/bin/env python3
import contextlib
import io
import json
import os
import runpy
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import unittest
import warnings
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "bin" / "tb-weather-gen"
ATC_REMOTE = "git@github.com:clickety-clacks/tightbeam-atc.git"
SPECS_REMOTE = "git@github.com:clickety-clacks/tightbeam-specs.git"
LACHESIS_REMOTE = "git@github.com:clickety-clacks/lachesis.git"
TIGHTBEAM_REMOTE = "git@github.com:clickety-clacks/tightbeam.git"
LOCAL_HOST = socket.gethostname().split(".", 1)[0]


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
    def test_unlinked_turn_attribution_matches_prior_query_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            con = make_state_db(base / "state.db")
            sessions = [
                ("session:live", "live", "active", None, "coder", "codex", "test"),
                ("session:multi", "multiple", "active", None, "coder", "codex", "test"),
                ("session:linked", "linked", "active", None, "coder", "codex", "test"),
                ("session:ended", "ended", "active", None, "coder", "codex", "test"),
                ("session:null", "null", "active", None, "coder", "codex", "test"),
                ("session:bad", "non-integer", "active", None, "coder", "codex", "test"),
            ]
            con.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", sessions)
            item_ids = [
                "wi_boundary", "wi_pre_turn", "wi_multiple", "wi_direct",
                "wi_linked_attest", "wi_ended", "wi_null", "wi_non_integer",
            ]
            con.executemany(
                "INSERT INTO work_items VALUES (?, ?, 'open', ?)",
                [(wid, wid, index) for index, wid in enumerate(item_ids, 1)],
            )
            assignments = [
                ("asg_" + wid, wid, "session:holder", "closed", 1, None)
                for wid in item_ids
            ]
            con.executemany("INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?)",
                            assignments)
            con.executemany(
                "INSERT INTO turns VALUES (?, ?, NULL, ?, ?, ?)",
                [
                    ("session:live", None, 90, 100, None),
                    ("session:multi", None, 90, 100, None),
                    ("session:multi", None, 190, 200, None),
                    ("session:linked", "asg_wi_direct", 90, 100, None),
                    ("session:ended", None, 90, 100, 200),
                    ("session:null", None, 90, None, None),
                    ("session:bad", None, 90, "not-an-epoch", None),
                ],
            )
            con.executemany(
                "INSERT INTO attests VALUES (?, ?, ?, 'progress', NULL, NULL)",
                [
                    ("asg_wi_boundary", "session:live", 100),
                    ("asg_wi_pre_turn", "session:live", 99),
                    ("asg_wi_multiple", "session:multi", 150),
                    ("asg_wi_linked_attest", "session:linked", 150),
                    ("asg_wi_ended", "session:ended", 150),
                    ("asg_wi_null", "session:null", 150),
                    ("asg_wi_non_integer", "session:bad", 150),
                ],
            )
            con.commit()
            con.close()

            output = base / "weather.json"
            env = os.environ.copy()
            env.update({"TB_BASE_DIR": str(base), "TB_WEATHER_OUT": str(output)})
            env.pop("TB_WEATHER_DEST", None)
            result = subprocess.run(["python3", str(GENERATOR)], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)

            snapshot = json.loads(output.read_text())
            items = {item["id"]: item for item in snapshot["items"]}
            agents = {agent["name"]: agent["id"] for agent in snapshot["agents"]}
            self.assertEqual({agents["live"]: "run"}, items["wi_boundary"]["turns"])
            self.assertEqual({}, items["wi_pre_turn"]["turns"])
            self.assertEqual({agents["multiple"]: "run"},
                             items["wi_multiple"]["turns"])
            self.assertEqual({agents["linked"]: "run"}, items["wi_direct"]["turns"])
            self.assertEqual({}, items["wi_linked_attest"]["turns"])
            self.assertEqual({}, items["wi_ended"]["turns"])
            self.assertEqual({}, items["wi_null"]["turns"])
            self.assertEqual({}, items["wi_non_integer"]["turns"])

    def test_emits_only_durable_evidence_and_canonical_merge_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            helper = WeatherGeneratorCanonicalMergeTest()
            source, remote, merged, _, not_merged = helper._make_remote(base)
            self._make_state_db(base / "state.db", source, merged, not_merged)
            snapshot = helper._run_snapshot(base, {ATC_REMOTE: remote})
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
            sources = {s["remote"]: s["branches"]
                       for s in snapshot["mergeSources"]}
            self.assertEqual({
                ATC_REMOTE: ["refs/heads/main"],
                SPECS_REMOTE: ["refs/heads/main"],
                LACHESIS_REMOTE: ["refs/heads/main"],
                TIGHTBEAM_REMOTE: ["refs/heads/main", "refs/heads/0.1.8"],
            }, sources)

    def test_effects_and_assignment_counts_share_one_read_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = base / "state.db"
            con = make_state_db(db)
            con.commit()
            self.assertEqual("wal", con.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            con.execute("INSERT INTO work_items VALUES "
                        "('wi_race', 'effect coverage race', 'open', 1)")
            con.execute("INSERT INTO assignments VALUES "
                        "('asg_first', 'wi_race', 'session:holder', 'closed', 1, NULL)")
            con.execute("INSERT INTO assignment_effects VALUES "
                        "('asg_first', 'coordination')")
            con.commit()
            con.close()

            real_connect = sqlite3.connect
            interleaved = {"done": False}

            class InterleavingConnection:
                def __init__(self, reader):
                    self.reader = reader

                @property
                def row_factory(self):
                    return self.reader.row_factory

                @row_factory.setter
                def row_factory(self, value):
                    self.reader.row_factory = value

                def execute(self, sql, params=()):
                    cursor = self.reader.execute(sql, params)
                    if (not interleaved["done"]
                            and "LEFT JOIN assignment_effects" in sql):
                        writer = real_connect(db)
                        writer.execute("INSERT INTO assignments VALUES "
                                       "('asg_later', 'wi_race', 'session:holder', "
                                       "'open', NULL, NULL)")
                        writer.commit()
                        writer.close()
                        interleaved["done"] = True
                    return cursor

            def intercepted_connect(database, *args, **kwargs):
                reader = real_connect(database, *args, **kwargs)
                return InterleavingConnection(reader)

            output = base / "weather.json"
            env = {
                "TB_BASE_DIR": str(base),
                "TB_WEATHER_OUT": str(output),
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("sqlite3.connect", side_effect=intercepted_connect):
                os.environ.pop("TB_WEATHER_DEST", None)
                with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
                    warnings.simplefilter("ignore", ResourceWarning)
                    runpy.run_path(str(GENERATOR), run_name="__main__")

            item = next(item for item in json.loads(output.read_text())["items"]
                        if item["id"] == "wi_race")
            self.assertTrue(interleaved["done"])
            self.assertEqual({"open": 0, "terminal": 1}, item["assignments"])
            self.assertIs(item["code"], False)
            check = real_connect(db)
            self.assertEqual(2, check.execute(
                "SELECT count(*) FROM assignments WHERE workItemId='wi_race'"
            ).fetchone()[0])
            check.close()

    def _make_state_db(self, path, source, merged, not_merged):
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

        repo = f"{LOCAL_HOST}:{source}"
        refs = lambda sha: json.dumps([{"repo": repo, "commit": sha}])
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
    def test_registered_integration_refs_prove_atc_and_tightbeam_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            atc_source, atc_remote, landed, equivalent, absent = self._make_remote(
                root / "atc")
            tb_source, tb_remote, release_landed, release_absent = \
                self._make_release_remote(root / "tightbeam")
            missing = "f" * 40
            self._make_state_db(root / "state.db", {
                "wi_landed": [(atc_source, landed)],
                "wi_equivalent": [(atc_source, equivalent)],
                "wi_absent": [(atc_source, absent)],
                "wi_missing": [(atc_source, missing)],
                "wi_mixed": [(atc_source, absent), (atc_source, missing)],
                "wi_tightbeam_release": [(tb_source, release_landed)],
                "wi_tightbeam_absent": [(tb_source, release_absent)],
            })
            items = self._run(root, {
                ATC_REMOTE: atc_remote,
                TIGHTBEAM_REMOTE: tb_remote,
            })

            self.assertIs(True, items["wi_landed"]["merged"])
            self.assertIs(True, items["wi_equivalent"]["merged"])
            self.assertIs(False, items["wi_absent"]["merged"])
            self.assertIsNone(items["wi_missing"]["merged"])
            self.assertIsNone(items["wi_mixed"]["merged"])
            self.assertIs(True, items["wi_tightbeam_release"]["merged"])
            self.assertIs(False, items["wi_tightbeam_absent"]["merged"])
            self.assertTrue((root / "atc-canonical" / "tightbeam-atc.git").is_dir())
            self.assertTrue((root / "atc-canonical" / "tightbeam.git").is_dir())

    def test_https_origin_normalizes_to_registered_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, landed, _, _ = self._make_remote(
                root, origin="https://github.com/clickety-clacks/tightbeam-atc")
            self._make_state_db(root / "state.db", {
                "wi_https": [(source, landed)],
            })

            items = self._run(root, {ATC_REMOTE: remote})

            self.assertIs(True, items["wi_https"]["merged"])

    def test_workdir_identity_is_never_required_for_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, landed, _, absent = self._make_remote(root / "atc")
            unregistered, _, unregistered_landed, _, _ = self._make_remote(
                root / "unregistered",
                origin="git@github.com:clickety-clacks/not-registered.git")
            partial, partial_remote, _, _, partial_absent = self._make_remote(
                root / "partial", origin=TIGHTBEAM_REMOTE)
            self._make_state_db(root / "state.db", {
                "wi_deleted": [(root / "deleted-workdir", landed)],
                "wi_deleted_absent": [(root / "deleted-workdir", absent)],
                "wi_unregistered": [(unregistered, unregistered_landed)],
                "wi_missing_repo": [(None, landed)],
                "wi_missing_commit_field": [(source, None)],
                "wi_missing_commit": [(source, "f" * 40)],
                "wi_partial_registry": [(partial, partial_absent)],
                "wi_registered_absent": [(source, absent)],
            })
            items = self._run(root, {
                ATC_REMOTE: remote,
                TIGHTBEAM_REMOTE: partial_remote,
            })

            # A deleted workdir only costs us the shortcut of knowing WHICH
            # repository to ask. Ancestry on a registered canonical ref is
            # landing proof whoever produced the commit, so the landed one
            # still resolves; the one on no canonical ref stays unknown,
            # because an unreachable repository is not proof of absence.
            self.assertIs(True, items["wi_deleted"]["merged"])
            self.assertIsNone(items["wi_deleted_absent"]["merged"])
            self.assertIsNone(items["wi_unregistered"]["merged"])
            # No repo recorded at all: the spec still says test the recorded
            # COMMIT against the configured canonical branches, so a commit
            # that is an ancestor of one resolves.
            self.assertIs(True, items["wi_missing_repo"]["merged"])
            self.assertIsNone(items["wi_missing_commit_field"]["merged"])
            self.assertIsNone(items["wi_missing_commit"]["merged"])
            self.assertIsNone(items["wi_partial_registry"]["merged"])
            self.assertIs(False, items["wi_registered_absent"]["merged"])

    def test_canonical_lookup_failure_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, landed, _, _ = self._make_remote(root)
            self._make_state_db(root / "state.db", {
                "wi_lookup_failure": [(source, landed)],
            })
            items = self._run(root, {ATC_REMOTE: remote}, fail_fetch=True)

            self.assertIsNone(items["wi_lookup_failure"]["merged"])

    def test_canonical_lookup_timeout_is_unknown_and_writes_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, landed, _, _ = self._make_remote(root)
            self._make_state_db(root / "state.db", {
                "wi_lookup_timeout": [(source, landed)],
            })
            items = self._run(root, {ATC_REMOTE: remote}, hang_fetch=True)

            self.assertIsNone(items["wi_lookup_timeout"]["merged"])

    def _run(self, base, remotes, fail_fetch=False, hang_fetch=False):
        snapshot = self._run_snapshot(base, remotes, fail_fetch, hang_fetch)
        return {item["id"]: item for item in snapshot["items"]}

    def _run_snapshot(self, base, remotes, fail_fetch=False, hang_fetch=False):
        output = base / "weather.json"
        wrapper = base / "git-wrapper" / "git"
        wrapper.parent.mkdir()
        wrapper.write_text("""#!/usr/bin/env python3
import json
import os
import sys
import time

args = sys.argv[1:]
if "fetch" in args and os.environ.get("TEST_GIT_FAIL") == "1":
    raise SystemExit(1)
if "fetch" in args and os.environ.get("TEST_GIT_HANG") == "1":
    time.sleep(60)
remotes = json.loads(os.environ["TEST_GIT_REMOTES"])
args = [remotes.get(arg, arg) for arg in args]
os.execv(os.environ["TEST_REAL_GIT"], [os.environ["TEST_REAL_GIT"], *args])
""")
        wrapper.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "PATH": str(wrapper.parent) + os.pathsep + env["PATH"],
            "TB_BASE_DIR": str(base),
            "TB_WEATHER_OUT": str(output),
            "TEST_GIT_REMOTES": json.dumps({key: str(value)
                                             for key, value in remotes.items()}),
            "TEST_REAL_GIT": shutil.which("git"),
            "TEST_GIT_FAIL": "1" if fail_fetch else "0",
            "TEST_GIT_HANG": "1" if hang_fetch else "0",
        })
        env.pop("TB_WEATHER_DEST", None)
        subprocess.run(["python3", str(GENERATOR)], env=env, check=True,
                       capture_output=True, text=True, timeout=8)
        return json.loads(output.read_text())

    def _make_remote(self, root, origin=ATC_REMOTE):
        root.mkdir(parents=True, exist_ok=True)
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
        subprocess.run(["git", "-C", str(source), "remote", "set-url", "origin", origin],
                       check=True)
        return source, remote, landed, equivalent, absent

    def _make_release_remote(self, root):
        root.mkdir(parents=True, exist_ok=True)
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

        subprocess.run(["git", "-C", str(source), "switch", "-c", "0.1.8", base],
                       check=True, capture_output=True, text=True)
        (source / "release.txt").write_text("release integration\n")
        subprocess.run(["git", "-C", str(source), "add", "release.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "release landed"],
                       check=True, capture_output=True, text=True)
        landed = self._rev(source, "HEAD")

        subprocess.run(["git", "-C", str(source), "switch", "-c", "candidate", base],
                       check=True, capture_output=True, text=True)
        (source / "candidate.txt").write_text("not integrated\n")
        subprocess.run(["git", "-C", str(source), "add", "candidate.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "candidate"],
                       check=True, capture_output=True, text=True)
        absent = self._rev(source, "HEAD")

        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)],
                       check=True)
        subprocess.run(["git", "-C", str(source), "push", "origin",
                        "main", "0.1.8", "candidate"], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(source), "remote", "set-url", "origin",
                        TIGHTBEAM_REMOTE], check=True)
        return source, remote, landed, absent

    def _make_state_db(self, path, refs_by_item):
        con = make_state_db(path)
        for created_at, (wid, refs) in enumerate(refs_by_item.items(), 1):
            assignment = "asg_" + wid
            con.execute("INSERT INTO work_items VALUES (?, ?, 'open', ?)",
                        (wid, wid, created_at))
            con.execute("INSERT INTO assignments VALUES (?, ?, ?, 'closed', ?, NULL)",
                        (assignment, wid, "session:holder", 9_999_999_999_999))
            con.execute("INSERT INTO assignment_effects VALUES (?, 'code')", (assignment,))
            encoded_refs = []
            for repo, commit in refs:
                ref = {}
                if repo is not None:
                    ref["repo"] = f"{LOCAL_HOST}:{repo}"
                if commit is not None:
                    ref["commit"] = commit
                encoded_refs.append(ref)
            con.execute("INSERT INTO attests VALUES (?, ?, ?, 'progress', NULL, ?)",
                        (assignment, "session:holder", 100, json.dumps(encoded_refs)))
        con.commit()
        con.close()

    def _rev(self, repo, ref):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True,
                              capture_output=True, text=True).stdout.strip()


if __name__ == "__main__":
    unittest.main()
