"""What to paste into each harness to connect an agent: MCP server, hooks, timeouts. Printed, never written:
harness configs are the user's.

nanotea setup HARNESS --name NAME [--line LINE] [--voice ID] [--channel NAME ...] [--bang]
nanotea sop [--name NAME] [--line LINE] [--idle MODE] [--bang] [--skill]
"""

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nanotea.client import Client, NanoteaError, line_for
from nanotea.config import CONFIG, load_config
from nanotea.credentials import owner_key, token_path
from nanotea.sop import IDLE_MODES, render


@dataclass
class Agent:
    exe: str
    config: str
    name: str
    line: str
    args: list[str]  # nanotea mcp's arguments
    token_file: str  # where this agent's token is: a path, never the secret

    @property
    def env(self) -> dict[str, str]:
        return {"NANOTEA_CONFIG": self.config, "NANOTEA_TOKEN_FILE": self.token_file}

    def env_args(self, flag: str) -> str:
        return " ".join(f"{flag} {k}={shlex.quote(v)}" for k, v in self.env.items())

    def hook(self, mode: str, *extra: str) -> str:
        line = [] if self.line == line_for(self.name) else ["--line", self.line]
        argv = [self.exe, "hook", mode, "--name", self.name, *line, *extra]
        return " ".join(f"{k}={shlex.quote(v)}" for k, v in self.env.items()) + " " + shlex.join(argv)


def _json(obj) -> str:
    return json.dumps(obj, indent=2)


HELD_NOTE = ("Permission prompts: these show {owner} when the agent is stopped at a prompt asking for "
             "permission, and when it gets past it. They decide nothing.")


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(i) for i in items) + "]"


def _std(a: Agent, **extra) -> dict:
    return {"command": a.exe, "args": a.args, "env": a.env, **extra}


def claude(a: Agent) -> str:
    rewake = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": a.hook("rewake"), "asyncRewake": True,
                                             "timeout": 86400}]}]}}
    stop = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": a.hook("stop", "--harness", "claude"),
                                           "timeout": 60}]}]}}
    return f"""\
MCP server: .mcp.json in the project (or ~/.claude.json for every project):

{_json({"mcpServers": {"nanotea": _std(a)}})}

or: claude mcp add --scope project {a.env_args("--env")} nanotea -- {shlex.join([a.exe, *a.args])}

Wake hook: .claude/settings.json (or ~/.claude/settings.json). When the agent ends its turn this runs in the
background and wakes it when {{owner}} writes. The timeout is above the hook's own 23 hour limit, because Claude
Code wakes the agent when a hook times out.

{_json(rewake)}

{HELD_NOTE}

{_json({"hooks": {
    "PermissionRequest": [{"hooks": [{"type": "command", "command": a.hook("held"), "timeout": 10}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": a.hook("clear"), "async": True, "timeout": 10}]}],
    "PostToolUseFailure": [{"hooks": [{"type": "command", "command": a.hook("clear"), "async": True,
                                       "timeout": 10}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": a.hook("clear"), "timeout": 10}]}]}})}

Headless runs (claude -p) don't run background hooks or ask permission. Use the plain stop hook there instead, which sends the
agent back if something is already waiting when it stops:

{_json(stop)}

Instructions: the MCP server gives them to the agent. Claude Code reads AGENTS.md only when there is no
CLAUDE.md; with one, add the line @AGENTS.md to it. Skill: nanotea sop --skill > .claude/skills/nanotea/SKILL.md
"""


