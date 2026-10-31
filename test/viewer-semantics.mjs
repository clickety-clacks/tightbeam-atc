import assert from 'node:assert/strict';
import { agentSampleTimes, applyModelDelta, canInspectNode, evidenceCounts, evidenceFreshnessLabel, evidencePageFreshness,
         evidencePageQuery, evidenceRecordAuthor,
         factAge, factCountValue, factListValue, factValue, idleAgeSeconds,
         isEvidencePage, isModelDelta, isModelEnvelope, knownStage,
         prepareModelSnapshot, reconciliationFreshness, renderModelStatusElement,
         ringMeaning, sceneSignature, shouldAcceptModel, shouldAcceptSnapshot } from '../web/viewer-semantics.mjs';

/* A ghost is still present in the map and remains available for inspection.
   Hiding the node layer or hovering non-node geometry removes that path. */
assert.equal(canInspectNode({type: 'item', layerVisible: true}), true);
assert.equal(canInspectNode({type: 'agent', layerVisible: true}), true);
assert.equal(canInspectNode({type: 'item', layerVisible: false}), false);
assert.equal(canInspectNode({type: null, layerVisible: true}), false);

const model = {
  schemaVersion: 1, generation: 'service-a', revision: 8, generatedAt: 2000,
  gatewayVersion: {version: '0.1.9', build: 2908, sha: '8433d22f'}, metrics: null,
  agents: [{id: 'agent-1', evidence: {parent: {status: 'known'}}}],
  items: [{id: 'wi-1', stage: null, ring: null,
    evidence: {stage: {status: 'unknown'}, ring: {status: 'unknown'}}}],
  links: [], wakes: [], messages: [],
  freshness: {state: 'rebuilding', since: 1000, reason: null,
    rebuildIntervalMs: 5000, lastCompleteAt: 500, reconciliation: null},
};

assert.equal(isModelEnvelope(model), true);
assert.equal(isModelEnvelope({...model, items: [null]}), true);
assert.equal(isModelEnvelope({...model, items: [model.items[0], model.items[0]]}), true);
assert.equal(isModelEnvelope({...model, freshness: {...model.freshness, reconciliation: undefined}}), true);
assert.equal(isModelEnvelope((({metrics, ...rest}) => rest)(model)), true);
assert.equal(shouldAcceptModel(model, null), true);
assert.equal(shouldAcceptModel({...model, revision: 8}, model), false);
assert.equal(shouldAcceptModel({...model, revision: 9}, model), true);
assert.equal(shouldAcceptModel({...model, generation: 'service-b', revision: 1}, model), true);
assert.equal(shouldAcceptModel({...model, schemaVersion: 2, revision: 9}, model), true);
assert.equal(shouldAcceptSnapshot(model, model), true);
assert.equal(shouldAcceptSnapshot({...model, generatedAt: 3000}, model), false);
assert.equal(shouldAcceptSnapshot({...model, generatedAt: 1000}, model), false);
assert.equal(shouldAcceptSnapshot({...model, revision: 7, generatedAt: 3000}, model), false);

/* Unknown envelope metadata warns while valid entities survive malformed peers. */
const incompleteSnapshot = {...model,
  schemaVersion: 2, generatedAt: null,
  futureModelField: 'kept as unknown metadata',
  gatewayVersion: {...model.gatewayVersion, futureGatewayField: true},
  freshness: {...model.freshness, futureFreshnessField: true},
  agents: [{id: 'valid-agent'}, null, {id: 'valid-agent'}, {name: 'missing id'}],
  items: null,
};
const preparedSnapshot = prepareModelSnapshot(incompleteSnapshot, model);
assert.equal(isModelEnvelope(preparedSnapshot), true);
assert.deepEqual(preparedSnapshot.agents, [{id: 'valid-agent'}]);
assert.deepEqual(preparedSnapshot.items, model.items, 'missing same-generation collections retain known rows');
const snapshotWarnings = preparedSnapshot.viewerWarnings.join(' ');
assert.match(snapshotWarnings, /schema version is unknown/);
assert.match(snapshotWarnings, /futureModelField/);
assert.match(snapshotWarnings, /futureGatewayField/);
assert.match(snapshotWarnings, /futureFreshnessField/);
assert.match(snapshotWarnings, /skipped 3 malformed agents entries/);
assert.match(snapshotWarnings, /items collection is unavailable/);
assert.match(snapshotWarnings, /snapshot timestamp is unavailable/);
const snapshotStatus = {textContent: '', className: '', title: ''};
renderModelStatusElement(snapshotStatus, {model: preparedSnapshot, streamState: 'connected'}, 2000);
assert.match(snapshotStatus.textContent, /Warning: unknown model field/);
assert.match(snapshotStatus.title, /schema version is unknown/);

