/* Pure viewer rules shared with the browser and the package-free test suite. */
export function canInspectNode({type, layerVisible}) {
  return Boolean(type) && layerVisible === true;
}

const RING_VALUES = new Set(['black', 'green', 'light-gray', 'yellow']);
const FRESHNESS_VALUES = new Set(['live', 'stale', 'rebuilding']);
const MODEL_COLLECTIONS = ['agents', 'items', 'links', 'wakes', 'messages'];
const owns = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const MODEL_FIELDS = new Set([
  'schemaVersion', 'generation', 'revision', 'generatedAt', 'freshness',
  'gatewayVersion', 'payloadWarning', 'metrics', 'viewerWarnings', ...MODEL_COLLECTIONS,
]);
const METRICS_FIELDS = new Set([
  'bootstrapReads', 'bootstrapMs', 'payloads', 'bootstrapComplete', 'targetedReads', 'rebuilds',
]);
const FRESHNESS_FIELDS = new Set([
  'state', 'since', 'reason', 'rebuildIntervalMs', 'lastCompleteAt', 'reconciliation',
]);
const GATEWAY_VERSION_FIELDS = new Set(['version', 'build', 'sha']);
const isRecord = value => Boolean(value && typeof value === 'object' && !Array.isArray(value));

function listedFields(fields, label) {
  if (!fields.length) return null;
  const shown = fields.slice(0, 5).join(', ');
  const rest = fields.length > 5 ? `, and ${fields.length - 5} more` : '';
  return `Warning: unknown ${label} ignored (${shown}${rest}).`;
}

function modelMetadataWarnings(model) {
  const warnings = [];
  if (model.schemaVersion !== 1)
    warnings.push('Warning: model schema version is unknown; known entity fields are still displayed.');
  const unknownModelFields = Object.keys(model).filter(key => !MODEL_FIELDS.has(key));
  const modelFieldWarning = listedFields(unknownModelFields, 'model field(s)');
  if (modelFieldWarning) warnings.push(modelFieldWarning);
  if (!Number.isFinite(model.generatedAt))
    warnings.push('Warning: snapshot timestamp is unavailable.');

  if (!owns(model, 'gatewayVersion') || model.gatewayVersion === null
      || (typeof model.gatewayVersion !== 'string' && !isRecord(model.gatewayVersion))) {
    warnings.push('Warning: Gateway version metadata is unknown.');
  } else if (isRecord(model.gatewayVersion)) {
    if (typeof model.gatewayVersion.version !== 'string'
        || !Number.isFinite(model.gatewayVersion.build)
        || typeof model.gatewayVersion.sha !== 'string')
      warnings.push('Warning: Gateway version metadata is incomplete.');
    const unknownVersionFields = Object.keys(model.gatewayVersion)
      .filter(key => !GATEWAY_VERSION_FIELDS.has(key));
    const versionFieldWarning = listedFields(unknownVersionFields, 'Gateway version field(s)');
    if (versionFieldWarning) warnings.push(versionFieldWarning);
  }

  if (!isRecord(model.freshness)) {
    warnings.push('Warning: freshness metadata is unknown.');
  } else {
    if (!FRESHNESS_VALUES.has(model.freshness.state))
      warnings.push('Warning: freshness metadata is unknown.');
    const missingFreshnessFields = ['since', 'reason', 'rebuildIntervalMs', 'lastCompleteAt', 'reconciliation']
      .filter(key => !owns(model.freshness, key));
    const missingFreshnessWarning = listedFields(missingFreshnessFields, 'unavailable freshness field(s)');
    if (missingFreshnessWarning) warnings.push(missingFreshnessWarning);
    if (!Number.isFinite(model.freshness.since))
      warnings.push('Warning: freshness start time is unavailable.');
    if (model.freshness.lastCompleteAt !== null
        && !Number.isFinite(model.freshness.lastCompleteAt))
      warnings.push('Warning: last complete-read time is unavailable.');
    if (model.freshness.reason !== null && typeof model.freshness.reason !== 'string')
      warnings.push('Warning: freshness reason is unavailable.');
    if (!Number.isFinite(model.freshness.rebuildIntervalMs) || model.freshness.rebuildIntervalMs < 0)
      warnings.push('Warning: rebuild interval metadata is unavailable.');
    if (!isRecord(model.freshness.reconciliation))
      warnings.push('Warning: reconciliation metadata is unavailable.');
    const unknownFreshnessFields = Object.keys(model.freshness)
      .filter(key => !FRESHNESS_FIELDS.has(key));
    const freshnessFieldWarning = listedFields(unknownFreshnessFields, 'freshness field(s)');
    if (freshnessFieldWarning) warnings.push(freshnessFieldWarning);
  }

  if (!owns(model, 'metrics') || model.metrics === null || !isRecord(model.metrics)) {
    warnings.push('Warning: metrics metadata is unavailable.');
  } else if (isRecord(model.metrics)) {
    const metricFieldWarning = listedFields(
      Object.keys(model.metrics).filter(key => !METRICS_FIELDS.has(key)), 'metrics field(s)');
    if (metricFieldWarning) warnings.push(metricFieldWarning);
  }
  if (owns(model, 'viewerWarnings') && !Array.isArray(model.viewerWarnings))
    warnings.push('Warning: viewer warning metadata is unavailable.');
  if (typeof model.payloadWarning === 'string' && model.payloadWarning.trim())
    warnings.push(model.payloadWarning);
  else if (model.payloadWarning !== undefined && model.payloadWarning !== null
      && typeof model.payloadWarning !== 'string')
    warnings.push('Warning: payload compatibility metadata is unknown.');
  return warnings;
}

