"""When a message may notify the owner: data/hush.json. Muted lines, questions only, and quiet hours.

None of it hides a message from the app. It only decides whether a notification goes out for it, now or at all.
Quiet hours hold notifications and the escalation clock until they end; the held ones come back as one summary.
Times are the service's local time."""

import json
import os
import re
import threading
from datetime import datetime, time
from pathlib import Path

from nanotea.store import Message

DEFAULTS = {"muted": [], "questions_only": False, "quiet": None}
CLOCK = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
NAME_MAX = 80


class HushError(ValueError):
    pass


def _clock(text) -> time:
    if not (isinstance(text, str) and (m := CLOCK.fullmatch(text))):
        raise HushError(f"{text!r} is not a time of day as HH:MM, 24 hours")
    return time(int(m[1]), int(m[2]))


def check(patch) -> dict:
    """A partial change, validated. muted: agent names and #channels. quiet: {start, end} or null."""
    if not isinstance(patch, dict) or not patch:
        raise HushError("send an object of notification settings to change")
    for key, value in patch.items():
        if key not in DEFAULTS:
            raise HushError(f"unknown notification setting {key!r}; known: {', '.join(DEFAULTS)}")
        if key == "muted":
            if not (isinstance(value, list) and all(isinstance(n, str) and n.strip() and len(n) <= NAME_MAX
                                                    for n in value)):
                raise HushError("'muted' must be a list of agent names and #channels")
        elif key == "questions_only":
            if not isinstance(value, bool):
                raise HushError("questions_only must be true or false")
        elif value is not None:
            if not (isinstance(value, dict) and set(value) == {"start", "end"}):
                raise HushError("'quiet' must be null or {start, end} as HH:MM")
            if _clock(value["start"]) == _clock(value["end"]):
                raise HushError("quiet hours must start and end at different times")
    return patch


def key_of(msg: Message) -> list[str]:
    """The names a mute can match: the sender, and the channel as #name."""
    return [msg.sender.casefold()] + ([f"#{msg.channel}".casefold()] if msg.channel else [])


class HushBook:
    def __init__(self, root: Path):
        self.path = root / "hush.json"
        self.held_path = root / "hush-held.json"
        self._lock = threading.Lock()
        stored = json.loads(self.path.read_text()) if self.path.exists() else {}
        if stored:
            check(stored)
        self._now = {**DEFAULTS, **stored}
        self._held = json.loads(self.held_path.read_text()) if self.held_path.exists() else []

    def get(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._now))

    def change(self, patch) -> dict:
        check(patch)
        if "muted" in patch:
            patch = {**patch, "muted": sorted({n.strip() for n in patch["muted"]}, key=str.casefold)}
        with self._lock:
            self._now = {**self._now, **patch}
            self._write(self.path, self._now)
            return json.loads(json.dumps(self._now))

    @staticmethod
    def _write(path: Path, obj) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, path)

    def quiet_now(self, now: datetime | None = None) -> bool:
        quiet = self.get()["quiet"]
        if quiet is None:
            return False
        at = (now or datetime.now().astimezone()).time().replace(second=0, microsecond=0)
        start, end = _clock(quiet["start"]), _clock(quiet["end"])
        return start <= at < end if start < end else at >= start or at < end

    def muted(self, msg: Message) -> bool:
        names = {n.casefold() for n in self.get()["muted"]}
        return any(k in names for k in key_of(msg))

    def verdict(self, msg: Message) -> str:
        """send, muted, skipped (not a question, with questions_only) or held (in quiet hours). A failure
        is never skipped: the owner should hear it unless the line is muted."""
        if self.muted(msg):
            return "muted"
        if self.get()["questions_only"] and not msg.ask and msg.status != "failed":
            return "skipped"
        return "held" if self.quiet_now() else "send"

    def hold(self, msg_id: str) -> None:
        with self._lock:
            self._held.append(msg_id)
            self._write(self.held_path, self._held)

    def take_held(self) -> list[str]:
        """The held message ids, once quiet hours are over; empty until then."""
        if self.quiet_now():
            return []
        with self._lock:
            held, self._held = self._held, []
            if held:
                self._write(self.held_path, self._held)
            return held