def codex(a: Agent) -> str:
    return f"""\
MCP server: ~/.codex/config.toml (or .codex/config.toml in a trusted project). Codex gives a tool call 60
seconds by default; wait returns within {{wait_s}}.

[mcp_servers.nanotea]
command = {json.dumps(a.exe)}
args = {_toml_list(a.args)}
tool_timeout_sec = 120

[mcp_servers.nanotea.env]
NANOTEA_CONFIG = {json.dumps(a.config)}
NANOTEA_TOKEN_FILE = {json.dumps(a.token_file)}

or: codex mcp add nanotea {a.env_args("--env")} -- {shlex.join([a.exe, *a.args])}

Stop hook: ~/.codex/hooks.json (or .codex/hooks.json). If something of {{owner}}'s is waiting when the agent
stops, it goes on and checks it. Review and trust it with /hooks before it runs, and again after any change.

{_json({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": a.hook("stop", "--harness", "codex"),
                                       "timeout": 60}]}],
                   "PermissionRequest": [{"hooks": [{"type": "command", "command": a.hook("held"), "timeout": 10}]}],
                   "PostToolUse": [{"hooks": [{"type": "command", "command": a.hook("clear"), "timeout": 10}]}]}})}

PermissionRequest and PostToolUse show {{owner}} when the agent is stopped at a prompt asking for permission,
and when it gets past it. They decide nothing.

Instructions: AGENTS.md (nanotea sop >> AGENTS.md). Skill: nanotea sop --skill > .agents/skills/nanotea/SKILL.md
"""


def cursor(a: Agent) -> str:
    return f"""\
MCP server: .cursor/mcp.json (or ~/.cursor/mcp.json). Cursor allows a tool call about 60 seconds; wait returns
within {{wait_s}}.

{_json({"mcpServers": {"nanotea": {"type": "stdio", **_std(a)}}})}

Stop hook: .cursor/hooks.json (or ~/.cursor/hooks.json). If something of {{owner}}'s is waiting when the agent
stops, Cursor sends it a follow-up to check it. Unverified: whether the cursor-agent CLI runs hooks.

{_json({"version": 1, "hooks": {"stop": [{"command": a.hook("stop", "--harness", "cursor"), "timeout": 60,
                                          "loop_limit": 5}]}})}

Cursor has no hook for its permission prompts, so {{owner}} isn't shown when the agent is held at one.

Instructions: AGENTS.md (nanotea sop >> AGENTS.md). Skill: nanotea sop --skill > .cursor/skills/nanotea/SKILL.md
"""


def gemini(a: Agent) -> str:
    return f"""\
MCP server: .gemini/settings.json (or ~/.gemini/settings.json). Gemini allows a tool call 10 minutes.

{_json({"mcpServers": {"nanotea": _std(a, timeout=600000)}})}

or: gemini mcp add -s project {a.env_args("-e")} --timeout 600000 nanotea {shlex.join([a.exe, *a.args])}

AfterAgent hook, in the same settings.json: if something of {{owner}}'s is waiting when the agent finishes, it
goes on and checks it. Unverified: the matcher AfterAgent expects.

{_json({"hooks": {"AfterAgent": [{"matcher": "*", "hooks": [{"name": "nanotea", "type": "command",
                                                              "command": a.hook("stop", "--harness", "gemini"),
                                                              "timeout": 60000}]}]}})}

{HELD_NOTE} The held hook ignores notifications other than ToolPermission.

{_json({"hooks": {"Notification": [{"matcher": "*", "hooks": [{"name": "nanotea-held", "type": "command",
                                                               "command": a.hook("held"), "timeout": 10000}]}],
                   "AfterTool": [{"matcher": "*", "hooks": [{"name": "nanotea-clear", "type": "command",
                                                             "command": a.hook("clear"), "timeout": 10000}]}]}})}

Instructions: Gemini reads AGENTS.md only when settings.json names it: "context": {{"fileName": ["AGENTS.md",
"GEMINI.md"]}}. Skill: nanotea sop --skill > .gemini/skills/nanotea/SKILL.md
"""


def opencode(a: Agent) -> str:
    return f"""\
MCP server: opencode.json in the project (or ~/.config/opencode/opencode.json). Unverified: whether timeout
bounds tool calls or only listing them; it is set above wait's {{wait_s}} either way.

{_json({"$schema": "https://opencode.ai/config.json",
        "mcp": {"nanotea": {"type": "local", "command": [a.exe, *a.args], "enabled": True,
                            "environment": a.env, "timeout": 120000}}})}

No stop hook: the agent checks before it finishes, and stays on call with wait when asked to.

Instructions: AGENTS.md (nanotea sop >> AGENTS.md). Skill: nanotea sop --skill > .opencode/skills/nanotea/SKILL.md
"""


