"""Speech-to-text plugins (kind stt). Each returns the transcript of an audio file."""

import subprocess
from pathlib import Path

from nanotea.config import BASE, PACKAGE, Option
from nanotea.plugins import API, Context


class Command:
    """Any engine that prints a transcript to stdout. In argv, `{audio_file}` is the audio's path and
    `{package}` the nanotea package's directory (for its transcribe.sh)."""
    api = API
    OPTIONS = (
        Option("argv", list, "The program and its arguments. {audio_file}: the recording; {package}: where nanotea "
                             "is installed, for its transcribe.sh. Run in the config's directory.", machine=True),
        Option("timeout_s", int, "Seconds before the program is killed and the transcript fails."),
    )

    def __init__(self, cfg: dict, ctx: Context):
        self.argv = ctx.program(cfg)
        self.timeout = ctx.need(cfg, "timeout_s", int)

    def transcribe(self, audio: Path) -> str:
        argv = [a.replace("{audio_file}", str(audio)).replace("{package}", str(PACKAGE)) for a in self.argv]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, cwd=BASE)
        if proc.returncode != 0:
            raise RuntimeError(f"{argv[0]} exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
        text = proc.stdout.strip()
        if not text:
            raise RuntimeError("the transcript is empty")
        return text
