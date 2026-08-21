#!/usr/bin/env python3
"""tb-atc-api — the control plane for the Air Traffic Control view.

A deliberately small, dependency-free HTTP service holding three things in
memory: the page's current selection, a queue of commands for the page, and a
set of tags. Anything on the network may read or write it; there is no
authentication, by design, because it controls a display and nothing else.

The page polls /api/pull, applies any commands it has not seen, and reports
what it currently has selected. External callers use the verbs below.

    GET    /api/state                    everything at once
    GET    /api/selection[?clientId=id]  one viewer's selection, focus, and camera
    POST   /api/select                   {add:[id], remove:[id], clear:bool}
    POST   /api/focus                    {id, mode:"single"|"neighborhood"|"clear"}
    POST   /api/fit                      {on:bool}
    POST   /api/filter                   {ids:[id]} | {query:"text"} | {clear:true}
    POST   /api/arrows                   {arrows:[{from, to, text, color, source, author}]}
    GET    /api/arrows                   [{arrowId, from, to, text, color, source, author, at}]
    DELETE /api/arrows?author=<who>      clear that author's
    DELETE /api/arrows?all=true          clear every author's
    DELETE /api/arrows/<arrowId>         clear one
    GET    /api/help                     this service, documented for operators
    GET    /api/searches                 [{searchId, query, ids, label, source, author, at}]
    POST   /api/searches                 {searches:[{query|ids, label, author, searchId?}]}
    DELETE /api/searches/<searchId>      forget one
    DELETE /api/searches?author=<who>    forget that author's
    GET    /api/tags                     [{tagId, target, text, source, author, at}]
    POST   /api/tags                     {tags:[{target, text, source, color}]}
    DELETE /api/tags?author=<who>        clear that author's
    DELETE /api/tags?all=true            clear every author's
    DELETE /api/tags/<tagId>             clear one

`author` is who is speaking — a name a reader would recognise, e.g.
"watchdog:018". It is not a credential and nothing verifies it; it exists so a
board carrying several agents' notes can show who wrote what, and so an agent
tidying up can remove its own without taking everyone else's. A bare
`DELETE /api/tags` is refused for exactly that reason: say whose.
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
import copy, json, math, os, posixpath, threading, time, uuid
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
MAX_AUTHOR = 48
# Searches accumulate as cards a human can re-open, so the ceiling is a
# readable pile rather than a log.
MAX_SEARCHES = 40
MAX_SEARCH_LABEL = 80
MAX_VIEWER_REPORTS = 50
MAX_ID = 128
MAX_IDS_PER_CALL = 500
CMD_HISTORY = 200

# A page that reconnects across a restart must be able to tell that the world
# it was tracking is gone: sequence numbers alone restart at zero and would
# silently skip commands.
GENERATION = uuid.uuid4().hex[:12]

lock = threading.Lock()
state = {
    # Page-local telemetry stays keyed by viewer. selectionBy points at the
    # report projected through the legacy compatibility fields.
    "viewerReports": {},    # clientId -> {selection, focused, focusMode, camera, at}
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
    # A search a human or an agent ran, kept so it can be re-opened after it is
    # dismissed. Applying one is still /api/filter; this is only the history.
    "searches": {},         # searchId -> search
    "searchSeq": 0,
    "searchesRev": 0,
}


HELP = """# Tightbeam Air Traffic Control — operating the view

A live 3D display of this org. You are reading its own documentation; this
endpoint is the authority, and any skill or note that disagrees with it is
stale. `GET /api/help` returns this text; add `?format=json` for the endpoint
list as data.

There is no authentication. It drives a display and nothing else. Several
agents and at least one human share it at the same time, so everything below
about provenance and cleanup is about not trampling each other.

## What you can do

  read what viewers are looking at    GET  /api/selection
  read everything at once             GET  /api/state
  read the population                 GET  /data.json
  narrow the view to a set            POST /api/filter   {ids:[...], query|label}
  narrow the view by words            POST /api/filter   {query:"..."}
  point at one node                   POST /api/focus    {id, mode}
  add to what is selected             POST /api/select   {add:[...]}
  fly to the selection                POST /api/fit      {on:true}
  pin a note to a node                POST /api/tags
  draw a relation between two         POST /api/arrows
  file/rename a search                POST /api/searches

## Ids

Work items are the substrate's full id, `wi_` and a UUID — the same string
`tightbeam work-item-get` accepts. Agents are their session suffix, `s_...`.
The feed's `short` field is for display only; act on `id`.

## Reading before writing

`/api/selection` returns every current viewer under `viewers`, and projects the
most recent report through the compatibility fields at the top level. Pass
`?clientId=<id>` to read only that viewer. Every result says which `clientId` it
describes and includes that viewer's camera pose.

