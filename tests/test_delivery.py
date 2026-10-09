"""Delivery modes ([delivery]): pull, the push hook, and nanotea listen; what the service, the MCP server and setup
say of each."""

import asyncio
import json
import os
import re
import subprocess
import sys
import unittest

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from harness import Case
from nanotea import delivery, sop
from nanotea.client import Client as Service
from nanotea.config import ConfigError
from nanotea.mcp_server import Agent, make, options
from nanotea.settings import DEFAULTS

PUSH = {"hook_event_name": "PostToolUse", "tool_name": "Bash"}
SESSIONS: dict[str, str] = {}


class Settings(unittest.TestCase):
    def test_the_table_is_checked(self):
        self.assertEqual(delivery.settings({}), {"mode": "pull", "agents": {}})
        table = delivery.settings({"delivery": {"mode": "push", "agents": {"desk": "listen"}}})
        self.assertEqual((delivery.mode_for(table, "desk"), delivery.mode_for(table, "other"),
                          delivery.mode_for(table, None)), ("listen", "push", "push"))
        for bad, says in (({"delivery": []}, r"\[delivery\] must be a table"),
                          ({"delivery": {"mood": "pull"}}, r"\[delivery\] mood: unknown; known: mode, agents"),
                          ({"delivery": {"mode": "poll"}}, "mode must be one of pull, push, listen, not 'poll'"),
                          ({"delivery": {"agents": ["desk"]}}, "agents must be a table"),
                          ({"delivery": {"agents": {"desk": "shout"}}}, "'desk' has mode 'shout'")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ConfigError, says):
                delivery.settings(bad)

    def test_a_server_refuses_a_mode_its_harness_cant_do(self):
        cfg = {"port": 1, "app": {"owner": "Robin", "name": "Nanotea"},
               "delivery": {"agents": {"Pusher": "push", "Lister": "listen"}}}

        def server(name, args):
            agent = Agent(Service(1, None), "Robin", {**DEFAULTS})
            agent.name = name
            return make(cfg, agent, options(["--name", name, *args]))

        for name, harness in (("Pusher", "codex"), ("Lister", "gemini")):
            with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, rf"gives {name} mode \w+, which {harness} can't do.*agents.{name} = \"pull\""):
                server(name, ["--harness", harness])
        # claude can; with no harness named, nothing says it can't.
        self.assertIn("A hook adds Robin's messages", server("Pusher", ["--harness", "claude"]).instructions)
        self.assertIn("a hook adds them to what you see", server("Pusher", []).instructions)


