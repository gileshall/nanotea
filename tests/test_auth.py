"""Who may do what: agents' tokens, the owner's key, events tokens, and no trust in an address."""

import http.client
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import COOKIE, PROXY, ROOT, Case, spawn, stop
from nanotea import tokens as token_book

SECRET = re.compile(r"nt_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
BAD = "nt_00000000_" + "A" * 43


class Auth(Case):
    PORT_OFFSET = 9

    def raw(self, method, path, body=None, headers=None):
        """Status, headers and body of one request."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {**({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})})
        r = conn.getresponse()
        out = (r.status, dict(r.getheaders()), r.read().decode())
        conn.close()
        return out

    def issue(self, name, kind="agent", line=None, **more):
        status, out = self.owner("POST", "/api/tokens", {"kind": kind, "name": name, "line": line, **more})
        self.assertEqual(status, 200, out)
        return json.loads(out)

    def error(self, out):
        return json.loads(out)["error"]

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "nanotea", *args], env=env or self.env, cwd=self.tmp.name,
                              capture_output=True, text=True, timeout=60)

    def test_no_token_is_refused_with_the_fix(self):
        for method, path, body in (("POST", "/api/messages", {"text": "hi", "from": "mallory", "voice": "v1"}),
                                   ("POST", "/api/dm-victim", {"name": "victim", "open": True}),
                                   ("GET", "/api/dm-victim/pending?name=victim", None),
                                   ("GET", "/api/voices", None),
                                   ("POST", "/api/talk", {"from": "mallory", "to": "victim", "text": "x"}),
                                   ("POST", "/api/dm-victim/events", {"kind": "k", "source": "s", "summary": "x"}),
                                   ("GET", "/api/agents/victim/waiting", None)):
            status, headers, out = self.raw(method, path, body)
            self.assertEqual(status, 401, (path, out))
            self.assertIn("asks for approval of a token", self.error(out))
            self.assertEqual(headers["WWW-Authenticate"], 'Bearer realm="nanotea"')

    def test_wrong_and_malformed_tokens_are_refused(self):
        for secret in (BAD, "nt_zz", "hunter2", self.key[::-1]):
            status, out = self.bearer("GET", "/api/voices", None, secret)
            self.assertEqual(status, 401, (secret, out))
        self.assertIn("unknown token", self.error(self.bearer("GET", "/api/voices", None, BAD)[1]))
        status, _, out = self.raw("GET", "/api/voices", headers={"Authorization": "Basic Zm9vOmJhcg=="})
        self.assertEqual(status, 401)
        self.assertIn("Bearer", self.error(out))

    def test_revoked_token_is_told_it_was_revoked(self):
        made = self.issue("rev")
        token = made["token"]
        self.assertEqual(self.bearer("GET", "/api/voices", None, token)[0], 200)
        status, out = self.owner("POST", f"/api/tokens/{made['record']['id']}/revoke", {})
        self.assertEqual(status, 200, out)
        status, out = self.bearer("GET", "/api/voices", None, token)
        self.assertEqual(status, 401)
        self.assertIn("revoked", self.error(out))
        self.assertIn("asks for approval of a new one", self.error(out))
        self.assertEqual(self.owner("POST", f"/api/tokens/{made['record']['id']}/revoke", {})[0], 409)
        self.assertEqual(self.owner("POST", "/api/tokens/00000000/revoke", {})[0], 404)

    def test_a_token_acts_only_as_its_own_name(self):
        a, b = "alpha", "bravo"
        self.assertEqual(self.local("POST", "/api/dm-bravo", {"name": b, "open": True})[0], 200)
        mine = self.token(a)
        for method, path, body in (
                ("POST", "/api/messages", {"text": "hi", "from": b, "voice": self.agent(a)}),
                ("POST", "/api/dm-bravo", {"name": b, "open": False}),
                ("GET", f"/api/dm-bravo/pending?name={b}", None),
                ("GET", f"/api/dm-bravo/history?name={b}", None),
                ("GET", f"/api/agents/{b}/waiting?line=dm-bravo", None),
                ("GET", f"/api/agents/{b}/answers", None),
                ("GET", f"/api/talk/pending?name={b}", None),
                ("POST", "/api/talk", {"from": b, "to": a, "text": "x"}),
                ("POST", "/api/dm-bravo/delivered", {"name": b, "ids": []}),
                ("POST", "/api/dm-bravo/events", {"kind": "k", "source": b, "summary": "x"}),
                ("POST", "/api/status", {"name": b, "text": "x"}),
                ("POST", f"/api/agents/{b}/held", {"clear": True}),
                ("POST", "/api/sessions", {"name": b, "session": "ab12", "pid": 1, "line": "bravo"})):
            status, out = self.bearer(method, path, body, mine)
            self.assertEqual(status, 403, (method, path, out))
            self.assertIn(f"for {a!r}, not {b!r}", self.error(out))

    def test_one_agent_cannot_read_anothers_inbox_or_messages(self):
        self.send("carol", "to the line", title="carol's")
        carol_msg = self.send("carol", "private", title="carol's private")
        self.assertEqual(self.local("POST", "/api/dm-carol", {"name": "carol", "open": True})[0], 200)
        self.assertEqual(self.owner("POST", "/api/dm-carol/messages", {"text": "for carol only"})[0], 200)
        # dave asks as himself: the line is not his.
        status, out = self.local("GET", "/api/dm-carol/pending?name=dave")
        self.assertEqual(status, 409, out)
        self.assertNotIn("for carol only", out)
        self.assertNotIn("for carol only", self.local("GET", "/api/dm-carol/history?name=dave")[1])
        self.assertEqual(self.local("POST", "/api/dm-carol", {"name": "dave", "open": False})[0], 409)
        # and as carol, with dave's token: refused before the line is looked at.
        status, out = self.bearer("GET", "/api/dm-carol/pending?name=carol", None, self.token("dave"))
        self.assertEqual(status, 403, out)
        self.assertNotIn("for carol only", out)
        # carol's own messages are hers alone.
        self.assertEqual(self.local("GET", f"/api/messages/{carol_msg}", as_="carol")[0], 200)
        status, out = self.local("GET", f"/api/messages/{carol_msg}", as_="dave")
        self.assertEqual(status, 403, out)
        self.assertEqual(self.local("GET", "/api/dm-carol/pending?name=carol")[0], 200)

    def test_an_agent_holds_one_line(self):
        self.assertEqual(self.local("POST", "/api/dm-wa", {"name": "wren", "open": True})[0], 200)
        for line in ("wb", "main"):
            status, out = self.local("POST", f"/api/{'inbox' if line == 'main' else 'dm-' + line}",
                                     {"name": "wren", "open": True}, as_="wren")
            self.assertEqual(status, 409, out)
            self.assertIn("'wren' already holds the line 'wa'", self.error(out))
            self.assertIn("--close-inbox --inbox wa", self.error(out))
        # Refused before the line is made: no second row.
        self.assertEqual(self.local("GET", "/api/dm-wb/pending?name=wren")[0], 404)
        rows = [r for g in json.loads(self.local("GET", "/api/board")[1])["sidebar"]["groups"] for r in g["rows"]
                if r["agent"] == "wren"]
        self.assertEqual([r["path"] for r in rows], ["/chat/wa"])
        self.assertEqual(self.local("POST", "/api/dm-wa", {"name": "wren", "open": True})[0], 200)
        self.assertEqual(self.local("POST", "/api/dm-wa", {"name": "wren", "open": False})[0], 200)
        self.assertEqual(self.local("POST", "/api/dm-wb", {"name": "wren", "open": True})[0], 200)
        self.assertEqual(self.local("POST", "/api/dm-wb", {"name": "wren", "open": False})[0], 200)

    def test_an_agent_sees_only_its_own_owner_messages_in_a_thread(self):
        self.assertEqual(self.local("POST", "/api/dm-una", {"name": "una", "open": True})[0], 200)
        status, out = self.owner("POST", "/api/dm-una/messages", {"text": "for una alone"})
        self.assertEqual(status, 200, out)
        mine = self.send("una", "una's own", title="una's")
        time.sleep(1.1)
        status, out = self.owner("POST", "/api/dm-una/messages", {"text": "una, about that", "re": mine})
        self.assertEqual(status, 200, out)
        pending = json.loads(self.local("GET", "/api/dm-una/pending?name=una")[1])
        (owner_id,) = [m["id"] for m in pending if m["text"] == "for una alone"]

        # The owner's message on una's line is not vic's: replying to it reads as no such message.
        def refused(target):
            status, out = self.local("POST", "/api/messages", {"from": "vic", "text": "me too", "re": target})
            self.assertEqual(status, 400, out)
            return self.error(out).replace(target, "ID")

        self.assertEqual(refused(owner_id), refused("20000101-000000-0000"))
        # una answers it while it waits on her line, and once given it.
        status, out = self.local("POST", "/api/messages", {"from": "una", "text": "on it", "re": owner_id})
        self.assertEqual(status, 202, out)
        self.assertEqual(self.local("POST", "/api/dm-una/delivered",
                                    {"name": "una", "ids": [m["id"] for m in pending]})[0], 200)
        status, out = self.local("POST", "/api/messages", {"from": "una", "text": "got it", "re": owner_id})
        self.assertEqual(status, 202, out)
        self.assertEqual(self.local("POST", "/api/dm-una", {"name": "una", "open": False})[0], 200)
        self.assertEqual(refused(owner_id), refused("20000101-000000-0000"))
        # vic may answer una herself; the thread then shows vic hers and its own, not the owner's to her.
        status, out = self.local("POST", "/api/messages", {"from": "vic", "text": "me too", "re": mine,
                                                           "voice": self.agent("vic")})
        self.assertEqual(status, 202, out)

        def thread(name):
            status, out = self.local("GET", f"/api/agents/{name}/thread/m-{mine}")
            self.assertEqual(status, 200, out)
            return [(m["from"], m["text"]) for m in json.loads(out)["messages"]]

        self.wait_for(lambda: len(thread("vic")) == 2, "vic's reply in the thread")
        self.assertEqual(thread("vic"), [("una", "una's own"), ("vic", "me too")])
        self.assertEqual(thread("una"), [("una", "una's own"), ("owner", "una, about that"), ("vic", "me too")])
        self.assertEqual(self.local("GET", f"/api/agents/wes/thread/m-{mine}")[0], 403)

    def test_sessions_belong_to_their_agent(self):
        sid = "5e55100a"
        self.assertEqual(self.local("POST", "/api/sessions", {"name": "erin", "session": sid, "pid": os.getpid(),
                                                                "line": "erin"})[0], 200)
        for path in (f"/api/sessions/{sid}/beat", f"/api/sessions/{sid}/close"):
            status, out = self.local("POST", path, {}, as_="finn")
            self.assertEqual(status, 403, out)
            self.assertIn("erin", self.error(out))
        self.assertEqual(self.local("POST", f"/api/sessions/{sid}/close", {}, as_="erin")[0], 200)

    def test_owner_reads_need_the_key_even_locally(self):
        self.send("gil", "something to read")
        for path in ("/api/messages", "/api/theme", "/api/settings", "/api/channels", "/api/rules", "/api/board",
                     "/api/tokens"):
            status, _, out = self.raw("GET", path)
            self.assertEqual(status, 401, (path, out))
        for path in ("/", "/settings", "/tokens", "/waiting", "/export.md", "/chat", "/rules"):
            status, headers, _ = self.raw("GET", path)
            self.assertEqual(status, 302, path)
            self.assertTrue(headers["Location"].startswith("https://nanotea.test/"), headers)
            # The proxy's headers are not credentials.
            status, _, out = self.raw("GET", path, headers=PROXY)
            self.assertEqual(status, 403, path)
            self.assertIn("isn't paired", out)
        for path in ("/api/messages", "/api/tokens", "/settings", "/tokens"):
            self.assertEqual(self.call("GET", path)[0], 200, path)
            self.assertEqual(self.raw("GET", f"{path}?k={self.key}", headers=PROXY)[0], 200, path)
            self.assertEqual(self.raw("GET", path, headers={"Cookie": f"{COOKIE}={self.key}"})[0], 200, path)
            self.assertEqual(self.owner("GET", path)[0], 200, path)
        self.assertEqual(self.raw("GET", "/api/messages?k=" + "0" * len(self.key))[0], 401)
        # An agent's token is not the owner's key.
        for path in ("/api/messages", "/api/theme", "/api/tokens"):
            status, out = self.local("GET", path)
            self.assertIn(status, (403, 404), (path, out))
        for path in ("/api/messages", "/settings", "/export.md"):
            self.assertEqual(self.local("GET", path)[0], 403, path)
        for path, body in (("/api/settings", {"held": False}), ("/api/tokens", {"kind": "agent", "name": "zed"}),
                           ("/api/read-all", {}), ("/api/hush", {"muted": []}), ("/api/rules", {"text": "x"})):
            self.assertEqual(self.local("POST", path, body)[0], 403, path)

    def test_the_owners_key_is_not_an_agent(self):
        for method, path, body in (("POST", "/api/messages", {"text": "x", "from": "robin", "voice": "v1"}),
                                   ("GET", "/api/dm-x/pending?name=x", None),
                                   ("POST", "/api/dm-x", {"name": "x", "open": True})):
            status, out = self.owner(method, path, body)
            self.assertEqual(status, 403, (path, out))
            self.assertIn("agent's token", self.error(out))

    def test_owner_posts_need_the_key_too(self):
        for path, body in (("/api/settings", {"held": False}), ("/api/tokens", {"kind": "agent", "name": "zed"}),
                           ("/api/rules", {"text": "x"})):
            self.assertEqual(self.raw("POST", path, body)[0], 401, path)
            self.assertEqual(self.raw("POST", path, body, PROXY)[0], 401, path)

    def test_an_events_token_only_posts_its_own_events(self):
        made = self.issue("ci", "events", "main")
        token = made["token"]
        self.assertEqual(made["record"]["line"], "main")
        status, out = self.bearer("POST", "/api/inbox/events", {"kind": "ran", "source": "ci", "summary": "green"},
                                  token)
        self.assertEqual(status, 202, out)
        # Another source, another line, any read, any other route: refused.
        refused = (
            ("POST", "/api/inbox/events", {"kind": "ran", "source": "other", "summary": "x"}),
            ("POST", "/api/dm-victim/events", {"kind": "ran", "source": "ci", "summary": "x"}),
            ("GET", "/api/voices", None), ("GET", "/api/inbox/pending?name=ci", None),
            ("GET", "/api/messages/20260101-000000-abcd", None), ("GET", "/api/channels", None),
            ("POST", "/api/messages", {"text": "x", "from": "ci", "voice": "v1"}),
            ("POST", "/api/inbox", {"name": "ci", "open": True}),
            ("POST", "/api/talk", {"from": "ci", "to": "x", "text": "x"}),
            ("POST", "/api/tokens", {"kind": "agent", "name": "x"}))
        for method, path, body in refused:
            status, out = self.bearer(method, path, body, token)
            self.assertEqual(status, 403, (method, path, out))
        # An agent token for the same name is an agent's, not an events source's, and posts events only as itself.
        agent = self.token("ci")
        self.assertEqual(self.bearer("POST", "/api/inbox/events", {"kind": "ran", "source": "ci", "summary": "x"},
                                     agent)[0], 202)
        self.assertEqual(self.bearer("POST", "/api/inbox/events", {"kind": "ran", "source": "bob", "summary": "x"},
                                     agent)[0], 403)
        # A malformed event is refused saying what an event is, not with an exception's name.
        for body, error in (({"source": "ci"}, "an event needs 'kind', 'summary': {kind, source, summary, data?}"),
                            (["ci"], "an event is a JSON object: {kind, source, summary, data?}")):
            status, out = self.bearer("POST", "/api/inbox/events", body, token)
            self.assertEqual((status, json.loads(out)), (400, {"error": error}))

    def test_events_token_needs_a_line_and_lines_are_line_names(self):
        self.assertEqual(self.owner("POST", "/api/tokens", {"kind": "events", "name": "ci2"})[0], 400)
        self.assertEqual(self.owner("POST", "/api/tokens", {"kind": "events", "name": "ci2", "line": "Bad Line"})[0],
                         400)
        self.assertEqual(self.owner("POST", "/api/tokens", {"kind": "agent", "name": "x", "line": "Bad Line"})[0], 400)
        self.assertEqual(self.owner("POST", "/api/tokens", {"kind": "agent", "name": "x", "line": 7})[0], 400)
        self.assertEqual(self.owner("POST", "/api/tokens", {"kind": "root", "name": "x"})[0], 400)
        for name in ("", " padded ", "a" * 81, "tab\there", "owner", "Owner"):
            self.assertEqual(self.owner("POST", "/api/tokens", {"kind": "agent", "name": name})[0], 400, repr(name))

    def test_a_token_bound_to_a_line_keeps_to_it_and_keeps_others_off_it(self):
        made = self.issue("hal", line="hq")
        self.assertEqual(made["record"]["line"], "hq")
        hal = made["token"]

        def as_hal(method, path, body=None):
            return self.bearer(method, path, body, hal)

        def session(token, name, line):
            return self.bearer("POST", "/api/sessions", {"name": name, "session": os.urandom(4).hex(),
                                                         "pid": os.getpid(), "line": line}, token)

        # hal works on hq, and nowhere else.
        self.assertEqual(as_hal("POST", "/api/dm-hq", {"name": "hal", "open": True})[0], 200)
        self.assertEqual(as_hal("GET", "/api/dm-hq/pending?name=hal")[0], 200)
        for path in ("/api/inbox", "/api/dm-hal"):
            status, out = as_hal("POST", path, {"name": "hal", "open": True})
            self.assertEqual(status, 403, out)
            self.assertIn("bound to line 'hq'", self.error(out))
        self.assertEqual(session(hal, "hal", "elsewhere")[0], 403)
        status, out = session(hal, "hal", "hq")
        self.assertEqual(status, 200, out)
        self.assertEqual(self.bearer("POST", f"/api/sessions/{json.loads(out)['session']}/close", {}, hal)[0], 200)
        self.assertEqual(as_hal("POST", "/api/dm-hq", {"name": "hal", "open": False})[0], 200)
        # With hq free, another agent still can't take it, listen on it, or open a session there.
        ivy = self.token("ivy")
        for method, path, body in (("POST", "/api/dm-hq", {"name": "ivy", "open": True}),
                                   ("GET", "/api/dm-hq/pending?name=ivy", None),
                                   ("GET", "/api/agents/ivy/waiting?line=dm-hq", None)):
            status, out = self.bearer(method, path, body, ivy)
            self.assertEqual(status, 403, f"{path}: {out}")
            self.assertIn("bound to 'hal'", self.error(out))
        self.assertEqual(session(ivy, "ivy", "hq")[0], 403)
        # No second agent is bound to a line that has one; hal's own rotation keeps it.
        status, out = self.owner("POST", "/api/tokens", {"kind": "agent", "name": "jo", "line": "hq"})
        self.assertEqual(status, 409, out)
        self.assertIn("revoke that one first", self.error(out))
        hal = self.issue("hal", line="hq", replace=True)["token"]
        self.assertEqual(as_hal("POST", "/api/dm-hq", {"name": "hal", "open": True})[0], 200)
        self.assertEqual(as_hal("POST", "/api/dm-hq", {"name": "hal", "open": False})[0], 200)
        # A line someone else holds can't be bound out from under them.
        self.assertEqual(self.bearer("POST", "/api/dm-held", {"name": "ivy", "open": True}, ivy)[0], 200)
        status, out = self.owner("POST", "/api/tokens", {"kind": "agent", "name": "jo", "line": "held"})
        self.assertEqual(status, 409, out)
        self.assertIn("held by 'ivy'", self.error(out))
        self.assertEqual(self.bearer("POST", "/api/dm-held", {"name": "ivy", "open": False}, ivy)[0], 200)
        # Nor can an agent be bound away from a line it holds.
        self.assertEqual(self.bearer("POST", "/api/dm-z1", {"name": "ivy", "open": True}, ivy)[0], 200)
        status, out = self.owner("POST", "/api/tokens", {"kind": "agent", "name": "ivy", "line": "z2", "replace": True})
        self.assertEqual(status, 409, out)
        self.assertIn("'ivy' holds z1", self.error(out))
        self.assertEqual(self.bearer("POST", "/api/dm-z1", {"name": "ivy", "open": False}, ivy)[0], 200)
        # Revoked, the binding goes with it.
        ident = next(t["id"] for t in json.loads(self.owner("GET", "/api/tokens")[1]) if t["name"] == "hal")
        self.assertEqual(self.owner("POST", f"/api/tokens/{ident}/revoke", {})[0], 200)
        self.assertEqual(self.bearer("POST", "/api/dm-hq", {"name": "ivy", "open": True}, ivy)[0], 200)
        self.assertEqual(self.bearer("POST", "/api/dm-hq", {"name": "ivy", "open": False}, ivy)[0], 200)

    def test_the_cli_binds_a_token_to_a_line(self):
        r = self.cli("token", "add", "kip", "--line", "kq")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bound to line kq", r.stdout)
        r = self.cli("token", "list")
        self.assertRegex(r.stdout, r"kip\s+agent on kq")
        self.assertIn("agent on line kq", self.call("GET", "/tokens")[1])
        r = self.cli("token", "add", "kip-events", "--events")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--events needs --line", r.stderr)

    def test_tokens_are_stored_only_as_hashes(self):
        made = self.issue("hashy")
        data = Path(self.tmp.name) / "data"
        stored = (data / "tokens.json").read_text()
        self.assertNotIn(made["token"], stored)
        self.assertNotIn(made["token"].split("_", 2)[2], stored)
        self.assertNotIn(made["token"].split("_", 2)[2], (data / "tokens-log.jsonl").read_text())
        self.assertIn(token_book.digest(made["token"]), stored)
        self.assertEqual(stat.S_IMODE((data / "tokens.json").stat().st_mode), 0o600)
        listed = self.owner("GET", "/api/tokens")[1]
        self.assertNotIn("sha256", listed)
        self.assertNotIn(made["token"], listed)
        self.assertNotIn(made["token"].split("_", 2)[2], self.call("GET", "/tokens")[1])

    def test_list_use_revoke_and_rotate(self):
        first = self.issue("rot")
        self.assertEqual(self.bearer("GET", "/api/voices", None, first["token"])[0], 200)
        listed = {t["id"]: t for t in json.loads(self.owner("GET", "/api/tokens")[1])}
        self.assertEqual(listed[first["record"]["id"]]["name"], "rot")
        self.assertIsNotNone(listed[first["record"]["id"]]["last_used"])
        second = self.issue("rot", replace=True)
        self.assertEqual(second["revoked"], [first["record"]["id"]])
        self.assertEqual(self.bearer("GET", "/api/voices", None, first["token"])[0], 401)
        self.assertEqual(self.bearer("GET", "/api/voices", None, second["token"])[0], 200)
        everything = {t["id"] for t in json.loads(self.owner("GET", "/api/tokens?revoked=1")[1])}
        self.assertLessEqual({first["record"]["id"], second["record"]["id"]}, everything)
        live = {t["id"] for t in json.loads(self.owner("GET", "/api/tokens")[1])}
        self.assertNotIn(first["record"]["id"], live)
        # Names are the same in any case: no second agent with an old one's name.
        status, out = self.owner("POST", "/api/tokens", {"kind": "agent", "name": "ROT"})
        self.assertEqual(status, 409, out)

    def test_the_page_lists_tokens_and_agents_without_one(self):
        made = self.issue("ghost")
        self.assertEqual(self.bearer("POST", "/api/dm-ghost", {"name": "ghost", "open": True}, made["token"])[0], 200)
        def without(page):
            return re.findall(r"No token yet for (.*?)\. Each asks for one", page)

        page = self.call("GET", "/tokens")[1]
        self.assertIn(f'data-act="revoke" data-id="{made["record"]["id"]}"', page)
        self.assertFalse([n for n in without(page) if "ghost" in n.lower()], without(page))
        self.owner("POST", f"/api/tokens/{made['record']['id']}/revoke", {})
        page = self.call("GET", "/tokens")[1]
        self.assertTrue([n for n in without(page) if "ghost" in n.lower()], page)
        self.assertNotIn(f'data-id="{made["record"]["id"]}"', page)
        self.assertIn('href="/tokens"', self.call("GET", "/settings")[1])

    def test_compare_is_constant_time(self):
        """Every check, known token or not, compares one digest with hmac.compare_digest."""
        with tempfile.TemporaryDirectory() as tmp:
            book = token_book.TokenBook(Path(tmp))
            token, record, _ = book.issue("agent", "timer")
            with mock.patch.object(token_book.hmac, "compare_digest", wraps=token_book.hmac.compare_digest) as cmp:
                book.check(token)
                self.assertEqual(cmp.call_count, 1)
                for bad in (BAD, token[:-1] + ("A" if token[-1] != "A" else "B"), "nt_" + record["id"] + "_x", "junk",
                            ""):
                    cmp.reset_mock()
                    with self.assertRaises(token_book.TokenError) as raised:
                        book.check(bad)
                    self.assertEqual(raised.exception.status, 401)
                    self.assertEqual(cmp.call_count, 1, bad)
                    # What is compared is a fixed-length digest, whatever was sent.
                    self.assertEqual({len(a) for call in cmp.call_args_list for a in call.args}, {64})
        from nanotea import server
        with mock.patch.object(server.hmac, "compare_digest", wraps=server.hmac.compare_digest) as cmp:
            self.assertTrue(server._same(self.key, self.key))
            self.assertFalse(server._same("x", self.key))
            self.assertEqual(cmp.call_count, 2)

    def test_the_cli_writes_a_private_token_file_and_programs_use_it(self):
        r = self.cli("token", "add", "cliagent")
        self.assertEqual(r.returncode, 0, r.stderr)
        path = Path(self.tmp.name) / "tokens" / "cliagent.token"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        secret = path.read_text().strip()
        self.assertRegex(secret, SECRET)
        self.assertNotIn(secret, r.stdout + r.stderr)
        again = self.cli("token", "add", "cliagent")
        self.assertEqual(again.returncode, 1)
        self.assertIn("nanotea token rotate cliagent", again.stderr)
        self.assertEqual(path.read_text().strip(), secret)
        # A program that names the agent finds the file.
        r = subprocess.run([sys.executable, "-m", "nanotea.tell", "--from", "cliagent", "--channels"], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        listed = self.cli("token", "list")
        self.assertIn("cliagent", listed.stdout)
        self.assertNotIn(secret, listed.stdout)
        # Rotating writes a new secret and refuses the old one.
        r = self.cli("token", "rotate", "cliagent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("revoked the old token", r.stdout)
        self.assertNotEqual(path.read_text().strip(), secret)
        self.assertEqual(self.bearer("GET", "/api/voices", None, secret)[0], 401)
        self.assertEqual(self.bearer("GET", "/api/voices", None, path.read_text().strip())[0], 200)
        ident = re.search(r"^(\w{8})\s+cliagent", self.cli("token", "list").stdout, re.M)[1]
        r = self.cli("token", "revoke", ident)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, "-m", "nanotea.tell", "--from", "cliagent", "--channels"], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("revoked", r.stderr)
        self.assertIn("asks for approval of a new one", r.stderr)

    def denied(self, args, name, kind="agent", env=None):
        """nanotea-tell without a token asks for one; denied, it fails saying so."""
        run = subprocess.Popen([sys.executable, "-m", "nanotea.tell", *args], env=env or self.env, cwd=self.tmp.name,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        (req,) = self.wait_for(lambda: [r for r in json.loads(self.owner("GET", "/api/enroll")[1])
                                        if r["name"] == name], f"{name}'s request")
        self.assertEqual(req["kind"], kind)
        self.assertEqual(self.owner("POST", f"/api/enroll/{req['id']}/deny", {})[0], 200)
        _, err = run.communicate(timeout=60)
        return run.returncode, err, req

    def test_a_program_with_no_token_or_a_loose_file_fails_with_the_fix(self):
        def tell(*args, env=None):
            return subprocess.run([sys.executable, "-m", "nanotea.tell", *args], env=env or self.env,
                                  capture_output=True, text=True, timeout=60)
        code, err, _ = self.denied(("--from", "untokened", "--channels"), "untokened")
        self.assertEqual(code, 1, err)
        self.assertIn("was denied by owner", err)
        self.token("loose")
        path = Path(self.tmp.name).resolve() / "tokens" / "loose.token"
        path.chmod(0o644)
        r = tell("--from", "loose", "--channels")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"chmod 600 {path}", r.stderr)
        path.chmod(0o600)
        path.write_text("not a token\n")
        r = tell("--from", "loose", "--channels")
        self.assertEqual(r.returncode, 1)
        self.assertIn("doesn't hold a token", r.stderr)
        # A named file that is missing is asked for, under the name, which has a token already: refused.
        r = tell("--from", "loose", "--channels", env={**self.env, "NANOTEA_TOKEN_FILE": str(path) + ".gone"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("'loose' already has a token", r.stderr)
        self.assertFalse(Path(str(path) + ".gone").exists())
        # Another agent's token under this name is refused by the service, naming both.
        r = tell("--from", "loose", "--ask", "x", env=self.env_for("untokened2"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("for 'untokened2', not 'loose'", r.stderr)

    def test_hooks_fail_without_a_token_and_mcp_asks_for_one(self):
        for args in (("hook", "stop", "--name", "notoken"), ("hook", "held", "--name", "notoken"),
                     ("hook", "rewake", "--name", "notoken", "--max-s", "1")):
            r = subprocess.run([sys.executable, "-m", "nanotea", *args], env=self.env, cwd=self.tmp.name,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("nanotea mcp asks for one", r.stderr)
            self.assertNotRegex(r.stderr, SECRET)
        # Its input closed, the server ends; the request was made, and kept to collect next time.
        r = subprocess.run([sys.executable, "-m", "nanotea", "mcp", "--name", "notoken"], env=self.env,
                           cwd=self.tmp.name, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("asked for a token for 'notoken'", r.stderr)
        self.assertTrue((Path(self.tmp.name) / "tokens" / "notoken.token.request").exists())
        (req,) = [q for q in json.loads(self.owner("GET", "/api/enroll")[1]) if q["name"] == "notoken"]
        self.assertEqual(self.owner("POST", f"/api/enroll/{req['id']}/deny", {})[0], 200)
        # Without a name or a token file, it waits for join to name it.
        r = subprocess.run([sys.executable, "-m", "nanotea", "mcp"], env=self.env, cwd=self.tmp.name,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_events_token_through_nanotea_tell(self):
        self.token("deploys", events_line="main")
        r = subprocess.run([sys.executable, "-m", "nanotea.tell", "--from", "deploys", "--event", "deployed",
                            "v2 is live"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        # It has no events token for this source, and an agent file isn't looked at instead: it asks for one, for
        # the line it reports to.
        self.token("deploys2")
        code, err, req = self.denied(("--from", "deploys2", "--event", "deployed", "v2 is live"), "deploys2",
                                     "events")
        self.assertEqual(code, 1, err)
        self.assertEqual(req["line"], "main")

    def test_setup_names_the_token_file_and_never_the_secret(self):
        self.token("setupper")
        path = Path(self.tmp.name).resolve() / "tokens" / "setupper.token"
        secret = path.read_text().strip()
        for harness in ("claude", "codex", "cursor", "gemini", "opencode", "goose", "vscode", "zed", "cline", "amp",
                        "other"):
            r = self.cli("setup", harness, "--name", "setupper")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(str(path), r.stdout, harness)
            self.assertIn("NANOTEA_TOKEN_FILE", r.stdout, harness)
            self.assertNotIn(secret, r.stdout, harness)
            self.assertNotIn("No token for", r.stdout, harness)
        r = self.cli("setup", "claude", "--name", "nottokened")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("asks for one when it first starts", r.stdout)

    def test_sop_uses_the_owners_key_and_says_when_it_has_none(self):
        r = self.cli("sop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nanotea", r.stdout)
        r = self.cli("sop", env={**self.env, "NANOTEA_KEY_FILE": str(Path(self.tmp.name) / "no-such-key")})
        self.assertEqual(r.returncode, 1)
        self.assertIn("no pairing key at", r.stderr)
        loose = Path(self.tmp.name) / "loose-key"
        loose.write_text(self.key)
        loose.chmod(0o644)
        r = self.cli("sop", env={**self.env, "NANOTEA_KEY_FILE": str(loose)})
        self.assertEqual(r.returncode, 1)
        self.assertIn("chmod 600", r.stderr)

    def test_zz_a_restart_names_the_agents_with_no_token(self):
        """An install from before tokens, whose agents have none: the service starts and says so, loudly."""
        made = self.issue("legacy")
        self.assertEqual(self.bearer("POST", "/api/dm-legacy", {"name": "legacy", "open": True}, made["token"])[0],
                         200)
        self.owner("POST", f"/api/tokens/{made['record']['id']}/revoke", {})
        stop(self.proc, self.reader)
        cls = type(self)
        cls.proc, cls.log, cls.reader = spawn(self.env, self.tmp.name)
        self.wait_for(lambda: any("listening on" in line for line in self.log), "the service to start")
        lines = list(self.log)
        log = "".join(lines)
        error = next((ln for ln in lines if "ERROR" in ln and "legacy" in ln), None)
        self.assertIsNotNone(error, log)
        for words in ("have no token", "asks for one when its nanotea mcp next starts", "the owner approves it"):
            self.assertIn(words, error)
        self.assertEqual(self.bearer("GET", "/api/voices", None, made["token"])[0], 401)
        self.assertEqual(self.bearer("GET", "/api/voices", None, self.token("tester"))[0], 200)


class Migration(unittest.TestCase):
    """The service from before tokens: data/ holds agents' work and no tokens.json."""

    def test_a_service_with_agents_and_no_token_book_starts_and_says_what_to_run(self):
        import shutil
        from harness import BASE_PORT, CONFIG
        port = BASE_PORT + 10
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "env").write_text("X=1\n")
            data = tmp / "data"
            data.mkdir()
            config = tmp / "config.toml"
            config.write_text(CONFIG.format(port=port, data=data, env=tmp / "env", py=sys.executable, root=ROOT,
                                            tests=ROOT / "tests", notes=tmp / "notes.jsonl", extra=""))
            env = {**os.environ, "NANOTEA_CONFIG": str(config), "TMPDIR": str(tmp)}
            # First run, as the old version left it: an agent known from its voice.
            first = subprocess.Popen([sys.executable, "-m", "nanotea", "serve"], cwd=tmp, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    try:
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                        conn.request("GET", "/api/voices")
                        conn.getresponse().read()
                        break
                    except OSError:
                        time.sleep(0.1)
                key = (data / "reply_key").read_text().strip()
                made = subprocess.run([sys.executable, "-m", "nanotea", "token", "add", "oldtimer"], cwd=tmp, env=env,
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(made.returncode, 0, made.stderr)
                token = (tmp / "tokens" / "oldtimer.token").read_text().strip()
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("POST", "/api/dm-oldtimer", json.dumps({"name": "oldtimer", "open": True}),
                             {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                self.assertEqual(conn.getresponse().status, 200)
                self.assertTrue(key)
            finally:
                first.terminate()
                first.wait(timeout=10)
            for gone in ("tokens.json", "tokens-log.jsonl"):
                (data / gone).unlink()
            shutil.rmtree(tmp / "tokens")
            second, lines, reader = spawn(env, tmp)
            try:
                deadline = time.time() + 20
                while time.time() < deadline and not any("listening on" in line for line in lines):
                    time.sleep(0.1)
                error = next((ln for ln in lines if "ERROR" in ln), "")
                self.assertIn("oldtimer", error, "".join(lines))
                self.assertIn("asks for one when its nanotea mcp next starts", error)
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/api/voices", None, {"Authorization": f"Bearer {token}"})
                r = conn.getresponse()
                body = json.loads(r.read())
                self.assertEqual(r.status, 401)
                self.assertIn("asks for approval of a new one", body["error"])
            finally:
                stop(second, reader)


if __name__ == "__main__":
    unittest.main()
