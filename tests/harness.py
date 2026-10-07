"""A real service on a temporary data directory for a test class, with a recording notifier and many voices.

Each class takes its own port, NANOTEA_TEST_PORT (default 17447) plus its PORT_OFFSET."""

import http.client
import json
import os
import subprocess
import sys
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
BASE_PORT = int(os.environ.get("NANOTEA_TEST_PORT", "17447"))
COOKIE = "nanotea_key"
PROXY = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}
CONFIG = """
host = "127.0.0.1"
port = {port}
public_url = "https://nanotea.test"
trusted_proxies = ["127.0.0.1"]
data_dir = "{data}"
env_file = "{env}"
plugin_path = ["{tests}"]

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
voices = ["v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "v12"]
timeout_s = 60

[stt]
backend = "command"

[stt.command]
argv = ["{py}", "-c", "print('transcript')", "{{audio_file}}"]
timeout_s = 60

[notify]
now = ["fake_notify:Recorder"]
escalate = []
escalate_after_min = 30

[notify.push]
subject = "https://nanotea.test"

[notify."fake_notify:Recorder"]
path = "{notes}"
{extra}"""


def spawn(env, cwd):
    """The service, with its stderr drained into a list of lines: a pipe nobody reads fills and stops the service
    at its next log line. Returns the process, the lines, and the reader thread."""
    proc = subprocess.Popen([sys.executable, "-m", "nanotea", "serve"], cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.extend(proc.stderr), daemon=True)
    reader.start()
    return proc, lines, reader


def stop(proc, reader):
    proc.terminate()
    proc.wait(timeout=10)
    reader.join(timeout=5)
    proc.stderr.close()


class Tokens:
    """What a test class running a real service needs to act as its agents: needs port, key, env and tmp.

    token() issues through nanotea token add, as an operator would, and the programs the tests run find the file
    where they look for it (tmp/tokens/<slug>.token, beside the config)."""

    port: int
    key: str
    env: dict
    tmp: tempfile.TemporaryDirectory

    @classmethod
    def token(cls, name, events_line=None):
        """name's token (its events token, reporting to events_line), issued the first time."""
        cache = cls.__dict__.get("_tokens")
        if cache is None:
            cache = cls._tokens = {}
        key = (name, events_line)
        if key not in cache:
            extra = ["--events", "--line", events_line] if events_line else []
            r = subprocess.run([sys.executable, "-m", "nanotea", "token", "add", name, *extra], env=cls.env,
                               cwd=cls.tmp.name, capture_output=True, text=True, timeout=60)
            if r.returncode:
                raise RuntimeError(f"nanotea token add {name}: {r.stderr}")
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
            cache[key] = (Path(cls.tmp.name) / "tokens" / f"{slug}{'.events' if events_line else ''}.token"
                          ).read_text().strip()
        return cache[key]

    @classmethod
    def env_for(cls, name):
        """The environment of a program that names no agent itself (nanotea mcp, joining by tool): the file of
        name's token."""
        cls.token(name)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
        return {**cls.env, "NANOTEA_TOKEN_FILE": str(Path(cls.tmp.name) / "tokens" / f"{slug}.token")}

    def named(self, method, path, body):
        """The agent a request is for, from the name it gives; or the owner's view of who sent a message."""
        url = urlsplit(path)
        if isinstance(body, dict):
            for k in ("from", "name", "source"):
                if isinstance(body.get(k), str):
                    return body[k].strip()
        if names := parse_qs(url.query).get("name"):
            return names[0].strip()
        if m := re.match(r"/api/agents/([^/]+)/", url.path):
            return unquote(m[1])
        if m := re.fullmatch(r"/api/messages/([0-9a-z-]+)", url.path):
            status, out = self.request("GET", url.path, None, {"Authorization": f"Bearer {self.key}"})
            if status == 200:
                return json.loads(out)["sender"]
        return "tester"

    def local(self, method, path, body=None, as_=None):
        """As an agent, with its token. as_: which agent; by default the one the request names."""
        return self.bearer(method, path, body, self.token(as_ or self.named(method, path, body)))

    def bearer(self, method, path, body=None, secret=None):
        return self.request(method, path, body, {"Authorization": f"Bearer {secret}"} if secret else {})

    def owner(self, method, path, body=None):
        """As the owner's own commands: the pairing key as a bearer, no proxy."""
        return self.bearer(method, path, body, self.key)


class Case(Tokens, unittest.TestCase):
    PORT_OFFSET = 1
    EXTRA = ""  # more config, appended

    @classmethod
    def edit_config(cls, text: str) -> str:
        """The config the class runs with, from the shared one."""
        return text

    @classmethod
    def setUpClass(cls):
        cls.port = BASE_PORT + cls.PORT_OFFSET
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / "env").write_text("X=1\n")
        cls.notes_path = tmp / "notes.jsonl"
        config = tmp / "config.toml"
        config.write_text(cls.edit_config(CONFIG.format(port=cls.port, data=tmp / "data", env=tmp / "env",
                                                        py=sys.executable, root=ROOT, tests=ROOT / "tests",
                                                        notes=cls.notes_path, extra=cls.EXTRA)))
        cls.env = {**os.environ, "NANOTEA_CONFIG": str(config), "TMPDIR": str(tmp)}
        cls.proc, cls.log, cls.reader = spawn(cls.env, tmp)
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/api/voices")
                conn.getresponse().read()
                conn.close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("service did not start")
        cls.key = (tmp / "data" / "reply_key").read_text().strip()
        cls.free = [f"v{n}" for n in range(1, 13)]
        cls.voice_of = {}

    @classmethod
    def tearDownClass(cls):
        stop(cls.proc, cls.reader)
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {**({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})})
        r = conn.getresponse()
        data = r.read().decode()
        conn.close()
        return r.status, data

    def call(self, method, path, body=None, paired=True):
        """As the owner's phone: through the proxy, with the pairing cookie."""
        return self.request(method, path, body, {**PROXY, **({"Cookie": f"{COOKIE}={self.key}"} if paired else {})})

    def wait_for(self, check, what):
        deadline = time.time() + 20
        while time.time() < deadline:
            if got := check():
                return got
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def agent(self, name):
        """An agent's voice: each name gets one of its own, as the service wants."""
        if name not in self.voice_of:
            self.voice_of[name] = self.free.pop(0)
        return self.voice_of[name]

    def send(self, sender, text, ask=False, channel=None, title=None):
        """An agent posts; returns the message's id once it is delivered (rewritten and notified)."""
        body = {"text": text, "from": sender, "voice": self.agent(sender), "ask": ask,
                **({"channel": channel} if channel else {}), **({"title": title} if title else {})}
        status, out = self.local("POST", "/api/messages", body)
        self.assertEqual(status, 202, out)
        msg_id = json.loads(out)["id"]
        self.wait_for(lambda: json.loads(self.local("GET", f"/api/messages/{msg_id}", as_=sender)[1])["delivered"],
                      f"{msg_id} to be delivered")
        return msg_id

    def notes(self):
        if not self.notes_path.exists():
            return []
        return [json.loads(line) for line in self.notes_path.read_text().splitlines()]

    def flush(self):
        """After everything queued so far has been processed: the worker handles messages in order, so a
        message nobody mutes, once notified, means every earlier one is done."""
        tag = self.send("flusher", "flush", title="flush")
        self.wait_for(lambda: any(n["tag"] == tag for n in self.notes()), "the flush note")
