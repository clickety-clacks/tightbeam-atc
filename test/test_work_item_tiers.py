import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for viewer tier tests")
class ViewerWorkTierTest(unittest.TestCase):
    def test_placement_contract(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "test" / "work-item-tiers.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_readme_explains_all_open_work_placements(self):
        readme = " ".join((ROOT / "README.md").read_text().split())
        for phrase in (
            "cloud high above the org",
            "with a line to it",
            "forms a ring around the",
            "assignment state cannot be established",
            "open work stays in its tier",
            "Closed, iceboxed and failed",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
