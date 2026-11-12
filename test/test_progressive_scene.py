import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class ProgressiveSceneTest(unittest.TestCase):
    def test_bounded_complete_loading_and_truthful_status(self):
        result = subprocess.run(
            [shutil.which("node"), "test/progressive-scene.mjs"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_scene_preserves_holder_facts_and_full_geometry(self):
        result = subprocess.run(
            [shutil.which("node"), "test/progressive-scene-build.mjs"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
