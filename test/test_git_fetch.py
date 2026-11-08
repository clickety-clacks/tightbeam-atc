"""Real Git acquisition and interrupted-download lifecycle checks."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import time
from unittest.mock import patch

import test_git

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_git import GitResolver


class GitFetchTest(unittest.TestCase):
    def test_cold_ancestry_omits_payload_without_breaking_named_patch_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = test_git.CanonicalGitTest()
            _, remote, landed, equivalent, absent = helper._make_remote(root / "repo")
            subprocess.run(["git", "--git-dir", str(remote), "config",
                            "uploadpack.allowFilter", "true"], check=True)
            resolver = test_git.LocalGitResolver(root / "cache", {
                test_git.ATC_REMOTE: remote.as_uri(),
            })
            resolver.refs = {test_git.ATC_REMOTE: ("refs/heads/main",)}
            self.assertIs(True, resolver.resolve([{"repo": "retired:/source", "commit": landed}]))
            ancestry = next((root / "cache").glob("*.ancestry.git"))
            # This is a real filtered upload-pack: no tree/blob material was
            # downloaded, and no lazy download may supply it in this assertion.
            missing = subprocess.run(["git", "--git-dir", str(ancestry),
                                      "cat-file", "-e", landed + "^{tree}"],
                                     capture_output=True, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
            self.assertNotEqual(0, missing.returncode)
            # Named references keep the full cache and patch-equivalence path.
            self.assertIs(True, resolver.resolve([{"remote": test_git.ATC_REMOTE,
                                                   "commit": equivalent}]))
            self.assertIs(False, resolver.resolve([{"remote": test_git.ATC_REMOTE,
                                                    "commit": absent}]))
            self.assertEqual(2, len(list((root / "cache").glob("*.git"))))

    def test_failed_registry_load_is_bounded_across_items_and_recovers_after_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = test_git.CanonicalGitTest()
            _, remote, landed, _, _ = helper._make_remote(root / "repo")
            subprocess.run(["git", "--git-dir", str(remote), "config",
                            "uploadpack.allowFilter", "true"], check=True)

            class RetryResolver(test_git.LocalGitResolver):
                transport = {"fetches": 0, "fail": True}

                def _run(self, args):
                    if "fetch" in args:
                        self.transport["fetches"] += 1
                        if self.transport["fail"]:
                            raise subprocess.TimeoutExpired(args, self.timeout)
                    return super()._run(args)

            resolver = RetryResolver(root / "cache", {test_git.ATC_REMOTE: remote.as_uri()})
            resolver.refs = {test_git.ATC_REMOTE: ("refs/heads/main",)}
            start = time.monotonic()
            with patch("atc_git.time.monotonic", return_value=start):
                for i in range(5):
                    self.assertIsNone(resolver.resolve([{
                        "repo": f"retired:/item-{i}", "commit": landed,
                    }]))
                self.assertEqual(1, resolver.transport["fetches"])
            resolver.transport["fail"] = False
            with patch("atc_git.time.monotonic", return_value=start + resolver.ttl + 1):
                self.assertIs(True, resolver.resolve([{
                    "repo": "retired:/later-item", "commit": landed,
                }]))
            self.assertEqual(2, resolver.transport["fetches"])
            self.assertEqual([], list((root / "cache").rglob("tmp_pack_*")))

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_repeated_interrupted_fetch_reaps_helpers_and_partial_packs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack_dir = root / "objects" / "pack"
            pack_dir.mkdir(parents=True)
            existing = pack_dir / "tmp_pack_prior"
            existing.write_bytes(b"prior invocation")
            complete = pack_dir / "pack-complete.pack"
            complete.write_bytes(b"existing successful data")
            script = root / "download.py"
            script.write_text('''import pathlib, subprocess, sys, time
root = pathlib.Path(sys.argv[sys.argv.index("--git-dir") + 1])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
(root / "child-pid").write_text(str(child.pid))
(root / "objects/pack/tmp_pack_current").write_bytes(b"x" * 65536)
print("download started", flush=True)
time.sleep(30)
''')
            resolver = GitResolver(root, timeout=0.5)
            args = [sys.executable, str(script), "--git-dir", str(root), "fetch"]
            for _ in range(2):
                with self.assertRaises(subprocess.TimeoutExpired):
                    resolver._run(args)
                self.assertFalse((pack_dir / "tmp_pack_current").exists())
                self.assertEqual(b"prior invocation", existing.read_bytes())
                self.assertEqual(b"existing successful data", complete.read_bytes())
                # A killed orphan can briefly be a zombie until init reaps it;
                # either state means no surviving downloader can write packs.
                pid = int((root / "child-pid").read_text())
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                       capture_output=True, text=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            self.assertEqual({existing.name, complete.name}, {p.name for p in pack_dir.iterdir()})

    def test_acquisition_is_single_flight_and_does_not_block_loaded_proofs(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from atc_service import LiveService
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = test_git.CanonicalGitTest()
            _, fast, landed, _, _ = helper._make_remote(root / "fast")
            _, slow, slow_commit, _, _ = helper._make_remote(root / "slow")
            entered, release = Event(), Event()
            calls = []

            class HeldTransport(test_git.LocalGitResolver):
                def _run(self, args):
                    if "fetch" in args and test_git.SPECS_REMOTE in args:
                        calls.append(args)
                        entered.set()
                        if not release.wait(10):
                            raise AssertionError("acquisition was not released")
                    return super()._run(args)

            resolver = HeldTransport(root / "cache", {
                test_git.ATC_REMOTE: fast, test_git.SPECS_REMOTE: slow,
            })
            resolver.refs = {test_git.ATC_REMOTE: ("refs/heads/main",),
                             test_git.SPECS_REMOTE: ("refs/heads/main",)}
            resolver.acquisitions.executor = ThreadPoolExecutor(max_workers=2)
            service = LiveService(object(), resolver=resolver)
            workers = service._merge_resolvers()
            try:
                resolver.acquisitions.get(test_git.ATC_REMOTE)
                resolver.acquisitions.entries[test_git.ATC_REMOTE][0].result(timeout=10)
                self.assertTrue(resolver.acquisitions.get(test_git.ATC_REMOTE)["loaded"])
                self.assertIsNone(resolver.acquisitions.get(test_git.SPECS_REMOTE))
                self.assertTrue(entered.wait(5))
                for worker in workers:
                    self.assertIs(resolver.acquisitions, worker.acquisitions)
                    self.assertIsNone(worker.acquisitions.get(test_git.SPECS_REMOTE))
                # This proof executes while the second repository is still held.
                self.assertIs(True, workers[-1].resolve([
                    {"repo": "retired:/fast", "commit": landed}]))
                self.assertFalse(release.is_set())
                self.assertEqual(1, len(calls))
                release.set()
                resolver.acquisitions.entries[test_git.SPECS_REMOTE][0].result(timeout=10)
                self.assertIs(True, resolver.resolve([
                    {"repo": "retired:/slow", "commit": slow_commit}]))
            finally:
                release.set()
                resolver.acquisitions.executor.shutdown(wait=True)
                service.stop()

    def test_first_acquisition_has_separate_deadline_and_proofs_keep_command_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = test_git.CanonicalGitTest()
            _, remote, landed, _, _ = helper._make_remote(root / "repo")
            observed = []

            class Deadlines(test_git.LocalGitResolver):
                def _run(self, args):
                    if "fetch" in args:
                        observed.append((self.timeout, getattr(self, "_acquisition_deadline", None)))
                    return super()._run(args)

            resolver = Deadlines(root / "cache", {test_git.ATC_REMOTE: remote})
            resolver.refs = {test_git.ATC_REMOTE: ("refs/heads/main",)}
            began = time.monotonic()
            self.assertIs(True, resolver.resolve([{"repo": "retired:/source", "commit": landed}]))
            self.assertEqual(4, observed[0][0])
            self.assertGreater(observed[0][1], began)
            self.assertLessEqual(observed[0][1], time.monotonic() + 120)
            self.assertFalse(hasattr(resolver, "_acquisition_deadline"))
            # Refresh the same real repository; ordinary command cap applies.
            resolver.acquisitions.entries.clear()
            resolver.results.clear()
            self.assertIs(True, resolver.resolve([{"repo": "retired:/source", "commit": landed}]))
            self.assertEqual((4, None), observed[1])
