"""Text-to-speech plugins (kind tts).

Each has an `ext`, `voices()` (what senders may pick), and `synthesize(script, out, voice)`.
"""

import array
import http.client
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from nanotea import kokoro as files
from nanotea.config import BASE, ConfigError, Option, defaults, unknown_keys
from nanotea.plugins import API, Context


class Speechify:
    """The Speechify API (SPEECHIFY_API_KEY)."""
    api = API
    url = "https://api.speechify.ai/v1"
    ext = "mp3"
    SECRETS = ("SPEECHIFY_API_KEY",)
    OPTIONS = (
        Option("model", str, "The Speechify model. Only voices that support it are offered, such as simba-3.2."),
        Option("timeout_s", int, "Seconds to wait for Speechify before a request fails."),
    )

    def __init__(self, cfg: dict, ctx: Context):
        self.key = ctx.secret("SPEECHIFY_API_KEY")
        self.model = ctx.need(cfg, "model", str)
        self.timeout = ctx.need(cfg, "timeout_s", int)
        self._voices: list[dict] | None = None

    def _request(self, req: urllib.request.Request):
        req.add_header("Authorization", f"Bearer {self.key}")
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Speechify HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e

    def voices(self) -> list[dict]:
        """Voices that support the configured model. Fetched once per process."""
        if self._voices is None:
            found, cursor = [], None
            while True:
                query = urllib.parse.urlencode({"limit": 200, **({"cursor": cursor} if cursor else {})})
                with self._request(urllib.request.Request(f"{self.url}/voices?{query}")) as resp:
                    page = json.load(resp)
                found += [
                    {"id": v["id"], "name": v.get("display_name"), "gender": v.get("gender"),
                     "locale": v.get("locale")}
                    for v in page["voices"]
                    if any(m.get("name") == self.model for m in v.get("models", []))
                ]
                if not page["has_more"]:
                    break
                cursor = page["next_cursor"]
            self._voices = found
        return self._voices

    def synthesize(self, script: str, out: Path, voice: str) -> None:
        req = urllib.request.Request(
            f"{self.url}/audio/stream",
            data=json.dumps({"input": script, "voice_id": voice, "model": self.model}).encode(),
            headers={"Content-Type": "application/json", "Accept": "audio/mpeg"},
            method="POST",
        )
        with self._request(req) as resp, out.open("wb") as f:
            shutil.copyfileobj(resp, f)
        if out.stat().st_size == 0:
            raise RuntimeError("Speechify returned no audio")


