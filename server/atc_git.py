"""Canonical Git evidence, cached in ATC's own directory."""

import hashlib
from pathlib import Path, PurePosixPath
import re
import shlex
import socket
import subprocess
import time


# Known default branches are fallbacks for repositories whose symbolic HEAD
# cannot be read. Release-line branches are discovered from advertised refs.
CANONICAL_REFS = {
    "git@github.com:clickety-clacks/tightbeam-atc.git": ("refs/heads/main",),
    "git@github.com:clickety-clacks/tightbeam-specs.git": ("refs/heads/main",),
    "git@github.com:clickety-clacks/lachesis.git": ("refs/heads/main",),
    "git@github.com:clickety-clacks/tightbeam.git": ("refs/heads/main",),
}

ORG_REMOTE = re.compile(r"git@github\.com:clickety-clacks/[A-Za-z0-9_.-]+\.git\Z", re.IGNORECASE)
GITHUB_REPOSITORY = re.compile(
    r"(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:|"
    r"github\.com[:/]|github:)(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repository>[A-Za-z0-9_.-]+?)(?:\.git)?\Z",
    re.IGNORECASE,
)
ORG_REPOSITORY = re.compile(
    r"clickety-clacks/(?P<repository>[A-Za-z0-9_.-]+?)(?:\.git)?\Z",
    re.IGNORECASE,
)
# Bounded vocabulary for matching checkout directory names to public
# organization repositories. Explicit repo/remote fields remain unrestricted.
PUBLIC_ORG_REPOSITORIES = frozenset({
    "agent-window-resolver", "agentd", "agentd-hub", "argus", "clawdbot",
    "clawline", "clipmesh", "engram", "ghostty", "gooarchy",
    "gooarchy-flavorings", "harklight-agent", "harklight-app",
    "harklight-server", "homebrew-tap", "lachesis", "lachesis-omarchy",
    "omarchy-ask", "omarchy-key-visualizer", "omarchy-nasa2043-theme",
    "omarchy-subspace", "omarchy-window-panel", "pimcamp", "scottland",
    "seldon", "subspace", "subspace-daemon", "surf-ace", "swiftterm",
    "tightbeam", "tightbeam-atc", "tightbeam-chat", "tightbeam-decisions-omarchy",
    "tightbeam-design", "tightbeam-specs", "tightbeam-tuner",
    "tightbeam-workitem-pies", "vimarchy", "xdg-desktop-portal-wlr", "yoohoo",
})
RELEASE_LINE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)


def normalize_remote(remote):
    if not isinstance(remote, str):
        return None
    remote = remote.strip().rstrip("/")
    if remote.startswith("https://github.com/"):
        remote = "git@github.com:" + remote[len("https://github.com/"):]
    if remote.startswith("git@github.com:") and not remote.endswith(".git"):
        remote += ".git"
    return remote


def is_org_remote(remote):
    return isinstance(remote, str) and ORG_REMOTE.fullmatch(remote) is not None


def repository_remote(identity):
    """Return the named organization remote, False for other GitHub repos."""
    if not isinstance(identity, str):
        return None
    identity = identity.strip().split("#", 1)[0].rstrip("/")
    match = ORG_REPOSITORY.fullmatch(identity)
    if match:
        return f"git@github.com:clickety-clacks/{match['repository']}.git"
    match = GITHUB_REPOSITORY.fullmatch(identity)
    if not match:
        return None
    if match["owner"].casefold() != "clickety-clacks":
        return False
    return f"git@github.com:clickety-clacks/{match['repository']}.git"


def repository_remote_from_checkout(repo):
    """Return the longest public org slug candidate in a checkout basename."""
    if not isinstance(repo, str) or ":" not in repo:
        return None
    _, path = repo.split(":", 1)
    if not path.startswith("/"):
        return None
    checkout = PurePosixPath(path.rstrip("/")).name.casefold()
    matches = [name for name in PUBLIC_ORG_REPOSITORIES
               if checkout == name or checkout.startswith(name + "-")]
    if not matches:
        return None
    repository = max(matches, key=len)
    return f"git@github.com:clickety-clacks/{repository}.git"