function validEntity(row) {
  return isRecord(row) && typeof row.id === 'string' && row.id.length > 0;
}

function uniqueWarnings(warnings) {
  return [...new Set(warnings.filter(value => typeof value === 'string' && value.trim()))];
}

function hasModelIdentity(model) {
  /* Generation and revision preserve ordering; schema and diagnostics do not
     gate rows whose known entity fields can still be rendered. */
  return Boolean(isRecord(model) && typeof model.generation === 'string' && model.generation.length > 0
    && Number.isInteger(model.revision) && model.revision >= 0);
}

export function isModelEnvelope(model) {
  return hasModelIdentity(model) && MODEL_COLLECTIONS.every(key => Array.isArray(model[key]));
}

/* Keep valid rows in a snapshot even when a sibling row or diagnostic field is
   malformed. Unknown metadata remains available to the view and is reported
   through a plain warning instead of rejecting the whole model. */
export function prepareModelSnapshot(model, previousModel = null) {
  if (!hasModelIdentity(model)) return null;
  const next = {...model};
  const warnings = [
    ...(Array.isArray(model.viewerWarnings) ? model.viewerWarnings : []),
    ...modelMetadataWarnings(model),
  ];
  for (const name of MODEL_COLLECTIONS) {
    if (!Array.isArray(model[name])) {
      const sameGeneration = previousModel?.generation === model.generation;
      const retained = sameGeneration && Array.isArray(previousModel[name]);
      next[name] = retained ? previousModel[name] : [];
      warnings.push(`Warning: ${name} collection is unavailable; `
        + (retained ? 'last known entities were retained.' : 'absence is unknown.'));
      continue;
    }
    const ids = new Set();
    let skipped = 0;
    next[name] = model[name].filter(row => {
      if (!validEntity(row) || ids.has(row.id)) {
        skipped++;
        return false;
      }
      ids.add(row.id);
      return true;
    });
    if (skipped) warnings.push(`Warning: skipped ${skipped} malformed ${name} entr${skipped === 1 ? 'y' : 'ies'}.`);
  }
  next.viewerWarnings = uniqueWarnings(warnings);
  return isModelEnvelope(next) ? next : null;
}

export function isModelDelta(delta) {
  if (!isRecord(delta) || typeof delta.generation !== 'string' || !delta.generation.length
      || !Number.isInteger(delta.baseRevision) || delta.baseRevision < 0
      || !Number.isInteger(delta.revision) || delta.revision <= delta.baseRevision
      || !isRecord(delta.changes)) return false;

  return true;
}

/* Reconciliation timestamps are the last successful complete read for each
   group. The viewer turns them into ages locally so an unchanged service
   revision can still show an active scope crossing its budget. */
