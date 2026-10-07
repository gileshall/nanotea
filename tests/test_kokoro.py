"""The local kokoro tts plugin and `nanotea kokoro download`. Nothing here needs the kokoro-onnx package or the
network: the model's inference call is faked, and downloads come from a local server. One test runs the real
model when the extra and the files are there."""

import contextlib
import hashlib
import importlib.util
import io
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
import zipfile
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from nanotea import kokoro as files
from nanotea import plugins
from nanotea.config import ConfigError

FFMPEG = shutil.which("ffmpeg") is not None
FFPROBE = shutil.which("ffprobe") is not None
VOICE_IDS = ["af_heart", "am_adam", "bf_emma", "bm_george", "jf_alpha", "zf_xiaobei", "ef_dora", "ff_siwis"]
MODEL = b"not a real model " * 100
SAMPLES = array("f", (0.5 * math.sin(i / 10) for i in range(4800)))


def voices_file(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(f"{n}.npy", b"x")
    return buf.getvalue()


def pinned(model: bytes, voices: bytes, base: str = "http://unused") -> tuple[files.File, ...]:
    return (files.File(files.MODEL, f"{base}/{files.MODEL}", len(model), hashlib.sha256(model).hexdigest()),
            files.File(files.VOICES, f"{base}/{files.VOICES}", len(voices), hashlib.sha256(voices).hexdigest()))


def chunks(data: bytes) -> dict[bytes, bytes]:
    """The chunks of a RIFF file by id."""
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    found, pos = {}, 12
    while pos < len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        found[cid] = data[pos + 8:pos + 8 + size]
        pos += 8 + size + size % 2
    return found


class Engine:
    """Stands for kokoro_onnx.Kokoro: keeps what it was asked and watches for overlapping calls."""
    loads = 0
    active = 0
    overlapped = False
    calls: list = []

    def __init__(self, model_path, voices_path):
        time.sleep(0.05)  # long enough for a second thread to try loading too
        type(self).loads += 1
        self.paths = (model_path, voices_path)

    def create(self, text, voice, speed, lang):
        type(self).active += 1
        type(self).overlapped |= type(self).active > 1
        time.sleep(0.01)
        type(self).calls.append((text, voice, speed, lang))
        type(self).active -= 1
        return SAMPLES, 24000


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name, "models")
        self.dir.mkdir()
        self.voices = voices_file(VOICE_IDS)
        (self.dir / files.MODEL).write_bytes(MODEL)
        (self.dir / files.VOICES).write_bytes(self.voices)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(files, "FILES", pinned(MODEL, self.voices)))
        self.addCleanup(self.stack.close)
        self.addCleanup(self.tmp.cleanup)
        Engine.loads, Engine.active, Engine.overlapped, Engine.calls = 0, 0, False, []

    def has_extra(self, present: bool = True):
        real = importlib.util.find_spec
        self.stack.enter_context(mock.patch("importlib.util.find_spec", lambda name, *a: (
            (object() if present else None) if name == "kokoro_onnx" else real(name, *a))))

    def build(self, **table):
        ctx = {"env": {}, "owner": "Robin", "app_name": "Teapot", "data": Path(self.tmp.name), "services": {}}
        return plugins.make("tts", {"backend": "kokoro", "kokoro": {"model_dir": str(self.dir), **table}}, ctx)


