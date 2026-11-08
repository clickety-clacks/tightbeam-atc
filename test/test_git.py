from concurrent.futures import Future
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_evidence import merge_ring
from atc_git import CANONICAL_REFS, GitResolver

ATC_REMOTE = "git@github.com:clickety-clacks/tightbeam-atc.git"
TIGHTBEAM_REMOTE = "git@github.com:clickety-clacks/tightbeam.git"
SPECS_REMOTE = "git@github.com:clickety-clacks/tightbeam-specs.git"
CLAWLINE_REMOTE = "git@github.com:clickety-clacks/clawline.git"
HARKLIGHT_AGENT_REMOTE = "git@github.com:clickety-clacks/harklight-agent.git"
LOCAL_HOST = socket.gethostname().split(".", 1)[0]


class InlineAcquisitions:
    """Use real acquisition code without thread scheduling in transport fixtures."""
    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as error:
            future.set_exception(error)
        return future

    def shutdown(self, **kwargs):
        pass


def inline_acquisitions(resolver):
    resolver.acquisitions.executor.shutdown(wait=True)
    resolver.acquisitions.executor = InlineAcquisitions()


class LocalGitResolver(GitResolver):
    """Exercise real Git; only replace remote transport with local repositories."""
    def __init__(self, cache, remotes, failure=None):
        super().__init__(cache)
        inline_acquisitions(self)
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
    def test_path_sha_finds_application_and_tooling_shipping_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remotes = {}
            commits = {}
            for name in ("harklight-app", "surf-ace", "tightbeam-tuner"):
                remote_name = f"git@github.com:clickety-clacks/{name}.git"
                _, remote, commits[name] = self._make_shipping_line_remote(
                    root / name, origin=remote_name)
                remotes[remote_name] = remote
            resolver = LocalGitResolver(root / "cache", remotes)
            # Keep the shipped registry: no path-derived or test-added identity.
            for name, key in (("harklight-app", "default"), ("surf-ace", "0.4"),
                              ("tightbeam-tuner", "0.1.14-first")):
                with self.subTest(repository=name):
                    self.assertIs(True, resolver.resolve([{
                        "repo": "retired:/unrelated/deleted-checkout",
                        "commit": commits[name][key],
                    }]))
            for key in ("quarantine/held-work", "unreachable"):
                with self.subTest(unmerged=key):
                    self.assertIsNone(resolver.resolve([{
                        "repo": "retired:/unrelated/deleted-checkout",
                        "commit": commits["surf-ace"][key],
                    }]))
            # An explicit out-of-organization identity cannot use that lookup.
            self.assertIsNone(resolver.resolve([{
                "repo": "github:other/surf-ace", "commit": commits["surf-ace"]["0.4"],
            }]))

    def test_path_lookup_requires_full_sha_even_when_abbreviation_is_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, landed, _, _ = self._make_remote(root / "repo", origin=SPECS_REMOTE)
            resolver = LocalGitResolver(root / "cache", {SPECS_REMOTE: remote})
            resolver.refs = {SPECS_REMOTE: ("refs/heads/main",)}
            path = {"repo": "retired:/deleted-checkout", "commit": landed}
            self.assertIs(True, resolver.resolve([path]))
            with patch.object(resolver, "_run", wraps=resolver._run) as run:
                self.assertIsNone(resolver.resolve([{**path, "commit": landed[:12]}]))
                run.assert_not_called()
            self.assertEqual({"path-requires-full-sha": 1},
                             resolver.diagnostic_summary()["reasonCounts"])

    def test_failed_branch_load_backs_off_across_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, landed, _, _ = self._make_remote(root / "repo", origin=SPECS_REMOTE)

            class BranchFailureResolver(LocalGitResolver):
                transport = {"failed": True, "fetches": 0}

                def _run(self, args):
                    if "fetch" in args and any(a.startswith("refs/heads/main:") for a in args):
                        self.transport["fetches"] += 1
                        if self.transport["failed"]:
                            raise subprocess.TimeoutExpired(args, self.timeout)
                    return super()._run(args)

            resolver = BranchFailureResolver(root / "cache", {SPECS_REMOTE: remote})
            resolver.refs = {SPECS_REMOTE: ("refs/heads/main",)}
            # Path-only refs exercise the deployment registry, with no named
            # repository proof or folder inference to hide a failed branch.
            ref = {"repo": "retired:/checkout", "commit": landed}
            other = {"repo": "retired:/other", "commit": "f" * 40}
            with patch("atc_git.time.monotonic", return_value=100):
                self.assertIsNone(resolver.resolve([ref, dict(ref), other]))
                self.assertEqual(1, resolver.transport["fetches"])
                resolver.transport["failed"] = False
                self.assertIsNone(resolver.resolve([ref, dict(ref)]))
                self.assertEqual(1, resolver.transport["fetches"])
            with patch("atc_git.time.monotonic", return_value=401):
                self.assertIs(True, resolver.resolve([ref, dict(ref)]))
                self.assertIs(True, resolver.resolve([ref]))
                self.assertEqual(2, resolver.transport["fetches"])
                self.assertEqual(4, resolver.timeout)
                self.assertEqual(300, resolver.ttl)

    def test_failed_default_discovery_does_not_poison_later_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, landed, equivalent, absent = self._make_remote(root / "repo")

            class DiscoveryResolver(LocalGitResolver):
                failure_kind = None
                default_calls = 0

                def _run(self, args):
                    if "--symref" in args:
                        self.default_calls += 1
                        if self.failure_kind == "timeout":
                            raise subprocess.TimeoutExpired(args, 4)
                        if self.failure_kind == "error":
                            raise OSError("unavailable")
                        if self.failure_kind == "exit":
                            return subprocess.CompletedProcess(args, 128, "", "")
                    return super()._run(args)

            # This repository has no configured default fallback. The first
            # item's failed discovery produces no targets, but the next item's
            # distinct commit must be probed before the ordinary cache expires.
            for failure in ("timeout", "error", "exit"):
                with self.subTest(failure=failure), patch("atc_git.time.monotonic", return_value=100):
                    resolver = DiscoveryResolver(root / failure, {CLAWLINE_REMOTE: remote})
                    resolver.failure_kind = failure
                    self.assertIsNone(resolver.resolve([
                        {"remote": CLAWLINE_REMOTE, "commit": "f" * 40},
                        {"remote": CLAWLINE_REMOTE, "commit": "e" * 40},
                    ]))
                    self.assertEqual(1, resolver.default_calls)  # One attempt per check.
                    resolver.failure_kind = None
                    self.assertIs(True, resolver.resolve([{"remote": CLAWLINE_REMOTE, "commit": landed}]))
                    self.assertEqual(2, resolver.default_calls)
                    # Successful discovery still caches; real Git establishes
                    # patch equivalence and definite absence as before.
                    self.assertIs(True, resolver.resolve([{"remote": CLAWLINE_REMOTE, "commit": equivalent}]))
                    self.assertIs(False, resolver.resolve([{"remote": CLAWLINE_REMOTE, "commit": absent}]))
                    self.assertEqual(2, resolver.default_calls)
                    self.assertEqual(4, resolver.timeout)
                    self.assertEqual(300, resolver.ttl)

    def test_failed_shipping_inventory_retries_without_losing_positive_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, commits = self._make_shipping_line_remote(root / "repo")

            class DiscoveryResolver(LocalGitResolver):
                fail_heads = True
                heads_calls = 0

                def _run(self, args):
                    if "--heads" in args:
                        self.heads_calls += 1
                        if self.fail_heads:
                            raise subprocess.TimeoutExpired(args, 4)
                    return super()._run(args)

            resolver = DiscoveryResolver(root / "cache", {TIGHTBEAM_REMOTE: remote})
            with patch("atc_git.time.monotonic", return_value=100):
                # A partial inventory cannot prove absence on unlisted lines.
                self.assertIsNone(resolver.resolve([{
                    "remote": TIGHTBEAM_REMOTE, "commit": commits["0.1.14-first"],
                }]))
                # Known targets can still supply a definite proof on failure.
                self.assertIs(True, resolver.resolve([{
                    "remote": TIGHTBEAM_REMOTE, "commit": commits["default"],
                }]))
                resolver.fail_heads = False
                self.assertIs(True, resolver.resolve([{
                    "remote": TIGHTBEAM_REMOTE, "commit": commits["0.1.14-second"],
                }]))
                self.assertEqual(3, resolver.heads_calls)

    def test_successful_empty_target_discovery_stays_cached_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "empty.git"
            subprocess.run(["git", "init", "--bare", str(remote)],
                           check=True, capture_output=True, text=True)

            class DiscoveryResolver(LocalGitResolver):
                listings = 0

                def _run(self, args):
                    if "ls-remote" in args:
                        self.listings += 1
                    return super()._run(args)

            resolver = DiscoveryResolver(root / "cache", {CLAWLINE_REMOTE: remote})
            with patch("atc_git.time.monotonic", return_value=100):
                for commit in ("a" * 40, "b" * 40):
                    self.assertIsNone(resolver.resolve([{"remote": CLAWLINE_REMOTE, "commit": commit}]))
                self.assertEqual(2, resolver.listings)

    def test_fresh_registered_proof_without_stored_item_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, landed, _, _ = self._make_remote(root / "atc")
            for size in (15, 19):
                # Deleted checkout paths with no candidate still take the
                # pre-existing bounded ancestry route, without retained proof.
                refs = [{"repo": f"retired:/removed/task-{n}", "commit": landed}
                        for n in range(size)]
                resolver = LocalGitResolver(root / f"cache-{size}", {ATC_REMOTE: remote})
                self.assertIs(True, resolver.resolve(refs))
                resolver = LocalGitResolver(root / f"cache-{size}", {ATC_REMOTE: remote})
                self.assertIs(True, resolver.resolve(refs))

    def test_canonical_ancestry_patch_identity_and_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, landed, equivalent, absent = self._make_remote(root / "atc")
            release, release_remote, release_landed, release_absent = self._make_release_remote(root / "release")
            resolver = LocalGitResolver(root / "cache", {ATC_REMOTE: remote, TIGHTBEAM_REMOTE: release_remote})
            def ref(path, commit):
                return {"repo": f"{LOCAL_HOST}:{path}", "commit": commit,
                        "remote": ATC_REMOTE if path == source else TIGHTBEAM_REMOTE}
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
                root / "org-repo", origin=CLAWLINE_REMOTE, default_branch="trunk")
            resolver = LocalGitResolver(root / "cache", {CLAWLINE_REMOTE: remote})

            def ref(commit):
                return {"remote": CLAWLINE_REMOTE, "commit": commit}

            # The dynamically discovered repository default and whole-name
            # version branches are shipping lines, including future versions.
            for name in ("default", "0.4", "01.04", "0.1.14-first", "0.1.14-second", "0.2.0", "1.0.0"):
                with self.subTest(branch=name):
                    self.assertIs(True, resolver.resolve([ref(commits[name])]))

            # Branches containing code without landing on a shipping line stay
            # unmerged, even when their names contain a version number.
            for name in ("feature/change", "candidate/0.1.14", "backport/change",
                         "archive/change", "attic/change", "agent/change",
                         "codex/atc-kungfu-evidence", "0.1.14-rc.1", "release/0.1.14",
                         "0", "0.4.0.1", "0.4-rc", "release/0.4", "quarantine/0.4"):
                with self.subTest(branch=name):
                    self.assertIs(False, resolver.resolve([ref(commits[name])]))

            self.assertIsNone(resolver.resolve([ref(commits["unreachable"])]))

    def test_deleted_checkout_discovers_the_explicit_org_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, commits = self._make_shipping_line_remote(
                root / "org-repo", origin=CLAWLINE_REMOTE, default_branch="trunk")

            class TrackingResolver(LocalGitResolver):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.target_repositories = []

                def _target_refs(self, remote_name):
                    self.target_repositories.append(remote_name)
                    return super()._target_refs(remote_name)

            resolver = TrackingResolver(root / "cache", {CLAWLINE_REMOTE: remote})
            identities = (
                ("clickety-clacks/clawline", commits["default"]),
                ("github:clickety-clacks/clawline", commits["0.1.14-first"]),
                ("https://github.com/clickety-clacks/clawline.git",
                 commits["0.2.0"]),
                ("ssh://git@github.com/clickety-clacks/clawline.git",
                 commits["1.0.0"]),
                ("github.com:clickety-clacks/clawline",
                 commits["0.1.14-second"]),
                ("clickety-clacks/clawline#0.1.14",
                 commits["default"]),
            )
            for repo, commit in identities:
                with self.subTest(repo=repo):
                    self.assertIs(True, resolver.resolve([{"repo": repo, "commit": commit}]))

            # A missing checkout with an explicit repository identity probes
            # only that repository; it never broadens into the four fallbacks.
            self.assertEqual({CLAWLINE_REMOTE}, set(resolver.target_repositories))
            self.assertIs(True, resolver.resolve([{
                "repo": "retired:/checkout", "remote": "github:clickety-clacks/clawline",
                "commit": commits["0.1.14-first"],
            }]))
            self.assertEqual({CLAWLINE_REMOTE}, set(resolver.target_repositories))

            self.assertIs(False, resolver.resolve([{
                "repo": "clickety-clacks/clawline", "commit": commits["quarantine/held-work"],
            }]))
            self.assertIsNone(resolver.resolve([{
                "repo": "clickety-clacks/clawline", "commit": commits["unreachable"],
            }]))
            self.assertIsNone(resolver.resolve([{
                "repo": "https://github.com/example/clawline.git", "commit": commits["default"],
            }]))
            self.assertEqual({CLAWLINE_REMOTE}, set(resolver.target_repositories))

    def test_path_names_and_checkout_origins_cannot_nominate_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, remote, commits = self._make_shipping_line_remote(root / "repo", origin=CLAWLINE_REMOTE)
            resolver = LocalGitResolver(root / "cache", {CLAWLINE_REMOTE: remote})
            resolver.refs = {SPECS_REMOTE: ("refs/heads/main",)}
            # Even an existing checkout with a readable origin is only a path.
            for path in (f"{LOCAL_HOST}:{source}", "retired:/workspace/clawline-source"):
                with self.subTest(path=path), patch.object(resolver, "_run", wraps=resolver._run) as run:
                    self.assertIsNone(resolver.resolve([{"repo": path, "commit": commits["default"]}]))
                    commands = [call.args[0] for call in run.call_args_list]
                    self.assertFalse(any("-C" in command for command in commands))
                    self.assertFalse(any(CLAWLINE_REMOTE in command for command in commands))

    def test_origin_label_does_not_exclude_a_named_organization_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, commits = self._make_shipping_line_remote(root / "repo", origin=CLAWLINE_REMOTE)
            resolver = LocalGitResolver(root / "cache", {CLAWLINE_REMOTE: remote})
            for line, expected in (("default", True), ("0.4", True),
                                   ("quarantine/held-work", False), ("unreachable", None)):
                with self.subTest(line=line), patch.object(resolver, "_target_refs", wraps=resolver._target_refs) as targets:
                    self.assertIs(expected, resolver.resolve([{
                        "repo": "https://github.com/clickety-clacks/clawline.git",
                        "remote": "origin", "ref": "refs/heads/feature/work",
                        "commit": commits[line],
                    }]))
                    self.assertEqual([CLAWLINE_REMOTE], [call.args[0] for call in targets.call_args_list])
            self.assertIsNone(resolver.resolve([{
                "repo": "https://github.com/example/clawline.git", "remote": "origin",
                "commit": commits["default"],
            }]))
            self.assertEqual({"excluded-repository": 1}, resolver.diagnostic_summary()["reasonCounts"])
            # An actual explicit repository still takes precedence over repo.
            self.assertIsNone(resolver.resolve([{
                "repo": "https://github.com/clickety-clacks/clawline.git",
                "remote": "https://github.com/example/clawline.git", "commit": commits["default"],
            }]))

    def test_origin_label_on_a_path_uses_only_registered_or_same_item_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, commits = self._make_shipping_line_remote(root / "repo", origin=CLAWLINE_REMOTE)
            resolver = LocalGitResolver(root / "cache", {CLAWLINE_REMOTE: remote})
            resolver.refs = {}
            path = {"repo": "retired:/workspace/clawline", "remote": "origin", "commit": commits["0.4"]}
            with patch.object(resolver, "_run", wraps=resolver._run) as run:
                self.assertIsNone(resolver.resolve([path]))
                run.assert_not_called()  # No folder or checkout-origin inference.
            named = {"repo": "clickety-clacks/clawline", "remote": "origin", "commit": path["commit"]}
            for refs in ([path, named], [named, path]):
                with patch.object(resolver, "_registered_ancestry", wraps=resolver._registered_ancestry) as fallback:
                    self.assertIs(True, resolver.resolve(refs))
                    fallback.assert_not_called()
            self.assertIsNone(resolver.resolve([path]))  # No cross-item proof.
            resolver = LocalGitResolver(root / "registered", {CLAWLINE_REMOTE: remote})
            resolver.refs = {CLAWLINE_REMOTE: ("refs/heads/main",)}
            self.assertIs(True, resolver.resolve([path]))
            self.assertIsNone(resolver.resolve([{**path, "commit": commits["unreachable"]}]))
            self.assertEqual({"identity-missing": 1}, resolver.diagnostic_summary()["reasonCounts"])

    def test_same_item_named_proof_covers_exact_sha_without_cross_item_cache_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, commits = self._make_shipping_line_remote(root / "repo", origin=CLAWLINE_REMOTE)
            resolver = LocalGitResolver(root / "cache", {CLAWLINE_REMOTE: remote})
            resolver.refs = {}  # Isolate dependent proof from registry ancestry.
            path = {"repo": "retired:/generic/source", "commit": commits["0.4"]}
            named = {"repo": "github:clickety-clacks/clawline", "commit": commits["0.4"]}
            self.assertIsNone(resolver.resolve([path]))  # Cache a miss first.
            for refs in ([path, named], [named, path]):
                with patch.object(resolver, "_registered_ancestry", wraps=resolver._registered_ancestry) as fallback:
                    self.assertIs(True, resolver.resolve(refs))
                    fallback.assert_not_called()
            # A later item has the identical path but lacks the named proof.
            self.assertIsNone(resolver.resolve([path]))
            self.assertIsNone(resolver.results[(path["repo"], None, path["commit"])][1])
            # A named proof of a different SHA cannot cover the path reference.
            other_path = {**path, "commit": commits["default"]}
            with patch.object(resolver, "_registered_ancestry", wraps=resolver._registered_ancestry) as fallback:
                self.assertIs(True, resolver.resolve([other_path, named]))
                fallback.assert_called_once_with(other_path["commit"])
            self.assertEqual(1, resolver.diagnostic_ref_count)
            # Unmerged or unavailable named references are not positive proof.
            for key in ("quarantine/held-work", "unreachable"):
                with self.subTest(commit=key):
                    self.assertIsNone(resolver.resolve([
                        {**path, "commit": commits[key]}, {**named, "commit": commits[key]},
                    ]))

    def test_path_only_registered_proof_and_truthful_absence_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, remote, commits = self._make_shipping_line_remote(root / "repo", origin=SPECS_REMOTE)
            resolver = LocalGitResolver(root / "cache", {SPECS_REMOTE: remote})
            resolver.refs = {SPECS_REMOTE: ("refs/heads/main",)}
            def ref(commit):
                return {"repo": "retired:/workspace/misleading-clawline-source", "commit": commit}
            self.assertIs(True, resolver.resolve([ref(commits["0.4"])]))
            for commit in (commits["quarantine/held-work"], commits["unreachable"]):
                self.assertIsNone(resolver.resolve([ref(commit)]))
                self.assertEqual({"identity-missing": 1}, resolver.diagnostic_summary()["reasonCounts"])
            # A timeout is not evidence that all registered shipping lines lack
            # the commit, even when there is no positive proof this time.
            resolver = LocalGitResolver(root / "failed", {SPECS_REMOTE: remote}, "timeout")
            resolver.refs = {SPECS_REMOTE: ("refs/heads/main",)}
            self.assertIsNone(resolver.resolve([ref(commits["0.4"])]))
            self.assertEqual({"registered-lookup-inconclusive": 1}, resolver.diagnostic_summary()["reasonCounts"])

    def test_named_repository_does_not_infer_archive_or_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, archive, commits = self._make_shipping_line_remote(root / "archive", origin=CLAWLINE_REMOTE)
            archived = commits["0.4"]
            _, current, _, _, _ = self._make_remote(root / "current", origin=HARKLIGHT_AGENT_REMOTE)
            resolver = LocalGitResolver(root / "cache", {CLAWLINE_REMOTE: archive, HARKLIGHT_AGENT_REMOTE: current})
            with patch.object(resolver, "_target_refs", wraps=resolver._target_refs) as targets:
                self.assertIsNone(resolver.resolve([{"remote": HARKLIGHT_AGENT_REMOTE, "commit": archived}]))
                self.assertEqual([HARKLIGHT_AGENT_REMOTE], [call.args[0] for call in targets.call_args_list])

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

        names = ("0.4", "01.04", "0.1.14", "0.2.0", "1.0.0", "feature/change",
                 "candidate/0.1.14", "backport/change", "archive/change",
                 "attic/change", "agent/change", "codex/atc-kungfu-evidence",
                 "quarantine/held-work", "0.1.14-rc.1", "release/0.1.14",
                 "0", "0.4.0.1", "0.4-rc", "release/0.4", "quarantine/0.4")
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

