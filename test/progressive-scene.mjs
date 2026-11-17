import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createSceneLoad} from '../web/render-loop.mjs';
import {renderModelStatusElement} from '../web/viewer-semantics.mjs';

const applied = [];
let finished = 0;
function* records() { for (let i=0; i<2200; i++) { applied.push(i); yield; } }
const load = createSceneLoad(records(), () => finished++, {now:()=>0, batchSize:32});
load.advance();
assert.equal(applied.length,32);
assert.equal(finished,0);
load.hold(true);
for (let i=0;i<100;i++) load.advance();
assert.equal(applied.length,32,'held partial scene never advances or reports completion');
load.hold(false);
while (load.pending) load.advance();
assert.deepEqual(applied,Array.from({length:2200},(_,i)=>i),'all records applied exactly once');
assert.equal(finished,1);
load.advance(); assert.equal(finished,1);

let time = 0;
const costly = createSceneLoad(records(),()=>{}, {now:()=>time+=4, budgetMs:6});
const before = applied.length;
costly.advance();
assert.equal(applied.length-before,2,'time budget yields even below record limit');
costly.cancel(); costly.advance();
assert.equal(applied.length-before,2,'superseded revision cannot add later records');

// Cheap records should use the frame budget rather than force a long sequence
// of almost-empty frames. Costly records still yield at the same time limit.
let clock = 0, cheapApplied = 0;
function* cheapRecords(){ for(let i=0;i<2200;i++){cheapApplied++; clock+=.1; yield;} }
const timed = createSceneLoad(cheapRecords(),()=>{}, {now:()=>clock});
timed.advance();
assert.ok(cheapApplied >= 59 && cheapApplied <= 61, 'cheap work uses the six-millisecond budget');
assert.ok(timed.pending, 'a full-size model still paints a real partial board');
const heldCount=cheapApplied;
timed.hold(true); timed.advance();
assert.equal(cheapApplied,heldCount);
timed.hold(false);
while(timed.pending)timed.advance();
assert.equal(cheapApplied,2200);

const hud = {};
renderModelStatusElement(hud,{model:null,streamState:'connecting',loading:true});
assert.match(hud.textContent,/loading.*incomplete/);
renderModelStatusElement(hud,{model:null,streamState:'stale',loading:true});
assert.match(hud.textContent,/stale.*loading.*incomplete/);
const model = {agents:[{id:'agent'}], items:[], generatedAt:1000,
  freshness:{state:'stale'}};
renderModelStatusElement(hud,{model,streamState:'connected',loading:true},1000);
assert.match(hud.textContent,/stale.*loading.*incomplete/);
assert.doesNotMatch(hud.textContent,/1 agents/,'partial board never advertises a final node total');
renderModelStatusElement(hud,{model,streamState:'connected',loading:false},1000);
assert.doesNotMatch(hud.textContent,/loading/);
assert.match(hud.textContent,/1 agents/);

// Opening camera fitting uses the same status chrome as the full loader,
// without ever drawing that temporary measurement state or losing freshness.
const page=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const poseCode=page.slice(page.indexOf('function firstModelPose('),page.indexOf('function flyTo('));
const status={textContent:'stale · loading board (incomplete)',className:'stale',title:'loading'};
const original={...status};
const c=vm.createContext({document:{getElementById:()=>status},renderModelStatusElement,
  openingFrameStatus:{model:null,streamState:'connecting'},
  restingPose:()=>({fitStatus:status.textContent})});
vm.runInContext(poseCode,c);
assert.deepEqual(JSON.parse(JSON.stringify(c.firstModelPose(0))),{fitStatus:'connecting to live model…'});
assert.deepEqual(status,original,'measurement restores the visible incomplete status');
c.restingPose=()=>{throw new Error('fit failed');};
assert.throws(()=>c.firstModelPose(0),/fit failed/);
assert.deepEqual(status,original,'failed fit also restores the status');
