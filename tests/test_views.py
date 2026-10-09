"""The pages that cut across conversations: Waiting on you, Unread, Search, Export."""

import http.client
import json
import re
import subprocess
import sys
import time

from harness import COOKIE, PROXY, Case


class Views(Case):
    PORT_OFFSET = 1

    def test_waiting_lists_unanswered_questions_until_answered(self):
        ask = self.send("asker", "Ship it on Friday?", ask=True, title="Friday ship")
        self.send("asker", "Just telling you.", title="Plain note")
        status, page = self.call("GET", "/waiting")
        self.assertEqual(status, 200)
        self.assertIn("Friday ship", page)
        self.assertNotIn("Plain note", page)
        self.assertRegex(page, r"\d+ questions? waiting")
        side = json.loads(self.local("GET", "/api/board")[1])["sidebar"]
        self.assertGreaterEqual(side["waiting"], 1)
        self.assertEqual(self.call("POST", f"/api/messages/{ask}/reply", {"text": "yes"})[0], 200)
        page = self.call("GET", "/waiting")[1]
        self.assertNotIn("Friday ship", page)

    def test_waiting_is_for_the_paired_owner(self):
        self.assertEqual(self.call("GET", "/waiting", paired=False)[0], 403)
        self.assertEqual(self.request("GET", "/waiting")[0], 302)

    def test_sidebar_links_to_waiting(self):
        page = self.call("GET", "/waiting")[1]
        self.assertTrue(re.search(r'<a class="conv" href="/waiting"[^>]*>.*?Waiting on you', page, re.S), page[:500])


class Unread(Case):
    PORT_OFFSET = 2

    def test_unread_lists_unseen_messages_and_mark_all_read_clears_them(self):
        self.send("reader", "First thing.", title="Unseen thing")
        self.send("reader", "In general.", channel="general", title="Unseen in general")
        page = self.call("GET", "/unread")[1]
        self.assertIn("Unseen thing", page)
        self.assertIn("Unseen in general", page)
        self.assertIn("data-read-all", page)
        self.assertRegex(page, r"\d+ unread")
        self.assertEqual(self.call("POST", "/api/read-all", {}, paired=False)[0], 401)
        self.assertEqual(self.local("POST", "/api/read-all", {})[0], 403)
        status, body = self.call("POST", "/api/read-all", {})
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(json.loads(body)["marked"], 2)
        page = self.call("GET", "/unread")[1]
        self.assertNotIn("Unseen thing", page)
        self.assertIn("Nothing unread.", page)
        self.assertEqual(json.loads(self.local("GET", "/api/board")[1])["sidebar"]["unread"], 0)

    def test_viewing_unread_does_not_mark_anything_seen(self):
        self.send("reader", "Stay unread.", title="Stays unread")
        before = json.loads(self.local("GET", "/api/board")[1])["sidebar"]["unread"]
        self.call("GET", "/unread")
        self.assertEqual(json.loads(self.local("GET", "/api/board")[1])["sidebar"]["unread"], before)
        self.assertGreaterEqual(before, 1)


