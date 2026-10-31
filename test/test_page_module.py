import json
import re
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


@unittest.skipUnless(shutil.which("node"), "Node.js is required for page module smoke test")
class PageModuleSmokeTest(unittest.TestCase):
    def test_inline_page_module_parses_and_uses_current_hover_picker(self):
        parser = InlineModuleParser()
        parser.feed((ROOT / "web" / "index.html").read_text())
        module = "".join(parser.parts)
        self.assertTrue(module.strip(), "index.html must contain an inline module script")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs") as source_file:
            source_file.write(module)
            source_file.flush()
            result = subprocess.run(
                [shutil.which("node"), "--check", source_file.name],
                capture_output=True,
                text=True,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotRegex(module, r"\bpickAllHover\b")
        self.assertRegex(module, r"window\.__atc\s*=\s*\{[\s\S]*?pickNode,\s*pickHoverNodes,")
        self.assertRegex(module, r"function touchHoldKeys\(e\)\s*\{\s*return pickHoverNodes\(e\)")

        evaluation = r'''import assert from 'node:assert/strict';
import vm from 'node:vm';
const moduleSource = __MODULE_SOURCE__;
const objectStart = moduleSource.indexOf('window.__atc = {');
const objectEnd = moduleSource.indexOf('\n};', objectStart) + 3;
assert.ok(objectStart >= 0 && objectEnd > objectStart, 'window.__atc initializer must exist');
const noop = () => {};
const context = vm.createContext({
  window: {},
  clearRect: noop, frameDistance: noop, frameTo: noop, filteredView: noop,
  neighbourhoodView: noop, pickNode: noop, pickHoverNodes: noop,
  pickArrow: noop, pickClick: noop, arrowInScope: noop, focusArrow: noop,
  selectables: [], selKey: noop, inScope: noop, setInputMode: noop, apiPull: noop,
});
new vm.Script('"use strict";\n' + moduleSource.slice(objectStart, objectEnd)).runInContext(context);
assert.equal(typeof context.window.__atc.pickHoverNodes, 'function');

const holdStart = moduleSource.indexOf('function touchHoldKeys(e){');
const holdEnd = moduleSource.indexOf('\n}', holdStart) + 2;
assert.ok(holdStart >= 0 && holdEnd > holdStart, 'touchHoldKeys must exist');
const holdContext = vm.createContext({
  pickHoverNodes: () => [{key: 'i:item'}],
  selKey: node => node.key,
});
new vm.Script('"use strict";\n' + moduleSource.slice(holdStart, holdEnd) +
  '\nglobalThis.heldKeys = touchHoldKeys({});').runInContext(holdContext);
assert.deepEqual(Array.from(holdContext.heldKeys), ['i:item']);
'''.replace("__MODULE_SOURCE__", json.dumps(module))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs") as smoke_file:
            smoke_file.write(evaluation)
            smoke_file.flush()
            result = subprocess.run(
                [shutil.which("node"), smoke_file.name],
                capture_output=True,
                text=True,
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_work_item_detail_renders_full_evidence(self):
        parser = InlineModuleParser()
        parser.feed((ROOT / "web" / "index.html").read_text())
        module = "".join(parser.parts)
        self.assertTrue(module.strip(), "index.html must contain an inline module script")

        evaluation = r'''import assert from 'node:assert/strict';
import vm from 'node:vm';
const moduleSource = __MODULE_SOURCE__;
const contentStart = moduleSource.indexOf('function nodeContent(u, compact){');
const contentEnd = moduleSource.indexOf('\nconst detailEl', contentStart);
assert.ok(contentStart >= 0 && contentEnd > contentStart, 'nodeContent must exist');
const context = vm.createContext({
  esc: value => String(value ?? ''),
  factText: (_entity, _fact, value, render) => render(value),
  factListValue: () => 'full list evidence',
  factCountValue: value => value ?? 'unknown',
  knownStage: () => true,
  ringMeaning: () => 'unknown',
  unknownItemFacts: () => ({}),
  accessibleUnknownDetails: () => '',
  evidenceMarkup: () => '<div>full source evidence</div>',
  evidenceNotesMarkup: () => '<div>full attest evidence</div>',
  workPlacementText: () => '',
  OPEN_WORK_STAGE_QUALIFIER: '',
  byId: {},
});
const item = {
  type: 'item', stageUnknown: false, holdersUnknown: false,
  landed: false, unfinished: false,
  w: {
    id: 'wi-full-detail', short: 'wi-full-detail', title: 'Focused work item',
    state: 'open', stage: 4, ring: 'unknown', code: true,
    evidence: {}, assignments: {open: 0, terminal: 0}, turns: null, holders: [],
  },
};
new vm.Script('"use strict";\n' + moduleSource.slice(contentStart, contentEnd) +
  '\nglobalThis.detail = nodeContent(globalThis.item, false);').runInContext(
    Object.assign(context, {item}));
assert.match(context.detail, /full source evidence/);
assert.match(context.detail, /full attest evidence/);
assert.match(context.detail, /wi-full-detail/);
'''.replace("__MODULE_SOURCE__", json.dumps(module))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs") as smoke_file:
            smoke_file.write(evaluation)
            smoke_file.flush()
            result = subprocess.run(
                [shutil.which("node"), smoke_file.name],
                capture_output=True,
                text=True,
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_hover_card_backgrounds_are_opaque(self):
        page = (ROOT / "web" / "index.html").read_text()
        selectors = (
            ("dark", r"#hovercards\s+\.hc\s*\{([^}]*)\}"),
            ("light", r"body\.light\s+#hovercards\s+\.hc\s*\{([^}]*)\}"),
        )
        for theme, selector in selectors:
            with self.subTest(theme=theme):
                match = re.search(selector, page)
                self.assertIsNotNone(match, f"{theme} hover card style must exist")
                color = re.search(r"background\s*:\s*(#[0-9a-fA-F]{6})\s*;", match.group(1))
                self.assertIsNotNone(color, f"{theme} hover card background must be opaque")

    def test_compact_item_hints_include_r8_placement_for_unfinished_tiers(self):
        parser = InlineModuleParser()
        parser.feed((ROOT / "web" / "index.html").read_text())
        module = "".join(parser.parts)
        start = module.index("function nodeHintContent(u){")
        end = module.index("\nfunction nodeContent(u, compact){", start)
        source = module[start:end]
        hints_url = json.dumps((ROOT / "web" / "viewer-hints.mjs").as_uri())
        tiers_url = json.dumps((ROOT / "web" / "work-item-tiers.mjs").as_uri())

        evaluation = r'''import assert from 'node:assert/strict';
import vm from 'node:vm';
const {agentHintSummary, itemHintSummary} = await import(__HINTS_URL__);
const {classifyWorkItemPlacement, workPlacementText} = await import(__TIERS_URL__);
const moduleSource = __NODE_HINT_SOURCE__;
const active = {id: 'agent-active', name: 'Active agent', retired: false};
const retired = {id: 'agent-retired', name: 'Retired agent', retired: true};
const byId = new Map([[active.id, active], [retired.id, retired]]);
const sharedEvidence = {
  identity: {status: 'known', freshness: 'live', complete: true},
  stage: {status: 'known', freshness: 'unconfirmed', complete: true},
  assignments: {status: 'known', freshness: 'live', complete: true},
};
const item = (id, assignments, holders) => ({
  id, short: id, title: id, state: 'open', stage: 5, assignments, holders,
  evidence: {...sharedEvidence},
});
const cases = [
  [item('cloud', {open: 0, terminal: 0}, []),
    'cloud · not started, not carded'],
  [item('carded', {open: 1, terminal: 0}, [active.id]),
    'carded · to Active agent'],
  [item('perimeter', {open: 0, terminal: 2}, []),
    'perimeter ring · started, not carded to an active agent'],
  [item('retired-card', {open: 1, terminal: 0}, [retired.id]),
    'perimeter ring · started, not carded to an active agent · open card held by a retired agent'],
  [item('unknown-holders', {open: 0, terminal: 0}, null),
    'unknown · assignment state could not be established, so this item is not placed in a tier · holders unknown'],
  [item('incomplete-inventory', {open: null, terminal: null}, []),
    'unknown · assignment state could not be established, so this item is not placed in a tier · assignment inventory incomplete'],
  [item('missing-agent', {open: 1, terminal: 0}, ['missing-agent']),
    "unknown · assignment state could not be established, so this item is not placed in a tier · a card holder's agent record is unavailable"],
];
const context = vm.createContext({
  agentHintSummary, itemHintSummary, workPlacementText, byId,
  esc: value => String(value ?? ''),
});
new vm.Script('"use strict";\n' + moduleSource +
  '\nglobalThis.renderHint = nodeHintContent;').runInContext(context);
for (const [work, expectedPlacement] of cases){
  const placement = classifyWorkItemPlacement(work, byId);
  const html = context.renderHint({type: 'item', w: work, unfinished: true, placement});
  const stateLine = html.indexOf('state: open');
  const placementLabel = html.indexOf('>placement</span>');
  assert.ok(stateLine >= 0, `${work.id} hint must include labeled state values`);
  assert.ok(placementLabel > stateLine, `${work.id} placement must follow the state line`);
  assert.ok(html.includes(expectedPlacement), `${work.id} hint must explain its placement`);
}
const closed = item('closed', {open: 0, terminal: 2}, []);
closed.state = 'closed';
const closedHtml = context.renderHint({type: 'item', w: closed, unfinished: false, placement: null});
assert.ok(!closedHtml.includes('>placement</span>'), 'closed items keep their current hint treatment');
'''.replace("__HINTS_URL__", hints_url)
        evaluation = evaluation.replace("__TIERS_URL__", tiers_url)
        evaluation = evaluation.replace("__NODE_HINT_SOURCE__", json.dumps(source))

        with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs") as smoke_file:
            smoke_file.write(evaluation)
            smoke_file.flush()
            result = subprocess.run(
                [shutil.which("node"), smoke_file.name],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_delayed_visuals_and_camera_hover_changes_invalidate_rendering(self):
        parser = InlineModuleParser()
        page = (ROOT / "web" / "index.html").read_text()
        parser.feed(page)
        module = "".join(parser.parts)
        self.assertNotRegex(page, r"id=[\"']renderstatus[\"']")
        render_loop_url = json.dumps((ROOT / "web" / "render-loop.mjs").as_uri())

        evaluation = r'''import assert from 'node:assert/strict';
import vm from 'node:vm';
const moduleSource = __MODULE_SOURCE__;
const {renderInterval, scheduleVisualChange} = await import(__RENDER_LOOP_URL__);
function between(startMarker, endMarker){
  const start = moduleSource.indexOf(startMarker);
  const end = moduleSource.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} should be present`);
  return moduleSource.slice(start, end);
}
const snippets = [
  between('function spawnWakes(){', '\nfunction spawnMessages(){'),
  between('function spawnMessages(){', '\n/* Short viewports'),
  between('function flyTo(pos, target, after){', '\n/* End a superseded camera transaction'),
  between('function showHover(nodes, ev, restoredScroll = null){', '\nfunction restoreHoverByKey'),
  between('function selectionChanged(){', '\nfunction refitToSelection()'),
  between('function activeRenderInterval(controlsChanged){', '\nfunction tickFrame()'),
];
const timers = [];
const added = [];
const visual = {invalidations:0, refits:0};
const scheduler = (callback, delay, invalidate) =>
  scheduleVisualChange(callback, delay, invalidate, (fn, ms) => {
    timers.push({fn, ms});
    return timers.length;
  });
class FakeMaterial { constructor(options) { Object.assign(this, options); } }
class FakeMesh { constructor(geometry, material) { this.geometry = geometry; this.material = material; this.userData = {}; } }
class FakeGeometry { setFromPoints(points) { this.points = points; return this; } }
class FakeLine { constructor(geometry, material) { this.geometry = geometry; this.material = material; this.userData = {}; } }
const context = vm.createContext({
  DATA: {wakes:[{id:'wake-1', from:'agent-a', to:'agent-b'}],
         messages:[{id:'message-1', kind:'agent', from:'agent-a', to:'agent-b'}]},
  seenWakes:new Map(), seenMsgs:new Map(), firstBatch:false, reap:() => {},
  document:{hidden:false,
    createElement:() => ({style:{}, getBoundingClientRect:() => ({height:100})}),
    createElementNS:() => ({setAttribute(){}})},
  APOS:{'agent-a':{}, 'agent-b':{}},
  THREE:{Mesh:FakeMesh, MeshBasicMaterial:FakeMaterial, BufferGeometry:FakeGeometry,
         LineBasicMaterial:FakeMaterial, Line:FakeLine},
  T:{pulse:1, pulseSub:2, pulseUser:3}, pulseGeo:{},
  scene:{add(object){added.push(object);}, remove(){}},
  msgCurve:() => ({getPoints:() => [], getPoint:() => ({})}),
  pulses:[], msgLines:[], scheduleVisualChange:scheduler,
  agentMeshes:{waiting:{userData:{flash:'wait'}}, running:{userData:{flash:'run'}}},
  itemMeshes:[{userData:{workingRing:true, waitFlash:true, children:[]}}],
  linkGroup:{children:[{userData:{state:'wait'}}, {userData:{state:'run'}}]},
  requestRender:() => {visual.invalidations++;},
  captureHoverScroll:() => new Map(),
  hoverLayer:{textContent:'', appendChild(){}},
  leaderSvg:{textContent:'', appendChild(){}},
  hoverKeys:new Set(['agent-a']), hoverLeaders:[],
  selKey:node => node.key, screenOf:() => ({x:200, y:160}),
  nodeContent:() => 'node detail', restoreHoverScroll:() => {},
  hoverCardLimits:() => ({width:400, height:300}),
  fitHoverCard:(x, y, width, height) => ({x, y, width, height}),
  aimLeaders:() => {}, canvasBounds:{left:0, top:0, width:500, height:400},
  clearTimeout:() => {}, refitTimer:null,
  view:{fit:true, selection:new Set(['agent-a'])}, applyView:() => {},
  refitToSelection:() => {visual.refits++;},
  camera:{position:{clone:() => ({camera:true})}},
  controls:{target:{clone:() => ({target:true})}}, flight:null,
  chromeOff:{x:0,y:0}, chromeAim:{x:0,y:0},
  renderInterval,
});
new vm.Script(snippets.join('\n') + `
globalThis.probes = {spawnWakes, spawnMessages, flyTo, showHover, selectionChanged,
                    activeRenderInterval};`).runInContext(context);
const probes = context.probes;
assert.equal(probes.activeRenderInterval(false), 0,
  'persistent status, hover and idle camera state must not keep drawing');

probes.spawnWakes();
probes.spawnMessages();
assert.equal(timers.length, 2);
timers[0].fn();
assert.equal(context.pulses.length, 1, 'the delayed wake should add its visible pulse');
assert.equal(visual.invalidations, 1, 'the delayed wake should request a frame');
timers[1].fn();
assert.equal(context.msgLines.length, 1, 'the delayed message should add its visible line');
assert.equal(visual.invalidations, 2, 'the delayed message should request a frame');
assert.equal(probes.activeRenderInterval(false), 1000 / 60,
  'transient wake and message visuals should animate until they settle');

probes.selectionChanged();
assert.equal(timers[2].ms, 260, 'selection refit should remain debounced');
timers[2].fn();
assert.equal(visual.refits, 1, 'the delayed selection callback should perform its refit');
assert.equal(visual.invalidations, 3, 'the delayed refit should request a frame');

context.flight = null;
const invalidationsBeforeFlight = visual.invalidations;
probes.flyTo({clone:() => ({destination:true})}, {clone:() => ({focus:true})}, () => {});
assert.equal(visual.invalidations, invalidationsBeforeFlight + 1,
  'starting a camera flight should request its first frame');
assert.equal(probes.activeRenderInterval(false), 1000 / 60,
  'a camera flight should continue without unrelated input');
context.flight = null;

const invalidationsBeforeHover = visual.invalidations;
probes.showHover([{key:'agent-a', userData:{}}]);
assert.equal(visual.invalidations, invalidationsBeforeHover + 1,
  'showing a touch hover should request a frame');
assert.ok(context.hoverKeys.has('agent-a'));
context.pulses.length = 0;
context.msgLines.length = 0;
assert.equal(probes.activeRenderInterval(false), 0,
  'persistent hover should remain visible without starting an animation loop');
'''.replace("__MODULE_SOURCE__", json.dumps(module))
        evaluation = evaluation.replace("__RENDER_LOOP_URL__", render_loop_url)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs") as smoke_file:
            smoke_file.write(evaluation)
            smoke_file.flush()
            result = subprocess.run(
                [shutil.which("node"), smoke_file.name],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
