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
- **Floating solids above the grid are work items.** Open unfinished work is placed by where it
  stands in its assignment life first. Work not yet started and not carded to anyone floats in a
  cloud high above the org. Work carded to an active agent sits lower, over that agent, with a
  line to it. Work that has started but is not carded to an active agent forms a ring around the
  org's perimeter at middle height. Within each of these, colour shows the furthest evidence
  recorded on any of the item's assignments, from 0 to 4, and lower sits further along. That is
  not completion of the item: open work stays in its tier until the item is closed, and never
  sits on the floor. When assignment state cannot be established, the item is not placed in any
  of them: it waits off to the side with a `?`, and hover says why. Closed, iceboxed and failed
  items keep their own placement. Missing evidence stays unknown; later evidence does not
  manufacture earlier steps.
- **On the floor, a ring says what happened.** Black means merge proven by Git. Green means an explicit ready-to-merge verdict.
  Light gray means durably non-code. Yellow means code with unknown merge status.
  A floating item has no merge ring; floor position alone does not prove a merge.
- **An agent taking a turn wears a colored ring.** A queued turn has a steady
  visible glow, so waiting does not look like effort.
- **A line from an item to an agent is turn state.** Green while a turn is
  executing, yellow while one is queued and has not started, faint ink for an
  open card with no turn at all.
- **A line between agents is an obligation**, with a steady brightness gradient
  pointing toward whoever owes the turn. Yellow while it waits in their queue,
  green once it is running.
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
- A Tightbeam gateway with protocol-2 change feed and published state reads for
  the live model. Without live settings, the map, annotations, searches, and
  controls still start and remain available.
- The `websockets` dependency enables live change-feed updates. If it is
  unavailable, ATC keeps published reads active, uses periodic snapshots, and
  shows a feed warning.
- An existing Tightbeam operator login in the target base directory and the
  published Tightbeam CLI for complete inventory reads. At runtime, ATC consumes
  the provisioned login in place from `gateway.json`, as the CLI does. HTTP reads
  and the org-wide feed use `asUser` with the configured user; CLI reads use
  `--as-user`. ATC invokes only the read commands listed below.
- The gateway must support operator-login reads across state collections, session
  histories and the org-wide feed (the 0.1.11 read change). Older gateways may
  refuse these reads; ATC then reports incomplete/stale evidence through its
  existing recovery path. No device login is required or used.

## Run

The canonical branch is `main`. One process serves the model, stream, viewer and
ATC's annotation APIs. It does not read Tightbeam's SQLite database or generate a
static snapshot file.

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    # For the live model, supply TB_ATC_GATEWAY, TB_ATC_CLI,
    # TB_ATC_CLI_BASE_DIR and TB_ATC_USER for the same target gateway.
    .venv/bin/python server/tb-atc-api.py
    # http://127.0.0.1:8787/

`systemd/tb-atc.service` is an example user unit. It expects the checkout and
virtual environment in `~/.local/share/tb-atc`; service configuration in
`~/.config/tb-atc/service.env` is optional. If it is absent or live settings are
incomplete, the viewer and control APIs still start and the map shows a warning
that live data is unavailable. Configure only paths, the gateway URL and user;
never copy login tokens into the environment or unit file. Credential bytes stay
in the runtime credential consumer and authenticated gateway requests; they do
not enter command arguments, logs, model snapshots or viewer data. Existing
annotation files can be retained by setting `TB_ATC_STATE` to their path. The former weather generator and watcher
are retired; they are not part of this service.

## Configuration