def goose(a: Agent) -> str:
    args = ", ".join(a.args)
    return f"""\
MCP server: ~/.config/goose/config.yaml, under extensions. Goose allows a tool call 300 seconds.

extensions:
  nanotea:
    type: stdio
    name: nanotea
    enabled: true
    cmd: {a.exe}
    args: [{args}]
    envs: {{ NANOTEA_CONFIG: {a.config}, NANOTEA_TOKEN_FILE: {a.token_file} }}
    timeout: 300

No stop hook here: the agent checks before it finishes, and stays on call with wait when asked to.

Instructions: AGENTS.md (nanotea sop >> AGENTS.md).
"""


def vscode(a: Agent) -> str:
    return f"""\
MCP server: .vscode/mcp.json (its key is "servers").

{_json({"servers": {"nanotea": {"type": "stdio", **_std(a)}}})}

No stop hook here: the agent checks before it finishes, and stays on call with wait when asked to. No hook for
permission prompts either, so {{owner}} isn't shown when the agent is held at one.

Instructions: AGENTS.md (nanotea sop >> AGENTS.md; VS Code needs chat.useAgentsMdFile on).
"""


def zed(a: Agent) -> str:
    return f"""\
MCP server: Zed's settings.json (zed: open settings file). Zed allows a tool call about 60 seconds; wait
returns within {{wait_s}}.

{_json({"context_servers": {"nanotea": _std(a)}})}

Instructions: AGENTS.md (nanotea sop >> AGENTS.md).
"""


def cline(a: Agent) -> str:
    return f"""\
MCP server: Cline's MCP settings (the MCP panel; the CLI reads ~/.cline/mcp.json). Unverified: timeout's unit,
documented elsewhere as seconds.

{_json({"mcpServers": {"nanotea": _std(a, disabled=False, timeout=300)}})}

Instructions: AGENTS.md (nanotea sop >> AGENTS.md).
"""


def amp(a: Agent) -> str:
    return f"""\
MCP server: ~/.config/amp/settings.json (or .amp/settings.json, which Amp asks you to approve).

{_json({"amp.mcpServers": {"nanotea": _std(a)}})}

Instructions: AGENTS.md (nanotea sop >> AGENTS.md). Skill: nanotea sop --skill > .agents/skills/nanotea/SKILL.md
"""


def other(a: Agent) -> str:
    return f"""\
Any MCP client: run this over stdio, with this environment.

command: {a.exe}
args: {json.dumps(a.args)}
env: NANOTEA_CONFIG={a.config} NANOTEA_TOKEN_FILE={a.token_file}

wait returns within {{wait_s}}; give the server's tool calls at least that long. A harness with a stop or
after-turn hook can run: {a.hook("stop")}
It exits 2 with a note on stderr when something of {{owner}}'s is waiting, and 0 otherwise.

Instructions: AGENTS.md (nanotea sop >> AGENTS.md), or the Agent Skill (nanotea sop --skill).
Scripts and event sources: nanotea-tell, the command line (nanotea-tell --help).
"""


@dataclass
class Harness:
    show: Callable[[Agent], str]
    idle: str  # the agent's idle mode with this harness's hooks
    wait_s: int


HARNESSES = {
    "claude": Harness(claude, "hook", 50),
    "codex": Harness(codex, "finish", 50),
    "cursor": Harness(cursor, "finish", 50),
    "gemini": Harness(gemini, "finish", 300),
    "opencode": Harness(opencode, "finish", 50),
    "goose": Harness(goose, "finish", 240),
    "vscode": Harness(vscode, "finish", 50),
    "zed": Harness(zed, "finish", 50),
    "cline": Harness(cline, "finish", 50),
    "amp": Harness(amp, "finish", 50),
    "other": Harness(other, "finish", 50),
}


