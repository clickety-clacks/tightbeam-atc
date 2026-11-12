# Payload guarantees for the published gateway

## Work-item summary and observed landing audit (0.1.15)

The work-item summary addition is audited against Tightbeam source
[`b09a64a939ca00a86033fd5294b6a8dc6968df5d`](https://github.com/clickety-clacks/tightbeam/tree/b09a64a939ca00a86033fd5294b6a8dc6968df5d),
PR #555, which identifies as `0.1.15` build `3866` with source stamp `b09a64a9`.
The audit covers the summary fields on work-item collection/detail reads and
full `work_item.updated` payloads. No 0.1.15 real-feed acceptance test has run.

The exact source defines these fields:

| Field | Source meaning |
|---|---|
| `holders` | Unique session keys with open assignments on the item. |
| `started` | At least one assignment exists; this remains true after assignments close. |
| `attestCounts` | Counts of all attest kinds associated with the item. |
| `verdictCounts` | Counts of verdict kinds, including future vocabulary. |
| `effectiveCommitRefs` | Deduplicated refs, preferring each assignment's correction over its attest refs. |
| `landings` | Durable observed merge records whose merge commit or reviewed head matches an effective commit ref. Each has repository, branch, merge commit, reviewed head, pull request and the condition-fact observation time. |

The work-item row version is advanced when these summary values change.
Assignment, attest, correction and observed-landing mutations publish a complete
updated work-item row when the corresponding feed path requests it. Read paths
attach the summary to visible work items. The source association checks commit
identity; it does not claim current branch containment, and the summary does not
expose the condition fact's `origin` field. An empty `landings` list means no
matching observed record is available; it is not a negative merge result.

ATC consumes the summary when the complete field set is present. It keeps raw
attest reads where the existing stage rule needs timestamp order, refreshes a
scope if the summary counts do not match the retained detail, and no longer
rotates every terminal attest scope on the 56-hour history backstop. A 0.1.14
Gateway has no summary fields, so it retains the legacy reconstruction and Git
resolver paths, including their existing targeted reads and warnings, until a
compatible Gateway is separately released and installed. Other resource
payloads remain covered by the older 0.1.9 audit, so the global compatibility
warning remains on 0.1.15 even though its work-item summary fields are consumed.

The remaining resource field/version inventory below is the earlier source
audit for Tightbeam 0.1.9 build 2908; it does not extend the 0.1.15 audit to
other resources.

This older inventory audits **0.1.9 build 2908**, source
[`8433d22fdf86027d62a34e9923501775225813ad`](https://github.com/clickety-clacks/tightbeam/tree/8433d22fdf86027d62a34e9923501775225813ad).
It is a source audit. **No real-feed acceptance test has run.** Its field and
ordering policies apply across Gateway builds; `/version` identity
`("0.1.9", 2908, "8433d22f")` identifies the audited source and no longer gates
payload use. ATC warns when the connected build is different or unknown, while
each payload still has to pass its class, operation, identity, field and version
checks. A gap uses a targeted read. Behavior on other Gateway builds remains
unverified. The compile-time stamp is defined in
[build_stamp.ex](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/build_stamp.ex#L1).
Package/runtime identity must still be reconfirmed before acceptance execution.

## Shared envelope and ordering limits

The [registry](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/firehose/registry.ex#L24)
names classes, resources, operations and primary references. ATC subscribes with
`filters: {}` and does not depend on secondary-reference filtering. The
[publisher](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/firehose/publisher.ex#L60)
captures canonical transactional rows for supported handlers and queues notices
for post-commit delivery. Its
[wire encoder](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/firehose/publisher.ex#L373)
permits some legacy partial shapes: a resource mapping alone is **not** a
complete-payload guarantee. ATC requires the known class, upsert operation,
primary identity match and all required fields before applying a payload.

[Hub matching_frames](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/firehose/hub.ex#L361)
assigns protocol-2 sequence numbers per subscription connection. Those numbers
order delivery, not a collection read or a reconnect. Overflow disconnects and
rebuilds; there is no assumption of replay across connections. ATC rejects a
read overtaken by a notice using its local inventory epoch, and applies mutable
payloads only against a comparable row version. Equal-version disagreement,
missing baseline version, missing fields or unknown classes trigger a named
row/scope read. No payload establishes collection completeness or removes a row.

## Resources ATC maintains

| Resource / classes | Fields and version guarantee in this source | Authored payload policy / actual gap reads |
|---|---|---|
| Work items: `work_item.created`, `.updated`, `.iceboxed`, `.reopened`, `.closed`, `.failed` | Public metadata/state mutations reserve a strict version with `MAX(proposed, previous+1)`; the transactional publisher queries the canonical work item. Required shape is the complete work-item field set. Internal bracket IDs also appear in that shape but can change without this metadata stamp; equal-version disagreement must not overwrite a read. | Apply complete upserts by strict version. Equal-version disagreement or missing/versionless baseline uses `/api/work-items/:id`. Unannounced bracket/body metadata remains subject to interval reads. No deletion policy. [W1–W3] |
| Assignments: `assignment.opened`, `.closed`, `.reopened` | Serializer supplies the public assignment projection with a natural timestamp-derived version. Reopening can clear terminal timestamps; neither strict monotonic order across reopening nor a retained identity generation is established. `commitRefCorrections`, needed by ATC's Git resolution, are not in the feed assignment field set. | Read only the named `assignment-get` on notices, plus newly missing attest/holder-history scopes. Complete `assignments --state all` interval reads recover corrections and other unannounced changes. These are current-version ordering/field gaps. [A1–A3] |
| Attests: `attest.filed` | Registry explicitly declares append-only; insert path creates a unique ID and appends evidence. The closed attest serializer includes id, assignmentId, kind/verdict, note, original bySession/byUser, producer/command, harness/provider, commitRefs and ts. CLI and payload normalize to this shared set; wire rowVersion is not used to order immutable identities. | Apply complete upserts by immutable identity. A differing duplicate, missing field, or correlation gap reads that assignment's attests. A missing assignment is read by ID. A completion may change the assignment and a review verdict may settle its producer; read those named assignment rows because the attest payload does not include their resulting state. Progress payloads need no such read. [T1–T4] |
| Sessions: `session.spawned`, `.updated`, `.harness_changed`, `.retired` | Org's mutation seam compares the public session projection, stamps `max(now, previous+1)`, and publishes once per actual change. Retirement also stamps; topology reparenting uses the same seam. Session read/feed projections include recorded topology and the same version. Derived catalog capabilities are not an independent source-row version; equal-version disagreement falls back to a read. | Apply the full session field set by strict version. New visible sessions still require missing turn/wake/message histories; a session payload cannot establish history completeness. Other partial/conflicting notices read `/api/sessions/:key`. [S1–S4] |
| Turns: `turn.started`, `.ended` | Canonical turn payload is available, but rowVersion is the maximum natural timestamps/seq, not a per-mutation counter. Same-millisecond transitions and changes to fields outside those timestamp choices have no strict-order guarantee. Notice classes do not by themselves inventory queued turns. | Target `/api/turns/:seq` on notices; published session turn walks establish complete scopes. Retain unknown queued/idle facts until complete. [N1–N2] |
| Wakes: `wake.scheduled`, `.fired`, `.canceled` | Closed wake shape exists, but natural timestamp version does not version all mutable delivery/scheduling fields. No monotonic mutation counter or global complete feed inventory is guaranteed. | Target `/api/wakes/:id`; complete session wake walks on bootstrap/recovery/rotation. Pending wake does not prove a queued turn. [N1, K1] |
| Messages: `message.created` | Full canonical message projection is required, with primary messageId and sessionKey. Registry calls it append-only, but the terminal-failure path updates content/timestamp of an existing ID and republishes `message.created`; rowVersion stays seq. This disproves immutable-row and strict-mutable-version assumptions. | Target `/api/sessions/:key/messages/:id`; complete session walks handle unannounced changes. Missing session correlation stays unknown; there is no global message-detail route to invent. [M1–M3] |
| Roles: `role.created`, `.bound`, `.removed` | Closed projection exists. Binding writes wall-clock updatedAt without a strict counter. Removal snapshots a version greater than the old row, but no retained tombstone floor orders subsequent same-name recreation against delayed deletes or reads. | Read the complete `roles` array from packaged CLI `list`. It is the affected small resource scope, not a whole-model refetch. A complete read establishes removal; feed delete is not directly applied. [R1–R2] |
| Decision requests: `decision_request.opened`, `.updated`, `.answered`, `.returned`, `.ruled`, `.consumed`, `.withdrawn`, `.superseded` | Closed R7 fields for statute/agent/effort are covered by a stored positive rowVersion trigger on changed projection fields, including agent return fields. Operator rows use a separate open public serializer; its entire extra field set is not covered by that closed-shape guarantee. | Apply full statute/agent/effort upserts by strict version; agent requires returnedBy/returnReason/returnedAt. Operator, partial, unknown-kind or version-conflicting rows use `/api/decision-requests/:id`. All-page reads establish completeness. [D1–D3] |
| Artifacts: `artifact.recorded`, `.archived`, `.released` | Durable artifact_version_floors reserve strictly increasing versions, including custody transitions, and the publisher uses canonical rows with those floors. Closed wire fields include original createdBySession. Packaged CLI `artifacts` uses the legacy column list and omits the floor, so it cannot supply an ordered baseline. | Apply full upserts when no prior row exists or the prior row has a comparable canonical version. After a CLI inventory, read `/api/artifacts/:id` for an actual missing-baseline-version gap; subsequent payloads can apply directly. Interval CLI reads still establish the whole inventory. [F1–F3] |

There is **no direct payload deletion policy** for these resources. Role removal
uses a complete authoritative roles read. Other unexpected delete operations
fall back to reads and cannot infer absence from a 404 or an incomplete walk.
No published global bulk/incremental attest read was found: the CLI exposes only
`attests <assignmentId>`. Rotating complete scopes therefore has the cost/freshness
tradeoff documented in [live-evidence.md](live-evidence.md).

Announced changes use the payload/read paths above promptly. Unannounced changes
use a separate backstop: active histories target a complete reconfirmation every
15 minutes and become stale at 30 minutes without one. Terminal histories retain
the bounded interval rotation and may have multi-day ages. Payloads and detail
reads update individual facts but cannot reset the complete-scope confirmation
clock. Active/history counts and oldest confirmation timestamps are separate in
the full-model/delta metadata; the nominal 41.7-hour example applies to 16,000
terminal scopes, not current-activity freshness or measured startup performance.

Users, devices, read markers, condition facts, critical-state, config, host
environment, hosts, identity and kungfu do not supply facts to this ATC model.
They are outside its maintained resource set, not denied source guarantees.
Observational classes (including topline invalidations) have no ATC state-row
application. This version uses no unpublished global collection or Toplines routes.
Git merge facts come from the configured Git resolver; ATC annotations remain
ATC-owned and never come from gateway payloads.

## Exact primary sources

All links below are fixed to the audited source commit; source tests mentioned
here are inspected specifications, **not tests executed for this delivery**.

- W1: [work-item version stamp and read projection](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/work_items.ex#L1083).
  W2: [metadata comparison/stamp](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/work_items.ex#L263).
  W3: [bracket changes outside that stamp](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/work_items.ex#L675).
- A1/N1: [closed resource field sets](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex#L80).
  A2: [natural version fallback](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex#L2834).
  A3: [complete assignment corrections](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/assignments.ex#L1012).
- T1: [append-only registry declaration](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/firehose/registry.ex#L231).
  T2: [attest insert and original authors](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/assignments.ex#L3951).
  T3: [closed attest projection](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex#L1416).
  T4: [completion effects](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/assignments.ex#L2736) and [review producer settlement](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/assignments.ex#L3070).
- S1: [session mutation and strict stamp](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/org.ex#L1618).
  S2: [retirement stamp](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/org.ex#L980).
  S3: [reparenting through mutation seam](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/session_reparent.ex#L225).
  S4: [canonical session fields/version query](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex#L18).
- N2: [transactional canonical turn notice](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/firehose/publisher.ex#L240).
  K1: [wake serializer and natural version](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex#L1423).
- M1: [canonical message serializer](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex#L1578).
  M2: [message seq as rowVersion](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex#L2393).
  M3: [existing-message rewrite and republish](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/terminal_credential_failure.ex#L953).
- R1: [role binding timestamps](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/roles.ex#L115).
  R2: [role delete snapshot, no persistent floor](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/roles.ex#L154).
- D1: [R7 stored-version trigger and covered fields](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/escalation.ex#L218).
  D2: [closed decision projection](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex#L2429).
  D3: [separate operator serializer](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/state_resources.ex#L1455).
- F1: [durable artifact version floor and canonical read](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/artifacts.ex#L118).
  F2: [versioned custody transitions](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/artifacts.ex#L631).
  F3: [CLI inventory without floor](https://github.com/clickety-clacks/tightbeam/blob/8433d22fdf86027d62a34e9923501775225813ad/lib/tightbeam/artifacts.ex#L421).

## Evidence still missing

No real feed payload, observed ordering/deletion/reconnect trace, event latency,
read-count benchmark, or scale/cold-start measurement has been collected. All
new tests are authored and unrun. Published source guarantees justify the selected
algorithms; they are not a substitute for the required scratch-gateway execution
on the exact integrated service/viewer revision. That execution remains held.
Acceptance must also measure realistic active-scope count, achieved active
confirmation time and read load. If the 30-minute budget cannot be achieved at
acceptable load, the observed shortfall must be reported; no slower budget is
silently substituted.

## Item placement time sources

The 0.1.15 source audit above establishes that work-item collection/detail rows
have no close/icebox timestamp. `WorkItems.trace_result` exposes `JobTrace.build`
through the read-only `work-item-trace` command; its `causal_event` entries include
`disposition_transition`, `at`, `seqTiebreak`, `jobRef` and `detail.toState`.
ATC requests that optional read for closed/iceboxed items when their recorded
version changes. The latest matching disposition supplies an exact finish time.
A missing, unavailable or mismatching transition uses the latest recorded item,
assignment, assignment-linked or jobRef-only turn, attest or artifact activity,
explicitly approximate.
Trace reads are limited to items whose `rowVersion` could fall within the maximum
30-day window: every disposition advances that version, so an older version is a
safe upper bound. Missing or invalid versions remain eligible. Activity maxima
are cached by assignment, attest, artifact and turn inventory revisions; unchanged
publications do not rescan those historical rows. Changed reads or feed payloads
recompute the maxima, including removals and reassignment.
A trace failure does not block the core inventory. No trace is read for failed
placement. The audit is source evidence, not a new live-Gateway observation.

`WorkItems.stamp_version_in_txn` advances `rowVersion` from wall-clock milliseconds
(or the preceding value plus one within a millisecond); it supplies item-update
activity, not an exact state-transition time. Assignment `closedAt` remains a
separate fact and is never called an exact work-item close.

Landing windows use the earliest durable landing `observedAt`. Otherwise the Git
resolver supplies a commit timestamp only for an identified integration merge:
the referenced merge itself, or the first first-parent descendant that merges the
reference from outside its first parent. Direct commits and patch-equivalence
proofs without an identified merge have unknown landing time. Git-check
`observedAt` never supplies a window timestamp. Optional date lookup failure
leaves merge proof intact. Stored Git landing times survive restart.


## Failure acceptance in controlled viewer models

Current upstream records do not reliably distinguish a direct operator failure
or acceptance from a session acting under a user identity, and do not link a
failure to a structured operator decision. ATC does not infer acceptance from
reason text, user attribution or a mentioned decision ID. Its production model
therefore supplies no acceptance evidence; every failed item remains unaccepted.
No upstream record or API change is implemented here.

Isolated served-model tests exercise acceptance using an ATC viewer evidence
shape: `failureAcceptance: {complete: true, records: [...]}`. A record identifies
`id`, `itemId`, `source` (`operator-action` or `operator-decision`), boolean
`accepted`, and optional epoch-ms `at`. These source labels mean evidence of the
direct operator action or linked decision, not merely attribution to a user.
This is a normalized viewer contract, not a claim that today's upstream offers
such records. Any future upstream mapping requires its own supported contract.

Matching records that both accept and reject are conflicting; no source outranks
another. Incomplete evidence, missing records, unknown sources and conflicts are
unaccepted. Detail names the evidence or states its absence/conflict; actual
conflicts must be returned for product-owner judgment. Current records cannot
produce a conflicting acceptance case.

For an accepted test-model failure, exact finish time is the later of `failedAt`
and its first acceptance timestamp. If either timestamp is unknown, the window
uses `latestActivityAt`, explicitly approximate. Missing approximate time remains
outside the window. All unaccepted failures are exempt from the window, whether
landed or not: attached items retain their position, unknown attachment stays off
the ring with `?`, and known-unattached items use the age ring with a geometric
cross independent of annotation visibility. Merge/stage evidence is unchanged.
