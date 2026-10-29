import assert from 'node:assert/strict';
import {
  applyModelDelta, captureHoverScroll, captureViewerState, clientToNdc, ndcToClient,
  fitHoverCard, hoverCardLimits, rebuildWithViewerState, restoreHoverScroll,
  singleHoverNodes, isHoverCardEventTarget,
  renderEvidenceMarkup, renderModelStatusElement, restoreViewerState, sceneSignature,
  resolveHoverNodes,
} from '../web/viewer-semantics.mjs';

/* Pointer and projected pixels share the canvas's CSS-pixel rectangle, even
   when it is offset from or smaller than the browser viewport. */
const canvasRect = {left: 125, top: 37, width: 800, height: 600};
assert.deepEqual(clientToNdc(125, 37, canvasRect), {x: -1, y: 1});
assert.deepEqual(clientToNdc(525, 337, canvasRect), {x: 0, y: 0});
assert.deepEqual(ndcToClient(-1, 1, canvasRect), {x: 125, y: 37});
const reusedNdc = {x: 0, y: 0};
assert.equal(clientToNdc(525, 337, canvasRect, reusedNdc), reusedNdc);
assert.deepEqual(reusedNdc, {x: 0, y: 0});
const clientPoint = {x: 277.25, y: 488.5};
const projectedPoint = clientToNdc(clientPoint.x, clientPoint.y, canvasRect);
const restoredPoint = ndcToClient(projectedPoint.x, projectedPoint.y, canvasRect);
assert.ok(Math.abs(restoredPoint.x - clientPoint.x) < 1e-9);
assert.ok(Math.abs(restoredPoint.y - clientPoint.y) < 1e-9);
for (const rect of [
  {left: 0, top: 0, width: 1440, height: 900},
  {left: 37.5, top: 21.25, width: 390, height: 844},
]){
  const point = {x: rect.left + rect.width * .37, y: rect.top + rect.height * .68};
  const ndc = clientToNdc(point.x, point.y, rect);
  const roundTrip = ndcToClient(ndc.x, ndc.y, rect);
  assert.ok(Math.abs(roundTrip.x - point.x) < 1e-9);
  assert.ok(Math.abs(roundTrip.y - point.y) < 1e-9);
}

const nativeViewport = {left: 0, top: 0, width: 1234, height: 846};
assert.deepEqual(hoverCardLimits(nativeViewport), {width: 1214, height: 826});
assert.deepEqual(fitHoverCard(1100, 1161, 290, 3021, nativeViewport),
  {x: 934, y: 10, width: 290, height: 826},
  'a 3021px hint stays scroll-bounded and its visible card box stays in the viewport');
const smallViewport = {left: 0, top: 0, width: 1024, height: 720};
assert.deepEqual(fitHoverCard(800, 916, 290, 2384, smallViewport),
  {x: 724, y: 10, width: 290, height: 700},
  'a long hint also fits a smaller viewport with its leader anchor in view');
assert.deepEqual(fitHoverCard(0, 900, 180, 120, nativeViewport),
  {x: 10, y: 716, width: 180, height: 120},
  'a short card remains bounded at viewport edges');
const hoverScroll = captureHoverScroll([
  {key: 'i:wi-1', el: {scrollTop: 430}},
  {key: 'a:agent-1', el: {scrollTop: 92}},
]);
const rebuiltHoverCard = {scrollTop: 0};
restoreHoverScroll('i:wi-1', rebuiltHoverCard, hoverScroll);
assert.equal(rebuiltHoverCard.scrollTop, 430,
  'a long card retains its reading position when a model rebuild replaces its mesh');

const itemUnderPointer = {id: 'item'};
const unrelatedAgentUnderPointer = {id: 'agent'};
assert.deepEqual(singleHoverNodes([itemUnderPointer, unrelatedAgentUnderPointer]), [itemUnderPointer],
  'one pointer ray produces one hint, choosing its nearest hit');
assert.deepEqual(singleHoverNodes([]), [], 'an empty pointer ray produces no hint');
const hintCard = {}, hintChild = {}, sceneNode = {};
const hintLayer = {contains: target => target === hintCard || target === hintChild};
assert.equal(isHoverCardEventTarget(hintChild, hintLayer), true,
  'events from inside a hint card are intercepted before canvas picking');