export function reconciliationFreshness(reconciliation, now = Date.now()) {
  if (!reconciliation || typeof reconciliation !== 'object' || Array.isArray(reconciliation)
      || !Number.isFinite(now)
      || !['live', 'stale'].includes(reconciliation.announcedChanges)
      || reconciliation.unannouncedChanges !== 'swept'
      || !Number.isFinite(reconciliation.activeBudgetMs) || reconciliation.activeBudgetMs <= 0
      || !Number.isFinite(reconciliation.activeRefreshMs) || reconciliation.activeRefreshMs <= 0
      || !Number.isFinite(reconciliation.recentWindowMs) || reconciliation.recentWindowMs <= 0) return null;

  const group = (value, active = false) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)
        || !Number.isInteger(value.scopeCount) || value.scopeCount < 0
        || !Number.isInteger(value.unconfirmedScopes) || value.unconfirmedScopes < 0
        || value.unconfirmedScopes > value.scopeCount
        || !(value.oldestConfirmedAt === null
          || (Number.isFinite(value.oldestConfirmedAt) && value.oldestConfirmedAt >= 0))) return null;
    if ((value.scopeCount === 0 && value.oldestConfirmedAt !== null)
        || (value.oldestConfirmedAt === null && value.unconfirmedScopes !== value.scopeCount)
        || (value.oldestConfirmedAt !== null && value.unconfirmedScopes === value.scopeCount)) return null;
    if (active && (!Number.isInteger(value.overBudgetScopes) || value.overBudgetScopes < 0
        || value.overBudgetScopes > value.scopeCount - value.unconfirmedScopes)) return null;
    const ageMs = value.oldestConfirmedAt === null
      ? null : Math.max(0, now - value.oldestConfirmedAt);
    const agePastBudget = active && ageMs !== null && ageMs >= reconciliation.activeBudgetMs;
    return {
      scopeCount: value.scopeCount,
      unconfirmedScopes: value.unconfirmedScopes,
      ageMs,
      ...(active ? {overBudgetScopes: value.overBudgetScopes,
        agePastBudget,
        stale: value.overBudgetScopes > 0 || agePastBudget} : {}),
    };
  };

  const active = group(reconciliation.active, true);
  const history = group(reconciliation.history);
  if (!active || !history) return null;
  return {
    announcedChanges: reconciliation.announcedChanges,
    unannouncedChanges: reconciliation.unannouncedChanges,
    activeBudgetMs: reconciliation.activeBudgetMs,
    activeRefreshMs: reconciliation.activeRefreshMs,
    recentWindowMs: reconciliation.recentWindowMs,
    active,
    history,
  };
}

function applyCollection(rows, change) {
  const next = new Map(rows.map(row => [row.id, row]));
  const remove = new Set();
  const upsert = new Map();
  let skipped = 0;
  for (const id of change.remove) {
    if (typeof id !== 'string' || !id.length || remove.has(id)) {
      skipped++;
      continue;
    }
    remove.add(id);
  }
  for (const row of change.upsert) {
    if (!validEntity(row) || upsert.has(row.id)) {
      skipped++;
      continue;
    }
    upsert.set(row.id, row);
  }
  for (const id of upsert.keys()) {
    if (remove.has(id)) {
      remove.delete(id);
      upsert.delete(id);
      skipped++;
    }
  }
  remove.forEach(id => next.delete(id));
  upsert.forEach((row, id) => next.set(id, row));
  return {rows: [...next.values()], skipped};
}

/* Revision identity remains atomic. Malformed collection entries are skipped
   locally while other valid changes in the same delta can advance. */
