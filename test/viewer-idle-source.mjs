import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {
  sceneSignature, prepareModelSnapshot, shouldAcceptSnapshot, shouldAcceptModel,
  agentSampleTimes, idleAgeSeconds, renderEvidenceMarkup,
} from '../web/viewer-semantics.mjs';

const model = {
  schemaVersion:1, generation:'source-test', revision:1, generatedAt:1000000,
  freshness:{state:'live'}, agents:[{
    id:'agent-S', sessionKey:'S', name:'Agent', idleSince:950000, idleFor:50,
    evidence:{idleFor:{status:'known', complete:true, freshness:'live', observedAt:950000,
      unconfirmedSince:null, source:['cli:attests A']},
      identity:{status:'known', source:['sessions']}}
  }], items:[], links:[], wakes:[], messages:[],
};
const update = (revision, source) => {
  const next = structuredClone(model);
  next.revision = revision;
  next.generatedAt += revision;
  next.agents[0].evidence.idleFor.source = source;
  return next;
};
const next = update(2, ['/api/sessions/S/turns']);
assert.equal(sceneSignature(model), sceneSignature(next));
assert.deepEqual(model.agents[0].evidence.idleFor.source, ['cli:attests A'],
  'signature calculation must not mutate published evidence');
for (const change of [
  m => { m.agents[0].evidence.idleFor.complete = false; },
  m => { m.agents[0].evidence.idleFor.status = 'unknown'; },
  m => { m.agents[0].evidence.identity.source = ['other']; },
  m => { m.agents[0].name = 'Changed'; },
  m => { m.links = [{id:'link', from:'agent-S', to:'item'}]; },
]) {
  const changed = structuredClone(model); change(changed);
  assert.notEqual(sceneSignature(model), sceneSignature(changed));
}
const itemModel = {...model, items:[{id:'item', evidence:{idleFor:{source:['old']}}}]};
const itemChanged = structuredClone(itemModel);
itemChanged.items[0].evidence.idleFor.source = ['new'];
assert.notEqual(sceneSignature(itemModel), sceneSignature(itemChanged),
  'only agent idleFor.source is excluded');

// Execute the actual page update, reference refresh, node content and panel
// functions. Mock only their scene/DOM boundaries; count rebuild/hover effects.
const page = fs.readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
function extract(start, end) {
  const first = page.indexOf(start), last = page.indexOf(end, first);
  assert.ok(first >= 0 && last > first, start);
  return page.slice(first, last);
}
const mesh = {userData:{a:model.agents[0]}};
const detail = {style:{}, innerHTML:''};
const calls = {scene:0, links:0, hoverReset:0};
const noop = () => {};
const context = vm.createContext({
  console, Map, Set, Date, sceneSignature, prepareModelSnapshot, shouldAcceptSnapshot,
  shouldAcceptModel, agentSampleTimes, idleAgeSeconds,
  firstBoardComplete:true,openingFrameStatus:null,startupWarning:null,userMoved:false,
  DATA:null, lastModel:null, lastSceneSignature:null, lastRenderedModel:null,
  agentSamples:new Map(), itemEvidencePages:new Map(), itemEvidenceNotices:new Map(),
  pendingEvidenceReloads:new Set(), pendingSceneRollbackView:null, deferredSceneSyncError:null,
  streamState:'connected', streamError:null, sceneTime:0, finishedDays:30,
  hoverLeaders:[], view:{focus:{stage:1}, selection:new Set(), framing:'custom'},
  focusLayer:null, byId:{}, agentMeshes:{'agent-S':mesh}, itemMeshes:[],
  selectables:() => [], paintAgentMesh:noop, shadeSelected:noop, selKey:() => '',
  deferredSceneUpdates:{run:fn => fn()}, captureHoverScroll:() => null,
  captureModelPose:() => ({}), captureViewerState:() => ({}), restoreModelPose:noop,
  rebuildWithViewerState:(_state, build) => build(), restoreViewerModelState:noop,
  clearHover:() => {calls.hoverReset++; context.hoverLeaders = [];},
  buildTieredScene:agents => {calls.scene++; mesh.userData.a = agents[0];},
  buildLinks:() => {calls.links++;}, restoreHoverByKey:noop,
  migrateViewKeys:noop, pruneWindowSelection:noop, applyView:noop,
  renderModelStatus:noop, spawnWakes:noop, spawnMessages:noop,
  resumePendingEvidenceReloads:noop, requestRender:noop,
  itemInWindow:() => true, focusMesh:() => mesh,
  document:{getElementById:id => id==='detail' ? detail : {getBoundingClientRect:() => ({bottom:0})}},
  matchMedia:() => ({matches:true}), innerHeight:900,
  esc:value => String(value ?? ''),
  factText:(_entity, _fact, value, render) => render(value),
  evidenceMarkup:entity => renderEvidenceMarkup(entity, 1000000),
});
const code = extract('function refreshModelReferences(model){', 'function receiveModel(')
  + extract('function receiveModel(model,', '\nfunction ')
  + extract('function nodeContent(u, compact){', '\nasync function loadItemEvidence(');
new vm.Script(code).runInContext(context);
assert.equal(context.receiveModel(model), true, context.streamError);
context.renderDetail();
assert.match(detail.innerHTML, /cli:attests A/);
context.hoverLeaders = [{key:'a:agent-S'}];
const hover = context.hoverLeaders;
const baseline = {...calls};
assert.equal(context.receiveModel(next), true, context.streamError);
assert.deepEqual(calls, baseline, 'source-only update must not rebuild scene/links or reset hover');
assert.equal(context.hoverLeaders, hover);
assert.match(detail.innerHTML, /\/api\/sessions\/S\/turns/);
assert.doesNotMatch(detail.innerHTML, /cli:attests A/);

context.view.focus.stage = 0;
context.renderDetail();
assert.equal(detail.style.display, 'none');
const closedUpdate = update(3, ['cli:attests B']);
assert.equal(context.receiveModel(closedUpdate), true, context.streamError);
assert.deepEqual(calls, baseline);
context.view.focus.stage = 1;
context.renderDetail();
assert.match(detail.innerHTML, /cli:attests B/);
assert.doesNotMatch(detail.innerHTML, /\/api\/sessions\/S\/turns/);

const geometryUpdate = structuredClone(closedUpdate);
geometryUpdate.revision = 4;
geometryUpdate.agents[0].name = 'New name';
assert.equal(context.receiveModel(geometryUpdate), true, context.streamError);
assert.equal(calls.scene, baseline.scene + 1);
assert.equal(calls.links, baseline.links + 1);
assert.equal(calls.hoverReset, baseline.hoverReset + 1);
