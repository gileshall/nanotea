"""The owner's settings: data/settings.json. Every setting is declared once, in SCHEMA: its type, default, label
and help. Validation, the settings page and [settings] in the config (defaults for a new data directory's
owner, until the owner changes them) all come from it. Agents' MCP servers read the agents' section when a
session starts; standing rules, the owner's pages and reading aloud follow a change at once."""

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from nanotea.config import ConfigError
from nanotea.sop import INTRO, JOIN, lines, on

OPTIONAL_TOOLS = ("react", "status", "typing", "channels", "board", "history", "controls", "thread")
AGENT_MESSAGES = ("off", "shown", "hidden")
HELD_PUSH_MAX_MIN = 24 * 60
EVENT_SETTLE_S = 5  # with batch_events: how long wait and the wake hook let events gather
AGENT_NAME = re.compile(r"[^\x00-\x1f,]{1,64}")  # as a sender's name, less commas, which separate names


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Setting:
    """One setting. Its type is its default's: true/false, a whole number from lo to hi, one of choices
    ((value, label), ...), or a list of agents' names (names)."""
    key: str
    section: str
    label: str
    about: str
    default: object
    choices: tuple = ()
    lo: int = 0
    hi: int = 0
    names: bool = False

    @property
    def kind(self) -> str:
        if self.names:
            return "names"
        if self.choices:
            return "choice"
        return "bool" if isinstance(self.default, bool) else "int"

    def check(self, value, where: str = "") -> None:
        name = f"{where}{self.key}"
        if self.kind == "bool" and not isinstance(value, bool):
            raise SettingsError(f"{name} must be true or false")
        if self.kind == "int" and (type(value) is not int or not self.lo <= value <= self.hi):
            raise SettingsError(f"{name} must be a whole number, {self.lo} to {self.hi}")
        if self.kind == "choice" and value not in [v for v, _ in self.choices]:
            raise SettingsError(f"{name} must be one of {', '.join(v for v, _ in self.choices)}")
        if self.kind == "names":
            if not isinstance(value, list) or not all(isinstance(n, str) and AGENT_NAME.fullmatch(n.strip())
                                                      and n == n.strip() for n in value):
                raise SettingsError(f"{name} must be a list of agents' names, each 1 to 64 characters, no commas")
            if len({n.casefold() for n in value}) != len(value):
                raise SettingsError(f"{name} names someone twice")


SECTIONS = [
    ("agents", "Agents", "What agents are told and may use. Agents get a change when their next session starts; "
                         "standing rules and these pages follow it at once."),
    ("conversation", "Conversations", "How your pages draw a conversation or channel. Agents see no difference: "
                                      "they get each message with what it replies to either way."),
    ("reading", "Reading aloud", "How a message is read when you tap play. Agents write for reading; these turn "
                                 "what they wrote into speech without rewriting it. A message played before a "
                                 "change is read again the new way."),
    ("board", "Board", "Who may arrange the sidebar besides you."),
    ("service", "Service", "What this app may change in the service's config."),
]

