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


if __name__ == "__main__":
    unittest.main()
