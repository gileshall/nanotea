"""Delivery modes ([delivery]): poll, the push hook, and nanotea listen; what the service, the MCP server and setup
say of each."""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import unittest

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from harness import Case
from nanotea import delivery, sop
from nanotea.mailbox import Mailbox, claim
from nanotea.client import Client as Service
from nanotea.config import ConfigError
from nanotea.mcp_server import Agent, make, options
from nanotea.settings import DEFAULTS

PUSH = {"hook_event_name": "PostToolUse", "tool_name": "Bash"}
SESSIONS: dict[str, str] = {}


class Settings(unittest.TestCase):
    def test_the_table_is_checked(self):
        self.assertEqual(delivery.settings({}), {"mode": "poll", "agents": {}})
        table = delivery.settings({"delivery": {"mode": "push", "agents": {"desk": "listen"}}})
        self.assertEqual((delivery.mode_for(table, "desk"), delivery.mode_for(table, "other"),
                          delivery.mode_for(table, None)), ("listen", "push", "push"))
        self.assertEqual(delivery.mode_for(table, "DESK"), "listen")  # names match whatever their case
        for bad, says in (({"delivery": []}, r"\[delivery\] must be a table"),
                          ({"delivery": {"agents": {"desk": "push", "Desk": "poll"}}},
                           "'desk' and 'Desk' are the same agent"),
                          ({"delivery": {"mood": "poll"}}, r"\[delivery\] mood: unknown; known: mode, agents"),
                          ({"delivery": {"mode": "pull"}}, "mode must be one of poll, push, listen, not 'pull'"),
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
                    ValueError, rf"gives {name} mode \w+, which {harness} can't do.*agents.{name} = \"poll\""):
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
        self.assertEqual((self.waiting("Plain")["delivery"], self.waiting("Plain")["channels"]), ("poll", []))
        self.assertEqual((self.waiting("Pusher")["delivery"], self.waiting("Pusher")["channels"]),
                         ("push", ["general"]))
        status, out = self.local("POST", "/api/sessions", {"name": "Plain", "session": "abcd", "pid": os.getpid(),
                                                           "line": "plain", "channels": "general"})
        self.assertEqual((status, json.loads(out)["error"]), (400, "ValueError: 'channels' must be a list of channel names"))
        view = json.loads(self.owner("GET", "/api/settings")[1])
        self.assertEqual(view["delivery"]["agents"]["Lister"], "listen")

    def test_channel_items_count_as_they_would_end_a_wait(self):
        self.open("Heeder", ["general"])
        self.local("GET", "/api/channels/general/pending?name=Heeder")  # its cursor starts here
        chatter = self.send("poster", "routine chatter", channel="general")
        w = self.waiting("Heeder")
        self.assertEqual((w["channels_waiting"], w["total"]), (0, 0))
        status, out = self.call("POST", f"/api/messages/{chatter}/react", {"emoji": "👍"})
        self.assertEqual(status, 200, out)
        self.wait_for(lambda: len(self.app_pending("Heeder")) == 2, "the reaction to reach the channel")
        self.assertEqual(self.waiting("Heeder")["channels_waiting"], 0)  # a reaction to poster is for poster
        mention = self.send("poster", "@heeder can you look?", channel="general")
        w = self.waiting("Heeder")
        self.assertEqual((w["channels_waiting"], w["total"], w["ids"]), (1, 1, [mention]))
        try:
            self.assertEqual(self.call("POST", "/api/settings", {"wake_filter": False})[0], 200)
            self.assertEqual(self.waiting("Heeder")["channels_waiting"], 2)  # still not the reaction
        finally:
            self.assertEqual(self.call("POST", "/api/settings", {"wake_filter": True})[0], 200)

    def app_pending(self, name):
        return json.loads(self.local("GET", f"/api/channels/general/pending?name={name}", as_=name)[1])

    def test_tell_listen_wakes_for_a_mention_not_chatter(self):
        self.local("GET", "/api/channels/general/pending?name=Teller")  # its cursor starts here
        proc = subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", "Teller", "--listen", "--channel",
                                 "general"], cwd=self.tmp.name, env=self.env_for("Teller"), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            self.send("poster", "routine chatter for the room", channel="general")
            time.sleep(7)  # two polls of the listener's
            self.assertIsNone(proc.poll(), proc.stderr.read() if proc.poll() is not None else "")
            self.send("poster", "@Teller the build is red", channel="general")
            out, err = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("routine chatter for the room", out)  # it comes with what woke it
        self.assertIn("@Teller the build is red", out)

    def test_two_takers_never_hand_over_the_same_item(self):
        self.open("Twin", ["general"])
        self.local("GET", "/api/channels/general/pending?name=Twin")  # its cursor starts here
        self.owner_says("Twin", "on the line")
        post = self.send("poster", "@Twin in the channel", channel="general")
        asked = self.send("Twin", "which one?", ask=True)
        self.assertEqual(self.call("POST", f"/api/messages/{asked}/reply", {"text": "the left"})[0], 200)
        self.assertEqual(self.call("POST", "/api/settings", {"agent_messages": "shown"})[0], 200)
        try:
            self.assertEqual(self.local("POST", "/api/talk", {"from": "poster", "to": "Twin", "text": "psst"},
                                        as_="poster")[0], 200)
        finally:
            self.assertEqual(self.call("POST", "/api/settings", {"agent_messages": DEFAULTS["agent_messages"]})[0],
                             200)
        client = Service(self.port, self.token("Twin"))
        one, two = (Mailbox(client, "Twin", "twin", ["general"], f"pid {n}") for n in (1, 2))
        first, second = one.gather(False), two.gather(False)  # both saw everything before either claimed
        self.assertEqual([len(got) for _, _, got in first], [1, 1, 1, 1])
        items = claim(client, first, False)
        self.assertEqual({it["kind"] for it in items}, {"message", "post", "answer", "agent"})
        self.assertIn(post, [it["id"] for it in items])
        self.assertEqual(claim(client, second, False), [])
        # The same taker asking again, its first answer lost, is given them again.
        self.assertEqual(claim(client, first, False), items)

    def test_a_listener_prints_only_what_it_was_given(self):
        self.local("GET", "/api/channels/general/pending?name=Pair")  # its cursor starts here
        self.send("poster", "@Pair only once", channel="general")
        # Another taker claims the post between this listener's look and its claim.
        script = """if True:
            import sys
            from nanotea import tell
            from nanotea.credentials import agent_token
            tell.TOKEN = agent_token("Pair")
            look = tell.request

            def racing(url, body=None, **kw):
                out = look(url, body, **kw)
                if body is None and "/pending" in url and out:
                    look(url.split("/pending")[0] + "/delivered",
                         {"name": "Pair", "ids": [m["id"] for m in out], "listener": "the other"})
                return out

            tell.request = racing
            tell._listen(sys.argv[1], "Pair", None, None, ["general"], False, once=True)
        """
        r = subprocess.run([sys.executable, "-c", script, f"http://127.0.0.1:{self.port}"], cwd=self.tmp.name,
                           env=self.env_for("Pair"), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("only once", r.stdout)
        self.assertIn("another taker for Pair was given them first", r.stderr)

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
        self.assertIn("Plain's delivery mode is poll, not listen", r.stderr)

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
                self.assertIn(sop.delivery_line("Robin", "poll", brief=True), c.instructions)  # before join
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