class Search(Case):
    PORT_OFFSET = 3

    def test_search_matches_every_word_in_text_title_sender_and_answer(self):
        a = self.send("finder", "The deploy used a blue-green rollout.", title="Rollout notes")
        self.send("finder", "Unrelated chatter about lunch.", title="Lunch")
        ask = self.send("seeker", "Which region?", ask=True, title="Region question")
        self.assertEqual(self.call("POST", f"/api/messages/{ask}/reply", {"text": "frankfurt please"})[0], 200)
        page = self.call("GET", "/search?q=BLUE+rollout")[1]
        self.assertIn("Rollout notes", page)
        self.assertNotIn("Lunch", page.split("<main")[1])
        self.assertIn("1 match", page)
        self.assertIn("Region question", self.call("GET", "/search?q=frankfurt")[1])
        self.assertIn("Region question", self.call("GET", "/search?q=seeker")[1])
        self.assertIn("Rollout notes", self.call("GET", "/search?q=rollout+notes")[1])
        self.assertIn(f'/m/{a}', self.call("GET", "/search?q=blue-green")[1])

    def test_search_without_a_match_or_a_query(self):
        self.assertIn("No message matches.", self.call("GET", "/search?q=zzzzqqqq")[1])
        page = self.call("GET", "/search")[1]
        self.assertIn('name="q"', page)
        self.assertNotIn("No message matches.", page)

    def test_search_escapes_the_query_and_is_for_the_paired_owner(self):
        page = self.call("GET", "/search?q=%3Cscript%3Ealert(1)%3C/script%3E")[1]
        self.assertNotIn("<script>alert(1)", page)
        self.assertEqual(self.call("GET", "/search?q=x", paired=False)[0], 403)

    def test_keyboard_shortcuts_are_in_every_page_and_described_on_search(self):
        for path in ("/search", "/waiting", "/"):
            self.assertIn('const GO = { w: "/waiting"', self.call("GET", path)[1], path)
        self.assertIn("/ opens search", self.call("GET", "/search")[1])


class Export(Case):
    PORT_OFFSET = 5

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", path, headers={**PROXY, "Cookie": f"{COOKIE}={self.key}"})
        r = conn.getresponse()
        body = r.read().decode()
        conn.close()
        return r.status, dict(r.getheaders()), body

    def test_export_is_markdown_oldest_first_with_answers_and_filters(self):
        first = self.send("exporter", "Which region?", ask=True, title="Region")
        self.assertEqual(self.call("POST", f"/api/messages/{first}/reply", {"text": "frankfurt please"})[0], 200)
        time.sleep(1.1)  # messages order by the second they were made
        self.send("exporter", "A channel post.", channel="general", title="Posted")
        time.sleep(1.1)
        self.send("other", "Something else entirely.", title="Elsewhere")
        status, headers, text = self.get("/export.md")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/markdown"))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertLess(text.index("## Region"), text.index("## Posted"))
        self.assertLess(text.index("## Posted"), text.index("## Elsewhere"))
        self.assertIn("Which region?", text)
        self.assertIn("frankfurt please", text)
        self.assertIn("Question from", text)
        text = self.get("/export.md?from=EXPORTER")[2]
        self.assertIn("## Region", text)
        self.assertNotIn("## Elsewhere", text)
        text = self.get("/export.md?channel=general")[2]
        self.assertIn("## Posted", text)
        self.assertNotIn("## Region", text)
        text = self.get("/export.md?q=frankfurt")[2]
        self.assertIn("## Region", text)
        self.assertNotIn("## Posted", text)
        self.assertIn("1 message,", text)

    def test_export_is_for_the_paired_owner_and_linked_from_search(self):
        self.send("exporter", "Linkable content.", title="Linkable")
        self.assertEqual(self.call("GET", "/export.md", paired=False)[0], 403)
        self.assertEqual(self.request("GET", "/export.md")[0], 302)
        self.assertIn('href="/export.md" download', self.call("GET", "/search")[1])
        self.assertIn('href="/export.md?q=linkable" download', self.call("GET", "/search?q=linkable")[1])


class Manifest(Case):
    PORT_OFFSET = 6

    def test_manifest_has_shortcuts_that_carry_the_key(self):
        status, body = self.call("GET", f"/manifest.webmanifest?k={self.key}", paired=False)
        self.assertEqual(status, 200)
        shortcuts = json.loads(body)["shortcuts"]
        self.assertEqual([s["url"] for s in shortcuts],
                         [f"/waiting?k={self.key}", f"/unread?k={self.key}", f"/search?k={self.key}"])


