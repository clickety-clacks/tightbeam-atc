"""Exercise passive failure capture without network or extra resolver work."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_git import GitResolver, reference_digest
from atc_service import LiveService
from test_git import inline_acquisitions


REMOTE = "git@github.com:clickety-clacks/tightbeam-atc.git"
COMMIT = "a" * 40
SECRET = "credential-must-not-appear"


class GitDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.resolver = GitResolver(Path(self.tmp.name), refs={REMOTE: ("refs/heads/main",)})
        inline_acquisitions(self.resolver)
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

    def test_raw_fetch_allowance_and_cached_result_do_not_retain_diagnostics(self):
        refs = [{"remote": REMOTE, "commit": COMMIT}]
        data, calls = self.capture(refs, self.command)
        self.assertGreater(calls, 0)
        ref = data["refs"][0]
        self.assertEqual(COMMIT, ref["commit"])
        self.assertEqual(1, ref["rawFetchAttempts"])
        failed = ref["failure"]
        self.assertEqual("raw-fetch", failed["step"])
        self.assertEqual(128, failed["exitCode"])
        self.assertEqual("git", failed["command"])
        self.assertEqual("other", failed["stderrCategory"])
        self.assertEqual(REMOTE, failed["remote"])
        self.assertEqual("refs/heads/main", failed["branch"])
        self.assertEqual("allowance-exhausted", ref["rawFetchSkipped"])
        self.assertEqual([], self.resolver.diagnostics)
        self.assertIsNone(self.resolver._diagnostic_ref)
        # A later check retries the unknown result once; duplicates within
        # that check still reuse it without extra fetches or failure history.
        repeated = refs + refs
        cached, calls = self.capture(repeated, self.command)
        self.assertGreater(calls, 0)
        self.assertEqual(1, cached["refs"][0]["rawFetchAttempts"])
        ref = cached["refs"][1]
        self.assertEqual("result-cache", ref["cache"])
        self.assertEqual(1, cached["reasonCounts"]["cached-inconclusive"])
        self.assertGreaterEqual(ref["cacheDetail"]["ageMs"], 0)
        self.assertIsNone(ref["cacheDetail"]["value"])
        self.assertEqual(reference_digest(repeated), cached["inputDigest"])
        self.assertEqual(2, cached["inputCount"])
        self.assertEqual(0, ref["rawFetchAttempts"])
        self.assertIsNone(ref["failure"])  # Do not invent a current command cause.
        self.assert_lookup_caches_unchanged()

    def test_unlabeled_exclusion_and_invalid_ref_are_identified_without_git(self):
        refs = [{"repo": "github.com/example/project", "commit": COMMIT},
                {"remote": "https://user:" + SECRET + "@example.invalid/repo", "commit": SECRET}]
        data, calls = self.capture(refs, self.command)
        self.assertEqual(0, calls)
        self.assertEqual({"excluded-repository": 1, "invalid-commit": 1}, data["reasonCounts"])
        self.assertEqual("example/project", data["refs"][0]["repository"])
        self.assertEqual("named-repository", data["refs"][0]["identityRoute"])
        self.assertEqual("<invalid>", data["refs"][1]["commit"])
        self.assertIsNone(data["refs"][1]["repository"])
        self.assertNotEqual(data["refs"][0]["inputDigest"], data["refs"][1]["inputDigest"])

    def test_error_categories_keep_no_raw_stderr(self):
        for message, category in (("fatal: not our ref ", "object-not-served"),
                                  ("Permission denied ", "authentication-or-authorization"),
                                  ("Could not resolve hostname ", "transport"),
                                  ("unrecognized ", "other")):
            with self.subTest(category=category):
                self.resolver.results.clear()
                def run(args):
                    result = self.command(args)
                    if "fetch" in args and args[-1] == COMMIT:
                        result.stderr = message + SECRET
                    return result
                data, _ = self.capture([{"remote": REMOTE, "commit": COMMIT}], run)
                self.assertEqual(category, data["refs"][0]["failure"]["stderrCategory"])

    def test_merge_patch_output_shape_and_failure_branch_survive_later_probes(self):
        def run(args):
            if "cat-file" in args:
                return subprocess.CompletedProcess(args, 0, "", "")
            if "merge-base" in args:
                return subprocess.CompletedProcess(args, 1, "", "")
            if "cherry" in args:
                return subprocess.CompletedProcess(args, 0, "+ " + COMMIT + "\n- " + SECRET + "\n", "")
            return self.command(args)
        data, _ = self.capture([{"remote": REMOTE, "commit": COMMIT}], run)
        ref = data["refs"][0]
        self.assertEqual("explicit-remote", ref["identityRoute"])
        self.assertIsNone(ref["fallback"])
        self.assertEqual("refs/heads/main", ref["failure"]["branch"])
        self.assertEqual("refs/heads/0.1.14", ref["branch"])  # Last probe, not the failed step.
        self.assertEqual({"lines": 2, "plus": 1, "minus": 1, "other": 0},
                         ref["failure"]["outputShape"])
        self.assertEqual({"patch-equivalence-output": 1}, data["failureCounts"])
        self.assertEqual({"inconclusive-probe": 1}, data["reasonCounts"])
        self.assertNotIn("/private/", json.dumps(data))

    def test_empty_targets_are_distinct_from_failed_branch_cache(self):
        def empty(args):
            return subprocess.CompletedProcess(args, 0, "", "")
        named = "git@github.com:clickety-clacks/example.git"
        data, calls = self.capture([{"remote": named, "commit": COMMIT}], empty)
        self.assertEqual(2, calls)
        self.assertEqual({"no-targets": 1}, data["reasonCounts"])
        self.assertEqual({"count": 0, "complete": False, "remote": named}, data["refs"][0]["targets"])
        data, calls = self.capture([{"remote": named, "commit": "b" * 40}], empty)
        self.assertEqual(0, calls)
        self.assertEqual("targets-cache", data["refs"][0]["cache"])
        self.assertEqual(0, data["refs"][0]["cacheDetail"]["count"])

    def assert_lookup_caches_unchanged(self):
        for cache in (self.resolver.results, self.resolver.origins,
                      self.resolver.targets, self.resolver.branches):
            for entry in cache.values():
                self.assertEqual(2, len(entry))  # Only original time/value payload.

    def test_branch_timeout_keeps_fallback_without_storing_failure_history(self):
        def run(args):
            if "fetch" in args:
                raise subprocess.TimeoutExpired(args + [SECRET], 4, stderr=SECRET)
            return self.command(args)

        refs = [{"repo": "retired:/private/tightbeam-atc-task", "commit": COMMIT},
                {"repo": "retired:/private/other", "commit": "b" * 40}]
        data, _ = self.capture(refs, run)
        ref = data["refs"][0]
        failed = ref["failure"]
        self.assertEqual("branch-fetch", failed["step"])
        self.assertTrue(failed["timeout"])
        self.assertEqual("TimeoutExpired", failed["error"])
        self.assertEqual("registered-ancestry", ref["fallback"])
        self.assertEqual("branch-cache", ref["cache"])
        # A different reference in this check reuses the failed lookup, but must
        # label that as cached/incomplete rather than proof of absent identity.
        ref = data["refs"][1]
        self.assertEqual("branch-cache", ref["cache"])
        self.assertFalse(ref["cacheDetail"]["available"])
        self.assertGreaterEqual(ref["cacheDetail"]["ageMs"], 0)
        self.assertEqual("TimeoutExpired", ref["failure"]["error"])
        self.assertEqual({"registered-lookup-inconclusive": 2}, data["reasonCounts"])
        self.assertNotIn("/private/", json.dumps(data))
        # Backoff avoids another download while keeping the bounded failure
        # category available for the current inconclusive check.
        data, calls = self.capture([refs[1]], run)
        self.assertEqual(calls, 0)
        self.assertTrue(data["refs"][0]["failure"]["timeout"])
        self.assertEqual({"registered-lookup-inconclusive": 1}, data["reasonCounts"])
        self.assert_lookup_caches_unchanged()

    def test_many_refs_and_shipping_lines_have_bounded_line_and_scratch(self):
        refs = [{"repo": f"retired:/removed/tightbeam-atc-task-{n}", "remote": REMOTE,
                 "commit": f"{n:040x}"} for n in range(100)]

        def run(args):
            if "--heads" in args:
                heads = ["main"] + [f"0.1.{n}" for n in range(30)]
                out = "".join(f"{COMMIT}\trefs/heads/{branch}\n" for branch in heads)
                return subprocess.CompletedProcess(args, 0, out, "")
            if "fetch" in args and len(args[-1]) == 40:
                return subprocess.CompletedProcess(args, 128, "", SECRET)
            return self.command(args)

        data, _ = self.capture(refs, run)
        self.assertLessEqual(len(json.dumps(data, separators=(",", ":")).encode())
                             + len("merge check inconclusive "), 4096)
        self.assertLessEqual(len(data["refs"]), 4)
        self.assertEqual(100, len(data["refs"]) + data["omittedRefs"])
        self.assertEqual(100, sum(data["reasonCounts"].values()))
        self.assertEqual({"raw-fetch": 100}, data["failureCounts"])
        self.assertEqual(100, data["inputCount"])
        self.assertEqual(reference_digest(refs), data["inputDigest"])
        self.assertNotIn("priorSteps", json.dumps(data))
        self.assertEqual([], self.resolver.diagnostics)
        self.assertIsNone(self.resolver._diagnostic_ref)
        self.assert_lookup_caches_unchanged()

    def test_path_only_references_do_not_probe_checkout_or_routed_ssh(self):
        self.resolver.host_routes = {"remote-host": "ssh-target-" + SECRET}

        def run(args):
            self.assertEqual("git", args[0])
            self.assertNotIn("-C", args)
            self.assertNotIn(SECRET, json.dumps(args))
            return self.command(args)

        data, calls = self.capture([{"repo": "remote-host:/" + SECRET, "commit": COMMIT}], run)
        self.assertGreater(calls, 0)
        self.assertIsNone(data["refs"][0]["failure"])
        self.assertEqual("missing-identity", data["refs"][0]["identityRoute"])
        self.assertEqual({"registered-lookup-inconclusive": 1}, data["reasonCounts"])

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
            self.assertEqual([], self.resolver.diagnostics)
            self.assertIsNone(self.resolver._diagnostic_ref)
            self.assert_lookup_caches_unchanged()

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
