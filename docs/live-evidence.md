# Live evidence inventory

Implementation audit for Tightbeam **0.1.9 build 2908**, source
[`8433d22fdf86027d62a34e9923501775225813ad`](https://github.com/clickety-clacks/tightbeam/tree/8433d22fdf86027d62a34e9923501775225813ad).
This is a source audit, not a successful gateway acceptance report.
Real-feed observations and remote verification are pending.

## Read, payload, and recovery paths

| Resource | Published inventory reads | Notice path | Reason |
|---|---|---|---|
| Work items | All pages of `/api/work-items` | Full payload by strict version; `/api/work-items/:id` on field/version gaps | Transactional metadata versions order supported mutations. Equal-version disagreements use reads. |
| Assignments | Packaged CLI `assignments --state all` | Packaged CLI `assignment-get` for the named assignment; read newly introduced attest scopes and holder histories | Corrections are included in both CLI reads. Reopen transitions cannot be ordered safely by assuming timestamp-based versions are monotonic. |
| Attests | Packaged CLI `attests <assignmentId>` for every assignment | Full immutable payload; named history on gaps and named assignments for completion/review effects | Append-only insertion and closed shared evidence fields are source-qualified. Original authors and notes remain intact. |
| Sessions | All pages of `/api/sessions` | Full payload by strict version; `/api/sessions/:key` on gaps and missing visible-session histories | Public mutations and topology use a strict per-session stamp. Interval reads still recover unannounced changes. |
| Turns | All pages of `/api/sessions/:key/turns` for visible sessions | `/api/turns/:seq` | Started and ended timestamps establish actual running/queued state. A pending wake cannot substitute for a turn. |
| Wakes | All pages of `/api/sessions/:key/wakes` for visible sessions | `/api/wakes/:id` | Mutable state; targeted read preserves the current row. |
| Messages | All pages of `/api/sessions/:key/messages` for visible sessions | `/api/sessions/:key/messages/:id` | The source contains message rewrites. The feed registry's append-only label alone is insufficient. |
| Roles | Complete `roles` array from packaged CLI `list` | Repeat that read on role notices | The packaged CLI has no `role-list` verb. `list` maps to the published inspect read, which includes all roles. Only the roles array enters this inventory. |
| Decision requests | All pages of `/api/decision-requests` | Full statute/agent/effort payloads by strict version; `/api/decision-requests/:id` for other shapes/gaps | Stored trigger covers the closed R7 projection; operator projection is separate. |
| Artifacts | Complete packaged CLI `artifacts` command | Full canonical payload by strict version; `/api/artifacts/:id` when the CLI baseline lacks its version floor | Durable floors order wire metadata. CLI inventory omits the floor. Artifact contents remain outside the evidence model. |
| Canonical merges | Explicit canonical repository/branch registry, ancestry and patch equivalence | Recheck changed commit references asynchronously; expire resolver caches | Git failure is unknown. A removed checkout can recover only through proof in a registered canonical repository. |
| Annotations and saved cards | ATC-owned JSON persistence | Existing annotation APIs with author attribution | Rebuilding upstream evidence does not replace authored marks. |

`PAYLOAD_BUILDS` selects source-qualified policies only for the exact
`("0.1.9", 2908, "8433d22f")` identity. A different build uses targeted reads.
The [per-resource guarantee inventory](payload-guarantees.md) cites the precise
field, ordering and deletion guarantees or missing guarantees for every resource
ATC maintains. All policies still require the expected class, operation, primary
identity and complete field set; none permits direct deletion. These authored
policies are based on published source, **not real-feed acceptance evidence**.
The required scratch-gateway proof remains pending, as do all test executions.

CLI commands run with an explicit user and target base. The packaged CLI owns
credential discovery; ATC does not open its credential files or read its token.
REST and feed traffic use the separate device bearer. CLI read failures become
incomplete results, and diagnostics are not copied into viewer responses.

The CLI source exposes every required inventory above, including commit-reference
corrections. This is a source finding; an exact-package execution remains pending.
Bootstrap and feed-gap recovery still cover every required scope. Normal intervals
refresh the six base inventories and at most 32 existing history scopes in a fair
rotation; newly discovered scopes are additional actual gaps. Reads run on a
bounded worker pool, leaving feed application on the model worker. A notice
received during a read invalidates that read's epoch; the stale response is
discarded and reread instead of replacing newer evidence. Targeted reads normally
go first, with every fourth dispatch reserved for waiting background work.

The exact CLI exposes `attests <assignmentId>` with a complete history, but no
bulk or incremental attest read. Therefore the scope budget bounds scheduled
history reads, not response bytes, history length, or pages inside one scope.
Complete inventories are never replaced with a recent-history approximation.
Base inventories also have no fixed row-count bound. Actual cost remains unmeasured.
With 16,000 history scopes, 32 per 300-second interval implies a nominal 41.7-hour
rotation, potentially longer with slow or failed reads. Feed notices still take
the targeted path immediately. This is a visible freshness tradeoff, not evidence
of five-minute recovery for every unannounced historical change. The model exposes
`historyScopes`, `scopeBudget`, `estimatedSweepMs`, `lastSweepAt`, and per-fact
`observedAt`; the estimate is not a completion guarantee.

The service subscribes before reading initial state. It processes incoming
changes while published reads execute in the background.
Immutable identities deduplicate; strict versions prevent older buffered rows
from replacing newer reads. Incomplete payload projections use the targeted
read path, even when their resource policy is enabled.

All pages must succeed before a collection replaces its inventory. An
incomplete read retains previous rows, records stale evidence, and cannot
prove absence. A successful detail read establishes that row only, never the
completeness of its collection. Unknown schema versions fail the read.

Disconnects, missing sequence numbers, protocol errors and stalled heartbeat
delivery mark evidence stale. Reconnection starts a fresh subscription and
rebuild. Rolling interval reconciliation recovers unannounced corrections,
topology changes, repairs and lost notices. An unfinished reconciliation does not
overlap another interval. Normal interval reads do not invalidate the entire map;
failed scopes retain their rows as stale or unknown. A disconnected feed still
invalidates all evidence, and successful reads cannot make it live while offline. A heartbeat sequence can include frames allocated but still queued,
so an ahead watermark alone does not establish loss.

Viewer publication compares derived model content and evidence state before
advancing the revision. Raw attest rows/notes stay internal; snapshots carry
actual evidence kinds, nullable counts, commit references and corrections.
Changes stream as ordered stable-ID upsert/remove deltas with full replacement
metadata. A new or lagging subscriber receives a full model snapshot; the service
retains only the latest delta. Full encoding is lazy and shared across subscribers.
Partial inventories cannot remove entities from uncertain projections. Timed
recent-mark expiry removes a visual mark, not its underlying source record.
Original notes and correction reasons are available through revision-pinned
on-demand evidence pages. Detail-only edits advance the model revision; mismatched
pagination returns HTTP 409 instead of mixing source versions. Idle age is
derived in the viewer from `idleSince`; recent mark expiry and reconciliation
have explicit deadlines. The five-minute clean-review rule compares recorded
attest timestamps, so it has no wall-clock expiry.

## Primary sources

- [Canonical public serializers and natural row versions](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex).
- [Read routes, page cursors and envelopes](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/wire/router.ex).
- [Packaged CLI verb arguments](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/cli/src/args.rs),
  [dispatch and endpoint discovery](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/cli/src/dispatch.rs),
  [assignment and correction inventories](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/assignments.ex),
  and [inspect's roles array](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/gateway.ex).
- [Feed resource registry](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/firehose/registry.ex)
  and [publisher projections](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/firehose/publisher.ex).
- [Work-item transactional row-version increments](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/work_items.ex).
- [Message rewrite during credential-failure settlement](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/terminal_credential_failure.ex).
- [Change socket protocol](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/wire/change_socket.ex).
- [Published package and checksums](https://github.com/clickety-clacks/tightbeam/releases/tag/v0.1.9%2B2908).

The downloaded macOS ARM64 package has SHA-256
`97f2960da8daf67ec316bdab0f246b1d304a52165ed5366500b8aced731fab1b`,
matching the release's `SHA256SUMS`. Both the external and bundled provenance
records identify the source commit above. These checks establish package
identity; they do not establish runtime correctness.

## Verification still required

Run the unchanged tracked `scripts/test.sh` wrapper in the permitted isolated
environment. Record the service revision, Python and dependency versions,
scratch gateway `/version`, release provenance and package checksum. Use a
fresh base, its own device login, packaged CLI using that base, and seeded records.

Real-response evidence must cover bootstrap completeness, admitted payload
classes, targeted gaps, partial/failed reads, disconnect/sequence recovery,
unannounced interval recovery, and event-to-model latency at representative
scale. Record request counts and payload counts separately. Viewer interaction
and retention need verification on the exact integrated service/viewer subject.
No synthetic test result substitutes for these observations.
