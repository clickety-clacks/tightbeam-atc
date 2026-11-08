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
                fetches = 0
                fail = True

                def _run(self, args):
                    if "fetch" in args:
                        self.fetches += 1
                        if self.fail:
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
                self.assertEqual(1, resolver.fetches)
            resolver.fail = False
            with patch("atc_git.time.monotonic", return_value=start + resolver.ttl + 1):
                self.assertIs(True, resolver.resolve([{
                    "repo": "retired:/later-item", "commit": landed,
                }]))
            self.assertEqual(2, resolver.fetches)
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
