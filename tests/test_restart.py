"""What presence keeps over a restart: an agent held at a prompt, and one whose listener handed it items."""

import sys
import tempfile
import unittest
from pathlib import Path

from harness import CONFIG, ROOT
from nanotea.config import load_config
from nanotea.server import App


class Kept(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "env").write_text("X=1\n")
        self.config = tmp / "config.toml"
        self.config.write_text(CONFIG.format(port=1, data=tmp / "data", env=tmp / "env", py=sys.executable, root=ROOT,
                                             tests=ROOT / "tests", notes=tmp / "notes.jsonl", extra=""))

    def tearDown(self):
        self.tmp.cleanup()

    def test_held_and_handed_outlast_a_restart(self):
        app = App(load_config(self.config), {})
        app.boxes["main"].claim("Wren", None)
        app.handed_over("wren")
        app.hold("Kim", "Edit: a.py")
        self.assertEqual(app.presence("main")[0], "busy")

        again = App(load_config(self.config), {})
        self.assertEqual(again.presence("main")[0], "busy")
        self.assertEqual(again.held_of("kim")["what"], "Edit: a.py")  # names match whatever their case
        again.active("KIM")
        self.assertIsNone(App(load_config(self.config), {}).held_of("Kim"))


if __name__ == "__main__":
    unittest.main()