SCHEMA = [
    Setting("rules", "agents", "Standing rules",
            "Rules you pin reach every session at join and whenever they change; agents follow them without asking "
            "again.", True),
    Setting("wake_filter", "agents", "Wake filter",
            "wait returns at once only for your words, answers and posts that @mention the agent. Other agents' "
            "posts collect until the wait ends.", True),
    Setting("batch_events", "agents", "Batch events",
            "Events that arrive together are handed over together: wait and the wake hook let them settle a few "
            "seconds.", True),
    Setting("strict_sop", "agents", "Strict working agreement",
            "No acknowledgements or recaps, one message per real change, nothing playable unasked, decisions "
            "through ask, standing orders acted on, misheard voice confirmed.", True),
    Setting("one_session", "agents", "One session per agent",
            "A second live session under the same name is refused, instead of two sessions sharing one line.", True),
    Setting("held", "agents", "Permission prompts",
            "With the held hook installed, shows when an agent is stuck at its harness's permission prompt.", True),
    Setting("held_push_min", "agents", "Notify when held",
            "Notify you once an agent has been held at a permission prompt this many minutes. 0: never.", 0,
            lo=0, hi=HELD_PUSH_MAX_MIN),
    Setting("agent_messages", "agents", "Agent to agent messages",
            "off: agents can't message each other. shown: they can, and you see it under Agent talk. hidden: they "
            "can, and it stays between them.", "off", choices=tuple((v, v) for v in AGENT_MESSAGES)),
    Setting("bang", "agents", "Bang commands",
            "Type !command on an agent's line and it runs on that agent's machine, in its directory, with no model "
            "deciding anything. The agent gets the command, its output and its exit code at its next check. Only in "
            "a session started with --bang.", False),
    Setting("thread_view", "conversation", "Replies",
            "linear: everything in time order, each reply quoting the message it answers. grouped: a reply folds "
            "under its thread's first message, which counts its replies and opens the thread to answer it.",
            "linear", choices=(("linear", "in time order"), ("grouped", "grouped by thread"))),
    Setting("read_code", "reading", "Code blocks",
            "A block of code: say that it is there, leave it out, or read it out.", "name",
            choices=(("name", "say it is there"), ("skip", "leave it out"), ("read", "read it"))),
    Setting("read_tables", "reading", "Tables",
            "Each row's cells in order; each cell after its column's heading; or only that a table is there.",
            "rows", choices=(("rows", "row by row"), ("labelled", "with headings"), ("name", "say it is there"))),
    Setting("read_links", "reading", "Web addresses",
            "An address written out in the text: read it, say only its site, or leave it out. A link's own "
            "words are always read.", "read",
            choices=(("read", "read it"), ("site", "say its site"), ("skip", "leave it out"))),
    Setting("read_paths", "reading", "File paths",
            "A path such as nanotea/server.py:1051: read it whole, or only the file's name and line.", "read",
            choices=(("read", "read it whole"), ("file", "file name and line"))),
    Setting("read_hashes", "reading", "Hashes and ids",
            "Long hexadecimal strings such as commit hashes: read them, or leave them out.", "read",
            choices=(("read", "read them"), ("skip", "leave them out"))),
    Setting("read_symbols", "reading", "Symbols in words",
            "Say arrows, comparisons and common abbreviations as words: -> as to, >= as at least, e.g. as for "
            "example.", False),
    Setting("read_max_words", "reading", "Longest reading",
            "Stop after about this many words, at the end of a sentence, and say the rest is in the app. "
            "0: read it all.", 0, lo=0, hi=20000),
    Setting("board_managers", "board", "Board managers",
            "Agents that may arrange the board: its groups, and which rows are hidden. Names, separated by commas.",
            [], names=True),
    Setting("config_programs", "service", "Programs from this app",
            "Configuration can change the settings that pick the programs the service runs, the code it loads, and "
            "where it keeps its data and secrets. Off, those change only in the config file.", False),
]
BY_KEY = {s.key: s for s in SCHEMA}
DEFAULTS = {**{s.key: s.default for s in SCHEMA}, "tools": {t: True for t in OPTIONAL_TOOLS}}
AGENT_KEYS = [s.key for s in SCHEMA if s.section == "agents"]
# What each agents' switch does, as the settings page says it. Tokens are counted for each from what it adds.
FEATURES = [(s.key, s.label, s.about) for s in SCHEMA if s.section == "agents"]


