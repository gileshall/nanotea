"""The board's arrangement, who may change it, and reading aloud under the owner's settings."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from harness import Case
from nanotea.board import Board, BoardError, check_groups, check_hidden
from nanotea.config import ConfigError
from nanotea.settings import SettingsBook, SettingsError, check


class Rules(unittest.TestCase):
    def test_groups_are_checked(self):
        check_groups([{"name": "Crew", "members": ["lou", "Max"]}, {"name": "Lab", "members": []}])
        for bad, says in (([{"name": "Crew"}], "exactly {name, members}"),
                          ([{"name": "Other", "members": []}], "the board's own group"),
                          ([{"name": "not connected", "members": []}], "the board's own group"),
                          ([{"name": "A", "members": []}, {"name": "a", "members": []}], "two groups"),
                          ([{"name": "A", "members": ["lou"]}, {"name": "B", "members": ["Lou"]}], "in both"),
                          ([{"name": "A: b", "members": []}], "no colons"),
                          ([{"name": "A", "members": ["x, y"]}], "no commas"),
                          ([{"name": "A", "members": [" lou"]}], "surrounding spaces")):
            with self.assertRaises(BoardError, msg=bad) as cm:
                check_groups(bad)
            self.assertIn(says, str(cm.exception))
        with self.assertRaisesRegex(BoardError, "twice"):
            check_hidden(["lou", "LOU"])

    def test_bad_starting_groups_stop_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, r"\[app\] groups: two groups"):
                Board(Path(tmp), [{"name": "A", "members": []}, {"name": "A", "members": []}])
            self.assertFalse((Path(tmp) / "groups.json").exists())
            (Path(tmp) / "board-hidden.json").write_text('["a", "A"]')
            with self.assertRaisesRegex(RuntimeError, "board-hidden.json: hidden names someone twice"):
                Board(Path(tmp), [])

    def test_changes_replace_what_is_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Board(Path(tmp), [{"name": "A", "members": ["lou"]}])
            self.assertEqual(board.get(), {"groups": [{"name": "A", "members": ["lou"]}], "hidden": []})
            board.change({"hidden": ["old"]}, "test")
            self.assertEqual(board.get()["groups"], [{"name": "A", "members": ["lou"]}])
            board.change({"groups": []}, "test")
            self.assertEqual(board.get(), {"groups": [], "hidden": ["old"]})
            for bad in ({}, {"groups": [], "zap": 1}, []):
                with self.assertRaises(BoardError):
                    board.change(bad, "test")


class Settings(unittest.TestCase):
    def test_config_defaults_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = SettingsBook(Path(tmp), {"read_links": "site", "board_managers": ["Desk"]})
            self.assertEqual(book.get()["read_links"], "site")
            self.assertTrue(book.is_manager("desk"))
            self.assertFalse(book.is_manager("deskhand"))
            for bad, says in (({"read_links": "loud"}, r"\[settings\] read_links must be one of"),
                              ({"read_max_words": 30000}, r"\[settings\] read_max_words must be a whole number"),
                              ({"board_managers": "desk"}, r"\[settings\] board_managers must be a list"),
                              ({"loud": True}, r"unknown setting \[settings\] 'loud'")):
                with self.assertRaisesRegex(ConfigError, says, msg=bad):
                    SettingsBook(Path(tmp), bad)

    def test_names(self):
        check({"board_managers": ["desk", "Integrator"]})
        for bad in (["desk", "Desk"], ["a,b"], [""], [" desk"], [3]):
            with self.assertRaises(SettingsError, msg=bad):
                check({"board_managers": bad})


class Arranging(Case):
    PORT_OFFSET = 13
    EXTRA = '\n[settings]\nboard_managers = ["Desk"]\nread_links = "site"\n'

    def board(self):
        status, out = self.local("GET", "/api/board")
        self.assertEqual(status, 200, out)
        return json.loads(out)

    def where(self, agent):
        return [g["name"] for g in self.board()["sidebar"]["groups"] for r in g["rows"] if r["agent"] == agent]

    def arrange(self, name, patch):
        status, out = self.local("POST", "/api/board/arrange", {**patch, "name": name})
        return status, json.loads(out)

    def tell(self, *args, input=None):
        sender = args[args.index("--from") + 1] if "--from" in args else "tester"
        return subprocess.run([sys.executable, "-m", "nanotea.tell", *args], env=self.env_for(sender), input=input,
                              capture_output=True, text=True, timeout=60)

    def test_config_settings_reach_the_owner(self):
        now = json.loads(self.call("GET", "/api/settings")[1])["settings"]
        self.assertEqual((now["board_managers"], now["read_links"]), (["Desk"], "site"))
        self.assertEqual(self.board()["managers"], ["Desk"])

    def test_only_managers_arrange(self):
        status, out = self.arrange("intruder", {"groups": []})
        self.assertEqual(status, 403)
        self.assertIn("intruder is not a board manager", out["error"])
        self.assertIn("now: Desk", out["error"])
        self.assertEqual(self.board()["arrangement"]["groups"], [{"name": "Builders", "members": ["builder"]}])
        status, out = self.arrange("desk", {"groups": [{"name": "Other", "members": []}]})
        self.assertEqual(status, 400)
        self.assertIn("the board's own group", out["error"])
        status, out = self.local("POST", "/api/board/arrange", {"hidden": []}, as_="desk")  # the token says who
        self.assertEqual(status, 200, out)
        status, out = self.local("POST", "/api/board/arrange", {"name": "desk", "hidden": []}, as_="intruder")
        self.assertEqual(status, 403)
        self.assertIn("this token is for 'intruder', not 'desk'", json.loads(out)["error"])
        # Agents' routes and the owner's are apart: no agent arranges as the owner, no browser as an agent.
        self.assertEqual(self.local("POST", "/api/board", {"groups": []})[0], 403)
        self.assertEqual(self.call("POST", "/api/board/arrange", {"name": "Desk", "groups": []})[0], 403)

    def test_a_manager_regroups_a_connected_agent(self):
        session = {"name": "Lou", "session": "ab12", "pid": os.getpid(), "line": "lou"}
        self.assertEqual(self.local("POST", "/api/sessions", session)[0], 200)
        try:
            self.send("Lou", "Hello.", title="From Lou")
            self.assertEqual(self.where("Lou"), ["Other"])
            status, out = self.arrange("desk", {"groups": [{"name": "Crew", "members": ["lou"]}]})
            self.assertEqual(status, 200, out)
            self.assertEqual(out["groups"], [{"name": "Crew", "members": ["lou"]}])
            self.assertEqual(self.where("Lou"), ["Crew"])
            stored = json.loads((Path(self.tmp.name) / "data" / "groups.json").read_text())
            self.assertEqual(stored, out["groups"])
        finally:
            self.local("POST", "/api/sessions/ab12/close", {}, as_="Lou")
            self.arrange("desk", {"groups": [{"name": "Builders", "members": ["builder"]}]})

    def test_hidden_rows_stay_off_until_there_is_news(self):
        self.send("old-bot", "Long gone.", title="Old")
        self.assertEqual(self.where("old-bot"), ["Not connected"])
        self.call("POST", "/api/read-all", {})
        status, out = self.call("POST", "/api/board", {"hidden": ["Old-Bot"]})
        self.assertEqual(status, 200, out)
        try:
            self.assertEqual(self.where("old-bot"), [])
            self.assertIn('value="Old-Bot"', self.call("GET", "/settings")[1])
            self.send("old-bot", "Back again.", title="News")
            self.assertEqual(self.where("old-bot"), ["Not connected"])
        finally:
            self.call("POST", "/api/read-all", {})
            self.call("POST", "/api/board", {"hidden": []})

    def test_owner_arranges_from_settings(self):
        self.assertEqual(self.call("POST", "/api/board", {"hidden": []}, paired=False)[0], 401)
        status, out = self.call("POST", "/api/board", {"groups": "Crew: lou"})
        self.assertEqual(status, 400)
        self.assertIn("groups must be a list", json.loads(out)["error"])
        page = self.call("GET", "/settings")[1]
        for bit in ("<h2>Reading aloud</h2>", "<h2>Board</h2>", "Builders: builder</textarea>",
                    'name="board_managers" data-kind="names" value="Desk"', '<option value="site" selected>'):
            self.assertIn(bit, page)

    def test_tell_arranges(self):
        r = self.tell("--from", "intruder", "--arrange", '{"hidden": []}')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a board manager", r.stderr)
        r = self.tell("--from", "desk", "--arrange", "-", input='{"hidden": ["ghost"]}')
        self.assertEqual(r.returncode, 0, r.stderr)
        try:
            self.assertEqual(json.loads(r.stdout)["hidden"], ["ghost"])
            r = self.tell("--from", "anyone", "--arrange")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["hidden"], ["ghost"])
            self.assertIn("goes alone", self.tell("--arrange", "--inbox", "x").stderr)
        finally:
            self.call("POST", "/api/board", {"hidden": []})

    def test_the_arrange_tool_is_for_managers(self):
        def server(name):
            return StdioServerParameters(command=sys.executable, args=["-m", "nanotea", "mcp", "--name", name],
                                         env=self.env_for(name), cwd=self.tmp.name)

        async def run():
            async with Client(server("intruder"), mode="legacy") as c:
                self.assertNotIn("arrange", {t.name for t in (await c.list_tools()).tools})
            async with Client(server("desk"), mode="legacy") as c:
                self.assertIn("arrange", {t.name for t in (await c.list_tools()).tools})
                r = await c.call_tool("arrange", {})
                self.assertFalse(r.is_error, r.content)
                self.assertEqual(r.structured_content["hidden"], [])
                r = await c.call_tool("arrange", {"hidden": ["ghost"]})
                self.assertFalse(r.is_error, r.content)
                self.assertEqual(r.structured_content["hidden"], ["ghost"])
                r = await c.call_tool("arrange", {"groups": [{"name": "Other", "members": []}]})
                self.assertTrue(r.is_error)
        try:
            asyncio.run(run())
        finally:
            self.call("POST", "/api/board", {"hidden": []})

    def test_reading_aloud_follows_the_settings(self):
        msg_id = self.send("reader", "See https://example.com/a/b for more.", title="Link")
        folder = Path(self.tmp.name) / "data" / msg_id
        status, _ = self.call("GET", f"/audio/{msg_id}")
        self.assertEqual(status, 200)
        site = [p for p in folder.iterdir() if p.name.startswith("audio-")]
        self.assertEqual(len(site), 1, list(folder.iterdir()))
        self.assertIn(b"See a link to example.com for more.", site[0].read_bytes())
        try:
            self.assertEqual(self.call("POST", "/api/settings", {"read_links": "read"})[0], 200)
            self.call("GET", f"/audio/{msg_id}")
            self.assertIn(b"https://example.com/a/b", (folder / "audio.m4a").read_bytes())
        finally:
            self.call("POST", "/api/settings", {"read_links": "site"})
        before = site[0].stat().st_mtime_ns
        self.call("GET", f"/audio/{msg_id}")
        self.assertEqual(site[0].stat().st_mtime_ns, before)  # kept, not voiced again
