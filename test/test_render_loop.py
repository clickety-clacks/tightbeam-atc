import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for render-loop tests")
class RenderLoopTest(unittest.TestCase):
    def test_context_generation_disposal_guard_skips_stale_gpu_cleanup(self):
        module_url = json.dumps((ROOT / "web" / "context-generation.mjs").as_uri())
        script = f'''import assert from 'node:assert/strict';
import {{ createContextGeneration }} from {module_url};

const contexts = createContextGeneration();
contexts.advance();
let deletes = 0;
let staleReleases = 0;
const staleHandler = contexts.guard(() => {{ deletes++; }}, () => {{ staleReleases++; }});
contexts.advance();
staleHandler({{}});
assert.equal(deletes, 0, 'a prior context must not delete handles from its lost context');
assert.equal(staleReleases, 1, 'a stale resource listener must release itself');
staleHandler({{}});
assert.equal(staleReleases, 1, 'a stale handler releases only once');

const currentHandler = contexts.guard(() => {{ deletes++; }});
currentHandler({{}});
assert.equal(deletes, 1, 'resources from the active context still use normal cleanup');
'''
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_orbit_controls_idle_updates_do_not_emit_change_for_an_in_range_target(self):
        controls_url = json.dumps((ROOT / "web" / "addons" / "controls" / "OrbitControls.js").as_uri())
        three_url = json.dumps((ROOT / "web" / "three.module.js").as_uri())
        script = f'''import assert from 'node:assert/strict';
import {{ mkdtemp, readFile, rm, writeFile }} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {{ pathToFileURL }} from 'node:url';
import {{ PerspectiveCamera }} from {three_url};

const temporary = await mkdtemp(path.join(os.tmpdir(), 'atc-orbit-controls-'));
try {{
  const controlsSource = await readFile(new URL({controls_url}), 'utf8');
  const rewritten = controlsSource.replace("from 'three';", `from ${{JSON.stringify(new URL({three_url}).href)}};`);
  assert.notEqual(rewritten, controlsSource, 'the controls module should use the checked-in Three.js module');
  const modulePath = path.join(temporary, 'OrbitControls.mjs');
  await writeFile(modulePath, rewritten);
  const {{ OrbitControls }} = await import(pathToFileURL(modulePath));
  const camera = new PerspectiveCamera(55, 1, 0.1, 500);
  camera.position.set(38.125, 42.375, 67.625);
  const element = {{style: {{}}, addEventListener() {{}}, removeEventListener() {{}}}};
  const controls = new OrbitControls(camera, element);
  controls.enableDamping = true;
  controls.target.set(7.12345, 2.23456, -4.56789);
  controls.cursor.set(1.23456, -0.56789, 2.34567);
  camera.lookAt(controls.target);
  controls.update();
  const stableTarget = controls.target.clone();
  let changes = 0;
  controls.addEventListener('change', () => {{ changes++; }});
  for (let i = 0; i < 120; i++)
    assert.equal(controls.update(), false, 'an unchanged in-range camera should stay idle');
  assert.ok(controls.target.equals(stableTarget), 'an in-range target must remain bit-for-bit stable');
  assert.equal(changes, 0, 'idle control updates must not invalidate rendering');
  controls.dispose();

  const limitedCamera = new PerspectiveCamera(55, 1, 0.1, 500);
  limitedCamera.position.set(20, 4, 0);
  const limited = new OrbitControls(limitedCamera, element);
  limited.cursor.set(1, 2, 3);
  limited.target.set(20, 2, 3);
  limited.maxTargetRadius = 5;
  limited.update();
  assert.ok(Math.abs(limited.target.distanceTo(limited.cursor) - 5) < 1e-10,
    'out-of-range targets must still be clamped to the configured radius');
  limited.dispose();
}} finally {{
  await rm(temporary, {{recursive:true, force:true}});
}}
'''
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_idle_hidden_context_loss_and_restore_gates(self):
        module_url = json.dumps((ROOT / "web" / "render-loop.mjs").as_uri())
        script = f'''import assert from 'node:assert/strict';
import {{ areProgramsValid, createDeferredUpdateGate, createRenderLoop }} from {module_url};

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
let contextValid = true;
const loop = createRenderLoop({{
  canvas, document, now: () => time,
  requestFrame(callback) {{ const id = nextId++; frames.set(id, callback); return id; }},
  cancelFrame(id) {{ frames.delete(id); }},
  setTimer(callback, delay) {{ const id = nextId++; timers.set(id, {{callback, delay}}); return id; }},
  clearTimer(id) {{ timers.delete(id); }},
  isContextValid() {{ return contextValid; }},
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
assert.equal(loop.suspended, false);
assert.equal(loop.scheduled, false, 'an unchanged scene must go idle');

animationDelay = 200;
loop.invalidate();
drawOneFrame();
assert.equal(timers.size, 1, 'active motion should schedule a throttled follow-up');
loop.invalidate();
assert.equal(frames.size, 1, 'a live change should queue a prompt frame');
document.hidden = true;
document.dispatch('visibilitychange');
assert.equal(loop.suspended, true, 'hidden pages suspend model scene work');
assert.equal(frames.size, 0, 'hiding the page must cancel a queued frame');
loop.invalidate();
assert.equal(loop.scheduled, false, 'hidden pages must not draw');
document.hidden = false;
document.dispatch('visibilitychange');
assert.equal(loop.suspended, false, 'showing the page resumes scene work');
drawOneFrame();
assert.equal(draws, 3, 'showing the page must redraw the current scene');
assert.equal(timers.size, 1, 'active motion should resume at its throttled rate');

let prevented = false;
canvas.dispatch('webglcontextlost', {{preventDefault() {{ prevented = true; }}}});
loop.invalidate();
assert.equal(prevented, true, 'context loss must be prevented so the browser can restore it');
assert.equal(loop.contextLost, true);
assert.equal(loop.suspended, true, 'a lost WebGL context suspends model scene work');
assert.equal(lostEvents, 1);
assert.equal(loop.scheduled, false, 'lost contexts must not draw');
canvas.dispatch('webglcontextrestored');
assert.equal(loop.suspended, false, 'context restoration resumes scene work');
drawOneFrame();
assert.equal(loop.contextLost, false);
assert.equal(restoredEvents, 1);
assert.equal(draws, 4, 'context restore must redraw the scene');
contextValid = false;
assert.equal(loop.suspended, true, 'an invalid renderer context suspends model scene work');
contextValid = true;
assert.equal(loop.suspended, false);
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

    def test_hidden_scene_rebuilds_coalesce_and_catch_up_to_latest_model(self):
        module_url = json.dumps((ROOT / "web" / "render-loop.mjs").as_uri())
        script = f'''import assert from 'node:assert/strict';