const backstop = reconciliationFreshness({
  announcedChanges: 'live', unannouncedChanges: 'swept', activeBudgetMs: 1_800_000,
  activeRefreshMs: 900_000, recentWindowMs: 1_800_000, activeBatchLimit: 32,
  active: {scopeCount: 3, unconfirmedScopes: 1, oldestConfirmedAt: 1000, overBudgetScopes: 1},
  history: {scopeCount: 2, unconfirmedScopes: 2, oldestConfirmedAt: null},
}, 1_801_000);
assert.equal(backstop.announcedChanges, 'live');
assert.equal(backstop.active.ageMs, 1_800_000);
assert.equal(backstop.active.stale, true);
assert.equal(backstop.active.overBudgetScopes, 1);
assert.equal(backstop.history.ageMs, null);
assert.equal(backstop.history.scopeCount, 2);
assert.equal(backstop.history.unconfirmedScopes, 2);
const disconnectedBackstop = reconciliationFreshness({
  announcedChanges: 'stale', unannouncedChanges: 'swept', activeBudgetMs: 1_800_000,
  activeRefreshMs: 900_000, recentWindowMs: 1_800_000,
  active: {scopeCount: 1, unconfirmedScopes: 0, oldestConfirmedAt: 1000, overBudgetScopes: 0},
  history: {scopeCount: 0, unconfirmedScopes: 0, oldestConfirmedAt: null},
}, 1_801_000);
assert.equal(disconnectedBackstop.active.ageMs, 1_800_000);
assert.equal(disconnectedBackstop.active.stale, true);
const noScopes = reconciliationFreshness({
  announcedChanges: 'stale', unannouncedChanges: 'swept', activeBudgetMs: 1_800_000,
  activeRefreshMs: 900_000, recentWindowMs: 1_800_000,
  active: {scopeCount: 0, unconfirmedScopes: 0, oldestConfirmedAt: null, overBudgetScopes: 0},
  history: {scopeCount: 0, unconfirmedScopes: 0, oldestConfirmedAt: null},
}, 5_000);
assert.equal(noScopes.active.scopeCount, 0);
assert.equal(noScopes.active.ageMs, null);
assert.equal(noScopes.active.stale, false);
const neverConfirmedMetadata = {
  announcedChanges: 'live', unannouncedChanges: 'swept', activeBudgetMs: 1_800_000,
  activeRefreshMs: 900_000, recentWindowMs: 1_800_000,
  active: {scopeCount: 2, unconfirmedScopes: 2, oldestConfirmedAt: null, overBudgetScopes: 0},
  history: {scopeCount: 0, unconfirmedScopes: 0, oldestConfirmedAt: null},
};
const neverConfirmed = reconciliationFreshness(neverConfirmedMetadata, 5_000);
assert.equal(neverConfirmed.active.scopeCount, 2);
assert.equal(neverConfirmed.active.ageMs, null);
assert.equal(neverConfirmed.active.stale, false);
assert.equal(reconciliationFreshness({...neverConfirmedMetadata,
  active: {...neverConfirmedMetadata.active, unconfirmedScopes: 1}}, 5_000), null);

