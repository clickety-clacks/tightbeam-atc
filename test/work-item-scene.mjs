import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import * as THREE from '../web/three.module.js';
import * as tiers from '../web/work-item-tiers.mjs';
import * as windowRules from '../web/work-item-window.mjs';
import {factValue, knownStage, ringMeaning} from '../web/viewer-semantics.mjs';
import {unknownItemType} from '../web/viewer-unknown-status.mjs';

const page = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const now = Date.now();
const context = vm.createContext({THREE, ...tiers, ...windowRules, factValue, knownStage,
  ringMeaning, unknownItemType, Date, Map, Object,
  byId:{}, APOS:{}, itemGroup:new THREE.Group(), tetherGroup:new THREE.Group(),
  itemMeshes:[], renderedItemIds:[], finishedDays:1,
  T:{stage:[0,1,2,3,4,5,6], itemClosed:1, factUnknown:2, orphan:3},
  disposeGroup(group){group.clear();}, labelSprite(){return new THREE.Sprite();},
  diagramMesh(geometry,color){return new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({color}));},
  layerState:{workItems:false, agents:true},
});
const build = page.slice(page.indexOf('function buildItems('), page.indexOf('function buildTieredScene('));
const nodeLayer = page.slice(page.indexOf('function nodeLayerOn('),page.indexOf('/* Keep node parents mounted:'));
vm.runInContext(build + '\n' + nodeLayer, context);
const open = {id:'orphan', short:'orphan', state:'open', holders:[], stage:0,
  latestActivityAt:now, assignments:{open:0,terminal:0}, evidence:{assignments:{complete:true},stage:{status:'known',complete:true}}};
const done = {id:'done', state:'closed', holders:[], stage:5, finishedAt:now};
const failed = {...open,id:'failed', short:'failed', state:'failed', stage:5};
const unknown = {...open,id:'unknown',holders:null};
const items = [open,done,failed,unknown];
const layout = tiers.prepareWorkTierLayout(items,[],2.2,now);
context.items = items; context.layout = layout;
vm.runInContext('buildItems(items,layout)',context);
const meshes = ()=>context.itemMeshes.filter(m=>!m.userData.isDisc);
assert.deepEqual(Array.from(meshes(),m=>m.userData.w.id),['orphan','done','failed','unknown']);
const orphanPosition = meshes()[0].position.clone();
const failedPosition = meshes()[2].position.clone();
assert.equal(meshes()[2].userData.placement.tier,'perimeter');
assert.equal(meshes()[2].children.filter(ch=>ch.userData.failedMarker).length,1);
const main = meshes()[1].position;
assert.ok(Math.hypot(main.x,main.z)<layout.innerRadius);
assert.equal(context.nodeLayerOn(meshes()[0]),true,'work layer cannot hide an orphan');
assert.equal(context.nodeLayerOn(meshes()[2]),true,'work layer cannot hide a failed orphan');
context.finishedDays = 0;
vm.runInContext('buildItems(items,layout)',context);
assert.deepEqual(Array.from(meshes(),m=>m.userData.w.id),['orphan','failed','unknown']);
assert.ok(meshes()[0].position.equals(orphanPosition));
assert.ok(meshes()[1].position.equals(failedPosition));
context.finishedDays = 30;
vm.runInContext('buildItems(items,layout)',context);
assert.ok(meshes()[0].position.equals(orphanPosition));
assert.ok(meshes()[2].position.equals(failedPosition));

// A controlled served model exercises cases current upstream records cannot
// express. The HTTP server and Three.js scene exist only in this test process.
const {createServer} = await import('node:http');
const agent = {id:'holder', name:'Holder', evidence:{parent:{status:'known',complete:true}},parent:null};
const proof = (id, accepted, at=now)=>({id:accepted?'acceptance':'rejection',itemId:id,
  source:'operator-decision',accepted,at});
