#!/usr/bin/env python3
"""tb-atc-api — the control plane for the Air Traffic Control view.

A deliberately small, dependency-free HTTP service holding three things in
memory: the page's current selection, a queue of commands for the page, and a
set of tags. Anything on the network may read or write it; there is no
authentication, by design, because it controls a display and nothing else.

The page polls /api/pull, applies any commands it has not seen, and reports
what it currently has selected. External callers use the verbs below.

    GET    /api/state                    everything at once
    GET    /api/selection                {selected:[...], focused:{mode, nodes:[...]}}
    POST   /api/select                   {add:[id], remove:[id], clear:bool}
    POST   /api/focus                    {id, mode:"single"|"neighborhood"|"clear"}
    POST   /api/fit                      {on:bool}
    POST   /api/filter                   {ids:[id]} | {clear:true}
    POST   /api/arrows                   {arrows:[{from, to, text, color, source}]}
    GET    /api/arrows                   [{arrowId, from, to, text, color, source, at}]
    DELETE /api/arrows                   clear all
    DELETE /api/arrows/<arrowId>         clear one
    GET    /api/tags                     [{tagId, target, text, source, at}]
    POST   /api/tags                     {tags:[{target, text, source, color}]}
    DELETE /api/tags                     clear all
    DELETE /api/tags/<tagId>             clear one
    GET    /api/pull?since=<seq>         page only: commands + tags

Node ids are the ones the feed publishes: work items as `wi_...`, agents as
their session suffix `s_...`. `type` is "item" or "agent".

It also serves the page itself, so one process is the whole installation: no
nginx, no second port, and the API sits at /api next to the page that uses it.

Config:
  TB_ATC_HOST  bind address                     (default 127.0.0.1)
  TB_ATC_PORT  port                             (default 8787)
  TB_ATC_WEB   directory holding index.html etc (default ./web next to this file)
  TB_ATC_STATE file tags are persisted to  (default <web>/../tags.json)
"""
import json, os, posixpath, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

HOST = os.environ.get("TB_ATC_HOST", "127.0.0.1")
PORT = int(os.environ.get("TB_ATC_PORT", "8787"))
WEB = Path(os.environ.get("TB_ATC_WEB", Path(__file__).resolve().parent.parent / "web"))
# Tags are somebody's annotations, so they must survive a restart of the
# service that happens to be serving the page they are pinned to.
STATE_FILE = Path(os.environ.get("TB_ATC_STATE", WEB.parent / "tags.json"))

# Bounds. Everything here is in memory and reachable without credentials, so
# each collection needs a ceiling; without one a single caller can exhaust the
# process by accident as easily as on purpose.
MAX_BODY = 256 * 1024
MAX_TAGS = 500
MAX_TAG_TEXT = 80
# Colours are named, not hex: the page resolves each name to a value that suits
# the current theme, so a tag stays legible in both.
TAG_COLORS = ("neutral", "red", "amber", "green", "cyan", "blue", "violet")
# An arrow spans two nodes and draws thicker than anything else on screen, so
# a smaller ceiling than tags: a hundred of them is already an unreadable view.
MAX_ARROWS = 100
MAX_ARROW_TEXT = 120
MAX_ID = 128
MAX_IDS_PER_CALL = 500
CMD_HISTORY = 200

# A page that reconnects across a restart must be able to tell that the world
# it was tracking is gone: sequence numbers alone restart at zero and would
# silently skip commands.
GENERATION = uuid.uuid4().hex[:12]

lock = threading.Lock()
state = {
    "selection": [],        # brushed by a human — telemetry, not truth
    "focused": [],          # highlighted by a focus or filter, whoever set it
    "focusMode": "none",    # none | single | neighborhood | filter
    "selectionAt": 0,
    "selectionBy": None,
    "commands": [],         # [{seq, kind, ...}] — a broadcast log, trimmed
    "seq": 0,
    "oldestSeq": 0,
    "tags": {},             # tagId -> tag
    "tagSeq": 0,
    "tagsRev": 0,
    "arrows": {},           # arrowId -> arrow
    "arrowSeq": 0,
    "arrowsRev": 0,
}


def push(kind, **payload):
    state["seq"] += 1
    cmd = {"seq": state["seq"], "kind": kind, **payload}
    state["commands"].append(cmd)
    if len(state["commands"]) > CMD_HISTORY:
        del state["commands"][:-CMD_HISTORY]
        state["oldestSeq"] = state["commands"][0]["seq"]
    return cmd


def save_tags():
    try:
        STATE_FILE.write_text(json.dumps(
            {"tags": list(state["tags"].values()), "tagSeq": state["tagSeq"],
             "arrows": list(state["arrows"].values()), "arrowSeq": state["arrowSeq"]}))
    except OSError:
        pass                            # a display, not a database: never fail a request on this


