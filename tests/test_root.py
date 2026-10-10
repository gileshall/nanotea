"""[app] root_agent: off by default, so there is no main line, and nothing of the owner's can go to one."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import CONFIG, Case, ROOT
from nanotea.config import load_config
from nanotea.server import App


class NoRoot(Case):
    PORT_OFFSET = 23

    @classmethod
    def edit_config(cls, text):
        return text.replace("root_agent = true\n", "")

    def test_no_main_line(self):
        board = json.loads(self.local("GET", "/api/board", as_="builder")[1])
        self.assertNotIn("/chat", [r["path"] for g in board["sidebar"]["groups"] for r in g["rows"]])
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn('href="/chat"', page)
        status, page = self.call("GET", "/chat")
        self.assertEqual(status, 404)
        self.assertIn("root_agent is off", page)
        status, body = self.call("POST", "/api/inbox/messages", {"text": "to nobody"})
        self.assertEqual(status, 404, body)
        self.assertIn("root_agent is off", json.loads(body)["error"])

    def test_listening_with_no_line_is_refused(self):
        r = subprocess.run([sys.executable, "-m", "nanotea.tell", "--from", "builder", "--listen"],
                           env=self.env_for("builder"), capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("root_agent is off", r.stderr)
        self.assertIn("--inbox NAME", r.stderr)

    def test_a_lineless_agents_message_takes_no_reply(self):
        msg_id = self.send("Wanderer", "no line of mine")
        status, page = self.call("GET", f"/m/{msg_id}")
        self.assertEqual(status, 200)
        self.assertIn("holds no line, so a reply here would reach nobody", page)
        status, body = self.call("POST", f"/api/messages/{msg_id}/react", {"emoji": "👍"})
        self.assertEqual(status, 409, body)
        self.assertIn("there is no main line", json.loads(body)["error"])


class InUse(unittest.TestCase):
    def test_a_main_line_in_use_stops_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "env").write_text("X=1\n")
            inbox = tmp / "data" / "inbox"
            (inbox / "messages").mkdir(parents=True)
            (inbox / "inbox.json").write_text(json.dumps({"holder": "desk", "about": None}))
            config = tmp / "config.toml"
            config.write_text(CONFIG.format(port=1, data=tmp / "data", env=tmp / "env", py=sys.executable, root=ROOT,
                                            tests=ROOT / "tests", notes=tmp / "notes.jsonl", extra="")
                              .replace("root_agent = true\n", ""))
            with self.assertRaisesRegex(Exception, r"\[app\] root_agent is off, but desk holds the main line"):
                App(load_config(config), {})


if __name__ == "__main__":
    unittest.main()
