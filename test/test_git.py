import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_git import CANONICAL_REFS, GitResolver

ATC_REMOTE = "git@github.com:clickety-clacks/tightbeam-atc.git"
TIGHTBEAM_REMOTE = "git@github.com:clickety-clacks/tightbeam.git"
LOCAL_HOST = socket.gethostname().split(".", 1)[0]


class LocalGitResolver(GitResolver):
    """Exercise real Git; only replace remote transport with local repositories."""
    def __init__(self, cache, remotes, failure=None):
        super().__init__(cache)
        self.remotes, self.failure = remotes, failure

    def _run(self, args):
        if "fetch" in args and self.failure == "timeout":
            raise subprocess.TimeoutExpired(args, 4)
        if "fetch" in args and self.failure == "failed":
            return subprocess.CompletedProcess(args, 1, "", "unavailable")
        mapped = [str(self.remotes.get(a, self.cache / "unavailable"))
                  if a.startswith("git@github.com:") else a for a in args]
        return super()._run(mapped)


class CanonicalGitTest(unittest.TestCase):
    def test_canonical_ancestry_patch_identity_and_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, landed, equivalent, absent = self._make_remote(root / "atc")
            release, release_remote, release_landed, release_absent = self._make_release_remote(root / "release")
            resolver = LocalGitResolver(root / "cache", {ATC_REMOTE: remote, TIGHTBEAM_REMOTE: release_remote})
            def ref(path, commit):
                return {"repo": f"{LOCAL_HOST}:{path}", "commit": commit}
            self.assertIs(True, resolver.resolve([ref(source, landed)]))
            self.assertIs(True, resolver.resolve([ref(source, equivalent)]))
            self.assertIs(False, resolver.resolve([ref(source, absent)]))
            self.assertIsNone(resolver.resolve([ref(source, "f" * 40)]))
            self.assertIsNone(resolver.resolve([ref(source, absent), ref(source, "f" * 40)]))
            self.assertIs(True, resolver.resolve([ref(release, release_landed)]))
            self.assertIs(False, resolver.resolve([ref(release, release_absent)]))
            self.assertEqual(("refs/heads/main", "refs/heads/0.1.8"), CANONICAL_REFS[TIGHTBEAM_REMOTE])

    def test_https_origin_and_deleted_checkout_canonical_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, landed, _, absent = self._make_remote(root, origin="https://github.com/clickety-clacks/tightbeam-atc")
            resolver = LocalGitResolver(root / "cache", {ATC_REMOTE: remote})
            self.assertIs(True, resolver.resolve([{"repo": f"{LOCAL_HOST}:{source}", "commit": landed}]))
            self.assertIs(True, resolver.resolve([{"repo": f"{LOCAL_HOST}:{root / 'deleted'}", "commit": landed}]))
            self.assertIsNone(resolver.resolve([{"repo": f"{LOCAL_HOST}:{root / 'deleted'}", "commit": absent}]))
            self.assertIs(True, resolver.resolve([{"commit": landed}]))
            self.assertIsNone(resolver.resolve([{"repo": f"{LOCAL_HOST}:{source}"}]))
            self.assertIsNone(resolver.resolve([{"remote": "https://example.invalid/unregistered", "commit": landed}]))

    def test_failed_and_timed_out_lookup_stay_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, landed, _, _ = self._make_remote(root)
            for failure in ("failed", "timeout"):
                resolver = LocalGitResolver(root / failure, {ATC_REMOTE: remote}, failure)
                self.assertIsNone(resolver.resolve([{"repo": f"{LOCAL_HOST}:{source}", "commit": landed}]))

    def test_correction_can_name_canonical_remote_after_checkout_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, landed, _, _ = self._make_remote(root)
            resolver = LocalGitResolver(root / "cache", {ATC_REMOTE: remote})
            self.assertIs(True, resolver.resolve([{"repo": "missing:/retired", "remote": ATC_REMOTE,
                                                  "ref": "refs/heads/main", "commit": landed}]))

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

    def _rev(self, repo, ref):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True,
                              capture_output=True, text=True).stdout.strip()

