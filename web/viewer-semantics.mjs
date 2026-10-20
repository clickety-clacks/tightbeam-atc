/* Pure viewer rules shared with the browser and the package-free test suite. */
export function canInspectNode({type, layerVisible}) {
  return Boolean(type) && layerVisible === true;
}

const RING_VALUES = new Set(['black', 'green', 'light-gray', 'yellow']);
const FRESHNESS_VALUES = new Set(['live', 'stale', 'rebuilding']);
const MODEL_COLLECTIONS = ['agents', 'items', 'links', 'wakes', 'messages'];
const owns = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const rowsHaveStableIds = rows => {
  const ids = new Set();
  return rows.every(row => {
    if (!row || typeof row.id !== 'string' || !row.id.length || ids.has(row.id)) return false;
    ids.add(row.id); return true;
  });
};

export function isModelEnvelope(model) {
  return Boolean(model && model.schemaVersion === 1
    && typeof model.generation === 'string' && model.generation.length > 0
    && Number.isInteger(model.revision) && model.revision >= 0
    && Number.isFinite(model.generatedAt)
    && ['agents', 'items', 'links', 'wakes', 'messages'].every(k => Array.isArray(model[k]))
    && MODEL_COLLECTIONS.every(k => rowsHaveStableIds(model[k]))
    && model.freshness && !Array.isArray(model.freshness)
    && FRESHNESS_VALUES.has(model.freshness.state)
    && ['since', 'reason', 'rebuildIntervalMs', 'lastCompleteAt', 'reconciliation']
      .every(key => owns(model.freshness, key))
    && (model.freshness.reconciliation === null
      || (model.freshness.reconciliation && typeof model.freshness.reconciliation === 'object'
        && !Array.isArray(model.freshness.reconciliation)))
    && owns(model, 'gatewayVersion')
    && (model.gatewayVersion === null || typeof model.gatewayVersion === 'string')
    && owns(model, 'metrics')
    && (model.metrics === null || (model.metrics && typeof model.metrics === 'object'
      && !Array.isArray(model.metrics))));
}

export function isModelDelta(delta) {
  if (!delta || delta.schemaVersion !== 1
      || typeof delta.generation !== 'string' || !delta.generation.length
      || !Number.isInteger(delta.baseRevision) || delta.baseRevision < 0
      || !Number.isInteger(delta.revision) || delta.revision <= delta.baseRevision
      || !Number.isFinite(delta.generatedAt)
      || !delta.freshness || Array.isArray(delta.freshness) || !FRESHNESS_VALUES.has(delta.freshness.state)
      || !owns(delta.freshness, 'reconciliation')
      || (delta.freshness.reconciliation !== null
        && (!delta.freshness.reconciliation || typeof delta.freshness.reconciliation !== 'object'
          || Array.isArray(delta.freshness.reconciliation)))
      || !owns(delta, 'gatewayVersion')
      || (delta.gatewayVersion !== null && typeof delta.gatewayVersion !== 'string')
      || !owns(delta, 'metrics')
      || (delta.metrics !== null && (!delta.metrics || typeof delta.metrics !== 'object' || Array.isArray(delta.metrics)))
      || !delta.changes || typeof delta.changes !== 'object') return false;

  return MODEL_COLLECTIONS.every(name => {
    const change = delta.changes[name];
    if (!change || !Array.isArray(change.upsert) || !Array.isArray(change.remove)) return false;
    const ids = new Set();
    for (const row of change.upsert) {
      if (!row || typeof row.id !== 'string' || !row.id.length || ids.has(row.id)) return false;
      ids.add(row.id);
    }
    const removed = new Set();
    for (const id of change.remove) {
      if (typeof id !== 'string' || !id.length || removed.has(id) || ids.has(id)) return false;
      removed.add(id);
    }
    return true;
  });
}

function applyCollection(rows, change) {
  const next = new Map(rows.map(row => [row.id, row]));
  change.remove.forEach(id => next.delete(id));
  change.upsert.forEach(row => next.set(row.id, row));
  return [...next.values()];
}

/* A delta is atomic: reject the entire envelope unless it is based on the
   exact model currently held by the viewer. Upserts replace complete rows. */
export function applyModelDelta(current, delta) {
  if (!isModelEnvelope(current) || !isModelDelta(delta)
      || delta.generation !== current.generation
      || delta.baseRevision !== current.revision) return null;

  const {baseRevision, changes, ...metadata} = delta;
  const next = {...current, ...metadata};
  for (const name of MODEL_COLLECTIONS)
    next[name] = applyCollection(current[name], changes[name]);
  return isModelEnvelope(next) ? next : null;
}

export function agentSampleTimes(currentTimes, model,
  {fullSnapshot = false, upsertedIds = [], previousModel = null} = {}) {
  const next = new Map(currentTimes || []);
  const sameGeneration = previousModel?.generation === model.generation;
  const sameRevision = sameGeneration && previousModel.revision === model.revision;
  const previousAgents = new Map((sameGeneration ? previousModel.agents : [])
    .map(agent => [agent.id, agent]));
  const present = new Set(model.agents.map(agent => agent.id));
  for (const id of next.keys()) if (!present.has(id)) next.delete(id);
  if (fullSnapshot) {
    if (!sameGeneration) next.clear();
    model.agents.forEach(agent => {
      const previous = previousAgents.get(agent.id);
      const unchanged = sameRevision || (previous && JSON.stringify(previous) === JSON.stringify(agent));
      if (!unchanged) next.set(agent.id, model.generatedAt);
      else if (!next.has(agent.id)) next.set(agent.id, previousModel.generatedAt);
    });
  }
  else upsertedIds.forEach(id => { if (present.has(id)) next.set(id, model.generatedAt); });
  return next;
}

