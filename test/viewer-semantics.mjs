import assert from 'node:assert/strict';
import { canInspectNode, evidenceCounts, factAge, factCountValue, factListValue,
         factValue, isModelEnvelope, knownStage,
         ringMeaning, sceneSignature, shouldAcceptModel } from '../web/viewer-semantics.mjs';

/* A ghost is still present in the map and remains available for inspection.
   Hiding the node layer or hovering non-node geometry removes that path. */
assert.equal(canInspectNode({type: 'item', layerVisible: true}), true);
assert.equal(canInspectNode({type: 'agent', layerVisible: true}), true);
assert.equal(canInspectNode({type: 'item', layerVisible: false}), false);
assert.equal(canInspectNode({type: null, layerVisible: true}), false);

const model = {
  schemaVersion: 1, generation: 'service-a', revision: 8, generatedAt: 2000,
  agents: [{id: 'agent-1', evidence: {parent: {status: 'known'}}}],
  items: [{id: 'wi-1', stage: null, ring: null,
    evidence: {stage: {status: 'unknown'}, ring: {status: 'unknown'}}}],
  links: [], wakes: [], messages: [],
  freshness: {state: 'rebuilding', since: 1000, reason: null,
    rebuildIntervalMs: 5000, lastCompleteAt: 500},
};

assert.equal(isModelEnvelope(model), true);
assert.equal(isModelEnvelope({...model, items: [null]}), false);
assert.equal(shouldAcceptModel(model, null), true);
assert.equal(shouldAcceptModel({...model, revision: 8}, model), false);
assert.equal(shouldAcceptModel({...model, revision: 9}, model), true);
assert.equal(shouldAcceptModel({...model, generation: 'service-b', revision: 1}, model), true);
assert.equal(shouldAcceptModel({...model, schemaVersion: 2}, model), false);

/* Clock and evidence ages repaint without rebuilding the scene. */
assert.equal(sceneSignature(model), sceneSignature({...model, revision: 9, generatedAt: 3000,
  freshness: {...model.freshness, state: 'stale', since: 3000},
  agents: [{...model.agents[0], idleFor: 91,
    evidence: {parent: {status: 'known', observedAt: 3000}}}],
  items: [{...model.items[0], evidence: {stage: {status: 'unknown', freshness: 'stale', observedAt: 3000},
    ring: {status: 'unknown', observedAt: 3000}}}]}));
assert.notEqual(sceneSignature(model), sceneSignature({...model, items: [{...model.items[0], stage: 2}]}));

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