assert.equal(isHoverCardEventTarget(sceneNode, hintLayer), false,
  'scene pointer events remain eligible outside hint cards');

const rebuiltMeshes = new Map([
  ['i:wi-1', {id: 'replacement-item'}],
  ['a:agent-1', {id: 'replacement-agent'}],
]);
assert.deepEqual(resolveHoverNodes(['i:wi-1', 'i:removed', 'a:agent-1'],
  key => rebuiltMeshes.get(key)),
[rebuiltMeshes.get('i:wi-1'), rebuiltMeshes.get('a:agent-1')]);

/* The same model and DOM seam used by the HUD must expose global state, ages,
   and the distinction between feed updates and bounded reconciliation. */
const now = 1_800_000_000_000;
const statusModel = {
  schemaVersion: 1, generation: 'mock-service', revision: 12, generatedAt: now - 120_000,
  gatewayVersion: '0.1.9+2908', metrics: null,
  agents: [{id: 'agent-1', evidence: {
    feed: {status: 'known', freshness: 'stale', observedAt: now - 45_000,
      source: ['firehose'], complete: true},
    turn: {status: 'unknown', freshness: 'rebuilding', observedAt: null,
      source: ['turns'], complete: false},
  }}],
  items: [{id: 'wi-1', title: 'A work item', evidence: {}}],
  links: [], wakes: [], messages: [],
  freshness: {
    state: 'rebuilding', since: now - 120_000, reason: 'partial scope read',
    rebuildIntervalMs: 5_000, lastCompleteAt: now - 600_000,
    reconciliation: {
      mode: 'bounded rotation', pendingScopes: 4, totalScopes: 132,
      scopeBudget: 32, historyScopes: 120, lastSweepAt: now - 4 * 86_400_000,
      estimatedSweepMs: 20 * 86_400_000,
      announcedChanges: 'live', unannouncedChanges: 'swept',
      activeBudgetMs: 1_800_000, activeRefreshMs: 900_000,
      recentWindowMs: 1_800_000, activeBatchLimit: 32,
      active: {scopeCount: 2, unconfirmedScopes: 1,
        oldestConfirmedAt: now - 31 * 60_000, overBudgetScopes: 1},
      history: {scopeCount: 120, unconfirmedScopes: 4,
        oldestConfirmedAt: now - 72 * 60 * 60_000},
    },
  },
};

const hud = {textContent: '', className: '', title: ''};
renderModelStatusElement(hud, {model: statusModel, streamState: 'connected', streamError: null}, now);
assert.equal(hud.className, 'stale', 'active work beyond its age budget must appear stale');
assert.match(hud.textContent,
  /^stale · 1 agents · 1 items · 1 stale · 1 rebuilding · 1 unknown · snapshot age 2m$/);
assert.match(hud.title, /Service freshness: rebuilding/);
assert.match(hud.title, /Announced changes: live .*feed delivery/);
assert.match(hud.title, /Unannounced changes: swept .*confirmation sweeps/);
assert.match(hud.title, /confirmation budget 30m/);
assert.match(hud.title, /active: 2 scopes · 1 with unknown age · oldest known confirmation age 31m/);
assert.match(hud.title, /Terminal-history backstop: 120 scopes · 4 with unknown age · oldest known confirmation age 3d/);
assert.match(hud.title, /estimated rotation 20d/);
assert.match(hud.title, /last sweep 4d ago/);
assert.match(hud.title, /no separate gaze signal/);

/* A connected partial read is shown as rebuilding while the affected item
   still carries its own stale evidence. A small lagging scope must not turn
   the entire map status red. */
const partialModel = structuredClone(statusModel);
partialModel.freshness.state = 'rebuilding';
partialModel.freshness.reason = 'reading published state: recovery';
partialModel.freshness.reconciliation.mode = 'recovery';
partialModel.freshness.reconciliation.pendingScopes = 11;
partialModel.freshness.reconciliation.pendingByResource = {messages: 4, wakes: 7};
partialModel.freshness.reconciliation.totalScopes = 788;
partialModel.freshness.reconciliation.readDiagnostics = {readQueue: {
  pending: 43, pendingByResource: {messages: 35, wakes: 8},
  active: 5, activeByResource: {messages: 3, wakes: 2},
}};
partialModel.freshness.reconciliation.active.overBudgetScopes = 0;
partialModel.freshness.reconciliation.active.oldestConfirmedAt = now - 5 * 60_000;
partialModel.items[0].evidence.identity = {status: 'known', freshness: 'stale',
  observedAt: now - 30_000, unconfirmedSince: now - 120_000,
  source: ['work-items'], complete: false};