class GitResolver:
    def __init__(self, cache, refs=None, host_routes=None, ttl=300, timeout=4):
        self.cache = Path(cache)
        self.refs = CANONICAL_REFS if refs is None else refs
        self.host_routes = host_routes or {}
        self.ttl, self.timeout = ttl, timeout
        self.results, self.origins, self.branches, self.targets = {}, {}, {}, {}
        self.clear_diagnostics()
        self.local_hosts = {socket.gethostname(), socket.gethostname().split(".", 1)[0]}

    def _run(self, args):
        return subprocess.run(args, capture_output=True, text=True, timeout=self.timeout)

    def clear_diagnostics(self):
        # Keep at most four deciding ref summaries for this check only. Never
        # retain diagnostics in the lookup caches or persisted merge evidence.
        self.diagnostics = []
        self.diagnostic_ref_count = 0
        self._diagnostic_ref = None

    def _note(self, step, **fields):
        ref = self._diagnostic_ref
        if ref is None:
            return
        # Never capture argv, paths, command output or exception messages.
        remote = fields.get("remote")
        if remote is not None:
            fields["remote"] = remote[:180] if is_org_remote(remote) else "<other>"
        branch = fields.get("branch")
        if branch is not None:
            fields["branch"] = (branch[:180] if re.fullmatch(
                r"refs/heads/[A-Za-z0-9._/-]+", branch) else "<other>")
        if step in {"origin-selected", "probe"}:
            ref.update({k: fields[k] for k in ("remote", "branch", "rawFetchAllowance")
                        if k in fields})
        elif step == "raw-fetch":
            ref["rawFetchAttempts"] += 1
        elif step == "raw-fetch-skipped":
            ref["rawFetchSkipped"] = fields["reason"]
        elif step == "fallback":
            ref["fallback"] = fields["route"]
        elif step.endswith("-cache"):
            ref["cache"] = step
        if step == "commit-object" and fields.get("exitCode"):
            ref["objectMissing"] = True
        # A missing object or non-ancestor is a deciding fact, not a failed
        # command. Prefer a canonical probe failure to a missing checkout.
        failed = "error" in fields or (
            fields.get("exitCode", 0) != 0 and step != "commit-object"
            and not (step == "ancestry" and fields["exitCode"] == 1))
        previous = ref.get("failure")
        if failed and (previous is None or (previous["step"] == "origin"
                                           and fields.get("remote") is not None)):
            ref["failure"] = {"step": step, **fields}

    def _finish_diagnostic(self, value):
        if value is None:
            self.diagnostic_ref_count += 1
            if len(self.diagnostics) < 4:
                self.diagnostics.append(self._diagnostic_ref)
        self._diagnostic_ref = None

    def diagnostic_summary(self):
        # An unexpected worker exception can interrupt the current reference.
        refs = self.diagnostics + ([self._diagnostic_ref] if self._diagnostic_ref else [])
        count = self.diagnostic_ref_count + bool(self._diagnostic_ref)
        return {"refs": refs[:4], "omittedRefs": max(0, count - 4)}

    def _command(self, step, args, **context):
        started = time.monotonic()
        command = "routed-ssh/git" if args[0] == "ssh" else "git"
        try:
            result = self._run(args)
        except Exception as error:
            self._note(step, command=command, **context,
                       elapsedMs=round((time.monotonic() - started) * 1000, 3),
                       error=type(error).__name__[:80],
                       timeout=isinstance(error, subprocess.TimeoutExpired))
            raise
        if result.returncode or step == "raw-fetch":
            self._note(step, command=command, **context,
                       elapsedMs=round((time.monotonic() - started) * 1000, 3),
                       exitCode=result.returncode)
        return result

    def _origin(self, reference):
        explicit_value = reference.get("remote")
        explicit = normalize_remote(explicit_value)
        if explicit:
            if explicit in self.refs or is_org_remote(explicit):
                return explicit
            named = repository_remote(explicit_value)
            return named if named is not None else False
        repo = reference.get("repo")
        cached = self.origins.get(repo)
        if cached and time.monotonic() - cached[0] < self.ttl:
            self._note("origin-cache", remote=cached[1])
            return cached[1]
        origin = repository_remote(repo)
        if isinstance(repo, str) and ":" in repo:
            host, path = repo.split(":", 1)
            if origin is None and Path(path).is_absolute():
                command = ["git", "-C", path, "remote", "get-url", "origin"]
                if host in self.local_hosts:
                    result = self._command("origin", command)
                elif host in self.host_routes:
                    result = self._command("origin", ["ssh", "-o", "BatchMode=yes",
                                        self.host_routes[host], shlex.join(command)])
                else:
                    result = None
                if result is not None and result.returncode == 0:
                    found = normalize_remote(result.stdout.strip())
                    origin = found if found in self.refs or is_org_remote(found) else False
            if origin is None:
                origin = repository_remote_from_checkout(repo)
        self.origins[repo] = (time.monotonic(), origin)
        return origin

    def _target_refs(self, remote):
        cached = self.targets.get(remote)
        if cached and time.monotonic() - cached[0] < self.ttl:
            self._note("targets-cache", remote=remote, complete=cached[1][1])
            return cached[1]

        default_ref = None
        heads = set()
        heads_complete = False
        try:
            default = self._command("default-branch", ["git", "ls-remote", "--symref", remote, "HEAD"], remote=remote)
            if default.returncode == 0:
                for line in default.stdout.splitlines():
                    if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                        default_ref = line.split("\t", 1)[0][5:]
                        break
        except (OSError, subprocess.TimeoutExpired):
            pass

        if not default_ref:
            known_defaults = self.refs.get(remote, ())
            default_ref = known_defaults[0] if known_defaults else None

        try:
            advertised = self._command("shipping-lines", ["git", "ls-remote", "--heads", remote], remote=remote)
            if advertised.returncode == 0:
                heads_complete = True
                for line in advertised.stdout.splitlines():
                    _, separator, branch = line.partition("\t")
                    if separator and branch.startswith("refs/heads/"):
                        heads.add(branch)
        except (OSError, subprocess.TimeoutExpired):
            pass

        version_refs = sorted(
            branch for branch in heads
            if RELEASE_LINE.fullmatch(branch.removeprefix("refs/heads/"))
        )
        refs = tuple(dict.fromkeys(([default_ref] if default_ref else []) + version_refs))
        complete = heads_complete and default_ref is not None and default_ref in heads
        result = (refs, complete)
        self._note("targets", remote=remote, complete=complete, count=len(refs))
        self.targets[remote] = (time.monotonic(), result)
        return result

    def _branch(self, remote, branch):
        key = (remote, branch)
        cached = self.branches.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            self._note("branch-cache", remote=remote, branch=branch, available=cached[1] is not None)
            return cached[1]
        directory = self.cache / (hashlib.sha256(remote.encode()).hexdigest()[:20] + ".git")
        directory.parent.mkdir(parents=True, exist_ok=True)
        if not directory.exists():
            if self._command("cache-init", ["git", "init", "--bare", "--quiet", str(directory)], remote=remote, branch=branch).returncode:
                return None
        local_ref = "refs/atc/" + hashlib.sha256(branch.encode()).hexdigest()[:20]
        args = ["git", "--git-dir", str(directory)]
        try:
            fetched = self._command("branch-fetch", args + ["fetch", "--quiet", "--force", remote, branch + ":" + local_ref], remote=remote, branch=branch)
            value = (args, local_ref) if fetched.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            value = None
        self.branches[key] = (time.monotonic(), value)
        return value

    def _probe(self, remote, branch, commit, ancestry_only=False,
               allow_commit_fetch=True):
        self._note("probe", remote=remote, branch=branch, ancestryOnly=ancestry_only,
                   rawFetchAllowance=("disabled-ancestry-only" if ancestry_only else
                                      "available" if allow_commit_fetch else "exhausted"))
        loaded = self._branch(remote, branch)
        if loaded is None:
            return None
        args, local_ref = loaded
        if self._command("commit-object", args + ["cat-file", "-e", commit + "^{commit}"], remote=remote, branch=branch).returncode:
            if ancestry_only or not allow_commit_fetch:
                self._note("raw-fetch-skipped", remote=remote, branch=branch,
                           reason="ancestry-only" if ancestry_only else "allowance-exhausted")
                return None
            try:
                fetched = self._command("raw-fetch", args + ["fetch", "--quiet", remote, commit], remote=remote, branch=branch)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if fetched.returncode:
                return None
        ancestry = self._command("ancestry", args + ["merge-base", "--is-ancestor", commit, local_ref], remote=remote, branch=branch)
        if ancestry.returncode == 0:
            return True
        if ancestry.returncode != 1:
            return None
        if ancestry_only:
            return None
        # Preserve patch-equivalence evidence for cherry-picked/rebased work.
        equivalent = self._command("patch-equivalence", args + ["cherry", local_ref, commit, commit + "~1"], remote=remote, branch=branch)
        if equivalent.returncode != 0:
            return None
        lines = equivalent.stdout.splitlines()
        if len(lines) != 1 or lines[0][:1] not in {"-", "+"}:
            self._note("patch-equivalence-output", remote=remote, branch=branch,
                       error="UnexpectedOutput")
            return None
        return lines[0].startswith("-")

    def _registered_ancestry(self, commit):
        """Use known repositories only for positive ancestry recovery."""
        self._note("fallback", route="registered-ancestry")
        probes = []
        for registered in self.refs:
            target_refs, _ = self._target_refs(registered)
            for branch in target_refs:
                try:
                    probes.append(self._probe(
                        registered, branch, commit, ancestry_only=True,
                        allow_commit_fetch=False))
                except (OSError, subprocess.TimeoutExpired):
                    probes.append(None)
        return True if any(probe is True for probe in probes) else None

    def resolve(self, references):
        self.clear_diagnostics()
        findings = []
        attempted_commit_fetches = set()
        for index, reference in enumerate(references):
            commit = reference.get("commit")
            self._diagnostic_ref = {"index": index, "commit": commit if isinstance(commit, str)
                                    and re.fullmatch(r"[0-9a-f]{7,64}", commit) else "<invalid>",
                                    "failure": None, "rawFetchAttempts": 0,
                                    "rawFetchAllowance": "not-reached", "fallback": None,
                                    "cache": None}
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
                self._note("invalid-commit", error="InvalidCommit")
                self._finish_diagnostic(None)
                findings.append(None)
                continue
            key = (reference.get("repo"), reference.get("remote"), commit)
            cached = self.results.get(key)
            if cached and time.monotonic() - cached[0] < self.ttl:
                self._note("result-cache", value=cached[1],
                           ageMs=round((time.monotonic() - cached[0]) * 1000, 3))
                self._finish_diagnostic(cached[1])
                findings.append(cached[1])
                continue
            try:
                repo_identity = reference.get("repo")
                path_candidate = None
                if not reference.get("remote") and repository_remote(repo_identity) is None:
                    path_candidate = repository_remote_from_checkout(repo_identity)
                remote = self._origin(reference)
                self._note("origin-selected", remote=remote,
                           checkoutCandidate=bool(remote) and path_candidate == remote,
                           available=bool(remote))
                if remote is False:
                    value = None
                elif remote:
                    target_refs, complete = self._target_refs(remote)
                    probes = []
                    for branch in target_refs:
                        fetch_key = (remote, commit)
                        allow_fetch = fetch_key not in attempted_commit_fetches
                        attempted_commit_fetches.add(fetch_key)
                        try:
                            probes.append(self._probe(
                                remote, branch, commit, allow_commit_fetch=allow_fetch))
                        except (OSError, subprocess.TimeoutExpired):
                            # A failed branch probe is incomplete evidence for
                            # that branch. Keep checking other shipping lines.
                            probes.append(None)
                    value = True if any(p is True for p in probes) else (
                        False if complete and probes and all(p is False for p in probes) else None)
                    # A checkout basename only nominates a candidate repo. If
                    # Git cannot establish an answer there, retain the legacy
                    # bounded positive-only proof across registered repos.
                    if path_candidate == remote and value is None:
                        value = self._registered_ancestry(commit)
                else:
                    # A missing checkout cannot identify its repository. Keep
                    # the existing registered-repository recovery path: only
                    # a positive ancestry proof may recover that identity.
                    value = self._registered_ancestry(commit)
            except (OSError, subprocess.TimeoutExpired) as error:
                self._note("reference-error", error=type(error).__name__[:80])
                value = None
            self.results[key] = (time.monotonic(), value)
            self._finish_diagnostic(value)
            findings.append(value)
        return True if any(v is True for v in findings) else (
            False if findings and all(v is False for v in findings) else None)
