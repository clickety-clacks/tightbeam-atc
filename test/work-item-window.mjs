import assert from 'node:assert/strict';
import {DAY_MS, itemWindow, itemInWindow, orphanRadius, windowDescription}
  from '../web/work-item-window.mjs';
const now = 100 * DAY_MS;
const recent = {state:'closed', finishedAt: now - DAY_MS / 2, finishedAtApproximate:true};
assert.equal(itemInWindow(recent, 1, now), true);
assert.equal(itemInWindow(recent, 0, now), false);
assert.match(windowDescription(recent), /approximate/);
assert.equal(itemInWindow({...recent, finishedAt:now - DAY_MS}, 1, now), false);
assert.equal(itemInWindow({...recent, finishedAt:null}, 30, now), false);
assert.equal(itemInWindow({...recent, state:'iceboxed'}, 1, now), true);
assert.equal(itemWindow({...recent, state:'failed', merged:true}), null);
for (const days of [0, 1, 30]) {
  assert.equal(itemInWindow({state:'open', holders:[]}, days, now), true);
  assert.equal(itemInWindow({state:'open', holders:null}, days, now), true);
  assert.equal(itemInWindow({state:'failed'}, days, now), true);
}
const landed = {state:'open', merged:true, landings:[{observedAt:now - 2 * DAY_MS},
  {observedAt:now}], evidence:{merged:{observedAt:now}}};
assert.equal(itemInWindow(landed, 1, now), false);
assert.equal(itemInWindow({...landed, landings:[]}, 30, now), false);
assert.equal(itemInWindow({...landed, landings:[], landingAt:now}, 1, now), true);
assert.equal(orphanRadius({latestActivityAt:now}, 20, 80, now), 20);
assert.equal(orphanRadius({latestActivityAt:now - 15 * DAY_MS}, 20, 80, now), 50);
assert.equal(orphanRadius({latestActivityAt:now - 30 * DAY_MS}, 20, 80, now), 80);
assert.equal(orphanRadius({latestActivityAt:now - 90 * DAY_MS}, 20, 80, now), 80);
assert.equal(orphanRadius({}, 20, 80, now), 80);
