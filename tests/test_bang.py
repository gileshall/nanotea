"""Bang commands: the owner's !command runs in the agent's own session, and what it did comes back."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from harness import Case
from mcp import Client
from mcp.client.stdio import StdioServerParameters

CAP = 1024  # [bang] output_kb below, in bytes


class Bang(Case):
    PORT_OFFSET = 11
    EXTRA = "\n[bang]\ntimeout_s = 2\nexpire_s = 5\noutput_kb = 1\nkeep_mb = 1\n"

    def setUp(self):
        self.listeners = []
        self.sessions = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for proc in self.listeners:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()
        for sid, name in self.sessions:
            self.local("POST", f"/api/sessions/{sid}/close", {}, as_=name)
        self.setting(bang=False, one_session=True)

    def setting(self, **patch):
        """Changes settings as the owner; bang on goes with the host key, as nanotea bang on sends it."""
        if patch.get("bang"):
            status, body = self.bearer("POST", "/api/settings", patch, self.host_key())
        else:
            status, body = self.call("POST", "/api/settings", patch)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def host_key(self):
        return (Path(self.tmp.name) / "data" / "host_key").read_text().strip()

    def audit(self):
        path = Path(self.tmp.name) / "data" / "bang-log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def listener(self, name, bang=True):
        """nanotea-tell --listen on name's line (its slug), with the opt-in or without."""
        line = name.lower()
        self.token(name)
        proc = subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", name, "--listen", "--inbox", line,
                                 *(["--bang"] if bang else [])], env=self.env, cwd=self.tmp.name,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.listeners.append(proc)

        def up():
            if proc.poll() is not None:
                self.fail(f"the listener for {name} exited: {proc.stderr.read()}")
            if bang:
                return self.ready(name)
            return self.call("GET", f"/chat/{line}")[0] == 200

        self.wait_for(up, f"{name}'s listener")
        return proc

    def end(self, proc, name):
        """Writes to name's line so its listener returns, and gives what it printed."""
        status, body = self.call("POST", f"/api/dm-{name.lower()}/messages", {"text": "that is all"})
        self.assertEqual(status, 200, body)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        return out

    def bang(self, name, command):
        return self.call("POST", f"/api/dm-{name.lower()}/messages", {"text": command})

    def ready(self, name):
        """name's bang session is registered and holds its line. A command needs both, and a session registers
        before it claims the line."""
        if name not in json.loads(self.call("GET", "/api/settings")[1])["bang_sessions"]:
            return False
        status, body = self.owner("GET", f"/api/dm-{name.lower()}")
        return status == 200 and json.loads(body)["holder"] == name

    def sent(self, name, command):
        status, body = self.bang(name, command)
        self.assertEqual(status, 200, body)
        return json.loads(body)["bang"]

    def state(self, name, msg_id, *states):
        """The page's view of a command, once it is in one of states."""
        seen = []

        def look():
            for m in json.loads(self.local("GET", f"/api/dm-{name.lower()}/history?name={name}&n=50")[1]):
                if m["id"] == msg_id:
                    seen[:] = [m["summary"]]
                    if re.search(rf"\(({'|'.join(states)})\b", m["summary"]):
                        return m
        deadline = time.time() + 20
        while time.time() < deadline:
            if got := look():
                return got
            time.sleep(0.1)
        self.fail(f"timed out waiting for {msg_id} to be {' or '.join(states)}; last seen: {seen}")

    def hint(self, name):
        """Whom the composer says a ! message will run for: "" when it says nothing."""
        return re.search(r'data-bang="([^"]*)"', self.page(name))[1]

    def page(self, name):
        status, html = self.call("GET", f"/chat/{name.lower()}")
        self.assertEqual(status, 200)
        return html

    def refused(self, name, command, status, *words):
        got, body = self.bang(name, command)
        self.assertEqual(got, status, body)
        message = json.loads(body)["error"]
        for w in words:
            self.assertIn(w, message)
        return message

    def register(self, name, bang):
        sid = os.urandom(4).hex()
        status, body = self.local("POST", "/api/sessions", {"name": name, "session": sid, "pid": os.getpid(),
                                                            "line": name.lower(), "bang": bang})
        self.assertEqual(status, 200, body)
        self.sessions.append((sid, name))
        self.assertEqual(self.local("POST", f"/api/dm-{name.lower()}", {"name": name, "open": True})[0], 200)
        return sid

    def test_off_by_default_and_ordinary(self):
        self.assertFalse(json.loads(self.call("GET", "/api/settings")[1])["settings"]["bang"])
        proc = self.listener("Ada", bang=False)
        # Delivered as an ordinary message, which returns the listener as any message does.
        self.assertEqual(self.bang("Ada", "!echo nope")[0], 200)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("!echo nope", out)
        self.assertNotIn("[bang", out)
        self.assertEqual(self.hint("Ada"), "")
        self.assertEqual([a for a in self.audit() if a.get("agent") == "Ada"], [])

    def test_agreement_and_tools_follow_the_switch(self):
        def sop(*args):
            r = subprocess.run([sys.executable, "-m", "nanotea", "sop", *args], env=self.env, cwd=self.tmp.name,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

        async def instructions(*args):
            server = StdioServerParameters(command=sys.executable, args=["-m", "nanotea", "mcp", *args],
                                           env=self.env_for("Cy"), cwd=self.tmp.name)
            async with Client(server, mode="legacy") as c:
                return c.instructions

        self.assertNotIn('"bang"', sop("--bang"))
        self.assertNotIn('"bang"', asyncio.run(instructions("--name", "Cy", "--bang")))
        self.setting(bang=True)
        self.assertIn('"bang"', sop("--bang"))
        self.assertNotIn('"bang"', sop())
        self.assertIn('"bang"', asyncio.run(instructions("--name", "Cy", "--bang")))
        self.assertNotIn('"bang"', asyncio.run(instructions("--name", "Cy")))

    def test_settings_show_the_cost_and_the_opted_in_sessions(self):
        view = json.loads(self.call("GET", "/api/settings")[1])
        self.assertGreater(view["costs"]["by"]["bang"], 0)
        page = self.call("GET", "/settings")[1]
        self.assertIn("Bang commands", page)
        self.assertIn("--bang", page)
        self.assertIn("tokens", page)
        self.assertEqual(self.call("POST", "/api/settings", {"bang": True}, paired=False)[0], 401)

    def test_only_the_host_key_turns_it_on_and_anyone_paired_turns_it_off(self):
        for status, body in (self.call("POST", "/api/settings", {"bang": True}),
                             self.owner("POST", "/api/settings", {"bang": True})):
            self.assertEqual(status, 403, body)
            self.assertIn("nanotea bang on", json.loads(body)["error"])
        self.assertFalse(json.loads(self.call("GET", "/api/settings")[1])["settings"]["bang"])
        self.assertRegex(self.call("GET", "/settings")[1], r'<input disabled type="checkbox" name="bang"')
        # The host key is a bearer secret only: not a pairing link, not a page.
        host = self.host_key()
        as_link, wrong = self.call("GET", f"/?k={host}", paired=False), self.call("GET", "/?k=wrong", paired=False)
        self.assertEqual(as_link[0], wrong[0])
        self.assertIn("Not paired", as_link[1])
        self.assertNotIn(host, self.call("GET", "/settings")[1])
        r = self.cli("bang", "on")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bang commands are on", r.stdout)
        self.assertNotRegex(self.call("GET", "/settings")[1], r'<input disabled type="checkbox" name="bang"')
        # The pairing key can't say on even while it is on (it would race the owner turning it off); it can say off.
        self.assertEqual(self.call("POST", "/api/settings", {"bang": True})[0], 403)
        self.assertEqual(self.call("POST", "/api/settings", {"bang": False})[0], 200)
        r = self.cli("bang", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bang commands are off", r.stdout)
        switched = [(a["event"], a["via"]) for a in self.audit() if a["event"].startswith("switched")][-2:]
        self.assertEqual(switched, [("switched on", "the host key"), ("switched off", "the pairing key")])

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "nanotea", *args], env=self.env, cwd=self.tmp.name,
                              capture_output=True, text=True, timeout=60)

    def test_runs_and_both_sides_see_it(self):
        self.setting(bang=True)
        proc = self.listener("Bea")
        ok = self.sent("Bea", "!echo hello; pwd")
        bad = self.sent("Bea", "!sh -c 'echo out; echo err >&2; exit 3'")
        self.state("Bea", ok, "done")
        self.state("Bea", bad, "done")

        # The owner: the command in the thread, then its result in place.
        html = self.page("Bea")
        self.assertIn('<pre class="cmd">echo hello; pwd</pre>', html)
        cwd = os.path.realpath(self.tmp.name)
        self.assertRegex(html, rf"Exit 0 in [0-9.]+ s, in {re.escape(cwd)}")
        self.assertRegex(html, r"Exit 3 in")
        self.assertIn("hello", html)
        self.assertIn("err", html)

        # A result alone doesn't wake an idle agent.
        time.sleep(4)
        self.assertIsNone(proc.poll())
        w = json.loads(self.local("GET", "/api/agents/Bea/waiting?line=dm-bea")[1])
        self.assertEqual((w["total"], w["ids"], w["bang"]), (0, [], 2))

        out = self.end(proc, "Bea")
        self.assertRegex(out, rf"\[bang from Robin, [^\]]*, id {ok}, exit 0, [0-9.]+ s, cwd {re.escape(cwd)}\] "
                              r"\$ echo hello; pwd\n")
        self.assertIn(f"[bang stdout]\nhello\n{cwd}\n", out)
        self.assertRegex(out, rf"id {bad}, exit 3, [0-9.]+ s, cwd {re.escape(cwd)}\] \$ sh -c 'echo out; echo err >&2; "
                              r"exit 3'")
        self.assertIn("[bang stdout]\nout\n[bang stderr]\nerr\n", out)
        self.assertLess(out.index(f"id {ok}"), out.index(f"id {bad}"))
        self.assertLess(out.index(f"id {bad}"), out.index("that is all"))
        self.assertIn("a command the owner ran with !, not you", out)

        log = self.audit()
        for msg_id, code in ((ok, 0), (bad, 3)):
            done = next(a for a in log if a.get("id") == msg_id and a["event"] == "done")
            self.assertEqual((done["exit"], done["timed_out"], done["cwd"], done["by"], done["box"]),
                             (code, False, cwd, "owner", "bea"))
            started = next(a for a in log if a.get("id") == msg_id and a["event"] == "started")
            self.assertEqual((started["agent"], started["pid"] > 0, bool(started["session"])), ("Bea", True, True))
        queued = [a for a in log if a["event"] == "queued" and a["agent"] == "Bea"]
        self.assertEqual([a["command"] for a in queued], ["echo hello; pwd", "sh -c 'echo out; echo err >&2; exit 3'"])

    def test_escape_sends_a_literal_bang(self):
        self.setting(bang=True)
        proc = self.listener("Dot", bang=False)
        self.assertEqual(self.bang("Dot", "\\!important")[0], 200)
        out = self.end(proc, "Dot")
        self.assertIn("!important", out)
        self.assertNotIn("\\!important", out)
        self.assertNotIn("[bang", out)

    def test_composer_says_a_bang_message_will_run(self):
        self.assertEqual(self.local("POST", "/api/dm-eli", {"name": "Eli", "open": True})[0], 200)
        self.assertEqual(self.hint("Eli"), "")
        self.setting(bang=True)
        self.assertEqual(self.hint("Eli"), "Eli")
        self.assertIn("bang-hint", self.page("Eli"))
        self.assertEqual(re.search(r'data-bang="([^"]*)"', self.call("GET", "/chat")[1])[1], "")

    def test_timeout_kills_the_group_and_reports_what_came(self):
        self.setting(bang=True)
        proc = self.listener("Fay")
        marker = Path(self.tmp.name) / "fay-child-survived"
        msg_id = self.sent("Fay", f"!echo partial; (sleep 4; touch {marker}) & sleep 30")
        started = time.monotonic()
        self.state("Fay", msg_id, "done")
        self.assertLess(time.monotonic() - started, 12)
        self.assertIn("Timed out after 2 s and was killed", self.page("Fay"))
        out = self.end(proc, "Fay")
        self.assertRegex(out, rf"id {msg_id}, exit 143 \(signal SIGTERM\), timed out, ")
        self.assertIn("[bang stdout]\npartial\n", out)
        time.sleep(4)
        self.assertFalse(marker.exists(), "a background child outlived the timeout")
        done = next(a for a in self.audit() if a.get("id") == msg_id and a["event"] == "done")
        self.assertTrue(done["timed_out"])

    def test_big_output_is_capped_and_kept_whole(self):
        self.setting(bang=True)
        proc = self.listener("Gus")
        msg_id = self.sent("Gus", "!head -c 5000 /dev/zero | tr '\\0' x; head -c 3000 /dev/zero | tr '\\0' y >&2")
        self.state("Gus", msg_id, "done")
        html = self.page("Gus")
        self.assertIn(f'href="/bang/dm-gus/{msg_id}/stdout">Whole stdout', html)
        self.assertIn("Showing 1,024 bytes of 5,000 bytes (first and last half)", html)
        self.assertIn("Showing 1,024 bytes of 3,000 bytes (first and last half)", html)
        out = self.end(proc, "Gus")
        full = re.search(r"^\[bang full output\] (.+)$", out, re.M)[1]
        self.assertEqual(Path(full, "stdout").read_bytes(), b"x" * 5000)
        self.assertEqual(Path(full, "stderr").read_bytes(), b"y" * 3000)
        self.assertTrue(Path(full).is_relative_to(Path(self.tmp.name) / "data"))
        shown = re.search(r"^\[bang stdout\]\n(.*?)\n\[bang stderr\]\n(.*?)\n\[bang stdout cut\]", out, re.M | re.S)
        self.assertIsNotNone(shown, out)
        self.assertIn("[... 3976 bytes cut ...]", shown[1])
        self.assertLessEqual(len(shown[1]), CAP + 40)
        self.assertRegex(out, r"\[bang stdout cut\] 5000 bytes in all; showing 1024 \(first and last half\); "
                              r"3976 bytes are not shown here")
        self.assertRegex(out, r"\[bang stderr cut\] 3000 bytes in all; showing 1024 \(first and last half\); "
                              r"1976 bytes are not shown here")
        raw = self.call("GET", f"/bang/dm-gus/{msg_id}/stdout")
        self.assertEqual((raw[0], raw[1]), (200, "x" * 5000))

    def test_refusals_are_explained_and_logged(self):
        self.setting(bang=True)
        before = len(self.audit())

        # A line with no session at all.
        self.assertEqual(self.local("POST", "/api/dm-hub", {"name": "Hub", "open": True})[0], 200)
        self.refused("Hub", "!ls", 409, "no session is open", "--bang")
        # A session without the local opt-in.
        proc = self.listener("Ivy", bang=False)
        self.register("Ivy", bang=False)
        self.refused("Ivy", "!ls", 403, "wasn't started with --bang")
        # A reply isn't a command.
        self.assertEqual(self.bang("Ivy", "hello")[0], 200)
        first = self.wait_for(lambda: json.loads(self.local("GET", "/api/dm-ivy/history?name=Ivy&n=5")[1]),
                              "the history")[-1]["id"]
        got = self.call("POST", "/api/dm-ivy/messages", {"text": "!ls", "re": first})
        self.assertEqual(got[0], 400, got)
        self.assertIn("no recording, file or reply", json.loads(got[1])["error"])
        self.end(proc, "Ivy")
        # Channels and the main inbox.
        self.assertEqual(self.call("POST", "/api/channels/general/messages", {"text": "!ls"})[0], 400)
        self.assertIn("not in #general", json.loads(self.call("POST", "/api/channels/general/messages",
                                                              {"text": "!ls"})[1])["error"])
        got = self.call("POST", "/api/inbox/messages", {"text": "!ls"})
        self.assertEqual(got[0], 400)
        self.assertIn("not the main inbox", json.loads(got[1])["error"])
        # Nothing after the bang.
        self.refused("Hub", "!  ", 400, "nothing after the !")
        # Two sessions on one line.
        self.setting(one_session=False)
        self.register("Jax", bang=True)
        self.register("Jax", bang=True)
        self.refused("Jax", "!ls", 409, "2 sessions are open", "exactly one")
        self.assertEqual(self.call("POST", "/api/dm-hub/messages", {"text": "!ls"}, paired=False)[0], 401)

        refused = [a for a in self.audit()[before:] if a["event"] == "refused"]
        self.assertEqual(len(refused), 8)
        self.assertTrue(all(a["by"] == "owner" and a["command"] is not None for a in refused))
        self.assertTrue(any(a["box"] == "#general" for a in refused))
        self.assertFalse([a for a in self.audit()[before:] if a["event"] in ("queued", "started")])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_recording_is_never_a_command(self):
        self.setting(bang=True)
        proc = self.listener("Lou")
        wav = Path(self.tmp.name, "lou.wav")
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=0.25", "-ac", "1", str(wav)], check=True)
        import http.client
        from harness import COOKIE, PROXY
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest("POST", "/api/dm-lou/drafts?name=lou.wav")
        for k, v in {**PROXY, "Cookie": f"{COOKIE}={self.key}", "Content-Type": "audio/wav",
                     "Content-Length": str(wav.stat().st_size)}.items():
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(wav.read_bytes())
        r = conn.getresponse()
        draft = json.loads(r.read())["draft"]
        conn.close()
        status, body = self.call("POST", "/api/dm-lou/messages", {"text": "!echo spoken", "drafts": [draft]})
        self.assertEqual(status, 400, body)
        self.assertIn("no recording, file or reply", json.loads(body)["error"])
        status, body = self.call("POST", "/api/dm-lou/messages", {"text": None, "drafts": [draft]})
        self.assertEqual(status, 200, body)
        out, _ = proc.communicate(timeout=30)
        self.assertIn("[voice, transcribed] transcript", out)
        self.assertNotIn("[bang", out)
        self.assertEqual([a for a in self.audit() if a.get("agent") == "Lou" and a["event"] != "refused"], [])

    def test_only_the_agents_own_token_reaches_its_commands(self):
        self.setting(bang=True)
        proc = self.listener("Max")
        nan = self.register("Nan", bang=True)
        msg_id = self.sent("Max", "!sleep 1; echo mine")
        poll = "/api/agents/Max/bang?session={}&wait=0"
        # Another agent's token, no token, the owner's key and the owner's cookie are all refused on Max's routes.
        self.assertEqual(self.local("GET", poll.format(nan), as_="Nan")[0], 403)
        self.assertEqual(self.request("GET", poll.format(nan))[0], 401)
        self.assertEqual(self.owner("GET", poll.format(nan))[0], 403)
        self.assertEqual(self.call("GET", poll.format(nan))[0], 403)
        # Max's own token can't take on Nan's session.
        self.assertEqual(self.local("GET", poll.format(nan), as_="Max")[0], 404)
        # Nobody else can report what it ran.
        report = {"session": nan, "exit": 0}
        self.assertEqual(self.local("POST", f"/api/agents/Max/bang/{msg_id}", report, as_="Nan")[0], 403)
        self.assertEqual(self.request("POST", f"/api/agents/Max/bang/{msg_id}", report)[0], 401)
        self.assertEqual(self.owner("POST", f"/api/agents/Max/bang/{msg_id}", report)[0], 403)
        self.assertEqual(self.call("POST", f"/api/agents/Max/bang/{msg_id}", report)[0], 403)
        # And Max's own token can't report it from a session it wasn't handed to.
        status, out = self.local("POST", f"/api/agents/Max/bang/{msg_id}", report, as_="Max")
        self.assertEqual(status, 403, out)
        self.assertIn("another session", json.loads(out)["error"])
        # Nan's own session asks for its own commands and finds none.
        self.assertEqual(json.loads(self.local("GET", f"/api/agents/Nan/bang?session={nan}&wait=0", as_="Nan")[1]),
                         {"job": None})
        # None of that disturbed the command.
        self.state("Max", msg_id, "done")
        self.assertIn("mine", self.end(proc, "Max"))
        # A reported command can't be reported again.
        status, out = self.local("POST", f"/api/agents/Max/bang/{msg_id}", report, as_="Max")
        self.assertEqual(status, 409, out)

    def test_a_command_runs_only_in_the_holders_session(self):
        self.setting(bang=True)
        self.assertEqual(self.local("POST", "/api/dm-pia", {"name": "Pia", "open": True})[0], 200)
        sid = os.urandom(4).hex()
        status, body = self.local("POST", "/api/sessions", {"name": "Quo", "session": sid, "pid": os.getpid(),
                                                            "line": "pia", "bang": True})
        self.assertEqual(status, 200, body)
        self.sessions.append((sid, "Quo"))
        self.refused("Pia", "!echo for pia", 409, "'Quo'", "Pia holds the line", "holder's own session")
        self.assertEqual(json.loads(self.local("GET", f"/api/agents/Quo/bang?session={sid}&wait=0")[1]),
                         {"job": None})

    def test_another_agents_session_on_the_line_neither_runs_nor_blocks_a_command(self):
        self.setting(bang=True)
        sid = self.register("Sam", bang=True)
        tom = os.urandom(4).hex()
        status, body = self.local("POST", "/api/sessions", {"name": "Tom", "session": tom, "pid": os.getpid(),
                                                            "line": "sam", "bang": True})
        self.assertEqual(status, 200, body)
        self.sessions.append((tom, "Tom"))
        msg_id = self.sent("Sam", "!true")
        self.assertEqual(json.loads(self.local("GET", f"/api/agents/Tom/bang?session={tom}&wait=0")[1]),
                         {"job": None})
        job = json.loads(self.local("GET", f"/api/agents/Sam/bang?session={sid}&wait=5")[1])["job"]
        self.assertEqual(job["id"], msg_id)
        # Nor can Tom take over Sam's session by its id.
        status, body = self.local("POST", "/api/sessions", {"name": "Tom", "session": sid, "pid": os.getpid(),
                                                            "line": "sam", "bang": True})
        self.assertEqual(status, 403, body)
        self.assertIn("another agent's", json.loads(body)["error"])

    def test_a_command_whose_line_changes_hands_before_it_runs_expires(self):
        self.setting(bang=True)
        sid = self.register("Ria", bang=True)
        msg_id = self.sent("Ria", "!echo late")
        self.assertEqual(self.local("POST", "/api/dm-ria", {"name": "Ria", "open": False})[0], 200)
        self.assertEqual(json.loads(self.local("GET", f"/api/agents/Ria/bang?session={sid}&wait=0")[1]),
                         {"job": None})
        self.assertIn("not run: nobody holds the line now, not Ria", self.page("Ria"))
        self.assertEqual(next(a for a in self.audit() if a.get("id") == msg_id and a["event"] != "queued")["event"],
                         "expired")

    def test_a_sessions_failure_reason_is_text_on_the_page(self):
        self.setting(bang=True)
        sid = self.register("Xan", bang=True)
        msg_id = self.sent("Xan", "!true")
        job = json.loads(self.local("GET", f"/api/agents/Xan/bang?session={sid}&wait=5", as_="Xan")[1])["job"]
        self.assertEqual(job["id"], msg_id)
        mark = '<img src=x onerror="alert(1)">'
        status, out = self.local("POST", f"/api/agents/Xan/bang/{msg_id}", {"session": sid, "error": mark}, as_="Xan")
        self.assertEqual(status, 200, out)
        html = self.page("Xan")
        self.assertNotIn(mark, html)
        self.assertIn("couldn&#x27;t start it: &lt;img src=x onerror=&quot;alert(1)&quot;&gt;", html)

    def test_a_command_nobody_picks_up_expires_and_never_runs_later(self):
        self.setting(bang=True)
        sid = self.register("Oto", bang=True)
        marker = Path(self.tmp.name) / "oto-ran"
        msg_id = self.sent("Oto", f"!touch {marker}")
        self.assertIn("Waiting for the session to pick it up", self.page("Oto"))
        self.state("Oto", msg_id, "expired")
        self.assertIn("not run: no session picked it up within 5 s", self.page("Oto"))
        # It is gone for good: asking now finds nothing.
        status, out = self.local("GET", f"/api/agents/Oto/bang?session={sid}&wait=0", as_="Oto")
        self.assertEqual((status, json.loads(out)), (200, {"job": None}))
        time.sleep(1)
        self.assertFalse(marker.exists())
        gone = next(a for a in self.audit() if a.get("id") == msg_id and a["event"] == "expired")
        self.assertIn("no session picked it up", gone["reason"])
        # And it never reaches the agent.
        self.assertEqual(json.loads(self.local("GET", "/api/dm-oto/pending?name=Oto")[1]), [])

    def test_turning_the_setting_off_expires_what_waits(self):
        self.setting(bang=True)
        self.register("Pru", bang=True)
        msg_id = self.sent("Pru", "!echo late")
        self.setting(bang=False)
        self.state("Pru", msg_id, "expired")
        self.assertIn("turned off in Settings", self.page("Pru"))

    def test_mcp_session_runs_commands_and_checks_them_in(self):
        self.setting(bang=True)
        cwd = os.path.realpath(self.tmp.name)

        async def owner(*args):
            return await asyncio.to_thread(self.call, *args)

        async def session():
            server = StdioServerParameters(command=sys.executable, cwd=self.tmp.name, env=self.env_for("Quin"),
                                           args=["-m", "nanotea", "mcp", "--name", "Quin", "--bang"])
            async with Client(server, mode="legacy") as c:
                self.assertIn('kind "bang"', c.instructions)
                await asyncio.to_thread(self.wait_for, lambda: self.ready("Quin"), "Quin's session on its line")
                status, body = await owner("POST", "/api/dm-quin/messages", {"text": "!echo from mcp; echo e >&2"})
                self.assertEqual(status, 200, body)
                msg_id = json.loads(body)["bang"]
                for _ in range(200):
                    r = await c.call_tool("check", {})
                    self.assertFalse(r.is_error, r.content)
                    if items := r.structured_content["items"]:
                        break
                    await asyncio.sleep(0.1)
                self.assertEqual(len(items), 1, items)
                item = items[0]
                self.assertEqual({k: item[k] for k in ("kind", "from", "id", "command", "cwd", "exit", "stdout",
                                                       "stderr", "truncated", "timed_out")},
                                 {"kind": "bang", "from": "owner", "id": msg_id, "command": "echo from mcp; echo e >&2",
                                  "cwd": cwd, "exit": 0, "stdout": "from mcp\n", "stderr": "e\n",
                                  "truncated": None, "timed_out": False})
                self.assertIn("not you", item["note"])
                self.assertEqual(Path(item["full_output"], "stdout").read_text(), "from mcp\n")
                self.assertEqual(r.structured_content["waiting"], 0)


        asyncio.run(session())
        # Closing the session took it off the line.
        self.assertNotIn("Quin", json.loads(self.call("GET", "/api/settings")[1])["bang_sessions"])

    def test_mcp_wait_isnt_ended_by_a_result(self):
        self.setting(bang=True)

        async def owner(*args):
            return await asyncio.to_thread(self.call, *args)

        async def session():
            server = StdioServerParameters(command=sys.executable, cwd=self.tmp.name, env=self.env_for("Rae"),
                                           args=["-m", "nanotea", "mcp", "--name", "Rae", "--bang"])
            async with Client(server, mode="legacy") as c:
                await asyncio.to_thread(self.wait_for, lambda: self.ready("Rae"), "Rae's session on its line")
                status, body = await owner("POST", "/api/dm-rae/messages", {"text": "!echo while idle"})
                self.assertEqual(status, 200, body)
                started = time.monotonic()
                r = await c.call_tool("wait", {"timeout_s": 6})
                self.assertFalse(r.is_error, r.content)
                return time.monotonic() - started, r.structured_content

        took, out = asyncio.run(session())
        self.assertGreaterEqual(took, 5.5)
        self.assertEqual([i["kind"] for i in out["items"]], ["bang"])
        self.assertEqual(out["items"][0]["stdout"], "while idle\n")

    def test_mcp_without_the_opt_in_runs_nothing(self):
        self.setting(bang=True)

        async def session():
            server = StdioServerParameters(command=sys.executable, cwd=self.tmp.name, env=self.env_for("Sol"),
                                           args=["-m", "nanotea", "mcp", "--name", "Sol"])
            async with Client(server, mode="legacy") as c:
                self.assertNotIn('"bang"', c.instructions)
                status, body = await asyncio.to_thread(self.bang, "Sol", "!echo no")
                self.assertEqual(status, 403, body)
                self.assertIn("wasn't started with --bang", json.loads(body)["error"])

        asyncio.run(session())

    def test_setup_puts_the_opt_in_in_the_config_it_prints(self):
        r = subprocess.run([sys.executable, "-m", "nanotea", "setup", "claude", "--name", "Tal", "--bang"],
                           env=self.env, cwd=self.tmp.name, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"--bang"', r.stdout)
        self.assertIn("--bang is in the arguments", r.stderr)
        plain = subprocess.run([sys.executable, "-m", "nanotea", "setup", "claude", "--name", "Tal"], env=self.env,
                               cwd=self.tmp.name, capture_output=True, text=True, timeout=60)
        self.assertNotIn("--bang", plain.stdout)

    def test_a_command_running_when_the_session_ends_still_reports(self):
        self.setting(bang=True)

        async def session():
            server = StdioServerParameters(command=sys.executable, cwd=self.tmp.name, env=self.env_for("Tim"),
                                           args=["-m", "nanotea", "mcp", "--name", "Tim", "--bang"])
            async with Client(server, mode="legacy"):
                await asyncio.to_thread(self.wait_for, lambda: self.ready("Tim"), "Tim's session on its line")
                status, body = await asyncio.to_thread(self.bang, "Tim", "!sleep 1.5; echo finished")
                self.assertEqual(status, 200, body)
                msg_id = json.loads(body)["bang"]
                await asyncio.to_thread(self.state, "Tim", msg_id, "running")
                return msg_id

        msg_id = asyncio.run(session())
        self.state("Tim", msg_id, "done")
        self.assertIn("finished", self.page("Tim"))
        self.assertNotIn("Tim", json.loads(self.call("GET", "/api/settings")[1])["bang_sessions"])
        self.refused("Tim", "!ls", 409, "no session is open")

    def test_listen_bang_needs_an_agents_own_line(self):
        self.token("Una")
        for extra in ([], ["--inbox", "main"], ["--channel", "general"]):
            r = subprocess.run([sys.executable, "-m", "nanotea.tell", "--from", "Una", "--listen", "--bang", *extra],
                               env=self.env, cwd=self.tmp.name, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("--bang goes with --listen --inbox NAME", r.stderr)
        r = subprocess.run([sys.executable, "-m", "nanotea.tell", "--from", "Una", "--bang", "hello"], env=self.env,
                           cwd=self.tmp.name, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)

    def test_bad_config_stops_the_service(self):
        from nanotea import bang
        from nanotea.config import ConfigError
        for table, words in (({"nope": 1}, "[bang] nope: unknown"), ({"timeout_s": 0}, "[bang] timeout_s must be"),
                             ({"output_kb": "big"}, "[bang] output_kb must be")):
            with self.assertRaises(ConfigError) as ctx:
                bang.settings({"bang": table})
            self.assertIn(words, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
