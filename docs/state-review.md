# Display state review and proposed state machine

Independent review (GPT-5.6 Sol, high effort) of every piece of display state in
`web/index.html`, read-only against commit 625755d. Verbatim apart from link paths.

Reviewed commit `625755d` read-only. The tree is clean; I made no changes.

The short diagnosis: `fitTarget` is not choosing the camera target. The failure was a stale `camMark`: `fitSelection()` sees any bookmark as “second press” and restores it before inspecting selection. Agent focus/filter flights previously did not invalidate that bookmark. Commit `625755d`, which landed during this review, now clears it for successful focus and non-empty filter framing ([web:684](`web/index.html:684`), [web:1836](`web/index.html:1836`)). The underlying state ambiguity remains.

## 1. Inventory

Definitions:

- **INDEPENDENT**: user choice, external input, or history that must be remembered.
- **DERIVED**: recomputable from independent state and the current snapshot.
- **REDUNDANT**: stored today but should become derived or disappear.

| State | Class | Assessment |
|---|---|---|
| `DATA` snapshot | INDEPENDENT | External model input. `byId`, `APOS`, meshes, groups, link geometry, tag sprites, and arrow geometry are DERIVED render caches ([web:239](`web/index.html:239`)). |
| `view.selection` | INDEPENDENT | Persistent set chosen by human or API. Its documentation incorrectly calls it purely human-brushed because `/api/select` mutates the same set ([web:251](`web/index.html:251`), [web:1221](`web/index.html:1221`)). |
| `view.focus.key/stage` | INDEPENDENT | Inspection target and depth. It currently remains stored underneath a filter. |
| `view.filter.kind/query/ids` | INDEPENDENT | Base visibility restriction and provenance. |
| `view.input` | INDEPENDENT | Navigate versus brush is genuinely orthogonal to selection and scope. |
| `selecting`, `selOp` | INDEPENDENT transient | Active brush gesture and its add/remove operation. `selRadius` is an independent preference ([web:1476](`web/index.html:1476`)). |
| `hiAgents`, `hiItems` | DERIVED | Effective ghost set. Filter has absolute precedence over focus ([web:463](`web/index.html:463`)). They should be selectors, not mutable globals. |
| Camera position and `controls.target` | INDEPENDENT physical state | Authoritative current pose. |
| `camMark` | INDEPENDENT history | Prior pose required for a round trip. It should exist only inside an explicit fit transaction. |
| `flight` | INDEPENDENT transient | An active animation, but it belongs inside the camera state machine. |
| `view.framing` | REDUNDANT | Attempts to describe camera intent, but can say `none` while a filter is active or `focus` while fitting a filter. It also contains undocumented `filter` and `restoring` values despite its declaration ([web:262](`web/index.html:262`)). |
| `view.fitTarget` | REDUNDANT | Written after target points have already been chosen, then used only to label `framing` and bookmark metadata ([web:1890](`web/index.html:1890`)). It cannot cause target selection directly. |
| `userMoved` | REDUNDANT | Conflates manual suspension, flight interlock, and ambient-orbit enablement. |
| `orbitAngle/R/H/Tgt` | REDUNDANT | Duplicate camera pose/target in polar form. Store an orbit mode and center; derive angle/radius/height from pose. |
| `chromeAim` | DERIVED | Comes from visible edge-pinned DOM. `chromeOff` is transient interpolation output, not semantic view state ([web:619](`web/index.html:619`)). |
| Detail pane visibility/content | DERIVED | Currently based only on single focus, ignoring filter precedence ([web:1453](`web/index.html:1453`)). |
| Tag composer visibility/position | DERIVED | Draft text and `tagColor` are INDEPENDENT form state; visibility should derive from the active inspection ([web:1012](`web/index.html:1012`)). |
| Search field/pill/count/clear button | Mixed | Filter/search draft is INDEPENDENT. Pill, placeholder, count, and button visibility are DERIVED. Current DOM value is a second source of truth during debounce ([web:1075](`web/index.html:1075`)). |
| Hover tooltip | DERIVED | From pointer hit, input mode, and effective scope. Current brush-mode hover ignores scope while brushing cannot select outside it ([web:1462](`web/index.html:1462`)). |
| Theme `T` | INDEPENDENT preference | System preference is external input. Current media changes overwrite a manual theme choice. |
| Legend state | Mixed | User preference is INDEPENDENT; effective collapsed state is DERIVED from preference and viewport. `legendUserSet` plus a DOM class is split state ([web:1004](`web/index.html:1004`)). |
| `tags`, `arrows` | INDEPENDENT external annotation state | Their meshes, opacity, labels, and endpoint placement are DERIVED. Revision counters are transport caches. |
| `spin`, pulses/messages, seen maps | INDEPENDENT temporal presentation state | Orthogonal animation continuity; not semantic navigation state. |
| API sequence/generation/join/report state | INDEPENDENT operational state | Report bodies are DERIVED from canonical viewer state. |
| Server `selection/focused/focusMode` | REDUNDANT flattened telemetry | It is the latest report from whichever page wrote last, not authoritative control state ([server:83](`server/tb-atc-api.py:83`), [server:297](`server/tb-atc-api.py:297`)). |
| Feed/API status DOM | DERIVED | Derived from fetch state and snapshot age. |

