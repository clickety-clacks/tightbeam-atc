# Tightbeam Air Traffic Control

A live 3D view of a running [Tightbeam](https://github.com/clickety-clacks/tightbeam)
organization, drawn as a technical diagram: hidden-line wireframes on plotter
paper in the light theme, a vector display in the dark one.

Organization facts come from Tightbeam's published reads and change feed; merge
evidence comes from canonical Git branches. An unavailable answer stays unknown.
Each fact carries its observation time and freshness, including during recovery.

It also has a control plane, so an agent can point a human at something: read
what is selected, focus a node, pin short tags onto agents and work items, and
draw labelled arrows between them.

## Reading the picture

- **Flat discs hanging below the grid are agents**, laid out as the spawner
  tree: the root deepest, leaves at the surface, each family in its own sector.
  Colour is recency — solid blue for an agent that has just acted, fading to
  grey by an hour idle. Size shrinks with depth.
- **Floating solids above the grid are work items.** Altitude and colour show the highest recorded evidence stage, from 0 to 6.
  Missing evidence stays unknown; later evidence does not manufacture earlier steps.
- **On the floor, a ring says what happened.** Black means merge proven by Git. Green means an explicit ready-to-merge verdict.
  Light gray means durably non-code. Yellow means code with unknown merge status.
  A floating item has no merge ring; floor position alone does not prove a merge.
- **An agent taking a turn wears a rippling ring** — one sine travelling round
  a circle, drawn three times at the same frequency and amplitude with the
  period and radius offset slightly, in red, green and blue. Where they coincide
  they sum back to white; where they pull apart you see the fringes. It is
  chromatic aberration on purpose, and it reads as vibration rather than as
  three rings. A queued turn keeps a slow flash instead: waiting should not look
  like effort.
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
showing. Ghosts remain available for hover inspection. Narrow first, then brush.

**Search is deliberately asymmetric.** A human types a query and everything
unmatched ghosts, with the camera framing what remains. An agent achieves the
same display by supplying a list of ids to `/api/filter` — identical ghosting
and framing, but the box shows an *agent search* pill so it is never mistaken
for something you typed. Emptying the box releases either kind.

## Requirements

- Python 3.10 or newer, Git, and the pinned dependency in `requirements.txt`.
- A Tightbeam gateway with protocol-2 change feed and published state reads.
- An approved device login for the feed and state reads, plus the published
  Tightbeam CLI and its target base directory for complete inventory reads.
  The CLI runs as the configured user and can also authorize mutations. ATC
  invokes only the read commands listed below. ATC does not open the CLI's
  credential files or pass their tokens to the browser.

## Run

The canonical branch is `main`. One process serves the model, stream, viewer and
ATC's annotation APIs. It does not read Tightbeam's SQLite database or generate a
static snapshot file.

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    # Supply TB_ATC_GATEWAY, TB_ATC_DEVICE_TOKEN, TB_ATC_CLI,
    # TB_ATC_CLI_BASE_DIR and TB_ATC_USER for the same target gateway.
    .venv/bin/python server/tb-atc-api.py
    # http://127.0.0.1:8787/

`systemd/tb-atc.service` is an example user unit. It expects the checkout and
virtual environment in `~/.local/share/tb-atc` and service configuration in
`~/.config/tb-atc/service.env`. Existing annotation files can be retained by
setting `TB_ATC_STATE` to their path. The former weather generator and watcher
are retired; they are not part of this service.

## Configuration

| variable | meaning | default |
|---|---|---|
| `TB_ATC_GATEWAY` | gateway HTTP(S) base URL | required |
| `TB_ATC_DEVICE_TOKEN` | approved device token for state reads and org-wide feed | required |
| `TB_ATC_CLI` | absolute path to the published package's `bin/tightbeam` | required |
| `TB_ATC_CLI_BASE_DIR` | absolute target gateway base; the CLI reads its own credentials there | required |
| `TB_ATC_USER` | explicit user for those read commands | required |
| `TB_ATC_HOST` | viewer bind address | `127.0.0.1` |
| `TB_ATC_PORT` | viewer port | `8787` |
| `TB_ATC_WEB` | directory holding `index.html` | `../web` |
| `TB_ATC_STATE` | ATC-owned annotations and saved searches | `<web>/../tags.json` |
| `TB_ATC_GIT_CACHE` | service-owned canonical Git cache | beside the annotation file |
| `TB_ATC_HOST_ROUTES` | JSON mapping of recorded checkout hosts to existing SSH destinations | `{}` |
| `TB_ATC_REBUILD_SECONDS` | full reconciliation interval, shown in the viewer | `300` |

The read-only bridge invokes `assignments --state all`, `attests <assignmentId>`,
`assignment-get <assignmentId>`, `list` (for its complete roles array), and
`artifacts`, each with `--as-user <TB_ATC_USER>`. There is no packaged `role-list`
command. The bridge sets `TIGHTBEAM_BASE_DIR` explicitly, removes inherited
endpoint overrides and runs outside any session working directory so the CLI
uses the intended base. Failed reads remain incomplete; they never establish
an empty inventory.

## Freshness and recovery

The service subscribes before reading initial state. Complete collections establish
absence; a failed or partial read removes nothing. Last-known rows remain stale,
and facts that cannot be established remain unknown. The viewer's camera, selection,
focus, scope, pan and layers stay local through model changes and recovery.

Payloads can update only resource classes verified for the connected gateway build.
Other notices trigger a read of the affected row or scope. Sequence gaps, lost
heartbeats and disconnected feeds trigger recovery. Interval rebuilds cover facts
without notices and changes lost before sequence allocation. A connected feed does
not make those facts newer than their last read; their age and the rebuild interval
remain visible.

**On merge detection.** No attest kind means "merged" — an item can hold a
completion, a clean review *and* a verified verdict while its code is still off
the branch. Each recorded `host:path` checkout is used only to read its `origin`;
the service then resolves the commit through a service-owned cache for that
canonical repository. Arbitrary remote branches are never landing proof. The
product-owned integration-ref registry is:

- `clickety-clacks/tightbeam-atc`: `main`
- `clickety-clacks/tightbeam-specs`: `main`
- `clickety-clacks/lachesis`: `main`
- `clickety-clacks/tightbeam`: `main` and `0.1.8`

SSH and HTTPS GitHub origins normalize to the same registered identity. A commit
is merged when any registered ref proves ancestry or patch identity. It is not
merged only when every registered ref was fetched and every probe proves
absence. A missing checkout, unregistered origin, missing commit, or failed
lookup reports unknown rather than guessing.

## Control API

Anything that can reach the service can drive the view. There is no
authentication: it controls a display and nothing else. Bind to localhost unless
you mean otherwise.

Because several agents may annotate one board, `author` is carried on tags and
arrows and the unqualified clear-all verbs are refused: a caller must say whose
notes it is removing, or identify a human reset with `?all=true&source=user`. Per-ID deletion and
saved-card edits use the same author rule. Human actions carry `source=user`;
this is attribution under the existing unsigned API, not authentication. The field is a
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
| `POST` | `/api/filter` | `{ids:[id], query, author}` or `{clear:true}` — ghost everything else, and file it as a card |
| `GET` | `/api/searches` | `[{searchId, query, ids, label, source, author, at}]` |
| `POST` | `/api/searches` | `{searches:[{query\|ids, label, author, searchId?}]}` — file or edit |
| `DELETE` | `/api/searches/<searchId>?author=<who>` | forget your card |
| `GET` | `/api/tags` | `[{tagId, target, text, source, color, author, at}]` |
| `POST` | `/api/tags` | `{tags:[{target, text, source:"agent"\|"user", color, author}]}` |
| `DELETE` | `/api/tags?author=<who>` | clear that author's |
| `DELETE` | `/api/tags?all=true&source=user` | clear every author's |
| `DELETE` | `/api/tags/<tagId>?author=<who>` | clear your tag |
| `GET` | `/api/arrows` | `[{arrowId, from, to, text, source, color, author, at}]` |
| `POST` | `/api/arrows` | `{arrows:[{from, to, text, source, color, author}]}` |
| `DELETE` | `/api/arrows?author=<who>` | clear that author's |
| `DELETE` | `/api/arrows?all=true&source=user` | clear every author's |
| `DELETE` | `/api/arrows/<arrowId>?author=<who>` | clear your arrow |

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
either will silently miss commands. Tags, arrows and saved cards persist in ATC's own annotation file. Missing
authors remain unknown; editing does not rewrite the original author.

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

`GET /api/model` returns the current model. `GET /api/events` streams SSE events
named `model`, each containing the full model. A new connection immediately gets
current state. `/data.json` is a dynamic compatibility alias for `/api/model`;
there is no static-file polling path.

    {
      "schemaVersion": 1,
      "generation": "process identity",
      "revision": 1,
      "generatedAt": 1780000000000,
      "freshness": {
        "state": "live",
        "since": 1780000000000,
        "reason": null,
        "rebuildIntervalMs": 300000,
        "lastCompleteAt": 1780000000000
      },
      "agents": [], "items": [], "links": [], "wakes": [], "messages": []
    }

Revisions increase only for model or evidence-state changes within a generation.
A viewer accepts a new generation and
ignores older revisions from its current generation. Each event is usable on its
own; reconnect does not require replay. Transport loss is shown stale locally
until the stream returns.

Items retain full work IDs, titles, holders, evidence kinds and counts. Raw attest
histories and notes stay in the service's evidence inventories; they are not
included in viewer snapshots. Counts are `null` when the relevant inventory is
incomplete. Their
`stage`, `code`, `merged`, `readyToMerge` and assignment counts may be `null`.
`ring` is `black`, `green`, `light-gray`, `yellow` or `null`. Agents retain stable
session suffix IDs, recorded parents and separate `run`, `wait`, `idle` or unknown
turn state. Pending wakes do not prove queued turns. `idleSince` is the observed
last activity timestamp or `null`; `idleFor` is sampled at `generatedAt`.
Viewers advance live idle age locally and retain stale/unknown indications.
The service schedules removal of recent wake/message marks at their expiry;
elapsed idle seconds do not trigger a new full snapshot.

Each entity's `evidence` maps fact names to `status` (`known` or `unknown`),
`freshness`, `observedAt`, `source` and `complete`. Fact age is measured from
`observedAt`, never from the model's publication time. A full model can contain
retained stale rows after an incomplete rebuild; clients must keep them in scope.

## Tests

Run the tracked wrapper, which invokes the existing suite command unchanged:

    ./scripts/test.sh

Tests exercise evidence meanings, read completeness, recovery, real temporary Git
repositories and the service/viewer transport. Live gateway acceptance uses seeded
scratch state and reports its exact gateway build separately from unit evidence.

MIT licensed.
