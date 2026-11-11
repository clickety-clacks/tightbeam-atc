import {orphanRadius, itemWindow} from './work-item-window.mjs';
/* Spatial and wording rules for unfinished work. Kept pure so the same
   placement contract can be exercised without a browser or service. */
const ACTIVE_REASON = "a card holder's agent record is unavailable";
export const OPEN_WORK_STAGE_QUALIFIER = 'furthest evidence on any of its assignments; the item is still open';
export const OPEN_WORK_LEGEND = 'open unlanded work without an active holder: orphan ring · distance = age since latest activity, outer edge = 30 days or unknown age · near an agent with a line = carded · ? = attachment unknown · landed/finished visibility is set in days';

function agentFor(agentsById, id) {
  return agentsById instanceof Map ? agentsById.get(id) : agentsById?.[id];
}

export function classifyWorkItemPlacement(item, agentsById) {
  if (item?.state !== 'open' || item.merged === true) return null;

  const holders = item.holders;
  const activeHolders = Array.isArray(holders)
    ? holders.filter(id => agentFor(agentsById, id) && !agentFor(agentsById, id).retired)
    : [];
  /* A known active holder is enough to establish the carded tier, even while
     other assignment or holder facts are incomplete. */
  if (activeHolders.length)
    return {tier: 'carded', reason: null, activeHolders, retiredOpenCard: false};

  /* A complete per-item summary supplies the two placement facts that matter
     here: whether any assignment ever existed and who currently holds an open
     assignment. The detailed assignment collection may still be incomplete. */
  const summaryKnown = typeof item.started === 'boolean'
    && item.evidence?.summary?.status === 'known'
    && item.evidence.summary.complete === true;
  if (summaryKnown) {
    if (!Array.isArray(holders))
      return {tier: 'unknown', reason: 'holders unknown', activeHolders: [], retiredOpenCard: false};
    if (!item.started && holders.length)
      return {tier: 'unknown', reason: 'work-item summary is inconsistent', activeHolders: [], retiredOpenCard: false};
    if (holders.some(id => !agentFor(agentsById, id)))
      return {tier: 'unknown', reason: ACTIVE_REASON, activeHolders: [], retiredOpenCard: false};
    if (holders.length)
      return {tier: 'perimeter', reason: null, activeHolders: [], retiredOpenCard: true};
    return {tier: 'perimeter', reason: null, activeHolders: [], retiredOpenCard: false};
  }

  const open = item.assignments?.open;
  const terminal = item.assignments?.terminal;
  const countsKnown = item.evidence?.assignments?.complete === true
    && Number.isSafeInteger(open) && open >= 0
    && Number.isSafeInteger(terminal) && terminal >= 0;
  if (!countsKnown)
    return {tier: 'unknown', reason: 'assignment inventory incomplete', activeHolders: [], retiredOpenCard: false};
  if (!Array.isArray(holders))
    return {tier: 'unknown', reason: 'holders unknown', activeHolders: [], retiredOpenCard: false};

  if (holders.some(id => !agentFor(agentsById, id)))
    return {tier: 'unknown', reason: ACTIVE_REASON, activeHolders: [], retiredOpenCard: false};

  /* The service publishes unique holder IDs, not one ID per open assignment.
     If there are more open cards than resolved holders, it cannot prove that
     a missing session row did not hide another holder. Keep that placement
     unknown rather than treating incomplete coverage as an orphan. */
  if (holders.length < open)
    return {tier: 'unknown', reason: ACTIVE_REASON, activeHolders: [], retiredOpenCard: false};
  if (holders.length > open)
    return {tier: 'unknown', reason: 'assignment inventory incomplete', activeHolders: [], retiredOpenCard: false};

  if (open === 0) {
    return {tier: 'perimeter', reason: null, activeHolders: [], retiredOpenCard: false};
  }
  return {tier: 'perimeter', reason: null, activeHolders: [],
    retiredOpenCard: holders.some(id => agentFor(agentsById, id).retired)};
}

export function displayedWorkStage(stage) {
  return Number.isInteger(stage) && stage >= 0 && stage <= 6
    ? Math.min(stage, 4) : null;
}

export function workItemDisplay(item, stage) {
  const unfinished = item?.state === 'open';
  return {
    unfinished,
    colorStage: unfinished ? displayedWorkStage(stage) : stage,
    landed: !unfinished && ((stage !== null && stage >= 5) || item?.state === 'closed'),
  };
}

export function workPlacementAltitude(placement, stage, offset = 0, cloudTexture = 0) {
  const tier = typeof placement === 'string' ? placement : placement?.tier;
  const shownStage = displayedWorkStage(stage);
  if (tier === 'cloud') return 40.25 + Math.max(0, Math.min(2.5, cloudTexture));
  if (tier === 'perimeter') return shownStage === null ? 28 : 20 + (4 - shownStage) * 1.5;
  if (tier === 'carded') return shownStage === null ? 15 + offset : 6 + (4 - shownStage) * 2 + offset;
  if (tier === 'unknown') return shownStage === null ? 27 + offset : 6 + (4 - shownStage) * 4.2 + offset;
  return null;
}