Each viewer report contains two different things and they do not imply each other:

  selected   what a human brushed
  focused    what a focus or filter is lighting, with `mode`

A human's bare "these" or "this one" almost always means `selected`. Answering
about the wrong one is the most common way to be confidently useless here.

## Searching, and naming what you searched for

`POST /api/filter` with `ids` narrows to a set; with `query` it runs the same
text match a human types. Every search is FILED as a card the human can click
to run again, so a filter is not a fleeting command — it leaves something
behind.

**Naming is required, not encouraged.** A filter carrying `ids` is REFUSED
without a `query` or a `label`, and so is a new card through /api/searches. The
refusal carries this guidance. Name it for WHAT WAS ASKED, not what matched:

  good   "everything blocking the 0.1.8 cut"
  good   "cards the reviewer has not seen"
  bad    "17 picked"
  bad    "filter"

The terms say what matched. The name says what the question was, and it is the
only thing a human reads when deciding whether to re-open your search an hour
later. A card without one is nearly useless to them.

  POST /api/searches {"searches":[{"searchId":"q7","label":"blocking the cut"}]}

edits a card in place — revise yours rather than filing near-duplicates.
Identical searches are refreshed, not stacked, so re-running one on a cadence
leaves a single card.

## Annotating

A **tag** is a short label pinned above one node. An **arrow** is a labelled
curve between two, for a relation: blocks, owns, caused. Colour is yours from
`neutral red amber green cyan blue violet`, and means whatever you decide —
group a batch by colour rather than decorating each one differently.

**Always pass `author`** — a name a reader would recognise, like `watchdog:018`.
It renders under the note, and it is how you clean up your own work without
taking anyone else's: `DELETE /api/tags?author=watchdog:018`. An unqualified
clear-all is refused for that reason; `?all=true` exists for a human resetting
the board, not for you.

Short arrow labels ride the curve; long ones fall back to a card. Keep them to
two or three words and put the explanation in a tag.

## Not fighting the human

The display changes under their hands. Prefer tagging, which is passive, over
focusing, which moves their camera. Say what you did and why. Clean up when the
question is closed. If they are mid-drag your commands are held until they lift
the pointer — that is deliberate, not lag.

## What the picture means

Agents hang below the grid as discs, laid out as the spawner tree; work items
float above it and descend a band per satisfied requirement. A ring on the
floor says whether the work reached the branch. An agent taking a turn wears a
rippling ring. Ghosted means faint and still there — nothing is ever hidden.