export function applyModelDelta(current, delta) {
  if (!isModelEnvelope(current) || !isModelDelta(delta)
      || delta.generation !== current.generation
      || delta.baseRevision !== current.revision) return null;

  const {baseRevision, changes, ...metadata} = delta;
  const next = {...current, ...metadata};
  const warnings = [...(Array.isArray(current.viewerWarnings) ? current.viewerWarnings : []),
    ...modelMetadataWarnings(next)];
  if (delta.schemaVersion !== 1)
    warnings.push('Warning: model schema version is unknown; known entity fields are still displayed.');
  const unknownChangeFields = Object.keys(changes).filter(key => !MODEL_COLLECTIONS.includes(key));
  const changeFieldWarning = listedFields(unknownChangeFields, 'update collection(s)');
  if (changeFieldWarning) warnings.push(changeFieldWarning);
  for (const name of MODEL_COLLECTIONS) {
    const change = changes[name];
    if (!isRecord(change) || !Array.isArray(change.upsert) || !Array.isArray(change.remove)) {
      next[name] = current[name];
      warnings.push(`Warning: ${name} update is unavailable; last known entities were retained.`);
      continue;
    }
    const result = applyCollection(current[name], change);
    next[name] = result.rows;
    if (result.skipped)
      warnings.push(`Warning: skipped ${result.skipped} malformed ${name} update entr${result.skipped === 1 ? 'y' : 'ies'}.`);
    const unknownFields = Object.keys(change).filter(key => !['upsert', 'remove'].includes(key));
    const fieldWarning = listedFields(unknownFields, `${name} update field(s)`);
    if (fieldWarning) warnings.push(fieldWarning);
  }
  next.viewerWarnings = uniqueWarnings(warnings);
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
  const freshness = page?.freshness;
  return Boolean(Number.isInteger(offset) && offset >= 0
    && page && page.itemId === itemId && page.generation === generation
    && page.revision === revision && Array.isArray(page.records)
    && page.records.every(record => record && typeof record === 'object' && !Array.isArray(record))
    && Number.isInteger(page.total) && page.total >= 0
    && (page.nextOffset === null || (Number.isInteger(page.nextOffset)
      && page.nextOffset === offset + page.records.length && page.nextOffset <= page.total))
    && typeof page.complete === 'boolean'
    && freshness && typeof freshness === 'object' && !Array.isArray(freshness)
    && ['known', 'unknown'].includes(freshness.status)
    && ['live', 'stale'].includes(freshness.freshness)
    && (freshness.observedAt === null || Number.isFinite(freshness.observedAt))
    && Array.isArray(freshness.source) && freshness.source.every(source => typeof source === 'string')
    && typeof freshness.complete === 'boolean' && freshness.complete === page.complete);
}

export function evidencePageFreshness(freshness) {
  return freshness && typeof freshness === 'object' && !Array.isArray(freshness)
    && ['live', 'stale'].includes(freshness.freshness)
    ? freshness.freshness : 'unknown freshness';
}

export function evidenceRecordAuthor(record) {
  return [record?.byUser, record?.bySession]
    .find(value => typeof value === 'string' && value.trim().length) || 'author unknown';
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
    || (next.revision === current.revision
      && (!Number.isFinite(next.generatedAt) || next.generatedAt === current.generatedAt));
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

function cloneViewerValue(value) {
  if (value && typeof value.clone === 'function') return value.clone();
  if (Array.isArray(value)) return value.map(cloneViewerValue);
  if (value && typeof value === 'object')
    return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, cloneViewerValue(child)]));
  return value;
}

/* Snapshot the user-owned view before scene objects are replaced. Focus uses
   stable IDs, and the pose contains cloned camera vectors from the caller. */
export function captureViewerState(view, focusLayer, cameraPose) {
  const filter = view.filter || {};
  return {
    selection: [...view.selection],
    filter: {
      kind: filter.kind,
      query: filter.query,
      ids: filter.ids ? [...filter.ids] : null,
    },
    focusLayer: cloneViewerValue(focusLayer),
    fit: view.fit,
    input: view.input,
    framing: view.framing,
    fitTarget: view.fitTarget,
    cameraPose: cloneViewerValue(cameraPose),
  };
}

/* Restore stable view choices after a scene rebuild, then let the page resolve
   IDs and derive its highlighted scope against the newly applied model. */
export function restoreViewerState(view, saved, {setFocusLayer, projectFocus, restoreCameraPose}) {
  if (view.selection instanceof Set) {
    view.selection.clear();
    saved.selection.forEach(key => view.selection.add(key));
  } else view.selection = new Set(saved.selection);

  if (!view.filter) view.filter = {};
  view.filter.kind = saved.filter.kind;
  view.filter.query = saved.filter.query;
  view.filter.ids = saved.filter.ids ? new Set(saved.filter.ids) : null;
  view.fit = saved.fit;
  view.input = saved.input;
  view.framing = saved.framing;
  view.fitTarget = saved.fitTarget;
  setFocusLayer?.(cloneViewerValue(saved.focusLayer));
  projectFocus?.();
  restoreCameraPose?.(saved.cameraPose);
  return true;
}