class SetAside(Case):
    PORT_OFFSET = 14

    def answers(self, agent):
        return json.loads(self.local("GET", f"/api/agents/{agent}/answers")[1])

    def test_setting_aside_clears_waiting_tells_the_asker_and_leaves_it_answerable(self):
        old = self.send("asider", "Rename the module?", ask=True, title="Old question")
        cut = time.time()
        new = self.send("asider", "Merge today?", ask=True, title="New question")
        page = self.call("GET", "/waiting")[1]
        self.assertRegex(page, r'data-set-aside="\d+\.\d{6}" data-n="\d+">Set all \d+ aside')
        self.assertNotIn("older than a day", page)
        self.assertEqual(self.call("POST", "/api/set-aside", {"before": cut}, paired=False)[0], 401)
        self.assertEqual(self.local("POST", "/api/set-aside", {"before": cut})[0], 403)
        self.assertEqual(self.call("POST", "/api/set-aside", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/set-aside", {"before": "today"})[0], 400)
        status, body = self.call("POST", "/api/set-aside", {"before": cut})
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(json.loads(body)["set_aside"], 1)
        page = self.call("GET", "/waiting")[1]
        self.assertNotIn("Old question", page)
        self.assertIn("New question", page)
        got = json.loads(self.local("GET", f"/api/messages/{old}")[1])
        self.assertIsNotNone(got["set_aside"])
        self.assertIsNotNone(got["seen"])
        self.assertIsNone(json.loads(self.local("GET", f"/api/messages/{new}")[1])["set_aside"])
        page = self.call("GET", f"/m/{old}")[1]
        self.assertIn(">set aside</span>", page)
        self.assertIn("(set aside; they were told)", page)

        (item,) = self.answers("asider")
        self.assertEqual((item["kind"], item["id"], item["re"]["id"]), ("set_aside", f"aside-{old}", old))
        self.assertIn("set this question aside without answering it", item["text"])
        # What MCP servers from before set_aside read of an answer.
        self.assertFalse({"text", "reaction", "audio", "files", "tap", "at", "re"} - item.keys())
        from nanotea.mailbox import _answer_item
        self.assertEqual(_answer_item(item), {"kind": "set_aside", "from": "nanotea", "at": item["at"],
                                              "id": f"aside-{old}", "re": item["re"], "text": item["text"]})
        self.assertEqual(self.local("POST", "/api/agents/asider/answers/delivered",
                                    {"ids": [f"aside-{new}"]})[0], 409)
        self.assertEqual(self.local("POST", "/api/agents/other/answers/delivered",
                                    {"ids": [f"aside-{old}"]})[0], 409)
        status, body = self.local("POST", "/api/agents/asider/answers/delivered",
                                  {"ids": [f"aside-{old}"], "listener": "test"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.answers("asider"), [])

        # Answered after all: the answer still reaches the asker.
        self.assertEqual(self.call("POST", f"/api/messages/{old}/reply", {"text": "yes, rename it"})[0], 200)
        (item,) = self.answers("asider")
        self.assertEqual((item["kind"], item["id"], item["text"]), ("answer", old, "yes, rename it"))
        self.assertNotIn(">set aside</span>", self.call("GET", f"/m/{old}")[1])

    def test_set_aside_before_the_asker_hears_sends_only_the_answer(self):
        q = self.send("late", "Which port?", ask=True, title="Port question")
        self.assertEqual(self.call("POST", "/api/set-aside", {"before": time.time()})[0], 200)
        self.assertEqual([a["kind"] for a in self.answers("late")], ["set_aside"])
        self.assertEqual(self.call("POST", f"/api/messages/{q}/reply", {"text": "7447"})[0], 200)
        self.assertEqual([(a["kind"], a["id"]) for a in self.answers("late")], [("answer", q)])

    def test_tell_ask_stops_waiting_when_set_aside(self):
        self.token("teller")
        p = subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", "teller", "--voice", self.agent("teller"),
                              "--ask", "--title", "Tell question", "Proceed?"], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(lambda: [m for m in json.loads(self.owner("GET", "/api/messages")[1])
                                   if m["sender"] == "teller" and m["delivered"]], "teller's question")
            self.assertEqual(self.call("POST", "/api/set-aside", {"before": time.time()})[0], 200)
            out, err = p.communicate(timeout=30)
        finally:
            p.kill()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("set this question aside without answering it", err)
        self.assertEqual(self.answers("teller"), [])
