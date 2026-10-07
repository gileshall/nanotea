"""Message storage: one directory per message."""

import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from nanotea import files
from nanotea.index import Index

ID_PATTERN = r"\d{8}-\d{6}-[0-9a-f]{4}"
DRAFT_PATTERN = r"[0-9a-f]{12}"
PENDING = {"queued", "rewriting", "speaking", "notifying"}
# What agents attach, by kind. Each is placed in the text with [[<kind> N]]; audio plays in the voice's track.
MEDIA = {"audio": {"mp3", "wav", "m4a", "aac", "flac", "aif", "aiff", "ogg", "opus", "webm", "caf"},
         "image": {"png", "jpg", "jpeg", "gif", "webp", "heic"},
         "video": {"mp4", "mov", "m4v"},
         "pdf": {"pdf"}}
KIND_OF_EXT = {ext: kind for kind, exts in MEDIA.items() for ext in exts}
AUDIO_MARKER = re.compile(r"\[\[audio (\d+)\]\]")
MARKER = re.compile(rf"\[\[({'|'.join(MEDIA)}) (\d+)\]\]")
SHOWN = re.compile(r"\[\[(image|video|pdf) (\d+)\]\]")  # what the page shows and the voice skips


@dataclass
class Message:
    id: str
    created: str
    sender: str
    title_hint: str | None
    ask: bool = False
    voice: str | None = None
    attachments: list[str] = field(default_factory=list)
    status: str = "queued"
    title: str | None = None
    audio: str | None = None
    error: str | None = None
    delivered: str | None = None  # when the owner was first notified; None for messages before escalation existed
    notify_error: str | None = None
    channel: str | None = None  # posted to #channel rather than to the owner directly
    raw: bool = False  # a question asking for its answer recorded raw: the mic's processing off
    control: dict | None = None  # a control's spec, as its plugin checked it; its state is in control.json
    re: dict | None = None  # the message it replies to: {id, kind, sender, title, thread}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def media_kind(name: str) -> str:
    """audio, image, video or pdf, by the file's extension. KeyError for anything else."""
    return KIND_OF_EXT[name.rsplit(".", 1)[-1].lower() if "." in name else ""]


def show_markers(text: str, attachments: list[str]) -> str:
    """Replace each [[<kind> N]] with the attachment's file name, for reading."""
    return MARKER.sub(lambda m: f"[{m[1]}: {attachments[int(m[2]) - 1].split('-', 1)[1]}]", text)


def check_markers(text: str, names: list[str]) -> None:
    """Each [[<kind> N]] names attachment N, once, as the kind it is."""
    refs = [int(n) for _, n in MARKER.findall(text)]
    for kind, n in MARKER.findall(text):
        r = int(n)
        if not 1 <= r <= len(names):
            raise ValueError(f"[[{kind} {r}]] has no matching attachment")
        if refs.count(r) > 1:
            raise ValueError(f"attachment {r} is placed more than once")
        if (actual := media_kind(names[r - 1])) != kind:
            raise ValueError(f"[[{kind} {r}]]: attachment {r}, {names[r - 1]!r}, is {actual}; write [[{actual} {r}]]")


