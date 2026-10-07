"""The owner's inbox for the main agent: the owner writes any time, or replies to any agent's message through it;
the main agent picks messages up with nanotea-tell --listen. One agent holds the inbox at a time.

data/inbox/inbox.json, and messages/<id>/meta.json plus any audio. Delivery is at least once:
messages stay pending until the holder's listener marks them delivered.
"""

import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

from nanotea import files
from nanotea.index import Index
from nanotea.store import DRAFT_PATTERN, ID_PATTERN, now_iso


def excerpt(m: dict) -> str | None:
    """A line of what the owner said, to name the message a reply answers; None before a recording is transcribed."""
    words = " ".join((m.get("text") or m.get("transcript") or "").split())
    if not words:
        return None
    return words if len(words) <= 80 else words[:77].rstrip() + "..."


class InboxError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Folder:
    """What the owner sends to one place: messages/<id>/ (meta.json plus any audio and files), and drafts/
    for the recordings and files of messages they are still composing. Every write re-syncs the message's row in
    the index, as folder root.name."""

    def __init__(self, root: Path, index: Index):
        self.root = root
        self.index = index
        self.folder = root.name
        (root / "messages").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _sync(self, msg_id: str) -> None:
        self.index.sync_owner(self.folder, self._msg_dir(msg_id))

    @staticmethod
    def _write(path: Path, obj) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, path)

    def new_draft(self, ext: str) -> tuple[str, Path]:
        drafts = self.root / "drafts"
        drafts.mkdir(exist_ok=True)
        draft = secrets.token_hex(6)
        return draft, drafts / f"{draft}.{ext}"

    def draft_audio(self, draft: str) -> Path | None:
        if not re.fullmatch(DRAFT_PATTERN, draft):
            raise ValueError(f"bad draft id: {draft!r}")
        found = list((self.root / "drafts").glob(f"{draft}.*"))
        return found[0] if found else None

    @property
    def drafts(self) -> Path:
        return self.root / "drafts"

    def path_of(self, msg_id: str, stored: str) -> Path:
        return self._msg_dir(msg_id) / stored

    def _add(self, fields: dict, audio_src: Path | None, file_drafts: list[tuple[Path, dict]]) -> dict:
        now = datetime.now().astimezone()
        msg = {
            "id": f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}",
            "at": now.isoformat(timespec="microseconds"),  # orders messages sent within one second
            **fields,
            "audio": f"audio{audio_src.suffix}" if audio_src else None,
            "transcript": None,
            "transcript_status": "pending" if audio_src else "none",
            "transcript_error": None,
        }
        with self._lock:
            # Build in a hidden dir, then rename, so a listener never sees a partial message.
            staging = self.root / "messages" / f".{msg['id']}"
            staging.mkdir()
            if audio_src:
                os.replace(audio_src, staging / msg["audio"])
            msg["files"] = files.move_into(staging, list(file_drafts))
            self._write(staging / "meta.json", msg)
            staging.rename(self.root / "messages" / msg["id"])
            self._sync(msg["id"])
        return msg

    def _msg_dir(self, msg_id: str) -> Path:
        if not re.fullmatch(ID_PATTERN, msg_id):
            raise ValueError(f"bad message id: {msg_id!r}")
        return self.root / "messages" / msg_id

    def messages(self, where: str = "1", params: tuple = (), limit: int | None = None) -> list[dict]:
        """From the index, those whose row matches where; newest first."""
        sql = (f"SELECT meta FROM owner WHERE folder = ? AND ({where}) ORDER BY t DESC, id DESC"
               + (" LIMIT ?" if limit else ""))
        args = (self.folder, *params) + ((limit,) if limit else ())
        return [json.loads(m) for (m,) in self.index.rows(sql, args)]

    def audio_path(self, msg_id: str) -> Path | None:
        meta = self._msg_dir(msg_id) / "meta.json"
        if not meta.exists():
            return None
        audio = json.loads(meta.read_text())["audio"]
        return self._msg_dir(msg_id) / audio if audio else None

    def update(self, msg_id: str, **changes) -> None:
        with self._lock:
            path = self._msg_dir(msg_id) / "meta.json"
            self._write(path, {**json.loads(path.read_text()), **changes})
            self._sync(msg_id)

    def react(self, msg_id: str, by: str, emoji: str) -> dict:
        """An agent reacts to one of the owner's messages. Returns the message."""
        with self._lock:
            path = self._msg_dir(msg_id) / "meta.json"
            msg = json.loads(path.read_text())
            # Messages from before reactions existed have no "reactions" key.
            kept = [r for r in msg.get("reactions", []) if (r["by"], r["emoji"]) != (by, emoji)]
            msg["reactions"] = kept + [{"by": by, "emoji": emoji, "at": now_iso()}]
            self._write(path, msg)
            self._sync(msg_id)
            return msg