The page’s claim that only “two independent things plus one input mode” hold view state is already contradicted by the same object and the surrounding globals ([web:242](`web/index.html:242`)).

## 2. Current transition table

| Event | What happens today | Finding |
|---|---|---|
| `/` | Focuses/selects the search input; no semantic change ([web:1304](`web/index.html:1304`)). | Defined. |
| Search typing | After 220 ms replaces the filter with a user query, recomputes ghosting, and frames matches. | Defined, but focus remains latent underneath. |
| Search × / empty | Clears only `view.filter`; latent focus immediately becomes effective. Camera/framing is not reconciled. × also broadcasts an API clear ([web:1081](`web/index.html:1081`)). | **INCONSISTENT.** |
| `s` | Toggles brush mode and controls/cursor. Does not cancel flights or a fit transaction. | “Camera is frozen” is false for automated flights. |
| `d` | Clears selection and redraws. Does not invalidate or recompute an active selection fit. | **UNDEFINED.** |
| `f` / Space | If `camMark` exists, restores it immediately. Otherwise collects extant selected meshes; selection wins, then effective highlight, then whole org. Creates a bookmark and flight ([web:1890](`web/index.html:1890`)). | Target semantics are partly defined; bookmark validity is not. |
| `t` / system theme change | Rebuilds much of the scene and reapplies view. Active flight targets/bookmarks are retained. | Manual theme can be overwritten by OS change. |
| `l` | Toggles DOM class and permanently disables responsive auto-choice. | Defined but split state. |
| `c` | Asynchronously deletes all tags and arrows from the server. | Defined; no optimistic local change. |
| `[` / `]` / radius slider | Changes brush radius only. | Defined. |
| Node click | Can pick even a ghosted node. Stores/toggles focus, keeps filter, then flies camera. | **INCONSISTENT hybrid**, described below. |
| Background click | Branches on `view.framing`, not focus/filter. Clears focus, never filter; either flies home or resumes orbit at the current pose ([web:1348](`web/index.html:1348`)). | **INCONSISTENT.** |
| Manual OrbitControls start | Sets `userMoved=true`; does not cancel a flight or bookmark. A flight can overwrite the drag and later re-enable orbit. | **UNDEFINED.** |
| Brush down/move/up | Mutates selection immediately inside current `hi` scope. `paintAt()` then reads nonexistent `selected.size` rather than `view.selection.size` ([web:1764](`web/index.html:1764`)). | Definite `ReferenceError` after each hit; selection mutation has already happened. |
| API `select` | Mutates the same selection immediately, including mid-drag, redraws, and reports ([web:1221](`web/index.html:1221`)). | No ownership/provenance. |
| API `focus` | Immediately replaces stored focus. If resolvable, flies and now invalidates `camMark`; if absent from the snapshot, it neither flies nor invalidates the mark. | **UNDEFINED** for missing IDs and brush races. |
| API `filter` | Replaces filter. A non-empty match frames and invalidates `camMark`; zero matches or clear do neither. | **INCONSISTENT.** |
| API `fit` | `on:true` invokes the same ambiguous toggle; `on:false` restores only if a bookmark exists. Server returns success before application ([server:294](`server/tb-atc-api.py:294`)). | Accepted is not applied/visible. |
| Snapshot refresh | Remaps IDs, rebuilds meshes, recomputes highlight/DOM. It does not recompute a current flight destination or active fit ([web:1275](`web/index.html:1275`)). | Camera can cease to frame its claimed subject. |
| Resize/orientation | Recomputes projection, render size, legend, and chrome offset only ([web:2101](`web/index.html:2101`)). | Active fit is not re-solved. |
| API reporting | Every page periodically overwrites one server record. Filter masks stored focus in the report; camera/input are absent ([web:1170](`web/index.html:1170`)). | Multiple viewers are last-writer-wins and indistinguishable. |

### Answers to the eight scenarios

