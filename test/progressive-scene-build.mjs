import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {register} from 'node:module';
import * as THREE from '../web/three.module.js';
import * as tiers from '../web/work-item-tiers.mjs';
import {factValue, knownStage, ringMeaning} from '../web/viewer-semantics.mjs';
import {unknownItemType} from '../web/viewer-unknown-status.mjs';
import {createSceneLoad} from '../web/render-loop.mjs';
register('data:text/javascript,'+encodeURIComponent(`export async function resolve(name,context,next){
  if(name==='three')return {url:${JSON.stringify(new URL('../web/three.module.js',import.meta.url).href)},shortCircuit:true};
  return next(name,context);
}`),import.meta.url);
const {LineMaterial}=await import('../web/addons/lines/LineMaterial.js');
const {LineSegmentsGeometry}=await import('../web/addons/lines/LineSegmentsGeometry.js');
const {LineSegments2}=await import('../web/addons/lines/LineSegments2.js');
const page=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const builders=page.slice(page.indexOf('function* groundSteps('),page.indexOf('function restartFirstScene('));
const agents=Array.from({length:80},(_,i)=>({id:`agent-${i}`,kind:'coder',parent:i?'agent-0':null,
  evidence:{parent:{status:'known',complete:true}}}));
const items=Array.from({length:220},(_,i)=>({id:`item-${i}`,short:`item-${i}`,state:'open',stage:2,
  holders:i%2?[`agent-${i%80}`]:[],assignments:{open:i%2?1:0,terminal:1},
  evidence:{assignments:{complete:true},stage:{status:'known',complete:true}}}));
const model={agents,items,links:[{from:'agent-1',to:'agent-2',state:'run'}]};
function context(){return vm.createContext({THREE,...tiers,factValue,knownStage,ringMeaning,
  unknownItemType,LineMaterial,LineSegmentsGeometry,LineSegments2,AGENT_R:2.2,TETHER_SEGS:8,
  byId:{},APOS:{},agentMeshes:{},itemMeshes:[],
  groundGroup:new THREE.Group(),itemGroup:new THREE.Group(),tetherGroup:new THREE.Group(),linkGroup:new THREE.Group(),
  T:{stage:[0,1,2,3,4,5,6],arcA:1,arcB:2,orphan:3,tetherSched:4,tetherRun:5,tetherWait:6},
  canvasBounds:{width:1440,height:1000},edgeMats:new Set(),
  disposeGroup:g=>g.clear(),clearHover(){},paintAgentMesh(){},agentActivityColor:()=>1,
  labelSprite:()=>new THREE.Sprite(),
  diagramMesh:(geometry,color)=>new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({color})),
});}
const full=context(),partial=context();
vm.runInContext(builders,full);vm.runInContext(builders,partial);
full.buildTieredScene(agents,items);full.buildLinks(model.links);
const scene=c=>({agents:Object.values(c.agentMeshes).map(m=>[m.userData.a.id,m.position.toArray(),m.material.color.getHex()]),
  items:c.itemMeshes.map(m=>[m.userData.w.id,m.position.toArray(),m.material.color.getHex(),m.userData.placement?.tier,m.children.some(ch=>ch.userData.orphanRing)]),
  links:c.linkGroup.children.map(m=>[m.userData.from,m.userData.to,m.userData.state]),
  arcs:c.groundGroup.children.filter(m=>m.userData.arc).map(m=>[m.userData.from,m.userData.to]),
  tethers:c.tetherGroup.children.map(m=>[m.userData.itemMesh.userData.w.id,m.userData.hid,m.userData.state])});
let done=false;
const task=createSceneLoad(partial.firstSceneSteps(model),()=>done=true,{now:()=>0,batchSize:17});
task.advance();
assert.equal(done,false);assert.equal(Object.keys(partial.agentMeshes).length,17);
assert.equal(Object.keys(partial.byId).length,80,'holder facts cover the full revision before any item is drawn');
while(task.pending){
  task.advance();
  for(const mesh of partial.itemMeshes){
    const complete=full.itemMeshes.find(m=>m.userData.w.id===mesh.userData.w.id);
    assert.ok(mesh.position.equals(complete.position));
    assert.equal(mesh.userData.placement?.tier,complete.userData.placement?.tier);
    assert.equal(mesh.children.some(ch=>ch.userData.orphanRing),complete.children.some(ch=>ch.userData.orphanRing));
  }
}
assert.equal(done,true);
assert.deepEqual(JSON.parse(JSON.stringify(scene(partial))),JSON.parse(JSON.stringify(scene(full))));

// Drawing successive partial boards must not allocate a fresh tether buffer
// for unchanged endpoints. Moving endpoints still updates geometry and bounds.
const updateCode=page.slice(page.indexOf('function updateTetherEndpoints('),page.indexOf('function tickFrame('));
vm.runInContext(updateCode,partial);
const tether=new LineSegmentsGeometry().setPositions([0,0,0,0,0,0]);
const buffer=tether.attributes.instanceStart.data;
const from=new THREE.Vector3(1.25,2.5,3.75),to=new THREE.Vector3(4.5,5.25,6.5);
partial.updateTetherEndpoints(tether,from,to);
const version=buffer.version;
for(let i=0;i<200;i++)partial.updateTetherEndpoints(tether,from,to);
assert.equal(tether.attributes.instanceStart.data,buffer);
assert.equal(tether.attributes.instanceEnd.data,buffer);
assert.equal(buffer.version,version,'unchanged frames do not re-upload tethers');
to.set(-2.25,4.75,9.5);
partial.updateTetherEndpoints(tether,from,to);
const expected=new LineSegmentsGeometry().setPositions([...from.toArray(),...to.toArray()]);
assert.deepEqual([...buffer.array],[...expected.attributes.instanceStart.data.array]);
assert.ok(tether.boundingBox.equals(expected.boundingBox));
assert.ok(tether.boundingSphere.equals(expected.boundingSphere));
assert.equal(tether.attributes.instanceStart.data,buffer,'moving a tether reuses its allocation');
