"""The container's example config builds its plugins, and compose.yaml agrees with it. Docker itself isn't run."""

import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "docs" / "docker.config.toml"
COMPOSE = (ROOT / "compose.yaml").read_text()


class Kokoro(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        voices = [{"id": v, "name": v} for v in ("af_heart", "bm_george")]
        body = json.dumps({"voices": voices}).encode()
        self.send_response(200 if self.path == "/v1/audio/voices" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DockerConfigTests(unittest.TestCase):
    def test_example_builds_against_a_kokoro_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Kokoro)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        text = EXAMPLE.read_text()
        self.assertIn('url = "http://kokoro:8880"', text)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "env").write_text("")
            config = tmp / "config.toml"
            config.write_text(text.replace("http://kokoro:8880", f"http://127.0.0.1:{server.server_address[1]}")
                              .replace('data_dir = "/data"', f'data_dir = "{tmp / "data"}"'))
            proc = subprocess.run([sys.executable, "-m", "nanotea.cli", "plugins", "--check"], text=True,
                                  capture_output=True, env={**os.environ, "NANOTEA_CONFIG": str(config)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ok      tts kokoro-server: 2 voices, .wav", proc.stdout)
        self.assertIn("ok      stt command", proc.stdout)

    def test_compose_matches_the_example(self):
        cfg = tomllib.loads(EXAMPLE.read_text())
        port = str(cfg["port"])
        self.assertEqual(re.findall(r"NANOTEA_PORT:-(\d+)", COMPOSE), [port] * 2)
        # Published to the host's loopback only.
        self.assertIn(f'"127.0.0.1:${{NANOTEA_PORT:-{port}}}:${{NANOTEA_PORT:-{port}}}"', COMPOSE)
        # The service listens on the container's interfaces; a proxy on the host arrives from the gateway.
        self.assertEqual(cfg["host"], "0.0.0.0")
        gateway = re.search(r"gateway: (\S+)", COMPOSE)[1]
        self.assertEqual(cfg["trusted_proxies"], [gateway])
        self.assertTrue(ipaddress.ip_address(gateway) in ipaddress.ip_network(re.search(r"subnet: (\S+)", COMPOSE)[1]))

    def test_images_are_pinned(self):
        tags = re.findall(r"image: (ghcr\.io/remsky/\S+)", COMPOSE)
        self.assertEqual(len(tags), 2)
        for image in tags:
            self.assertRegex(image, r":v\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
