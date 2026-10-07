"""A real service on a temporary data directory, for the docs' pictures and video. It is seeded as agents and
the owner would: agents through their tokens, the owner through the proxy headers and the pairing cookie."""

import http.client
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from nanotea.client import Client  # noqa: E402

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
name = "nanotea"
owner = "Sam"
{app}

[control]
use = ["choice", "checklist"]

[rewrite]
backend = "identity"

[tts]
backend = "command"

[tts.command]
argv = ["{py}", "{tests}/fake_tts.py", "{{text_file}}", "{{out}}"]
ext = "m4a"
voices = ["v1", "v2", "v3", "v4", "v5", "v6"]
timeout_s = 60

[stt]
backend = "command"

[stt.command]
argv = ["{py}", "-c", "print('transcript')", "{{audio_file}}"]
timeout_s = 60

[notify]
now = ["push"]
escalate = []
escalate_after_min = 30

[notify.push]
subject = "https://nanotea.test"
"""


class Service:
    """app: the lines of [app] after its name and owner (channels, groups)."""

    def __init__(self, tmp: Path, port: int, app: str):
        self.tmp, self.port = tmp, port
        (tmp / "env").write_text("")
        config = tmp / "config.toml"
        config.write_text(CONFIG.format(port=port, data=tmp / "data", env=tmp / "env", py=sys.executable,
                                        tests=ROOT / "tests", app=app))
        self.env = {**os.environ, "NANOTEA_CONFIG": str(config)}
        self.log = open(tmp / "service.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-m", "nanotea", "serve"], cwd=ROOT, env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=self.log)
        for _ in range(100):
            if self.proc.poll() is not None:
                raise RuntimeError(f"the service exited {self.proc.returncode}: see {tmp / 'service.log'}")
            try:
                http.client.HTTPConnection("127.0.0.1", port, timeout=5).request("GET", "/favicon.ico")
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("the service did not start")
        self.key = (tmp / "data" / "reply_key").read_text().strip()
        self.clients = {}
        self.voices = {}

    def agent(self, name: str) -> Client:
        """name's client, with the token an operator issues it."""
        if name not in self.clients:
            r = subprocess.run([sys.executable, "-m", "nanotea", "token", "add", name], cwd=ROOT, env=self.env,
                               capture_output=True, text=True, timeout=60)
            if r.returncode:
                raise RuntimeError(f"nanotea token add {name}: {r.stderr}")
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
            token = (self.tmp / "tokens" / f"{slug}.token").read_text().strip()
            self.clients[name] = Client(self.port, token, timeout_s=30)
        return self.clients[name]

    def owner(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {**({"Content-Type": "application/json"} if body is not None else {}), **self.headers()})
        r = conn.getresponse()
        data = r.read().decode()
        conn.close()
        if r.status >= 300:
            raise RuntimeError(f"{method} {path}: {r.status} {data}")
        return json.loads(data) if data else None

    def headers(self):
        return {**PROXY, "Cookie": f"nanotea_key={self.key}"}

    def open_line(self, name: str, line: str, session: int):
        """name holds line, with a session open in this process."""
        a = self.agent(name)
        a.post(f"/api/dm-{line}", {"name": name, "open": True})
        a.post("/api/sessions", {"name": name, "session": f"{session:x}", "pid": os.getpid(), "line": line})

    def send(self, sender, text, title, settle=True, **more):
        """An agent posts; returns its id once delivered. settle: wait out the second, as times are to it."""
        voice = self.voices.setdefault(sender, f"v{len(self.voices) + 1}")
        a = self.agent(sender)
        msg_id = a.post("/api/messages", {"text": text, "from": sender, "voice": voice, "title": title,
                                          **more})["id"]
        while not a.get(f"/api/messages/{msg_id}")["delivered"]:
            time.sleep(.1)
        if settle:
            time.sleep(1.1)
        return msg_id

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.log.close()
