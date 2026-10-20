import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for viewer UI mock tests")
class ViewerUiMockTest(unittest.TestCase):
    def test_rendered_freshness_age_and_view_state_survive_delta_rebuild(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "test" / "viewer-ui-mock.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
