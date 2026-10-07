"""Requests for a first token: how an agent or an event source without one gets one. data/enroll.json (mode 0600).

A program on the service's machine asks, naming who it is; the service answers with the request's id and a secret
that only the asker holds. The approver (an agent the owner chose, nanotea/tokens.py) or the owner approves or
denies it. The asker then collects its token with the secret: the token is made at that moment and sent to the
asker alone, so whoever approved never sees it.

enroll.json is {"version": 1, "requests": [{id, kind, name, line, dir, from, had, asked, sha256, state, decided,
by, reason, told, token}]}. state: pending, approved, denied, collected, expired, replaced (asked again), or failed
(approved, but the token couldn't be made). A pending request expires after PENDING_S, an approved one not collected
after APPROVED_S. Closed requests are kept for KEEP_S."""

import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from nanotea.config import ConfigError
from nanotea.store import now_iso
from nanotea.tokens import KINDS, LINE, TokenBook, TokenError, check_name, digest, same

PENDING_S = 24 * 3600
APPROVED_S = 7 * 24 * 3600
KEEP_S = 30 * 24 * 3600
MAX_PENDING = 20
DIR_MAX = 500
REASON_MAX = 500
OPEN = ("pending", "approved")


def _older(at: str, seconds: int, now: datetime) -> bool:
    return now - datetime.fromisoformat(at) > timedelta(seconds=seconds)