class KokoroServer:
    """A Kokoro text-to-speech server you run yourself (Kokoro-FastAPI): its voices, kept as lossless wav.

    `url` is the server's address, such as http://127.0.0.1:8880. The voices are the ones the server lists, so a
    server that is down fails the request instead of guessing. `format` is wav, flac, mp3, opus or aac.
    """
    api = API
    FORMATS = ("wav", "flac", "mp3", "opus", "aac")  # pcm is left out: no header, nothing can play it
    OPTIONS = (
        Option("url", str, "The server's address, such as http://127.0.0.1:8880."),
        Option("model", str, "The model name the server is asked for.", "kokoro"),
        Option("format", str, "The audio the server returns. wav and flac are lossless.", "wav", choices=FORMATS),
        Option("speed", (int, float), "How fast it speaks, as the server's own scale.", 1.0, lo=0.25, hi=4.0),
        Option("timeout_s", int, "Seconds to wait for the server before a request fails.", 600),
    )
    DEFAULTS = defaults(OPTIONS)
    # Straight to the server: a proxy in the environment is for the internet, not for a Kokoro box.
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __init__(self, cfg: dict, ctx: Context):
        where = self.where = f"[{ctx.kind}.{ctx.name}]"
        unknown_keys(cfg, self.OPTIONS, where)
        url = ctx.need(cfg, "url", str)
        try:
            parts = urllib.parse.urlsplit(url)
            parts.port  # raises on a bad port
        except ValueError as err:
            raise ConfigError(f"{where} url {url!r} is not an address: {err}") from err
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/") \
                or parts.query or parts.fragment:
            raise ConfigError(f"{where} url must be the server's address, such as http://127.0.0.1:8880; not {url!r}")
        self.url = url.rstrip("/")
        cfg = {**self.DEFAULTS, **cfg}
        self.model = ctx.need(cfg, "model", str)
        if not self.model:
            raise ConfigError(f"{where} model must not be empty")
        self.ext = ctx.need(cfg, "format", str)
        if self.ext not in self.FORMATS:
            raise ConfigError(f"{where} format must be one of {', '.join(self.FORMATS)}; not {self.ext!r}")
        self.speed = ctx.need(cfg, "speed", (int, float))
        if isinstance(self.speed, bool) or not 0.25 <= self.speed <= 4.0:  # the server's own range
            raise ConfigError(f"{where} speed must be a number from 0.25 to 4.0; not {self.speed!r}")
        self.timeout = ctx.need(cfg, "timeout_s", int)
        if isinstance(self.timeout, bool) or self.timeout <= 0:
            raise ConfigError(f"{where} timeout_s must be a positive number of seconds; not {self.timeout!r}")

    def _call(self, path: str, body: dict | None = None) -> tuple[str, bytes]:
        """The reply's content type and bytes. Every failure raises RuntimeError saying what to do."""
        req = urllib.request.Request(
            f"{self.url}/v1{path}", method="GET" if body is None else "POST",
            data=None if body is None else json.dumps(body).encode(),
            headers={} if body is None else {"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.headers.get_content_type(), resp.read()  # whole, so a cut-off reply raises
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"Kokoro HTTP {err.code}: {self._detail(err.read())}") from err
        except (TimeoutError, urllib.error.URLError) as err:
            if isinstance(err, TimeoutError) or isinstance(err.reason, TimeoutError):
                raise RuntimeError(f"the Kokoro server at {self.url} took over {self.timeout} s; "
                                   f"raise {self.where} timeout_s, or check the server") from err
            raise RuntimeError(f"can't reach the Kokoro server at {self.url}: {err.reason}; "
                               f"start it, or fix {self.where} url") from err
        except (OSError, http.client.HTTPException) as err:
            raise RuntimeError(f"the Kokoro server at {self.url} dropped the request: "
                               f"{type(err).__name__}: {err}") from err

    @staticmethod
    def _detail(raw: bytes) -> str:
        """The server's message from an error body: FastAPI's {"detail": {"message": ...}} or {"detail": "..."}."""
        text = raw.decode(errors="replace")
        try:
            detail = json.loads(text)["detail"]
        except (ValueError, KeyError, TypeError):
            return text[:500]
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            return detail["message"]
        return str(detail)[:500]

    def voices(self) -> list[dict]:
        """The server's voice list, asked every time so a voice added to the server shows at once."""
        _, body = self._call("/audio/voices")
        try:
            found = [{"id": v["id"], "name": v["name"]} for v in json.loads(body)["voices"]]
            if not all(isinstance(v["id"], str) and isinstance(v["name"], str) for v in found):
                raise TypeError("voice ids and names must be strings")
        except (ValueError, KeyError, TypeError) as err:
            raise RuntimeError(f"{self.url}/v1/audio/voices did not list voices as Kokoro-FastAPI does "
                               f"({type(err).__name__}: {err}); is {self.where} url a Kokoro server?") from err
        if not found:
            raise RuntimeError(f"the Kokoro server at {self.url} lists no voices; load some on the server")
        return found

    def synthesize(self, script: str, out: Path, voice: str) -> None:
        ctype, audio = self._call("/audio/speech", {
            "model": self.model, "input": script, "voice": voice, "response_format": self.ext,
            "speed": self.speed, "stream": False,  # one whole file, not chunks
        })
        if not ctype.startswith("audio/"):
            raise RuntimeError(f"the Kokoro server at {self.url} answered {ctype}, not audio; "
                               f"is {self.where} url a Kokoro server?")
        if not audio:
            raise RuntimeError("Kokoro returned no audio")
        out.write_bytes(audio)


class Kokoro:
    """Kokoro text-to-speech run inside the service, with no server: English voices, lossless wav.

    Needs the kokoro-onnx package (`uv sync --extra kokoro`) and the model files, which only
    `nanotea kokoro download` fetches. They go in `model_dir`, by default under the user's cache.
    """
    api = API
    ext = "wav"
    FORMATS = ("float32", "pcm24")  # float32 is the model's output as it is
    # The documented voice ids: first letter the language, second the gender (hexgrad/Kokoro-82M, VOICES.md).
    # Only English is spoken here: the other languages need their own phonemizer (kokoro-onnx's examples).
    LANGS = {"a": ("en-us", "en-US"), "b": ("en-gb", "en-GB")}
    GENDERS = {"f": "female", "m": "male"}
    OPTIONS = (
        Option("model_dir", str, "Where the model files are, from nanotea kokoro download. Relative to the config's "
                                 "directory. Unset: ~/.cache/nanotea/kokoro.", None, machine=True),
        Option("format", str, "float32 is the model's samples as they are; pcm24 is 24-bit. Both are lossless wav at "
                              "the model's 24 kHz.", "float32", choices=tuple(FORMATS)),
        Option("speed", (int, float), "How fast it speaks.", 1.0, lo=0.5, hi=2.0),
    )

    def __init__(self, cfg: dict, ctx: Context):
        where = f"[{ctx.kind}.{ctx.name}]"
        unknown_keys(cfg, self.OPTIONS, where)
        self.dir = files.model_dir(cfg, where)
        self.format = ctx.need({"format": "float32", **cfg}, "format", str)
        if self.format not in self.FORMATS:
            raise ConfigError(f"{where} format must be one of {', '.join(self.FORMATS)}; not {self.format!r}")
        self.speed = ctx.need({"speed": 1.0, **cfg}, "speed", (int, float))
        if isinstance(self.speed, bool) or not 0.5 <= self.speed <= 2.0:  # kokoro-onnx's own range
            raise ConfigError(f"{where} speed must be a number from 0.5 to 2.0; not {self.speed!r}")
        if importlib.util.find_spec("kokoro_onnx") is None:
            raise ConfigError(f"{where} needs the kokoro-onnx package: install it with `uv sync --extra kokoro` "
                              "(from a checkout) or `uv tool install 'nanotea[kokoro]'`")
        if self.format == "pcm24" and shutil.which("ffmpeg") is None:
            raise ConfigError(f"{where} format pcm24 needs ffmpeg on the PATH to write the wav")
        bad = [why for f in files.FILES if (why := files.problem(self.dir, f))]
        if bad:
            raise ConfigError(f"{where} model files in {self.dir}: {'; '.join(bad)}. "
                              "Fetch them, once, with: nanotea kokoro download")
        self._voices = self._read_voices(where)
        self._model = None
        self._lock = threading.Lock()  # the model is loaded once, and one synthesis runs at a time

    def _read_voices(self, where: str) -> dict[str, dict]:
        """The voices file is an npz: one array per voice, so its member names are the voices."""
        path = self.dir / files.VOICES
        try:
            with zipfile.ZipFile(path) as z:
                ids = sorted(n.removesuffix(".npy") for n in z.namelist() if n.endswith(".npy"))
        except zipfile.BadZipFile as err:
            raise ConfigError(f"{where} {path} is not a voices file: {err}") from err
        found = {}
        for vid in ids:
            m = re.fullmatch(r"([ab])([fm])_([a-z0-9]+)", vid)
            if m:
                lang, locale = self.LANGS[m[1]]
                found[vid] = {"id": vid, "name": m[3].capitalize(), "gender": self.GENDERS[m[2]], "locale": locale,
                              "lang": lang}
        if not found:
            raise ConfigError(f"{where} {path} has no English voices")
        return found

    def voices(self) -> list[dict]:
        return [{k: v for k, v in voice.items() if k != "lang"} for voice in self._voices.values()]

    def _engine(self):
        """The loaded model: built on first use, so starting the service stays quick."""
        if self._model is None:
            try:
                from kokoro_onnx import Kokoro as Engine
                self._model = Engine(str(self.dir / files.MODEL), str(self.dir / files.VOICES))
            except Exception as err:
                raise RuntimeError(f"can't load the Kokoro model from {self.dir}: {type(err).__name__}: {err}") \
                    from err
        return self._model

    def synthesize(self, script: str, out: Path, voice: str) -> None:
        if voice not in self._voices:
            raise RuntimeError(f"unknown Kokoro voice {voice!r}")
        with self._lock:
            try:
                audio, rate = self._engine().create(script, voice=voice, speed=self.speed,
                                                    lang=self._voices[voice]["lang"])
            except ValueError as err:
                raise RuntimeError(f"Kokoro can't speak this: {err}") from err
        view = memoryview(audio)
        if view.format != "f" or view.ndim != 1 or not view.c_contiguous or not view.nbytes:
            raise RuntimeError("Kokoro gave no audio, or not one channel of float32 samples")
        if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
            raise RuntimeError(f"Kokoro gave a sample rate of {rate!r}")
        floats = array.array("f", view.cast("B").tobytes())
        if sys.byteorder == "big":
            floats.byteswap()  # wav is little-endian
        samples = floats.tobytes()
        part = out.with_name(out.name + ".part")
        if self.format == "float32":
            part.write_bytes(float_wav(samples, rate))
            return os.replace(part, out)
        with tempfile.NamedTemporaryFile(suffix=".f32") as raw:
            raw.write(samples)
            raw.flush()
            args = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1",
                    "-i", raw.name, "-c:a", "pcm_s24le", "-f", "wav", str(part)]
            proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
        os.replace(part, out)


