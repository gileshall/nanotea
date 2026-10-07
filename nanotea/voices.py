"""Which sender speaks with which voice. Senders choose; one sender per voice; choices persist."""

import json
import os
import threading
from pathlib import Path


class VoiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class VoiceBook:
    def __init__(self, path: Path):
        self.path = path
        self.names_path = path.with_name("names.json")  # sender -> first name, shown as "Name (sender)"
        self._lock = threading.Lock()

    def names(self) -> dict[str, str]:
        with self._lock:
            return json.loads(self.names_path.read_text()) if self.names_path.exists() else {}

    def apply_roster(self, roster: list[dict], known_voices: set[str]) -> None:
        """Bind each {from, voice, name} at once, or none of them: one sender per voice, one per name."""
        with self._lock:
            book = self._load()
            names = json.loads(self.names_path.read_text()) if self.names_path.exists() else {}
            for row in roster:
                sender, voice, name = row["from"], row["voice"], row["name"]
                for field, value in (("from", sender), ("voice", voice), ("name", name)):
                    if not (isinstance(value, str) and value.strip() and len(value) <= 80):
                        raise VoiceError(400, f"{field} must be a short non-empty string: {row!r}")
                if voice not in known_voices:
                    raise VoiceError(400, f"unknown voice {voice!r} for {sender!r}; see nanotea-tell --voices")
                book[sender.strip()], names[sender.strip()] = voice, name.strip()
            for mapping, what in ((book, "voice"), (names, "name")):
                seen: dict[str, str] = {}
                for sender, value in mapping.items():
                    key = value.lower()
                    if key in seen:
                        raise VoiceError(409, f"{what} {value!r} would belong to both {seen[key]!r} and {sender!r}")
                    seen[key] = sender
            for path, data in ((self.path, book), (self.names_path, names)):
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
                os.replace(tmp, path)

    def _load(self) -> dict[str, str]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def voice_of(self, sender: str) -> str | None:
        with self._lock:
            return self._load().get(sender)

    def owners(self) -> dict[str, str]:
        """voice id -> sender"""
        with self._lock:
            return {v: s for s, v in self._load().items()}

    def resolve(self, sender: str, requested: str | None) -> str:
        with self._lock:
            book = self._load()
            if requested is None:
                if sender not in book:
                    raise VoiceError(400, (
                        f"{sender!r} has no voice yet. Pick a free one that fits how you identify "
                        "(nanotea-tell --voices), then send with --voice ID. The choice is remembered."))
                return book[sender]
            owner = next((s for s, v in book.items() if v == requested and s != sender), None)
            if owner is not None:
                raise VoiceError(409, f"voice {requested!r} belongs to {owner!r}; pick a free one")
            if book.get(sender) != requested:
                book[sender] = requested
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(book, indent=2, sort_keys=True))
                os.replace(tmp, self.path)
            return requested
