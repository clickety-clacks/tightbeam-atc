import assert from 'node:assert/strict';
import { accessibleUnknownDetails, unknownItemFacts } from '../web/viewer-unknown-status.mjs';

const unconfirmedMerge = unknownItemFacts({
  stageKnown: true, holdersKnown: true, landed: true, ring: 'unknown',
});
assert.deepEqual(unconfirmedMerge, ['merge status is not confirmed']);
assert.equal(accessibleUnknownDetails(unconfirmedMerge),
  'Unknown: merge status is not confirmed');

assert.deepEqual(unknownItemFacts({
  stageKnown: true, holdersKnown: true, landed: true, ring: 'black',
}), [], 'known rings keep their existing appearance and have no unknown marker');

assert.deepEqual(unknownItemFacts({
  stageKnown: false, holdersKnown: false, landed: false, ring: 'unknown',
}), ['stage is unknown', 'holder information is unknown']);
assert.equal(accessibleUnknownDetails([]), '');