1. **`f` during neighbourhood focus with a selection:** If there is no bookmark, selection wins and is framed. Highlighting remains the neighbourhood. If a bookmark exists, `f` restores instead. Thus “neighbourhood + selection” is insufficient to predict the event; camera transaction state is also required.

2. **`f` twice with a snapshot between:** First press bookmarks and frames. Refresh rebuilds/repositions nodes but does not reframe. Second press restores the stored position, target, and orbit parameters. It does not restore the projection/chrome offset, and the selection can cease to be framed between presses.

3. **Filter active, human clicks a node:** Neither clean replacement nor clean nesting. Filter continues controlling ghosting and API reporting; focus controls detail/composer, focused-item motion, and parts of the camera. On stage 2, `neighbourhoodView()` uses the filter-derived `hi` sets, so it frames the filter rather than the clicked node’s neighbourhood.

4. **Agent focus during brush drag:** It applies immediately, changes the brush’s reachable scope halfway through the stroke, invalidates the bookmark, and starts a camera flight even though brush mode claims the camera is frozen. Prior selection mutations remain.

5. **Background click while filtered:** Filter stays. Focus clears. Camera either flies home or resumes from its current pose depending on stale `view.framing`, not on the filter itself.

6. **`f` with filter active and no selection:** Normally frames filter matches, but calls that state `focus`. With zero matches it can frame a latent focus or the entire org. A zero-match filter also fails to invalidate an older bookmark, so `f` may restore instead.

7. **Escape/back:** There is no Escape transition. Search explicitly documents that choice ([web:1099](`web/index.html:1099`)). Camera return, focus clear, filter clear, brush exit, and selection clear are separate unrelated actions.

8. **Coexistence today:** Almost everything may coexist. Filter wins `hi` and API mode; focus can simultaneously win pane/composer/camera/motion; selection can remain outside scope and selected mesh materials bypass normal ghost shading ([web:1990](`web/index.html:1990`)); brush blocks ambient orbit but not flights; bookmark and flight can coexist with all of them. There is no defensible invariant beyond each individual enum’s legal values.

## 3. Proposed state machine

Use one reducer over five small axes:

```text
AppState
  model:       {snapshot, revision}

  selection:   Set<NodeKey>

  scope:
    filter:    none | {source: human|agent, query|ids}
    focus:     none | {key, depth: single|neighborhood, source}

  interaction:
    mode:      navigate | brush
    gesture:   idle | {operation: add|remove, beforeSelection}
    hoverKey
    brushRadius

  camera:
    motion:    idle | flying
    nav:       ambient | manual | framed
    frame:     none |
               {owner: scope, target: scope} |
               {owner: toggle, target: selection|scope|all,
                returnPose, subjectRevision}
    pose

  ui:
    themePreference
    legendPreference: auto|open|closed
    searchDraft
    tagDraft
    tagColor
    annotations
```

### Scope and rendering invariants

1. `selection`, input mode, UI preferences, and annotations are genuinely independent.

2. Filter and focus may coexist only with one meaning: **focus is an inspection nested within the filter**.

3. Effective semantic scope is:

```text
focusIds ∩ filterIds    when both exist
focusIds                with focus only
filterIds               with filter only
all nodes               with neither
```

4. A human may only click, hover, or brush nodes in effective scope. A focus outside the filter must either clear the filter first or be rejected; it may never create an empty hidden focus.

5. Normal lit nodes equal effective scope. Selection styling is an overlay and does not silently defeat filter opacity.

6. While `f` owns a selection fit, `displayIds = selection`; this is a temporary selection lens. Returning restores the semantic scope unchanged. That makes “fly to the brushed selection” visually truthful even when the base view is filtered/focused.

7. Detail pane and tag composer show only for an effective single focus. During a selection lens they hide and return afterward. Search chrome always describes the base filter. Counts should say, for example, “1 inspected · 12 filtered”.

8. Tags ghost with their target; arrows are full-strength only when both endpoints are in `displayIds`.

### Camera invariants

1. A return pose exists iff `camera.frame.owner === toggle`.

2. First `f`: choose non-empty extant selection, otherwise non-empty effective scope, otherwise all nodes only when unfiltered. Save one return pose.

3. Second `f`: restore that pose and end the transaction.

4. Any unrelated camera intent—node/background framing, filter/focus command, manual drag—cancels the transaction. No stale bookmark survives.

5. Selection mutation or snapshot refresh while fitted to selection re-solves the frame while preserving the original return pose. If the target becomes empty, restore and end.

6. Resize re-solves the current frame against the new clear rectangle. “Exact return” means exact pose/orbit when viewport dimensions are unchanged; pixel-identical projection across a resize is impossible and should not be promised.

7. One flight exists at a time and carries a state revision. Completion callbacks for superseded revisions do nothing.

