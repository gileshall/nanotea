"""Per-harness quirks, and the working agreement's brief form: every rule the full one has, within what the
harnesses that cut a server's instructions keep."""

import unittest

from nanotea import quirks, setup, sop
from nanotea.client import Client
from nanotea.mcp_server import Agent, make, options
from nanotea.settings import DEFAULTS

EVERYTHING = {**DEFAULTS, "agent_messages": "shown", "bang": True, "tools": {k: True for k in DEFAULTS["tools"]}}


class Quirks(unittest.TestCase):
    def test_every_harness_setup_knows_has_quirks(self):
        self.assertEqual(set(setup.HARNESSES), set(quirks.QUIRKS))

    def test_a_harness_sets_the_defaults_and_flags_override_them(self):
        a = options(["--harness", "claude"])
        self.assertEqual((a.idle, a.wait_s, a.quirks.instructions_max), ("hook", 50, 2048))
        self.assertEqual(options(["--harness", "gemini"]).wait_s, 300)
        a = options(["--harness", "claude", "--idle", "wait", "--wait-s", "20"])
        self.assertEqual((a.idle, a.wait_s), ("wait", 20))
        a = options([])
        self.assertEqual((a.harness, a.idle, a.wait_s, a.quirks), (None, "finish", 50, quirks.Quirks()))

    def test_brief_max_is_the_tightest_harness(self):
        self.assertEqual(sop.BRIEF_MAX, min(q.instructions_max for q in quirks.QUIRKS.values()
                                            if q.instructions_max))


class Brief(unittest.TestCase):
    def test_it_fits_with_everything_on_and_long_names(self):
        for idle in sop.IDLE_MODES:
            for name, line in ((None, None), ("N" * 64, "L" * 64)):
                with self.subTest(idle=idle, joined=bool(name)):
                    text = sop.render("O" * 24, EVERYTHING, name, line, idle, brief=True)
                    self.assertLessEqual(len(text), sop.BRIEF_MAX)

    def test_every_line_has_a_brief_form(self):
        for f, text, short in sop.LINES:
            # Only a line that is always there may leave its rule to another line's brief form.
            if f != "idle" and not short:
                self.assertIsNone(f, text)

    def test_a_setting_drops_its_brief_line(self):
        for f, _, short in sop.LINES:
            if f in (None, "idle"):
                continue
            off = {**EVERYTHING, "tools": dict(EVERYTHING["tools"])}
            if f.startswith("tools."):
                off["tools"][f[6:]] = False
            else:
                off[f] = "off" if f == "agent_messages" else False
            with self.subTest(setting=f):
                self.assertIn(short.format(owner="Robin"), sop.render("Robin", EVERYTHING, brief=True))
                self.assertNotIn(short.format(owner="Robin"), sop.render("Robin", off, brief=True))

    def test_the_server_gives_it_and_refuses_one_too_long(self):
        cfg = {"port": 1, "app": {"owner": "Robin", "name": "Nanotea"}}
        server = make(cfg, Agent(Client(1, None), "Robin", EVERYTHING), options(["--harness", "claude"]))
        self.assertEqual(server.instructions, sop.render("Robin", EVERYTHING, idle="hook", brief=True))
        long = "R" * 200
        with self.assertRaisesRegex(ValueError, r"brief working agreement is \d+ characters, over the 2048 claude"):
            make({**cfg, "app": {**cfg["app"], "owner": long}}, Agent(Client(1, None), long, EVERYTHING),
                 options(["--harness", "claude"]))
        full = make(cfg, Agent(Client(1, None), "Robin", EVERYTHING), options(["--harness", "codex"])).instructions
        self.assertEqual(full, sop.render("Robin", EVERYTHING))
        self.assertGreater(len(full), sop.BRIEF_MAX)


if __name__ == "__main__":
    unittest.main()
