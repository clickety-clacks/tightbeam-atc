---
name: tightbeam-atc
description: Drive the Tightbeam Air Traffic Control view — a live 3D display of the running org. Read what a human is looking at, narrow the view to a set or a search, point at nodes, pin tags, draw labelled arrows between nodes, and file named searches. Use when asked to show, point at, highlight, annotate, connect, search, or look at something in the visualization, or to find out what the user is currently looking at.
---

# Air Traffic Control

A live 3D display of the running Tightbeam org: agents hang below a grid, work
items float above it and descend as evidence accumulates, and lines show turns
and obligations. It is driven by a small unauthenticated HTTP API, so any agent
on the box can point the human at something.

- **Page:** <http://127.0.0.1:8787/>
- **API base:** `http://127.0.0.1:8787/api`

## Read the help first

```bash
curl -s localhost:8787/api/help
```

**That endpoint is the authority on HOW.** It ships with the service, so it can
never be out of date with the running version; this skill is only a map of WHAT
is possible. Where the two disagree, the help is right and this file is stale.
`?format=json` gives the endpoint list as data.

## What you can do with it

**Find out what the human is looking at.** The view reports their selection and
whatever a focus or filter is lighting, as two separate things. When someone
says "these" or "this one" with nothing else identifying them, they mean what
is *selected* — read it before answering, not after.

**Narrow the view** to a set of nodes you name, or to a text query, which runs
the same match a human gets by typing. Everything unmatched ghosts and the
camera frames what is left.

**Name what you searched for.** Every search is filed as a card the human can
click to run again. Give it a name that says what was ASKED — "everything
blocking the 0.1.8 cut" — not what matched. That name is the only thing they
read when deciding whether to re-open your search an hour later.

**Point at one thing.** Focus a node, or its whole neighbourhood.

**Select, and fly to it.** Add to the human's selection, and frame it.

**Annotate.** Pin a tag to a node. Draw a labelled arrow between two nodes for
a relation — blocks, owns, caused by. Both carry your name and a colour.

**Read the population.** `data.json` carries every agent — with its archetype,
harness and model — and every work item, with its evidence stage and whether
its code actually reached the branch.

**Show or hide agent names.** `POST /api/names {"on":false}` hides the
agent-name field everywhere it appears on the view (label, hover, focus pane,
tag/arrow authorship, a work item's turn list); `{"on":true}` shows it again.
Role/archetype, id and everything else about an agent stay as they are — this
toggles one display field, not the underlying data. `GET /api/state` carries
the current `namesVisible` value; check it before flipping if you are not
sure which way it is set, and say what you changed.

**Show or hide annotations.** `POST /api/annotations {"on":false}` hides every
tag and arrow (geometry, heads, labels); the data stays, new ones are still
accepted, `{"on":true}` restores rendering. `GET /api/state` carries
`annotationsVisible`.

**Read the Desk.** `data.json`'s `decisions[]` is every OPEN operator decision
request — an agent asked, only George can answer, it expires on a deadline.
It comes straight from `state.db` (read-only), never from polling the
`tightbeam` CLI on a cadence (that grew state.db to 4.9GB and crashed a VM —
clickety-clacks/tightbeam#10 — do not reintroduce that pattern here or
anywhere else in this repo). The generator, not you, owns `author=atc:desk`
arrows and tags mirroring each open, not-yet-expired request onto the board;
you can read them like any other, but don't author your own under that name.
ATC never rules — it shows the exact `operator-rule` command to copy, ruling
itself is Roci Desk's job.

## The three rules that matter

**Say who you are.** Every tag, arrow and search takes an `author`. It renders
on the card, and it is how you remove your own work without taking anyone
else's — several agents annotate the same board at once, and the unqualified
clear-all verbs are refused for exactly that reason.

**Read before you write.** What the human already has selected is usually a
better subject than one you pick yourself.

**Do not fight for the camera.** Tagging is passive; focusing moves their view.
Prefer the first, announce the second, and clean up when the question is
closed.

## Checking it is alive

```bash
systemctl --user is-active tb-atc
curl -s localhost:8787/api/state
```

If the page says "feed unavailable" the snapshot generator is down, not the API.

## Source

<https://github.com/clickety-clacks/tightbeam-atc>
