import assert from 'node:assert/strict';
import { canInspectNode } from '../web/viewer-semantics.mjs';

/* A ghost is still present in the map and remains available for inspection.
   Hiding the node layer or hovering non-node geometry removes that path. */
assert.equal(canInspectNode({type: 'item', layerVisible: true}), true);
assert.equal(canInspectNode({type: 'agent', layerVisible: true}), true);
assert.equal(canInspectNode({type: 'item', layerVisible: false}), false);
assert.equal(canInspectNode({type: null, layerVisible: true}), false);