def float_wav(samples: bytes, rate: int) -> bytes:
    """Mono little-endian float32 samples as a plain IEEE-float wav (format 3, with the fact chunk the format asks
    for), the same on every machine: ffmpeg's header for it differs between versions."""
    def chunk(cid: bytes, data: bytes) -> bytes:
        return cid + struct.pack("<I", len(data)) + data + b"\0" * (len(data) % 2)
    body = (b"WAVE" + chunk(b"fmt ", struct.pack("<HHIIHHH", 3, 1, rate, rate * 4, 4, 32, 0))
            + chunk(b"fact", struct.pack("<I", len(samples) // 4)) + chunk(b"data", samples))
    if len(body) > 0xFFFFFFFF:
        raise RuntimeError(f"{len(samples) // 4} samples are too many for one wav file")
    return b"RIFF" + struct.pack("<I", len(body)) + body


class Command:
    """Any local engine. In argv, `{text_file}` is the script, `{out}` the audio path, `{voice}` the voice."""
    api = API
    OPTIONS = (
        Option("argv", list, "The program and its arguments. {text_file}: the script, {out}: where the audio goes, "
                             "{voice}: the sender's voice. Run in the config's directory.", machine=True),
        Option("ext", str, "The audio file's extension: what the program writes, such as m4a, wav or mp3."),
        Option("voices", list, "The voices senders may pick: names the program understands as {voice}."),
        Option("timeout_s", int, "Seconds before the program is killed and the message fails."),
    )

    def __init__(self, cfg: dict, ctx: Context):
        self.argv = ctx.program(cfg)
        self.ext = ctx.need(cfg, "ext", str)
        self.voice_ids = ctx.need(cfg, "voices", list)
        self.timeout = ctx.need(cfg, "timeout_s", int)

    def voices(self) -> list[dict]:
        return [{"id": v} for v in self.voice_ids]

    def synthesize(self, script: str, out: Path, voice: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
            f.write(script)
            f.flush()
            subs = {"{text_file}": f.name, "{out}": str(out), "{voice}": voice}
            argv = [self._fill(a, subs) for a in self.argv]
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, cwd=BASE)
        if proc.returncode != 0:
            raise RuntimeError(f"{argv[0]} exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"{argv[0]} wrote no audio to {out}")

    @staticmethod
    def _fill(arg: str, subs: dict[str, str]) -> str:
        for k, v in subs.items():
            arg = arg.replace(k, v)
        return arg
