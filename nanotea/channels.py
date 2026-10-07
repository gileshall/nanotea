"""Public channels: #<name> conversations every agent can post to and listen on, and the owner reads and writes.

data/channels.json lists them ({name: {created, by, topic}}). Agents' posts are ordinary messages in the
store, tagged with their channel. What the owner posts lives in data/channel-<name>/messages/<id>/, like an
inbox's. Delivery is per agent and at least once: data/channel-<name>/listeners.json keeps each agent's
cursor, the time it first listened here and every item it has been given since.
"""

import json
import re
import threading
from pathlib import Path

from nanotea.index import Index
from nanotea.inbox import Folder
from nanotea.store import ID_PATTERN, now_iso

NAME = r"[a-z0-9][a-z0-9-]{0,39}"
# An item a listener was given: a post (agent's or the owner's) by id, or the owner's answer to a question.
ITEM_KEY = rf"(?:answer-)?{ID_PATTERN}"


class ChannelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Channel(Folder):
    def __init__(self, root: Path, name: str, info: dict, index: Index):
        super().__init__(root, index)
        self.name = name
        self.info = info
        self._listeners = root / "listeners.json"
        self.last_listen: dict[str, float] = {}  # agent -> monotonic time of its last poll

    def add(self, text: str | None, audio_src: Path | None, re_: dict | None, reaction: bool = False,
            file_drafts: list[tuple[Path, dict]] = (), tap: dict | None = None) -> dict:
        """re_: the post it answers ({id, sender, title}). reaction: text is an emoji the owner reacted to re_ with.
        tap: text is what the owner tapped on re_'s control ({control, act, value, data})."""
        return self._add({
            "re": re_,
            "reaction": reaction,
            "tap": tap,
            "reactions": [],  # agents' emoji reactions to it: {by, emoji, at}
            "text": text,
            "deliveries": [],  # {to, at, by}: every listener that was given it
        }, audio_src, file_drafts)

    def listeners(self) -> dict:
        """agent -> {since, delivered}"""
        return json.loads(self._listeners.read_text()) if self._listeners.exists() else {}

    def cursor(self, agent: str) -> dict:
        """The agent's cursor; one that has never listened here starts now."""
        with self._lock:
            listeners = self.listeners()
            if agent not in listeners:
                listeners[agent] = {"since": now_iso(), "delivered": []}
                self._write(self._listeners, listeners)
            return listeners[agent]

    def mark_delivered(self, agent: str, keys: list[str], listener: str | None) -> None:
        for key in keys:
            if not re.fullmatch(ITEM_KEY, key):
                raise ValueError(f"bad item id: {key!r}")
        at = now_iso()
        with self._lock:
            listeners = self.listeners()
            if agent not in listeners:
                raise ChannelError(409, f"{agent!r} has never listened to #{self.name}")
            done = listeners[agent]["delivered"]
            done += [k for k in keys if k not in done]
            self._write(self._listeners, listeners)
            for key in keys:
                path = self._msg_dir(key) / "meta.json" if re.fullmatch(ID_PATTERN, key) else None
                if path is not None and path.exists():
                    msg = json.loads(path.read_text())
                    msg["deliveries"].append({"to": agent, "at": at, "by": listener})
                    self._write(path, msg)
                    self._sync(key)


class Channels:
    def __init__(self, data: Path, index: Index, by: str, starting: dict[str, str]):
        """starting: {name: topic} for the channels a new install begins with; by: who is credited with them."""
        self.data = data
        self.index = index
        self.path = data / "channels.json"
        self._lock = threading.Lock()
        if not self.path.exists():
            Folder._write(self.path, {name: {"created": now_iso(), "by": by, "topic": topic}
                                      for name, topic in starting.items()})
        self._open = {name: Channel(data / f"channel-{name}", name, info, index)
                      for name, info in json.loads(self.path.read_text()).items()}

    def all(self) -> list[Channel]:
        return list(self._open.values())

    def get(self, name: str) -> Channel:
        if name not in self._open:
            known = ", ".join(f"#{n}" for n in self._open)
            raise ChannelError(404, f"no channel #{name}; the owner makes channels in the app. There are: {known}")
        return self._open[name]

    def create(self, name: str, by: str) -> Channel:
        if not (isinstance(name, str) and re.fullmatch(NAME, name)):
            raise ChannelError(400, "a channel name is lowercase letters, digits and dashes, at most 40")
        with self._lock:
            if name in self._open:
                raise ChannelError(409, f"#{name} already exists")
            info = {"created": now_iso(), "by": by, "topic": ""}
            Folder._write(self.path, {**json.loads(self.path.read_text()), name: info})
            self._open[name] = Channel(self.data / f"channel-{name}", name, info, self.index)
            return self._open[name]
