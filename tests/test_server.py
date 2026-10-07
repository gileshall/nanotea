"""End to end: a real service on a temp data dir, driven through the CLI and the proxied HTTP API."""

import asyncio
import base64
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from urllib.parse import quote

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from nanotea.settings import DEFAULTS, EVENT_SETTLE_S, session_total
from harness import Tokens, spawn, stop

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("NANOTEA_TEST_PORT", "17447"))
COOKIE = "nanotea_key"
PROXY = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}
CONFIG = """
host = "127.0.0.1"
port = {port}
public_url = "https://nanotea.test"
trusted_proxies = ["127.0.0.1"]
data_dir = "{data}"
env_file = "{env}"

[app]
name = "Teapot"
owner = "Robin"
channels = {{ general = "Announcements" }}
groups = [{{ name = "Builders", members = ["builder"] }}]

[control]
use = ["choice", "checklist"]

[rewrite]
backend = "identity"

[tts]
backend = "command"

[tts.command]
argv = ["{py}", "{root}/tests/fake_tts.py", "{{text_file}}", "{{out}}"]
ext = "m4a"
voices = ["v1", "v2", "v3", "v4", "v5", "v6", "v7"]
timeout_s = 60

[stt]
backend = "command"

[stt.command]
argv = ["{py}", "-c", "print('transcript')", "{{audio_file}}"]
timeout_s = 60

[notify]
now = []
escalate = []
escalate_after_min = 30

[notify.push]
subject = "https://nanotea.test"

[audio]
max_upload_mb = 5
"""


