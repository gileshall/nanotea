"""Rewriters turn an agent's message into a title and the script the voice reads."""

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from nanotea import markdown
from nanotea.config import Option
from nanotea.plugins import API, Context
from nanotea.prompts import Prompts
from nanotea.store import MARKER

SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "description": "A few words saying what the message is about."},
        "script": {"type": "string", "minLength": 1, "description": "The text the voice will read aloud."},
    },
    "required": ["title", "script"],
    "additionalProperties": False,
})


def request(text: str, sender: str, title_hint: str | None, ask: bool) -> str:
    """What the model is given for one message, beside the instructions."""
    return (f"Speaker: {sender}\nSender's title: {title_hint or '(none)'}\n"
            f"Wants a reply: {'yes' if ask else 'no'}\n\n<message>\n{text}\n</message>")


@dataclass
class Spoken:
    title: str
    script: str


TITLE_MAX = 60


class IdentityRewriter:
    """No rewrite: the voice reads the original text, less its markdown. The title is the sender's, or the text's
    first line."""
    api = API
    OPTIONS = ()

    def __init__(self, cfg: dict, ctx: Context):
        pass

    def rewrite(self, text: str, sender: str, title_hint: str | None, ask: bool) -> Spoken:
        return Spoken(title=(title_hint or "").strip() or self._title(text), script=text)

    @staticmethod
    def _title(text: str) -> str:
        lines = [ln.strip().lstrip("#>").strip() for ln in MARKER.sub("", markdown.plain(text)).splitlines()]
        line = " ".join(next((ln for ln in lines if ln), "").split())
        if not line:
            return "Attachment"
        if len(line) <= TITLE_MAX:
            return line
        cut = line[:TITLE_MAX]
        if line[TITLE_MAX] != " " and " " in cut:
            cut = cut.rsplit(" ", 1)[0]  # whole words only
        return cut + "..."


class ClaudeRewriter:
    """Headless Claude Code on the local login. No tools, MCP servers, or user hooks."""
    api = API
    OPTIONS = (
        Option("command", str, "The claude program: its name on the service's PATH, or its full path.",
               machine=True),
        Option("model", str, "The model, as claude --model takes it."),
        Option("effort", str, "Reasoning effort, as claude --effort takes it: low, medium, high."),
        Option("timeout_s", int, "Seconds before claude is killed and the message fails."),
    )

    def __init__(self, cfg: dict, ctx: Context):
        self.prompts = Prompts(ctx.data, ctx.owner)
        self.command = ctx.program(cfg, "command", str)
        self.model = ctx.need(cfg, "model", str)
        self.effort = ctx.need(cfg, "effort", str)
        self.timeout = ctx.need(cfg, "timeout_s", int)

    def rewrite(self, text: str, sender: str, title_hint: str | None, ask: bool) -> Spoken:
        prompt = request(text, sender, title_hint, ask)
        argv = [
            self.command, "-p",
            "--model", self.model,
            "--effort", self.effort,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "project,local",
            "--no-session-persistence",
            "--system-prompt", self.prompts.text("rewrite"),
            "--json-schema", SCHEMA,
            "--output-format", "json",
        ]
        # Neutral cwd so no project CLAUDE.md leaks into the rewrite.
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              timeout=self.timeout, cwd=tempfile.gettempdir())
        if proc.returncode != 0:
            raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}")
        try:
            events = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"claude returned non-JSON: {proc.stdout[:300]!r}") from e
        results = [ev for ev in events if ev.get("type") == "result"]
        if len(results) != 1:
            raise RuntimeError(f"expected one result event from claude, got {len(results)}")
        result = results[0]
        if result["is_error"] or result["subtype"] != "success":
            raise RuntimeError(f"claude failed: {result['subtype']}: {str(result.get('result'))[:300]}")
        out = result["structured_output"]
        title, script = out["title"].strip(), out["script"].strip()
        if not title or not script:
            raise RuntimeError("claude returned an empty title or script")
        return Spoken(title=title, script=script)


OUTPUT_CONTRACT = """

Reply with one JSON object and nothing else, no code fences: {"title": "...", "script": "..."}."""


class CommandRewriter:
    """Any LLM command line. The request comes on stdin; stdout must be one JSON object {title, script}.
    argv placeholders: {system} the instructions, {system_file} the same in a file, {schema_file} the reply's
    JSON schema in a file."""
    api = API
    OPTIONS = (
        Option("argv", list, "The program and its arguments. {system}: the instructions as text, {system_file}: the "
                             "same in a file, {schema_file}: the reply's JSON schema in a file. The request comes "
                             "on stdin; stdout must be one JSON object {\"title\": ..., \"script\": ...}.",
               machine=True),
        Option("timeout_s", int, "Seconds before the program is killed and the message fails."),
    )

    def __init__(self, cfg: dict, ctx: Context):
        self.prompts = Prompts(ctx.data, ctx.owner)
        self.argv = ctx.program(cfg)
        self.timeout = ctx.need(cfg, "timeout_s", int)

    def rewrite(self, text: str, sender: str, title_hint: str | None, ask: bool) -> Spoken:
        system = self.prompts.text("rewrite") + OUTPUT_CONTRACT
        with tempfile.TemporaryDirectory(prefix="nanotea-rewrite-") as tmp:
            system_file, schema_file = Path(tmp) / "system.md", Path(tmp) / "schema.json"
            system_file.write_text(system)
            schema_file.write_text(SCHEMA)
            argv = [a.replace("{system_file}", str(system_file)).replace("{schema_file}", str(schema_file))
                    .replace("{system}", system) for a in self.argv]
            # Neutral cwd so no project instructions leak into the rewrite.
            proc = subprocess.run(argv, input=request(text, sender, title_hint, ask), capture_output=True, text=True,
                                  timeout=self.timeout, cwd=tmp)
        if proc.returncode != 0:
            raise RuntimeError(f"{self.argv[0]} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}")
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{self.argv[0]} returned non-JSON: {proc.stdout[:300]!r}") from e
        if not isinstance(out, dict) or not isinstance(out.get("title"), str) or not isinstance(out.get("script"), str):
            raise RuntimeError(f"{self.argv[0]} returned no title and script: {proc.stdout[:300]!r}")
        title, script = out["title"].strip(), out["script"].strip()
        if not title or not script:
            raise RuntimeError(f"{self.argv[0]} returned an empty title or script")
        return Spoken(title=title, script=script)
