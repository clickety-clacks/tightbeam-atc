import assert from 'node:assert/strict';
import {
  classifyWorkItemPlacement, displayedWorkStage, OPEN_WORK_LEGEND,
  OPEN_WORK_STAGE_QUALIFIER, packedDiscPoints, prepareWorkTierLayout,
  workItemDisplay, workPlacementAltitude, workPlacementText,
} from '../web/work-item-tiers.mjs';

const active = {id: 'agent-active', name: 'Agent A', retired: false};
const retired = {id: 'agent-retired', name: 'Retired agent', retired: true};
const agents = {[active.id]: active, [retired.id]: retired};
const openItem = (id, {open = 0, terminal = 0, holders = [], complete = true, stage = 0, ...rest} = {}) => ({
  id, state: 'open', stage, holders,
  assignments: {open, terminal}, evidence: {assignments: {complete}}, ...rest,
});

const carded = classifyWorkItemPlacement(openItem('carded', {
  open: 1, holders: [active.id], complete: false,
}), agents);
assert.equal(carded.tier, 'carded', 'a resolved active card is positive evidence despite incomplete inventories');
assert.equal(workPlacementText(carded, agents), 'carded · to Agent A');

const cloud = classifyWorkItemPlacement(openItem('cloud'), agents);
assert.equal(cloud.tier, 'perimeter');
assert.equal(workPlacementText(cloud, agents), 'orphan ring · no active holder · distance shows age since latest activity');

const perimeter = classifyWorkItemPlacement(openItem('perimeter', {terminal: 2}), agents);
assert.equal(perimeter.tier, 'perimeter');
assert.equal(workPlacementText(perimeter, agents), 'orphan ring · no active holder · distance shows age since latest activity');

const retiredCard = classifyWorkItemPlacement(openItem('retired-card', {
  open: 1, holders: [retired.id],
}), agents);
assert.equal(retiredCard.tier, 'perimeter');
assert.equal(workPlacementText(retiredCard, agents),
  'orphan ring · no active holder · distance shows age since latest activity · open card held by a retired agent');

const unknownHolders = classifyWorkItemPlacement(openItem('unknown-holders', {
  holders: null,
}), agents);
assert.equal(unknownHolders.tier, 'unknown');
assert.equal(workPlacementText(unknownHolders, agents),
  'unknown · assignment state could not be established, so this item is not placed in a tier · holders unknown');

const summaryOpenItem = (id, started, holders, extra = {}) => ({
  ...openItem(id, {open: null, terminal: null, holders, complete: false}),
  started, evidence: {assignments: {complete: false}, summary: {status: 'known', complete: true}},
  ...extra,
});
assert.equal(classifyWorkItemPlacement(summaryOpenItem('summary-cloud', false, []), agents).tier,
  'perimeter', 'the complete item summary can place an unassigned item with stale assignment details');
assert.equal(classifyWorkItemPlacement(summaryOpenItem('summary-started', true, []), agents).tier,
  'perimeter', 'the complete item summary distinguishes a previously assigned item');
assert.equal(classifyWorkItemPlacement(summaryOpenItem('summary-carded', true, [active.id]), agents).tier,
  'carded', 'summary holders retain active-holder placement');
assert.equal(classifyWorkItemPlacement(summaryOpenItem('summary-retired', true, [retired.id]), agents).tier,
  'perimeter', 'summary holders preserve retired-card placement');
assert.equal(classifyWorkItemPlacement(summaryOpenItem('summary-unknown', true, ['missing-agent']), agents).tier,
  'unknown', 'an unresolved summary holder stays visibly unknown');

const incompleteInventory = classifyWorkItemPlacement(openItem('incomplete', {
  open: null, terminal: null, holders: [retired.id], complete: false,
}), agents);
assert.equal(incompleteInventory.tier, 'unknown');
assert.equal(incompleteInventory.reason, 'assignment inventory incomplete');

const missingHolderRow = classifyWorkItemPlacement(openItem('missing-holder-row', {
  open: 1, holders: [],
}), agents);
assert.equal(missingHolderRow.tier, 'unknown', 'a dropped holder row must not turn an open card into cloud or perimeter');
assert.equal(missingHolderRow.reason, "a card holder's agent record is unavailable");

const unresolvedListedHolder = classifyWorkItemPlacement(openItem('unresolved-holder', {
  open: 1, holders: ['absent-agent'],
}), agents);
assert.equal(unresolvedListedHolder.tier, 'unknown');
assert.equal(unresolvedListedHolder.reason, "a card holder's agent record is unavailable");

assert.equal(classifyWorkItemPlacement({id: 'closed', state: 'closed'}, agents), null,
  'closed, iceboxed, and failed items keep their existing treatment');

const openStageFive = openItem('open-stage-five', {terminal: 12, stage: 5, closedAt: 123});
const openDisplay = workItemDisplay(openStageFive, 5);
assert.equal(openDisplay.unfinished, true);
assert.equal(openDisplay.colorStage, 4);
assert.equal(openDisplay.landed, false, 'an open stage-five item never uses the floor');
assert.equal(displayedWorkStage(6), 4);
assert.equal(workPlacementAltitude('perimeter', 5), 20,
  'open stage five uses the stage-four perimeter band');
assert.equal(workPlacementAltitude('unknown', 5), 6,
  'unknown placement keeps the stage-four floating altitude');