def executable() -> str:
    """The nanotea command beside this interpreter: what a harness launches."""
    exe = Path(sys.executable).with_name("nanotea")
    if not exe.exists():
        sys.exit(f"nanotea setup: no nanotea command at {exe}; install nanotea (uv tool install, pipx, or a venv) "
                 f"and run setup from there")
    return str(exe)


def setup(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="nanotea setup", description="Print what to paste into a harness to connect "
                                                                  "an agent. Writes nothing.")
    p.add_argument("harness", choices=HARNESSES)
    p.add_argument("--name", required=True, help="the agent's name: its role, the same every session")
    p.add_argument("--line", help="its line (default: the name, slugged; main for the main inbox)")
    p.add_argument("--voice", help="its voice, if it hasn't one yet (nanotea mcp lists them via the voices tool)")
    p.add_argument("--channel", action="append", default=[], help="a channel to receive posts from")
    p.add_argument("--bang", action="store_true",
                   help="let this session run the commands the owner types with ! (they must also be on in "
                        "Settings). This puts --bang in the MCP arguments: anyone who can edit that config can give it")
    a = p.parse_args(argv)
    cfg = load_config()
    h = HARNESSES[a.harness]
    line = a.line or line_for(a.name)
    args = ["mcp", "--name", a.name]
    if a.line:
        args += ["--line", a.line]
    if a.voice:
        args += ["--voice", a.voice]
    for c in a.channel:
        args += ["--channel", c]
    if h.idle != "finish":
        args += ["--idle", h.idle]
    if h.wait_s != 50:
        args += ["--wait-s", str(h.wait_s)]
    if a.bang:
        args += ["--bang"]
    token_file = token_path(a.name)
    agent = Agent(executable(), str(CONFIG.expanduser().resolve()), a.name, line, args, str(token_file))
    if not token_file.exists():
        print(f"No token for {a.name!r} yet at {token_file}. nanotea mcp asks for one when it first starts, and "
              f"collects it there once the approver or you approve it, in the app under Tokens.\n"
              f"The config below names that file and holds no secret.\n")
    print(h.show(agent).replace("{owner}", cfg["app"]["owner"]).replace("{wait_s}", f"{h.wait_s} seconds"), end="")
    if a.bang:
        print(f"nanotea setup: --bang is in the arguments: this session runs the commands {cfg['app']['owner']} types "
              f"with !, on this machine, once Settings turns bang commands on", file=sys.stderr)


SKILL_HEAD = """\
---
name: nanotea
description: Message {owner} through the nanotea MCP tools (join, send, ask, check, wait).
  Use when {owner} should hear a result or decide something, when you are put on call, and whenever a nanotea
  tool result shows waiting above zero.
---

# Nanotea

"""


def sop(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="nanotea sop",
                                description="Print the working agreement: a section for AGENTS.md, or an Agent Skill.")
    p.add_argument("--name", help="the agent's name, if every agent reading it is the same one")
    p.add_argument("--line", help="with --name: its line")
    p.add_argument("--idle", choices=IDLE_MODES, default="finish", help="what to do with nothing left to do")
    p.add_argument("--skill", action="store_true", help="print a SKILL.md instead of an AGENTS.md section")
    p.add_argument("--bang", action="store_true",
                   help="for an agent whose session is started with --bang: include what to do with the owner's "
                        "bang commands, if they are on in Settings")
    a = p.parse_args(argv)
    if a.line and not a.name:
        p.error("--line goes with --name")
    cfg = load_config()
    owner = cfg["app"]["owner"]
    try:
        settings = Client(cfg["port"], owner_key(cfg)).get("/api/settings")["settings"]
    except NanoteaError as err:
        sys.exit(f"nanotea sop: the agreement follows the service's settings, so it needs the service: {err}")
    settings["bang"] = settings["bang"] and a.bang
    body = render(owner, settings, a.name, (a.line or line_for(a.name)) if a.name else None, a.idle)
    print((SKILL_HEAD.format(owner=owner) if a.skill else "## Nanotea\n\n") + body, end="")