const deltaBase = {...model,
  agents: [{id: 'agent-1', name: 'old', idleFor: 20,
    evidence: {idleFor: {status: 'known', freshness: 'live', complete: true}}}],
  items: [{id: 'wi-1', title: 'old', oldField: true}],
  links: [{id: 'link-1', oldField: true}],
  wakes: [{id: 'wake-1', oldField: true}],
  messages: [{id: 'message-1', oldField: true}],
};
const delta = {
  schemaVersion: 1, generation: 'service-a', baseRevision: 8, revision: 9,
  generatedAt: 3000, freshness: {...model.freshness, state: 'live'},
  gatewayVersion: {version: '0.1.9', build: 2908, sha: '8433d22f'}, metrics: {pendingScopes: 2},
  changes: {
    agents: {upsert: [{id: 'agent-1', name: 'new', idleFor: 31,
      evidence: {idleFor: {status: 'known', freshness: 'live', complete: true}}}], remove: []},
    items: {upsert: [{id: 'wi-1', title: 'new'}], remove: []},
    links: {upsert: [], remove: ['link-1']},
    wakes: {upsert: [], remove: []},
    messages: {upsert: [], remove: []},
  },
};
assert.equal(isModelDelta(delta), true);
const deltaModel = applyModelDelta(deltaBase, delta);
assert.equal(deltaModel.revision, 9);
assert.equal(deltaModel.agents[0].name, 'new');
assert.equal(deltaModel.agents[0].oldField, undefined);
assert.equal(deltaModel.items[0].title, 'new');
assert.equal(deltaModel.items[0].oldField, undefined);
assert.deepEqual(deltaModel.links, []);
assert.equal(deltaModel.wakes[0].oldField, true);
assert.equal(deltaModel.messages[0].oldField, true);
assert.deepEqual(deltaModel.gatewayVersion, {version: '0.1.9', build: 2908, sha: '8433d22f'});
assert.equal(deltaModel.metrics.pendingScopes, 2);
assert.equal(applyModelDelta(deltaBase, {...delta, baseRevision: 7}), null);
assert.equal(applyModelDelta(deltaBase, {...delta, generation: 'service-b'}), null);
assert.equal(isModelDelta({...delta, changes: {...delta.changes,
  agents: {upsert: [{id: 'agent-1'}, {id: 'agent-1'}], remove: []}}}), true);
assert.equal(isModelDelta({...delta, changes: {...delta.changes,
  items: {upsert: [{id: 'wi-1'}], remove: ['wi-1']}}}), true);
const conflictingDelta = applyModelDelta(deltaBase, {...delta, changes: {...delta.changes,
  items: {upsert: [{id: 'wi-1', title: 'ambiguous'}], remove: ['wi-1']}}});
assert.deepEqual(conflictingDelta.items, deltaBase.items,
  'a conflicting row is skipped without discarding other collection updates');
assert.match(conflictingDelta.viewerWarnings.join(' '), /skipped 1 malformed items update entry/);

const partialDelta = {...delta, schemaVersion: 2, generatedAt: null,
  changes: {
    agents: {upsert: [{id: 'agent-2', name: 'valid'}, null,
      {id: 'agent-2', name: 'duplicate'}, {name: 'missing id'}], remove: []},
    items: {upsert: [{id: 'wi-2', title: 'valid'}], remove: []},
    links: null,
    wakes: {upsert: [], remove: []},
    futureCollection: {upsert: [], remove: []},
  },
};
assert.equal(isModelDelta(partialDelta), true);
const partialDeltaModel = applyModelDelta(deltaBase, partialDelta);
assert.equal(partialDeltaModel.revision, 9);
assert.deepEqual(partialDeltaModel.agents.map(row => row.id), ['agent-1', 'agent-2']);
assert.deepEqual(partialDeltaModel.items.map(row => row.id), ['wi-1', 'wi-2']);
assert.deepEqual(partialDeltaModel.links, deltaBase.links, 'malformed collection updates retain known rows');
assert.deepEqual(partialDeltaModel.messages, deltaBase.messages, 'missing collection updates retain known rows');
const deltaWarnings = partialDeltaModel.viewerWarnings.join(' ');
assert.match(deltaWarnings, /schema version is unknown/);
assert.match(deltaWarnings, /skipped 3 malformed agents update entries/);
assert.match(deltaWarnings, /links update is unavailable/);
assert.match(deltaWarnings, /messages update is unavailable/);
assert.match(deltaWarnings, /futureCollection/);
assert.match(deltaWarnings, /snapshot timestamp is unavailable/);