export function workPlacementText(placement, agentsById) {
  if (!placement) return '';
  if (placement.tier === 'cloud') return 'cloud · not started, not carded';
  if (placement.tier === 'carded') {
    const names = placement.activeHolders.map(id => agentFor(agentsById, id)?.name || id);
    return `carded · to ${names.join(', ')}`;
  }
  if (placement.tier === 'perimeter')
    return 'orphan ring · no active holder · distance shows age since latest activity'
      + (placement.retiredOpenCard ? ' · open card held by a retired agent' : '');
  if (placement.tier === 'unknown')
    return 'unknown · assignment state could not be established, so this item is not placed in a tier'
      + (placement.reason ? ` · ${placement.reason}` : '');
  return '';
}

export function deterministicItemScatter(id) {
  let h = 2166136261;
  for (let i = 0; i < id.length; i++){ h ^= id.charCodeAt(i); h = Math.imul(h, 16777619); }
  const a = ((h >>> 0) % 3600) / 3600 * Math.PI * 2;
  const r = 1.1 + (((h >>> 12) & 1023) / 1023) * 3.4;
  return {x: Math.cos(a) * r, z: Math.sin(a) * r, phase: a};
}

export function spawnerAgentDepths(agents) {
  const byId = new Map(agents.map(agent => [agent.id, agent]));
  const children = new Map();
  const parentKnown = agent => agent.evidence?.parent?.status === 'known'
    && agent.evidence.parent.complete !== false;
  for (const agent of agents) {
    if (parentKnown(agent) && agent.parent && byId.has(agent.parent)) {
      const rows = children.get(agent.parent) || [];
      rows.push(agent.id);
      children.set(agent.parent, rows);
    }
  }
  const depths = new Map();
  const place = (id, depth) => {
    if (depths.has(id)) return;
    depths.set(id, depth);
    for (const child of children.get(id) || []) place(child, depth + 1);
  };
  for (const agent of agents)
    if (parentKnown(agent) && !agent.parent) place(agent.id, 0);
  return depths;
}

export function packedDiscPoints(count, spacing = 2.2) {
  if (!Number.isSafeInteger(count) || count <= 0) return [];
  const extent = Math.max(2, Math.ceil(Math.sqrt(count) * 2));
  const points = [];
  for (let q = -extent; q <= extent; q++) {
    for (let r = -extent; r <= extent; r++) {
      const x = spacing * (q + r / 2);
      const z = spacing * Math.sqrt(3) * r / 2;
      points.push({x, z, q, r, radiusSq: x * x + z * z, angle: Math.atan2(z, x)});
    }
  }
  points.sort((a, b) => a.radiusSq - b.radiusSq || a.angle - b.angle || a.q - b.q || a.r - b.r);
  return points.slice(0, count).map(({x, z}) => ({x, z}));
}

function sideAreaShift(points, minimumRadius) {
  if (!points.length) return 0;
  return Math.max(0, ...points.map(({x, z}) =>
    Math.sqrt(Math.max(0, minimumRadius * minimumRadius - x * x)) - z));
}

const idOrder = (left, right) => left.id < right.id ? -1 : left.id > right.id ? 1 : 0;

// Preserve failed-item and detached-agent coordinates from the prior layout.
// These legacy membership facts never place open items in the rendered scene.
function legacyPlacement(item, agentsById) {
  const placement = classifyWorkItemPlacement({...item, merged:false}, agentsById);
  if (placement?.tier !== 'perimeter' || item.holders?.length) return placement;
  const summaryKnown = typeof item.started === 'boolean'
    && item.evidence?.summary?.status === 'known' && item.evidence.summary.complete === true;
  const neverStarted = summaryKnown ? !item.started
    : item.assignments?.open === 0 && item.assignments?.terminal === 0;
  return neverStarted ? {...placement, tier:'cloud'} : placement;
}

