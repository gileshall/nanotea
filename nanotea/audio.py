"""Splicing speech and attached clips into one track, in the format [audio] names."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from nanotea.config import ConfigError, Option, defaults

# format -> (extension, ffmpeg codec arguments); {kbps} only for the lossy ones
FORMATS = {
    "mp3": ("mp3", ["-c:a", "libmp3lame", "-b:a", "{kbps}k"]),
    "aac": ("m4a", ["-c:a", "aac", "-b:a", "{kbps}k"]),
    "flac": ("flac", ["-c:a", "flac", "-sample_fmt", "s32"]),
    "wav": ("wav", ["-c:a", "pcm_s24le"]),
}
RATES = (44100, 48000, 88200, 96000)
OPTIONS = (
    Option("format", str, "The format of a joined track: a message's speech with its clips, or several takes sent "
                          "together. flac and wav are lossless; wav is 24-bit.", "mp3", choices=tuple(FORMATS)),
    Option("kbps", int, "Bitrate of an mp3 or aac joined track.", 192, lo=32, hi=320),
    Option("rate", int, "Sample rate of a joined track, in Hz.", 48000, choices=RATES),
    Option("max_upload_mb", int, "The largest file you can attach, in MB. The reverse proxy must allow it too.", 1024,
           lo=1),
)
DEFAULTS = defaults(OPTIONS)


@dataclass(frozen=True)
class Mix:
    """How a message's speech and clips, or the owner's several takes, are joined: format, bitrate for mp3 and
    aac, sample rate. Clips sent alone are never re-encoded; their originals are kept beside any mix."""
    format: str = DEFAULTS["format"]
    kbps: int = DEFAULTS["kbps"]
    rate: int = DEFAULTS["rate"]

    @property
    def ext(self) -> str:
        return FORMATS[self.format][0]

    def join(self, parts: list[Path | float], out: Path) -> Path:
        """Concatenate audio files and silences (floats, in seconds) into one stereo file at out with this
        format's extension. Returns its path."""
        out = out.with_suffix(f".{self.ext}")
        args = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"]
        for p in parts:
            if isinstance(p, float):
                args += ["-f", "lavfi", "-t", str(p), "-i", f"anullsrc=r={self.rate}:cl=stereo"]
            else:
                args += ["-i", str(p)]
        chains = [f"[{i}:a]aresample={self.rate},aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
                  for i in range(len(parts))]
        inputs = "".join(f"[a{i}]" for i in range(len(parts)))
        graph = ";".join(chains) + f";{inputs}concat=n={len(parts)}:v=0:a=1[out]"
        codec = [a.format(kbps=self.kbps) for a in FORMATS[self.format][1]]
        args += ["-filter_complex", graph, "-map", "[out]", *codec, str(out)]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
        return out


def settings(cfg: dict) -> tuple[Mix, int]:
    """[audio] from the config: the Mix, and the largest file the owner can attach, in bytes. Every key is
    optional; one that isn't known, or has the wrong type or value, stops the service."""
    table = cfg.get("audio", {})
    if not isinstance(table, dict):
        raise ConfigError("[audio] must be a table")
    unknown = sorted(set(table) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"[audio] has no {', '.join(unknown)}; it takes {', '.join(DEFAULTS)}")
    got = {**DEFAULTS, **table}
    if got["format"] not in FORMATS:
        raise ConfigError(f"[audio] format must be one of {', '.join(FORMATS)}; not {got['format']!r}")
    for key in ("kbps", "rate", "max_upload_mb"):
        if not isinstance(got[key], int) or isinstance(got[key], bool) or got[key] <= 0:
            raise ConfigError(f"[audio] {key} must be a positive whole number; not {got[key]!r}")
    if got["rate"] not in RATES:
        raise ConfigError(f"[audio] rate must be one of {', '.join(map(str, RATES))}; not {got['rate']}")
    if not 32 <= got["kbps"] <= 320:
        raise ConfigError(f"[audio] kbps must be from 32 to 320; not {got['kbps']}")
    return Mix(got["format"], got["kbps"], got["rate"]), got["max_upload_mb"] * 1024 * 1024