class EnrollBook:
    def __init__(self, root: Path, tokens: TokenBook):
        self.path = root / "enroll.json"
        self.tokens = tokens
        self._lock = threading.Lock()
        self._requests: list[dict] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if data["version"] != 1 or not isinstance(data["requests"], list):
                    raise ValueError("not a version 1 request file")
                self._requests = data["requests"]
            except (ValueError, KeyError, TypeError) as err:
                raise ConfigError(f"{self.path} is unreadable ({type(err).__name__}: {err}); restore it from a "
                                  f"backup, or move it aside: the agents waiting on approval then ask again") from err

    def _save(self, requests: list[dict]) -> None:
        now = datetime.now().astimezone()
        requests = [r for r in requests if r["state"] in OPEN or not _older(r["decided"] or r["asked"], KEEP_S, now)]
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"version": 1, "requests": requests}, indent=2))
        os.replace(tmp, self.path)
        self._requests = requests

    def _expire(self) -> None:
        """Called holding _lock."""
        now = datetime.now().astimezone()
        at = now.isoformat(timespec="microseconds")
        changed = False
        out = []
        for r in self._requests:
            if (r["state"] == "pending" and _older(r["asked"], PENDING_S, now)
                    or r["state"] == "approved" and _older(r["decided"], APPROVED_S, now)):
                r = {**r, "state": "expired", "decided": at, "by": None}
                changed = True
            out.append(r)
        if changed:
            self._save(out)

    @staticmethod
    def public(r: dict) -> dict:
        return {k: v for k, v in r.items() if k != "sha256"}

    def ask(self, kind, name, line, where, address: str) -> tuple[dict, str]:
        """A new request: (its record, the secret that collects it). where: the asker's working directory, its own
        word. address: where the request came from. Asking again for the same name replaces an open request."""
        if kind not in KINDS:
            raise TokenError(400, f"'kind' must be one of {', '.join(KINDS)}")
        check_name(name, kind)
        if kind == "events" and line is None:
            raise TokenError(400, "an event source names 'line', the line it reports to: main, or a direct line's "
                                  "name (lowercase letters, digits, dashes)")
        if line is not None and not (isinstance(line, str) and LINE.fullmatch(line)):
            raise TokenError(400, "'line' must be main, or a direct line's name (lowercase letters, digits, dashes)")
        if where is not None and not (isinstance(where, str) and len(where) <= DIR_MAX and where.isprintable()):
            raise TokenError(400, f"'dir' must be a directory's path, at most {DIR_MAX} characters")
        if held := self.tokens.has_live(kind, name):
            raise TokenError(409, f"{held!r} already has {'an events token' if kind == 'events' else 'a token'}. Use "
                                  f"its token file; if that is lost, the owner revokes the token in the app, under "
                                  f"Tokens, and {held!r} asks again")
        secret = secrets.token_urlsafe(32)
        at = now_iso()
        with self._lock:
            self._expire()
            mine = [r for r in self._requests if r["state"] in OPEN and r["kind"] == kind
                    and r["name"].casefold() == name.casefold()]
            if sum(r["state"] == "pending" for r in self._requests) - len(mine) >= MAX_PENDING:
                raise TokenError(429, f"{MAX_PENDING} requests are waiting already; they need a decision first")
            record = {"id": secrets.token_hex(4), "kind": kind, "name": name, "line": line, "dir": where,
                      "from": address, "had": self.tokens.revoked_at(kind, name), "asked": at,
                      "sha256": digest(secret), "state": "pending", "decided": None, "by": None, "reason": None,
                      "told": None, "token": None}
            gone = {r["id"] for r in mine}
            self._save([{**r, "state": "replaced", "decided": at} if r["id"] in gone else r
                        for r in self._requests] + [record])
        return self.public(record), secret

    def waiting(self, new: bool = False) -> list[dict]:
        """Requests waiting on a decision, oldest first. new: only those the approver hasn't been given."""
        with self._lock:
            self._expire()
            return [self.public(r) for r in self._requests if r["state"] == "pending" and not (new and r["told"])]

    def get(self, ident: str) -> dict:
        with self._lock:
            self._expire()
            return self.public(self._find(ident))

    def told(self, ids: list[str]) -> None:
        """The approver has been given these."""
        at = now_iso()
        with self._lock:
            self._save([{**r, "told": at} if r["id"] in ids and not r["told"] else r for r in self._requests])

    def untell(self) -> None:
        """A new approver is given every request still waiting."""
        with self._lock:
            self._save([{**r, "told": None} for r in self._requests])

    def _find(self, ident) -> dict:
        hit = next((r for r in self._requests if r["id"] == ident), None)
        if hit is None:
            raise TokenError(404, f"no request {ident!r}")
        return hit

    def decide(self, ident: str, approve: bool, by: str, reason) -> dict:
        """by: who decided, "owner" or the approver's name. reason: said to the asker, optional."""
        if reason is not None and not (isinstance(reason, str) and len(reason) <= REASON_MAX):
            raise TokenError(400, f"'reason' must be text, at most {REASON_MAX} characters")
        with self._lock:
            self._expire()
            hit = self._find(ident)
            if hit["state"] != "pending":
                raise TokenError(409, f"request {ident} is {hit['state']}, not waiting on a decision")
            if approve and (held := self.tokens.has_live(hit["kind"], hit["name"])):
                raise TokenError(409, f"{held!r} has a token already; deny this request")
            done = {**hit, "state": "approved" if approve else "denied", "decided": now_iso(), "by": by,
                    "reason": reason}
            self._save([done if r is hit else r for r in self._requests])
        return self.public(done)

    def collect(self, ident: str, secret) -> tuple[dict, str | None]:
        """The asker, with its secret: (its request, the token if it was approved). The token is made now, once."""
        if not isinstance(secret, str):
            raise TokenError(400, "send {secret}, the secret given when the request was made")
        with self._lock:
            self._expire()
            hit = next((r for r in self._requests if r["id"] == ident), None)
            # Every path compares one digest, in constant time.
            if not same(digest(secret), hit["sha256"] if hit else digest("no such request")) or hit is None:
                raise TokenError(404, f"no request {ident!r} with that secret; ask again")
            if hit["state"] != "approved":
                return self.public(hit), None
            try:
                token, record, _ = self.tokens.issue(hit["kind"], hit["name"], hit["line"], first=True)
            except TokenError as err:
                failed = {**hit, "state": "failed", "reason": str(err)}
                self._save([failed if r is hit else r for r in self._requests])
                return self.public(failed), None
            done = {**hit, "state": "collected", "token": record["id"]}
            self._save([done if r is hit else r for r in self._requests])
        return self.public(done), token
