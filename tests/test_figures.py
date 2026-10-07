import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Figures(unittest.TestCase):
    def test_figures_match_the_code(self):
        """Every figure's anchors hold, and every figure is shown on a page."""
        r = subprocess.run([sys.executable, str(ROOT / "docs" / "figures" / "run.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
