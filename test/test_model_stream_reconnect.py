import json
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InlineModuleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_module = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") == "module":
            self.in_module = True

    def handle_endtag(self, tag):
        if tag == "script" and self.in_module:
            self.in_module = False

    def handle_data(self, data):
        if self.in_module:
            self.parts.append(data)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for stream lifecycle behavior tests")
class ModelStreamReconnectTest(unittest.TestCase):
    def test_closed_stream_retries_with_a_cap_and_resets_after_a_model(self):
        parser = InlineModuleParser()
        parser.feed((ROOT / "web" / "index.html").read_text())
        module = "".join(parser.parts)
        constants_start = module.index("const MODEL_STREAM_RETRY_BASE_MS =")
        constants_end = module.index("\nlet agentSamples", constants_start)
        lifecycle_start = module.index("function scheduleModelStreamRetry(){")
        lifecycle_end = module.index("\n\n/* ---------- theme toggle ---------- */", lifecycle_start)
        self.assertGreater(constants_end, constants_start)
        self.assertGreater(lifecycle_end, lifecycle_start)

        evaluation = r'''import assert from 'node:assert/strict';
import vm from 'node:vm';
const moduleSource = __MODULE_SOURCE__;
const constantsStart = moduleSource.indexOf('const MODEL_STREAM_RETRY_BASE_MS =');
const constantsEnd = moduleSource.indexOf('\nlet agentSamples', constantsStart);
const lifecycleStart = moduleSource.indexOf('function scheduleModelStreamRetry(){');
const lifecycleEnd = moduleSource.indexOf('\n\n/* ---------- theme toggle ---------- */', lifecycleStart);
assert.ok(constantsStart >= 0 && constantsEnd > constantsStart);
assert.ok(lifecycleStart >= 0 && lifecycleEnd > lifecycleStart);

const timers = [];
function fakeSetTimeout(callback, delay){
  const timer = {callback, delay, cleared:false};
  timers.push(timer);
  return timer;
}
function fakeClearTimeout(timer){ if (timer) timer.cleared = true; }
async function fireNextTimer(){
  const timer = timers.find(value => !value.cleared && !value.fired);
  assert.ok(timer, 'a retry timer must be scheduled');
  timer.fired = true;
  timer.callback();
  await Promise.resolve();
}
class FakeEventSource{
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances = [];
  constructor(url){
    this.url = url;
    this.readyState = FakeEventSource.CONNECTING;
    this.listeners = new Map();
    FakeEventSource.instances.push(this);
  }
  close(){ this.readyState = FakeEventSource.CLOSED; }
  open(){ this.readyState = FakeEventSource.OPEN; this.onopen?.(); }
  error(state){ this.readyState = state; this.onerror?.(); }
  emit(type, data){
    for (const listener of this.listeners.get(type) || []) listener({data:JSON.stringify(data)});
  }
  addEventListener(type, listener){
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
}
const context = vm.createContext({
  assert,
  timers,
  fireNextTimer,
  FakeEventSource,
  EventSource: FakeEventSource,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  renderModelStatus: () => {},
  receiveModel: model => { context.lastModel = model; context.received.push(model); return true; },
  applyModelDelta: () => null,
  DATA: {items:[]},
  lastModel: null,
  streamState: 'connecting',
  streamError: null,
  startupWarning: null,
  startupStatusChecked: true,
  startupStatusPending: null,
  modelEventSource: null,
  resyncTimer: null,
  received: [],
});
const lifecycle = moduleSource.slice(lifecycleStart, lifecycleEnd);
new vm.Script(`
${moduleSource.slice(constantsStart, constantsEnd)}
${lifecycle}
globalThis.__test = {
  get source(){ return modelEventSource; },
  get retryDelay(){ return modelStreamRetryDelay; },
  get timer(){ return resyncTimer; },
  fireNextTimer,
};
`).runInContext(context);

await vm.runInContext(`(async()=>{
  await startModelStream();
  const first = __test.source;
  assert.equal(FakeEventSource.instances.length, 1);
  assert.equal(first.url, './api/events');
  first.error(FakeEventSource.CONNECTING);
  assert.equal(__test.source, first, 'native transient reconnect remains attached');
  assert.equal(__test.timer, null, 'transient reconnect does not add another retry loop');

  first.error(FakeEventSource.CLOSED);
  assert.equal(__test.source, null, 'terminally closed source is released');
  assert.equal(__test.timer.delay, 1000);
  first.error(FakeEventSource.CLOSED);
  assert.equal(timers.filter(timer=>!timer.cleared&&!timer.fired).length, 1,
    'repeated terminal notifications coalesce into one retry');

  await __test.fireNextTimer();
  const second = __test.source;
  assert.equal(FakeEventSource.instances.length, 2);
  second.error(FakeEventSource.CLOSED);
  assert.equal(__test.timer.delay, 2000);

  await __test.fireNextTimer();
  const third = __test.source;
  third.error(FakeEventSource.CLOSED);
  assert.equal(__test.timer.delay, 2000);

  await __test.fireNextTimer();
  const fourth = __test.source;
  fourth.error(FakeEventSource.CLOSED);
  assert.equal(__test.timer.delay, 2000, 'terminal retries remain capped');

  await __test.fireNextTimer();
  const restored = __test.source;
  restored.open();
  restored.emit('model', {generation:'fixture', revision:2, items:Array(25).fill({})});
  assert.equal(received.at(-1).revision, 2, 'the latest snapshot is accepted on reconnect');
  assert.equal(__test.retryDelay, 1000, 'accepted full model resets the retry delay');
  assert.equal(__test.source, restored);

  restored.error(FakeEventSource.CLOSED);
  assert.equal(__test.timer.delay, 1000, 'next terminal error starts from the base delay');
  await __test.fireNextTimer();
  assert.notEqual(__test.source, restored, 'the scheduled callback reconnects with a fresh source');
})()` , context);
'''.replace("__MODULE_SOURCE__", json.dumps(module))

        with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs") as script_file:
            script_file.write(evaluation)
            script_file.flush()
            result = subprocess.run(
                [shutil.which("node"), script_file.name],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
        self.assertEqual(0, result.returncode, result.stderr)
