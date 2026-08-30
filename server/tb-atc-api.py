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
    POST   /api/names                    {on:bool} — show/hide the agent-name display field
    POST   /api/annotations              {on:bool} — show/hide tags and arrows
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
  TB_BASE_DIR  Tightbeam base dir, for state.db (default ~/.tightbeam)
  TB_ATC_TOKEN_FILE  operator token path (default <web>/../operator.token)
  TB_ATC_AUDIT_LOG   ruling audit log path (default <web>/../atc-rulings.log)
"""
import copy, json, math, os, posixpath, re, secrets, sqlite3, subprocess, threading, time, uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

HOST = os.environ.get("TB_ATC_HOST", "127.0.0.1")
PORT = int(os.environ.get("TB_ATC_PORT", "8787"))
WEB = Path(os.environ.get("TB_ATC_WEB", Path(__file__).resolve().parent.parent / "web"))
# Tags are somebody's annotations, so they must survive a restart of the
# service that happens to be serving the page they are pinned to.
STATE_FILE = Path(os.environ.get("TB_ATC_STATE", WEB.parent / "tags.json"))
# state.db lives in the Tightbeam base dir, not the ATC deployment — same
# path the generator already uses, opened the same way: mode=ro, always.
BASE_DIR = Path(os.environ.get("TB_BASE_DIR", str(Path.home() / ".tightbeam")))
STATE_DB = BASE_DIR / "state.db"
# The operator token gates /rule and /ask. It is not a security boundary —
# there is no login here, by design — it exists only to stop a stray curl or
# a careless agent from ruling something. Generated once, on first start. The
# browser never sees it directly: GET / sets it as an HttpOnly cookie, so
# there is nothing for a human to type or store, and nothing client JS can
# read. The X-ATC-Operator header still works too, for a future approved
# client that isn't a browser tab.
TOKEN_FILE = Path(os.environ.get("TB_ATC_TOKEN_FILE", WEB.parent / "operator.token"))
OPERATOR_COOKIE = "atc_operator"
# A second audit trail, independent of whatever the gateway itself records —
# belt and suspenders while ATC rules via the interim (CLI shell-out) path.
AUDIT_LOG = Path(os.environ.get("TB_ATC_AUDIT_LOG", WEB.parent / "atc-rulings.log"))

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
MAX_DECISION_LABEL = 200
MAX_RATIONALE = 4000
MAX_RESPONSE = 4000
MAX_QUESTION = 2000
CLI_TIMEOUT = 20   # seconds — the one gateway call a rule/ask click makes

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
    # Whether the page shows an agent's name field. Everything else about the
    # agent (role/archetype, id, idle, workload) is unaffected — this hides
    # one field, not the agent. Defaults on; ephemeral, like fit/focus/
    # selection — a service restart resets it rather than silently leaving
    # names hidden with no visible reason why.
    "namesVisible": True,
    # Tags and arrows are presentation-only annotations. Keep accepting and
    # retaining them while hidden; this changes the view, not the data. Like
    # names, it is intentionally ephemeral and resets shown on service restart.
    "annotationsVisible": True,
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
    # A follow-up George asked on a decision, captured at ask-time so the
    # chip can still show the original question and countdown clock even
    # after the row itself is superseded and drops out of decisions[].
    "followups": {},        # drId -> {drId, question, askedAt, originalQuestion, raisedAt, deadlineAt}
    "followupsRev": 0,
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
  read what is waiting on George      GET  /data.json  (its `decisions` array)
  narrow the view to a set            POST /api/filter   {ids:[...], query|label}
  narrow the view by words            POST /api/filter   {query:"..."}
  point at one node                   POST /api/focus    {id, mode}
  add to what is selected             POST /api/select   {add:[...]}
  fly to the selection                POST /api/fit      {on:true}
  show/hide agent names               POST /api/names    {on:bool}
  show/hide annotations               POST /api/annotations {on:bool}
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

Each viewer report contains several different things and they do not imply
each other:

  selected   what a human brushed
  focused    what a focus or filter is lighting, with `mode`
  decision   which Desk chip, if any, that viewer has open right now

A human's bare "these" or "this one" almost always means `selected`. Answering
about the wrong one is the most common way to be confidently useless here.

`decision` is `{id, question, raiserAgentId, workItemId, deadlineAt}` when a
Desk chip is open, `null` otherwise — set the moment a chip opens or closes,
the same way `focused` tracks a node focus. Asked "which decision do I have
selected?", read this rather than inferring it from the focus neighbourhood:
opening a chip also focuses the raiser, but the reverse is not true, and a
focus alone does not mean a decision is open.

## Searching, and naming what you searched for

`POST /api/filter` with `ids` narrows to a set; with `query` it runs the same
text match a human types. Every search is FILED as a card the human can click
to run again, so a filter is not a fleeting command — it leaves something
behind.

**Every call files a card. Nothing here deduplicates for you.** Filing twice
leaves two cards, even when they are identical: this service will not decide
that your call was really somebody else's card, because that is state you
cannot see from the outside. Keeping the pile tidy is YOUR job. Read
`GET /api/searches` first, and when you are updating something you filed
before, pass its `searchId` to `POST /api/searches` — that edits the card in
place instead of minting a twin. Re-running the same question on a schedule
means reusing one `searchId`, not filing sixty cards.

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

## Showing or hiding agent names

`POST /api/names {"on":false}` hides the agent-name field everywhere it is
displayed — the floating label, hover, the focus pane, tag/arrow authorship,
and a work item's own turn list all stop showing it. Role/archetype, id,
idle/workload and the rest of an agent's data are unaffected; this hides one
field, not the agent. `{"on":true}` shows it again. `GET /api/state` and
`GET /api/pull` both carry the current `namesVisible` value, so read before
you flip it if you are not sure which way it is set. It resets to shown on a
service restart.

## Annotating

## Showing or hiding annotations

`POST /api/annotations {"on":false}` hides all tag cards and every authored
arrow, including arrow geometry, heads, and labels. Tags and arrows remain in
the service, new annotations are still accepted, and the normal read endpoints
continue to return them. `{"on":true}` restores their rendering. `GET
/api/state` and `GET /api/pull` carry `annotationsVisible`; the setting resets
to shown on a service restart.

A hidden arrow is not clickable or focusable, because it is no longer visible.
This does not clear an existing Arrow Focus; its data still exists and it will
render again when annotations are restored.

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

## The Desk — operator decisions waiting on George

`GET /data.json`'s `decisions` array carries every OPEN `kind='operator'`
decision request — an agent asked (`operator-ask`), only George can answer
(`operator-rule`), and it expires on a deadline. Each entry: `id`, `question`,
`options` (label strings), `note` (the raiser's own PO-note context, if any),
`raiserId` (a readable identity string) and `raiserAgentId` (the board's own
short id for that session, if resolvable), `assignmentId`/`workItemId` (if the
request is tied to one), `ownerUserId`, `raisedAt`, `deadlineAt`. It is
generated straight from `decision_requests` in `state.db` (read-only) — never
by polling the `tightbeam` CLI, which is how a prior read loop grew state.db
to 4.9GB and crashed the VM (clickety-clacks/tightbeam#10).

The generator (not the page, and not you) is the sole author of `atc:desk`:
while a request is open and short of its deadline, it owns one red arrow
raiser → work item (`awaiting ruling`) and one red tag on the raiser
(`needs George · Nh`), both cleared the moment the row is ruled, withdrawn, or
past its deadline, and re-created if the same situation recurs. It also files
one search, `where George is needed`, over every open raiser and its work
item. Because it is the only writer of that author, `DELETE ...?author=atc:desk`
is always safe to run yourself if you want the board clear regardless — the
next generator tick simply re-files whatever is still actually open.

Agents never rule — only the two endpoints below do, and both require the
operator token George alone holds. Roci Desk is the other control surface;
ATC is a second one, not a replacement.

## Ruling and follow-ups (operator-token only)

Both endpoints require the operator token, sent either way: as the
`atc_operator` cookie GET / sets (HttpOnly, SameSite=Strict, path `/api`) —
the browser tab does this automatically, nothing to type or paste — or as
header `X-ATC-Operator: <token>` for a future non-browser client. The token
itself lives in `~/.tightbeam-atc/operator.token` (mode 600), generated on
first start, never served over HTTP directly. Wrong or missing token/cookie
→ 403. The cookie is a speed bump against accidental calls — a stray curl
or a careless agent — not a security boundary: the ruling identity (an
approved ATC device, `ruledViaSessionKey`) is the audit, once that lands;
until then the ATC-side ruling log below is the second trail.

`POST /api/decisions/<dr_id>/rule` — body `{decision:<label>} | {response:<text>}`,
plus `rationale` (required: at least one sentence; a long-enough free-text
`response` may serve as its own rationale, a picked `decision` label never
does). The row is read fresh from `state.db` (mode=ro) at click time — never
cached — and must be `kind='operator', status='open'`; a `decision` must
match one of the row's own `options[].label`. On success this makes ONE
`tightbeam operator-rule` call and writes a line to the ATC-side audit log
(`~/.tightbeam-atc/atc-rulings.log`) as a second trail independent of
whatever the gateway itself records.

`POST /api/decisions/<dr_id>/ask` — body `{question:<text>}`. Same row
checks. On success this sends ONE `tightbeam wake` to the row's raiser with
a fixed prompt asking them to answer by re-filing with `--supersedes
<dr_id>`, and records the follow-up (question, asked-at, the original
question/deadline) so the page can show "asked Nm ago — waiting" and later
render the superseding row in place of the original.

Neither endpoint runs on a cadence — each is exactly one gateway call, made
only when a human clicks, for the same reason `decisions[]` itself never
polls the CLI: clickety-clacks/tightbeam#10.

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
            "decisions": "/data.json's decisions[] is every open operator "
                         "decision request, read from state.db (mode=ro), "
                         "never from CLI polling; ATC displays it and files "
                         "author=atc:desk arrows/tags",
            "operatorToken": "required on /rule and /ask, via either the "
                              "atc_operator cookie GET / sets automatically "
                              "(HttpOnly, SameSite=Strict) or an X-ATC-Operator "
                              "header for non-browser clients; token lives in "
                              "~/.tightbeam-atc/operator.token (mode 600), "
                              "never served over HTTP directly — a speed bump, "
                              "not a security boundary",
            "decisionSelection": "GET /api/selection and /api/state carry "
                                  "decision:{id,question,raiserAgentId,"
                                  "workItemId,deadlineAt}|null — which Desk "
                                  "chip a viewer has open; read it rather "
                                  "than inferring from the focus",
        },
        "endpoints": [
            {"method": "GET", "path": "/api/help", "does": "this document"},
            {"method": "GET", "path": "/api/state", "does": "viewer reports, tags, arrows, searches, followups"},
            {"method": "POST", "path": "/api/decisions/<dr_id>/rule",
             "body": "{decision:<label>}|{response:<text>}, rationale (required); "
                     "needs the atc_operator cookie or X-ATC-Operator header"},
            {"method": "POST", "path": "/api/decisions/<dr_id>/ask",
             "body": "{question:<text>}; needs the atc_operator cookie or X-ATC-Operator header"},
            {"method": "GET", "path": "/api/selection", "does": "per-client selection, focus, camera"},
            {"method": "GET", "path": "/data.json", "does": "the population, decisions[], and derived state"},
            {"method": "POST", "path": "/api/select", "body": "{add:[id], remove:[id], clear:bool}"},
            {"method": "POST", "path": "/api/focus", "body": '{id, mode:"single"|"neighborhood"|"clear"}'},
            {"method": "POST", "path": "/api/fit", "body": "{on:bool}"},
            {"method": "POST", "path": "/api/names", "body": "{on:bool} — show/hide the agent-name field"},
            {"method": "POST", "path": "/api/annotations", "body": "{on:bool} — show/hide tags and arrows"},
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
             "searches": list(state["searches"].values()), "searchSeq": state["searchSeq"],
             "followups": list(state["followups"].values())}))
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
    for f in d.get("followups", []):
        if isinstance(f, dict) and f.get("drId"):
            state["followups"][f["drId"]] = f
    state["followupsRev"] += 1


def ensure_operator_token():
    """Read the existing token, or mint and persist one (mode 600). Never
    served over HTTP — a human reads it off disk once and pastes it into
    the page. If the file can't be read or written, fall back to an
    in-memory token: the service still works, it just won't survive a
    restart with the same token (a stray curl gets no easier either way)."""
    try:
        if TOKEN_FILE.exists():
            tok = TOKEN_FILE.read_text().strip()
            if tok:
                return tok
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tok = secrets.token_hex(24)
        TOKEN_FILE.write_text(tok)
        os.chmod(TOKEN_FILE, 0o600)
        return tok
    except OSError as e:
        print(f"WARNING: operator token file {TOKEN_FILE} unusable ({e}); "
              "using an ephemeral in-memory token instead", flush=True)
        return secrets.token_hex(24)


def read_decision_row(dr_id):
    """One-shot, at click time — never cached, never polled. Opened and
    closed within this call; the mode=ro connection matches the generator's
    own and is the only DB access this server ever makes."""
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    try:
        r = con.execute("SELECT * FROM decision_requests WHERE id=?", (dr_id,)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    d = dict(r)
    try:
        opts = json.loads(d.get("options") or "[]")
        d["options"] = opts if isinstance(opts, list) else []
    except (TypeError, ValueError):
        d["options"] = []
    return d


def looks_like_a_sentence(text):
    """Not a grammar checker — a floor. 'Because …' passes; 'ok' or a bare
    option label does not. Whitespace-only or single-token text is refused
    outright; anything with real content and a space is accepted."""
    t = (text or "").strip()
    return len(t) >= 8 and " " in t


def run_cli(args):
    """The one gateway call a rule/ask click makes — never on a cadence,
    never in a loop. Runs OUTSIDE the state lock: it can take real time,
    and every other viewer's request must not wait on it."""
    try:
        r = subprocess.run(["tightbeam", *args], capture_output=True, text=True,
                            timeout=CLI_TIMEOUT)
        return (r.returncode == 0, r.stdout, r.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
        return (False, "", str(e))


def log_ruling(dr_id, decision_or_response):
    try:
        with open(AUDIT_LOG, "a") as f:
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            f.write(f"ruled {dr_id} {decision_or_response} by operator via atc at {iso}\n")
    except OSError:
        pass


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


def decision_report(v):
    """Which Desk chip, if any, this viewer currently has open — read-
    before-write for the Desk: an agent asked what's selected must be able
    to tell a decision is open the same way it can tell a node is focused,
    rather than inferring it from the focus neighbourhood alone."""
    if not isinstance(v, dict) or not v.get("id"):
        return None
    deadline = v.get("deadlineAt")
    return {"id": clip(v.get("id"), MAX_ID),
            "question": clip(v.get("question"), 2000),
            "raiserAgentId": clip(v.get("raiserAgentId"), MAX_ID),
            "workItemId": clip(v.get("workItemId"), MAX_ID),
            "deadlineAt": deadline if isinstance(deadline, (int, float)) and not isinstance(deadline, bool) else None}


def compatibility_report(report, client_id=None):
    """Project one private viewer report through the original read shape."""
    report = report or {}
    return {"clientId": report.get("clientId", client_id),
            "selected": copy.deepcopy(report.get("selection", [])),
            "focused": {"mode": report.get("focusMode", "none"),
                        "nodes": copy.deepcopy(report.get("focused", []))},
            "camera": copy.deepcopy(report.get("camera")),
            "decision": copy.deepcopy(report.get("decision")),
            "at": report.get("at", 0)}


# Set once in __main__, before the server starts accepting requests. Reading
# a plain module global at call time (not import time) is deliberate: it
# means the token file is only ever touched when this file actually runs as
# the server, never on a bare import (e.g. from a test).
OPERATOR_TOKEN = None


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

    def _check_operator_token(self):
        """Not a security boundary — there is no login here, by design.
        This exists only to stop a stray curl or a careless agent from
        ruling a decision or waking a raiser. The browser tab authenticates
        via the HttpOnly cookie GET / set for it; the header remains for a
        future non-browser client. Either is checked with a constant-time
        compare: it costs nothing and removes the question."""
        header_tok = self.headers.get("X-ATC-Operator")
        if header_tok and secrets.compare_digest(header_tok, OPERATOR_TOKEN):
            return True
        cookie_header = self.headers.get("Cookie")
        if cookie_header:
            jar = SimpleCookie()
            try:
                jar.load(cookie_header)
            except Exception:
                jar = {}
            morsel = jar.get(OPERATOR_COOKIE)
            if morsel and secrets.compare_digest(morsel.value, OPERATOR_TOKEN):
                return True
        return False

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
        if target.name == "index.html":
            # every page load re-sets the cookie to the current token, so a
            # service restart (which may mint a new one) is invisible to a
            # tab that gets reloaded — no prompt, nothing to re-paste.
            self.send_header("Set-Cookie",
                              f"{OPERATOR_COOKIE}={OPERATOR_TOKEN}; Path=/api; "
                              "HttpOnly; SameSite=Strict")
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
                        "camera": compat["camera"], "decision": compat["decision"],
                        "viewerReports": copy.deepcopy(list(state["viewerReports"].values())),
                        "tags": list(state["tags"].values()), "seq": state["seq"],
                        "arrows": list(state["arrows"].values()),
                        "searches": list(state["searches"].values()),
                        "namesVisible": state["namesVisible"],
                        "annotationsVisible": state["annotationsVisible"],
                        "followups": list(state["followups"].values())}
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
                        "searchesRev": state["searchesRev"],
                        "namesVisible": state["namesVisible"],
                        "annotationsVisible": state["annotationsVisible"],
                        "followups": list(state["followups"].values()),
                        "followupsRev": state["followupsRev"]}
        if snap is None:
            return self._send({"error": "not found"}, 404)
        self._send(snap)

    # ---------- ruling and follow-ups ----------
    # Both make the ONE gateway call a click is allowed to make, OUTSIDE the
    # state lock — a subprocess can take real time, and no other viewer's
    # request should have to wait on it. Only the (short) state update that
    # follows a successful call takes the lock.
    def _rule_decision(self, dr_id, b):
        if not self._check_operator_token():
            return ({"error": "forbidden"}, 403)
        row = read_decision_row(dr_id)
        if not row:
            return ({"error": "no such decision"}, 404)
        if row.get("kind") != "operator" or row.get("status") != "open":
            return ({"error": "not an open operator decision"}, 409)
        decision = clip(b.get("decision"), MAX_DECISION_LABEL)
        response = clip(b.get("response"), MAX_RESPONSE)
        rationale = clip(b.get("rationale"), MAX_RATIONALE)
        if decision and response:
            return ({"error": "pass decision or response, not both"}, 400)
        if not decision and not response:
            return ({"error": "decision or response required"}, 400)
        if decision:
            labels = {o.get("label") for o in row["options"] if isinstance(o, dict)}
            if decision not in labels:
                return ({"error": "decision does not match this row's options"}, 400)
        # rationale is required on every ruling; a long-enough free-text
        # response may serve as its own rationale, nothing else does
        rationale_text = rationale if looks_like_a_sentence(rationale) else None
        if not rationale_text and response and looks_like_a_sentence(response):
            rationale_text = response
        if not rationale_text:
            return ({"error": "rationale required: at least one sentence "
                               "(a long-enough free-text response can serve as "
                               "its own rationale; a picked option cannot)"}, 400)
        args = ["--as-user", str(row.get("ownerUserId") or ""), "operator-rule", dr_id]
        args += ["--decision", decision] if decision else ["--response", response]
        args += ["--rationale", rationale_text]
        ok, out_text, err_text = run_cli(args)
        if not ok:
            return ({"error": "gateway call failed", "detail": err_text[:500]}, 502)
        log_ruling(dr_id, decision or response)
        return ({"ok": True, "cli": out_text[:2000]}, 200)

    def _ask_followup(self, dr_id, b):
        if not self._check_operator_token():
            return ({"error": "forbidden"}, 403)
        row = read_decision_row(dr_id)
        if not row:
            return ({"error": "no such decision"}, 404)
        if row.get("kind") != "operator" or row.get("status") != "open":
            return ({"error": "not an open operator decision"}, 409)
        question = clip(b.get("question"), MAX_QUESTION)
        if not question or not question.strip():
            return ({"error": "question required"}, 400)
        raiser_key = row.get("raiserSessionKey")
        if not raiser_key:
            return ({"error": "this row has no raiser session to wake"}, 409)
        prompt = (
            "George has a follow-up on your decision request " + dr_id + " before he rules:\n"
            "\"" + question + "\"\n"
            "Answer by re-asking with `operator-ask … --supersedes " + dr_id + "` and put your "
            "answer in context.note (keep the original note; add a section headed by George's "
            "question). Same options unless the answer changes them. Do not wait for the deadline."
        )
        ok, out_text, err_text = run_cli(["wake", "--session", raiser_key, "--prompt", prompt])
        if not ok:
            return ({"error": "wake failed", "detail": err_text[:500]}, 502)
        with lock:
            state["followups"][dr_id] = {
                "drId": dr_id, "question": question,
                "askedAt": int(time.time() * 1000),
                "originalQuestion": row.get("question"),
                "raisedAt": row.get("raisedAt"), "deadlineAt": row.get("deadlineAt"),
            }
            state["followupsRev"] += 1
            save_tags()
        return ({"ok": True}, 200)

    # ---------- writes ----------
    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        if b is None:
            return self._send({"error": "body too large"}, 413)
        parts = u.path.split("/")
        if (len(parts) == 5 and parts[1] == "api" and parts[2] == "decisions"
                and parts[4] in ("rule", "ask")):
            dr_id = clip(parts[3], MAX_ID)
            handler = self._rule_decision if parts[4] == "rule" else self._ask_followup
            result, code = handler(dr_id, b)
            return self._send(result, code)
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
                # it is dismissed. This never collapses a new card into an
                # existing one: silently returning somebody else's card is
                # state the caller cannot see, and it left agents believing
                # they had filed something they had not. Deduplicating is the
                # CALLER's job -- pass `searchId` to update a card you filed
                # (POST /api/searches), and the pile stays honest about what
                # was actually asked for.
                def _file_search(query, ids, author, source, result_kind):
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

            if u.path == "/api/names":
                state["namesVisible"] = bool(b.get("on", True))
                out = (push("names", on=state["namesVisible"]), 200)

            if u.path == "/api/annotations":
                state["annotationsVisible"] = bool(b.get("on", True))
                out = (push("annotations", on=state["annotationsVisible"]), 200)

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
                        "decision": decision_report(b.get("decision")),
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
    OPERATOR_TOKEN = ensure_operator_token()
    print(f"tb-atc on http://{HOST}:{PORT}/  (web root: {WEB})", flush=True)
    print(f"operator token: {TOKEN_FILE} (mode 600) — set as a cookie on every page load", flush=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.serve_forever()
