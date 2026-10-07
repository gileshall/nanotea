"""Threads: a message and every reply to it, from agents and the owner, wherever each was sent from."""

import asyncio
import json
import re
import subprocess
import sys
import time

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from harness import Case


class Threads(Case):
    PORT_OFFSET = 7

    def reply(self, sender, text, re_id, channel=None, title=None):
        """An agent replies; returns the reply's meta once it is delivered."""
        body = {"text": text, "from": sender, "voice": self.agent(sender), "re": re_id,
                **({"channel": channel} if channel else {}), **({"title": title} if title else {})}
        status, out = self.local("POST", "/api/messages", body)
        self.assertEqual(status, 202, out)
        msg_id = json.loads(out)["id"]
        return self.wait_for(lambda: (m := json.loads(self.local("GET", f"/api/messages/{msg_id}")[1]))["delivered"]
                             and m, f"{msg_id} to be delivered")

    def owner_says(self, line, holder, text, re_id=None):
        """The owner writes on a line; returns the message's id, as its holder picks it up."""
        status, out = self.call("POST", f"/api/dm-{line}/messages", {"text": text, **({"re": re_id} if re_id else {})})
        self.assertEqual(status, 200, out)
        pending = json.loads(self.local("GET", f"/api/dm-{line}/pending?name={holder}")[1])
        return [m for m in pending if m["text"] == text][0]

    def thread_api(self, agent, ref):
        status, out = self.local("GET", f"/api/agents/{agent}/thread/{ref}")
        return status, json.loads(out)

    # Message times are to the second: a test waits a second where the order in a thread matters.

    def test_an_agent_replies_to_its_own_message(self):
        self.assertEqual(self.local("POST", "/api/dm-ana", {"name": "ana", "open": True})[0], 200)
        root = self.send("ana", "The build is red on main.", title="Red build")
        time.sleep(1.1)
        reply = self.reply("ana", "Fixed: a stale fixture.", root, title="Build fixed")
        self.assertEqual(reply["re"], {"id": root, "kind": "agent", "sender": "ana", "title": "Red build",
                                       "thread": f"m-{root}"})
        status, page = self.call("GET", f"/t/m-{root}")
        self.assertEqual(status, 200)
        self.assertIn("Red build", page)
        self.assertIn("Build fixed", page)
        self.assertIn(f'id="sep-m-{root}">1 reply</div>', page)
        self.assertIn(f'data-thread="{root}"', page)
        self.assertIn("Reply in this thread", page)
        # In the conversation, the first message counts its replies and a reply quotes what it answers.
        chat = self.call("GET", "/chat/ana")[1]
        self.assertIn(f'<a class="replies" href="/t/m-{root}">', chat)
        self.assertIn("<b>1 reply</b>", chat)
        self.assertIn(f'href="/t/m-{root}#m-{root}">Replying to', chat)
        # A second reply changes the thread's version and its count.
        _, v1 = self.call("GET", f"/api/chat/version?thread=m-{root}")
        time.sleep(1.1)
        self.reply("ana", "And CI is green again.", reply["id"], title="Green")
        _, v2 = self.call("GET", f"/api/chat/version?thread=m-{root}")
        self.assertNotEqual(json.loads(v1)["version"], json.loads(v2)["version"])
        self.assertIn("<b>2 replies</b>", self.call("GET", "/chat/ana")[1])
        status, out = self.thread_api("ana", root)
        self.assertEqual(status, 200, out)
        self.assertEqual([m["title"] for m in out["messages"]], ["Red build", "Build fixed", "Green"])
        self.assertEqual(out["messages"][2]["re"], reply["id"])
        self.assertEqual((out["thread"], out["channel"], out["first_deleted"]), (f"m-{root}", None, False))
        self.assertEqual(out["url"], f"https://nanotea.test/t/m-{root}")
        export = self.call("GET", "/export.md?from=ana")[1]
        self.assertIn(f'Replying to ana: "Red build" (id {root}, thread m-{root}).', export)

    def test_a_reply_goes_where_its_thread_is(self):
        self.assertEqual(self.call("POST", "/api/channels", {"name": "ops"})[0], 200)
        root = self.send("bea", "Deploying at noon.", channel="general", title="Noon deploy")
        time.sleep(1.1)
        reply = self.reply("cal", "I'll hold my merge.", root)
        self.assertEqual(reply["channel"], "general")
        status, out = self.local("POST", "/api/messages", {"text": "x", "from": "cal", "voice": self.agent("cal"),
                                                           "re": root, "channel": "ops"})
        self.assertEqual(status, 400, out)
        self.assertIn("a reply goes where its thread is", out)
        # Any agent reads a channel's thread.
        status, out = self.thread_api("dee", f"m-{root}")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["channel"], "general")
        self.assertEqual([m["from"] for m in out["messages"]], ["bea", "cal"])
        # The owner replies in the channel; a reply to it is in the same thread.
        self.local("GET", "/api/channels/general/pending?name=dee")  # dee listens from here on
        time.sleep(1.1)
        status, out = self.call("POST", "/api/channels/general/messages", {"text": "thanks both", "re": reply["id"]})
        self.assertEqual(status, 200, out)
        posts = json.loads(self.local("GET", "/api/channels/general/pending?name=dee")[1])
        mine = [p for p in posts if p.get("text") == "thanks both"][0]
        self.assertEqual(mine["re"]["thread"], f"m-{root}")
        self.assertEqual(mine["re_url"], f"https://nanotea.test/t/m-{root}#m-{reply['id']}")
        time.sleep(1.1)
        self.reply("bea", "Done.", mine["id"], title="Deployed")
        out = self.thread_api("dee", root)[1]
        self.assertEqual([m["from"] for m in out["messages"]], ["bea", "cal", "owner", "bea"])
        # Not from a line's composer: it is in #general.
        self.local("POST", "/api/dm-bea", {"name": "bea", "open": True})
        status, out = self.call("POST", "/api/dm-bea/messages", {"text": "x", "re": root})
        self.assertEqual(status, 400, out)
        self.assertIn("is in #general; reply there", out)
        self.assertIn("Reply in this thread, in #general", self.call("GET", f"/t/m-{root}")[1])

    def test_a_question_in_a_channel_is_answered_before_replies(self):
        ask = self.send("eve", "Freeze the branch?", ask=True, channel="general", title="Freeze?")
        page = self.call("GET", f"/t/m-{ask}")[1]
        self.assertIn('class="answer-first"', page)
        status, out = self.call("POST", "/api/channels/general/messages", {"text": "x", "re": ask})
        self.assertEqual(status, 400, out)
        self.assertIn("answer it on /m/", out)
        self.assertEqual(self.call("POST", f"/api/messages/{ask}/reply", {"text": "yes"})[0], 200)
        page = self.call("GET", f"/t/m-{ask}")[1]
        self.assertNotIn('class="answer-first"', page)
        self.assertIn(f'data-thread="{ask}"', page)
        out = self.thread_api("fay", ask)[1]
        self.assertEqual([(m["kind"], m["from"]) for m in out["messages"]], [("question", "eve"), ("answer", "owner")])

    def test_a_thread_the_owner_starts(self):
        self.assertEqual(self.local("POST", "/api/dm-gus", {"name": "Gus", "open": True})[0], 200)
        first = self.owner_says("gus", "Gus", "Can you look at the flaky upload test?")
        self.assertIsNone(first["re"])
        time.sleep(1.1)
        reply = self.reply("Gus", "It races the transcriber; fixing.", first["id"], title="Flaky upload")
        key = f"g-{first['id']}"
        self.assertEqual(reply["re"]["thread"], key)
        self.assertEqual(reply["re"]["kind"], "owner")
        self.assertEqual(reply["re"]["title"], "Can you look at the flaky upload test?")
        time.sleep(1.1)
        again = self.owner_says("gus", "Gus", "Thanks, and the export one?", reply["id"])
        self.assertEqual(again["re"]["thread"], key)
        self.assertEqual(again["re_url"], f"https://nanotea.test/t/{key}#m-{reply['id']}")
        status, page = self.call("GET", f"/t/{key}")
        self.assertEqual(status, 200)
        self.assertIn(f'id="sep-{key}">2 replies</div>', page)
        self.assertIn("Reply in this thread (goes to Gus)", page)
        self.assertIn(f'<a class="replies" href="/t/{key}">', self.call("GET", "/chat/gus")[1])
        status, out = self.thread_api("Gus", key)
        self.assertEqual(status, 200, out)
        self.assertEqual([m["from"] for m in out["messages"]], ["owner", "Gus", "owner"])
        # A direct line's thread is its agents' alone.
        status, out = self.thread_api("Hal", key)
        self.assertEqual(status, 403, out)
        self.assertIn("another agent's line", out["error"])
        # Through the proxy, even paired, agents' routes are refused.
        self.assertEqual(self.call("GET", f"/api/agents/Gus/thread/{key}")[0], 403)
        status, page = self.call("GET", f"/api/thread?thread={key}&before=0~0~20000101-000000-0000")
        self.assertEqual(status, 200, page)

    def test_what_is_not_a_thread(self):
        missing = "20990101-000000-abcd"
        self.assertEqual(self.call("GET", f"/t/m-{missing}")[0], 404)
        self.assertEqual(self.call("GET", f"/t/g-{missing}")[0], 404)
        self.assertEqual(self.call("GET", "/api/chat/version?thread=x-1")[0], 400)
        self.assertEqual(self.thread_api("ivy", missing)[0], 400)
        status, out = self.local("POST", "/api/messages", {"text": "x", "from": "ivy", "voice": self.agent("ivy"),
                                                           "re": missing})
        self.assertEqual(status, 400, out)
        self.assertIn("no message", out)

    def test_tell_and_mcp_reply_in_a_thread(self):
        root = self.send("jo", "Release notes drafted.", title="Release notes")
        time.sleep(1.1)
        self.token("kit")
        r = subprocess.run([sys.executable, "-m", "nanotea.tell", "--from", "kit", "--voice", self.agent("kit"),
                            "--title", "Notes reviewed", "--re", root, "Two typos, otherwise good."],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_for(lambda: self.thread_api("jo", root)[1]["messages"][1:], "kit's reply")
        server = StdioServerParameters(command=sys.executable, args=["-m", "nanotea", "mcp"],
                                       env=self.env_for("jo"), cwd=self.tmp.name)

        async def session():
            async with Client(server, mode="legacy") as c:
                self.assertIn("thread", {t.name for t in (await c.list_tools()).tools})
                r = await c.call_tool("join", {"name": "jo", "voice": self.agent("jo")})
                self.assertFalse(r.is_error, r.content)
                r = await c.call_tool("send", {"text": "Fixed the typos.", "re": root})
                self.assertFalse(r.is_error, r.content)
                r = await c.call_tool("thread", {"id": root})
                self.assertFalse(r.is_error, r.content)
                return r.structured_content
        time.sleep(1.1)
        out = asyncio.run(session())
        self.assertEqual([m["from"] for m in out["messages"]], ["jo", "kit", "jo"])
        self.assertEqual(out["messages"][1]["title"], "Notes reviewed")


class ThreadViews(Case):
    """How a conversation draws threads: linear or grouped, what the composer says, and what a message's menu holds."""
    PORT_OFFSET = 16

    def reply(self, sender, text, re_id, title):
        body = {"text": text, "from": sender, "voice": self.agent(sender), "re": re_id, "title": title}
        status, out = self.local("POST", "/api/messages", body)
        self.assertEqual(status, 202, out)
        msg_id = json.loads(out)["id"]
        return self.wait_for(lambda: (m := json.loads(self.local("GET", f"/api/messages/{msg_id}")[1]))["delivered"]
                             and m, f"{msg_id} to be delivered")["id"]

    def owner_says(self, text, re_id=None):
        status, out = self.call("POST", "/api/dm-pam/messages", {"text": text, **({"re": re_id} if re_id else {})})
        self.assertEqual(status, 200, out)
        pending = json.loads(self.local("GET", "/api/dm-pam/pending?name=Pam")[1])
        return [m for m in pending if m["text"] == text][0]["id"]

    def set_view(self, view):
        status, out = self.call("POST", "/api/settings", {"thread_view": view})
        self.assertEqual(status, 200, out)
        self.assertEqual(json.loads(out)["settings"]["thread_view"], view)

    def test_linear_and_grouped(self):
        self.assertEqual(self.local("POST", "/api/dm-pam", {"name": "Pam", "open": True})[0], 200)
        root = self.send("Pam", "Plan: split the upload path.", title="Plan")
        time.sleep(1.1)
        first = self.reply("Pam", "Part one is in.", root, "Part one")
        time.sleep(1.1)
        mine = self.owner_says("Good, go on.", first)
        time.sleep(1.1)
        later = self.owner_says("Unrelated: how is the build?")
        key = f"m-{root}"

        # The default is the linear view: every message where it was sent, replies quoting what they answer.
        view = json.loads(self.call("GET", "/api/settings")[1])
        self.assertEqual(view["settings"]["thread_view"], "linear")
        chat = self.call("GET", "/chat/pam")[1]
        for row in (f'id="m-{root}"', f'id="m-{first}"', f'id="g-{mine}"', f'id="g-{later}"'):
            self.assertIn(row, chat)
        self.assertIn(f'<a href="/t/{key}#m-{root}">Replying to', chat)
        # An answer to a reply says it is in the thread.
        self.assertIn(f'<a class="in-thread" href="/t/{key}">in thread</a>', chat)
        self.assertIn("<b>2 replies</b>", chat)
        self.assertNotIn("Reply in thread</button>", chat)

        # The composer's box says what a send will be there; Reply quotes a message and Cancel goes back to a new
        # thread.
        self.assertIn('data-to="to Pam"', chat)
        self.assertIn("`New thread ${to}`", chat)
        self.assertIn(f'data-reply-to="{first}" data-reply-label="Pam: Part one"', chat)
        self.assertIn(f'data-reply-to="{mine}" data-reply-label="Robin: Good, go on."', chat)

        # What the owner can do with a message is in its menu, which a tap opens; none of it is drawn on the row.
        menus = re.findall(r'<template class="menu" data-label="([^"]*)" data-react="([^"]*)">(.*?)</template>', chat)
        by_label = {label: (react, tools) for label, react, tools in menus}
        react, tools = by_label["Pam: Part one"]
        self.assertEqual(react, first)
        self.assertIn(f'data-copy-original="{first}"', tools)
        self.assertIn('<p class="facts">Sent ', tools)
        react, tools = by_label["Robin: Good, go on."]
        self.assertEqual(react, "")
        self.assertIn(f'href="/rules?pin={mine}&amp;where=dm-pam">Pin as rule</a>', tools)
        self.assertIn('data-copy-text="Good, go on."', tools)
        outside = re.sub(r"<template.*?</template>", "", chat, flags=re.S)
        for drawn in ("Pin as rule", 'data-reply-to="', 'data-copy-text="', 'data-copy-original="',
                      'aria-label="More"'):
            self.assertNotIn(drawn, outside)

        # Picked up is a check by the time, with who and when in the menu; only what isn't takes a line.
        status, out = self.local("POST", "/api/dm-pam/delivered", {"name": "Pam", "ids": [mine]})
        self.assertEqual(status, 200, out)
        chat = self.call("GET", "/chat/pam")[1]

        def row(msg_id):
            return re.search(rf'<article [^>]*id="g-{msg_id}".*?</article>', chat, re.S)[0]
        self.assertIn('<span class="got" title="Picked up by Pam ', row(mine))
        self.assertNotIn('class="state"', row(mine))
        self.assertIn(". Picked up by Pam ", row(mine).split('<p class="facts">')[1])
        self.assertIn('<div class="state">Waiting to be picked up', row(later))
        self.assertNotIn('class="got"', row(later))

        # Grouped: replies fold under the first message, which says how many and how many are new. Each thread
        # keeps one activity row, at its newest reply, standing for every reply folded into it.
        self.set_view("grouped")
        try:
            chat = self.call("GET", "/chat/pam")[1]
            self.assertIn(f'id="m-{root}"', chat)
            self.assertIn(f'id="g-{later}"', chat)
            self.assertEqual(chat.count('class="msg activity"'), 1)
            self.assertIn(f'<article class="msg activity" id="g-{mine}" data-covers="m-{first} g-{mine}">', chat)
            self.assertNotIn(f'id="m-{first}"', chat)
            self.assertIn('In thread Pam: <span>Plan</span>', chat)
            self.assertIn(f'<a class="what" href="/t/{key}#g-{mine}">Good, go on.</a>', chat)
            self.assertIn(f'<a class="replies" href="/t/{key}#g-{mine}">', chat)
            # The activity row is where the newest reply was said: before the later message, after its root.
            self.assertLess(chat.index(f'id="m-{root}"'), chat.index(f'id="g-{mine}"'))
            self.assertLess(chat.index(f'id="g-{mine}"'), chat.index(f'id="g-{later}"'))
            self.assertIn("<b>2 replies</b>", chat)
            self.assertNotIn('<span class="tag new">', chat.split("<b>2 replies</b>")[1].split("</a>")[0])
            self.assertIn("Reply in thread</button>", chat)
            self.assertIn("rowFor", chat)
            time.sleep(1.1)
            newer = self.reply("Pam", "Part two is in.", root, "Part two")
            chat = self.call("GET", "/chat/pam")[1]
            self.assertEqual(chat.count('class="msg activity"'), 1)
            self.assertIn(f'id="m-{newer}" data-covers="m-{first} g-{mine} m-{newer}"', chat)
            self.assertNotIn(f'id="g-{mine}"', chat)
            self.assertEqual(chat.count('<b>3 replies</b><span class="tag new">1 new</span>'), 2)
            # The owner's own reply, sent from the bottom, is the newest thing in the conversation.
            time.sleep(1.1)
            sent = self.owner_says("Ship it.", root)
            chat = self.call("GET", "/chat/pam")[1]
            self.assertEqual(chat.count('class="msg activity"'), 1)
            self.assertIn(f'id="g-{sent}" data-covers="m-{first} g-{mine} m-{newer} g-{sent}"', chat)
            self.assertLess(chat.index(f'id="g-{later}"'), chat.index(f'id="g-{sent}"'))
            self.assertIn('<b>4 replies</b>', chat)
            # The thread's page shows every message, and the setting does not touch it.
            page = self.call("GET", f"/t/{key}")[1]
            for row in (f'id="m-{first}"', f'id="g-{mine}"', f'id="m-{newer}"', f'id="g-{sent}"'):
                self.assertIn(row, page)
            self.assertNotIn('class="msg activity"', page)
            self.assertIn('data-to="to Pam"', page)
            self.assertNotIn('data-thread=""', page)
            self.assertIn("`Reply in this thread, ${to}`", page)
            self.assertNotIn(f'id="g-{later}"', page)
            # An older page of the conversation holds one activity row too, not one per reply it reaches.
            status, out = self.call("GET", "/api/thread?box=pam&before=9999999999~0~20991231-235959-ffff")
            self.assertEqual(status, 200, out)
            self.assertEqual(out.count('class=\\"msg activity\\"'), 1)
            self.assertIn(f'id=\\"m-{root}\\"', out)
            self.assertNotIn(f'id=\\"m-{first}\\"', out)
        finally:
            self.set_view("linear")
        chat = self.call("GET", "/chat/pam")[1]
        self.assertIn(f'id="m-{first}"', chat)
        self.assertNotIn('class="msg activity"', chat)

    def test_a_reply_from_another_line_leaves_the_activity_row_here(self):
        # Sol answers a message on Ria's line; its reply lands on Sol's line, not in Ria's conversation.
        for name in ("Ria", "Sol"):
            self.assertEqual(self.local("POST", f"/api/dm-{name.lower()}", {"name": name, "open": True})[0], 200)
        root = self.send("Ria", "Which branch ships?", title="Branch")
        key = f"m-{root}"
        time.sleep(1.1)
        mine = self.call("POST", "/api/dm-ria/messages", {"text": "The release one.", "re": root})
        self.assertEqual(mine[0], 200, mine[1])
        mine = [m for m in json.loads(self.local("GET", "/api/dm-ria/pending?name=Ria")[1])
                if m["text"] == "The release one."][0]["id"]
        time.sleep(1.1)
        theirs = self.reply("Sol", "Release, agreed.", root, "Agreed")
        self.set_view("grouped")
        try:
            chat = self.call("GET", "/chat/ria")[1]
            self.assertEqual(chat.count('class="msg activity"'), 1)
            self.assertIn(f'id="g-{mine}" data-covers="g-{mine}"', chat)
            self.assertNotIn(f'id="m-{theirs}"', chat)
            self.assertIn("<b>2 replies</b>", chat)  # the count is the whole thread's
            self.assertLess(chat.index(f'id="m-{root}"'), chat.index(f'id="g-{mine}"'))
            # Where the root is not, the reply shows as itself, quoting what it answers.
            sol = self.call("GET", "/chat/sol")[1]
            self.assertIn(f'id="m-{theirs}"', sol)
            self.assertNotIn('class="msg activity"', sol)
            self.assertIn(f'<a href="/t/{key}#m-{root}">Replying to', sol)
            # An older page agrees.
            status, out = self.call("GET", "/api/thread?box=ria&before=9999999999~0~20991231-235959-ffff")
            self.assertEqual(status, 200, out)
            self.assertEqual(out.count('class=\\"msg activity\\"'), 1)
        finally:
            self.set_view("linear")

    def test_a_changed_view_changes_the_version(self):
        self.assertEqual(self.local("POST", "/api/dm-quin", {"name": "Quin", "open": True})[0], 200)
        self.send("Quin", "Hello.", title="Hi")
        before = json.loads(self.call("GET", "/api/chat/version?box=quin")[1])["version"]
        self.set_view("grouped")
        try:
            after = json.loads(self.call("GET", "/api/chat/version?box=quin")[1])["version"]
        finally:
            self.set_view("linear")
        self.assertNotEqual(before, after)

    def test_the_setting(self):
        status, out = self.call("POST", "/api/settings", {"thread_view": "stacked"})
        self.assertEqual(status, 400, out)
        self.assertIn("thread_view", out)
        page = self.call("GET", "/settings")[1]
        self.assertIn("<h2>Conversations</h2>", page)
        self.assertIn('<option value="linear" selected>in time order</option>', page)
        self.assertIn('<option value="grouped">grouped by thread</option>', page)
