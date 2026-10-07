"""The files the local kokoro tts plugin runs on, and `nanotea kokoro download`, the only thing that fetches them."""

import argparse
import hashlib
import os
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from nanotea.config import CONFIG, ConfigError, resolve

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
MODEL = "kokoro-v1.0.onnx"
VOICES = "voices-v1.0.bin"


@dataclass(frozen=True)
class File:
    name: str
    url: str
    size: int
    sha256: str


# Sizes and SHA-256 are the release's own: the asset digests GitHub lists for model-files-v1.1
# (api.github.com/repos/thewh1teagle/kokoro-onnx/releases), the release kokoro-onnx's README links.
FILES = (
    File(MODEL, f"{RELEASE}/{MODEL}", 325505369, "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a"),
    File(VOICES, f"{RELEASE}/{VOICES}", 28214398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
)


def default_dir() -> Path:
    """Under the cache, not data/: the files are large and can be fetched again, so they aren't the record."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "nanotea" / "kokoro"


def model_dir(table: dict, where: str) -> Path:
    """[tts.<name>] model_dir (relative to the config's directory), or the default."""
    if "model_dir" not in table:
        return default_dir()
    if not isinstance(table["model_dir"], str) or not table["model_dir"]:
        raise ConfigError(f"{where} model_dir must be a directory path; not {table['model_dir']!r}")
    return resolve(table["model_dir"])


def problem(directory: Path, f: File) -> str | None:
    """Why this file isn't usable, or None when it is present and matches its pinned size and SHA-256."""
    path = directory / f.name
    if not path.is_file():
        return f"{f.name} is missing"
    if path.stat().st_size != f.size:
        return f"{f.name} has {path.stat().st_size} bytes, not {f.size}"
    with path.open("rb") as fh:
        if hashlib.file_digest(fh, "sha256").hexdigest() != f.sha256:
            return f"{f.name} fails its SHA-256 check"
    return None


class DownloadError(RuntimeError):
    pass


def _progress(name: str, done: int, total: int, last: list[int]) -> None:
    """One line per percent on a terminal, rewritten in place; one per 10% in a log."""
    pct = 100 * done // total
    tty = sys.stderr.isatty()
    if pct == last[0] or (not tty and pct % 10):
        return
    last[0] = pct
    text = f"{name}: {done / 1e6:.1f} of {total / 1e6:.1f} MB ({pct}%)"
    if tty:
        print(f"\r{text}", end="\n" if pct == 100 else "", file=sys.stderr, flush=True)
    else:
        print(text, file=sys.stderr)


def fetch(directory: Path, f: File) -> None:
    """Download f into directory, check it, and put it in place only if it matches."""
    tmp = tempfile.NamedTemporaryFile(dir=directory, prefix=f"{f.name}.", suffix=".part", delete=False)
    try:
        with tmp:
            try:
                with urllib.request.urlopen(f.url, timeout=60) as resp:
                    digest, done, last = hashlib.sha256(), 0, [-1]
                    while chunk := resp.read(1 << 20):
                        tmp.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        _progress(f.name, min(done, f.size), f.size, last)
            except urllib.error.HTTPError as err:
                raise DownloadError(f"{f.url} answered HTTP {err.code}") from err
            except (TimeoutError, urllib.error.URLError) as err:
                raise DownloadError(f"can't fetch {f.url}: {getattr(err, 'reason', err)}") from err
        if done != f.size:
            raise DownloadError(f"{f.name} came to {done} bytes, expected {f.size}")
        if digest.hexdigest() != f.sha256:
            raise DownloadError(f"{f.name} fails its SHA-256 check: got {digest.hexdigest()}, expected {f.sha256}")
        os.replace(tmp.name, directory / f.name)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def download(directory: Path) -> None:
    """Fetch each file that isn't already there and verified. Progress goes to stderr."""
    directory.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        if problem(directory, f) is None:
            print(f"{f.name}: already here and verified", file=sys.stderr)
            continue
        fetch(directory, f)
        print(f"{f.name}: verified", file=sys.stderr)


def main(argv: list[str]) -> None:
    """nanotea kokoro download [--dir DIR]: the files the kokoro tts plugin needs."""
    p = argparse.ArgumentParser(prog="nanotea kokoro", description="The local Kokoro voice's model files.")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("download", help="fetch and verify the model and voices files; skips ones already here")
    d.add_argument("--dir", help="where to put them; default: [tts.kokoro] model_dir in the config, else "
                   f"{default_dir()}")
    a = p.parse_args(argv)
    try:
        if a.dir:
            directory = Path(a.dir).expanduser()
        else:
            table = {}
            if CONFIG.expanduser().exists():
                # Only this table: the model can be fetched before the rest of the config is written.
                with CONFIG.expanduser().open("rb") as f:
                    try:
                        tts = tomllib.load(f).get("tts", {})
                    except tomllib.TOMLDecodeError as err:
                        raise ConfigError(f"{CONFIG}: {err}") from err
                table = tts.get("kokoro", {}) if isinstance(tts, dict) else None
                if not isinstance(table, dict):
                    raise ConfigError("[tts.kokoro] must be a table")
            directory = model_dir(table, "[tts.kokoro]")
        print(f"model directory: {directory}", file=sys.stderr)
        download(directory)
    except (ConfigError, DownloadError) as err:
        sys.exit(f"nanotea kokoro: {err}")
