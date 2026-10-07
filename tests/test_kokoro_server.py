"""The kokoro-server tts plugin, against a fake server that answers as Kokoro-FastAPI does (GET /v1/audio/voices,
POST /v1/audio/speech)."""

import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from nanotea import plugins
from nanotea.config import ConfigError

VOICES = {"voices": [{"id": "af_heart", "name": "af_heart", "overall_grade": "A"},
                     {"id": "bm_george", "name": "bm_george"}], "default_voice": "af_heart"}
AUDIO = b"RIFF fake wav"


class Fake(BaseHTTPRequestHandler):
    """Behaves by what the test set on the server: `mode`."""

    def log_message(self, *args):
        pass

    def reply(self, code: int, ctype: str, body: bytes, length: int | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.seen.append(("GET", self.path, None))
        mode = self.server.mode
        if self.path != "/v1/audio/voices":
            return self.reply(404, "application/json", b'{"detail": "Not Found"}')
        if mode == "voices-error":
            return self.reply(500, "application/json", json.dumps(
                {"detail": {"error": "server_error", "message": "Failed to retrieve voice list",
                            "type": "server_error"}}).encode())
        if mode == "voices-shape":
            return self.reply(200, "application/json", b'{"voices": ["af_heart"]}')
        if mode == "voices-none":
            return self.reply(200, "application/json", b'{"voices": []}')
        if mode == "html":
            return self.reply(200, "text/html", b"<html>")
        self.reply(200, "application/json", json.dumps(VOICES).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(("POST", self.path, body))
        mode = self.server.mode
        if self.path != "/v1/audio/speech":
            return self.reply(404, "application/json", b'{"detail": "Not Found"}')
        if mode == "html":
            return self.reply(200, "text/html", b"<html>")
        if mode == "empty":
            return self.reply(200, "audio/wav", b"")
        if mode == "cut":
            return self.reply(200, "audio/wav", AUDIO, length=len(AUDIO) + 100)
        if mode == "slow":
            self.server.release.wait(10)
            return
        if mode == "error":
            return self.reply(500, "application/json", json.dumps(
                {"detail": {"error": "processing_error", "message": "model fell over",
                            "type": "server_error"}}).encode())
        if mode == "plain-error":
            return self.reply(502, "text/plain", b"bad gateway")
        if body["voice"] not in {v["id"] for v in VOICES["voices"]}:
            return self.reply(400, "application/json", json.dumps(
                {"detail": {"error": "validation_error",
                            "message": f"Voice '{body['voice']}' not found. Available voices: af_heart, bm_george",
                            "type": "invalid_request_error"}}).encode())
        self.reply(200, "audio/wav", AUDIO)


class KokoroServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.server.daemon_threads = True
        cls.server.release = threading.Event()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.server.mode = "ok"
        self.server.seen = []

    def build(self, table: dict):
        ctx = {"env": {}, "owner": "Robin", "app_name": "Teapot", "data": self.dir, "services": {}}
        return plugins.make("tts", {"backend": "kokoro-server", "kokoro-server": table}, ctx)

    def make(self, **table):
        return self.build({"url": self.url, **table})

    def test_speaks(self):
        kokoro = self.make()
        out = self.dir / "speech.wav"
        kokoro.synthesize("Hello there.", out, "bm_george")
        self.assertEqual((kokoro.ext, out.read_bytes()), ("wav", AUDIO))
        self.assertEqual(self.server.seen, [("POST", "/v1/audio/speech", {
            "model": "kokoro", "input": "Hello there.", "voice": "bm_george", "response_format": "wav",
            "speed": 1.0, "stream": False})])

    def test_settings_reach_the_server(self):
        kokoro = self.make(url=self.url + "/", model="tts-1-hd", format="flac", speed=1.25, timeout_s=30)
        kokoro.synthesize("Hi.", self.dir / "speech.flac", "af_heart")
        body = self.server.seen[0][2]
        self.assertEqual((kokoro.ext, kokoro.url, body["model"], body["response_format"], body["speed"]),
                         ("flac", self.url, "tts-1-hd", "flac", 1.25))

    def test_voices_come_from_the_server(self):
        self.assertEqual(self.make().voices(), [{"id": "af_heart", "name": "af_heart"},
                                                {"id": "bm_george", "name": "bm_george"}])
        self.server.mode = "voices-shape"
        with self.assertRaisesRegex(RuntimeError, "did not list voices as Kokoro-FastAPI does"):
            self.make().voices()
        self.server.mode = "voices-none"
        with self.assertRaisesRegex(RuntimeError, "lists no voices"):
            self.make().voices()
        self.server.mode = "voices-error"
        with self.assertRaisesRegex(RuntimeError, "Kokoro HTTP 500: Failed to retrieve voice list"):
            self.make().voices()

    def test_unreachable(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{s.getsockname()[1]}"
        kokoro = self.make(url=url)
        with self.assertRaisesRegex(RuntimeError, r"can't reach the Kokoro server at .*start it, or fix "
                                                  r"\[tts.kokoro-server\] url"):
            kokoro.voices()
        out = self.dir / "never.wav"
        with self.assertRaisesRegex(RuntimeError, "can't reach the Kokoro server"):
            kokoro.synthesize("Hi.", out, "af_heart")
        self.assertFalse(out.exists())

    def test_http_errors(self):
        out = self.dir / "err.wav"
        for mode, message in [("error", "Kokoro HTTP 500: model fell over"),
                              ("plain-error", "Kokoro HTTP 502: bad gateway")]:
            self.server.mode = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, message):
                self.make().synthesize("Hi.", out, "af_heart")
        self.assertFalse(out.exists())

    def test_unknown_voice(self):
        out = self.dir / "nobody.wav"
        with self.assertRaisesRegex(RuntimeError, r"Kokoro HTTP 400: Voice 'nobody' not found\. Available voices"):
            self.make().synthesize("Hi.", out, "nobody")
        self.assertFalse(out.exists())

    def test_bad_replies(self):
        out = self.dir / "bad.wav"
        for mode, message in [("html", "answered text/html, not audio"), ("empty", "Kokoro returned no audio"),
                              ("cut", "dropped the request: IncompleteRead")]:
            self.server.mode = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, message):
                self.make().synthesize("Hi.", out, "af_heart")
        self.assertFalse(out.exists())

    def test_timeout(self):
        self.server.mode = "slow"
        with self.assertRaisesRegex(RuntimeError, r"took over 1 s; raise \[tts.kokoro-server\] timeout_s"):
            self.make(timeout_s=1).synthesize("Hi.", self.dir / "slow.wav", "af_heart")
        self.server.release.set()
        self.server.release = threading.Event()

    def test_config_errors(self):
        ok = {"url": self.url}
        cases = [
            ({}, r"\[tts.kokoro-server\] needs url"),
            ({"url": 8880}, r"\[tts.kokoro-server\] url has the wrong type: int"),
            ({"url": "127.0.0.1:8880"}, r"\[tts.kokoro-server\] url must be the server's address"),
            ({"url": "ftp://127.0.0.1"}, r"\[tts.kokoro-server\] url must be the server's address"),
            ({"url": "http://127.0.0.1:8880/v1"}, r"\[tts.kokoro-server\] url must be the server's address"),
            ({"url": "http://127.0.0.1:port"}, r"\[tts.kokoro-server\] url 'http://127.0.0.1:port' is not an address"),
            ({**ok, "model": 1}, r"\[tts.kokoro-server\] model has the wrong type: int"),
            ({**ok, "model": ""}, r"\[tts.kokoro-server\] model must not be empty"),
            ({**ok, "format": "pcm"},
             r"\[tts.kokoro-server\] format must be one of wav, flac, mp3, opus, aac; not 'pcm'"),
            ({**ok, "format": 3}, r"\[tts.kokoro-server\] format has the wrong type: int"),
            ({**ok, "speed": "fast"}, r"\[tts.kokoro-server\] speed has the wrong type: str"),
            ({**ok, "speed": True}, r"\[tts.kokoro-server\] speed must be a number from 0.25 to 4.0"),
            ({**ok, "speed": 5}, r"\[tts.kokoro-server\] speed must be a number from 0.25 to 4.0; not 5"),
            ({**ok, "speed": 0.1}, r"\[tts.kokoro-server\] speed must be a number from 0.25 to 4.0"),
            ({**ok, "timeout_s": 0}, r"\[tts.kokoro-server\] timeout_s must be a positive number of seconds"),
            ({**ok, "timeout_s": 1.5}, r"\[tts.kokoro-server\] timeout_s has the wrong type: float"),
            ({**ok, "timeout_s": True}, r"\[tts.kokoro-server\] timeout_s must be a positive number of seconds"),
            ({**ok, "voice": "af_heart"},
             r"\[tts.kokoro-server\] has no key voice; keys are url, model, format, speed, timeout_s"),
        ]
        for table, message in cases:
            with self.subTest(table=table), self.assertRaisesRegex(ConfigError, message):
                self.build(table)

    def test_registered_and_described(self):
        found = plugins.find("tts", "kokoro-server")
        self.assertEqual(found.origin, "built-in")
        self.assertTrue(found.about().startswith("A Kokoro text-to-speech server you run yourself"))


if __name__ == "__main__":
    unittest.main()
