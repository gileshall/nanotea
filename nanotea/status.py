"""What each agent says it's doing (nanotea-tell --status). status.json holds the current ones, {sender: {text, at}};
status-log.jsonl keeps every change, a cleared status as text ""."""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from nanotea.store import now_iso

STATUS_MAX = 140


class StatusError(ValueError):
    pass


class StatusBook:
    def __init__(self, root: Path):
        self.path = root / "status.json"
        self.log_path = root / "status-log.jsonl"
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict]:
        with self._lock:
            return self._load()

    def _load(self) -> dict[str, dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def set(self, sender, text) -> dict | None:
        """Sets the sender's status, or clears it for "". Returns the new one, or None once cleared."""
        if not (isinstance(sender, str) and sender.strip()):
            raise StatusError("name must be a non-empty string")
        if not isinstance(text, str):
            raise StatusError("text must be a string; \"\" clears the status")
        sender, text = sender.strip(), text.strip()
        if "\n" in text or "\r" in text:
            raise StatusError("a status is one line")
        if len(text) > STATUS_MAX:
            raise StatusError(f"a status is at most {STATUS_MAX} characters, not {len(text)}")
        at = now_iso()
        with self._lock:
            book = self._load()
            with self.log_path.open("a") as log:
                log.write(json.dumps({"name": sender, "text": text, "at": at}) + "\n")
            if text:
                book[sender] = {"text": text, "at": at}
            else:
                book.pop(sender, None)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(book, indent=2, sort_keys=True))
            os.replace(tmp, self.path)
            return book.get(sender)


class ListenBook:
    """When each agent's listener last polled, in any inbox or channel: listened.json, {sender: at}. Polls come
    every few seconds, so a time is rewritten only once it is LISTEN_STEP_S old."""

    LISTEN_STEP_S = 60

    def __init__(self, root: Path):
        self.path = root / "listened.json"
        self._lock = threading.Lock()
        self._book = json.loads(self.path.read_text()) if self.path.exists() else {}

    def all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._book)

    def saw(self, sender: str) -> None:
        now = datetime.now().astimezone()
        with self._lock:
            last = self._book.get(sender)
            if last is not None and (now - datetime.fromisoformat(last)).total_seconds() < self.LISTEN_STEP_S:
                return
            self._book[sender] = now.isoformat(timespec="microseconds")
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._book, indent=2, sort_keys=True))
            os.replace(tmp, self.path)
