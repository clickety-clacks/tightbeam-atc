"""Published Tightbeam reads. Credentials stay in this service."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def now_ms():
    return time.time_ns() // 1_000_000


@dataclass
class Read:
    rows: list
    complete: bool
    at: int
    source: str
    error: str | None = None
    page: int | None = None


# These are the evidence fields shared by the published attest read and feed.
# Artifact metadata remains a separate read; it is not silently lost when a
# feed projection omits the read command's extra metadata fields.
ATTEST_FIELDS = (
    "id", "assignmentId", "kind", "verdictKind", "note", "bySession", "byUser",
    "producer", "producerCommand", "byHarness", "byProvider", "commitRefs", "ts",
)


def attest_row(row):
    return {key: row.get(key) for key in ATTEST_FIELDS}


class PackagedCLI:
    """Read-only CLI bridge and its existing provisioned operator login."""

    def __init__(self, executable, base_dir, user):
        if not Path(executable).is_absolute() or not Path(base_dir).is_absolute():
            raise ValueError("CLI executable and target base must be absolute paths")
        if not user:
            raise ValueError("CLI reads require an explicit user")
        self.executable, self.base_dir = executable, base_dir
        self.user = user

    def operator_token(self):
        # Consume the same provisioned login as the packaged CLI, in place at
        # runtime. Only the path and user are configuration; no token is copied
        # into the service environment or passed through a command invocation.
        with (Path(self.base_dir) / "gateway.json").open() as source:
            return json.load(source)["cliToken"]

    def read(self, verb, **params):
        if verb == "assignments" and params == {"state": "all"}:
            args = ["assignments", "--state", "all"]
        elif verb in {"attests", "assignment-get"} and set(params) == {"assignment_id"}:
            key = params["assignment_id"]
            if not isinstance(key, str) or not key or key.startswith("-"):
                raise ValueError("read requires an assignment identity")
            args = [verb, key]
        elif verb == "role-list" and not params:
            # There is no packaged `role-list` verb. `list` returns the
            # complete roles array through the published inspect command.
            args = ["list"]
        elif verb == "artifacts" and not params:
            args = ["artifacts"]
        else:
            raise ValueError("unsupported CLI read")
        env = dict(os.environ)
        # CLI discovery prefers a session in cwd, then explicit endpoint env,
        # then the configured base. Select this base without reading its token.
        for key in ("TIGHTBEAM_URL", "TIGHTBEAM_TOKEN"):
            env.pop(key, None)
        env["TIGHTBEAM_BASE_DIR"] = self.base_dir
        result = subprocess.run(
            [self.executable, *args, "--as-user", self.user], cwd="/", env=env,
            capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            # Do not surface CLI stdout/stderr, which may include diagnostics.
            raise ValueError("packaged CLI read failed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("error") or value.get("ok") is False:
            raise ValueError("invalid packaged CLI result")
        return value


class Gateway:
    def __init__(self, url, cli):
        self.url = url.rstrip("/")
        self.cli = cli
        self.read_count = 0
        self.count_lock = threading.Lock()

    def request(self, path):
        headers = {"Authorization": "Bearer " + self.cli.operator_token()}
        path += ("&" if "?" in path else "?") + urlencode({"asUser": self.cli.user})
        with self.count_lock:
            self.read_count += 1
        with urlopen(Request(self.url + path, headers=headers),
                     timeout=30) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError("upstream read failed")
        return result

    def version(self):
        result = self.request("/version")
        return {key: result[key] for key in ("version", "build", "sha")}

    def authenticate_feed(self, connection):
        connection.send(json.dumps({"type": "auth", "token": self.cli.operator_token(),
                                    "asUser": self.cli.user}))

    def command(self, verb, **params):
        # This closed set is the adapter's read interface, not a generic
        # command proxy available to viewers.
        if verb not in {"assignments", "attests", "role-list", "assignment-get", "artifacts"}:
            raise ValueError("unsupported read")
        with self.count_lock:
            self.read_count += 1
        return self.cli.read(verb, **params)

    def collection(self, path):
        rows, cursor, seen, page_number = [], None, set(), 0
        try:
            while True:
                page_number += 1
                query = {"limit": 500}
                if cursor is not None:
                    query["after"] = cursor
                result = self.request(path + "?" + urlencode(query))
                if result.get("schemaVersion") != 1:
                    raise ValueError("unsupported collection schema")
                items, page = result["items"], result["page"]
                if not isinstance(items, list) or not all(isinstance(r, dict) for r in items):
                    raise ValueError("invalid collection rows")
                rows.extend(items)
                if cursor is None and page["hasMoreBefore"] is not False:
                    raise ValueError("collection did not start at head")
                if page["hasMoreAfter"] is False:
                    # Starting at the head and walking forward is complete;
                    # a server unexpectedly returning a tail is not.
                    return Read(rows, True, now_ms(), path, page=page_number)
                if page["hasMoreAfter"] is not True:
                    raise ValueError("invalid page completion")
                cursor = page["newestCursor"]
                if not cursor or cursor in seen:
                    raise ValueError("collection cursor did not advance")
                seen.add(cursor)
        except Exception as error:
            # Keep partial pages, but never expose response bodies or tokens.
            return Read(rows, False, now_ms(), path, type(error).__name__, page_number)

    def read(self, resource, scope=None):
        if resource in {"work-items", "sessions", "decision-requests"}:
            return self.collection("/api/" + resource)
        if resource in {"turns", "wakes", "messages"}:
            return self.collection("/api/sessions/" + quote(scope, safe="") + "/" + resource)
        source = {
            "assignments": "cli:assignments --state all",
            "attests": "cli:attests " + str(scope),
            "roles": "cli:list.roles",
            "artifacts": "cli:artifacts",
        }.get(resource, resource)
        try:
            if resource == "assignments":
                rows = self.command("assignments", state="all")["assignments"]
            elif resource == "attests":
                rows = [attest_row(r) for r in self.command(
                    "attests", assignment_id=scope)["attests"]]
            elif resource == "roles":
                rows = self.command("role-list")["roles"]
            elif resource == "artifacts":
                rows = self.command("artifacts")["artifacts"]
            else:
                raise ValueError("unsupported resource")
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError("invalid read rows")
            return Read(rows, True, now_ms(), source)
        except Exception as error:
            return Read([], False, now_ms(), source, type(error).__name__)

    def detail(self, resource, key, scope=None):
        source = ("cli:assignment-get " + str(key) if resource == "assignments"
                  else "/api/" + resource + "/" + str(key))
        try:
            if resource == "assignments":
                # This read includes unannounced canonical-ref corrections.
                row = self.command("assignment-get", assignment_id=key)
            elif resource == "messages":
                response = self.request("/api/sessions/" + quote(scope, safe="") +
                                        "/messages/" + quote(str(key), safe=""))
                if response.get("schemaVersion") != 1:
                    raise ValueError("unsupported detail schema")
                row = response["item"]
            else:
                response = self.request("/api/" + resource + "/" + quote(str(key), safe=""))
                if response.get("schemaVersion") != 1:
                    raise ValueError("unsupported detail schema")
                row = response["item"]
            if not isinstance(row, dict):
                raise ValueError("invalid detail row")
            return Read([row], True, now_ms(), source)
        except Exception as error:
            # A detail 404 is not an authorized complete inventory.
            return Read([], False, now_ms(), source, type(error).__name__)