class Service(Tokens, unittest.TestCase):
    port = PORT
    # Agents the programs the tests run name with --from or --name; each has a token file where they look.
    AGENTS = ("bob", "builder", "Gus", "Frank", "Erin", "Kim", "Max", "taker", "alice", "Tia", "Ola", "Nobody",
              "tester")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / "env").write_text("X=1\n")
        cls.config = tmp / "config.toml"
        cls.config.write_text(CONFIG.format(port=PORT, data=tmp / "data", env=tmp / "env", py=sys.executable, root=ROOT))
        cls.env = {**os.environ, "NANOTEA_CONFIG": str(cls.config), "TMPDIR": str(tmp)}
        cls.proc, cls.log, cls.reader = spawn(cls.env, tmp)
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1)
                conn.request("GET", "/api/voices")
                conn.getresponse().read()
                conn.close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("service did not start")
        cls.key = (tmp / "data" / "reply_key").read_text().strip()
        for name in cls.AGENTS:
            cls.token(name)

    @classmethod
    def tearDownClass(cls):
        stop(cls.proc, cls.reader)
        cls.tmp.cleanup()

    def tell(self, *args, input=None):
        args = args if "--from" in args else ("--from", "tester", *args)
        r = subprocess.run([sys.executable, "-m", "nanotea.tell", *args], env=self.env, input=input,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def call(self, method, path, body=None, paired=True):
        headers = dict(PROXY)
        if paired:
            headers["Cookie"] = f"{COOKIE}={self.key}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        data = r.read().decode()
        conn.close()
        return r.status, data

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {**({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})})
        r = conn.getresponse()
        data = r.read().decode()
        conn.close()
        return r.status, data

    def row(self, agent):
        board = json.loads(self.local("GET", "/api/board")[1])
        found = [(g["name"], r) for g in board["sidebar"]["groups"] for r in g["rows"] if r["agent"] == agent]
        self.assertEqual(len(found), 1, board)
        return found[0]

    def listen(self, *args):
        return subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--listen", *args],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    @staticmethod
    def stop(proc):
        proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()

    def wait_for(self, check, what):
        deadline = time.time() + 20
        while time.time() < deadline:
            if check():
                return
            time.sleep(0.2)
        self.fail(f"timed out waiting for {what}")

    def test_status_typing_groups_and_pulse(self):
        self.tell("--from", "bob", "--status", "parsing the logs")
        listener = self.listen("--from", "bob", "--inbox", "bob")
        try:
            self.wait_for(lambda: self.call("GET", "/chat/bob")[0] == 200, "bob's line")
            group, row = self.row("bob")
            self.assertEqual(group, "Other")
            self.assertEqual(row["status"]["text"], "parsing the logs")
            self.assertFalse(row["responding"])
            self.assertEqual(self.call("POST", "/api/dm-bob/messages", {"text": "look at this"})[0], 200)
            out, _ = listener.communicate(timeout=20)
        finally:
            self.stop(listener)
        self.assertIn("look at this", out)
        self.assertTrue(self.row("bob")[1]["responding"])
        self.tell("--from", "bob", "--typing", "--inbox", "bob")
        self.assertTrue(self.row("bob")[1]["typing"])
        # Waiting with --idle says bob is done working on the message.
        idle = self.listen("--from", "bob", "--inbox", "bob", "--idle")
        try:
            self.wait_for(lambda: not self.row("bob")[1]["responding"], "bob to stop responding")
        finally:
            self.stop(idle)
        builder = self.listen("--from", "builder", "--inbox", "builder")
        try:
            self.wait_for(lambda: self.call("GET", "/chat/builder")[0] == 200, "builder's line")
            self.assertEqual(self.row("builder")[0], "Builders")
        finally:
            self.stop(builder)
        self.assertIn("parsing the logs", self.tell("--board"))

    def test_mcp_agent(self):
        # legacy: the initialize handshake today's harnesses use; auto: the 2026-07-28 stateless protocol
        for mode, name in (("legacy", "Carol"), ("auto", "Dave")):
            with self.subTest(mode=mode):
                asyncio.run(self.mcp_agent(mode, name))

    async def mcp_agent(self, mode, agent):
        server = StdioServerParameters(command=sys.executable, args=["-m", "nanotea", "mcp"],
                                       env=self.env_for(agent), cwd=self.tmp.name)

        async def tool(client, tool_name, **args):
            r = await client.call_tool(tool_name, args)
            self.assertFalse(r.is_error, r.content)
            return r.structured_content

        async with Client(server, mode=mode) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            self.assertLessEqual({"join", "send", "ask", "check", "wait", "status", "typing", "react"}, names)
            r = await c.call_tool("check", {})
            self.assertTrue(r.is_error)
            self.assertIn("call join first", r.content[0].text)
            voice = {"Carol": "v2", "Dave": None}[agent]
            out = await tool(c, "join", name=agent, voice=voice, about="testing the MCP server")
            self.assertEqual((out["line"], out["voice"], out["waiting"]), (agent.lower(), voice, 0))
            if voice is None:
                self.assertIn("no voice yet", out["note"])
                r = await c.call_tool("send", {"text": "no voice"})
                self.assertTrue(r.is_error)
                self.assertIn("you have no voice yet: call voices", r.content[0].text)
                out = await tool(c, "join", name=agent, voice="v3")
                self.assertEqual(out["voice"], "v3")
            line = agent.lower()
            self.assertEqual(self.row(agent)[1]["path"], f"/chat/{line}")
            sent = await tool(c, "send", text="Hello from MCP.")
            self.assertTrue(sent["id"])
            time.sleep(1.1)  # message times are to the second: a send in the delivery's second counts as a reply
            self.assertEqual(self.call("POST", f"/api/dm-{line}/messages", {"text": f"hi {line}"})[0], 200)
            out = await tool(c, "status", text="reading")
            self.assertEqual(out["waiting"], 1)
            out = await tool(c, "wait", timeout_s=10)
            self.assertEqual([(i["kind"], i["from"], i["text"]) for i in out["items"]],
                             [("message", "owner", f"hi {line}")])
            self.assertTrue(self.row(agent)[1]["responding"])
            out = await tool(c, "wait", timeout_s=1)
            self.assertTrue(out["timed_out"])
            self.assertFalse(self.row(agent)[1]["responding"])
            asked = await tool(c, "ask", question="Ship it?", raw=True)
            self.wait_for(lambda: json.loads(self.local("GET", f"/api/messages/{asked['id']}")[1])["status"]
                          == "done", "the question to be voiced")
            self.assertTrue(json.loads(self.local("GET", f"/api/messages/{asked['id']}")[1])["raw"])
            status, draft = self.upload(f"/api/messages/{asked['id']}/drafts", "take", "audio/wav", b"RIFF take")
            self.assertEqual(status, 200, draft)
            capture = {"raw": True, "rate": None, "channels": None, "echo_cancellation": False,
                       "noise_suppression": False, "auto_gain_control": None, "mic": None}
            self.assertEqual(self.call("POST", f"/api/messages/{asked['id']}/reply",
                                       {"text": "yes", "drafts": [draft["draft"]],
                                        "captures": {draft["draft"]: capture}})[0], 200)
            out = await tool(c, "wait", timeout_s=10)
            self.assertEqual([(i["kind"], i["re"]["id"], i["text"]) for i in out["items"]],
                             [("answer", asked["id"], "yes")])
            voice = out["items"][0]["voice"]
            self.assertEqual((voice["transcript"], voice["takes"]),
                             ("transcript", [{"type": "audio/wav", "size": 9, "capture": capture,
                                              "path": voice["audio_path"]}]))
            out = await tool(c, "check")
            self.assertEqual(out["items"], [])
            second = await tool(c, "ask", question="And the docs?")
        # The answer waits on the server for the next session of the same agent.
        self.wait_for(lambda: json.loads(self.local("GET", f"/api/messages/{second['id']}")[1])["status"]
                      == "done", "the second question to be voiced")
        self.assertEqual(self.call("POST", f"/api/messages/{second['id']}/reply", {"text": "later"})[0], 200)
        async with Client(server, mode=mode) as c:
            out = await tool(c, "join", name=agent)
            self.assertEqual(out["waiting"], 1)
            out = await tool(c, "check")
            self.assertEqual([(i["kind"], i["re"]["id"], i["text"]) for i in out["items"]],
                             [("answer", second["id"], "later")])
            self.assertEqual(out["waiting"], 0)

    def test_mcp_preset(self):
        asyncio.run(self.mcp_preset())
        r = subprocess.run([sys.executable, "-m", "nanotea", "mcp", "--name", "Gus", "--voice", "nope"],
                           cwd=self.tmp.name, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nanotea mcp: unknown voice 'nope'", r.stderr)

    async def mcp_preset(self):
        server = StdioServerParameters(command=sys.executable, cwd=self.tmp.name, env=self.env,
                                       args=["-m", "nanotea", "mcp", "--name", "Frank", "--idle", "hook",
                                             "--wait-s", "2"])
        async with Client(server, mode="legacy") as c:
            self.assertIn('You are joined as "Frank" on the line "frank"', c.instructions)
            self.assertIn("end your turn: a hook brings you back when Robin writes", c.instructions)
            self.assertEqual([p.name for p in (await c.list_prompts()).prompts], ["on_call"])
            started = time.monotonic()
            r = await c.call_tool("wait", {})
            self.assertFalse(r.is_error, r.content)
            self.assertTrue(r.structured_content["timed_out"])
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(self.row("Frank")[1]["path"], "/chat/frank")

    def test_plugins_command(self):
        def run(*args, env=None):
            return subprocess.run([sys.executable, "-m", "nanotea", "plugins", *args], cwd=self.tmp.name,
                                  env=env or self.env, capture_output=True, text=True, timeout=60)

        listed = run()
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("tts:\n   speechify    built-in", listed.stdout)
        self.assertIn(" * identity     built-in     No rewrite: the voice reads the original text, less its markdown. The "
                      "title is the sender's, or the text's first line.\n", listed.stdout)
        self.assertIn("phone:\n  (none installed)", listed.stdout)
        checked = run("--check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("ok      tts command: 7 voices, .m4a\n", checked.stdout)
        self.assertIn("ok      stt command\n", checked.stdout)
        self.assertIn("ok      rewrite identity\n", checked.stdout)
        fresh = Path(self.tmp.name, "fresh.toml")
        fresh.write_text(self.config.read_text().replace(str(Path(self.tmp.name, "data")),
                                                          str(Path(self.tmp.name, "fresh", "data"))))
        first = run("--check", env={**self.env, "NANOTEA_CONFIG": str(fresh)})
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(Path(self.tmp.name, "fresh", "data", "vapid_private.pem").exists())
        broken = Path(self.tmp.name, "broken.toml")
        broken.write_text(self.config.read_text().replace('[stt.command]\nargv = [', '[stt.command]\nargs = ['))
        failed = run("--check", env={**self.env, "NANOTEA_CONFIG": str(broken)})
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("FAILED  stt command: ConfigError: [stt.command] needs argv", failed.stdout)

    def test_setup_and_sop(self):
        def run(*args):
            r = subprocess.run([sys.executable, "-m", "nanotea", *args], cwd=self.tmp.name, env=self.env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

        exe = str(Path(sys.executable).with_name("nanotea"))
        for harness in ("claude", "codex", "cursor", "gemini", "opencode", "goose", "vscode", "zed", "cline", "amp",
                        "other"):
            with self.subTest(harness=harness):
                out = run("setup", harness, "--name", "Builder Two", "--channel", "general")
                self.assertIn(exe, out)
                self.assertNotIn("{owner}", out)
                self.assertNotIn("{wait_s}", out)
                # Every JSON block printed parses.
                blocks = re.findall(r"^\{\n.*?^\}$", out, re.S | re.M)
                for b in blocks:
                    json.loads(b)
                self.assertTrue(blocks or harness in ("codex", "goose", "other"))
        out = run("setup", "claude", "--name", "Builder Two")
        server = json.loads(re.search(r"^\{\n.*?^\}$", out, re.S | re.M)[0])["mcpServers"]["nanotea"]
        self.assertEqual(server["args"], ["mcp", "--name", "Builder Two", "--idle", "hook"])
        self.assertEqual(server["env"]["NANOTEA_CONFIG"], str(self.config.resolve()))
        self.assertIn("hook rewake --name 'Builder Two'", out)
        toml = tomllib.loads(re.search(r"^\[mcp_servers\.nanotea\]$.*?^NANOTEA_CONFIG = .*?$",
                                       run("setup", "codex", "--name", "builder"), re.S | re.M)[0])
        self.assertEqual(toml["mcp_servers"]["nanotea"]["args"], ["mcp", "--name", "builder"])
        self.assertEqual(toml["mcp_servers"]["nanotea"]["env"]["NANOTEA_CONFIG"], str(self.config.resolve()))
        out = run("sop", "--name", "builder", "--idle", "wait")
        self.assertTrue(out.startswith("## Nanotea\n\nNanotea connects you to Robin"))
        self.assertIn('You are joined as "builder"', out)
        self.assertIn("call wait, and call it again", out)
        out = run("sop", "--skill")
        self.assertTrue(out.startswith("---\nname: nanotea\n"))
        self.assertIn("Call join once at the start", out)

    def hook(self, *args, stdin="{}"):
        return subprocess.run([sys.executable, "-m", "nanotea", "hook", *args], cwd=self.tmp.name, env=self.env,
                              input=stdin, capture_output=True, text=True, timeout=60)

    def test_hooks(self):
        self.assertEqual(self.local("POST", "/api/dm-erin", {"name": "Erin", "open": True})[0], 200)
        r = self.hook("stop", "--name", "Erin")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertFalse(self.row("Erin")[1]["listening"])
        r = self.hook("rewake", "--name", "Erin", "--max-s", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.row("Erin")[1]["listening"])  # an idle agent its wake hook waits for is reachable
        self.assertEqual(self.call("POST", "/api/dm-erin/messages", {"text": "wake up"})[0], 200)
        w = json.loads(self.local("GET", "/api/agents/Erin/waiting?line=dm-erin")[1])
        self.assertEqual((w["line"], w["answers"], w["total"], len(w["ids"])), (1, 0, 1, 1))
        for harness in ("claude", "codex", "gemini"):
            r = self.hook("stop", "--name", "Erin", "--harness", harness)
            self.assertEqual((r.returncode, r.stdout), (2, ""))
            self.assertIn("1 message(s) from Robin waiting. Call the nanotea check tool", r.stderr)
        r = self.hook("stop", "--name", "Erin", "--harness", "cursor")
        self.assertEqual(r.returncode, 0)
        self.assertIn("1 message(s) from Robin", json.loads(r.stdout)["followup_message"])
        for event in ('{"stop_hook_active": true}', '{"status": "completed", "loop_count": 1}'):
            r = self.hook("stop", "--name", "Erin", "--harness", "cursor", stdin=event)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            self.assertIn("not again", r.stderr)
        r = self.hook("rewake", "--name", "Erin", "--max-s", "10")
        self.assertEqual(r.returncode, 2)
        self.assertIn("1 message(s) from Robin", r.stderr)
        # Not woken again for the same message right away; a new one wakes it.
        r = self.hook("rewake", "--name", "Erin", "--max-s", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.call("POST", "/api/dm-erin/messages", {"text": "and this"})[0], 200)
        r = self.hook("rewake", "--name", "Erin", "--max-s", "10")
        self.assertEqual(r.returncode, 2)
        self.assertIn("2 message(s) from Robin", r.stderr)
        r = self.hook("stop", "--name", "Nobody")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nanotea-hook:", r.stderr)
        self.assertEqual(self.local("GET", "/api/agents/Erin/waiting?idle=1")[0], 400)
        # Through the proxy, even paired, nothing reads or claims what waits for an agent.
        for path in ("/api/dm-erin/pending?name=Erin", "/api/agents/Erin/waiting?line=dm-erin",
                     "/api/agents/Erin/answers"):
            self.assertEqual(self.call("GET", path)[0], 403, path)

    def test_pages_carry_the_configured_names(self):
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('data-app="Teapot"', page)
        self.assertIn('data-owner="Robin"', page)
        status, manifest = self.call("GET", f"/manifest.webmanifest?k={self.key}", paired=False)
        self.assertEqual(json.loads(manifest)["name"], "Teapot")
        self.assertEqual(json.loads(manifest)["theme_color"], "#ffffff")
        for word in ("Giles", "Agway", "Humwort"):
            self.assertNotIn(word, page)

    def test_only_the_app_frames_its_pages(self):
        paired = {**PROXY, "Cookie": f"{COOKIE}={self.key}"}
        for method, path, headers, want in (("GET", "/", paired, 200), ("GET", "/api/enroll", paired, 200),
                                            ("GET", "/", PROXY, 403), ("GET", "/no-such-page", paired, 404),
                                            ("BREW", "/", paired, 501)):
            conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
            conn.request(method, path, headers=headers)
            r = conn.getresponse()
            r.read()
            conn.close()
            self.assertEqual((r.status, r.getheader("Content-Security-Policy"), r.getheader("X-Frame-Options")),
                             (want, "frame-ancestors 'self'", "SAMEORIGIN"), (method, path))

    def test_markdown(self):
        text = "**Ship** it, see `x.py`\n\n```py\nrun()\nagain()\n```\n\n<script>alert(1)</script> [x](javascript:y)"
        msg = {"id": re.search(r"/m/([^/?#\s]+)", self.tell("--from", "builder", "--voice", "v1", text))[1]}
        self.wait_for(lambda: json.loads(self.local("GET", f"/api/messages/{msg['id']}")[1])["status"] == "done",
                      "the message")
        status, spoken = self.call("GET", f"/audio/{msg['id']}")  # voiced on request
        self.assertEqual(status, 200, spoken)
        self.assertEqual(spoken, "fake audio for Ship it, see x.py\n\nThere's a 2-line code block here, in the app."
                                 "\n\n<script>alert(1)</script> [x](javascript:y)")
        status, page = self.call("GET", f"/m/{msg['id']}")
        self.assertEqual(status, 200)
        self.assertIn("<strong>Ship</strong>", page)
        self.assertIn('<div class="code"><div class="code-head"><span class="lang">py</span>', page)
        self.assertIn("<code>run()\nagain()</code>", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn('href="javascript', page)
        self.assertIn("**Ship** it", page)  # the Original, as written
        # The owner's text: markdown, its first line's indent kept, blank lines before and spaces after dropped.
        status, body = self.call("POST", "/api/channels/general/messages", {"text": "\n\n    x = 1\n**bold**  \n"})
        self.assertEqual(status, 200, body)
        self.assertIn("<p>    x = 1\n<strong>bold</strong></p>", self.call("GET", "/c/general")[1])

    def upload(self, path, name, ctype, raw, length=None):
        """The owner attaches a file: raw bytes, as the page sends them. length: claim another size, sending none."""
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        conn.putrequest("POST", f"{path}?name={quote(name)}")
        for k, v in {**PROXY, "Cookie": f"{COOKIE}={self.key}", "Content-Type": ctype,
                     "Content-Length": str(len(raw) if length is None else length)}.items():
            conn.putheader(k, v)
        conn.endheaders()
        if length is None:
            conn.send(raw)
        r = conn.getresponse()
        body = json.loads(r.read())
        conn.close()
        return r.status, body

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_every_page_script_parses_as_served(self):
        # As served, not as written in pages.py: Python's escapes can break a JS string that reads fine there.
        status, out = self.local("POST", "/api/messages", {"text": "a page", "from": "Pix", "voice": "v7"})
        self.assertEqual(status, 202, out)
        msg_id = json.loads(out)["id"]
        with tempfile.TemporaryDirectory() as tmp:
            for path in ("/", "/chat", "/c/general", f"/m/{msg_id}", "/settings", "/config", "/prompts",
                         "/rules", "/talk"):
                status, page = self.call("GET", path)
                self.assertEqual(status, 200, path)
                for i, script in enumerate(re.findall(r"<script>(.*?)</script>", page, re.S)):
                    f = Path(tmp, f"{len(path)}-{i}.js")
                    f.write_text(script)
                    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, f"{path} script {i}: {r.stderr}")

    def test_owner_attaches_audio(self):
        raw = b"RIFF" + bytes(range(256)) * 16384  # 4 MB, under [audio] max_upload_mb
        status, out = self.upload("/api/channels/general/files", "Take 2.wav", "application/octet-stream", raw)
        self.assertEqual(status, 200, out)
        status, body = self.call("POST", "/api/channels/general/messages", {"text": "the bridge", "files": [out["file"]]})
        self.assertEqual(status, 200, body)
        page = self.call("GET", "/c/general")[1]
        src = re.search(r'<audio controls preload="none" src="(/files/c/general/[^"]+/1-Take_2\.wav)" '
                        r'data-title="Take 2\.wav" data-artist="Robin"></audio>', page)
        self.assertIsNotNone(src, page[-3000:])
        self.assertIn("Take 2.wav, 4.0 MB, as sent</a>", page)
        for extra, want in (({}, (200, "audio/wav", raw)), ({"Range": "bytes=0-3"}, (206, "audio/wav", b"RIFF"))):
            conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
            conn.request("GET", src[1], headers={**PROXY, "Cookie": f"{COOKIE}={self.key}", **extra})
            r = conn.getresponse()
            self.assertEqual((r.status, r.getheader("Content-Type"), r.read()), want)
            conn.close()
        self.assertEqual(list(Path(self.tmp.name, "data", "uploads").iterdir()), [])
        status, out = self.upload("/api/channels/general/files", "notes.txt", "text/plain", b"hi")
        self.assertEqual(status, 415)
        self.assertIn("pictures, PDFs, video (mp4, mov) and audio", out["error"])
        status, out = self.upload("/api/channels/general/files", "long.wav", "audio/wav", b"", length=6 * 2**20)
        self.assertEqual((status, out["error"]), (413, "the request body is empty or over 5 MB"))
        self.assertEqual(list(Path(self.tmp.name, "data", "uploads").iterdir()), [])

    def test_agent_media(self):
        def att(name, data=b"bytes of " ):
            return {"name": name, "data": base64.b64encode(data + name.encode()).decode()}
        files = [att("shot.png"), att("spec.pdf"), att("demo.mp4"), att("notes.wav")]
        for text, says in (("[[image 4]]", "attachment 4, 'notes.wav', is audio; write [[audio 4]]"),
                           ("[[pdf 9]]", "[[pdf 9]] has no matching attachment"),
                           ("[[image 1]] [[image 1]]", "attachment 1 is placed more than once")):
            status, body = self.local("POST", "/api/messages", {"text": text, "from": "Pix", "attachments": files})
            self.assertEqual(status, 400, text)
            self.assertIn(says, json.loads(body)["error"])
        status, body = self.local("POST", "/api/messages", {"text": "t", "from": "Pix", "attachments": [att("a.exe")]})
        self.assertEqual(status, 400)
        self.assertIn("not audio, an image, a video or a PDF", json.loads(body)["error"])
        status, body = self.local("POST", "/api/messages", {"text": "Here it is: [[image 1]]\n\nRead this.",
                                                            "from": "Pix", "voice": "v7", "attachments": files[:3]})
        self.assertEqual(status, 202, body)
        msg_id = json.loads(body)["id"]
        self.wait_for(lambda: json.loads(self.local("GET", f"/api/messages/{msg_id}")[1])["status"] == "done",
                      "the message to be ready")
        page = self.call("GET", f"/m/{msg_id}")[1]
        text = re.search(r'<div class="text">(.*?)</div>', page, re.S)[1]
        self.assertLess(text.index(f'<img class="thumb" src="/attachment/{msg_id}/1"'), text.index("Read this."))
        self.assertGreater(text.index(f'<a class="file" href="/attachment/{msg_id}/2"'), text.index("Read this."))
        self.assertIn(f'<video class="media" controls playsinline preload="metadata" src="/attachment/{msg_id}/3"',
                      text)
        self.assertIn("Attached files, as sent", page)
        for n, ctype in ((1, "image/png"), (2, "application/pdf"), (3, "video/mp4")):
            conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
            conn.request("GET", f"/attachment/{msg_id}/{n}", headers={**PROXY, "Cookie": f"{COOKIE}={self.key}"})
            r = conn.getresponse()
            self.assertEqual((r.status, r.getheader("Content-Type"), r.getheader("X-Content-Type-Options")),
                             (200, ctype, "nosniff"))
            r.read()
            conn.close()
        status, _ = self.call("GET", f"/audio/{msg_id}")
        self.assertEqual(status, 200)
        voiced = json.loads(self.local("GET", f"/api/messages/{msg_id}")[1])
        spoken = Path(voiced["dir"], voiced["audio"]).read_bytes()
        self.assertIn(b"Here it is:", spoken)
        self.assertNotIn(b"[[image", spoken)
        status, out = self.upload("/api/channels/general/files", "screen.mov", "video/quicktime", b"moov")
        self.assertEqual(status, 200, out)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_raw_takes(self):
        def wav(name, seconds):
            out = Path(self.tmp.name, name)
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            f"sine=frequency=440:sample_rate=48000:duration={seconds}", "-ac", "2", str(out)],
                           check=True)
            return out.read_bytes()

        raw_cap = {"raw": True, "rate": 48000, "channels": 2, "echo_cancellation": False,
                   "noise_suppression": False, "auto_gain_control": False, "mic": "Test Mic"}
        speech_cap = {**raw_cap, "raw": False, "echo_cancellation": True, "noise_suppression": None,
                      "channels": 1, "mic": None}
        status, body = self.local("POST", "/api/messages", {"text": "Say it", "from": "taker", "voice": "v4",
                                                            "raw": True})
        self.assertEqual((status, json.loads(body)["error"]),
                         (400, "ValueError: 'raw' asks for the answer to be recorded raw; it goes with 'ask'"))
        status, body = self.local("POST", "/api/messages", {"text": "Repeat after me: *the quick brown fox*.",
                                                            "from": "taker", "voice": "v4", "ask": True,
                                                            "raw": True})
        self.assertEqual(status, 202, body)
        msg_id = json.loads(body)["id"]
        page = self.call("GET", f"/m/{msg_id}")[1]
        self.assertIn('data-raw="1"', page)
        self.assertIn(", recorded raw</div>", page)
        drafts = []
        for name, seconds in (("a.wav", 0.5), ("b.wav", 0.25)):
            status, out = self.upload(f"/api/messages/{msg_id}/drafts", name, "audio/wav", wav(name, seconds))
            self.assertEqual(status, 200, out)
            drafts.append(out["draft"])
        r = subprocess.run([sys.executable, "-m", "nanotea.tell", "--from", "taker", "--raw", "x"], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--raw goes with --ask", r.stderr)
        reply = f"/api/messages/{msg_id}/reply"
        for captures, error in (({drafts[0]: {"raw": True}}, f"capture of {drafts[0]} must have exactly raw, rate"),
                                ({drafts[0]: {**raw_cap, "rate": "48000"}}, "bad rate '48000'"),
                                ({drafts[0]: {**raw_cap, "raw": 1}}, "bad raw 1"),
                                ({"0123456789ab": raw_cap}, "'captures' must map recording ids sent in 'drafts'")):
            status, body = self.call("POST", reply, {"text": None, "drafts": drafts, "captures": captures})
            self.assertEqual(status, 400, body)
            self.assertIn(error, json.loads(body)["error"])
        status, body = self.call("POST", reply, {"text": None, "drafts": drafts,
                                                 "captures": {drafts[0]: raw_cap, drafts[1]: speech_cap}})
        self.assertEqual(status, 200, body)
        answers = []
        self.wait_for(lambda: answers.extend(a for a in json.loads(self.local("GET", "/api/agents/taker/answers")[1])
                                             if a["id"] == msg_id) or answers, "the answer")
        takes = answers[0]["takes"]
        self.assertEqual([(t["type"], t["capture"]) for t in takes],
                         [("audio/wav", raw_cap), ("audio/wav", speech_cap)])
        for t, name in zip(takes, ("a.wav", "b.wav")):
            self.assertTrue(t["path"].endswith(f"/takes/{Path(t['path']).name}"), t)
            self.assertEqual(Path(t["path"]).read_bytes(), Path(self.tmp.name, name).read_bytes())
            self.assertEqual(t["size"], Path(t["path"]).stat().st_size)
        self.assertTrue(answers[0]["audio_path"].endswith("/reply.mp3"))
        page = self.call("GET", f"/m/{msg_id}")[1]
        self.assertIn('<div class="meta">Recording 1, recorded raw: 48 kHz, 2 channels, Test Mic</div>', page)
        self.assertNotIn("Recording 2, recorded raw", page)

        # One take is the message's audio itself, as recorded.
        listener = subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", "taker", "--listen",
                                     "--inbox", "taker"], env=self.env, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        try:
            self.wait_for(lambda: self.call("GET", "/chat/taker")[0] == 200, "the taker line")
            status, out = self.upload("/api/dm-taker/drafts", "c.wav", "audio/wav", wav("c.wav", 0.25))
            self.assertEqual(status, 200, out)
            status, body = self.call("POST", "/api/dm-taker/messages",
                                     {"text": None, "drafts": [out["draft"]], "captures": {out["draft"]: raw_cap}})
            self.assertEqual(status, 200, body)
            printed, _ = listener.communicate(timeout=30)
        finally:
            self.stop(listener)
        audio = re.search(r"^\[voice audio\] (.+)$", printed, re.M)[1]
        size = len(Path(self.tmp.name, "c.wav").read_bytes())
        self.assertIn(f"[take] {audio} (audio/wav, {size} bytes; raw, 48000 Hz, 2 channels, echo cancellation off, "
                      "noise suppression off, auto gain control off, mic: Test Mic)", printed)

    def test_unpaired_pages_are_refused(self):
        self.assertEqual(self.call("GET", "/chat", paired=False)[0], 403)

    def test_unpaired_page_gives_nothing_away(self):
        status, page = self.call("GET", "/", paired=False)
        self.assertEqual(status, 403)
        self.assertNotIn(self.key, page)
        self.assertNotIn("manifest", page)
        self.assertNotIn("notify-on", page)
        self.assertIn("nanotea pair-link", page)
        self.assertNotIn("iMessage", page)

    def headed(self, method, path, headers, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        conn.request(method, path, body, headers)
        r = conn.getresponse()
        data = r.read().decode()
        conn.close()
        return r.status, data

    def test_foreign_origin_is_refused(self):
        # A cross-site request carries its Origin; refused before its credential is looked at.
        sent = json.dumps({"from": "rebinder", "text": "hi"})
        agent = {"Authorization": f"Bearer {self.token('rebinder')}", "Content-Type": "text/plain"}
        status, body = self.headed("POST", "/api/messages", {**agent, "Origin": "http://evil.test"}, sent)
        self.assertEqual(status, 403)
        self.assertIn("http://evil.test", body)
        owner = {**PROXY, "Cookie": f"{COOKIE}={self.key}", "Content-Type": "application/json"}
        status, body = self.headed("POST", "/api/read-all", {**owner, "Origin": "https://evil.test"}, "{}")
        self.assertEqual(status, 403)
        self.assertIn("https://evil.test", body)
        self.assertEqual(self.headed("POST", "/api/read-all", {**owner, "Origin": "https://nanotea.test"}, "{}")[0], 200)

    def test_manifest_only_for_paired_browsers(self):
        self.assertEqual(self.local("GET", "/manifest.webmanifest")[0], 403)
        status, body = self.call("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertIn(self.key, body)

    def test_message_ask_and_reply(self):
        self.tell("--from", "builder", "--voice", "v1", "--title", "Done", "Build finished.")
        proc = subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", "builder", "--ask",
                                 "Delete the cache?"], env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        try:
            deadline = time.time() + 20
            msg_id = None
            while time.time() < deadline and msg_id is None:
                time.sleep(0.3)
                status, body = self.call("GET", "/api/messages")
                ask = [m for m in json.loads(body)
                       if m.get("ask") and m["sender"] == "builder" and m["reply"] is None]
                msg_id = ask[0]["id"] if ask else None
            self.assertIsNotNone(msg_id)
            self.assertEqual(self.call("POST", f"/api/messages/{msg_id}/reply", {"text": "yes"}, paired=False)[0], 401)
            self.assertEqual(self.call("POST", f"/api/messages/{msg_id}/reply", {"text": "yes"})[0], 200)
            out, _ = proc.communicate(timeout=30)
        finally:
            proc.kill()
        self.assertEqual(out.strip().splitlines()[-1], "yes")
        self.assertIsNotNone(json.loads(self.local("GET", f"/api/messages/{msg_id}")[1])["answer_delivered"])

    def test_direct_line_and_channel(self):
        listener = subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", "alice",
                                     "--listen", "--inbox", "alice"], env=self.env, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        try:
            for _ in range(50):
                status, page = self.call("GET", "/chat/alice")
                if status == 200:
                    break
                time.sleep(0.2)
            self.assertEqual(status, 200)
            self.assertEqual(self.call("POST", "/api/dm-alice/messages", {"text": "hello alice"})[0], 200)
            out, _ = listener.communicate(timeout=20)
        finally:
            self.stop(listener)
        self.assertIn("[from Robin,", out)
        self.assertIn("hello alice", out)
        status, body = self.call("POST", "/api/channels", {"name": "ops"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["by"], "Robin")
        self.assertIn("#ops", self.tell("--channels"))
        self.assertIn("#general", self.tell("--channels"))

    # Settings are the service's, shared by every test: each test that changes them puts them back.
    def settings(self, **patch):
        status, body = self.call("POST", "/api/settings", patch)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def restore(self):
        self.settings(**DEFAULTS)

    def mcp(self, *args, as_="tester"):
        """An MCP server that joins by tool as as_: its token is the file NANOTEA_TOKEN_FILE names."""
        return StdioServerParameters(command=sys.executable, args=["-m", "nanotea", "mcp", *args],
                                     env=self.env_for(as_), cwd=self.tmp.name)

    async def tool(self, client, tool_name, **args):
        r = await client.call_tool(tool_name, args)
        self.assertFalse(r.is_error, r.content)
        return r.structured_content

    async def refused(self, client, tool_name, **args):
        r = await client.call_tool(tool_name, args)
        self.assertTrue(r.is_error, r.structured_content)
        return r.content[0].text

    def test_settings_and_costs(self):
        status, body = self.call("GET", "/api/settings")
        self.assertEqual(status, 200)
        view = json.loads(body)
        self.assertEqual(view["settings"], DEFAULTS)
        by = view["costs"]["by"]
        self.assertEqual(view["session_tokens"], session_total(view["costs"], view["settings"]))
        for feature in ("strict_sop", "rules", "agent_messages", "tools.react", "tools.history"):
            self.assertGreater(by[feature], 0, feature)
        for feature in ("one_session", "held", "wake_filter"):  # they change behavior, not what agents read
            self.assertEqual(by[feature], 0, feature)
        self.assertGreater(view["costs"]["base"], 0)
        self.assertEqual([f["key"] for f in view["features"]][:2], ["rules", "wake_filter"])
        for bad, says in (({"nope": True}, "unknown setting 'nope'"), ({"rules": "yes"}, "rules must be true"),
                          ({"tools": {"zap": True}}, "unknown tool 'zap'"),
                          ({"held_push_min": -1}, "held_push_min must be"),
                          ({"agent_messages": "loud"}, "agent_messages must be one of"), ([], "send an object")):
            status, body = self.call("POST", "/api/settings", bad)
            self.assertEqual(status, 400, bad)
            self.assertIn(says, json.loads(body)["error"])
        self.assertEqual(self.call("POST", "/api/settings", {"rules": False}, paired=False)[0], 401)
        strict = "Send nothing that only acknowledges"
        try:
            after = self.settings(strict_sop=False, tools={"react": False})
            self.assertEqual(after["session_tokens"], view["session_tokens"] - by["strict_sop"] - by["tools.react"])
            self.assertEqual(after["settings"]["tools"], {**DEFAULTS["tools"], "react": False})
            stored = json.loads((Path(self.tmp.name) / "data" / "settings.json").read_text())
            self.assertFalse(stored["strict_sop"])
            r = subprocess.run([sys.executable, "-m", "nanotea", "sop"], cwd=self.tmp.name, env=self.env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn(strict, r.stdout)

            async def session():
                async with Client(self.mcp(), mode="legacy") as c:
                    self.assertNotIn(strict, c.instructions)
                    names = {t.name for t in (await c.list_tools()).tools}
                    self.assertNotIn("react", names)
                    self.assertIn("history", names)
            asyncio.run(session())
        finally:
            self.restore()
        r = subprocess.run([sys.executable, "-m", "nanotea", "sop"], cwd=self.tmp.name, env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn(strict, r.stdout)
        status, page = self.call("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertIn(f'<b id="session-tokens">{view["session_tokens"]}</b>', page)
        self.assertIn("Standing rules", page)
        self.assertIn('name="tools.react" checked', page)
        for path in ("/settings", "/rules", "/talk"):
            self.assertEqual(self.call("GET", path, paired=False)[0], 403, path)

    def test_rules(self):
        added = []

        def add(text, for_=None):
            status, body = self.call("POST", "/api/rules", {"text": text, "for": for_})
            self.assertEqual(status, 200, body)
            added.append(json.loads(body)["id"])
            return added[-1]

        try:
            add("Never push to main.")
            add("Run the tests before you report.", "Hal")
            add("Not for Hal.", "Zed")
            self.assertEqual(self.call("POST", "/api/rules", {"text": "x"}, paired=False)[0], 401)
            self.assertEqual(self.call("POST", "/api/rules", {"text": " "})[0], 400)
            self.assertEqual(self.call("POST", "/api/rules/00000000/delete", {})[0], 404)
            asyncio.run(self.rules_session(add, added))
            # Pin one of the owner's messages as a rule.
            self.assertEqual(self.call("POST", "/api/dm-hal/messages", {"text": "Always say which branch."})[0], 200)
            pending = json.loads(self.local("GET", "/api/dm-hal/pending?name=Hal")[1])
            msg_id = [m["id"] for m in pending if m["text"] == "Always say which branch."][0]
            self.assertIn("Pin as rule", self.call("GET", "/chat/hal")[1])
            status, page = self.call("GET", f"/rules?pin={msg_id}&where=dm-hal")
            self.assertEqual(status, 200)
            self.assertIn(">Always say which branch.</textarea>", page)
            self.assertIn('<option value="Hal" selected>', page)
            self.assertIn(f'name="from" value="{msg_id}"', page)
            self.assertEqual(self.call("GET", "/rules?pin=20990101-000000-abcdef&where=dm-hal")[0], 404)
            status, page = self.call("GET", "/rules")
            self.assertIn("Never push to main.", page)
            self.assertNotIn("Standing rules are off", page)
            self.settings(rules=False)
            self.assertIn("Standing rules are off", self.call("GET", "/rules")[1])

            async def off():
                async with Client(self.mcp(as_="Hal"), mode="legacy") as c:
                    out = await self.tool(c, "join", name="Hal")
                    self.assertNotIn("rules", out)
                    self.assertNotIn("standing rules", c.instructions)
            asyncio.run(off())
        finally:
            self.restore()
            for rule in added:
                self.call("POST", f"/api/rules/{rule}/delete", {})
        log = [json.loads(x) for x in (Path(self.tmp.name) / "data" / "rules-log.jsonl").read_text().splitlines()]
        self.assertIn("Never push to main.", [x["added"]["text"] for x in log if "added" in x])

    async def rules_session(self, add, added):
        async with Client(self.mcp(as_="Hal"), mode="legacy") as c:
            self.assertIn("standing rules come with join", c.instructions)
            out = await self.tool(c, "join", name="Hal")
            self.assertEqual({r["text"] for r in out["rules"]}, {"Never push to main.",
                                                                 "Run the tests before you report."})
            self.assertNotIn("rules_changed", out)
            out = await self.tool(c, "check")
            self.assertNotIn("rules", out)
            new = add("Speak slowly.")
            out = await self.tool(c, "check")
            self.assertTrue(out["rules_changed"])
            self.assertIn("Speak slowly.", [r["text"] for r in out["rules"]])
            self.assertNotIn("rules", await self.tool(c, "check"))
            self.assertEqual(self.call("POST", f"/api/rules/{new}/delete", {})[0], 200)
            added.remove(new)
            out = await self.tool(c, "check")
            self.assertNotIn("Speak slowly.", [r["text"] for r in out["rules"]])

    def test_sessions_and_presence(self):
        asyncio.run(self.sessions())
        self.wait_for(lambda: self.row("Jay")[1]["session"] is None, "Jay's session to close")
        self.assertIn("no session open; messages wait until it checks", self.call("GET", "/chat/jay")[1])

    async def sessions(self):
        async with Client(self.mcp(as_="Jay"), mode="legacy") as c:
            await self.tool(c, "join", name="Jay")
            self.assertIsNotNone(self.row("Jay")[1]["session"])
            page = self.call("GET", "/chat/jay")[1]
            self.assertNotIn("no session open", page)
            async with Client(self.mcp(as_="Jay"), mode="legacy") as twin:
                said = await self.refused(twin, "join", name="Jay")
                self.assertIn("'Jay' already has a live session", said)
                try:
                    self.settings(one_session=False)
                    async with Client(self.mcp(as_="Jay"), mode="legacy") as third:
                        await self.tool(third, "join", name="Jay")
                finally:
                    self.restore()

    def test_held(self):
        self.assertEqual(self.local("POST", "/api/dm-kim", {"name": "Kim", "open": True})[0], 200)

        def held():
            return self.row("Kim")[1]["held"]

        r = self.hook("held", "--name", "Kim", stdin=json.dumps(
            {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "make deploy"}}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(held()["what"], "Bash: make deploy")
        page = self.call("GET", "/chat/kim")[1]
        self.assertIn("held at a permission prompt since", page)
        self.assertIn('class="dot held"', page)
        r = self.hook("clear", "--name", "Kim")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(held())
        # Anything the agent does means it got past the prompt.
        self.hook("held", "--name", "Kim", stdin='{"tool_name": "Edit"}')
        self.assertEqual(held()["what"], "Edit")
        self.tell("--from", "Kim", "--status", "deploying")
        self.assertIsNone(held())
        # Gemini: only its permission notification holds.
        self.hook("held", "--name", "Kim", stdin='{"notification_type": "Other", "message": "hi"}')
        self.assertIsNone(held())
        self.hook("held", "--name", "Kim", stdin='{"notification_type": "ToolPermission", "message": "Allow shell?"}')
        self.assertEqual(held()["what"], "Allow shell?")
        self.assertEqual(self.hook("stop", "--name", "Kim").returncode, 0)
        self.assertIsNone(held())
        self.hook("held", "--name", "Kim", stdin='{"tool_name": "Bash"}')
        self.assertEqual(self.hook("rewake", "--name", "Kim", "--max-s", "1").returncode, 0)
        self.assertIsNone(held())
        try:
            self.settings(held=False)
            r = self.hook("held", "--name", "Kim", stdin='{"tool_name": "Bash"}')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIsNone(held())
        finally:
            self.restore()
        # Events alone let others gather before they wake the agent.
        self.assertEqual(self.local("POST", "/api/dm-kim/events",
                                    {"kind": "round-done", "source": "ci", "summary": "build 12 passed"})[0], 202)
        started = time.monotonic()
        r = self.hook("rewake", "--name", "Kim", "--max-s", "30")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertGreaterEqual(time.monotonic() - started, EVENT_SETTLE_S)
        self.assertIn("1 event(s) waiting", r.stderr)

    def test_agent_talk(self):
        self.assertEqual(self.local("POST", "/api/dm-max", {"name": "Max", "open": True})[0], 200)
        status, body = self.local("POST", "/api/talk", {"from": "Lou", "to": "Max", "text": "hi"})
        self.assertEqual(status, 403)
        self.assertIn("Robin can turn them on in Settings", json.loads(body)["error"])
        self.assertIn("can't message each other", self.call("GET", "/talk")[1])
        self.assertNotIn('href="/talk"', self.call("GET", "/")[1])
        try:
            self.settings(agent_messages="shown")
            asyncio.run(self.talk())
            r = self.hook("stop", "--name", "Max")
            self.assertEqual(r.returncode, 2)
            self.assertIn("1 message(s) from other agents", r.stderr)
            page = self.call("GET", "/talk")[1]
            self.assertIn("hand me the logs", page)
            self.assertIn("not read yet", page)
            self.assertIn('href="/talk"', self.call("GET", "/")[1])
            asyncio.run(self.talk_check())
            self.assertIn("read ", self.call("GET", "/talk")[1])
            self.settings(agent_messages="hidden")
            page = self.call("GET", "/talk")[1]
            self.assertNotIn("hand me the logs", page)
            self.assertIn("stays between them", page)
            self.assertNotIn('href="/talk"', self.call("GET", "/")[1])
        finally:
            self.restore()

    async def talk(self):
        async with Client(self.mcp(as_="Lou"), mode="legacy") as c:
            self.assertIn("tell", {t.name for t in (await c.list_tools()).tools})
            self.assertIn("Other agents may write to you", c.instructions)
            await self.tool(c, "join", name="Lou")
            said = await self.refused(c, "tell", to="Nobody", text="hi")
            self.assertIn("Max", said)
            self.assertIn("can't tell yourself", await self.refused(c, "tell", to="lou", text="hi"))
            out = await self.tool(c, "tell", to="max", text="hand me the logs")
            self.assertTrue(out["id"])

    async def talk_check(self):
        async with Client(self.mcp(as_="Max"), mode="legacy") as c:
            await self.tool(c, "join", name="Max")
            out = await self.tool(c, "check")
            self.assertEqual([(i["kind"], i["from"], i["text"]) for i in out["items"]],
                             [("agent", "Lou", "hand me the logs")])

    def test_controls(self):
        status, body = self.local("GET", "/api/controls")
        self.assertEqual(status, 200)
        self.assertEqual([c["type"] for c in json.loads(body)], ["choice", "checklist"])
        self.assertIn('"type": "choice"', self.tell("--controls"))
        url = self.tell("--from", "Tia", "--voice", "v5", "--choice", "Left", "--choice", "Right", "Which way?")
        sent = json.loads(self.local("GET", f"/api/messages/{url.strip().rsplit('/', 1)[1]}")[1])
        self.assertEqual(sent["control"], {"type": "choice", "options": ["Left", "Right"]})
        for bad, says in (({"type": "nope"}, "no control 'nope'"), ({"type": "choice"}, "'options' must be a list"),
                          ({"type": "choice", "options": ["a", "a"]}, "the same label twice"),
                          ({"type": "checklist", "items": ["x"], "colour": 1}, "unknown key 'colour'"),
                          ([], "'control' must be")):
            status, body = self.local("POST", "/api/messages", {"text": "t", "from": "Tia", "control": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn(says, json.loads(body)["error"])
        proc = subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", "Tia", "--ask", "--choice", "Yes",
                                 "--choice", "No", "Merge?"], env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        try:
            def waiting():
                found = [m["id"] for m in json.loads(self.owner("GET", "/api/messages")[1])
                         if m["sender"] == "Tia" and m["ask"] and m["reply"] is None and m["control"]]
                return found[0] if found else None
            self.wait_for(waiting, "the question with a choice")
            asked = waiting()
            status, body = self.call("POST", f"/api/messages/{asked}/act", {"act": "pick", "value": 0})
            self.assertEqual((status, json.loads(body)["sent"]), (200, "answer"), body)
            out, _ = proc.communicate(timeout=30)
        finally:
            proc.kill()
            proc.stdout.close()
        self.assertEqual(out.strip().splitlines()[-2:], ["Yes", '[tap, choice control] {"picked": "Yes"}'])
        asyncio.run(self.controls())
        by =json.loads(self.call("GET", "/api/settings")[1])["costs"]["by"]
        self.assertGreater(by["tools.controls"], 0)
        try:
            self.settings(tools={"controls": False})

            async def off():
                async with Client(self.mcp(), mode="legacy") as c:
                    tools = {t.name: t for t in (await c.list_tools()).tools}
                    self.assertNotIn("controls", tools)
                    self.assertNotIn("control", tools["send"].input_schema["properties"])
                    self.assertNotIn("control", tools["ask"].input_schema["properties"])
            asyncio.run(off())
        finally:
            self.restore()

    async def controls(self):
        async with Client(self.mcp(as_="Tia"), mode="legacy") as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            self.assertIn("control", tools["send"].input_schema["properties"])
            await self.tool(c, "join", name="Tia")
            listed = await self.tool(c, "controls")
            self.assertIn("checklist", json.dumps(listed))
            self.assertIn("no control 'nope'", await self.refused(c, "send", text="t", control={"type": "nope"}))
            choice = (await self.tool(c, "send", text="Ship it?", control={"type": "choice",
                                                                            "options": ["Ship", "Hold"]}))["id"]
            check = (await self.tool(c, "ask", question="What did you check?",
                                     control={"type": "checklist", "items": ["Tests", "Docs", "Notes"]}))["id"]
            plain = (await self.tool(c, "send", text="No control here."))["id"]
            self.wait_for(lambda: json.loads(self.local("GET", f"/api/messages/{choice}")[1])["status"] == "done",
                          "the message to be ready")
            page = self.call("GET", f"/m/{choice}")[1]
            self.assertIn(f'data-control="{choice}"', page)
            self.assertIn('data-act="pick" data-value="1"', page)

            def act(msg_id, a, value=None, paired=True):
                status, body = self.call("POST", f"/api/messages/{msg_id}/act", {"act": a, "value": value}, paired)
                return status, json.loads(body)

            self.assertEqual(act(choice, "pick", 1, paired=False)[0], 401)
            self.assertEqual(act(choice, "pick", 5), (400, {"error": "not an option: 5"}))
            self.assertEqual(act(choice, "zap")[0], 400)
            self.assertEqual(act(plain, "pick", 0), (409, {"error": "this message has no control"}))
            status, out = act(choice, "pick", 1)
            self.assertEqual((status, out["sent"], out["answered"]), (200, "tia", False))
            self.assertIn('aria-pressed="true">Hold', out["html"])
            out = await self.tool(c, "wait", timeout_s=10)
            self.assertEqual([(i["kind"], i["text"], i["tap"], i["re"]["id"]) for i in out["items"]],
                             [("tap", "Hold", {"control": "choice", "data": {"picked": "Hold"}}, choice)])
            self.assertIn("You tapped", self.call("GET", "/chat/tia")[1])
            for i in (0, 2, 2):
                self.assertEqual(act(check, "tick", i)[1]["sent"], None)
            self.assertIn('data-value="0" checked', self.call("GET", f"/m/{check}")[1])
            status, out = act(check, "done")
            self.assertEqual((status, out["sent"], out["answered"]), (200, "answer", True))
            self.assertIn("disabled", out["html"])
            self.assertEqual(act(check, "tick", 1), (409, {"error": "this question is already answered"}))
            out = await self.tool(c, "wait", timeout_s=10)
            self.assertEqual([(i["kind"], i["re"]["id"], i["text"], i["tap"]) for i in out["items"]],
                             [("answer", check, "Done. Ticked: Tests. Not ticked: Docs, Notes.",
                               {"control": "checklist", "data": {"ticked": ["Tests"], "unticked": ["Docs", "Notes"]}})])

    def test_wake_filter(self):
        asyncio.run(self.wake_filter())

    async def wake_filter(self):
        async with Client(self.mcp(as_="Ned"), mode="legacy") as c:
            await self.tool(c, "join", name="Ned", channels=["general"])
            self.tell("--from", "Ola", "--voice", "v6", "--channel", "general", "--title", "Routine",
                      "Nightly run is green.")
            started = time.monotonic()
            out = await self.tool(c, "wait", timeout_s=8)
            self.assertGreaterEqual(time.monotonic() - started, 7.5)  # a plain post doesn't wake it
            self.assertEqual([i["text"] for i in out["items"]], ["Nightly run is green."])
            self.tell("--from", "Ola", "--channel", "general", "--title", "Ping", "@Ned can you look at the flake?")
            started = time.monotonic()
            out = await self.tool(c, "wait", timeout_s=30)
            self.assertLess(time.monotonic() - started, 20)
            self.assertEqual([i["text"] for i in out["items"]], ["@Ned can you look at the flake?"])
            self.assertEqual(self.local("POST", "/api/dm-ned/events",
                                        {"kind": "round-done", "source": "ci", "summary": "round 3 done"})[0], 202)
            started = time.monotonic()
            out = await self.tool(c, "wait", timeout_s=30)
            self.assertGreaterEqual(time.monotonic() - started, EVENT_SETTLE_S - 1)
            self.assertEqual([(i["kind"], i["text"]) for i in out["items"]], [("event", "round 3 done")])
        try:
            self.settings(wake_filter=False)
            async with Client(self.mcp(as_="Ned"), mode="legacy") as c:
                await self.tool(c, "join", name="Ned", channels=["general"])
                self.tell("--from", "Ola", "--channel", "general", "--title", "Routine", "Disk is fine.")
                started = time.monotonic()
                out = await self.tool(c, "wait", timeout_s=30)
                self.assertLess(time.monotonic() - started, 20)
                self.assertEqual([i["text"] for i in out["items"]], ["Disk is fine."])
        finally:
            self.restore()

    def test_live_page_stops_when_unpaired(self):
        self.assertEqual(self.local("POST", "/api/dm-pia", {"name": "Pia", "open": True})[0], 200)
        self.assertIn("no longer paired", self.call("GET", "/chat/pia")[1])


if __name__ == "__main__":
    unittest.main()