const oldSamples = new Map([['agent-1', 2000], ['removed-agent', 1800]]);
assert.deepEqual([...agentSampleTimes(oldSamples, {...deltaModel, generatedAt: 3000},
  {upsertedIds: []})], [['agent-1', 2000]]);
assert.deepEqual([...agentSampleTimes(oldSamples, {...deltaModel, generatedAt: 3000},
  {upsertedIds: ['agent-1']})], [['agent-1', 3000]]);
assert.deepEqual([...agentSampleTimes(oldSamples, {...deltaModel, generatedAt: 3000},
  {fullSnapshot: true, previousModel: deltaModel})], [['agent-1', 2000]]);
const newerSnapshot = {...deltaBase, revision: 10, generatedAt: 4000,
  agents: [deltaBase.agents[0], {id: 'agent-2', idleFor: 5}]};
assert.deepEqual([...agentSampleTimes(oldSamples, newerSnapshot,
  {fullSnapshot: true, previousModel: deltaBase})], [['agent-1', 2000], ['agent-2', 4000]]);
const changedSnapshot = {...newerSnapshot,
  agents: [{...deltaBase.agents[0], name: 'changed'}, newerSnapshot.agents[1]]};
assert.deepEqual([...agentSampleTimes(oldSamples, changedSnapshot,
  {fullSnapshot: true, previousModel: deltaBase})], [['agent-1', 4000], ['agent-2', 4000]]);

const page = {itemId: 'wi-1', generation: 'service-a', revision: 9,
  records: [{byUser: null, bySession: null, producer: 'producer-session', note: 'sample'}],
  total: 1, nextOffset: null, complete: true,
  freshness: {status: 'known', freshness: 'live', observedAt: 1000, source: ['work-items'], complete: true}};
assert.equal(isEvidencePage(page, {itemId: 'wi-1', generation: 'service-a', revision: 9}), true);
const unconfirmedPage = {...page, freshness: {...page.freshness,
  freshness: 'unconfirmed', unconfirmedSince: 1000}};
assert.equal(isEvidencePage(unconfirmedPage,
  {itemId: 'wi-1', generation: 'service-a', revision: 9}), true);
assert.equal(isEvidencePage({...unconfirmedPage, freshness: {
  ...unconfirmedPage.freshness, unconfirmedSince: null,
}}, {itemId: 'wi-1', generation: 'service-a', revision: 9}), false);
assert.equal(isEvidencePage(page, {itemId: 'wi-1', generation: 'service-a', revision: 10}), false);
assert.equal(isEvidencePage({...page, nextOffset: '10'},
  {itemId: 'wi-1', generation: 'service-a', revision: 9}), false);
assert.equal(isEvidencePage({...page, freshness: {
  status: 'known', state: 'live', observedAt: 1000, source: ['work-items'], complete: true,
}},
  {itemId: 'wi-1', generation: 'service-a', revision: 9}), false);
assert.equal(evidencePageFreshness(page.freshness), 'live');
assert.equal(evidencePageFreshness({...page.freshness, freshness: 'stale'}), 'stale');
assert.equal(evidencePageFreshness(unconfirmedPage.freshness), 'unconfirmed');
assert.equal(evidenceFreshnessLabel(unconfirmedPage.freshness),
  `unconfirmed since ${new Date(1000).toISOString()}`);
assert.equal(evidenceFreshnessLabel({...unconfirmedPage.freshness, freshness: 'stale'}),
  `stale · unconfirmed since ${new Date(1000).toISOString()}`);
assert.equal(evidencePageFreshness({state: 'live'}), 'unknown freshness');
assert.equal(evidenceRecordAuthor({byUser: 'mike', bySession: 'agent-session', producer: 'builder'}), 'mike');
assert.equal(evidenceRecordAuthor({bySession: 'agent-session', producer: 'builder'}), 'agent-session');
assert.equal(evidenceRecordAuthor({producer: 'builder'}), 'author unknown');
assert.equal(evidencePageQuery(0, 100, 'service-a', 9), '?offset=0&limit=100');
assert.equal(evidencePageQuery(100, 100, 'service-a', 9),
  '?offset=100&limit=100&generation=service-a&revision=9');
