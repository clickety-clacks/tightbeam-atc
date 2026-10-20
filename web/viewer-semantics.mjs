/* Pure viewer rules shared with the browser and the package-free test suite. */
export function canInspectNode({type, layerVisible}) {
  return Boolean(type) && layerVisible === true;
}

const RING_VALUES = new Set(['black', 'green', 'light-gray', 'yellow']);
const FRESHNESS_VALUES = new Set(['live', 'stale', 'rebuilding']);

export function isModelEnvelope(model) {
  return Boolean(model && model.schemaVersion === 1
    && typeof model.generation === 'string' && model.generation.length > 0
    && Number.isInteger(model.revision) && model.revision >= 0
    && Number.isFinite(model.generatedAt)
    && ['agents', 'items', 'links', 'wakes', 'messages'].every(k => Array.isArray(model[k]))
    && model.agents.every(row => row && typeof row.id === 'string' && row.id.length > 0)
    && model.items.every(row => row && typeof row.id === 'string' && row.id.length > 0)
    && ['links', 'wakes', 'messages'].every(k => model[k].every(row => row && typeof row === 'object'))
    && model.freshness && FRESHNESS_VALUES.has(model.freshness.state));
}

export function shouldAcceptModel(next, current) {
  if (!isModelEnvelope(next)) return false;
  return !current || next.generation !== current.generation
    || next.revision > current.revision;
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
  if (!idle.known || !Number.isFinite(idle.value) || evidence?.complete === false) return null;
  const reported = Math.max(0, idle.value);
  if (evidence.freshness !== 'live' || !Number.isFinite(generatedAt) || !Number.isFinite(now))
    return reported;
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
