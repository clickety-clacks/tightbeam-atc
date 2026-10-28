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


if __name__ == "__main__":
    unittest.main()
