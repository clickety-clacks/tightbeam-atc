// Extracts deskDisplayList() straight out of web/index.html and runs it in a
// sandbox, so this test exercises the real client source, not a hand-copied
// re-implementation that could quietly drift from it.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const html = readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');

const start = html.indexOf('function deskDisplayList(){');
assert.ok(start >= 0, 'deskDisplayList not found in web/index.html');
let depth = 0, i = html.indexOf('{', start), end = -1;
for (; i < html.length; i++){
  if (html[i] === '{') depth++;
  else if (html[i] === '}'){ depth--; if (depth === 0){ end = i + 1; break; } }
}
assert.ok(end > start, 'could not bound deskDisplayList body');
const source = html.slice(start, end);

function run(DATA, followups){
  const sandbox = { DATA, followups, result: undefined };
  vm.createContext(sandbox);
  vm.runInContext(source + '\nresult = deskDisplayList();', sandbox);
  return sandbox.result;
}

function decision(id, opts = {}){
  return { id, question: id + '?', raiserId: 'agent:x', raisedAt: 0, deadlineAt: 10_000,
           supersedes: null, note: null, ...opts };
}
function followup(drId, opts = {}){
  return { drId, question: 'follow-up on ' + drId, originalQuestion: drId + '?',
           raisedAt: 1_000, deadlineAt: 20_000, ...opts };
}

// Baseline: no followups, decisions pass through untouched.
{
  const list = run({ decisions: [decision('D1')] }, []);
  assert.equal(list.length, 1);
  assert.equal(list[0].key, 'D1');
  assert.equal(list[0].answered, false);
}

// Followed up, not yet answered: D1 still open, folds its own followup thread.
{
  const list = run({ decisions: [decision('D1')] }, [followup('D1')]);
  assert.equal(list.length, 1);
  assert.equal(list[0].key, 'D1');
  assert.equal(list[0].answered, false);
  assert.equal(list[0].shown.id, 'D1');
}

// Answered while the original LINGERS in the feed: folds onto D1, D2 is skipped
// as a standalone row (no duplicate).
{
  const list = run(
    { decisions: [decision('D1'), decision('D2', { supersedes: 'D1' })] },
    [followup('D1')]
  );
  assert.equal(list.length, 1);
  assert.equal(list[0].key, 'D1');
  assert.equal(list[0].answered, true);
  assert.equal(list[0].shown.id, 'D2');
}

// Answered and the original has ALREADY DROPPED from the feed (status=open
// only, real production shape) — this is the bug the reviewer caught: D2 must
// still anchor and render, keyed by D1 so the chip doesn't jump position.
{
  const list = run(
    { decisions: [decision('D2', { supersedes: 'D1', deadlineAt: 30_000 })] },
    [followup('D1', { deadlineAt: 20_000 })]
  );
  assert.equal(list.length, 1, 'the answered decision must not disappear');
  assert.equal(list[0].key, 'D1');
  assert.equal(list[0].answered, true);
  assert.equal(list[0].shown.id, 'D2');
  assert.equal(list[0].deadlineAt, 20_000, 'uses the earlier of the two deadlines');
}

// Same dropped-original case, but the NEW row's deadline is earlier than the
// original's captured deadline — must use the earlier one.
{
  const list = run(
    { decisions: [decision('D2', { supersedes: 'D1', deadlineAt: 5_000 })] },
    [followup('D1', { deadlineAt: 20_000 })]
  );
  assert.equal(list[0].deadlineAt, 5_000);
}

// An unrelated third decision is unaffected by the folding logic.
{
  const list = run(
    { decisions: [decision('D2', { supersedes: 'D1' }), decision('D3')] },
    [followup('D1')]
  );
  assert.equal(list.length, 2);
  assert.equal(JSON.stringify(list.map(e => e.key).sort()), JSON.stringify(['D1', 'D3']));
}

console.log('test_desk_display_list.mjs: all assertions passed');
