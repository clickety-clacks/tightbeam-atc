"""Exercise passive failure capture without network or extra resolver work."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_git import GitResolver
from atc_service import LiveService


REMOTE = "git@github.com:clickety-clacks/tightbeam-atc.git"
COMMIT = "a" * 40
SECRET = "credential-must-not-appear"


class GitDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.resolver = GitResolver(Path(self.tmp.name), refs={REMOTE: ("refs/heads/main",)})
        self.service = LiveService(object(), resolver=self.resolver)
        self.addCleanup(self.service.stop)

    def run_worker(self, refs, run, resolver=None):
        self.service.merge_jobs.put(("wi_12345678", refs))
        self.service.merge_jobs.put(None)
        with patch.object(self.resolver, "_run", side_effect=run) as command:
            self.service._git_worker(resolver or self.resolver)
        _, (_, _, value, _) = self.service.events.get_nowait()
        return value, command.call_count

    def command(self, args):
        # Only the transport is substituted; the real resolution path executes.
        out, code = "", 0
        if "--symref" in args:
            out = "ref: refs/heads/main\tHEAD\n"
        elif "--heads" in args:
            out = f"{COMMIT}\trefs/heads/main\n{COMMIT}\trefs/heads/0.1.14\n"
        elif "cat-file" in args:
            code = 1
        elif "fetch" in args and args[-1] == COMMIT:
            code = 128
        return subprocess.CompletedProcess(args, code, out, SECRET)

    def capture(self, refs, run):
        with self.assertLogs("atc.recovery", level="WARNING") as logs:
            value, calls = self.run_worker(refs, run)
        self.assertIsNone(value)
        self.assertEqual(1, len(logs.records))
        line = logs.records[0].getMessage()
        self.assertNotIn("\n", line)
        self.assertNotIn(SECRET, line)
        data = json.loads(line.removeprefix("merge check inconclusive "))
        self.assertEqual("wi_12345678", data["item"])
        self.assertGreaterEqual(data["elapsedMs"], 0)
        return data, calls

    def test_raw_fetch_allowance_and_cached_cause_with_one_line_per_check(self):
        refs = [{"remote": REMOTE, "commit": COMMIT}]
        data, calls = self.capture(refs, self.command)
        self.assertGreater(calls, 0)
        self.assertEqual(COMMIT, data["refs"][0]["commit"])
        steps = data["refs"][0]["steps"]
        fetches = [s for s in steps if s["step"] == "raw-fetch"]
        self.assertEqual(1, len(fetches))
        self.assertEqual(128, fetches[0]["exitCode"])
        self.assertEqual("git", fetches[0]["command"])
        self.assertEqual(REMOTE, fetches[0]["remote"])
        self.assertEqual("refs/heads/main", fetches[0]["branch"])
        self.assertTrue(any(s.get("reason") == "allowance-exhausted" for s in steps))
        cached, calls = self.capture(refs, self.command)
        self.assertEqual(0, calls)  # Capture must not bypass the result cache.
        prior = cached["refs"][0]["steps"][0]
        self.assertEqual("result-cache", prior["step"])
        self.assertIn(fetches[0], prior["priorSteps"])

    def test_branch_timeout_keeps_fallback_and_cached_failure_details(self):
        def run(args):
            if "fetch" in args:
                raise subprocess.TimeoutExpired(args + [SECRET], 4, stderr=SECRET)
            return self.command(args)

        data, _ = self.capture([{"repo": "retired:/private/tightbeam-atc-task", "commit": COMMIT}], run)
        steps = data["refs"][0]["steps"]
        failed = next(s for s in steps if s["step"] == "branch-fetch")
        self.assertTrue(failed["timeout"])
        self.assertEqual("TimeoutExpired", failed["error"])
        self.assertTrue(any(s.get("route") == "registered-ancestry" for s in steps))
        cache = next(s for s in steps if s["step"] == "branch-cache")
        self.assertFalse(cache["available"])
        self.assertIn(failed, cache["priorSteps"])
        self.assertNotIn("/private/", json.dumps(data))

    def test_routed_ssh_failure_has_no_command_or_error_text(self):
        self.resolver.host_routes = {"remote-host": "ssh-target-" + SECRET}

        def run(args):
            self.assertEqual("ssh", args[0])
            raise OSError(SECRET)

        data, calls = self.capture([{"repo": "remote-host:/" + SECRET, "commit": COMMIT}], run)
        self.assertEqual(1, calls)
        step = data["refs"][0]["steps"][0]
        self.assertEqual("origin", step["step"])
        self.assertEqual("routed-ssh/git", step["command"])
        self.assertEqual("OSError", step["error"])

    def test_definite_results_do_not_log_even_after_another_ref_fails(self):
        for answer in (True, False):
            self.resolver.results.clear()
            self.resolver.branches.clear()
            def run(args):
                if "cat-file" in args:
                    return subprocess.CompletedProcess(args, 0, "", SECRET)
                if "merge-base" in args:
                    return subprocess.CompletedProcess(args, 0 if answer else 1, "", SECRET)
                if "cherry" in args:
                    return subprocess.CompletedProcess(args, 0, "+ " + COMMIT, SECRET)
                return self.command(args)
            refs = [{"remote": REMOTE, "commit": COMMIT}]
            if answer:
                refs.insert(0, {"remote": "https://user:" + SECRET + "@example.invalid/repo", "commit": COMMIT})
            with self.assertNoLogs("atc.recovery", level="WARNING"):
                value, _ = self.run_worker(refs, run)
            self.assertIs(answer, value)

    def test_worker_exception_records_class_without_message(self):
        class BrokenResolver:
            def resolve(self, refs):
                raise RuntimeError(SECRET)

        with self.assertLogs("atc.recovery", level="WARNING") as logs:
            value, _ = self.run_worker([], self.command, BrokenResolver())
        self.assertIsNone(value)
        self.assertEqual(1, len(logs.records))
        self.assertNotIn(SECRET, logs.output[0])
        self.assertIn('"error":"RuntimeError"', logs.output[0])
