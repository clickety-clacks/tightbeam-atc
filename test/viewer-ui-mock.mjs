import assert from 'node:assert/strict';
import {
  applyModelDelta, captureViewerState, rebuildWithViewerState,
  renderEvidenceMarkup, renderModelStatusElement, restoreViewerState, sceneSignature,
} from '../web/viewer-semantics.mjs';

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
  ring: {status: 'known', freshness: 'rebuilding', observedAt: null,
    source: ['attests'], complete: true},
}}, now);
assert.match(facts, /stage<\/b> · unknown · stale · observed 1m ago · work-items · incomplete/);
assert.match(facts, /ring<\/b> · known · rebuilding · observed unknown · attests · complete/);

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
