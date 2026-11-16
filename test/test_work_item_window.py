import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for viewer tests")
class WorkItemWindowTest(unittest.TestCase):
    def test_scene_membership_and_orphan_layer_visibility(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "test/work-item-scene.mjs")],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_window_and_age_contract(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "test/work-item-window.mjs")],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