function legacyWorkLayout(items, agents, spacing = 2.2) {
  const agentsById = new Map(agents.map(agent => [agent.id, agent]));
  const depths = spawnerAgentDepths(agents);
  const orgRadius = Math.max(0, ...[...depths.values()].map(depth => depth * 12));
  const placements = new Map();
  for (const item of items) {
    const placement = legacyPlacement(item, agentsById);
    if (placement) placements.set(item.id, placement);
  }

  const cloudItems = items.filter(item => placements.get(item.id)?.tier === 'cloud').slice().sort(idOrder);
  const perimeterItems = items.filter(item => placements.get(item.id)?.tier === 'perimeter').slice().sort(idOrder);
  const radiusFor = count => count > 1
    ? Math.max(orgRadius + 12, spacing / (2 * Math.sin(Math.PI / count)))
    : orgRadius + 12;
  const ringRadius = radiusFor(perimeterItems.length);
  const minimumSideRadius = ringRadius + 12;
  const cloudPositions = new Map();
  const cloudPoints = packedDiscPoints(cloudItems.length, spacing);
  cloudItems.forEach((item, index) => cloudPositions.set(item.id, {
    ...cloudPoints[index], texture: (index % 100) * .025,
  }));
  const perimeterPositions = new Map();
  perimeterItems.forEach((item, index) => {
    const angle = index * Math.PI * 2 / perimeterItems.length;
    perimeterPositions.set(item.id, {x: Math.cos(angle) * ringRadius, z: Math.sin(angle) * ringRadius});
  });

  const sideItemBases = new Map(), sidePoints = {parked: [], unknown: []};
  items.forEach((item, index) => {
    const placement = placements.get(item.id);
    if (placement?.tier === 'carded' || placement?.tier === 'cloud' || placement?.tier === 'perimeter') return;
    const holdersKnown = Array.isArray(item.holders);
    const active = holdersKnown && item.holders.some(id => agentsById.has(id) && !agentsById.get(id).retired);
    if (active && placement?.tier !== 'unknown') return;
    const area = placement?.tier === 'unknown' || !holdersKnown ? 'unknown' : 'parked';
    const x = (area === 'parked' ? -55 : 55) + (index % 24) * 4.6;
    const z = 62 + Math.floor(index / 24) * 5;
    const scatter = deterministicItemScatter(item.short || item.id);
    const point = {x: x + scatter.x, z: z + scatter.z, area};
    sideItemBases.set(item.id, point);
    sidePoints[area].push(point);
  });

  const detached = agents.filter(agent => !depths.has(agent.id));
  const detachedBases = new Map();
  detached.forEach((agent, index) => {
    const scatter = deterministicItemScatter(agent.id);
    const point = {x: 66 + (index % 8) * 4.6 + scatter.x * .45,
      z: 54 + Math.floor(index / 8) * 4.6 + scatter.z * .45};
    detachedBases.set(agent.id, point);
  });
  const sideOffsets = {
    parked: sideAreaShift(sidePoints.parked, minimumSideRadius),
    unknown: sideAreaShift(sidePoints.unknown, minimumSideRadius),
    detached: sideAreaShift([...detachedBases.values()], minimumSideRadius),
  };
  const sideItemPositions = new Map([...sideItemBases].map(([id, point]) => [id,
    {x: point.x, z: point.z + sideOffsets[point.area], area: point.area}]));
  const detachedAgentPositions = new Map([...detachedBases].map(([id, point]) => [id,
    {x: point.x, z: point.z + sideOffsets.detached}]));

  return {agentsById, depths, orgRadius, ringRadius, placements,
    cloudPositions, perimeterPositions, sideOffsets, sideItemPositions, detachedAgentPositions};
}

export function prepareWorkTierLayout(items, agents, spacing = 2.2, now = Date.now()) {
  const layout = legacyWorkLayout(items, agents, spacing);
  const placements = new Map(items.map(item => [item.id,
    classifyWorkItemPlacement(item, layout.agentsById)]).filter(([, value]) => value));
  const orphans = items.filter(item => placements.get(item.id)?.tier === 'perimeter').sort(idOrder);
  // The central extent includes detached agents and the attached-item scatter.
  const graphRadius = Math.max(layout.orgRadius, ...[...layout.detachedAgentPositions.values()]
    .map(p => Math.hypot(p.x, p.z))) + 8;
  const innerRadius = graphRadius + 8, outerRadius = innerRadius + 60;
  const perimeterPositions = new Map();
  orphans.forEach((item, index) => {
    const angle = index * 2 * Math.PI / orphans.length;
    const radius = orphanRadius(item, innerRadius, outerRadius, now);
    perimeterPositions.set(item.id, {x: Math.cos(angle) * radius, z: Math.sin(angle) * radius});
  });
  const mainPositions = new Map();
  for (const item of items) {
    if (!itemWindow(item)) continue;
    if (Array.isArray(item.holders) && item.holders.some(id =>
        layout.agentsById.has(id) && !layout.agentsById.get(id).retired)) continue;
    const scatter = deterministicItemScatter(item.id);
    const radius = graphRadius * Math.min(.85, Math.hypot(scatter.x, scatter.z) / 5.5);
    mainPositions.set(item.id, {x: Math.cos(scatter.phase) * radius,
      z: Math.sin(scatter.phase) * radius});
    layout.sideItemPositions.delete(item.id);
  }
  return {...layout, placements, cloudPositions:new Map(), perimeterPositions,
    mainPositions, innerRadius, outerRadius, ringRadius:outerRadius};
}