/* Scene builders replace meshes, never the human's camera or scope. Keep this
   boundary explicit so a failed builder also restores the prior view. */
export function rebuildWithViewerState(saved, rebuild, restore) {
  try {
    return rebuild();
  } finally {
    restore(saved);
  }
}

function gatewayVersionLabel(value) {
  if (typeof value === 'string') return value || 'unknown';
  if (!isRecord(value)) return 'unknown';
  const parts = [];
  if (typeof value.version === 'string' && value.version) parts.push(value.version);
  if ((typeof value.build === 'string' && value.build)
      || (Number.isFinite(value.build) && value.build >= 0)) parts.push(`build ${value.build}`);
  if (typeof value.sha === 'string' && value.sha) parts.push(`sha ${value.sha.slice(0, 8)}`);
  return parts.length ? parts.join(' · ') : 'unknown';
}

function durationLabel(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return 'unknown';
  if (milliseconds < 60_000) return Math.round(milliseconds / 1000) + 's';
  if (milliseconds < 3_600_000) return Math.round(milliseconds / 60_000) + 'm';
  const hours = milliseconds / 3_600_000;
  if (hours < 48) return hours < 10 ? hours.toFixed(1) + 'h' : Math.round(hours) + 'h';
  return Math.round(milliseconds / 86_400_000) + 'd';
}

/* This writes the same user-facing fields that the HUD displays. Keeping the
   presentation at a small DOM seam lets mock models verify visible wording. */