const partialHud = {textContent: '', className: '', title: ''};
renderModelStatusElement(partialHud, {model: partialModel,
  streamState: 'connected', streamError: null}, now);
assert.equal(partialHud.className, 'rebuilding');
assert.match(partialHud.textContent,
  /^rebuilding · 11 scopes pending \(messages 4, wakes 7\) ·/);
assert.match(partialHud.textContent, /2 stale/);
assert.match(partialHud.title, /Reconciliation: recovery · 11 pending \/ 788 cycle scopes/);
assert.match(partialHud.title, /Read queue: 43 queued \(messages 35, wakes 8\) · 5 active \(messages 3, wakes 2\)/);
assert.match(partialHud.title, /Service freshness: rebuilding/);
assert.match(partialHud.title, /Reason: reading published state: recovery/);
const disconnectedPartialHud = {textContent: '', className: '', title: ''};
renderModelStatusElement(disconnectedPartialHud, {
  model: partialModel, streamState: 'stale', streamError: 'event stream disconnected; reconnecting',
}, now);
assert.equal(disconnectedPartialHud.className, 'stale');
assert.match(disconnectedPartialHud.textContent, /^stale ·/);
assert.doesNotMatch(disconnectedPartialHud.textContent, /scopes pending/);
assert.match(renderEvidenceMarkup(partialModel.items[0], now),
  new RegExp(`identity<\\/b> · known · stale · unconfirmed since ${new Date(now - 120_000).toISOString()} · observed 30s ago · work-items · incomplete`));

const offlineHud = {textContent: '', className: '', title: ''};
renderModelStatusElement(offlineHud, {
  model: null, streamState: 'stale', streamError: 'event stream disconnected; reconnecting',
});
assert.equal(offlineHud.textContent, 'stale · live model unavailable',
  'ordinary feed disconnect wording stays unchanged');
assert.equal(offlineHud.title, 'event stream disconnected; reconnecting');

renderModelStatusElement(offlineHud, {
  model: null, streamState: 'stale', streamError: 'missing configuration',
  startupWarning: 'Live model unavailable: missing TB_ATC_GATEWAY.',
});
assert.equal(offlineHud.textContent,
  'Warning: Live model unavailable: missing TB_ATC_GATEWAY.');
assert.equal(offlineHud.className, 'stale');

renderModelStatusElement(offlineHud, {
  model: statusModel, streamState: 'connected', streamError: null,
  startupWarning: 'Live feed is unavailable; using periodic snapshots.',
}, now);
assert.match(offlineHud.textContent, /Warning: Live feed is unavailable; using periodic snapshots\.$/);
assert.match(offlineHud.title, /^Warning: Live feed is unavailable/);

const facts = renderEvidenceMarkup({evidence: {
  stage: {status: 'unknown', freshness: 'stale', observedAt: now - 90_000,
    source: ['work-items'], complete: false},
  outage: {status: 'known', freshness: 'unconfirmed', unconfirmedSince: now,
    observedAt: now - 120_000, source: ['assignments'], complete: true},
  ring: {status: 'known', freshness: 'rebuilding', observedAt: null,
    source: ['attests'], complete: true},
}}, now);
assert.match(facts, /stage<\/b> · unknown · stale · observed 1m ago · work-items · incomplete/);
assert.match(facts, /ring<\/b> · known · rebuilding · observed unknown · attests · complete/);
assert.match(facts, new RegExp(`outage<\\/b> · known · unconfirmed since ${new Date(now).toISOString()} · observed 2m ago · assignments · complete`));

/* A feed delta changes scene data, then the same preservation boundary used by
   receiveModel must restore camera and every user-owned scope through rebuild. */