class Store:
    """meta.json is written only by the worker; reply.json only under the reply lock. Every write re-syncs the
    message's row in the index."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._reply_lock = threading.Lock()
        self.index = Index(root)

    def _sync(self, msg_id: str) -> None:
        self.index.sync_msg(self.path(msg_id))

    def path(self, msg_id: str, name: str = "") -> Path:
        if not re.fullmatch(ID_PATTERN, msg_id):
            raise ValueError(f"bad message id: {msg_id!r}")
        return self.root / msg_id / name

    def exists(self, msg_id: str) -> bool:
        return bool(re.fullmatch(ID_PATTERN, msg_id)) and (self.root / msg_id).is_dir()

    def create(self, text: str, sender: str, title_hint: str | None, ask: bool, voice: str,
               files: list[tuple[str, bytes]], channel: str | None = None, raw: bool = False,
               control: dict | None = None, re_: dict | None = None) -> Message:
        now = datetime.now().astimezone()
        msg = Message(
            id=f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}",
            created=now.isoformat(timespec="microseconds"),
            sender=sender,
            title_hint=title_hint,
            ask=ask,
            voice=voice,
            attachments=[f"{n}-{re.sub(r'[^A-Za-z0-9._-]', '_', name)}" for n, (name, _) in enumerate(files, 1)],
            channel=channel,
            raw=raw,
            control=control,
            re=re_,
        )
        # Build in a hidden dir, then rename, so readers never see a partial message.
        staging = self.root / f".{msg.id}"
        staging.mkdir()
        (staging / "original.md").write_text(text)
        if files:
            (staging / "attachments").mkdir()
            for stored, (_, data) in zip(msg.attachments, files):
                (staging / "attachments" / stored).write_bytes(data)
        (staging / "meta.json").write_text(json.dumps(asdict(msg), indent=2))
        staging.rename(self.root / msg.id)
        self._sync(msg.id)
        return msg

    def save(self, msg: Message) -> None:
        tmp = self.path(msg.id, "meta.json.tmp")
        tmp.write_text(json.dumps(asdict(msg), indent=2))
        os.replace(tmp, self.path(msg.id, "meta.json"))
        self._sync(msg.id)

    def load(self, msg_id: str) -> Message:
        return Message(**json.loads(self.path(msg_id, "meta.json").read_text()))

    def read_text(self, msg_id: str, name: str) -> str | None:
        p = self.path(msg_id, name)
        return p.read_text() if p.exists() else None

    def all(self) -> list[Message]:
        """Newest first."""
        return self.query("1")

    def query(self, where: str, params: tuple = (), limit: int | None = None) -> list[Message]:
        """Messages from the index whose row matches where, newest first."""
        sql = f"SELECT meta FROM msg WHERE {where} ORDER BY t DESC, id DESC" + (" LIMIT ?" if limit else "")
        return [Message(**json.loads(m)) for (m,) in self.index.rows(sql, params + ((limit,) if limit else ()))]

    def search(self, q: str, limit: int | None, where: str = "1", params: tuple = ()) -> tuple[list[Message], int]:
        """Messages (those matching the index condition where) whose sender, title, text, spoken script or answer
        contain every word of q, ignoring case: the newest limit of them (all, with None), and how many there are.
        Reads each message's files, so it is a scan."""
        words = q.casefold().split()
        if not words:
            raise ValueError("search needs at least one word")
        found = []
        for msg in self.query(where, params):
            reply = self.reply(msg.id) or {}
            hay = "\n".join(str(x) for x in (
                msg.sender, msg.title, msg.title_hint, self.read_text(msg.id, "original.md"),
                self.read_text(msg.id, "script.txt"), reply.get("text"), reply.get("transcript")) if x).casefold()
            if all(w in hay for w in words):
                found.append(msg)
        return found[:limit], len(found)

    def as_json(self, msg: Message) -> dict:
        return {**asdict(msg), "dir": str(self.path(msg.id)), "reply": self.reply(msg.id),
                "seen": self.seen(msg.id), "heard": self.heard(msg.id),
                "answer_delivered": self.answer_delivered(msg.id), "set_aside": self.set_aside(msg.id)}

    # Owner-side state lives in marker files so it never races the worker's meta.json.

    def _flag(self, msg_id: str, name: str) -> str | None:
        return self.read_text(msg_id, name)

    def _set_flag(self, msg_id: str, name: str, value: str) -> None:
        self.path(msg_id, name).write_text(value)
        self._sync(msg_id)

    def heard(self, msg_id: str) -> str | None:
        return self._flag(msg_id, "heard")

    def mark_heard(self, msg_id: str) -> None:
        if self.heard(msg_id) is None:
            self._set_flag(msg_id, "heard", now_iso())
        self.mark_seen(msg_id)

    def seen(self, msg_id: str) -> str | None:
        return self._flag(msg_id, "seen")

    def mark_seen(self, msg_id: str) -> None:
        if self.seen(msg_id) is None:
            self._set_flag(msg_id, "seen", now_iso())

    def escalated(self, msg_id: str) -> str | None:
        return self._flag(msg_id, "escalated")

    def mark_escalated(self, msg_id: str, outcome: str) -> None:
        self._set_flag(msg_id, "escalated", f"{now_iso()} {outcome}")

    def answer_delivered(self, msg_id: str) -> str | None:
        """When the asker was given the answer to its question, and by what."""
        return self._flag(msg_id, "answer_delivered")

    def mark_answer_delivered(self, msg_id: str, listener: str | None) -> None:
        if self.answer_delivered(msg_id) is None:
            self._set_flag(msg_id, "answer_delivered", f"{now_iso()} {listener or 'listener not recorded'}")

    def set_aside(self, msg_id: str) -> str | None:
        """When the owner set the question aside unanswered. It stays answerable."""
        return self._flag(msg_id, "set_aside")

    def mark_set_aside(self, msg_id: str) -> None:
        if self.set_aside(msg_id) is None:
            self._set_flag(msg_id, "set_aside", now_iso())
        self.mark_seen(msg_id)

    def aside_delivered(self, msg_id: str) -> str | None:
        """When the asker was told its question was set aside, and by what."""
        return self._flag(msg_id, "aside_delivered")

    def mark_aside_delivered(self, msg_id: str, listener: str | None) -> None:
        if self.aside_delivered(msg_id) is None:
            self._set_flag(msg_id, "aside_delivered", f"{now_iso()} {listener or 'listener not recorded'}")

    def delete(self, msg_id: str) -> None:
        """Gone from the app, but every recording and transcript is kept, in deleted/<id>/."""
        kept = self.root / "deleted"
        kept.mkdir(exist_ok=True)
        self.path(msg_id, "deleted").write_text(now_iso())
        os.replace(self.path(msg_id), kept / msg_id)
        self.index.drop_msg(msg_id)

    def new_draft(self, msg_id: str, ext: str) -> tuple[str, Path]:
        drafts = self.path(msg_id, "drafts")
        drafts.mkdir(exist_ok=True)
        draft = secrets.token_hex(6)
        return draft, drafts / f"{draft}.{ext}"

    def draft_audio(self, msg_id: str, draft: str) -> Path | None:
        if not re.fullmatch(DRAFT_PATTERN, draft):
            raise ValueError(f"bad draft id: {draft!r}")
        found = list(self.path(msg_id, "drafts").glob(f"{draft}.*"))
        return found[0] if found else None

    def reply(self, msg_id: str) -> dict | None:
        text = self.read_text(msg_id, "reply.json")
        return json.loads(text) if text is not None else None

    def _write_reply(self, msg_id: str, reply: dict) -> None:
        tmp = self.path(msg_id, "reply.json.tmp")
        tmp.write_text(json.dumps(reply, indent=2))
        os.replace(tmp, self.path(msg_id, "reply.json"))
        self._sync(msg_id)

    def save_reply(self, msg_id: str, reply: dict, audio_src: Path | None,
                   file_drafts: list[tuple[Path, dict]] = ()) -> None:
        """Raises FileExistsError if the message already has a reply."""
        with self._reply_lock:
            if self.path(msg_id, "reply.json").exists():
                raise FileExistsError(f"{msg_id} already has a reply")
            if audio_src is not None:
                os.replace(audio_src, self.path(msg_id, reply["audio"]))
            reply["files"] = files.move_into(self.path(msg_id), list(file_drafts))
            self._write_reply(msg_id, reply)
            # Keep the original takes (a joined reply is a re-encoded copy) and any unsent ones.
            drafts = self.path(msg_id, "drafts")
            if drafts.exists():
                os.replace(drafts, self.path(msg_id, "takes"))

    def update_reply(self, msg_id: str, **changes) -> None:
        with self._reply_lock:
            self._write_reply(msg_id, {**self.reply(msg_id), **changes})

    def control_state(self, msg_id: str) -> dict:
        text = self.read_text(msg_id, "control.json")
        return json.loads(text) if text is not None else {}

    def set_control_state(self, msg_id: str, state: dict) -> None:
        tmp = self.path(msg_id, "control.json.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.path(msg_id, "control.json"))
        self._sync(msg_id)

    def reactions(self, msg_id: str) -> list[str]:
        """The owner's emoji reactions to this message."""
        text = self.read_text(msg_id, "reactions.json")
        return json.loads(text) if text is not None else []

    def set_reaction(self, msg_id: str, emoji: str, on: bool) -> None:
        with self._reply_lock:
            current = [r for r in self.reactions(msg_id) if r != emoji] + ([emoji] if on else [])
            tmp = self.path(msg_id, "reactions.json.tmp")
            tmp.write_text(json.dumps(current))
            os.replace(tmp, self.path(msg_id, "reactions.json"))
            self._sync(msg_id)
