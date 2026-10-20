import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for viewer semantic tests")
class ViewerSemanticsTest(unittest.TestCase):
    def test_hover_inspection_semantics(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "test" / "viewer-semantics.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
