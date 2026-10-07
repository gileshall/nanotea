"""Agents' messages to each other (the tell tool): data/talk/<id>.json, {id, from, to, text, at, delivered_at,
deliveries}. Delivery is at least once, as with the owner's: pending until the recipient's session marks it."""

import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path

from nanotea.store import now_iso

TALK_MAX = 4000


class TalkError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class TalkBook:
    def __init__(self, root: Path):
        self.root = root / "talk"
        self.root.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self._msgs = {p.stem: json.loads(p.read_text()) for p in self.root.glob("*.json")}

    def _write(self, msg: dict) -> None:
        tmp = self.root / f"{msg['id']}.json.tmp"
        tmp.write_text(json.dumps(msg, indent=2))
        os.replace(tmp, self.root / f"{msg['id']}.json")

    def add(self, sender: str, to: str, text) -> dict:
        if not (isinstance(text, str) and text.strip()):
            raise TalkError(400, "a message needs text")
        if len(text) > TALK_MAX:
            raise TalkError(400, f"a message to another agent is at most {TALK_MAX} characters, not {len(text)}")
        at = now_iso()
        msg = {"id": f"{datetime.fromisoformat(at):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}", "from": sender, "to": to,
               "text": text.strip(), "at": at, "delivered_at": None, "deliveries": []}
        with self._lock:
            self._write(msg)
            self._msgs[msg["id"]] = msg
        return dict(msg)

    def pending(self, name: str) -> list[dict]:
        with self._lock:
            return sorted((dict(m) for m in self._msgs.values()
                           if m["to"].casefold() == name.casefold() and m["delivered_at"] is None),
                          key=lambda m: m["id"])

    def mark_delivered(self, name: str, ids: list[str], listener: str | None) -> None:
        with self._lock:
            for i in ids:
                msg = self._msgs.get(i)
                if msg is None:
                    raise TalkError(404, f"no agent message {i!r}")
                if msg["to"].casefold() != name.casefold():
                    raise TalkError(409, f"{i} is for {msg['to']!r}, not {name!r}")
            at = now_iso()
            for i in ids:
                msg = self._msgs[i]
                if msg["delivered_at"] is None:
                    msg["delivered_at"] = at
                msg["deliveries"].append({"at": at, "by": listener})
                self._write(msg)

    def recent(self, n: int) -> list[dict]:
        """The last n, oldest first."""
        with self._lock:
            return [dict(m) for m in sorted(self._msgs.values(), key=lambda m: m["id"])[-n:]]
