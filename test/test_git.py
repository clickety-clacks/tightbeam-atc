import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_evidence import merge_ring
from atc_git import CANONICAL_REFS, GitResolver

ATC_REMOTE = "git@github.com:clickety-clacks/tightbeam-atc.git"
TIGHTBEAM_REMOTE = "git@github.com:clickety-clacks/tightbeam.git"
SPECS_REMOTE = "git@github.com:clickety-clacks/tightbeam-specs.git"
OTHER_ORG_REMOTE = "git@github.com:clickety-clacks/other-project.git"
LOCAL_HOST = socket.gethostname().split(".", 1)[0]


class LocalGitResolver(GitResolver):
    """Exercise real Git; only replace remote transport with local repositories."""
    def __init__(self, cache, remotes, failure=None):
        super().__init__(cache)
        self.remotes, self.failure = remotes, failure

    def _run(self, args):
        if "ls-remote" in args and self.failure == "listing":
            return subprocess.CompletedProcess(args, 1, "", "unavailable")
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
            self.assertEqual(("refs/heads/main",), CANONICAL_REFS[TIGHTBEAM_REMOTE])

    def test_default_and_release_line_branches_count_across_org_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, commits = self._make_shipping_line_remote(
                root / "org-repo", origin=OTHER_ORG_REMOTE, default_branch="trunk")
            resolver = LocalGitResolver(root / "cache", {OTHER_ORG_REMOTE: remote})

            def ref(commit):
                return {"remote": OTHER_ORG_REMOTE, "commit": commit}

            # The dynamically discovered repository default and whole-name
            # version branches are shipping lines, including future versions.
            for name in ("default", "0.1.14-first", "0.1.14-second", "0.2.0", "1.0.0"):
                with self.subTest(branch=name):
                    self.assertIs(True, resolver.resolve([ref(commits[name])]))

            # Branches containing code without landing on a shipping line stay
            # unmerged, even when their names contain a version number.
            for name in ("feature/change", "candidate/0.1.14", "backport/change",
                         "archive/change", "attic/change", "agent/change",
                         "codex/atc-kungfu-evidence", "0.1.14-rc.1", "release/0.1.14"):
                with self.subTest(branch=name):
                    self.assertIs(False, resolver.resolve([ref(commits[name])]))

            self.assertIsNone(resolver.resolve([ref(commits["unreachable"])]))

    def test_quarantine_only_reference_stays_yellow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, commits = self._make_shipping_line_remote(root / "repo")
            resolver = LocalGitResolver(root / "cache", {TIGHTBEAM_REMOTE: remote})
            quarantine_only = {"remote": TIGHTBEAM_REMOTE,
                               "commit": commits["codex/atc-kungfu-evidence"]}
            self.assertIs(False, resolver.resolve([quarantine_only]))
            self.assertEqual("yellow", merge_ring(5, False, None, True))

            # This preserved work-item ref is not present in the fixture's
            # shipping lines; a missing object stays unknown and therefore yellow.
            preserved_quarantine_ref = {
                "remote": TIGHTBEAM_REMOTE,
                "commit": "d505270b2d72570a65f7668395e939b9a254925b",
            }
            self.assertIsNone(resolver.resolve([preserved_quarantine_ref]))
            self.assertEqual("yellow", merge_ring(5, None, None, True))

    def test_correction_ledger_refs_cover_main_release_and_shared_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, atc_remote, atc_landed, _, _ = self._make_remote(
                root / "atc", origin=ATC_REMOTE)
            _, specs_remote, specs_landed, _, _ = self._make_remote(
                root / "specs", origin=SPECS_REMOTE)
            _, tightbeam_remote, release_landed, _ = self._make_release_remote(root / "tightbeam")
            resolver = LocalGitResolver(root / "cache", {
                ATC_REMOTE: atc_remote,
                SPECS_REMOTE: specs_remote,
                TIGHTBEAM_REMOTE: tightbeam_remote,
            })
            corrected_main = {"repo": "retired:/atc", "remote": ATC_REMOTE,
                              "ref": "refs/heads/main", "commit": atc_landed}
            corrected_specs = {"remote": SPECS_REMOTE, "commit": specs_landed}
            release_line = {"repo": "retired:/tightbeam", "remote": TIGHTBEAM_REMOTE,
                            "ref": "refs/heads/0.1.8", "commit": release_landed}

            self.assertIs(True, resolver.resolve([corrected_main]))
            self.assertIs(True, resolver.resolve([corrected_specs]))
            self.assertIs(True, resolver.resolve([release_line]))
            # Two work items can share the same corrected port commit.
            self.assertIs(True, resolver.resolve([release_line, dict(release_line)]))

    def test_incomplete_branch_inventory_cannot_prove_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, _, _, absent = self._make_remote(root)
            resolver = LocalGitResolver(root / "cache", {ATC_REMOTE: remote}, "listing")
            self.assertIsNone(resolver.resolve([{"repo": f"{LOCAL_HOST}:{source}",
                                                 "commit": absent}]))

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

    def test_timed_out_default_branch_probe_does_not_hide_release_line_landing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, commits = self._make_shipping_line_remote(root / "repo")

            class DefaultBranchTimeoutResolver(LocalGitResolver):
                def _probe(self, remote_name, branch, commit, ancestry_only=False,
                           allow_commit_fetch=True):
                    if branch == "refs/heads/main":
                        raise subprocess.TimeoutExpired(["git", "fetch"], 4)
                    return super()._probe(
                        remote_name, branch, commit, ancestry_only,
                        allow_commit_fetch=allow_commit_fetch)

            resolver = DefaultBranchTimeoutResolver(root / "cache", {TIGHTBEAM_REMOTE: remote})
            result = resolver.resolve([{
                "repo": f"{LOCAL_HOST}:{source}", "commit": commits["0.1.14-first"],
            }])
            self.assertIs(True, result)

    def test_duplicate_unavailable_reference_reuses_result_and_fetches_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, commits = self._make_shipping_line_remote(root / "repo")

            class CountingResolver(LocalGitResolver):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.unavailable_commit = None
                    self.commit_fetches = 0
                    self.origin_lookups = 0

                def _origin(self, reference):
                    self.origin_lookups += 1
                    return super()._origin(reference)

                def _run(self, args):
                    if args and args[-1] == self.unavailable_commit:
                        self.commit_fetches += 1
                    return super()._run(args)

            resolver = CountingResolver(root / "cache", {TIGHTBEAM_REMOTE: remote})
            resolver.unavailable_commit = commits["unreachable"]
            ref = {"repo": f"{LOCAL_HOST}:{source}", "remote": TIGHTBEAM_REMOTE,
                   "commit": commits["unreachable"]}
            result = resolver.resolve([ref, dict(ref)])
            self.assertIsNone(result)
            self.assertEqual(1, resolver.origin_lookups)
            self.assertEqual(1, resolver.commit_fetches)

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
        subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD",
                        "refs/heads/main"], check=True, capture_output=True, text=True)
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
        subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD",
                        "refs/heads/main"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(source), "remote", "set-url", "origin",
                        TIGHTBEAM_REMOTE], check=True)
        return source, remote, landed, absent

    def _make_shipping_line_remote(self, root, origin=TIGHTBEAM_REMOTE, default_branch="main"):
        root.mkdir(parents=True, exist_ok=True)
        source = root / "source"
        remote = root / "canonical.git"
        subprocess.run(["git", "init", "-b", default_branch, str(source)], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email",
                        "test@example.com"], check=True)
        (source / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(source), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "base"],
                       check=True, capture_output=True, text=True)
        base = self._rev(source, "HEAD")

        commits = {}
        branches = [default_branch]
        (source / "default.txt").write_text("default shipping line\n")
        subprocess.run(["git", "-C", str(source), "add", "default.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "default shipping line"],
                       check=True, capture_output=True, text=True)
        commits["default"] = self._rev(source, "HEAD")

        names = ("0.1.14", "0.2.0", "1.0.0", "feature/change",
                 "candidate/0.1.14", "backport/change", "archive/change",
                 "attic/change", "agent/change", "codex/atc-kungfu-evidence",
                 "0.1.14-rc.1", "release/0.1.14")
        for name in names:
            subprocess.run(["git", "-C", str(source), "switch", "-c", name, base],
                           check=True, capture_output=True, text=True)
            filename = name.replace("/", "-") + ".txt"
            (source / filename).write_text(name + "\n")
            subprocess.run(["git", "-C", str(source), "add", filename], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-m", name],
                           check=True, capture_output=True, text=True)
            branches.append(name)
            if name == "0.1.14":
                commits["0.1.14-first"] = self._rev(source, "HEAD")
                (source / "0.1.14-second.txt").write_text("second sample\n")
                subprocess.run(["git", "-C", str(source), "add", "0.1.14-second.txt"], check=True)
                subprocess.run(["git", "-C", str(source), "commit", "-m", "second release sample"],
                               check=True, capture_output=True, text=True)
                commits["0.1.14-second"] = self._rev(source, "HEAD")
            else:
                commits[name] = self._rev(source, "HEAD")

        subprocess.run(["git", "-C", str(source), "switch", "-c", "unpublished", base],
                       check=True, capture_output=True, text=True)
        (source / "unreachable.txt").write_text("not published\n")
        subprocess.run(["git", "-C", str(source), "add", "unreachable.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "unpublished"],
                       check=True, capture_output=True, text=True)
        commits["unreachable"] = self._rev(source, "HEAD")

        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)],
                       check=True)
        subprocess.run(["git", "-C", str(source), "push", "origin", *branches], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD",
                        f"refs/heads/{default_branch}"], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(source), "remote", "set-url", "origin", origin],
                       check=True)
        return source, remote, commits

    def _rev(self, repo, ref):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], check=True,
                              capture_output=True, text=True).stdout.strip()