export function renderModelStatusElement(element, {model, streamState, streamError}, now = Date.now()) {
  if (!model) {
    element.textContent = streamState === 'stale'
      ? 'stale · live model unavailable' : 'connecting to live model…';
    element.className = streamState === 'stale' ? 'stale' : 'sample';
    element.title = streamError || 'Waiting for the service model event stream.';
    return element;
  }

  const freshness = model.freshness || {};
  const backstop = reconciliationFreshness(freshness.reconciliation, now);
  const state = streamState === 'stale' || backstop?.announcedChanges === 'stale'
    || backstop?.active.stale ? 'stale'
    : (FRESHNESS_VALUES.has(freshness.state) ? freshness.state : 'unknown');
  const counts = evidenceCounts(model);
  const age = factAge(model.generatedAt, now);
  const warnings = Array.isArray(model.viewerWarnings) ? model.viewerWarnings : [];
  element.textContent = `${state} · ${model.agents.length} agents · ${model.items.length} items · `
    + `${counts.stale} stale · ${counts.rebuilding} rebuilding · ${counts.unknown} unknown · snapshot age ${age}`
    + (warnings.length ? ` · ${warnings.join(' · ')}` : '');
  element.className = state === 'live' ? 'live' : state === 'stale' ? 'stale'
    : state === 'rebuilding' ? 'rebuilding' : 'unknown';

  const sweepAge = freshness.reconciliation && typeof freshness.reconciliation === 'object'
    ? factAge(freshness.reconciliation.lastSweepAt, now) : 'unknown';
  const groupDetails = (label, group) => {
    if (!group) return `${label}: unknown (service metadata unavailable)`;
    if (group.scopeCount === 0) return `${label}: no scopes`;
    const confirmationAge = group.ageMs === null ? 'oldest known confirmation age unknown'
      : `oldest known confirmation age ${durationLabel(group.ageMs)}`;
    return `${label}: ${group.scopeCount} scopes · ${group.unconfirmedScopes} with unknown age · ${confirmationAge}`;
  };
  const activeBudgetDetail = backstop
    ? `${backstop.active.overBudgetScopes} reported over-budget scopes`
      + (backstop.active.agePastBudget && backstop.active.overBudgetScopes === 0
        ? ' · oldest confirmation age also exceeds the budget' : '')
    : '';
  const activeBackstop = backstop
    ? `Active backstop: ${backstop.active.stale ? 'stale' : 'within budget'} · `
      + `${activeBudgetDetail} · `
      + `confirmation budget ${durationLabel(backstop.activeBudgetMs)} · `
      + `refresh target ${durationLabel(backstop.activeRefreshMs)} · `
      + `recent-activity window ${durationLabel(backstop.recentWindowMs)} · `
      + groupDetails('active', backstop.active)
    : 'Active backstop: unknown (service metadata unavailable)';
  const historyBackstop = backstop
    ? groupDetails('Terminal-history backstop', backstop.history)
      + ` · estimated rotation ${durationLabel(freshness.reconciliation?.estimatedSweepMs)}`
    : 'Terminal-history backstop: unknown (service metadata unavailable)';
  const reconciliation = freshness.reconciliation && typeof freshness.reconciliation === 'object'
    ? `Reconciliation: ${freshness.reconciliation.mode || 'unknown'} · `
      + `${Number.isFinite(freshness.reconciliation.pendingScopes) ? freshness.reconciliation.pendingScopes : 'unknown'} pending / `
      + `${Number.isFinite(freshness.reconciliation.totalScopes) ? freshness.reconciliation.totalScopes : 'unknown'} cycle scopes · `
      + `budget ${Number.isFinite(freshness.reconciliation.scopeBudget) ? freshness.reconciliation.scopeBudget : 'unknown'} · `
      + `maintained scopes ${Number.isFinite(freshness.reconciliation.historyScopes) ? freshness.reconciliation.historyScopes : 'unknown'} · `
      + `last sweep ${sweepAge === 'unknown' ? 'unknown' : sweepAge + ' ago'}`
    : 'Reconciliation: unknown';
  element.title = [
    `Gateway version: ${gatewayVersionLabel(model.gatewayVersion)}`,
    `Service freshness: ${FRESHNESS_VALUES.has(freshness.state) ? freshness.state : 'unknown'}`,
    `Announced changes: ${backstop?.announcedChanges || 'unknown'} at the latest service publication (feed delivery, not proof that unannounced changes are absent)`,
    `Unannounced changes: ${backstop?.unannouncedChanges || 'unknown'} by background confirmation sweeps`,
    activeBackstop,
    historyBackstop,
    `Freshness since: ${factAge(freshness.since, now)} ago`,
    `Last complete read: ${factAge(freshness.lastCompleteAt, now)} ago`,
    Number.isFinite(freshness.rebuildIntervalMs) ? `Rebuild interval: ${Math.round(freshness.rebuildIntervalMs / 1000)}s` : 'Rebuild interval: unknown',
    reconciliation,
    freshness.reason ? `Reason: ${freshness.reason}` : null,
    ...warnings,
    streamError ? `Connection: ${streamError}` : 'Connection: event stream open',
    'Input: pointer and touch events; no separate gaze signal is exposed to this page.',
  ].filter(Boolean).join('\n');
  return element;
}

const escapeMarkup = value => String(value).replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[character]);

/* Per-fact freshness and age labels used by hover and detail cards. */
export function renderEvidenceMarkup(entity, now = Date.now()) {
  const entries = entity?.evidence && typeof entity.evidence === 'object'
    ? Object.entries(entity.evidence).sort(([a], [b]) => a.localeCompare(b)) : [];
  if (!entries.length) return '<span class="seg">Per-fact evidence and age unavailable.</span>';
  const rows = entries.map(([name, evidence]) => {
    if (!evidence || typeof evidence !== 'object')
      return `<span class="evidence-row"><b>${escapeMarkup(name)}</b> · unknown · age unknown · source unknown</span>`;
    const status = evidence.status === 'known' ? 'known' : 'unknown';
    const freshness = ['live', 'stale', 'rebuilding'].includes(evidence.freshness)
      ? evidence.freshness : 'unknown freshness';
    const observed = factAge(evidence.observedAt, now);
    const source = Array.isArray(evidence.source) ? evidence.source.join(', ')
      : (evidence.source || 'unknown source');
    const complete = evidence.complete === true ? 'complete'
      : evidence.complete === false ? 'incomplete' : 'unknown completeness';
    return `<span class="evidence-row"><b>${escapeMarkup(name)}</b> · ${status} · ${freshness} · observed ${observed}${observed === 'unknown' ? '' : ' ago'} · ${escapeMarkup(source)} · ${complete}</span>`;
  });
  return '<span class="seg">' + rows.join('') + '</span>';
}