class Modes(Case):
    PORT_OFFSET = 22
    EXTRA = '\n[delivery]\nagents = { Pusher = "push", Lister = "listen", Odd = "push" }\n'

    def run_nanotea(self, *args, stdin="{}"):
        return subprocess.run([sys.executable, "-m", "nanotea", *args], cwd=self.tmp.name, env=self.env,
                              input=stdin, capture_output=True, text=True, timeout=60)

    def open(self, name, channels=()):
        """name's line, and its one session (one per agent is on), with channels."""
        line = name.lower()
        self.assertEqual(self.local("POST", f"/api/dm-{line}", {"name": name, "open": True})[0], 200)
        sid = SESSIONS.setdefault(name, os.urandom(4).hex())
        status, out = self.local("POST", "/api/sessions", {"name": name, "session": sid,
                                                           "pid": os.getpid(), "line": line,
                                                           "channels": list(channels)})
        self.assertEqual(status, 200, out)

    def waiting(self, name):
        return json.loads(self.local("GET", f"/api/agents/{name}/waiting?line=dm-{name.lower()}")[1])

    def owner_says(self, name, text):
        self.assertEqual(self.call("POST", f"/api/dm-{name.lower()}/messages", {"text": text})[0], 200)

    def test_waiting_says_the_mode_and_channels(self):
        self.open("Plain")
        self.open("Pusher", ["general"])
        self.assertEqual((self.waiting("Plain")["delivery"], self.waiting("Plain")["channels"]), ("pull", []))
        self.assertEqual((self.waiting("Pusher")["delivery"], self.waiting("Pusher")["channels"]),
                         ("push", ["general"]))
        status, out = self.local("POST", "/api/sessions", {"name": "Plain", "session": "abcd", "pid": os.getpid(),
                                                           "line": "plain", "channels": "general"})
        self.assertEqual((status, json.loads(out)["error"]), (400, "ValueError: 'channels' must be a list of channel names"))
        view = json.loads(self.owner("GET", "/api/settings")[1])
        self.assertEqual(view["delivery"]["agents"]["Lister"], "listen")

    def test_push_hands_over_after_a_tool_call_only_in_push_mode(self):
        self.open("Plain")
        self.owner_says("Plain", "for plain")
        r = self.run_nanotea("hook", "push", "--name", "Plain", stdin=json.dumps(PUSH))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.waiting("Plain")["total"], 1)

        self.open("Pusher", ["general"])
        self.local("GET", "/api/channels/general/pending?name=Pusher")  # its cursor starts here
        r = self.run_nanotea("hook", "push", "--name", "Pusher", stdin=json.dumps(PUSH))
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.send("poster", "a post in general", channel="general")
        self.owner_says("Pusher", "stop the build")
        r = self.run_nanotea("hook", "push", "--name", "Pusher",
                             stdin=json.dumps({**PUSH, "hook_event_name": "PostToolUseFailure"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUseFailure")
        text = out["additionalContext"]
        self.assertTrue(text.startswith("Nanotea push: 2 item(s) for you"), text)
        self.assertIn("Robin sees none of your terminal", text)
        items = json.loads(text.split("\n", 1)[1])
        self.assertEqual([(it["kind"], it["text"]) for it in items],
                         [("post", "a post in general"), ("message", "stop the build")])
        self.assertEqual(self.waiting("Pusher")["total"], 0)  # delivered once
        r = self.run_nanotea("hook", "push", "--name", "Pusher", stdin=json.dumps(PUSH))
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = self.run_nanotea("hook", "push", "--name", "Pusher", stdin="{}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has no hook_event_name", r.stderr)

    def test_listen_exits_with_what_came(self):
        self.open("Lister")
        proc = subprocess.Popen([sys.executable, "-m", "nanotea", "listen", "--name", "Lister"], cwd=self.tmp.name,
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            board = lambda: json.loads(self.local("GET", "/api/board")[1])["sidebar"]["groups"]  # noqa: E731
            self.wait_for(lambda: any(r["agent"] == "Lister" and r["listening"] for g in board() for r in g["rows"]),
                          "Lister to show as listening")
            self.assertIsNone(proc.poll())
            self.owner_says("Lister", "ship it")
            out, err = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, err)
        head, rest = out.split("\n", 1)
        self.assertTrue(head.startswith("Nanotea listen: 1 item(s) for you"), head)
        items, again = rest.rsplit("\n", 2)[0], rest.rsplit("\n", 2)[1]
        self.assertEqual([it["text"] for it in json.loads(items)], ["ship it"])
        self.assertRegex(again, r"^Start nanotea listen again now, in the background: NANOTEA_CONFIG=\S+ "
                                r"\S+ -m nanotea listen --name Lister$")
        self.assertEqual(self.waiting("Lister")["total"], 0)
        self.open("Plain")
        r = self.run_nanotea("listen", "--name", "Plain")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Plain's delivery mode is pull, not listen", r.stderr)

    def mcp(self, *args, as_):
        return StdioServerParameters(command=sys.executable, args=["-m", "nanotea", "mcp", *args],
                                     env=self.env_for(as_), cwd=self.tmp.name)

    async def tool(self, client, tool_name, **args):
        r = await client.call_tool(tool_name, args)
        self.assertFalse(r.is_error, r.content)
        return r.structured_content

    def test_the_mcp_server_tells_the_agent_its_mode(self):
        for name, sid in list(SESSIONS.items()):  # the servers below open their own
            self.assertEqual(self.local("POST", f"/api/sessions/{sid}/close", {}, as_=name)[0], 200)
            del SESSIONS[name]

        async def session():
            async with Client(self.mcp("--harness", "claude", as_="Lister"), mode="legacy") as c:
                self.assertIn(sop.delivery_line("Robin", "pull", brief=True), c.instructions)  # before join
                out = await self.tool(c, "join", name="Lister", voice=self.agent("Lister"))
                self.assertEqual(out["delivery"]["mode"], "listen")
                self.assertEqual(out["delivery"]["text"], "Your delivery mode is listen: "
                                 + sop.delivery_line("Robin", "listen", brief=True))
                self.assertRegex(out["delivery"]["listen"], r"NANOTEA_TOKEN_FILE=\S+ \S+ -m nanotea listen --name "
                                                            r"Lister$")
                self.assertNotIn("delivery", await self.tool(c, "check"))  # told once
            async with Client(self.mcp("--name", "Lister", "--harness", "claude", as_="Lister"), mode="legacy") as c:
                self.assertIn(sop.delivery_line("Robin", "listen", brief=True), c.instructions)
                self.assertIn("nanotea listen --name Lister", (await self.tool(c, "check"))["delivery"]["listen"])
            async with Client(self.mcp("--name", "Pusher", "--harness", "claude", as_="Pusher"), mode="legacy") as c:
                self.assertIn(sop.delivery_line("Robin", "push", brief=True), c.instructions)
                self.assertNotIn("delivery", await self.tool(c, "check"))
            # The service's word, not the agreement's, when it says otherwise: here a harness that can't push.
            async with Client(self.mcp("--harness", "codex", as_="Odd"), mode="legacy") as c:
                out = await self.tool(c, "join", name="Odd", voice=self.agent("Odd"))
                self.assertEqual(out["delivery"]["mode"], "push")
                self.assertIn("gives Odd mode push, which codex can't do", out["delivery"]["text"])
                self.assertIn("Take them with check and wait", out["delivery"]["text"])
        asyncio.run(session())

    def test_setup_runs_push_after_every_tool_and_sop_follows_the_mode(self):
        r = self.run_nanotea("setup", "claude", "--name", "Pusher")
        self.assertEqual(r.returncode, 0, r.stderr)
        hooks = [json.loads(b) for b in re.findall(r"^\{\n.*?^\}$", r.stdout, re.S | re.M)]
        after = next(h["hooks"] for h in hooks if "PostToolUse" in h.get("hooks", {}))
        for event in ("PostToolUse", "PostToolUseFailure"):
            hook = after[event][0]["hooks"][0]
            self.assertIn("hook push --name Pusher", hook["command"])
            self.assertNotIn("async", hook)
        r = self.run_nanotea("sop", "--name", "Lister", "--harness", "claude")
        self.assertIn("Run nanotea listen in the background", r.stdout)
        r = self.run_nanotea("sop", "--name", "Plain")
        self.assertIn("reach you only through the check and wait tools", r.stdout)


if __name__ == "__main__":
    unittest.main()
