# Tightbeam Air Traffic Control

A live 3D view of a running [Tightbeam](https://github.com/clickety-clacks/tightbeam)
organization, drawn as a technical diagram: hidden-line wireframes on plotter
paper in the light theme, a vector display in the dark one.

Everything on screen derives from durable rows in Tightbeam's ledger. Nothing is
estimated. Where the ledger cannot answer a question, the view says unknown
rather than guessing.

It also has a control plane, so an agent can point a human at something: read
what is selected, focus a node, pin short tags onto agents and work items, and
draw labelled arrows between them.

## Reading the picture

- **Flat discs hanging below the grid are agents**, laid out as the spawner
  tree: the root deepest, leaves at the surface, each family in its own sector.
  Colour is recency — solid blue for an agent that has just acted, fading to
  grey by an hour idle. Size shrinks with depth.
- **Floating solids above the grid are work items.** Altitude is evidence
  remaining: they descend a band per satisfied requirement (progress, tests,
  completion, independent review, a clean verdict), and colour runs red to green
  over the same stops.
- **On the floor, a ring says what happened.** Yellow: reviewed but not in the
  branch. Green: in the branch, or closed. Grey: nothing to merge.
- **A line from an item to an agent is turn state.** Green while a turn is
  executing, yellow while one is queued and has not started, faint ink for an
  open card with no turn at all.
- **A line between agents is an obligation**, carrying a white pulse that runs
  toward whoever owes the turn. Yellow while it waits in their queue, green once
  it is running.
- **A travelling dot is a wake firing** — one agent's prompt reaching another.
  **A strobing line is a message**: substrate messages rise from below, operator
  messages descend from above.
- **A red ring around an item** means orphaned: no live holder.
- **Tags** are short labels pinned above a node. A drawn mark carries
  provenance — a hexagon for an agent, a bust for a person — and a named
  `author` renders as a caption beneath the text, so a board carrying several
  agents' notes says which one wrote each. Colour is the author's choice from
  seven named values that resolve per theme.
- **Arrows** are annotations of a *relation* rather than a node: a thick curve
  between two nodes, drawn heavier than any substrate line because it is
  someone pointing rather than the org's own traffic. A short label rides the
  curve letter by letter; one too long to fit falls back to the same card a tag
  draws. Arrows ghost with their endpoints — an arrow is only at full strength
  when both ends are.

## What is in force

Under the title bar, one chip per active display layer, outermost first, each
in its own colour: a **search**, a **fit** (`f`/space, showing the selection), a
**focus** or its **neighbourhood**, and **brush** mode alongside them. The chip
with the filled dot is the layer actually deciding what you see; the outlined
ones are still in force beneath it and are what you return to when you leave.

Entering a mode renders that mode: focus a node and everything else ghosts,
open its neighbourhood and the neighbourhood is what is lit. Leaving one puts
back exactly what was true before it, camera included. `docs/display-rules.md`
records the decisions.

## Keyboard

| key | |
|---|---|
| `T` | light / dark |
| `/` | search — ghosts everything unmatched and frames the rest |
| `S` | select mode — drag a circular brush across nodes |
| `[` `]` | brush radius |
| `D` | clear selection |
| `F` / space | fly to the selection; press again to return exactly where you were |
| `L` | legend |
| `C` | clear every tag and arrow, whoever wrote them |

There is deliberately no Escape binding: browsers claim that key in fullscreen,
so it never reaches the page. The search field has an × to clear it, a
background click clears focus, and `S` again leaves brush mode.

Click a node to focus it and fade the rest; click again for its neighbourhood;
click the background to reset. While a single node is focused, a box appears for
typing a tag onto it.

**Selection follows scope.** With nothing focused or filtered the brush reaches
anything; inside a focus or a filter it can only reach what that scope is
showing, which is the same rule hover follows. Narrow first, then brush.

**Search is deliberately asymmetric.** A human types a query and everything
unmatched ghosts, with the camera framing what remains. An agent achieves the
same display by supplying a list of ids to `/api/filter` — identical ghosting
and framing, but the box shows an *agent search* pill so it is never mistaken
for something you typed. Emptying the box releases either kind.

## Requirements

- A Tightbeam installation. The view reads `state.db` read-only.
- Python 3. Nothing else: no build step, no package manager, no CDN. three.js is
  vendored.
- Optional: `inotify-tools` for event-driven updates, and a clone of your code
  repository if you want merge state checked against a real branch.

## Install

One process serves both the page and the API.

    sudo mkdir -p /opt/tb-atc
    sudo cp -r web server/tb-atc-api.py /opt/tb-atc/
    sudo cp bin/tb-weather-gen bin/tb-weather-watch /usr/local/bin/

Run it:

    TB_ATC_WEB=/opt/tb-atc/web python3 /opt/tb-atc/tb-atc-api.py
    # http://127.0.0.1:8787/

Then keep the snapshot fresh, writing into the same web root:

    TB_WEATHER_OUT=/opt/tb-atc/web/data.json tb-weather-watch &

`systemd/tb-atc.service` and `systemd/tb-weather.service` are user units for
both. Copy them into `~/.config/systemd/user/`, adjust the paths, then
`systemctl --user enable --now tb-atc tb-weather`.

### Serving from an existing web server instead

The page is static, so `web/` can be served by anything. Point the generator at
that document root and proxy `/api` to the control service:

    server {
        listen 80;
        server_name atc.example.org;
        root /var/www/tb-atc;
        location / { try_files $uri $uri/ =404; }
        location /api/ { proxy_pass http://127.0.0.1:8787/api/; }
    }

The generator can also push the snapshot to another machine over ssh; see
`TB_WEATHER_DEST` below.

## Configuration

| variable | meaning | default |
|---|---|---|
| `TB_ATC_HOST` | bind address | `127.0.0.1` |
| `TB_ATC_PORT` | port | `8787` |
| `TB_ATC_WEB` | directory holding `index.html` | `../web` |
| `TB_ATC_STATE` | file tags and arrows are persisted to | `<web>/../tags.json` |
| `TB_BASE_DIR` | Tightbeam base directory | `~/.tightbeam` |
| `TB_WEATHER_OUT` | where the snapshot is written | `/tmp/tb-weather.json` |
| `TB_WEATHER_DEST` | optional `scp` target when the web root is another machine | unset |
| `TB_WEATHER_KEY` | ssh identity for that push | unset |
| `TB_WEATHER_REPO` | clone of the code repo, enabling merged-vs-pending detection | unset |
| `TB_WEATHER_BRANCH` | branch to test commits against | `origin/main` |

**On merge detection.** No attest kind means "merged" — an item can hold a
completion, a clean review *and* a verified verdict while its code is still off
the branch. With `TB_WEATHER_REPO` set, each item's recorded commits are tested
for ancestry and patch identity against `TB_WEATHER_BRANCH`, so work that landed
as a cherry-pick under a rewritten SHA is recognised. Leave it unset and merge
state reports unknown rather than guessing.

## Control API

Anything that can reach the service can drive the view. There is no
authentication: it controls a display and nothing else. Bind to localhost unless
you mean otherwise.

Because several agents may annotate one board, `author` is carried on tags and
arrows and the unqualified clear-all verbs are refused: a caller must say whose
notes it is removing, or ask for `?all=true` deliberately. The field is a
signature rather than a credential — nothing verifies it — and it exists so the
view can show provenance and so one caller's cleanup does not take another's
work with it.

| verb | path | body |
|---|---|---|
| `GET` | `/api/state` | selection, tags, generation, sequence |
| `GET` | `/api/selection` | `{selected:[…], focused:{mode, nodes:[…]}}` — brushed vs highlighted |
| `POST` | `/api/select` | `{add:[id], remove:[id], clear:bool}` |
| `POST` | `/api/focus` | `{id, mode:"single"\|"neighborhood"\|"clear"}` |
| `POST` | `/api/fit` | `{on:bool}` |
| `POST` | `/api/filter` | `{ids:[id]}` or `{clear:true}` — ghost everything else |
| `GET` | `/api/tags` | `[{tagId, target, text, source, color, author, at}]` |
| `POST` | `/api/tags` | `{tags:[{target, text, source:"agent"\|"user", color, author}]}` |
| `DELETE` | `/api/tags?author=<who>` | clear that author's |
| `DELETE` | `/api/tags?all=true` | clear every author's |
| `DELETE` | `/api/tags/<tagId>` | clear one |
| `GET` | `/api/arrows` | `[{arrowId, from, to, text, source, color, author, at}]` |
| `POST` | `/api/arrows` | `{arrows:[{from, to, text, source, color, author}]}` |
| `DELETE` | `/api/arrows?author=<who>` | clear that author's |
| `DELETE` | `/api/arrows?all=true` | clear every author's |
| `DELETE` | `/api/arrows/<arrowId>` | clear one |

Ids are the feed's own: work items `wi_...`, agents their session suffix
`s_...`. Both are stable across snapshots, so external callers can address them.

    curl -s localhost:8787/api/selection

    curl -s -X POST localhost:8787/api/focus \
      -H 'content-type: application/json' \
      -d '{"id":"wi_1a2b3c4d","mode":"neighborhood"}'

    curl -s -X POST localhost:8787/api/tags \
      -H 'content-type: application/json' \
      -d '{"tags":[{"target":"wi_1a2b3c4d","text":"review stale","source":"agent"}]}'

**Semantics worth knowing before building on it.** Commands are a broadcast log:
every open page applies them, and a page opened later replays recent history.
Selection is telemetry rather than truth — the last page to report wins, so with
several pages open it reflects whichever reported most recently. The service
carries a `generation` that changes on restart and reports `gap: true` when a
client has fallen further behind than the retained history; a client ignoring
either will silently miss commands. Tags live in memory and are capped, which is
the right lifetime for a pointing device.

## The operator skill

`skill/SKILL.md` is written for agents rather than people. Drop it into an
agent's skill directory and it can drive the view: find out what a human is
looking at, tag what it found, and frame something when asked. It carries the
etiquette as well — read the selection before choosing your own subject, prefer
tagging over seizing the camera, say what you tagged, and clean up afterwards.

    mkdir -p ~/.claude/skills/tightbeam-atc ~/.codex/skills/tightbeam-atc
    cp skill/SKILL.md ~/.claude/skills/tightbeam-atc/
    cp skill/SKILL.md ~/.codex/skills/tightbeam-atc/

Adjust the host and port inside it if you did not use the defaults.

## Data contract

The page fetches `data.json` every five seconds:

    {
      "generatedAt": 1780000000000,
      "agents":   [{ "id", "name", "kind", "parent", "retired", "turn", "idleFor" }],
      "items":    [{ "id", "short", "title", "stage", "holders", "turns",
                     "flow", "merged", "code", "closedAt" }],
      "links":    [{ "id", "from", "to", "state" }],
      "wakes":    [{ "id", "from", "to" }],
      "messages": [{ "id", "from", "to", "kind" }]
    }

`stage` runs 0–6 over the evidence ladder. `merged` is `true`, `false`, or
`null` for unknown. `id` is the full work item id — the one the substrate's CLI
will accept — and `short` is a display form the cards use; the control API
takes either and resolves short ids against the feed. Anything emitting this
shape can drive the page.

MIT licensed.
