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
    """Infer only the repository slug encoded by a standard `*-source` path."""
    if not isinstance(repo, str) or ":" not in repo:
        return None
    _, path = repo.split(":", 1)
    if not path.startswith("/"):
        return None
    checkout = PurePosixPath(path.rstrip("/")).name
    suffix = "-source"
    if not checkout.endswith(suffix):
        return None
    repository = checkout[:-len(suffix)]
    if not repository or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
        return None
    return f"git@github.com:clickety-clacks/{repository}.git"


class GitResolver:
    def __init__(self, cache, refs=None, host_routes=None, ttl=300, timeout=4):
        self.cache = Path(cache)
        self.refs = CANONICAL_REFS if refs is None else refs
        self.host_routes = host_routes or {}
        self.ttl, self.timeout = ttl, timeout
        self.results, self.origins, self.branches, self.targets = {}, {}, {}, {}
        self.local_hosts = {socket.gethostname(), socket.gethostname().split(".", 1)[0]}

    def _run(self, args):
        return subprocess.run(args, capture_output=True, text=True, timeout=self.timeout)

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
            return cached[1]
        origin = repository_remote(repo)
        if isinstance(repo, str) and ":" in repo:
            host, path = repo.split(":", 1)
            if origin is None and Path(path).is_absolute():
                command = ["git", "-C", path, "remote", "get-url", "origin"]
                if host in self.local_hosts:
                    result = self._run(command)
                elif host in self.host_routes:
                    result = self._run(["ssh", "-o", "BatchMode=yes",
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
            return cached[1]

        default_ref = None
        heads = set()
        heads_complete = False
        try:
            default = self._run(["git", "ls-remote", "--symref", remote, "HEAD"])
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
            advertised = self._run(["git", "ls-remote", "--heads", remote])
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
        self.targets[remote] = (time.monotonic(), result)
        return result

    def _branch(self, remote, branch):
        key = (remote, branch)
        cached = self.branches.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        directory = self.cache / (hashlib.sha256(remote.encode()).hexdigest()[:20] + ".git")
        directory.parent.mkdir(parents=True, exist_ok=True)
        if not directory.exists():
            if self._run(["git", "init", "--bare", "--quiet", str(directory)]).returncode:
                return None
        local_ref = "refs/atc/" + hashlib.sha256(branch.encode()).hexdigest()[:20]
        args = ["git", "--git-dir", str(directory)]
        try:
            fetched = self._run(args + ["fetch", "--quiet", "--force", remote, branch + ":" + local_ref])
            value = (args, local_ref) if fetched.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            value = None
        self.branches[key] = (time.monotonic(), value)
        return value

    def _probe(self, remote, branch, commit, ancestry_only=False,
               allow_commit_fetch=True):
        loaded = self._branch(remote, branch)
        if loaded is None:
            return None
        args, local_ref = loaded
        if self._run(args + ["cat-file", "-e", commit + "^{commit}"]).returncode:
            if ancestry_only or not allow_commit_fetch:
                return None
            try:
                fetched = self._run(args + ["fetch", "--quiet", remote, commit])
            except (OSError, subprocess.TimeoutExpired):
                return None
            if fetched.returncode:
                return None
        ancestry = self._run(args + ["merge-base", "--is-ancestor", commit, local_ref])
        if ancestry.returncode == 0:
            return True
        if ancestry.returncode != 1:
            return None
        if ancestry_only:
            return None
        # Preserve patch-equivalence evidence for cherry-picked/rebased work.
        equivalent = self._run(args + ["cherry", local_ref, commit, commit + "~1"])
        if equivalent.returncode != 0:
            return None
        lines = equivalent.stdout.splitlines()
        if len(lines) != 1 or lines[0][:1] not in {"-", "+"}:
            return None
        return lines[0].startswith("-")

    def resolve(self, references):
        findings = []
        attempted_commit_fetches = set()
        for reference in references:
            commit = reference.get("commit")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
                findings.append(None)
                continue
            key = (reference.get("repo"), reference.get("remote"), commit)
            cached = self.results.get(key)
            if cached and time.monotonic() - cached[0] < self.ttl:
                findings.append(cached[1])
                continue
            try:
                remote = self._origin(reference)
                if remote is False:
                    probes = []
                    complete = False
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
                else:
                    # A missing checkout cannot identify its repository. Keep
                    # the existing registered-repository recovery path: only
                    # a positive ancestry proof may recover that identity.
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
                    complete = False
                value = True if any(p is True for p in probes) else (
                    False if complete and probes and all(p is False for p in probes) else None)
            except (OSError, subprocess.TimeoutExpired):
                value = None
            self.results[key] = (time.monotonic(), value)
            findings.append(value)
        return True if any(v is True for v in findings) else (
            False if findings and all(v is False for v in findings) else None)
