"""A notify plugin that appends each note it is given to a file, one JSON object per line."""

import json
from dataclasses import asdict
from pathlib import Path

from nanotea.plugins import API, Context


class Recorder:
    """Writes every note to [notify.<name>] path."""
    api = API

    def __init__(self, cfg: dict, ctx: Context):
        self.path = Path(ctx.need(cfg, "path", str))

    def send(self, note) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(note)) + "\n")
