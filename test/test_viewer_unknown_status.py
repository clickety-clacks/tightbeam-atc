import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for viewer unknown-status tests")
class ViewerUnknownStatusTest(unittest.TestCase):
    def test_unknown_marker_is_specific_accessible_and_preserves_known_rings(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "test" / "viewer-unknown-status.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