export function isEvidencePage(page, {itemId, generation, revision, offset=0} = {}) {
  return Boolean(Number.isInteger(offset) && offset >= 0
    && page && page.itemId === itemId && page.generation === generation
    && page.revision === revision && Array.isArray(page.records)
    && page.records.every(record => record && typeof record === 'object' && !Array.isArray(record))
    && Number.isInteger(page.total) && page.total >= 0
    && (page.nextOffset === null || (Number.isInteger(page.nextOffset)
      && page.nextOffset === offset + page.records.length && page.nextOffset <= page.total))
    && typeof page.complete === 'boolean'
    && page.freshness && typeof page.freshness === 'object' && !Array.isArray(page.freshness));
}

export function evidencePageQuery(offset, limit, generation, revision) {
  if (!Number.isInteger(offset) || offset < 0
      || !Number.isInteger(limit) || limit < 1 || limit > 100) return null;
  const params = new URLSearchParams({offset: String(offset), limit: String(limit)});
  if (offset > 0) {
    if (typeof generation !== 'string' || !generation.length
        || !Number.isInteger(revision) || revision < 0) return null;
    params.set('generation', generation);
    params.set('revision', String(revision));
  }
  return `?${params.toString()}`;
}

export function shouldAcceptModel(next, current) {
  if (!isModelEnvelope(next)) return false;
  return !current || next.generation !== current.generation
    || next.revision > current.revision;
}

/* A revision is an immutable full snapshot. Equal-revision reconnects recover
   transport without replacing rows; only a newer revision can advance data. */
export function shouldAcceptSnapshot(next, current) {
  if (!isModelEnvelope(next)) return false;
  return !current || next.generation !== current.generation
    || next.revision > current.revision
    || (next.revision === current.revision && next.generatedAt === current.generatedAt);
}

/* Clock, freshness, and evidence-age repaints do not change scene geometry.
   The agent's idle color is updated in place, so idleFor and idleSince can stay out. */
export function sceneSignature(model) {
  if (!isModelEnvelope(model)) return '';
  const entity = (row, agent = false) => {
    const {idleFor, idleSince, evidence, ...rest} = row;
    const facts = evidence && typeof evidence==='object'
      ? Object.fromEntries(Object.entries(evidence).map(([name, value])=>{
          if (!value || typeof value!=='object') return [name, value];
          const {observedAt, freshness, ...stable} = value;
          return [name, stable];
        })) : evidence;
    return {...rest, ...(agent ? {} : {idleFor, idleSince}), evidence:facts};
  };
  return JSON.stringify({
    schemaVersion: model.schemaVersion,
    agents: model.agents.map(row=>entity(row, true)),
    items: model.items.map(row=>entity(row)),
    links: model.links,
    wakes: model.wakes,
    messages: model.messages,
  });
}

export function knownStage(stage) {
  return Number.isInteger(stage) && stage >= 0 && stage <= 6;
}

export function ringMeaning(ring, evidenceStatus, complete = true) {
  if (evidenceStatus !== 'known' || complete === false) return 'unknown';
  if (RING_VALUES.has(ring)) return ring;
  return ring === null ? 'none' : 'unknown';
}

export function factAge(observedAt, now = Date.now()) {
  if (!Number.isFinite(observedAt)) return 'unknown';
  const seconds = Math.max(0, Math.floor((now - observedAt) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export function factValue(entity, fact, value) {
  const status = entity?.evidence?.[fact]?.status;
  return status === 'known'
    ? {known: true, value}
    : {known: false, value: null};
}

/* idleFor is measured at generatedAt. Tick only while its evidence is live;
   stale or rebuilding values remain the last reported value. */
export function idleAgeSeconds(agent, generatedAt, now = Date.now()) {
  const idle = factValue(agent, 'idleFor', agent?.idleFor);
  const evidence = agent?.evidence?.idleFor;
  if (!idle.known || evidence?.complete === false) return null;
  const reported = Number.isFinite(idle.value) ? Math.max(0, idle.value) : null;
  if (evidence.freshness !== 'live' || !Number.isFinite(now)) return reported;
  if (Number.isFinite(agent?.idleSince)) return Math.max(0, (now - agent.idleSince) / 1000);
  if (reported === null || !Number.isFinite(generatedAt)) return null;
  return Math.max(0, reported + (now - generatedAt) / 1000);
}

export function factCountValue(value, evidence) {
  if (evidence?.status !== 'known' || !Number.isFinite(value)) return 'unknown';
  return evidence.complete === false ? `${value} reported (incomplete)` : String(value);
}

export function factListValue(value, evidence) {
  if (evidence?.status !== 'known' || !Array.isArray(value)) return 'unknown';
  if (!value.length) return evidence.complete === true ? 'none' : 'unknown · incomplete inventory';
  return value.map(String).join(', ') + (evidence.complete === false ? ' · incomplete inventory' : '');
}

export function evidenceCounts(model) {
  const counts = {stale:0, rebuilding:0, unknown:0};
  for (const entity of [...model.agents, ...model.items]) {
    const facts = entity.evidence;
    if (!facts || typeof facts !== 'object') { counts.unknown++; continue; }
    for (const fact of Object.values(facts)) {
      if (!fact || typeof fact !== 'object') { counts.unknown++; continue; }
      if (fact.freshness === 'stale') counts.stale++;
      if (fact.freshness === 'rebuilding') counts.rebuilding++;
      if (fact.status !== 'known' || fact.complete === false) counts.unknown++;
    }
  }
  return counts;
}
