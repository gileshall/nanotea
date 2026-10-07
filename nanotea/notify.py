"""Notifier plugins (kind notify). Each takes a Note: app push for every message, iMessage for escalations."""

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from nanotea.config import Option
from nanotea.plugins import API, Context

log = logging.getLogger(__name__)

# The recipient and the text come in files, never argv: any local user can read a process's arguments, and the
# text's link carries the pairing key.
SEND_IMESSAGE = """
on run argv
    set dir to item 1 of argv
    set who to read POSIX file (dir & "/to") as «class utf8»
    set msg to read POSIX file (dir & "/text") as «class utf8»
    tell application "Messages"
        set svc to first account whose service type is iMessage
        send msg to participant who of svc
    end tell
end run
"""


@dataclass
class Note:
    title: str
    text: str
    path: str    # in-app location, e.g. /m/<id>
    link: str    # absolute link that also pairs the device that opens it
    tag: str
    unseen: int


class IMessage:
    """Sends through Messages.app on this machine."""
    api = API
    OPTIONS = (Option("to", str, "Who gets the text: a phone number or an Apple ID email."),)

    def __init__(self, cfg: dict, ctx: Context):
        self.to = ctx.need(cfg, "to", str)
        if not self.to:
            raise ValueError("notify.imessage.to is empty")

    def send(self, note: Note) -> None:
        self.send_text(f"{note.title}\n\n{note.text}\n\n{note.link}")

    def send_text(self, text: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:  # 0700: only this user reads what is in it
            (Path(tmp) / "to").write_text(self.to)
            (Path(tmp) / "text").write_text(text)
            proc = subprocess.run(["osascript", "-e", SEND_IMESSAGE, tmp], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"osascript exited {proc.returncode}: {proc.stderr.strip()}")


class Push:
    """Web Push to every subscribed browser; fails only if none received it."""
    api = API
    BODY_MAX = 240
    OPTIONS = (Option("subject", str, "The contact the push services see: mailto:you@example.com, or https://host "
                                      "with no port or path. Required while push is in now or escalate."),)

    def __init__(self, cfg: dict, ctx: Context):
        self.book = ctx.services["push"]

    def send(self, note: Note) -> None:
        subs = self.book.subscriptions()
        if not subs:
            raise RuntimeError("no browser has turned on notifications")
        body = note.text if len(note.text) <= self.BODY_MAX else note.text[:self.BODY_MAX - 3] + "..."
        payload = {"title": note.title, "body": body, "url": note.path, "tag": note.tag, "unseen": note.unseen}
        errors = []
        for sub in subs:
            try:
                self.book.send(sub, payload)
            except Exception as e:
                log.exception("push to %s failed", sub["endpoint"][:60])
                errors.append(f"{type(e).__name__}: {e}")
        if len(errors) == len(subs):
            raise RuntimeError("push failed for every browser: " + "; ".join(errors))