Layers stack: a search, then a fit (`f`), then a focus. The innermost renders.
A background click pops one layer; the mode strip at the bottom left names what
is in force.
"""


def help_json():
    return {
        "service": "tightbeam-atc",
        "text": "GET /api/help",
        "conventions": {
            "ids": "work items are full wi_<uuid>; agents are s_<suffix>",
            "author": "always send it; it renders, and it scopes your cleanup",
            "searchNames": "name a search for what was ASKED, not what matched",
        },
        "endpoints": [
            {"method": "GET", "path": "/api/help", "does": "this document"},
            {"method": "GET", "path": "/api/state", "does": "viewer reports, tags, arrows, searches"},
            {"method": "GET", "path": "/api/selection", "does": "per-client selection, focus, camera"},
            {"method": "GET", "path": "/data.json", "does": "the population and its derived state"},
            {"method": "POST", "path": "/api/select", "body": "{add:[id], remove:[id], clear:bool}"},
            {"method": "POST", "path": "/api/focus", "body": '{id, mode:"single"|"neighborhood"|"clear"}'},
            {"method": "POST", "path": "/api/fit", "body": "{on:bool}"},
            {"method": "POST", "path": "/api/filter", "body": '{ids:[id]} | {query:"text"} | {clear:true}'},
            {"method": "GET", "path": "/api/searches", "does": "the filed search cards"},
            {"method": "POST", "path": "/api/searches", "body": "{searches:[{query|ids, label, author, searchId?}]}"},
            {"method": "DELETE", "path": "/api/searches/<id>", "does": "forget one"},
            {"method": "GET", "path": "/api/tags", "does": "every tag"},
            {"method": "POST", "path": "/api/tags", "body": "{tags:[{target, text, color, source, author}]}"},
            {"method": "DELETE", "path": "/api/tags?author=<who>", "does": "remove yours"},
            {"method": "GET", "path": "/api/arrows", "does": "every arrow"},
            {"method": "POST", "path": "/api/arrows", "body": "{arrows:[{from, to, text, color, author}]}"},
            {"method": "DELETE", "path": "/api/arrows?author=<who>", "does": "remove yours"},
        ],
    }


NAME_GUIDANCE = (
    "Name the search after what the user asked for. Pass `query` (the words) "
    "or `label` (a summary) — for an id list, `label` is what a human will "
    "read on the card. Say what the QUESTION was, not what matched: "
    "\"everything blocking the 0.1.8 cut\", not \"17 picked\". A card with no "
    "name is nearly useless to whoever finds it later."
)

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
             "arrows": list(state["arrows"].values()), "arrowSeq": state["arrowSeq"],
             "searches": list(state["searches"].values()), "searchSeq": state["searchSeq"]}))
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
    for q in d.get("searches", [])[:MAX_SEARCHES]:
        if isinstance(q, dict) and q.get("searchId"):
            state["searches"][q["searchId"]] = q
    state["searchSeq"] = max(int(d.get("searchSeq") or 0), len(state["searches"]))
    state["searchesRev"] += 1


def clip(v, n):
    return str(v)[:n] if v is not None else None


def id_list(v):
    """A string is iterable, so a caller sending add:"wi_1" instead of
    add:["wi_1"] would otherwise be read as one id per character."""
    if not isinstance(v, list):
        return []
    return [clip(x, MAX_ID) for x in v if isinstance(x, (str, int))][:MAX_IDS_PER_CALL]


def vector3(v):
    if not isinstance(v, list) or len(v) != 3:
        return None
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
           for x in v):
        return None
    return list(v)


def camera_report(v):
    if not isinstance(v, dict):
        return None
    position, target = vector3(v.get("position")), vector3(v.get("target"))
    if position is None or target is None:
        return None
    return {"position": position, "target": target,
            "framing": clip(v.get("framing"), 16),
            "inFlight": bool(v.get("inFlight"))}


def focused_arrow_report(v):
    if not isinstance(v, dict) or not v.get("arrowId"):
        return None
    return {"arrowId": clip(v.get("arrowId"), MAX_ID),
            "from": clip(v.get("from"), MAX_ID),
            "to": clip(v.get("to"), MAX_ID)}


def compatibility_report(report, client_id=None):
    """Project one private viewer report through the original read shape."""
    report = report or {}
    return {"clientId": report.get("clientId", client_id),
            "selected": copy.deepcopy(report.get("selection", [])),
            "focused": {"mode": report.get("focusMode", "none"),
                        "nodes": copy.deepcopy(report.get("focused", []))},
            "camera": copy.deepcopy(report.get("camera")),
            "at": report.get("at", 0)}


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
                report = state["viewerReports"].get(state["selectionBy"])
                compat = compatibility_report(report)
                snap = {"generation": GENERATION, "selection": compat["selected"],
                        "focused": compat["focused"]["nodes"],
                        "focusMode": compat["focused"]["mode"],
                        "selectionAt": compat["at"], "selectionBy": compat["clientId"],
                        "camera": compat["camera"],
                        "viewerReports": copy.deepcopy(list(state["viewerReports"].values())),
                        "tags": list(state["tags"].values()), "seq": state["seq"],
                        "arrows": list(state["arrows"].values()),
                        "searches": list(state["searches"].values())}
        elif u.path == "/api/selection":
            # two distinct things: what a human brushed, and what a focus or
            # filter is currently highlighting. Neither implies the other.
            with lock:
                requested = clip((parse_qs(u.query).get("clientId") or [None])[0], 32)
                client_id = requested if requested is not None else state["selectionBy"]
                snap = compatibility_report(state["viewerReports"].get(client_id), client_id)
                if requested is None:
                    snap["viewers"] = [compatibility_report(r)
                                       for r in state["viewerReports"].values()]
        elif u.path == "/api/tags":
            with lock:
                snap = list(state["tags"].values())
        elif u.path == "/api/arrows":
            with lock:
                snap = list(state["arrows"].values())
        elif u.path == "/api/searches":
            with lock:
                snap = list(state["searches"].values())
        elif u.path == "/api/help":
            if (parse_qs(u.query).get("format") or [""])[0] == "json":
                snap = help_json()
            else:
                body = HELP.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(body)
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
                        "arrowsRev": state["arrowsRev"],
                        "searches": list(state["searches"].values()),
                        "searchesRev": state["searchesRev"]}
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
                # Applying a search also files it, so it can be re-opened after
                # it is dismissed. Identical searches are refreshed rather than
                # piled up: a patrol re-running the same query every minute
                # should leave one card, not sixty.
                def _file_search(query, ids, author, source, result_kind):
                    key = (result_kind, query or "", tuple(ids or ()))
                    for rec in state["searches"].values():
                        rec_kind = rec.get("resultKind") or ("ids" if rec.get("ids") else "query")
                        rec_key = (rec_kind,
                                   rec.get("query") or "", tuple(rec.get("ids") or ()))
                        if rec_key == key:
                            rec["at"] = int(time.time() * 1000)
                            state["searchesRev"] += 1
                            return rec
                    if len(state["searches"]) >= MAX_SEARCHES:
                        oldest = min(state["searches"].values(), key=lambda x: x.get("at") or 0)
                        del state["searches"][oldest["searchId"]]
                    state["searchSeq"] += 1
                    rec = {"searchId": f"q{state['searchSeq']}", "query": query or "",
                           "ids": list(ids or []), "label": None,
                           "resultKind": result_kind,
                           "source": source, "author": clip(author, MAX_AUTHOR) or None,
                           "at": int(time.time() * 1000)}
                    state["searches"][rec["searchId"]] = rec
                    state["searchesRev"] += 1
                    return rec

                # an agent's filter is a membership list; the page ghosts
                # everything outside it exactly as a typed query does
                if b.get("clear"):
                    out = (push("filter", ids=[], clear=True), 200)
                else:
                    ids = id_list(b.get("ids"))
                    result_kind = "ids" if "ids" in b else "query"
                    query = clip(b.get("query"), MAX_SEARCH_LABEL)
                    label = clip(b.get("label"), MAX_SEARCH_LABEL)
                    # A filter is filed as a card the human can re-open, so an
                    # unnamed one is a row of numbers nobody can act on later.
                    # Refusing is kinder than accepting and being useless.
                    if result_kind == "ids" and not (query or label):
                        out = ({"error": "name this search",
                                "guidance": NAME_GUIDANCE}, 400)
                    elif result_kind == "query" and query:
                        # a text search, the same thing a human types: the page
                        # matches it against titles and ids. Without this the
                        # only way to express a filter on the wire was a picked
                        # set, so re-running a typed search had to be sent as a
                        # clear — which cleared it.
                        rec = _file_search(query, [], b.get("author"), "agent", "query")
                        save_tags()
                        out = (push("filter", ids=[], clear=False, query=query,
                                    resultKind="query",
                                    searchId=rec["searchId"]), 200)
                    elif result_kind == "query":
                        out = ({"error": "ids or query required"}, 400)
                    else:
                        rec = _file_search(query, ids, b.get("author"), "agent", "ids")
                        if label:
                            rec["label"] = label
                        save_tags()
                        out = (push("filter", ids=ids, clear=False,
                                    resultKind="ids",
                                    query=query or label,
                                    searchId=rec["searchId"]), 200)

            if u.path == "/api/fit":
                out = (push("fit", on=bool(b.get("on", True))), 200)

            if u.path == "/api/selection":       # a page reporting in
                client_id = clip(b.get("clientId"), 32)
                raw = b.get("selection")
                sel = raw[:MAX_IDS_PER_CALL] if isinstance(raw, list) else []
                selection = [
                    {"id": clip(x.get("id"), MAX_ID), "type": clip(x.get("type"), 8),
                     "title": clip(x.get("title"), 120)}
                    for x in sel if isinstance(x, dict)]
                foc = b.get("focused")
                focused = [
                    {"id": clip(x.get("id"), MAX_ID), "type": clip(x.get("type"), 8),
                     "title": clip(x.get("title"), 120)}
                    for x in (foc[:MAX_IDS_PER_CALL] if isinstance(foc, list) else [])
                    if isinstance(x, dict)]
                mode = b.get("focusMode")
                mode = mode if mode in ("none","single","neighborhood","filter") else "none"
                if client_id not in state["viewerReports"] and len(state["viewerReports"]) >= MAX_VIEWER_REPORTS:
                    out = ({"error": "viewer report capacity reached"}, 503)
                else:
                    state["viewerReports"][client_id] = {
                        "clientId": client_id, "selection": selection, "focused": focused,
                        "focusMode": mode, "focusedArrow": focused_arrow_report(b.get("focusedArrow")),
                        "camera": camera_report(b.get("camera")),
                        "at": int(time.time() * 1000),
                    }
                    state["selectionBy"] = client_id
                    out = ({"ok": True, "clientId": client_id,
                            "count": len(selection)}, 200)

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
                        "author": clip(t.get("author"), MAX_AUTHOR) or None,
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
                        "author": clip(a.get("author"), MAX_AUTHOR) or None,
                        "at": int(time.time() * 1000),
                    }
                    state["arrows"][arrow["arrowId"]] = arrow
                    made.append(arrow)
                if made:
                    state["arrowsRev"] += 1
                    save_tags()
                out = ({"created": made}, 200)

            if u.path == "/api/searches":
                # Create or edit. A searchId edits that card in place, so a
                # human can reopen a search and change its terms, and an agent
                # can revise one it filed, without piling up near-duplicates.
                made, unnamed = [], []
                raw = b.get("searches")
                for q in (raw[:MAX_SEARCHES] if isinstance(raw, list) else []):
                    if not isinstance(q, dict):
                        continue
                    sid = clip(q.get("searchId"), MAX_ID)
                    prev = state["searches"].get(sid) if sid else None
                    if not prev and len(state["searches"]) >= MAX_SEARCHES:
                        # drop the oldest rather than refuse: the pile is a
                        # convenience, and a full one must not block new work
                        oldest = min(state["searches"].values(), key=lambda x: x.get("at") or 0)
                        del state["searches"][oldest["searchId"]]
                    query = (q.get("query") or "").strip()[:MAX_SEARCH_LABEL]
                    ids = id_list(q.get("ids"))
                    result_kind = "ids" if "ids" in q else "query"
                    if not query and not ids and not prev:
                        continue
                    # a NEW card must arrive named; editing one by id need not
                    if not prev and not (query or q.get("label")):
                        unnamed.append(ids[:3])
                        continue
                    if prev:
                        rec = dict(prev)
                        if query or ids or "ids" in q:
                            rec["query"], rec["ids"] = query, ids
                            rec["resultKind"] = result_kind
                        if q.get("label") is not None:
                            rec["label"] = clip(q.get("label"), MAX_SEARCH_LABEL)
                    else:
                        state["searchSeq"] += 1
                        rec = {"searchId": f"q{state['searchSeq']}",
                               "query": query, "ids": ids,
                               "resultKind": result_kind,
                               "label": clip(q.get("label"), MAX_SEARCH_LABEL) or None,
                               "source": "user" if q.get("source") == "user" else "agent",
                               "author": clip(q.get("author"), MAX_AUTHOR) or None}
                    rec["at"] = int(time.time() * 1000)
                    state["searches"][rec["searchId"]] = rec
                    made.append(rec)
                if made:
                    state["searchesRev"] += 1
                    save_tags()
                if unnamed and not made:
                    out = ({"error": "name this search", "guidance": NAME_GUIDANCE}, 400)
                else:
                    out = ({"searches": made,
                            **({"refused": len(unnamed), "guidance": NAME_GUIDANCE}
                               if unnamed else {})}, 200)

            if u.path == "/api/tags/clear":
                state["tags"].clear(); state["tagsRev"] += 1; save_tags()
                out = ({"ok": True}, 200)
        if out is None:
            return self._send({"error": "not found"}, 404)
        self._send(out[0], out[1])

    def _sweep(self, coll, rev, q):
        """Remove a whole collection, or one author's share of it.

        Several agents annotate the same board, so an unqualified clear is an
        accident waiting to happen: one agent tidying up after itself takes
        everyone else's notes with it. Saying whose is the whole point, and
        `all=true` is there for a human resetting the board deliberately."""
        author = (q.get("author") or [None])[0]
        wants_all = (q.get("all") or [""])[0].lower() in ("1", "true", "yes")
        if not author and not wants_all:
            return ({"error": "say whose: pass ?author=<who>, or ?all=true to "
                              "clear every author's"}, 400)
        if wants_all:
            gone = len(state[coll]); state[coll].clear()
        else:
            doomed = [k for k, v in state[coll].items() if (v.get("author") or None) == author]
            for k in doomed:
                del state[coll][k]
            gone = len(doomed)
        if gone:
            state[rev] += 1; save_tags()
        return ({"deleted": gone, "author": None if wants_all else author}, 200)

    def do_DELETE(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        out = None
        with lock:
            if u.path == "/api/tags":
                out = self._sweep("tags", "tagsRev", q)
            elif u.path == "/api/arrows":
                out = self._sweep("arrows", "arrowsRev", q)
            elif u.path.startswith("/api/tags/"):
                tid = u.path.rsplit("/", 1)[-1]
                gone = state["tags"].pop(tid, None)
                if gone:
                    state["tagsRev"] += 1; save_tags()
                out = ({"deleted": bool(gone)}, 200 if gone else 404)
            elif u.path == "/api/searches":
                out = self._sweep("searches", "searchesRev", q)
            elif u.path.startswith("/api/searches/"):
                sid = u.path.rsplit("/", 1)[-1]
                gone = state["searches"].pop(sid, None)
                if gone:
                    state["searchesRev"] += 1; save_tags()
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
