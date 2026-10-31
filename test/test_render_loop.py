import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for render-loop tests")
class RenderLoopTest(unittest.TestCase):
    def test_idle_hidden_context_loss_and_restore_gates(self):
        module_url = json.dumps((ROOT / "web" / "render-loop.mjs").as_uri())
        script = f'''import assert from 'node:assert/strict';
import {{ areProgramsValid, createRenderLoop }} from {module_url};

class Target {{
  listeners = new Map();
  addEventListener(type, listener) {{
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }}
  removeEventListener(type, listener) {{ this.listeners.get(type)?.delete(listener); }}
  dispatch(type, event = {{}}) {{
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }}
}}

const document = new Target();
document.hidden = false;
const canvas = new Target();
let time = 0;
let nextId = 1;
const frames = new Map();
const timers = new Map();
let draws = 0;
let lostEvents = 0;
let restoredEvents = 0;
let animationDelay = 0;
const loop = createRenderLoop({{
  canvas, document, now: () => time,
  requestFrame(callback) {{ const id = nextId++; frames.set(id, callback); return id; }},
  cancelFrame(id) {{ frames.delete(id); }},
  setTimer(callback, delay) {{ const id = nextId++; timers.set(id, {{callback, delay}}); return id; }},
  clearTimer(id) {{ timers.delete(id); }},
  draw() {{ draws++; return animationDelay; }},
  onContextLost() {{ lostEvents++; }},
  onContextRestored() {{ restoredEvents++; }},
}});
function drawOneFrame() {{
  const [id, callback] = frames.entries().next().value ?? [];
  assert.ok(id, 'a frame should be queued');
  frames.delete(id);
  callback(time);
}}

drawOneFrame();
assert.equal(draws, 1);
assert.equal(loop.scheduled, false, 'an unchanged scene must go idle');

animationDelay = 200;
loop.invalidate();
drawOneFrame();
assert.equal(timers.size, 1, 'active motion should schedule a throttled follow-up');
loop.invalidate();
assert.equal(frames.size, 1, 'a live change should queue a prompt frame');
document.hidden = true;
document.dispatch('visibilitychange');
assert.equal(frames.size, 0, 'hiding the page must cancel a queued frame');
loop.invalidate();
assert.equal(loop.scheduled, false, 'hidden pages must not draw');
document.hidden = false;
document.dispatch('visibilitychange');
drawOneFrame();
assert.equal(draws, 3, 'showing the page must redraw the current scene');
assert.equal(timers.size, 1, 'active motion should resume at its throttled rate');

let prevented = false;
canvas.dispatch('webglcontextlost', {{preventDefault() {{ prevented = true; }}}});
loop.invalidate();
assert.equal(prevented, true, 'context loss must be prevented so the browser can restore it');
assert.equal(loop.contextLost, true);
assert.equal(lostEvents, 1);
assert.equal(loop.scheduled, false, 'lost contexts must not draw');
canvas.dispatch('webglcontextrestored');
drawOneFrame();
assert.equal(loop.contextLost, false);
assert.equal(restoredEvents, 1);
assert.equal(draws, 4, 'context restore must redraw the scene');
loop.destroy();
assert.equal(document.listeners.get('visibilitychange').size, 0);

const program = {{}};
const gl = {{ LINK_STATUS: 1, isContextLost: () => false,
  isProgram: candidate => candidate === program, getProgramParameter: () => true }};
assert.equal(areProgramsValid(gl, [{{program}}]), true);
assert.equal(areProgramsValid(gl, [{{program: {{}}}}]), false);
assert.equal(areProgramsValid(gl, [{{program, diagnostics: {{runnable: false}}}}]), false);
assert.equal(areProgramsValid({{...gl, isContextLost: () => true}}, [{{program}}]), false);
const failedLink = {{}};
assert.equal(areProgramsValid({{...gl, isProgram: candidate => candidate === failedLink,
  getProgramParameter: () => false}}, [{{program: failedLink}}]), false);
'''
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_delayed_visual_change_invalidates_an_idle_loop(self):
        module_url = json.dumps((ROOT / "web" / "render-loop.mjs").as_uri())
        script = f'''import assert from 'node:assert/strict';
import {{ createRenderLoop, scheduleVisualChange }} from {module_url};

const document = {{ hidden: false, addEventListener() {{}}, removeEventListener() {{}} }};
let nextId = 1;
const frames = new Map();
let draws = 0;
const loop = createRenderLoop({{
  document,
  requestFrame(callback) {{ const id = nextId++; frames.set(id, callback); return id; }},
  cancelFrame(id) {{ frames.delete(id); }},
  draw() {{ draws++; return 0; }},
}});
function drawOneFrame() {{
  const [id, callback] = frames.entries().next().value ?? [];
  assert.ok(id, 'a frame should be queued');
  frames.delete(id);
  callback(0);
}}

drawOneFrame();
assert.equal(loop.scheduled, false, 'the unchanged scene should be idle');
const objects = [];
let delayedCallback;
scheduleVisualChange(() => objects.push('wake'), 240, loop.invalidate,
  (callback, delay) => {{ assert.equal(delay, 240); delayedCallback = callback; return 9; }});
assert.equal(loop.scheduled, false, 'waiting for the visual timer should not draw');
delayedCallback();
assert.deepEqual(objects, ['wake']);
assert.equal(frames.size, 1, 'adding a delayed visual should request a frame');
drawOneFrame();
assert.equal(draws, 2);
assert.equal(loop.scheduled, false, 'the scene should settle after the new visual draws');
loop.destroy();
'''
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_render_interval_stops_for_unchanged_status_and_hover(self):
        module_url = json.dumps((ROOT / "web" / "render-loop.mjs").as_uri())
        script = f'''import assert from 'node:assert/strict';
import {{ renderInterval }} from {module_url};

assert.equal(renderInterval(), 0, 'an unchanged scene must not keep drawing');
assert.equal(renderInterval({{hovered:true, running:true}}), 0,
  'persistent status and hover state must not keep an animation loop alive');
assert.equal(renderInterval({{cameraMoving:true}}), 1000 / 60);
assert.equal(renderInterval({{controlsChanged:true}}), 1000 / 60);
assert.equal(renderInterval({{pulses:true}}), 1000 / 60);
assert.equal(renderInterval({{messages:true}}), 1000 / 60);
'''
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_live_invalidation_bypasses_animation_throttle(self):
        module_url = json.dumps((ROOT / "web" / "render-loop.mjs").as_uri())
        script = f'''import assert from 'node:assert/strict';
import {{ createRenderLoop }} from {module_url};

const listeners = new Map();
const document = {{
  hidden: false,
  addEventListener(type, listener) {{ listeners.set(type, listener); }},
  removeEventListener(type) {{ listeners.delete(type); }},
}};
let time = 0;
let nextId = 1;
const frames = new Map();
const timers = new Map();
let draws = 0;
const loop = createRenderLoop({{
  document, now: () => time,
  requestFrame(callback) {{ const id = nextId++; frames.set(id, callback); return id; }},
  cancelFrame(id) {{ frames.delete(id); }},
  setTimer(callback, delay) {{ const id = nextId++; timers.set(id, {{callback, delay}}); return id; }},
  clearTimer(id) {{ timers.delete(id); }},
  draw() {{ draws++; return 200; }},
}});
function drawOneFrame() {{
  const [id, callback] = frames.entries().next().value ?? [];
  assert.ok(id, 'a frame should be queued');
  frames.delete(id);
  callback(time);
}}

drawOneFrame();
assert.equal(draws, 1);
assert.equal(timers.size, 1, 'continuous motion should be rate limited');
assert.equal([...timers.values()][0].delay, 200);
loop.invalidate();
assert.equal(timers.size, 0, 'a new live change must cancel the throttle wait');
assert.equal(frames.size, 1, 'a live change should receive the next animation frame');
loop.pause();
assert.equal(loop.scheduled, false);
loop.resume();
drawOneFrame();
assert.equal(draws, 2);
loop.destroy();
'''
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