| variable | meaning | default |
|---|---|---|
| `TB_ATC_GATEWAY` | gateway HTTP(S) base URL | required for live model |
| `TB_ATC_CLI` | absolute path to the published package's `bin/tightbeam` | required for live model |
| `TB_ATC_CLI_BASE_DIR` | absolute target gateway base containing the existing operator login | required for live model |
| `TB_ATC_USER` | explicit user for all gateway reads (`asUser` / `--as-user`) | required for live model |
| `TB_ATC_HOST` | viewer bind address | `127.0.0.1` |
| `TB_ATC_PORT` | viewer port | `8787` |
| `TB_ATC_WEB` | directory holding `index.html` | `../web` |
| `TB_ATC_STATE` | ATC-owned annotations and saved searches | `<web>/../tags.json` |
| `TB_ATC_GIT_CACHE` | service-owned canonical Git cache | beside the annotation file |
| `TB_ATC_HOST_ROUTES` | JSON mapping of recorded checkout hosts to existing SSH destinations; malformed values disable remote host routes | `{}` |
| `TB_ATC_REBUILD_SECONDS` | full reconciliation interval, shown in the viewer; malformed values use the default | `300` |

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

Payload processing is not gated by the connected Gateway build. ATC applies its
audited per-resource field and version checks across builds; an unaudited or
unknown build shows a warning while incomplete or conflicting payloads continue
to use targeted reads. The source audit covers selected 0.1.9 build 2908
upserts, and real-feed verification on other builds remains pending. See the
[per-resource guarantee inventory](docs/payload-guarantees.md) for the exact
field/version checks, exclusions and targeted-read gaps.
Other notices trigger a background read of the affected row or scope. Sequence
gaps, lost heartbeats and disconnected feeds trigger complete recovery. Normal
intervals refresh base inventories and rotate through a bounded batch of terminal
histories. Active histories have an independent 15-minute refresh target and a
30-minute confirmation budget; exceeding it marks the scope and model stale.
This backstop covers facts without notices and changes lost before sequencing.
Terminal rotation can take many intervals. The reconciliation metadata reports
separate active/history counts and confirmation timestamps. A connected feed does
not make unannounced changes newer than their last complete read. Per-fact
observation ages remain authoritative. Active-scale throughput is not yet measured.
Feed processing continues while background reads run. Partial reads remove nothing.

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
| `GET` | `/api/status` | whether live data is available and any startup warning |
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

`GET /api/model` returns the current model. `GET /api/events` streams an initial
full `model` event followed by `delta` events for changes, with full resyncs when
needed. `/data.json` is a dynamic compatibility alias for `/api/model`;
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
Every new SSE connection starts with a full `event:model` snapshot, including
reconnects; `Last-Event-ID` is not used for replay. Subsequent `event:delta` frames
carry `generation`, `baseRevision`, `revision`, `generatedAt`, complete replacement
`freshness`, `gatewayVersion` and `metrics`, plus `changes`. Each of `agents`,
`items`, `links`, `wakes` and `messages` has `{upsert: [...], remove: [...]}`.
An upsert replaces the entire stable-ID row; removes contain IDs. Apply a delta
atomically only when its generation matches, its base equals the current revision,
and its revision advances. Malformed rows are skipped individually, with a plain
warning; valid rows in the same snapshot or delta remain available. Unknown model
fields and unsupported freshness metadata also warn without hiding entities. A
missing collection retains its last known same-generation rows, or stays unknown
when no earlier rows exist. Invalid revision identity or a mismatched delta base
still requires a full resync while the current map remains visible.
The service retains up to 64 recent deltas, bounded to 16 MiB of encoded event
data. A subscriber receives the missing delta frames in order when its revision
is still in that history; otherwise it receives a full model resync.
Transport loss is shown stale locally until a full snapshot arrives. Status-only
deltas can have empty collection changes.

`freshness.reconciliation` includes `mode` (`bootstrap`, `recovery`, `interval` or
null), `pendingScopes`, `totalScopes`, `scopeBudget`, `historyScopes`,
`estimatedSweepMs` and `lastSweepAt`. The estimate covers terminal history only,
not active freshness or a guarantee that every read will finish in that time.

Additional reconciliation fields distinguish the two freshness paths:

