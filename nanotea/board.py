"""The board: how the owner's sidebar is arranged. data/groups.json is its groups, [{name, members}] in order; a
member is an agent's name, case aside, or a line's label or box. data/board-hidden.json is the names kept off it
while they are not connected and nothing of theirs is unread. [app] groups starts groups.json on a new data
directory. After that the owner arranges it from Settings, and board managers (the board_managers setting) with
nanotea-tell --arrange or the arrange tool. Each change is logged with who made it."""

import json
import logging
import os
import re
import threading
from pathlib import Path

from nanotea.config import ConfigError
from nanotea.settings import AGENT_NAME

log = logging.getLogger("nanotea")

# The board's own groups, drawn after the owner's.
RESERVED = ("Other", "Not connected")
GROUP_NAME = re.compile(r"[^\x00-\x1f:,]{1,40}")  # the settings page writes "Name: a, b"


class BoardError(ValueError):
    pass


def _name(n, what: str) -> str:
    if not isinstance(n, str) or n != n.strip() or not AGENT_NAME.fullmatch(n):
        raise BoardError(f"{what} {n!r} must be a name of 1 to 64 characters, no commas or surrounding spaces")
    return n


def check_groups(groups) -> list[dict]:
    """groups, validated: a list of {name, members}, group names unique, each member in one group only."""
    if not isinstance(groups, list):
        raise BoardError("groups must be a list of {name, members}")
    names, members = set(), {}
    for g in groups:
        if not isinstance(g, dict) or set(g) != {"name", "members"}:
            raise BoardError(f"each group is exactly {{name, members}}, not {g!r}")
        name = g["name"]
        if not isinstance(name, str) or name != name.strip() or not GROUP_NAME.fullmatch(name):
            raise BoardError(f"group name {name!r} must be 1 to 40 characters, no colons, commas or surrounding spaces")
        if name.casefold() in {r.casefold() for r in RESERVED}:
            raise BoardError(f"{name!r} is the board's own group; name yours something else")
        if name.casefold() in names:
            raise BoardError(f"two groups are named {name!r}")
        names.add(name.casefold())
        if not isinstance(g["members"], list):
            raise BoardError(f"group {name!r}: members must be a list of names")
        for m in g["members"]:
            m = _name(m, f"group {name!r}: member")
            if m.casefold() in members:
                raise BoardError(f"{m!r} is in both {members[m.casefold()]!r} and {name!r}; a member is in one group")
            members[m.casefold()] = name
    return groups


def check_hidden(hidden) -> list[str]:
    if not isinstance(hidden, list):
        raise BoardError("hidden must be a list of names")
    for n in hidden:
        _name(n, "hidden name")
    if len({n.casefold() for n in hidden}) != len(hidden):
        raise BoardError("hidden names someone twice")
    return hidden


def _write(path: Path, value) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    os.replace(tmp, path)


class Board:
    def __init__(self, root: Path, starting: list[dict]):
        self.groups_path = root / "groups.json"
        self.hidden_path = root / "board-hidden.json"
        self._lock = threading.Lock()
        if not self.groups_path.exists():
            try:
                _write(self.groups_path, check_groups(starting))
            except BoardError as err:
                raise ConfigError(f"[app] groups: {err}") from err
        for path, check in ((self.groups_path, check_groups), (self.hidden_path, check_hidden)):
            if path.exists():
                try:
                    check(json.loads(path.read_text()))
                except (BoardError, json.JSONDecodeError) as err:
                    raise RuntimeError(f"{path}: {err}; fix or remove it") from err

    def get(self) -> dict:
        """{groups, hidden}."""
        with self._lock:
            return {"groups": json.loads(self.groups_path.read_text()),
                    "hidden": json.loads(self.hidden_path.read_text()) if self.hidden_path.exists() else []}

    def change(self, patch, by: str) -> dict:
        """patch: {groups?, hidden?}, each replacing what is there. by: who, for the log."""
        if not isinstance(patch, dict) or not patch or set(patch) - {"groups", "hidden"}:
            raise BoardError("send {groups?, hidden?}: groups a list of {name, members}, hidden a list of names")
        groups = check_groups(patch["groups"]) if "groups" in patch else None
        hidden = check_hidden(patch["hidden"]) if "hidden" in patch else None
        with self._lock:
            if groups is not None:
                _write(self.groups_path, groups)
                log.info("board groups set by %s: %s", by,
                         "; ".join(f"{g['name']}: {', '.join(g['members'])}" for g in groups) or "none")
            if hidden is not None:
                _write(self.hidden_path, hidden)
                log.info("board hidden set by %s: %s", by, ", ".join(hidden) or "none")
        return self.get()