class Config(Base):
    def test_config_errors(self):
        self.has_extra()
        cases = [
            ({"voice": "af_heart"}, r"\[tts.kokoro\] has no key voice; keys are model_dir, format, speed"),
            ({"model_dir": 3}, r"\[tts.kokoro\] model_dir must be a directory path; not 3"),
            ({"model_dir": ""}, r"\[tts.kokoro\] model_dir must be a directory path"),
            ({"format": "mp3"}, r"\[tts.kokoro\] format must be one of float32, pcm24; not 'mp3'"),
            ({"format": 24}, r"\[tts.kokoro\] format has the wrong type: int"),
            ({"speed": "fast"}, r"\[tts.kokoro\] speed has the wrong type: str"),
            ({"speed": True}, r"\[tts.kokoro\] speed must be a number from 0.5 to 2.0"),
            ({"speed": 0.25}, r"\[tts.kokoro\] speed must be a number from 0.5 to 2.0; not 0.25"),
            ({"speed": 2.5}, r"\[tts.kokoro\] speed must be a number from 0.5 to 2.0; not 2.5"),
        ]
        for table, message in cases:
            with self.subTest(table=table), self.assertRaisesRegex(ConfigError, message):
                self.build(**table)

    def test_missing_extra(self):
        self.has_extra(False)
        with self.assertRaisesRegex(ConfigError, r"\[tts.kokoro\] needs the kokoro-onnx package: install it with "
                                                 r"`uv sync --extra kokoro` \(from a checkout\) or "
                                                 r"`uv tool install 'nanotea\[kokoro\]'`"):
            self.build()

    def test_missing_ffmpeg(self):
        self.has_extra()
        with mock.patch("shutil.which", return_value=None):
            self.build()  # float32 is written without it
            with self.assertRaisesRegex(ConfigError, r"\[tts.kokoro\] format pcm24 needs ffmpeg"):
                self.build(format="pcm24")

    def test_files_missing_or_wrong(self):
        self.has_extra()
        (self.dir / files.MODEL).unlink()
        with self.assertRaisesRegex(ConfigError, rf"model files in {self.dir}: {files.MODEL} is missing\. "
                                                 r"Fetch them, once, with: nanotea kokoro download"):
            self.build()
        (self.dir / files.MODEL).write_bytes(MODEL + b"!")
        with self.assertRaisesRegex(ConfigError, rf"{files.MODEL} has {len(MODEL) + 1} bytes, not {len(MODEL)}"):
            self.build()
        (self.dir / files.MODEL).write_bytes(b"x" * len(MODEL))
        (self.dir / files.VOICES).unlink()
        with self.assertRaisesRegex(ConfigError, rf"{files.MODEL} fails its SHA-256 check; {files.VOICES} is missing"):
            self.build()

    def test_default_dir_is_empty_until_downloaded(self):
        self.has_extra()
        cache = Path(self.tmp.name, "cache")
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
            self.assertEqual(files.default_dir(), cache / "nanotea" / "kokoro")
            with self.assertRaisesRegex(ConfigError, rf"model files in {cache}/nanotea/kokoro: .*is missing"):
                plugins.make("tts", {"backend": "kokoro"}, {"env": {}, "owner": "R", "app_name": "T",
                                                            "data": Path(self.tmp.name), "services": {}})
        self.assertFalse(cache.exists())  # nothing fetched or made behind the owner's back

    def test_model_dir_is_relative_to_the_config(self):
        with mock.patch("nanotea.config.BASE", Path("/etc/nanotea")):
            self.assertEqual(files.model_dir({"model_dir": "models"}, "[tts.kokoro]"), Path("/etc/nanotea/models"))

    def test_voices(self):
        self.has_extra()
        got = self.build().voices()
        self.assertEqual(got, [
            {"id": "af_heart", "name": "Heart", "gender": "female", "locale": "en-US"},
            {"id": "am_adam", "name": "Adam", "gender": "male", "locale": "en-US"},
            {"id": "bf_emma", "name": "Emma", "gender": "female", "locale": "en-GB"},
            {"id": "bm_george", "name": "George", "gender": "male", "locale": "en-GB"},
        ])

    def test_voices_file_without_english_or_not_a_voices_file(self):
        self.has_extra()
        for data, message in [(voices_file(["jf_alpha", "ff_siwis"]), "has no English voices"),
                              (b"not a zip", "is not a voices file")]:
            (self.dir / files.VOICES).write_bytes(data)
            with self.subTest(message=message), \
                    mock.patch.object(files, "FILES", pinned(MODEL, data)), \
                    self.assertRaisesRegex(ConfigError, message):
                self.build()

    def test_registered_and_described(self):
        for name in ("kokoro", "kokoro-server"):
            self.assertEqual(plugins.find("tts", name).origin, "built-in")
        self.assertTrue(plugins.find("tts", "kokoro").about().startswith("Kokoro text-to-speech run inside"))
        self.assertNotEqual(plugins.find("tts", "kokoro").about(), plugins.find("tts", "kokoro-server").about())


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class Synthesis(Base):
    def setUp(self):
        super().setUp()
        self.has_extra()
        self.stack.enter_context(mock.patch.dict(sys.modules, {"kokoro_onnx": types.SimpleNamespace(Kokoro=Engine)}))

    def test_float32_wav_is_the_models_samples(self):
        kokoro = self.build(speed=1.5)
        out = self.dir / "speech.wav"
        kokoro.synthesize("Hello there.", out, "bf_emma")
        self.assertEqual(Engine.calls, [("Hello there.", "bf_emma", 1.5, "en-gb")])
        self.assertEqual(Engine.loads, 1)
        self.assertEqual(kokoro.ext, "wav")
        parts = chunks(out.read_bytes())
        tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", parts[b"fmt "][:16])
        self.assertEqual((tag, channels, rate, bits), (3, 1, 24000, 32))  # IEEE float, mono, native rate
        self.assertEqual(parts[b"fact"], struct.pack("<I", len(SAMPLES)))
        self.assertEqual(parts[b"data"], SAMPLES.tobytes())  # not one sample changed
        if FFPROBE:  # what other programs make of it
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels",
                                    "-of", "csv=p=0", str(out)], capture_output=True, text=True, check=True)
            self.assertEqual(probe.stdout.strip(), "pcm_f32le,24000,1")
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([files.MODEL, files.VOICES, "speech.wav"]))

    def test_pcm24(self):
        out = self.dir / "speech.wav"
        self.build(format="pcm24").synthesize("Hi.", out, "af_heart")
        self.assertEqual(Engine.calls[0][1:], ("af_heart", 1.0, "en-us"))
        with wave.open(str(out)) as w:
            self.assertEqual((w.getsampwidth(), w.getframerate(), w.getnchannels(), w.getnframes()),
                             (3, 24000, 1, len(SAMPLES)))
            raw = w.readframes(len(SAMPLES))
        first = [int.from_bytes(raw[i * 3:i * 3 + 3], "little", signed=True) for i in range(len(SAMPLES))]
        self.assertTrue(all(abs(v - round(s * 8388607)) <= 1 for v, s in zip(first, SAMPLES)))

    def test_failures_raise(self):
        kokoro = self.build()
        out = self.dir / "never.wav"
        with self.assertRaisesRegex(RuntimeError, "unknown Kokoro voice 'jf_alpha'"):
            kokoro.synthesize("Hi.", out, "jf_alpha")
        with mock.patch.object(Engine, "create", side_effect=ValueError("Nothing to synthesize")), \
                self.assertRaisesRegex(RuntimeError, "Kokoro can't speak this: Nothing to synthesize"):
            kokoro.synthesize("...", out, "af_heart")
        for bad in (array("d", [0.1, 0.2]), array("f"), [0.1, 0.2]):
            with self.subTest(bad=type(bad).__name__), \
                    mock.patch.object(Engine, "create", return_value=(bad, 24000)), \
                    self.assertRaises((RuntimeError, TypeError)):
                kokoro.synthesize("Hi.", out, "af_heart")
        self.assertFalse(out.exists() or out.with_name("never.wav.part").exists())

    def test_failures_leave_nothing(self):
        out = self.dir / "never.wav"
        with mock.patch.object(Engine, "create", return_value=(SAMPLES, 0)), \
                self.assertRaisesRegex(RuntimeError, "Kokoro gave a sample rate of 0"):
            self.build().synthesize("Hi.", out, "af_heart")
        failed = subprocess.CompletedProcess([], 1, "", "boom")
        with mock.patch("nanotea.tts.subprocess.run", return_value=failed), \
                self.assertRaisesRegex(RuntimeError, "ffmpeg exited 1: boom"):
            self.build(format="pcm24").synthesize("Hi.", out, "af_heart")
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([files.MODEL, files.VOICES]))

    def test_load_failure(self):
        with mock.patch.object(Engine, "__init__", side_effect=OSError("bad onnx")), \
                self.assertRaisesRegex(RuntimeError, r"can't load the Kokoro model from .*OSError: bad onnx"):
            self.build().synthesize("Hi.", self.dir / "x.wav", "af_heart")

    def test_loaded_once_and_one_synthesis_at_a_time(self):
        kokoro = self.build()
        self.assertEqual(Engine.loads, 0)  # starting the service doesn't load it
        errors = []

        def speak(n):
            try:
                kokoro.synthesize(f"line {n}", self.dir / f"t{n}.wav", "af_heart")
            except Exception as err:
                errors.append(err)

        threads = [threading.Thread(target=speak, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual((Engine.loads, len(Engine.calls), Engine.overlapped), (1, 8, False))


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        body = self.server.served.get(self.path)
        if body is None or self.server.mode == "404":
            self.send_response(404)
            self.end_headers()
            return
        if self.server.mode == "wrong" and self.path.endswith(files.MODEL):
            body = b"x" * len(body)
        if self.server.mode == "short" and self.path.endswith(files.MODEL):
            body = body[:-5]
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Download(Base):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.server.daemon_threads = True
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        self.server.mode, self.server.requests = "ok", []
        self.server.served = {f"/{files.MODEL}": MODEL, f"/{files.VOICES}": self.voices}
        self.stack.enter_context(mock.patch.object(files, "FILES", pinned(MODEL, self.voices, self.url)))
        self.target = Path(self.tmp.name, "fresh", "kokoro")  # made by the command
        self.cfg = Path(self.tmp.name, "config.toml")

    def run_main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            files.main(list(argv))
        return err.getvalue()

    def test_downloads_verifies_and_skips(self):
        said = self.run_main("download", "--dir", str(self.target))
        self.assertEqual((self.target / files.MODEL).read_bytes(), MODEL)
        self.assertEqual((self.target / files.VOICES).read_bytes(), self.voices)
        self.assertEqual(sorted(os.listdir(self.target)), sorted([files.MODEL, files.VOICES]))  # no .part left
        self.assertIn(f"model directory: {self.target}", said)
        self.assertIn(f"{files.MODEL}: {len(MODEL) / 1e6:.1f} of {len(MODEL) / 1e6:.1f} MB (100%)", said)
        self.assertIn(f"{files.VOICES}: verified", said)
        self.assertEqual(len(self.server.requests), 2)
        again = self.run_main("download", "--dir", str(self.target))
        self.assertEqual(len(self.server.requests), 2)  # nothing fetched twice
        self.assertIn(f"{files.MODEL}: already here and verified", again)

    def test_refetches_only_what_fails_verification(self):
        self.run_main("download", "--dir", str(self.target))
        (self.target / files.MODEL).write_bytes(b"y" * len(MODEL))
        self.run_main("download", "--dir", str(self.target))
        self.assertEqual(self.server.requests[2:], [f"/{files.MODEL}"])
        self.assertEqual((self.target / files.MODEL).read_bytes(), MODEL)

    def test_the_plugin_accepts_what_was_downloaded(self):
        self.run_main("download", "--dir", str(self.target))
        self.has_extra()
        self.assertEqual(len(self.build(model_dir=str(self.target)).voices()), 4)

    def test_bad_downloads_are_refused_and_leave_nothing(self):
        cases = [("wrong", rf"{files.MODEL} fails its SHA-256 check: got [0-9a-f]{{64}}, expected [0-9a-f]{{64}}"),
                 ("short", rf"{files.MODEL} came to {len(MODEL) - 5} bytes, expected {len(MODEL)}"),
                 ("404", r"answered HTTP 404")]
        for mode, message in cases:
            self.server.mode = mode
            with self.subTest(mode=mode), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaisesRegex(SystemExit, rf"nanotea kokoro: .*{message}"):
                files.main(["download", "--dir", str(self.target)])
            self.assertEqual(os.listdir(self.target), [])

    def test_unreachable(self):
        with socket_closed() as url, mock.patch.object(files, "FILES", pinned(MODEL, self.voices, url)), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(SystemExit, r"nanotea kokoro: can't fetch .*/kokoro-v1.0.onnx: "):
            files.main(["download", "--dir", str(self.target)])

    def test_dir_comes_from_the_config_then_the_default(self):
        self.cfg.write_text('[tts]\nbackend = "kokoro"\n[tts.kokoro]\nmodel_dir = "from-config"\n')
        with mock.patch.object(files, "CONFIG", self.cfg), mock.patch("nanotea.config.BASE", self.cfg.parent):
            said = self.run_main("download")
        self.assertIn(f"model directory: {self.cfg.parent / 'from-config'}", said)
        self.assertTrue((self.cfg.parent / "from-config" / files.MODEL).is_file())
        cache = Path(self.tmp.name, "cache")
        with mock.patch.object(files, "CONFIG", Path(self.tmp.name, "no-such.toml")), \
                mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
            said = self.run_main("download")
        self.assertIn(f"model directory: {cache / 'nanotea' / 'kokoro'}", said)
        self.assertTrue((cache / "nanotea" / "kokoro" / files.VOICES).is_file())

    def test_bad_config_stops_the_command(self):
        self.cfg.write_text('[tts.kokoro]\nmodel_dir = 5\n')
        with mock.patch.object(files, "CONFIG", self.cfg), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(SystemExit, r"nanotea kokoro: \[tts.kokoro\] model_dir must be a directory"):
            files.main(["download"])
        self.assertEqual(self.server.requests, [])

    def test_pinned_files_are_the_real_releases(self):
        pins = shipped_pins()
        self.assertEqual([(f.name, f.size, len(f.sha256)) for f in pins],
                         [("kokoro-v1.0.onnx", 325505369, 64), ("voices-v1.0.bin", 28214398, 64)])
        self.assertTrue(all(f.url.startswith("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                                             "model-files-v1.1/") for f in pins))


def shipped_pins():
    """The pins as shipped, read from a fresh copy of the module."""
    spec = importlib.util.spec_from_file_location("kokoro_pins", files.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FILES


@contextlib.contextmanager
def socket_closed():
    """The address of a port nothing listens on."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{s.getsockname()[1]}"


def real_model_present() -> bool:
    d = Path(os.environ.get("NANOTEA_KOKORO_DIR") or files.default_dir())
    return all((d / f.name).is_file() and (d / f.name).stat().st_size == f.size for f in files.FILES)


@unittest.skipUnless(importlib.util.find_spec("kokoro_onnx") and FFMPEG and real_model_present(),
                     "needs the kokoro extra, ffmpeg and the model files (nanotea kokoro download)")
class RealModel(unittest.TestCase):
    def test_speaks(self):
        d = Path(os.environ.get("NANOTEA_KOKORO_DIR") or files.default_dir())
        ctx = {"env": {}, "owner": "R", "app_name": "T", "data": d, "services": {}}
        kokoro = plugins.make("tts", {"backend": "kokoro", "kokoro": {"model_dir": str(d)}}, ctx)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "speech.wav")
            kokoro.synthesize("Hello. This is a test.", out, "af_heart")
            parts = chunks(out.read_bytes())
            tag, channels, rate = struct.unpack("<HHI", parts[b"fmt "][:8])
            self.assertEqual((tag, channels, rate), (3, 1, 24000))
            self.assertGreater(len(parts[b"data"]) / 4 / rate, 1.0)


if __name__ == "__main__":
    unittest.main()
