"""Harness hooks that bring an agent back to the owner's messages.

stop: the harness's stop hook (Claude Code and Codex Stop, Gemini AfterAgent, Cursor stop). If anything of the
owner's waits for the agent, the harness is told to keep going and check it: exit 2 with a note on stderr, or
for Cursor a followup_message.
rewake: run in the background when the agent goes idle (Claude Code: a Stop hook with asyncRewake). Shows the
agent as idle and listening while it waits until something of the owner's is ready, then exits 2 with a note,
which wakes the agent. It wakes again for the same items only after REPEAT_S, so an agent that doesn't check
isn't woken in a loop. With batch_events on, events alone wait EVENT_SETTLE_S for others before they wake it.
A bang command's result (the owner's !command) never wakes the agent or counts as something waiting: the server
leaves it out of the waiting total and ids. It reaches the agent with the next thing that does, or at its next check.
held: the harness is asking for permission (Claude Code and Codex PermissionRequest, Gemini Notification), so the
owner sees the agent held at a prompt. It decides nothing: exit 0, no output.
clear: the agent got past the prompt (PostToolUse, AfterTool, UserPromptSubmit). Calls the service only if held
ran since the last clear.

nanotea hook stop|rewake|held|clear --name NAME [--line LINE] [--harness cursor|...]
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from nanotea import version
from nanotea.client import Client, NanoteaError, line_for, segment
from nanotea.config import load_config
from nanotea.credentials import agent_token
from nanotea.settings import EVENT_SETTLE_S

POLL_S = 3
REWAKE_MAX_S = 23 * 3600
REPEAT_S = 120
HARNESSES = ("claude", "codex", "gemini", "vscode", "cursor")


def waiting(client: Client, name: str, line: str, idle: bool = False) -> dict:
    return client.get(f"/api/agents/{quote(name, safe='')}/waiting", line=segment(line), idle=1 if idle else None)


def note(owner: str, w: dict) -> str:
    parts = [f"{n} {what}" for n, what in ((w["line"] - w["events"], f"message(s) from {owner}"),
                                           (w["events"], "event(s)"),
                                           (w["answers"], "answer(s) to your questions"),
                                           (w["talk"], "message(s) from other agents")) if n]
    return f"Nanotea: {' and '.join(parts)} waiting. Call the nanotea check tool and act on them."


class Held:
    """Whether held has run since the last clear, so clear, which runs after every tool, costs nothing otherwise."""

    def __init__(self, port: int, name: str):
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower())
        self.path = Path(tempfile.gettempdir()) / f"nanotea-held-{port}-{slug}"

    def held(self, client: Client, name: str, what: str | None) -> None:
        client.post(f"/api/agents/{quote(name, safe='')}/held", {"what": what})
        self.path.touch()

    def clear(self, client: Client, name: str, always: bool = False) -> None:
        if always or self.path.exists():
            client.post(f"/api/agents/{quote(name, safe='')}/held", {"clear": True})
            self.path.unlink(missing_ok=True)


def asking(event: dict) -> str | None:
    """What the harness is asking permission for, in a line."""
    if event.get("message"):  # Gemini's Notification, Claude Code's
        return str(event["message"])
    tool = event.get("tool_name")
    if not tool:
        return None
    args = event.get("tool_input") or {}
    detail = next((args[k] for k in ("command", "file_path", "path", "url") if isinstance(args, dict) and args.get(k)),
                  None)
    return f"{tool}: {detail}" if detail else str(tool)


def stop(client: Client, held: Held, owner: str, name: str, line: str, harness: str | None) -> int:
    event = json.loads(sys.stdin.read() or "{}")
    held.clear(client, name)
    w = waiting(client, name, line)
    if not w["total"]:
        return 0
    # Claude Code, Codex, Gemini: stop_hook_active. Cursor: loop_count, with its own loop_limit as well.
    if event.get("stop_hook_active") or event.get("loop_count"):
        print(f"nanotea-hook: {note(owner, w)} (sent back once already this turn; not again)", file=sys.stderr)
        return 0
    if harness == "cursor":
        print(json.dumps({"followup_message": note(owner, w)}))
        return 0
    print(note(owner, w), file=sys.stderr)
    return 2


class Woken:
    """The items this agent was last woken for, and when, kept between rewake runs."""

    def __init__(self, port: int, name: str, line: str):
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower())
        self.path = Path(tempfile.gettempdir()) / f"nanotea-rewake-{port}-{slug}-{line}.json"

    def due(self, ids: list[str]) -> bool:
        if not self.path.exists():
            return True
        last = json.loads(self.path.read_text())
        return not set(ids) <= set(last["ids"]) or time.time() - last["at"] >= REPEAT_S

    def mark(self, ids: list[str]) -> None:
        self.path.write_text(json.dumps({"ids": ids, "at": time.time()}))


def rewake(client: Client, held: Held, woken: Woken, owner: str, name: str, line: str, max_s: int) -> int:
    sys.stdin.read()
    held.clear(client, name, always=True)  # the turn ended, so no prompt holds it
    batch = client.get("/api/settings")["settings"]["batch_events"]
    deadline = time.monotonic() + max_s
    first_event = None
    while time.monotonic() < deadline:
        w = waiting(client, name, line, idle=True)
        if version.stale(client.version):
            # The same wait, on the new code: the harness still holds this process, and the agent stays asleep.
            left = max(1, int(deadline - time.monotonic()))
            os.execv(sys.executable, [sys.executable, "-m", "nanotea", "hook", "rewake", "--name", name, "--line",
                                      line, "--max-s", str(left)])
        if w["total"] and woken.due(w["ids"]):
            only_events = w["total"] == w["events"]
            if only_events and batch:
                first_event = first_event or time.monotonic()
            if not (only_events and batch) or time.monotonic() - first_event >= EVENT_SETTLE_S:
                woken.mark(w["ids"])
                print(note(owner, w), file=sys.stderr)
                return 2
        time.sleep(POLL_S)
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nanotea hook",
                                description="Harness hooks that bring an agent back to the owner's messages.")
    p.add_argument("mode", choices=["stop", "rewake", "held", "clear"])
    p.add_argument("--name", required=True, help="the agent's name, as it joins")
    p.add_argument("--line", help="its line (default: the name, slugged)")
    p.add_argument("--harness", choices=HARNESSES,
                   help="stop: the harness, if it needs its own answer (cursor: followup_message); otherwise exit 2 "
                        "with the note on stderr")
    p.add_argument("--max-s", type=int, default=REWAKE_MAX_S,
                   help="rewake: give up quietly after this many seconds; keep it under the hook's own timeout")
    a = p.parse_args(argv)
    cfg = load_config()
    owner = cfg["app"]["owner"]
    try:
        client = Client(cfg["port"], agent_token(a.name))
        line = a.line or line_for(a.name)
        held = Held(cfg["port"], a.name)
        if a.mode == "stop":
            code = stop(client, held, owner, a.name, line, a.harness)
        elif a.mode == "rewake":
            code = rewake(client, held, Woken(cfg["port"], a.name, line), owner, a.name, line, a.max_s)
        elif a.mode == "held":
            event = json.loads(sys.stdin.read() or "{}")
            # Gemini's Notification hook runs for every notification; only a permission prompt holds it.
            if event.get("notification_type") in (None, "ToolPermission", "permission_prompt"):
                held.held(client, a.name, asking(event))
            code = 0
        else:
            sys.stdin.read()
            held.clear(client, a.name)
            code = 0
    except (NanoteaError, ValueError) as err:
        sys.exit(f"nanotea-hook: {err}")
    sys.exit(code)


if __name__ == "__main__":
    main()