def check(patch, where: str = "") -> dict:
    """A partial change, validated: every key known, every value its type. where: what to call the place the
    patch came from in errors, such as "[settings] "."""
    if not isinstance(patch, dict) or not patch:
        raise SettingsError(f"send an object of {where}settings to change")
    for key, value in patch.items():
        if key == "tools":
            if not isinstance(value, dict) or not value:
                raise SettingsError(f"{where}tools must be an object of tool: true|false")
            for tool, enabled in value.items():
                if tool not in OPTIONAL_TOOLS:
                    raise SettingsError(f"unknown tool {where}{tool!r}; optional tools: {', '.join(OPTIONAL_TOOLS)}")
                if not isinstance(enabled, bool):
                    raise SettingsError(f"{where}tools.{tool} must be true or false")
        elif key in BY_KEY:
            BY_KEY[key].check(value, where)
        else:
            raise SettingsError(f"unknown setting {where}{key!r}; known: {', '.join([*BY_KEY, 'tools'])}")
    return patch


class SettingsBook:
    """defaults: [settings] from the config, checked, over the built-in defaults. The owner's changes, stored,
    win over both."""
    def __init__(self, root: Path, defaults: dict | None = None):
        self.path = root / "settings.json"
        self._lock = threading.Lock()
        try:
            base = _merge(DEFAULTS, check(defaults, "[settings] ") if defaults else {})
        except SettingsError as err:
            raise ConfigError(str(err)) from err
        if self.path.exists():
            stored = json.loads(self.path.read_text())
            # Every stored key must still be one; new keys take their defaults.
            check(stored)
            self._now = _merge(base, stored)
        else:
            self._now = base

    def get(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._now))

    def change(self, patch) -> dict:
        check(patch)
        with self._lock:
            self._now = _merge(self._now, patch)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._now, indent=2))
            os.replace(tmp, self.path)
            return json.loads(json.dumps(self._now))

    def is_manager(self, name: str) -> bool:
        """name may arrange the board."""
        return name.strip().casefold() in {n.casefold() for n in self.get()["board_managers"]}


def _merge(base: dict, patch: dict) -> dict:
    out = json.loads(json.dumps(base))
    for key, value in patch.items():
        out[key] = {**out[key], **value} if key == "tools" else value
    return out


def tokens(text: str) -> int:
    """An estimate: about four characters a token. No tokenizer is public for every model."""
    return -(-len(text) // 4)


CORE_TOOLS = ("join", "send", "ask", "check", "wait", "voices")


def costs(owner: str, tool_defs: dict[str, dict], rules: list[dict], plain: dict[str, dict]) -> dict:
    """Estimated tokens each switch adds to every session's context, and what is always there. tool_defs: each
    tool as the model is shown it. rules: every agent's standing rules. plain: the tools with controls off, whose
    send and ask go without the control parameter."""
    everything = {**DEFAULTS, "agent_messages": "shown"}
    by: dict[str, int] = {}
    base = tokens(INTRO.format(owner=owner)) + tokens(JOIN)
    for feature, text in lines(owner, everything, "finish"):
        if feature in (None, "idle"):
            base += tokens(text)
        else:
            by[feature] = by.get(feature, 0) + tokens(text)
    base += sum(tokens(json.dumps(plain[t])) for t in CORE_TOOLS)
    for t in OPTIONAL_TOOLS:
        by[f"tools.{t}"] = by.get(f"tools.{t}", 0) + tokens(json.dumps(tool_defs[t]))
    by["agent_messages"] += tokens(json.dumps(tool_defs["tell"]))
    by["tools.controls"] += sum(tokens(json.dumps(tool_defs[t])) - tokens(json.dumps(plain[t])) for t in ("send", "ask"))
    rules_now = tokens(json.dumps([{"id": r["id"], "text": r["text"], "for": r["for"]} for r in rules]))
    for key in AGENT_KEYS:
        by.setdefault(key, 0)
    return {"base": base, "by": by, "rules_now": rules_now}


def session_total(c: dict, settings: dict) -> int:
    """What a session starts with under these settings, by costs' estimate."""
    return (c["base"] + sum(n for f, n in c["by"].items() if on(settings, f))
            + (c["rules_now"] if settings["rules"] else 0))
