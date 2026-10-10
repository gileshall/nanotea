"""unheard_push_min: the owner hears once when a message waits on a line nothing is listening on."""

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from harness import CONFIG, ROOT
from nanotea.config import load_config
from nanotea.server import App


class Unheard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "env").write_text("X=1\n")
        self.config = tmp / "config.toml"
        self.config.write_text(CONFIG.format(port=1, data=tmp / "data", env=tmp / "env", py=sys.executable, root=ROOT,
                                             tests=ROOT / "tests", notes=tmp / "notes.jsonl", extra=""))
        self.app = App(load_config(self.config), {})
        self.app.boxes["main"].claim("Wren", None)
        self.msg = self.app.boxes["main"].add("are you there?", None, "Wren", None)
        self.at = datetime.fromisoformat(self.msg["at"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_once_a_stall_after_the_minutes(self):
        self.assertEqual(self.app.check_unheard(self.at + timedelta(minutes=9)), [])
        notes = self.app.check_unheard(self.at + timedelta(minutes=10))
        self.assertEqual([n.title for n in notes], ["Wren hasn't picked up your message"])
        self.assertIn("1 waiting since", notes[0].text)
        self.assertIn("no session open", notes[0].text)
        self.assertEqual(self.app.check_unheard(self.at + timedelta(minutes=30)), [])
        # a restart doesn't tell again
        self.assertEqual(App(load_config(self.config), {}).check_unheard(self.at + timedelta(minutes=40)), [])

    def test_not_while_something_listens_or_works_on_it(self):
        self.app.last_listen["main"] = time.monotonic()
        self.assertEqual(self.app.check_unheard(self.at + timedelta(minutes=10)), [])
        self.app.last_listen["main"] = time.monotonic() - 3600
        self.app.handed["wren"] = (time.monotonic(), datetime.now().astimezone().isoformat())
        self.assertEqual(self.app.check_unheard(self.at + timedelta(minutes=10)), [])

    def test_once_when_an_agent_stops_listening_after_a_hand_over(self):
        box = self.app.boxes["main"]
        box.mark_delivered("Wren", [self.msg["id"]], "pid 1")
        self.app.handed["wren"] = (time.monotonic() - 3600, (self.at - timedelta(hours=1)).isoformat())
        notes = self.app.check_unheard(self.at)
        self.assertEqual([n.title for n in notes], ["Wren stopped listening"])
        self.assertEqual(self.app.check_unheard(self.at + timedelta(minutes=5)), [])
        self.app.handed["wren"] = (time.monotonic() - 1, self.at.isoformat())  # busy again: nothing to tell
        self.assertEqual(self.app.check_unheard(self.at + timedelta(minutes=5)), [])

    def test_off_at_zero(self):
        self.app.settings.change({"unheard_push_min": 0})
        self.assertEqual(self.app.check_unheard(self.at + timedelta(hours=5)), [])


if __name__ == "__main__":
    unittest.main()