assert.equal(workPlacementAltitude('unknown', null, 1.6), 28.6,
  'an unknown stage keeps its neutral side-area altitude');
for (const item of [
  {state: 'closed'}, {state: 'iceboxed'}, {state: 'failed'},
]) {
  assert.equal(workItemDisplay(item, 5).landed, true,
    `${item.state} stage-five items preserve their existing floor treatment`);
}
assert.equal(workItemDisplay({state: 'closed'}, 0).landed, true,
  'closed items retain their floor placement');

assert.equal(OPEN_WORK_STAGE_QUALIFIER,
  'furthest evidence on any of its assignments; the item is still open');
assert.match(OPEN_WORK_LEGEND, /distance = age/);
assert.doesNotMatch(OPEN_WORK_LEGEND, /cloud|started, not carded/);

const points = packedDiscPoints(270);
assert.equal(points.length, 270);
let cloudRadius = 0, cloudGap = Infinity;
for (let i = 0; i < points.length; i++) {
  const p = points[i];
  cloudRadius = Math.max(cloudRadius, Math.hypot(p.x, p.z));
  for (let j = i + 1; j < points.length; j++) {
    cloudGap = Math.min(cloudGap, Math.hypot(p.x - points[j].x, p.z - points[j].z));
  }
}
assert.ok(cloudRadius > 18 && cloudRadius < 21, `270 cloud items should fit in a radius of about 20 (got ${cloudRadius})`);
assert.ok(cloudGap >= 2.2 - 1e-9, `cloud item spacing was ${cloudGap}`);

const treeAgents = [];
for (let depth = 0; depth <= 6; depth++) {
  treeAgents.push({id: `tree-${depth}`, name: `Tree ${depth}`, retired: false,
    parent: depth === 0 ? null : `tree-${depth - 1}`,
    evidence: {parent: {status: 'known', complete: true}}});
}
for (let index = 7; index < 282; index++) {
  treeAgents.push({id: `tree-extra-${index}`, name: `Extra ${index}`, retired: false,
    parent: 'tree-5', evidence: {parent: {status: 'known', complete: true}}});
}
treeAgents.push({id: 'detached-agent', name: 'Detached', retired: false, parent: 'missing-parent',
  evidence: {parent: {status: 'known', complete: true}}});

const cloudItems = Array.from({length: 270}, (_,index) => openItem(`cloud-${String(index).padStart(3, '0')}`));
const ringItems = Array.from({length: 270}, (_,index) => openItem(`ring-${String(index).padStart(3, '0')}`, {
  terminal: 1, stage: index % 5 === 0 ? 5 : 4,
}));
const layout = prepareWorkTierLayout([
  ...cloudItems, ...ringItems,
  openItem('carded-at-agent', {open: 1, holders: ['tree-0'], stage: 0}),
  openItem('unknown-side', {open: null, terminal: null, holders: null, complete: false}),
  {id: 'parked-side', state: 'iceboxed', holders: [], stage: 4},
], treeAgents);
assert.equal(layout.orgRadius, 72);
assert.equal(layout.cloudPositions.size, 0);
assert.equal(layout.perimeterPositions.size, 540);
assert.ok(layout.innerRadius > layout.orgRadius);
assert.ok(layout.outerRadius > layout.innerRadius);
const ringPoints = [...layout.perimeterPositions.values()];
for (let i = 0; i < ringPoints.length; i++) {
  const p = ringPoints[i];
  assert.ok(Math.abs(Math.hypot(p.x, p.z) - layout.outerRadius) < 1e-8);
  const next = ringPoints[(i+1)%ringPoints.length];
  const gap = (Math.atan2(next.z,next.x)-Math.atan2(p.z,p.x)+2*Math.PI)%(2*Math.PI);
  assert.ok(Math.abs(gap-2*Math.PI/ringPoints.length)<1e-8);
}
assert.ok(layout.mainPositions.has('parked-side'));
assert.ok(!layout.sideItemPositions.has('parked-side'));
assert.ok(layout.sideItemPositions.has('unknown-side'));
const detached = layout.detachedAgentPositions.get('detached-agent');
assert.ok(Math.hypot(detached.x,detached.z)<layout.innerRadius);
const now = 100*86400000;
const ageItems = [0,1,3,15,30,90].map((age,i)=>openItem('age-'+i,{latestActivityAt:now-age*86400000}));
const ages = prepareWorkTierLayout(ageItems,treeAgents.slice(0,7),2.2,now);
const radii = ageItems.map(w=>Math.hypot(...Object.values(ages.perimeterPositions.get(w.id))));
for(let i=1;i<5;i++) assert.ok(radii[i]>radii[i-1]);
assert.ok(Math.abs(radii[4]-radii[5])<1e-8);
const more = prepareWorkTierLayout([...ageItems,openItem('extra')],treeAgents.slice(0,7),2.2,now);
assert.equal(ages.outerRadius,more.outerRadius);
const landed = openItem('landed',{merged:true});
assert.equal(classifyWorkItemPlacement(landed,agents),null);
for(const days of [0,1,30]) {
  // The full inventory always supplies geometry, regardless of window membership.
  const same = prepareWorkTierLayout([...ageItems,{id:'done',state:'closed',holders:[],finishedAt:now}],treeAgents.slice(0,7),2.2,now);
  assert.deepEqual([...same.perimeterPositions], [...ages.perimeterPositions]);
}
console.log('orphan and finished placement rules passed');
