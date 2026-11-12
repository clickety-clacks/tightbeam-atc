import assert from 'node:assert/strict';
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
