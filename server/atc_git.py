"""Canonical Git evidence, cached in ATC's own directory."""

import hashlib
import json
from pathlib import Path
import re
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
RELEASE_LINE = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")


def is_path_reference(reference):
    return not reference.get("remote") and repository_remote(reference.get("repo")) is None


def reference_digest(references):
    """Correlate inputs without recording checkout paths or remote credentials."""
    digest = hashlib.sha256()
    for reference in references:
        digest.update(json.dumps([reference.get(k) for k in ("repo", "remote", "commit")],
                                 separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def stderr_category(text, step):
    """Return vocabulary only, never command output (which may contain secrets)."""
    text = (text or "").lower()
    if step == "origin" and "no such remote" in text:
        return "missing-origin"
    if step == "origin" and ("no such file or directory" in text or "not a git repository" in text):
        return "missing-checkout"
    if "not our ref" in text or "unadvertised object" in text:
        return "object-not-served"
    if step == "commit-object" and "not a valid object name" in text:
        return "missing-object"
    if any(s in text for s in ("authentication failed", "permission denied", "could not read username")):
        return "authentication-or-authorization"
    if any(s in text for s in ("could not resolve", "connection refused", "network is unreachable",
                               "connection timed out", "connection reset")):
        return "transport"
    return "other" if text else "empty"


def reference_identity(reference):
    # Only an exact repository spelling is safe to include. Other inputs get a
    # digest so a retained reference record can be joined without logging it.
    for key in ("remote", "repo"):
        raw = reference.get(key)
        if isinstance(raw, str):
            match = GITHUB_REPOSITORY.fullmatch(raw.strip())
            if match:
                return (match["owner"] + "/" + match["repository"])[:180]
            match = ORG_REPOSITORY.fullmatch(raw.strip())
            if match:
                return ("clickety-clacks/" + match["repository"])[:180]
    return None


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


class GitResolver:
    def __init__(self, cache, refs=None, host_routes=None, ttl=300, timeout=4):
        self.cache = Path(cache)
        self.refs = CANONICAL_REFS if refs is None else refs
        self.host_routes = host_routes or {}
        self.ttl, self.timeout = ttl, timeout
        self.results, self.origins, self.branches, self.targets = {}, {}, {}, {}
        self._failed_targets = {}
        self.clear_diagnostics()

    def _run(self, args):
        return subprocess.run(args, capture_output=True, text=True, timeout=self.timeout)

    def clear_diagnostics(self):
        # Keep at most four deciding ref summaries for this check only. Never
        # retain diagnostics in the lookup caches or persisted merge evidence.
        self.diagnostics = []
        self.diagnostic_ref_count = 0
        self.diagnostic_reasons = {}
        self.diagnostic_failures = {}
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
            if step == "origin-selected":
                ref["origin"] = fields.get("remote")
        elif step == "raw-fetch":
            ref["rawFetchAttempts"] += 1
        elif step == "raw-fetch-skipped":
            ref["rawFetchSkipped"] = fields["reason"]
        elif step == "fallback":
            ref["fallback"] = fields["route"]
        elif step.endswith("-cache"):
            ref["cache"] = step
            ref["cacheDetail"] = {k: v for k, v in fields.items()
                                  if k in {"ageMs", "available", "complete", "count", "value"}}
        elif step == "identity":
            ref["identityRoute"] = fields["route"]
        elif step == "targets":
            ref["targets"] = {"remote": fields["remote"], "count": fields["count"],
                              "complete": fields["complete"]}
        elif step == "resolution":
            ref["reason"] = fields["reason"]
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
            reason = self._diagnostic_ref.get("reason", "interrupted-reference")
            self._diagnostic_ref["reason"] = reason
            self.diagnostic_reasons[reason] = self.diagnostic_reasons.get(reason, 0) + 1
            failure = self._diagnostic_ref.get("failure")
            if failure:
                step = failure["step"]  # Fixed command/decision vocabulary, never output.
                self.diagnostic_failures[step] = self.diagnostic_failures.get(step, 0) + 1
            self.diagnostic_ref_count += 1
            if len(self.diagnostics) < 4:
                self.diagnostics.append(self._diagnostic_ref)
        self._diagnostic_ref = None

    def diagnostic_summary(self):
        # An unexpected worker exception can interrupt the current reference.
        refs = self.diagnostics + ([self._diagnostic_ref] if self._diagnostic_ref else [])
        count = self.diagnostic_ref_count + bool(self._diagnostic_ref)
        reasons = dict(self.diagnostic_reasons)
        if self._diagnostic_ref:
            reasons["interrupted-reference"] = reasons.get("interrupted-reference", 0) + 1
        return {"refs": refs[:4], "omittedRefs": max(0, count - 4), "reasonCounts": reasons,
                "failureCounts": dict(self.diagnostic_failures)}

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
                       exitCode=result.returncode,
                       stderrCategory=stderr_category(result.stderr, step))
        return result

    def _origin(self, reference):
        explicit_value = reference.get("remote")
        explicit = normalize_remote(explicit_value)
        if explicit:
            self._note("identity", route="explicit-remote")
            if explicit in self.refs or is_org_remote(explicit):
                return explicit
            named = repository_remote(explicit_value)
            return named if named is not None else False
        repo = reference.get("repo")
        cached = self.origins.get(repo)
        if cached and time.monotonic() - cached[0] < self.ttl:
            self._note("identity", route="cached-origin")
            self._note("origin-cache", remote=cached[1],
                       ageMs=round((time.monotonic() - cached[0]) * 1000, 3))
            return cached[1]
        origin = repository_remote(repo)
        self._note("identity", route="named-repository" if origin is not None else "missing-identity")
        self.origins[repo] = (time.monotonic(), origin)
        return origin

    def _target_refs(self, remote):
        cached = self.targets.get(remote)
        if cached and time.monotonic() - cached[0] < self.ttl:
            self._note("targets-cache", remote=remote, complete=cached[1][1], count=len(cached[1][0]),
                       ageMs=round((time.monotonic() - cached[0]) * 1000, 3))
            return cached[1]
        if remote in self._failed_targets:
            self._note("targets-cache", remote=remote,
                       complete=self._failed_targets[remote][1])
            return self._failed_targets[remote]

        default_ref = None
        default_succeeded = False
        heads = set()
        heads_complete = False
        try:
            default = self._command("default-branch", ["git", "ls-remote", "--symref", remote, "HEAD"], remote=remote)
            if default.returncode == 0:
                default_succeeded = True
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
        if default_succeeded and heads_complete:
            self.targets[remote] = (time.monotonic(), result)
        else:
            # Reuse failed discovery only within this check. A later item must
            # not inherit a transient failure as a five-minute empty inventory.
            self._failed_targets[remote] = result
        return result

    def _branch(self, remote, branch):
        key = (remote, branch)
        cached = self.branches.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            self._note("branch-cache", remote=remote, branch=branch, available=cached[1] is not None,
                       ageMs=round((time.monotonic() - cached[0]) * 1000, 3))
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
        obj = self._command("commit-object", args + ["cat-file", "-e", commit + "^{commit}"], remote=remote, branch=branch)
        if obj.returncode:
            if ancestry_only or not allow_commit_fetch:
                self._note("raw-fetch-skipped", remote=remote, branch=branch,
                           reason="ancestry-only" if ancestry_only else "allowance-exhausted")
                if ancestry_only and len(commit) in {40, 64} and stderr_category(obj.stderr, "commit-object") == "missing-object":
                    return False  # Not in this successfully fetched shipping line.
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
            return False
        # Preserve patch-equivalence evidence for cherry-picked/rebased work.
        started = time.monotonic()
        equivalent = self._command("patch-equivalence", args + ["cherry", local_ref, commit, commit + "~1"], remote=remote, branch=branch)
        if equivalent.returncode != 0:
            return None
        lines = equivalent.stdout.splitlines()
        if len(lines) != 1 or lines[0][:1] not in {"-", "+"}:
            self._note("patch-equivalence-output", remote=remote, branch=branch,
                       error="UnexpectedOutput", command="git",
                       elapsedMs=round((time.monotonic() - started) * 1000, 3), outputShape={
                           "lines": len(lines), "plus": sum(s.startswith("+") for s in lines),
                           "minus": sum(s.startswith("-") for s in lines),
                           "other": sum(not s.startswith(("+", "-")) for s in lines)})
            return None
        return lines[0].startswith("-")

    def _registered_ancestry(self, commit):
        """Use known repositories only for positive ancestry recovery."""
        self._note("fallback", route="registered-ancestry")
        probes = []
        complete = True
        for registered in self.refs:
            target_refs, inventory_complete = self._target_refs(registered)
            complete = complete and inventory_complete
            for branch in target_refs:
                try:
                    probes.append(self._probe(
                        registered, branch, commit, ancestry_only=True,
                        allow_commit_fetch=False))
                except (OSError, subprocess.TimeoutExpired):
                    probes.append(None)
        self._note("resolution", reason=("identity-missing" if complete and probes
                   and all(probe is False for probe in probes) else "registered-lookup-inconclusive"))
        return True if any(probe is True for probe in probes) else None

    def resolve(self, references):
        self.clear_diagnostics()
        self._failed_targets.clear()
        findings = []
        attempted_commit_fetches = set()
        # A proof belongs to this item and exact object only. Named refs run
        # first so input order cannot make a path miss the matching proof.
        named_proofs = set()
        ordered = sorted(enumerate(references), key=lambda pair: is_path_reference(pair[1]))
        for index, reference in ordered:
            commit = reference.get("commit")
            self._diagnostic_ref = {"index": index, "commit": commit if isinstance(commit, str)
                                    and re.fullmatch(r"[0-9a-f]{7,64}", commit) else "<invalid>",
                                    "failure": None, "rawFetchAttempts": 0,
                                    "rawFetchAllowance": "not-reached", "fallback": None,
                                    "cache": None, "inputDigest": reference_digest([reference]),
                                    "repository": reference_identity(reference)}
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
                self._note("invalid-commit", error="InvalidCommit")
                self._note("resolution", reason="invalid-commit")
                self._finish_diagnostic(None)
                findings.append(None)
                continue
            path_only = is_path_reference(reference)
            if path_only and len(commit) in {40, 64} and commit in named_proofs:
                self._note("identity", route="same-item-named-proof")
                self._finish_diagnostic(True)
                findings.append(True)
                # Never cache this dependent proof under the path: another item
                # may contain that path without the named reference.
                continue
            key = (reference.get("repo"), reference.get("remote"), commit)
            cached = self.results.get(key)
            if cached and time.monotonic() - cached[0] < self.ttl:
                self._note("result-cache", value=cached[1],
                           ageMs=round((time.monotonic() - cached[0]) * 1000, 3))
                self._note("resolution", reason="cached-inconclusive")
                self._finish_diagnostic(cached[1])
                findings.append(cached[1])
                if cached[1] is True and not path_only:
                    named_proofs.add(commit)
                continue
            try:
                remote = self._origin(reference)
                self._note("origin-selected", remote=remote, available=bool(remote))
                if remote is False:
                    value = None
                    self._note("resolution", reason="excluded-repository")
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
                    self._note("resolution", reason=("no-targets" if not target_refs else
                               "incomplete-target-inventory" if not complete else "inconclusive-probe"))
                else:
                    # Paths cannot nominate repositories. Only the deployment
                    # registry can recover a proof without a same-item named ref.
                    value = self._registered_ancestry(commit)
            except (OSError, subprocess.TimeoutExpired) as error:
                self._note("reference-error", error=type(error).__name__[:80])
                self._note("resolution", reason="reference-error")
                value = None
            self.results[key] = (time.monotonic(), value)
            self._finish_diagnostic(value)
            findings.append(value)
            if value is True and not path_only:
                named_proofs.add(commit)
        self._failed_targets.clear()
        return True if any(v is True for v in findings) else (
            False if findings and all(v is False for v in findings) else None)
