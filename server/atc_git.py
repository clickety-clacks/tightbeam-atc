"""Canonical Git evidence, cached in ATC's own directory."""

import hashlib
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time


CANONICAL_REFS = {
    "git@github.com:clickety-clacks/tightbeam-atc.git": ("refs/heads/main",),
    "git@github.com:clickety-clacks/tightbeam-specs.git": ("refs/heads/main",),
    "git@github.com:clickety-clacks/lachesis.git": ("refs/heads/main",),
    "git@github.com:clickety-clacks/tightbeam.git": ("refs/heads/main", "refs/heads/0.1.8"),
}


def normalize_remote(remote):
    if not isinstance(remote, str):
        return None
    remote = remote.strip().rstrip("/")
    if remote.startswith("https://github.com/"):
        remote = "git@github.com:" + remote[len("https://github.com/"):]
    if remote.startswith("git@github.com:") and not remote.endswith(".git"):
        remote += ".git"
    return remote


class GitResolver:
    def __init__(self, cache, refs=None, host_routes=None, ttl=300, timeout=4):
        self.cache = Path(cache)
        self.refs = CANONICAL_REFS if refs is None else refs
        self.host_routes = host_routes or {}
        self.ttl, self.timeout = ttl, timeout
        self.results, self.origins, self.branches = {}, {}, {}
        self.local_hosts = {socket.gethostname(), socket.gethostname().split(".", 1)[0]}

    def _run(self, args):
        return subprocess.run(args, capture_output=True, text=True, timeout=self.timeout)

    def _origin(self, reference):
        explicit = normalize_remote(reference.get("remote"))
        if explicit:
            return explicit if explicit in self.refs else False
        repo = reference.get("repo")
        cached = self.origins.get(repo)
        if cached and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        origin = None
        if isinstance(repo, str) and ":" in repo:
            host, path = repo.split(":", 1)
            if Path(path).is_absolute():
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
                    origin = found if found in self.refs else False
        self.origins[repo] = (time.monotonic(), origin)
        return origin

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

    def _probe(self, remote, branch, commit, ancestry_only=False):
        loaded = self._branch(remote, branch)
        if loaded is None:
            return None
        args, local_ref = loaded
        if self._run(args + ["cat-file", "-e", commit + "^{commit}"]).returncode:
            if ancestry_only:
                return None
            if self._run(args + ["fetch", "--quiet", remote, commit]).returncode:
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
                elif remote:
                    probes = [self._probe(remote, branch, commit) for branch in self.refs[remote]]
                else:
                    # Preserve the existing canonical-registry recovery path:
                    # prove ancestry in an explicitly registered repository.
                    # Do not infer repository identity, or absence, from a miss.
                    probes = [self._probe(r, branch, commit, ancestry_only=True)
                              for r, branches in self.refs.items() for branch in branches]
                value = True if any(p is True for p in probes) else (
                    False if probes and all(p is False for p in probes) else None)
            except (OSError, subprocess.TimeoutExpired):
                value = None
            self.results[key] = (time.monotonic(), value)
            findings.append(value)
        return True if any(v is True for v in findings) else (
            False if findings and all(v is False for v in findings) else None)