import {{ createDeferredUpdateGate }} from {module_url};

let suspended = true;
let acceptedRevision = 0;
let renderedRevision = 0;
let visibleFrameRevision = null;
let rebuilds = 0;
const updates = createDeferredUpdateGate(() => suspended);
function drawFrame() {{ visibleFrameRevision = renderedRevision; }}
function acceptRevision(revision) {{
  acceptedRevision = revision;
  updates.run(() => {{ renderedRevision = acceptedRevision; rebuilds++; }});
}}

acceptRevision(1);
acceptRevision(2);
assert.equal(acceptedRevision, 2, 'model ingestion continues while rendering is suspended');
assert.equal(renderedRevision, 0, 'scene construction waits while hidden');
assert.equal(rebuilds, 0);
assert.equal(updates.pending, true);
assert.equal(updates.flush(), false, 'a hidden page cannot flush scene work');

suspended = false;
assert.equal(updates.flush(), true, 'showing the page reconciles before drawing');
assert.equal(renderedRevision, 2, 'catch-up uses only the latest accepted model');
assert.equal(rebuilds, 1, 'several hidden updates coalesce into one rebuild');
drawFrame();
assert.equal(visibleFrameRevision, 2, 'the first visible frame uses the caught-up scene');
assert.equal(updates.pending, false);
assert.equal(updates.flush(), false, 'a completed catch-up is not repeated');

suspended = true;
acceptRevision(3);
acceptRevision(4);
assert.equal(renderedRevision, 2, 'context loss also defers scene construction');
suspended = false;
assert.equal(updates.flush(), true);
assert.equal(renderedRevision, 4, 'context restoration catches up to the latest revision');

acceptRevision(5);
assert.equal(renderedRevision, 5, 'visible updates remain immediate');
assert.equal(rebuilds, 3);
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

    def test_sixty_fps_animation_chains_frames_without_a_timer(self):
        module_url = json.dumps((ROOT / "web" / "render-loop.mjs").as_uri())
        script = f'''import assert from 'node:assert/strict';
import {{ createRenderLoop }} from {module_url};

const document = {{ hidden: false, addEventListener() {{}}, removeEventListener() {{}} }};
let nextId = 1;
const frames = new Map();
const timers = new Map();
let draws = 0;
const loop = createRenderLoop({{
  document,
  requestFrame(callback) {{ const id = nextId++; frames.set(id, callback); return id; }},
  cancelFrame(id) {{ frames.delete(id); }},
  setTimer(callback, delay) {{ const id = nextId++; timers.set(id, {{callback, delay}}); return id; }},
  clearTimer(id) {{ timers.delete(id); }},
  draw() {{ draws++; return 1000 / 60; }},
}});
function drawOneFrame() {{
  const [id, callback] = frames.entries().next().value ?? [];
  assert.ok(id, 'an animation frame should be queued');
  frames.delete(id);
  callback(0);
}}

drawOneFrame();
assert.equal(draws, 1);
assert.equal(timers.size, 0, 'a 1000/60 ms interval must not queue a timer');
assert.equal(frames.size, 1, 'the next display frame should be requested directly');
loop.invalidate();
assert.equal(frames.size, 1, 'invalidation must not duplicate the queued frame');
drawOneFrame();
assert.equal(draws, 2);
assert.equal(timers.size, 0, 'frame chaining must remain timer-free');
loop.destroy();
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
