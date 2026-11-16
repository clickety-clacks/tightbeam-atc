import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import {createSceneLoad, createDeferredUpdateGate} from '../web/render-loop.mjs';
import {sceneSignature,prepareModelSnapshot,shouldAcceptSnapshot,shouldAcceptModel,
  agentSampleTimes,idleAgeSeconds,renderEvidenceMarkup} from '../web/viewer-semantics.mjs';
const page=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const model={schemaVersion:1,generation:'busy',revision:1,generatedAt:1000,
 freshness:{state:'live'},agents:Array.from({length:40},(_,i)=>({id:'a'+i,name:'Agent '+i})),
 items:[],links:[],wakes:[],messages:[]};
const mesh={userData:{a:model.agents[0]}}, detail={style:{},innerHTML:''};
const noop=()=>{}, calls={scene:0,links:0,hoverReset:0};
let drawn=0, starts=0, context;
context = vm.createContext({
  console, Map, Set, Date, sceneSignature, prepareModelSnapshot, shouldAcceptSnapshot,
  shouldAcceptModel, agentSampleTimes, idleAgeSeconds,
  firstBoardComplete:false,openingFrameStatus:null,startupWarning:null,userMoved:false,
  firstSceneUpdates:null,completingFirstScene:false,firstLoadTask:null,firstLoadHeld:false,
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
  buildTieredScene:agents => {calls.scene++; drawn = agents.length; mesh.userData.a = agents[0];},
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
context.firstSceneUpdates=createDeferredUpdateGate(()=>!context.firstBoardComplete && !!context.firstLoadTask?.pending);
context.createSceneLoad=(steps,finish)=>createSceneLoad(steps,finish,{now:()=>0,batchSize:4});
context.firstSceneSteps=function*(m){starts++;drawn=0;for(const a of m.agents){drawn++;yield;}};
context.renderDetail=noop;
const start=page.indexOf('function receiveModel(model,');
vm.runInContext(page.slice(start,page.indexOf('function scheduleModelStreamRetry(',start)),context);
assert.equal(context.receiveModel(model),true);
const task=context.firstLoadTask;
let previousCount=0, revision=1, frames=0;
while(task.pending){
 task.advance();frames++;
 assert.ok(drawn>=previousCount,'a new model must not erase drawn first-load progress');
 previousCount=drawn;
 if(task.pending){
  const next=structuredClone(model);next.revision=++revision;
  next.agents[0].name='Revision '+revision;
  assert.equal(context.receiveModel(next,{fullSnapshot:revision%2===0}),true);
  assert.equal(context.DATA.revision,revision,'model ingestion remains prompt');
  assert.equal(context.firstLoadTask,task,'updates keep the running build');
 }
 assert.ok(frames<=11,'continuous updates cannot starve completion');
}
assert.equal(starts,1,'one initial build despite a model update every frame');
assert.equal(context.firstBoardComplete,true);
assert.equal(context.lastRenderedModel.revision,revision,'latest accepted revision reconciles before completion');
assert.equal(mesh.userData.a.name,'Revision '+revision);
assert.equal(drawn,40);
assert.equal(calls.scene,1,'only one catch-up reconstruction');
assert.equal(calls.links,1);
assert.equal(context.firstSceneUpdates.pending,false);
// After completion, normal immediate updates still run.
const next=structuredClone(model);next.revision=++revision;next.agents[0].name='After load';
assert.equal(context.receiveModel(next),true);
assert.equal(context.lastRenderedModel.revision,revision);
assert.equal(calls.scene,2);
