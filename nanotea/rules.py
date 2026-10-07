"""Standing rules: what the owner said once and means for good. data/rules.json, [{id, text, for, at, from}]:
for is an agent's name, or None for every agent; from is the owner's message it was pinned from, if any.
rules-log.jsonl keeps every rule added and removed."""

import hashlib
import json
import os
import secrets
import threading
from pathlib import Path

from nanotea.store import now_iso

RULE_MAX = 500


class RuleError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class RuleBook:
    def __init__(self, root: Path):
        self.path = root / "rules.json"
        self.log_path = root / "rules-log.jsonl"
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def _save(self, rules: list[dict], change: dict) -> None:
        with self.log_path.open("a") as log:
            log.write(json.dumps({**change, "logged": now_iso()}) + "\n")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rules, indent=2))
        os.replace(tmp, self.path)

    def all(self) -> list[dict]:
        with self._lock:
            return self._load()

    def for_agent(self, name: str) -> list[dict]:
        """The rules an agent follows: everyone's and its own, oldest first."""
        return [r for r in self.all() if r["for"] is None or r["for"].casefold() == name.casefold()]

    @staticmethod
    def version(rules: list[dict]) -> str:
        return hashlib.sha256(json.dumps([(r["id"], r["text"]) for r in rules]).encode()).hexdigest()[:12]

    def add(self, text, for_, from_msg) -> dict:
        if not (isinstance(text, str) and text.strip()):
            raise RuleError(400, "a rule needs text")
        text = text.strip()
        if len(text) > RULE_MAX:
            raise RuleError(400, f"a rule is at most {RULE_MAX} characters, not {len(text)}")
        if for_ is not None and not (isinstance(for_, str) and for_.strip()):
            raise RuleError(400, "'for' must be an agent's name, or null for every agent")
        if from_msg is not None and not isinstance(from_msg, str):
            raise RuleError(400, "'from' must be a message id")
        rule = {"id": secrets.token_hex(4), "text": text, "for": for_.strip() if for_ else None, "at": now_iso(),
                "from": from_msg}
        with self._lock:
            self._save(self._load() + [rule], {"added": rule})
        return rule

    def delete(self, rule_id: str) -> None:
        with self._lock:
            rules = self._load()
            kept = [r for r in rules if r["id"] != rule_id]
            if len(kept) == len(rules):
                raise RuleError(404, f"no rule {rule_id!r}")
            self._save(kept, {"removed": next(r for r in rules if r["id"] == rule_id)})