class Inbox(Folder):
    def __init__(self, root: Path, index: Index):
        super().__init__(root, index)
        self._meta = root / "inbox.json"

    def info(self) -> dict:
        """holder is None when no agent holds the inbox. owner_delivered_at: when the holder was last given one of
        the owner's own messages (not an event or a reaction)."""
        return json.loads(self._meta.read_text()) if self._meta.exists() else {"holder": None, "about": None}

    def claim(self, name: str, about: str | None) -> dict:
        with self._lock:
            info = self.info()
            if info["holder"] not in (None, name):
                raise InboxError(409, f"the inbox is held by {info['holder']!r}; it must run "
                                      "nanotea-tell --close-inbox first")
            if info["holder"] is None:
                info = {"holder": name, "since": now_iso(), "about": None}
            if about is not None:
                info["about"] = about
            self._write(self._meta, info)
            return info

    def release(self, name: str) -> None:
        with self._lock:
            self.check_holder(name)
            self._write(self._meta, {"holder": None, "about": None})

    def check_holder(self, name: str) -> None:
        holder = self.info()["holder"]
        if holder is None:
            raise InboxError(409, f"nobody holds the inbox; {name!r} can claim it with nanotea-tell --listen")
        if holder != name:
            raise InboxError(409, f"the inbox is held by {holder!r}, not {name!r}")

    def add(self, text: str | None, audio_src: Path | None, to: str | None, re_: dict | None,
            reaction: bool = False, event: dict | None = None,
            file_drafts: list[tuple[Path, dict]] = (), tap: dict | None = None, bang: dict | None = None) -> dict:
        """to: the holder when the owner wrote it. re_: the agent message it answers ({id, sender, title}).
        reaction: text is an emoji the owner reacted to re_ with. event: not the owner's words but something a
        program on this machine reports ({kind, source, summary, data}), e.g. that they tapped Done on a page.
        tap: text is what the owner tapped on re_'s control ({control, act, value, data}).
        bang: text is a command the owner asked this line's session to run; see App.bang_send for its fields."""
        return self._add({
            "to": to,
            "re": re_,
            "reaction": reaction,
            "tap": tap,
            "event": event,
            "bang": bang,
            "reactions": [],  # the holder's emoji reactions to this message: {by, emoji, at}
            "text": text,
            "delivered_to": None,
            "delivered_at": None,
        }, audio_src, file_drafts)

    def pending(self) -> list[dict]:
        """Undelivered messages whose transcription (if any) is finished, oldest first. A bang command is
        the agent's only once it has run."""
        return [m for m in self.messages("delivered_at IS NULL AND transcript_status != 'pending'")[::-1]
                if not m.get("bang") or m["bang"]["state"] == "done"]

    def mark_delivered(self, name: str, ids: list[str], listener: str | None = None) -> None:
        """listener: the listener process that printed them (e.g. "pid 123"). Every delivery is kept, so
        a message two listeners printed shows both; delivered_to and delivered_at stay the first."""
        self.check_holder(name)
        at = now_iso()
        owners = False  # one of the owner's own messages among them
        with self._lock:
            for msg_id in ids:
                path = self._msg_dir(msg_id) / "meta.json"
                msg = json.loads(path.read_text())
                if msg["delivered_at"] is None:
                    msg["delivered_to"], msg["delivered_at"] = name, at
                    owners = owners or not any(msg.get(k) for k in ("event", "reaction", "tap", "bang"))
                # Messages delivered before listeners were recorded have no "deliveries".
                msg["deliveries"] = msg.get("deliveries", []) + [{"to": name, "at": at, "by": listener}]
                self._write(path, msg)
                self._sync(msg_id)
            if owners:
                self._write(self._meta, {**self.info(),
                                         "owner_delivered_at": datetime.now().astimezone().isoformat()})