assert.equal(evidencePageQuery(100, 100, null, null), null);

/* Clock and evidence ages repaint without rebuilding the scene. */
assert.equal(sceneSignature(model), sceneSignature({...model, revision: 9, generatedAt: 3000,
  freshness: {...model.freshness, state: 'stale', since: 3000},
  agents: [{...model.agents[0], idleFor: 91, idleSince: 1900,
    evidence: {parent: {status: 'known', freshness: 'unconfirmed', observedAt: 3000,
      unconfirmedSince: 2500}}}],
  items: [{...model.items[0], evidence: {stage: {status: 'unknown', freshness: 'unconfirmed',
    observedAt: 3000, unconfirmedSince: 2500},
    ring: {status: 'unknown', observedAt: 3000}}}]}));
assert.notEqual(sceneSignature(model), sceneSignature({...model, items: [{...model.items[0], stage: 2}]}));
assert.equal(sceneSignature(model), sceneSignature({...model,
  revision: 9, wakes: [{id: 'wake-1'}], messages: [{id: 'message-1'}]}),
  'wake/message effects do not require rebuilding base scene geometry');

assert.equal(knownStage(0), true);
assert.equal(knownStage(6), true);
assert.equal(knownStage(null), false);
assert.equal(knownStage(7), false);
assert.equal(ringMeaning('black', 'known'), 'black');
assert.equal(ringMeaning('black', 'unknown'), 'unknown');
assert.equal(ringMeaning('black', 'known', false), 'unknown');
assert.equal(ringMeaning(null, 'known'), 'none');
assert.equal(ringMeaning(null, 'unknown'), 'unknown');
assert.equal(ringMeaning(null, undefined), 'unknown');
assert.deepEqual(factValue(model.agents[0], 'parent', null), {known: true, value: null});
assert.deepEqual(factValue(model.items[0], 'stage', null), {known: false, value: null});
const liveIdle = {idleFor: 30, evidence: {idleFor: {status: 'known', freshness: 'live', complete: true}}};
assert.equal(idleAgeSeconds(liveIdle, 2000, 7000), 35);
assert.equal(idleAgeSeconds({...liveIdle, idleSince: 5000}, 2000, 7000), 2);
assert.equal(idleAgeSeconds({...liveIdle, evidence: {idleFor: {status: 'known', freshness: 'stale', complete: true}}},
  2000, 7000), 30);
assert.equal(idleAgeSeconds({...liveIdle, idleSince: 5000,
  evidence: {idleFor: {status: 'known', freshness: 'stale', complete: true}}}, 2000, 7000), 30);
assert.equal(idleAgeSeconds({...liveIdle, evidence: {idleFor: {status: 'known', freshness: 'rebuilding', complete: true}}},
  2000, 7000), 30);
assert.equal(idleAgeSeconds({...liveIdle, evidence: {idleFor: {status: 'unknown', freshness: 'live', complete: true}}},
  2000, 7000), null);
assert.equal(idleAgeSeconds({...liveIdle, evidence: {idleFor: {status: 'known', freshness: 'live', complete: false}}},
  2000, 7000), null);
assert.equal(factCountValue(null, {status: 'unknown', complete: false}), 'unknown');
assert.equal(factCountValue(0, {status: 'known', complete: true}), '0');
assert.equal(factCountValue(0, {status: 'known', complete: false}), '0 reported (incomplete)');
assert.equal(factListValue([], {status: 'known', complete: true}), 'none');
assert.equal(factListValue([], {status: 'known', complete: false}), 'unknown · incomplete inventory');
assert.equal(factListValue(['review'], {status: 'known', complete: false}), 'review · incomplete inventory');

assert.equal(factAge(null, 1000), 'unknown');
assert.equal(factAge(1000, 1000), '0s');
assert.equal(factAge(0, 61000), '1m');
assert.deepEqual(evidenceCounts({
  agents: [{evidence: {parent: {status: 'known', freshness: 'stale', complete: false},
    idleFor: {status: 'unknown', freshness: 'rebuilding'}}}],
  items: [{evidence: {stage: {status: 'unknown', freshness: 'live'}}}],
}), {stale: 1, rebuilding: 1, unknown: 3});