### Key transitions

| Event | Proposed transition |
|---|---|
| Node click | Require node in effective scope; set/cycle focus; retain filter as parent; cancel toggle fit; frame effective focus. |
| Background click | Clear focus only. Retain explicit filter. Frame remaining filter or all. |
| Set filter | Replace filter, clear focus, cancel toggle fit, frame matches. Zero matches stays visibly “no match”; never fall back to whole org. |
| Clear filter | Remove filter; retain a valid focus if present; otherwise show all. |
| Remote focus/filter/fit during brush gesture | Queue until pointer-up. Commit the human stroke, then apply queued events in command order. |
| Selection change during selection fit | Recompute the fit without replacing `returnPose`. |
| Snapshot | Reconcile IDs, discard missing focus, derive new scopes, re-solve active frame. |
| Manual camera gesture | Cancel flight and toggle transaction; enter `manual`. |
| Back | One reducer event: cancel gesture/exit brush → restore toggle fit → clear focus → clear filter → no-op. Selection is never implicitly destroyed. |
| `d` | Clear selection only; if selection fit is active, restore and end it. |

### API-visible state

The server should retain reports per `clientId`, rather than flattening all pages into one last-writer-wins record. A report should expose the same derived state the renderer uses:

```json
{
  "clientId": "abc123",
  "revision": 42,
  "appliedCommandSeq": 318,
  "selection": [],
  "scope": {
    "filter": null,
    "focus": {"id": "wi_...", "depth": "single", "source": "agent"},
    "effectiveNodes": []
  },
  "presentation": {
    "litNodes": [],
    "camera": {"nav": "framed", "target": "selection", "inFlight": false}
  },
  "input": {"mode": "brush", "dragging": false}
}
```

Compatibility `selected` and `focused` fields can be derived from this. A POST response remains “accepted”; `appliedCommandSeq` tells an agent when it actually reached a viewer. If the product assumes exactly one authoritative screen, designate that viewer explicitly instead of relying on the last reporter.

## 4. Migration

1. **Characterize first.** Add sequence tests for the eight scenarios, missing IDs, zero-match filters, manual movement during flight, and multiple pages. Preserve commit `625755d` as a tactical guard.

2. **Introduce `AppState` and a reducer alongside the globals.** Mirror reducer output into existing fields so rendering remains unchanged.

3. **Centralize selectors.** Replace `computeHighlight`, ad hoc detail checks, brush `inScope`, hover checks, and API report assembly with `effectiveScope()`, `displayIds()`, and `renderModel()`. This also fixes the `selected.size` typo.

4. **Normalize scope.** Map `view.filter` to `scope.filter` and `view.focus` to `scope.focus`; implement the nesting invariant. Stop allowing raycasts against ghosted nodes. Remove latent-focus behavior.

5. **Move all DOM writes behind `renderUI(renderModel)`.** Keep only form drafts/preferences independent. Delete DOM visibility as implicit state.

6. **Install the camera controller.** Map `camMark` to `camera.frame.returnPose`, `flight` to `camera.motion`, and pose/target to `camera.pose`. Recompute on snapshot/resize and cancel on manual or unrelated framing.

7. **Switch API reporting to the normalized state.** Add per-client reports and `appliedCommandSeq`; retain old response fields as derived compatibility data for one release.

8. **Delete redundant state.** Remove `view.framing`, `view.fitTarget`, `userMoved`, `hiAgents`, `hiItems`, duplicated orbit scalars, and direct display-state DOM mutations.

## 5. Product decisions and recommendations

These are choices, not correctness fixes:

- **Click within a filter:** nest/narrow within it. Search establishes context; click inspects one result.
- **Background click while filtered:** clear focus, retain filter. Explicit filters should require explicit release.
- **Remote command during human drag:** defer it. Human pointer ownership should outrank asynchronous camera control.
- **`f` with no selection under a filter:** frame filter results; zero matches is a no-op with feedback.
- **Back behavior:** one ordered `BACK` event, exposed both as Escape where available and as a visible control because fullscreen may consume Escape.
- **Selection outside current scope:** keep it stored, but do not let ordinary selection material defeat ghosting. `f` may temporarily make the selection the display lens.
- **Snapshot during fit:** continuously maintain the claimed framing. A camera label that says “selection” must remain true.
- **Multiple viewers:** expose per-client state. Last-writer-wins telemetry is unsuitable for an agent trying to learn what a particular human sees.

The main design rule is simple: semantic scope answers “what is in play,” selection answers “what has been collected,” input answers “what the pointer does,” and camera answers “where we are looking.” No one of those axes should impersonate another.
