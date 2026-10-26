# Payload guarantees for the published gateway

This inventory audits **0.1.9 build 2908**, source
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