const baseModel = {
  schemaVersion: 1, generation: 'mock-service', revision: 2, generatedAt: 2_000,
  gatewayVersion: null, metrics: null,
  agents: [{id: 'agent-1'}], items: [{id: 'wi-1', title: 'before'}],
  links: [], wakes: [], messages: [],
  freshness: {state: 'live', since: null, reason: null, rebuildIntervalMs: 5_000,
    lastCompleteAt: 2_000, reconciliation: null},
};
const delta = {
  schemaVersion: 1, generation: 'mock-service', baseRevision: 2, revision: 3,
  generatedAt: 3_000, gatewayVersion: null, metrics: null,
  freshness: {...baseModel.freshness},
  changes: {
    agents: {upsert: [], remove: []},
    items: {upsert: [{id: 'wi-1', title: 'after'}], remove: []},
    links: {upsert: [], remove: []},
    wakes: {upsert: [], remove: []},
    messages: {upsert: [], remove: []},
  },
};
const updatedModel = applyModelDelta(baseModel, delta);
assert.equal(updatedModel.revision, 3);
assert.equal(updatedModel.items[0].title, 'after');
assert.notEqual(sceneSignature(baseModel), sceneSignature(updatedModel));

const view = {
  selection: new Set(['i:wi-1', 'a:agent-1']),
  filter: {kind: 'agent', query: 'owner:alice', ids: new Set(['agent-1'])},
  fit: true, input: 'brush', framing: 'focus', fitTarget: 'focus',
};
let activeFocusLayer = {
  subject: {kind: 'node', key: 'i:wi-1', depth: 'neighborhood'},
  lowerPose: {position: {x: 1, y: 2, z: 3}, target: {x: 4, y: 5, z: 6}},
  returnPolicy: 'reframe-parent',
};
let camera = {
  position: {x: 10, y: 20, z: 30}, target: {x: 2, y: 3, z: 4},
  orbitAngle: 0.7, orbitR: 31, orbitH: 20,
};
const saved = captureViewerState(view, activeFocusLayer, camera);
const restore = snapshot => restoreViewerState(view, snapshot, {
  setFocusLayer(layer) { activeFocusLayer = layer; },
  projectFocus() { view.focus = activeFocusLayer?.subject ? {...activeFocusLayer.subject} : null; },
  restoreCameraPose(pose) { camera = structuredClone(pose); },
});

const rebuiltModel = rebuildWithViewerState(saved, () => {
  view.selection.clear();
  view.filter = {kind: 'none', query: '', ids: null};
  view.fit = false; view.input = 'navigate'; view.framing = 'none'; view.fitTarget = 'selection';
  activeFocusLayer = null;
  camera = {position: {x: 0, y: 0, z: 0}, target: {x: 0, y: 0, z: 0}};
  return updatedModel;
}, restore);
assert.equal(rebuiltModel.items[0].title, 'after');
assert.deepEqual([...view.selection].sort(), ['a:agent-1', 'i:wi-1']);
assert.deepEqual(view.filter, {kind: 'agent', query: 'owner:alice', ids: new Set(['agent-1'])});
assert.equal(view.fit, true);
assert.equal(view.input, 'brush');
assert.equal(view.framing, 'focus');
assert.equal(view.fitTarget, 'focus');
assert.deepEqual(activeFocusLayer.subject, {kind: 'node', key: 'i:wi-1', depth: 'neighborhood'});
assert.deepEqual(view.focus, activeFocusLayer.subject);
assert.deepEqual(camera.position, {x: 10, y: 20, z: 30});
assert.deepEqual(camera.target, {x: 2, y: 3, z: 4});
assert.deepEqual(activeFocusLayer.lowerPose.position, {x: 1, y: 2, z: 3});

assert.throws(() => rebuildWithViewerState(saved, () => {
  view.selection.clear(); view.filter.kind = 'none'; activeFocusLayer = null;
  camera = {position: {x: -1, y: -1, z: -1}};
  throw new Error('mock scene build failed');
}, restore), /mock scene build failed/);
assert.deepEqual([...view.selection].sort(), ['a:agent-1', 'i:wi-1']);
assert.equal(view.filter.kind, 'agent');
assert.deepEqual(activeFocusLayer.subject, {kind: 'node', key: 'i:wi-1', depth: 'neighborhood'});
assert.deepEqual(camera.position, {x: 10, y: 20, z: 30});
