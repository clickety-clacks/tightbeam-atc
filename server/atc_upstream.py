"""Published Tightbeam reads. Credentials stay in this service."""

from dataclasses import dataclass
import json
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


# These are the evidence fields shared by the published attest read and feed.
# Artifact metadata remains a separate read; it is not silently lost when a
# feed projection omits the read command's extra metadata fields.
ATTEST_FIELDS = (
    "id", "assignmentId", "kind", "verdictKind", "note", "bySession", "byUser",
    "producer", "producerCommand", "byHarness", "byProvider", "commitRefs", "ts",
)


def attest_row(row):
    return {key: row.get(key) for key in ATTEST_FIELDS}


class Gateway:
    def __init__(self, url, device_token, org_token, user, cli_version="0.1.9"):
        self.url = url.rstrip("/")
        self.device_token = device_token
        self.org_token = org_token
        self.user = user
        self.cli_version = cli_version
        self.read_count = 0
        self.count_lock = threading.Lock()

    def request(self, path, *, body=None, org=False):
        headers = {"Authorization": "Bearer " + (
            self.org_token if org else self.device_token)}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json",
                            "X-Tightbeam-Cli-Version": self.cli_version})
        with self.count_lock:
            self.read_count += 1
        with urlopen(Request(self.url + path, data=data, headers=headers),
                     timeout=30) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError("upstream read failed")
        return result

    def version(self):
        with urlopen(self.url + "/version", timeout=10) as response:
            result = json.load(response)
        return {key: result[key] for key in ("version", "build", "sha")}

    def command(self, verb, **params):
        # This closed set is the adapter's read interface, not a generic
        # command proxy available to viewers.
        if verb not in {"assignments", "attests", "role-list", "assignment-get", "artifacts"}:
            raise ValueError("unsupported read")
        response = self.request("/agent/dispatch", org=True, body={
            "verb": verb, "asUser": self.user, "params": params})
        result = response["result"]
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError("upstream read command failed")
        return result

    def collection(self, path):
        rows, cursor, seen = [], None, set()
        try:
            while True:
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
                    return Read(rows, True, now_ms(), path)
                if page["hasMoreAfter"] is not True:
                    raise ValueError("invalid page completion")
                cursor = page["newestCursor"]
                if not cursor or cursor in seen:
                    raise ValueError("collection cursor did not advance")
                seen.add(cursor)
        except Exception as error:
            # Keep partial pages, but never expose response bodies or tokens.
            return Read(rows, False, now_ms(), path, type(error).__name__)

    def read(self, resource, scope=None):
        if resource in {"work-items", "sessions", "decision-requests"}:
            return self.collection("/api/" + resource)
        if resource in {"turns", "wakes", "messages"}:
            return self.collection("/api/sessions/" + quote(scope, safe="") + "/" + resource)
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
            return Read(rows, True, now_ms(), resource)
        except Exception as error:
            return Read([], False, now_ms(), resource, type(error).__name__)

    def detail(self, resource, key, scope=None):
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
            return Read([row], True, now_ms(), resource + "/" + str(key))
        except Exception as error:
            # A detail 404 is not an authorized complete inventory.
            return Read([], False, now_ms(), resource + "/" + str(key), type(error).__name__)
