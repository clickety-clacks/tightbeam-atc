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
const failed = {id:'failed', short:'failed', state:'failed', holders:[], stage:5};
const unknown = {...open,id:'unknown',holders:null};
const items = [open,done,failed,unknown];
const layout = tiers.prepareWorkTierLayout(items,[],2.2,now);
context.items = items; context.layout = layout;
vm.runInContext('buildItems(items,layout)',context);
const meshes = ()=>context.itemMeshes.filter(m=>!m.userData.isDisc);
assert.deepEqual(Array.from(meshes(),m=>m.userData.w.id),['orphan','done','failed','unknown']);
const orphanPosition = meshes()[0].position.clone();
const failedPosition = meshes()[2].position.clone();
const failedScatter = tiers.deterministicItemScatter('failed');
assert.ok(Math.abs(failedPosition.x-(-55+2*4.6+failedScatter.x))<1e-9);
assert.ok(Math.abs(failedPosition.z-(62+failedScatter.z))<1e-9);
const main = meshes()[1].position;
assert.ok(Math.hypot(main.x,main.z)<layout.innerRadius);
assert.equal(context.nodeLayerOn(meshes()[0]),true,'work layer cannot hide an orphan');
assert.equal(context.nodeLayerOn(meshes()[2]),false,'failed layer behavior is unchanged');
context.finishedDays = 0;
vm.runInContext('buildItems(items,layout)',context);
assert.deepEqual(Array.from(meshes(),m=>m.userData.w.id),['orphan','failed','unknown']);
assert.ok(meshes()[0].position.equals(orphanPosition));
assert.ok(meshes()[1].position.equals(failedPosition));
context.finishedDays = 30;
vm.runInContext('buildItems(items,layout)',context);
assert.ok(meshes()[0].position.equals(orphanPosition));
assert.ok(meshes()[2].position.equals(failedPosition));