| Field | Meaning |
|---|---|
| `announcedChanges` | `live` when the feed is connected, otherwise `stale`; independent of backstop completeness. |
| `unannouncedChanges` | `swept`: changes without notices are caught by complete scope reads. |
| `activeBudgetMs` | 1800000: active scopes past 30 minutes without complete reconfirmation are stale. |
| `activeRefreshMs` | 900000: 15-minute scheduling target, leaving execution headroom; not a performance claim. |
| `recentWindowMs` | 1800000: recent activity classification window. |
| `activeBatchLimit` | 32: active scope reads queued per pass, with bounded worker concurrency. |
| `active` | `{scopeCount, unconfirmedScopes, oldestConfirmedAt, overBudgetScopes}`. |
| `history` | `{scopeCount, unconfirmedScopes, oldestConfirmedAt}`. |

`oldestConfirmedAt` is an epoch-millisecond timestamp: the oldest successful
complete scope confirmation in that group, or null when none exists. The viewer
computes current age locally; no clock-only frames are needed. `unconfirmedScopes`
counts scopes with no complete confirmation and therefore unknown age. An empty
group has zero counts and a null timestamp. Detail reads, payloads and partial
reads do not reset scope confirmation time. Ages keep increasing while stale.

Open-assignment attest histories are active. Session turn/wake/message histories
are active for open-assignment holders, running/queued sessions, recent activity,
or incomplete histories that cannot prove inactivity. Other maintained histories
are terminal. Partial reads cannot demote a previously active assignment history.
Previously visible retired sessions remain in terminal reconciliation. Only
successful complete reads reconfirm scopes; the scheduler does not silently
extend the budget when capacity is insufficient.

Long notes are loaded only on request through
`GET /api/items/:id/evidence?offset=0&limit=100`. The response has `generation`,
`revision`, `itemId`, `records`, `total`, `nextOffset`, `complete` and per-fact-style
`freshness`, plus the original `item` and `assignments` detail rows. Attests keep
nullable `bySession`, `byUser` and `note`. Subsequent pages must send the returned
`generation` and `revision` as query parameters; a change returns HTTP 409 with
`error: "revision-mismatch"` and the current pin. Restart from offset zero.
The page limit is 1–100; `nextOffset: null` marks the end. Partial inventories
report only retained records and `complete: false`. Detail-only edits also
advance the model revision, so pages never silently mix versions.

Items retain full work IDs, titles, holders, evidence kinds and counts. Raw attest
histories and notes stay in the service's evidence inventories; they are not
included in viewer snapshots. Counts are `null` when the relevant inventory is
incomplete. Their
`stage`, `code`, `merged`, `readyToMerge` and assignment counts may be `null`.
`ring` is `black`, `green`, `light-gray`, `yellow` or `null`. Agents retain stable
session suffix IDs, recorded parents and separate `run`, `wait`, `idle` or unknown
turn state. Pending wakes do not prove queued turns. `idleSince` is the observed
last activity timestamp or `null`; `idleFor` is sampled at the `generatedAt` of the event that applied that row.
A client retains that sample time across unrelated deltas.
Viewers advance live idle age locally and retain stale/unknown indications.
The service schedules removal of recent wake/message marks at their expiry;
elapsed idle seconds do not trigger a new full snapshot.

Each entity's `evidence` maps fact names to `status` (`known` or `unknown`),
`freshness`, `observedAt`, `source` and `complete`. Fact age is measured from
`observedAt`, never from the model's publication time. A full model can contain
retained stale rows after an incomplete rebuild; clients must keep them in scope.
Item-level turns evidence remains unknown or incomplete while any visible
session's turns inventory is unread, uses the stable source label `["turns"]`,
and leaves `observedAt` null. Per-session sources and timestamps remain on each
agent's turns evidence; aggregate `unconfirmedSince` remains on the item.

## Tests

Run the tracked wrapper, which invokes the existing suite command unchanged:

    ./scripts/test.sh

Tests exercise evidence meanings, read completeness, recovery, real temporary Git
repositories and the service/viewer transport. Live gateway acceptance uses seeded
scratch state and reports its exact gateway build separately from unit evidence.

MIT licensed.