def load_tags():
    try:
        d = json.loads(STATE_FILE.read_text())
    except Exception:
        return
    for t in d.get("tags", [])[:MAX_TAGS]:
        if isinstance(t, dict) and t.get("tagId"):
            state["tags"][t["tagId"]] = t
    state["tagSeq"] = max(int(d.get("tagSeq") or 0), len(state["tags"]))
    state["tagsRev"] += 1
    for a in d.get("arrows", [])[:MAX_ARROWS]:
        if isinstance(a, dict) and a.get("arrowId"):
            state["arrows"][a["arrowId"]] = a
    state["arrowSeq"] = max(int(d.get("arrowSeq") or 0), len(state["arrows"]))
    state["arrowsRev"] += 1


def clip(v, n):
    return str(v)[:n] if v is not None else None


def id_list(v):
    """A string is iterable, so a caller sending add:"wi_1" instead of
    add:["wi_1"] would otherwise be read as one id per character."""
    if not isinstance(v, list):
        return []
    return [clip(x, MAX_ID) for x in v if isinstance(x, (str, int))][:MAX_IDS_PER_CALL]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------- plumbing ----------
    def log_message(self, *a):
        pass                            # quiet; journald carries the unit's own lines

    def _send(self, obj, code=200):
        """Serialize and write OUTSIDE the state lock — a slow reader must not
        be able to freeze every other caller."""
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return None
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self._send({})

    # ---------- reads ----------
    def _serve_file(self, path):
        root = WEB.resolve()
        rel = posixpath.normpath(path.lstrip("/")) or "index.html"
        if rel in (".", "/", ""): rel = "index.html"
        target = (root / rel).resolve()
        # containment by path relationship, not string prefix: a sibling
        # directory whose name merely starts with the root would pass a
        # startswith() check
        if not target.is_relative_to(root):
            return self._send({"error": "not found"}, 404)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self._send({"error": "not found"}, 404)
        ctype = {".html":"text/html", ".js":"text/javascript", ".json":"application/json",
                 ".css":"text/css", ".png":"image/png", ".svg":"image/svg+xml"}.get(
                     target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # the feed and the page itself must never be cached; this is a live view
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._serve_file(u.path)
        snap = None
        if u.path == "/api/state":
            with lock:
                snap = {"generation": GENERATION, "selection": list(state["selection"]),
                        "focused": list(state["focused"]), "focusMode": state["focusMode"],
                        "selectionAt": state["selectionAt"], "selectionBy": state["selectionBy"],
                        "tags": list(state["tags"].values()), "seq": state["seq"],
                        "arrows": list(state["arrows"].values())}
        elif u.path == "/api/selection":
            # two distinct things: what a human brushed, and what a focus or
            # filter is currently highlighting. Neither implies the other.
            with lock:
                snap = {"selected": list(state["selection"]),
                        "focused": {"mode": state["focusMode"],
                                    "nodes": list(state["focused"])},
                        "at": state["selectionAt"]}
        elif u.path == "/api/tags":
            with lock:
                snap = list(state["tags"].values())
        elif u.path == "/api/arrows":
            with lock:
                snap = list(state["arrows"].values())
        elif u.path == "/api/pull":
            try:
                since = int((parse_qs(u.query).get("since") or ["0"])[0])
            except ValueError:
                since = 0
            with lock:
                # a page that fell further behind than the history we keep
                # cannot reconstruct deltas, and must be told so rather than
                # silently skipping to the head
                gap = since < state["oldestSeq"]
                snap = {"generation": GENERATION,
                        "commands": [] if gap else [c for c in state["commands"] if c["seq"] > since],
                        "gap": gap, "oldestSeq": state["oldestSeq"], "seq": state["seq"],
                        "tags": list(state["tags"].values()), "tagsRev": state["tagsRev"],
                        "arrows": list(state["arrows"].values()),
                        "arrowsRev": state["arrowsRev"]}
        if snap is None:
            return self._send({"error": "not found"}, 404)
        self._send(snap)

    # ---------- writes ----------
    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        if b is None:
            return self._send({"error": "body too large"}, 413)
        out = None
        with lock:
            if u.path == "/api/select":
                add, rm = id_list(b.get("add")), id_list(b.get("remove"))
                clear = bool(b.get("clear"))
                if not (add or rm or clear):
                    out = ({"error": "nothing to do"}, 400)
                else:
                    out = (push("select", add=add, remove=rm, clear=clear), 200)

            if u.path == "/api/focus":
                mode = b.get("mode", "single")
                if mode not in ("single", "neighborhood", "clear"):
                    out = ({"error": "mode must be single, neighborhood or clear"}, 400)
                elif mode != "clear" and not b.get("id"):
                    out = ({"error": "id required"}, 400)
                else:
                    out = (push("focus", id=clip(b.get("id"), MAX_ID), mode=mode), 200)

            if u.path == "/api/filter":
                # an agent's filter is a membership list; the page ghosts
                # everything outside it exactly as a typed query does
                if b.get("clear"):
                    out = (push("filter", ids=[], clear=True), 200)
                else:
                    ids = id_list(b.get("ids"))
                    out = ((({"error": "ids required"}), 400) if not ids
                           else (push("filter", ids=ids, clear=False), 200))

            if u.path == "/api/fit":
                out = (push("fit", on=bool(b.get("on", True))), 200)

            if u.path == "/api/selection":       # a page reporting in
                raw = b.get("selection")
                sel = raw[:MAX_IDS_PER_CALL] if isinstance(raw, list) else []
                state["selection"] = [
                    {"id": clip(x.get("id"), MAX_ID), "type": clip(x.get("type"), 8),
                     "title": clip(x.get("title"), 120)}
                    for x in sel if isinstance(x, dict)]
                foc = b.get("focused")
                state["focused"] = [
                    {"id": clip(x.get("id"), MAX_ID), "type": clip(x.get("type"), 8),
                     "title": clip(x.get("title"), 120)}
                    for x in (foc[:MAX_IDS_PER_CALL] if isinstance(foc, list) else [])
                    if isinstance(x, dict)]
                mode = b.get("focusMode")
                state["focusMode"] = mode if mode in ("none","single","neighborhood","filter") else "none"
                state["selectionAt"] = int(time.time() * 1000)
                state["selectionBy"] = clip(b.get("clientId"), 32)
                out = ({"ok": True, "count": len(state["selection"])}, 200)

            if u.path == "/api/tags":
                made = []
                raw_tags = b.get("tags")
                for t in (raw_tags[:MAX_TAGS] if isinstance(raw_tags, list) else []):
                    if not isinstance(t, dict):
                        continue
                    if len(state["tags"]) >= MAX_TAGS:
                        break
                    target = clip(t.get("target"), MAX_ID)
                    text = (t.get("text") or "")[:MAX_TAG_TEXT * 2].strip()
                    if not target or not text:
                        continue
                    state["tagSeq"] += 1
                    colour = t.get("color")
                    tag = {
                        "tagId": f"t{state['tagSeq']}",
                        "target": str(target),
                        "text": text[:MAX_TAG_TEXT],
                        "source": "user" if t.get("source") == "user" else "agent",
                        "color": colour if colour in TAG_COLORS else "neutral",
                        "at": int(time.time() * 1000),
                    }
                    state["tags"][tag["tagId"]] = tag
                    made.append(tag)
                if made:
                    state["tagsRev"] += 1
                    save_tags()
                out = ({"created": made}, 200)

            if u.path == "/api/arrows":
                made = []
                raw = b.get("arrows")
                for a in (raw[:MAX_ARROWS] if isinstance(raw, list) else []):
                    if not isinstance(a, dict):
                        continue
                    if len(state["arrows"]) >= MAX_ARROWS:
                        break
                    src, dst = clip(a.get("from"), MAX_ID), clip(a.get("to"), MAX_ID)
                    # an arrow to itself has no direction to draw
                    if not src or not dst or src == dst:
                        continue
                    state["arrowSeq"] += 1
                    colour = a.get("color")
                    arrow = {
                        "arrowId": f"r{state['arrowSeq']}",
                        "from": str(src), "to": str(dst),
                        "text": ((a.get("text") or "").strip())[:MAX_ARROW_TEXT],
                        "source": "user" if a.get("source") == "user" else "agent",
                        "color": colour if colour in TAG_COLORS else "neutral",
                        "at": int(time.time() * 1000),
                    }
                    state["arrows"][arrow["arrowId"]] = arrow
                    made.append(arrow)
                if made:
                    state["arrowsRev"] += 1
                    save_tags()
                out = ({"created": made}, 200)

            if u.path == "/api/tags/clear":
                state["tags"].clear(); state["tagsRev"] += 1; save_tags()
                out = ({"ok": True}, 200)
        if out is None:
            return self._send({"error": "not found"}, 404)
        self._send(out[0], out[1])

    def do_DELETE(self):
        u = urlparse(self.path)
        out = None
        with lock:
            if u.path == "/api/tags":
                state["tags"].clear(); state["tagsRev"] += 1; save_tags()
                out = ({"ok": True}, 200)
            elif u.path == "/api/arrows":
                state["arrows"].clear(); state["arrowsRev"] += 1; save_tags()
                out = ({"ok": True}, 200)
            elif u.path.startswith("/api/tags/"):
                tid = u.path.rsplit("/", 1)[-1]
                gone = state["tags"].pop(tid, None)
                if gone:
                    state["tagsRev"] += 1; save_tags()
                out = ({"deleted": bool(gone)}, 200 if gone else 404)
            elif u.path.startswith("/api/arrows/"):
                aid = u.path.rsplit("/", 1)[-1]
                gone = state["arrows"].pop(aid, None)
                if gone:
                    state["arrowsRev"] += 1; save_tags()
                out = ({"deleted": bool(gone)}, 200 if gone else 404)
        if out is None:
            return self._send({"error": "not found"}, 404)
        self._send(out[0], out[1])


if __name__ == "__main__":
    load_tags()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"tb-atc on http://{HOST}:{PORT}/  (web root: {WEB})", flush=True)
    srv.serve_forever()
