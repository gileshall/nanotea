"""How browsers reach the service: the config's checks, nanotea init, and a service on plain http with no proxy."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from harness import COOKIE, ROOT, Case
from nanotea.config import ConfigError, check
from nanotea.init_cmd import config_for
from nanotea.plugins import Context
from nanotea.server import Server, reach_warnings

MINIMAL = {"public_url": "http://127.0.0.1:7447", "app": {"owner": "Robin"}}


def minimal(**more):
    cfg = json.loads(json.dumps(MINIMAL))
    for key, value in more.items():
        cfg[key] = value
    return cfg


class Config(unittest.TestCase):
    def test_a_minimal_config_gets_the_documented_defaults(self):
        cfg = check(minimal())
        self.assertEqual({k: cfg[k] for k in ("host", "port", "trusted_proxies", "data_dir", "plugin_path")},
                         {"host": "127.0.0.1", "port": 7447, "trusted_proxies": [], "data_dir": "data",
                          "plugin_path": []})
        self.assertNotIn("env_file", cfg)
        self.assertEqual(cfg["app"], {"owner": "Robin", "name": "Nanotea", "channels": {}, "groups": []})
        self.assertEqual(cfg["notify"], {"now": [], "escalate": [], "escalate_after_min": 30})

    def test_mistakes_are_named(self):
        for cfg, says in ((minimal(prot=1), "unknown key 'prot'"),
                          ({"app": {"owner": "R"}}, "public_url is required"),
                          (minimal(app={}), r"\[app\] owner is required"),
                          (minimal(app={"owner": "R", "colour": 1}), r"unknown key \[app\] 'colour'"),
                          (minimal(port="7447"), "port must be a whole number"),
                          (minimal(port=70000), "port must be 1 to 65535"),
                          (minimal(trusted_proxies=["proxy.local"]), "trusted_proxies"),
                          (minimal(tts="say"), r"tts must be a table, \[tts\]"),
                          (minimal(public_url="https://x.example.com/"), "scheme and host only"),
                          (minimal(public_url="nanotea.example.com"), "scheme and host only"),
                          (minimal(public_url="https://x.example.com:443"), "without a default port"),
                          (minimal(public_url="https://X.example.com"), "in lower case"),
                          (minimal(notify={"now": ["push"]}), r"\[notify.push\] subject is required"),
                          (minimal(notify={"escalate": ["push"], "push": {"subject": "me@example.com"}}),
                           r"\[notify.push\] subject is required"),
                          (minimal(notify={"now": ["push"], "push": {"subject": "https://x.example.com:8443"}}),
                           "refused by the push library")):
            with self.assertRaisesRegex(ConfigError, says, msg=cfg):
                check(cfg)
        check(minimal(notify={"now": ["push"], "push": {"subject": "mailto:me@example.com"}}))

    def test_push_subjects_are_those_the_push_library_signs(self):
        from py_vapid import _check_sub
        from nanotea.config import PUSH_SUBJECT
        for subject in ("mailto:me@example.com", "mailto:me@localhost", "https://x.example.com", "https://localhost",
                        "https://192.168.1.5", "https://x.example.com:8443", "https://x.example.com/nanotea",
                        "https://x", "mailto:me", "mailto:@example.com", "https://[::1]:443"):
            self.assertEqual(bool(PUSH_SUBJECT.match(subject)), _check_sub(subject), subject)

    def test_the_shipped_configs_pass(self):
        for name in ("nanotea/config.example.toml", "docs/docker.config.toml"):
            check(tomllib.loads((ROOT / name).read_text()))

    def test_a_missing_program_stops_the_service(self):
        ctx = Context(kind="tts", name="command", env={}, owner="R", app_name="N", data=Path("/nonexistent"))
        self.assertEqual(ctx.program({"argv": ["sh", "-c", "x"]}), ["sh", "-c", "x"])
        with self.assertRaisesRegex(ConfigError, r"\[tts.command\] argv: 'no-such-voice' is not on this service's "
                                                 "PATH"):
            ctx.program({"argv": ["no-such-voice", "{out}"]})
        with self.assertRaisesRegex(ConfigError, "is not a program"):
            ctx.program({"argv": ["./no/such/voice"]})
        with self.assertRaisesRegex(ConfigError, "a program and its arguments"):
            ctx.program({"argv": []})
        with self.assertRaisesRegex(ConfigError, r"\[tts.command\] needs argv"):
            ctx.program({})

    def test_warnings_for_addresses_browsers_cannot_use_fully(self):
        def warned(**cfg):
            return " ".join(reach_warnings({"host": "127.0.0.1", "port": 7447, **cfg}))
        for url in ("http://127.0.0.1:7447", "http://localhost:7447", "http://nanotea.localhost:7447",
                    "https://box.tail1234.ts.net"):
            self.assertEqual(warned(public_url=url), "", url)
        self.assertIn("plain http on 192.168.1.5", warned(public_url="http://192.168.1.5:8080", host="0.0.0.0"))
        lan = warned(public_url="http://192.168.1.5:7447")
        self.assertIn("plain http on 192.168.1.5", lan)
        self.assertIn('listens only on this machine, so 192.168.1.5 can\'t reach it. Set host = "0.0.0.0"', lan)


class Bind(unittest.TestCase):
    def test_listening_asks_dns_nothing(self):
        """A reverse lookup of the host stalled every start for half a minute on some Macs."""
        def lookup(*a):
            raise AssertionError("the service asked DNS for its host's name")
        with mock.patch("socket.getfqdn", lookup):
            server = Server(("127.0.0.1", 0), app=None)
        try:
            with socket.create_connection(server.server_address, timeout=5):
                pass
            self.assertEqual(server.server_name, "127.0.0.1")
        finally:
            server.server_close()


class Init(unittest.TestCase):
    def config(self, url, say=True, espeak=False):
        return tomllib.loads(config_for("Robin", url, say, espeak))

    def test_this_machine_only(self):
        cfg = self.config("http://127.0.0.1:7447")
        self.assertEqual((cfg["host"], cfg["port"], cfg["trusted_proxies"]), ("127.0.0.1", 7447, []))
        self.assertEqual((cfg["app"]["owner"], cfg["notify"]["now"]), ("Robin", []))
        self.assertNotIn("env_file", cfg)
        self.assertEqual(self.config("http://localhost:8000")["port"], 8000)

    def test_https_in_front(self):
        cfg = self.config("https://box.tail1234.ts.net")
        self.assertEqual((cfg["host"], cfg["port"], cfg["trusted_proxies"]), ("127.0.0.1", 7447, ["127.0.0.1"]))
        self.assertEqual(cfg["notify"]["now"], ["push"])
        self.assertEqual(cfg["notify"]["push"]["subject"], "https://box.tail1234.ts.net")
        self.assertEqual(self.config("https://box.tail1234.ts.net:8443")["notify"]["push"]["subject"],
                         "https://box.tail1234.ts.net")

    def test_plain_http_on_a_network(self):
        cfg = self.config("http://192.168.1.5:7447")
        self.assertEqual((cfg["host"], cfg["port"], cfg["notify"]["now"]), ("0.0.0.0", 7447, []))
        with self.assertRaisesRegex(ConfigError, "give the port"):
            self.config("http://192.168.1.5")

    def test_the_voice_is_one_this_machine_has(self):
        self.assertEqual(self.config(MINIMAL["public_url"])["tts"]["command"]["argv"][0], "say")
        self.assertEqual(self.config(MINIMAL["public_url"], say=False, espeak=True)["tts"]["command"]["argv"][0],
                         "espeak-ng")

    def test_the_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "NANOTEA_CONFIG": str(Path(tmp, "config.toml"))}
            run = lambda *args: subprocess.run([sys.executable, "-m", "nanotea", "init", *args], env=env,  # noqa: E731
                                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            self.assertIn("--owner is needed", run().stderr)
            self.assertIn("--url is needed", run("--owner", "Robin").stderr)
            self.assertIn("in lower case", run("--owner", "Robin", "--url", "https://A.example.com").stderr)
            self.assertFalse(Path(tmp, "config.toml").exists())
            made = run("--owner", "Robin", "--url", "https://a.example.com/")
            self.assertEqual(made.returncode, 0, made.stderr)
            self.assertIn("forward to http://127.0.0.1:7447", made.stdout)
            self.assertEqual(tomllib.loads(Path(tmp, "config.toml").read_text())["public_url"],
                             "https://a.example.com")
            self.assertIn("exists", run("--owner", "Robin", "--url", "https://a.example.com").stderr)


class Direct(Case):
    """No proxy: the browser on this machine opens public_url, http://127.0.0.1:<port>, itself."""
    PORT_OFFSET = 12

    @classmethod
    def edit_config(cls, text):
        for old, new in (('public_url = "https://nanotea.test"', f'public_url = "http://127.0.0.1:{cls.port}"'),
                         ('trusted_proxies = ["127.0.0.1"]', "trusted_proxies = []")):
            assert old in text, old
            text = text.replace(old, new)
        return text

    def browser(self, path, host=None, paired=True):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}",
                                           **({"Cookie": f"{COOKIE}={self.key}"} if paired else {})})
        r = conn.getresponse()
        out = (r.status, dict(r.getheaders()), r.read().decode())
        conn.close()
        return out

    def test_an_unpaired_browser_is_told_to_pair_not_sent_round(self):
        status, _, page = self.browser("/", paired=False)
        self.assertEqual(status, 403)
        self.assertIn("pair", page.lower())
        status, headers, _ = self.browser("/waiting", host=f"localhost:{self.port}", paired=False)
        self.assertEqual((status, headers["Location"]), (302, f"http://127.0.0.1:{self.port}/waiting"))

    def test_the_pairing_cookie_is_kept_over_http(self):
        status, headers, _ = self.browser(f"/?k={self.key}", paired=False)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly; SameSite=Lax", headers["Set-Cookie"])
        self.assertNotIn("Secure", headers["Set-Cookie"])

    def test_the_manifest_is_the_owners(self):
        status, _, body = self.browser("/manifest.webmanifest")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["name"], "Teapot")
        self.assertEqual(self.local("GET", "/manifest.webmanifest")[0], 403)

    def test_without_push_the_page_and_the_service_say_so(self):
        self.assertIn('data-vapid=""', self.browser("/")[2])
        status, out = self.request("POST", "/api/push/subscribe", {"endpoint": "https://push.example/x"},
                                   {"Cookie": f"{COOKIE}={self.key}"})
        self.assertEqual(status, 409)
        self.assertIn('no "push" in [notify] now', json.loads(out)["error"])

    def test_the_log_says_how_it_is_reached(self):
        line = next(ln for ln in self.log if "listening on" in ln)
        self.assertIn(f"public http://127.0.0.1:{self.port}; proxies none", line)
        self.assertFalse([ln for ln in self.log if "plain http" in ln])