const six = [];
for (const merged of [false,true]) for (const attachment of ['none','active','unknown']) {
  const id = `${attachment}-${merged}`;
  six.push({...open,id,short:id,state:'failed',merged,stage:5,ring:merged?'black':'yellow',
    holders:attachment==='unknown'?null:attachment==='active'?['holder']:[],
    assignments:{open:attachment==='active'?1:0,terminal:1},
    evidence:{...open.evidence,ring:{status:'known',complete:true}},
    failReason:'The operator accepted this; see a decision mentioned in prose',
    byUser:'operator', failedAt:now-90*windowRules.DAY_MS});
}
const accepted = {...open,id:'accepted',state:'failed',failedAt:now-2*windowRules.DAY_MS,
  failureAcceptance:{complete:true,records:[proof('accepted',true,now-windowRules.DAY_MS/2)]}};
const conflicting = {...open,id:'conflicting',state:'failed',
  failureAcceptance:{complete:true,records:[proof('conflicting',true),proof('conflicting',false)]}};
const response = {items:[...six,accepted,conflicting],agents:[agent]};
const server = createServer((req,res)=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(response));});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let served;
try { served = await (await fetch(`http://127.0.0.1:${server.address().port}/model`)).json(); }
finally { await new Promise(resolve=>server.close(resolve)); }
context.byId = {holder:agent}; context.APOS = {holder:new THREE.Vector3(12,0,0)};
// Resolve the page's import-map name to the vendored Three.js module.
const {register} = await import('node:module');
register('data:text/javascript,' + encodeURIComponent(`export async function resolve(name, context, next) {
  if (name === 'three') return {url:${JSON.stringify(new URL('../web/three.module.js',import.meta.url).href)},shortCircuit:true};
  return next(name,context);
}`),import.meta.url);
// Actual line classes used by the page, without a WebGL renderer.
const {LineMaterial} = await import('../web/addons/lines/LineMaterial.js');
const {LineSegmentsGeometry} = await import('../web/addons/lines/LineSegmentsGeometry.js');
const {LineSegments2} = await import('../web/addons/lines/LineSegments2.js');
Object.assign(context,{LineMaterial,LineSegmentsGeometry,LineSegments2,
  canvasBounds:{width:1000,height:800},edgeMats:new Set()});
context.items = served.items;
context.layout = tiers.prepareWorkTierLayout(served.items,served.agents,2.2,now);
const positions = new Map();
for (const days of [0,30,1]) {
  context.finishedDays = days;
  vm.runInContext('buildItems(items,layout)',context);
  const byId = new Map(meshes().map(m=>[m.userData.w.id,m]));
  for (const item of six) {
    const mesh = byId.get(item.id);
    assert.ok(mesh, `${item.id} remains visible at N=${days}`);
    const tier = item.id.startsWith('none')?'perimeter':item.id.startsWith('active')?'carded':'unknown';
    assert.equal(mesh.userData.placement.tier,tier);
    assert.equal(mesh.children.some(ch=>ch.userData.failedMarker),tier==='perimeter');
    if (positions.has(item.id)) assert.ok(mesh.position.equals(positions.get(item.id)));
    positions.set(item.id,mesh.position.clone());
    if (tier==='perimeter') {
      const p = context.layout.perimeterPositions.get(item.id);
      assert.equal(mesh.position.x,p.x); assert.equal(mesh.position.z,p.z);
    }
    if (tier==='carded') {
      const scatter = tiers.deterministicItemScatter(item.short);
      assert.equal(mesh.position.x,12+scatter.x);
      assert.equal(mesh.position.z,scatter.z+3.2); // prior floor placement
    }
    if (item.merged) {
      const disc = context.itemMeshes.find(m=>m.userData.isDisc && m.userData.w.id===item.id);
      assert.ok(disc, 'known merge retains its floor ring');
    }
  }
  assert.equal(byId.has('accepted'),days>0);
  if (days>0) {
    assert.equal(byId.get('accepted').userData.placement,null);
    assert.ok(Math.hypot(byId.get('accepted').position.x,byId.get('accepted').position.z)<context.layout.innerRadius);
  }
  assert.equal(byId.get('conflicting').userData.placement.tier,'perimeter');
  assert.match(windowRules.failureDescription(byId.get('conflicting').userData.w),/conflicts/);
}
