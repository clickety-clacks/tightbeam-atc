# Tightbeam Org Weather

A live 3D visualization of a running [Tightbeam](https://github.com/clickety-clacks/tightbeam)
organization, rendered as a technical diagram: hidden-line wireframes on
plotter paper (light mode) or a vector display (dark mode).

Everything on screen derives from durable rows in Tightbeam's ledger.
Nothing is estimated.

## The visual grammar

- **Flat discs on the ground = agents**, laid out as the org's spawner tree:
  the root at center, each family fanning out in its own sector. Size shrinks
  with depth, so rank reads at a glance. Retired agents dim.
- **Floating solids = open work items.** Altitude is evidence remaining:
  items enter high and descend one band per satisfied merge-law requirement
  (progress attest, tests-passed, completion, independent review,
  reviewed-clean verdict). Color runs red → green over the same stops.
- **On the floor**: a work item over a **yellow disc** carries all its
  evidence but is **not in the branch yet**; over a **green disc** its code
  is in the branch. We have landed. (Branch state is only distinguished when
  a repo is configured — see `TB_WEATHER_REPO` below. Without it every
  floor item reads as mergeable, which is what the ledger alone can tell
  you: review verdicts prove a gate passed, never that code landed.)
- **Activity**: an item whose outline **flashes** has a turn queued; a soft
  translucent **bubble** breathing around it means a turn is running right
  now. All bubbles breathe in unison. Agent discs flash the same way
  (fast = running a turn, slow = turn queued).
- **Green edges** connect an item to the agent actively working it;
  **yellow edges** to an agent with a queued turn on it.
- **Traveling pulses = wakes**: real agent-to-agent messages, drawn from the
  last two minutes of fired wakes.
- **Red ring** below an item: orphaned (no active holder).
- Click an item or agent to fly to it; click empty space to pull back.
  Press **T** to toggle light/dark.

## Requirements

- A Tightbeam installation (the visualization reads `state.db` read-only).
- Python 3 on the Tightbeam host.
- Any static web server (nginx shown below; `python3 -m http.server` works).
- Optional: `inotify-tools` on the Tightbeam host for event-driven updates
  (without it, the watcher falls back to 5-second polling).

No CDNs: three.js is vendored in `web/`. The page is fully self-contained.

## Install

### 1. The web page

Copy `web/` to a directory your web server serves. Same-host example:

    sudo mkdir -p /var/www/tightbeam-weather
    sudo cp -r web/* /var/www/tightbeam-weather/

nginx site (adjust `server_name`, then symlink into `sites-enabled` and
reload):

    server {
        listen 80;
        server_name weather.example.org;
        root /var/www/tightbeam-weather;
        index index.html;
        location / { try_files $uri $uri/ =404; }
    }

Add TLS with your usual tooling (e.g. `certbot --nginx -d weather.example.org`).
The page shows "sample data (feed unavailable)" until the snapshot feed below
is running.

### 2. The snapshot generator (on the Tightbeam host)

    sudo cp bin/tb-weather-gen bin/tb-weather-watch /usr/local/bin/

Configuration is by environment variable:

| Variable | Meaning | Default |
|---|---|---|
| `TB_BASE_DIR` | Tightbeam base directory | `~/.tightbeam` |
| `TB_WEATHER_OUT` | where `data.json` is written | `/tmp/tb-weather.json` |
| `TB_WEATHER_DEST` | optional `scp` target when the web host is a different machine | unset |
| `TB_WEATHER_KEY` | optional ssh identity for that push | unset |
| `TB_WEATHER_REPO` | optional path to a clone of the code repo; enables merged-vs-pending detection | unset |
| `TB_WEATHER_BRANCH` | branch to test commits against | `origin/main` |

**Optional: real merge detection.** No attest kind means "merged" — an item
can hold a completion, a reviewed-clean verdict, *and* a verified verdict and
still not be on the branch. Point `TB_WEATHER_REPO` at a clone and the
generator tests each commit recorded on an item's attests for ancestry in
`TB_WEATHER_BRANCH` (fetching at most every five minutes), which separates
work that truly awaits a merge from work that merged and merely awaits its
bookkeeping. Leave it unset and merge state is reported as unknown rather
than guessed.

Same machine as the web server — write straight into the web root:

    TB_WEATHER_OUT=/var/www/tightbeam-weather/data.json tb-weather-gen

Web server on a different machine — push over ssh:

    TB_WEATHER_DEST=<user>@<viz-host>:/var/www/tightbeam-weather/data.json \
    TB_WEATHER_KEY=~/.ssh/<key> tb-weather-gen

Run it once by hand first; it prints a one-line summary of what it saw.

### 3. The watcher

`tb-weather-watch` keeps the snapshot fresh: it regenerates when
`state.db-wal` changes (debounced, minimum 3s between runs) and heartbeats
every 60s. Without `inotify-tools` it polls every 5 seconds instead.

Quick start:

    TB_WEATHER_OUT=/var/www/tightbeam-weather/data.json \
    nohup tb-weather-watch >/tmp/tb-weather-watch.log 2>&1 &

Proper install: edit `systemd/tb-weather.service` (user, paths, environment),
then:

    sudo cp systemd/tb-weather.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now tb-weather

## Data contract

The page fetches `data.json` every 5 seconds:

    {
      "generatedAt": 1780000000000,
      "agents":   [{ "id", "name", "kind", "parent", "retired", "turn" }],
      "items":    [{ "id", "title", "stage", "holders": [], "turns": {"<agentId>": "run"|"wait"} }],
      "messages": [{ "from": "<agentId>", "to": "<agentId>" }]
    }

`kind` ∈ main | po | patrol | orch | coder | rev | spec.
`stage` 0–6 per the merge-evidence ladder above. Anything that emits this
shape can drive the page.

## Notes

- The generator opens the database with `mode=ro`; it cannot write.
- Stage derivation is deliberately conservative and row-based; as Tightbeam's
  typed vocabulary grows, the mapping sharpens without page changes.
- Keep your own host names and topology out of this repo if you fork it;
  configuration belongs in environment variables.

MIT licensed.
