"""Tokens: what an agent, or a program reporting events, presents to the agents' API. data/tokens.json
(mode 0600), tokens-log.jsonl keeps every issue and revocation.

A token is nt_<id>_<secret>: id is 8 hex digits, secret 32 random bytes in base64url. Only the SHA-256 of the whole
token is kept, so a copy of data/ doesn't hold working tokens; the secret is high entropy, so a fast hash is
enough. A token is bound to a name for good. kind "agent": the agent of that name, on any line and channel it may
use; with a line, on that line alone, and no other agent's token may claim it. kind "events": a program named as that
source, which can post events to one line and do nothing else.

One agent's token may be the approver's: that agent approves the requests of agents and event sources that have no
token yet (nanotea/enroll.py). The owner chooses it; it moves to a token that replaces its own and ends with a
revocation.

tokens.json is {"version": 1, "tokens": [{id, kind, name, line, created, sha256, revoked}], "approver": id | null}.
A revoked record is kept, so a client holding its token is told it was revoked rather than that it is unknown."""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path

from nanotea.config import ConfigError
from nanotea.store import now_iso

TOKEN = re.compile(r"nt_([0-9a-f]{8})_([A-Za-z0-9_-]{43})")
KINDS = ("agent", "events")
NAME_MAX = 80
LINE = re.compile(r"main|[a-z0-9][a-z0-9-]{0,39}")
# What a lookup that finds no record compares against, so that it costs what one that does costs.
_NONE = hashlib.sha256(b"no such token").hexdigest()


class TokenError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    """Secrets are compared in constant time."""
    return hmac.compare_digest(a.encode(), b.encode())


def check_name(name, kind: str) -> str:
    what = "an agent's name" if kind == "agent" else "a source name"
    if not (isinstance(name, str) and name.strip() and name == name.strip() and len(name) <= NAME_MAX
            and name.isprintable()):
        raise TokenError(400, f"'name' must be {what}: printable, no space at either end, at most {NAME_MAX} "
                              f"characters")
    if name.casefold() == "owner":
        raise TokenError(400, "'owner' is how agents are told a message is the owner's own words; pick another name")
    return name


class TokenBook:
    def __init__(self, root: Path):
        self.path = root / "tokens.json"
        self.log_path = root / "tokens-log.jsonl"
        self._lock = threading.Lock()
        self.used: dict[str, str] = {}  # id -> when it last authenticated; not kept across restarts
        self.fresh = not self.path.exists()
        self._records: list[dict] = []
        self._approver: str | None = None
        if self.fresh:
            self._save([], {"created": True})
        else:
            self._records, self._approver = self._read()  # a bad file stops the service now

    def _read(self) -> tuple[list[dict], str | None]:
        try:
            data = json.loads(self.path.read_text())
            if data["version"] != 1 or not isinstance(data["tokens"], list):
                raise ValueError("not a version 1 token file")
            # Files written before there was an approver have no key for it.
            approver = data["approver"] if "approver" in data else None
            if approver is not None and not any(r["id"] == approver and r["kind"] == "agent" and not r["revoked"]
                                                for r in data["tokens"]):
                raise ValueError(f"the approver {approver!r} is not a live agent's token")
            return data["tokens"], approver
        except (ValueError, KeyError, TypeError) as err:
            raise ConfigError(f"{self.path} is unreadable ({type(err).__name__}: {err}); restore it from a backup, "
                              f"or move it aside and approve every agent again") from err

    def _save(self, records: list[dict], change: dict, approver: str | None = ...) -> None:
        """approver: the approver's token id after this change; left out, it stays."""
        approver = self._approver if approver is ... else approver
        with self.log_path.open("a") as log:
            log.write(json.dumps({**change, "logged": now_iso()}) + "\n")
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"version": 1, "tokens": records, "approver": approver}, indent=2))
        os.replace(tmp, self.path)
        self._records = records
        self._approver = approver

    def issue(self, kind: str, name, line: str | None = None, replace: bool = False,
              first: bool = False) -> tuple[str, dict, list[str]]:
        """A new token for name: (the token, shown once; its record; the ids it replaced). replace: revoke the
        name's other tokens of this kind as the new one is made. first: only if the name has no live token of this
        kind, so a name already in use can't be taken by asking again."""
        if kind not in KINDS:
            raise TokenError(400, f"'kind' must be one of {', '.join(KINDS)}")
        check_name(name, kind)
        if kind == "events" and line is None:
            raise TokenError(400, "an events token needs 'line', the line it reports to: main, or a direct line's "
                                  "name (lowercase letters, digits, dashes)")
        if line is not None and not (isinstance(line, str) and LINE.fullmatch(line)):
            raise TokenError(400, "'line' must be main, or a direct line's name (lowercase letters, digits, dashes)")
        with self._lock:
            records = self._records
            live = [r for r in records if r["kind"] == kind and not r["revoked"]]
            clash = next((r["name"] for r in live if r["name"].casefold() == name.casefold() and r["name"] != name),
                         None)
            if clash:
                raise TokenError(409, f"{clash!r} has a token, and agent names are the same in any case; use "
                                      f"{clash!r}, or revoke its tokens first")
            if first and any(r["name"] == name for r in live):
                raise TokenError(409, f"{name!r} already has a token, so it can't be given another. Use the file it "
                                      f"was written to; if that is lost, the owner revokes the token in the app, "
                                      f"under Tokens, and {name!r} asks again")
            if kind == "agent" and line is not None and (other := self._bound(line)) not in (None, name):
                raise TokenError(409, f"line {line!r} is bound to {other!r}'s token; revoke that one first")
            token = f"nt_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}"
            record = {"id": token.split("_")[1], "kind": kind, "name": name, "line": line, "created": now_iso(),
                      "sha256": digest(token), "revoked": None}
            gone = [r["id"] for r in live if r["name"] == name] if replace else []
            at = now_iso()
            kept = [{**r, "revoked": at} if r["id"] in gone else r for r in records]
            approver = record["id"] if self._approver in gone else self._approver
            self._save(kept + [record], {"issued": {k: v for k, v in record.items() if k != "sha256"},
                                         "revoked": gone, "approver": approver}, approver)
        return token, self.public(record), gone

    def _bound(self, line: str) -> str | None:
        """Called holding _lock. The agent whose live token is bound to line, if any."""
        return next((r["name"] for r in self._records
                     if r["kind"] == "agent" and r["line"] == line and not r["revoked"]), None)

    def bound(self, line: str) -> str | None:
        """The agent whose live token is bound to line, if any."""
        with self._lock:
            return self._bound(line)

    def revoke(self, ident: str) -> dict:
        with self._lock:
            records = self._records
            hit = next((r for r in records if r["id"] == ident), None)
            if hit is None:
                raise TokenError(404, f"no token {ident!r}; nanotea token list shows them")
            if hit["revoked"]:
                raise TokenError(409, f"token {ident} is already revoked")
            at = now_iso()
            approver = None if self._approver == ident else self._approver
            self._save([{**r, "revoked": at} if r is hit else r for r in records],
                       {"revoked": [ident], "of": hit["name"], "approver": approver}, approver)
        return self.public({**hit, "revoked": at})

    def appoint(self, ident: str | None) -> dict | None:
        """The owner makes the agent's token ident the approver, in place of any other; None: no approver."""
        with self._lock:
            if ident is not None:
                hit = next((r for r in self._records if r["id"] == ident), None)
                if hit is None:
                    raise TokenError(404, f"no token {ident!r}")
                if hit["revoked"] or hit["kind"] != "agent":
                    raise TokenError(409, f"token {ident} is {'revoked' if hit['revoked'] else 'an events token'}; "
                                          f"only a live agent's token can approve others")
            self._save(self._records, {"approver": ident}, ident)
            return self._public_approver()

    def _public_approver(self) -> dict | None:
        hit = next((r for r in self._records if r["id"] == self._approver), None)
        return self.public(hit) if hit else None

    def approver(self) -> dict | None:
        """The approver's token, if there is one."""
        with self._lock:
            return self._public_approver()

    def revoked_at(self, kind: str, name: str) -> str | None:
        """When name's last token of kind was revoked, if it had one and holds none now."""
        with self._lock:
            mine = [r for r in self._records if r["kind"] == kind and r["name"].casefold() == name.casefold()]
            return None if any(not r["revoked"] for r in mine) else max((r["revoked"] for r in mine), default=None)

    def has_live(self, kind: str, name: str) -> str | None:
        """The name, as issued, of a live token of kind for name in any case, if there is one."""
        with self._lock:
            return next((r["name"] for r in self._records if r["kind"] == kind and not r["revoked"]
                         and r["name"].casefold() == name.casefold()), None)

    def check(self, token: str) -> dict:
        """The record a presented token belongs to. TokenError 401 for one that is malformed, unknown, or revoked."""
        m = TOKEN.fullmatch(token)
        with self._lock:
            records = {r["id"]: r for r in self._records}
        record = records.get(m[1]) if m else None
        # Every path compares one digest, in constant time.
        match = same(digest(token), record["sha256"] if record else _NONE)
        if not (m and record and match):
            raise TokenError(401, "unknown token. nanotea mcp asks for approval of a new one when it starts")
        if record["revoked"]:
            what = "events token for" if record["kind"] == "events" else "token for"
            raise TokenError(401, f"this {what} {record['name']!r} was revoked ({record['revoked']}). nanotea mcp "
                                  f"asks for approval of a new one when it starts")
        self.used[record["id"]] = now_iso()
        return record

    def public(self, record: dict) -> dict:
        """A record as the owner sees it: everything but the hash, with when it was last used."""
        return {**{k: v for k, v in record.items() if k != "sha256"}, "last_used": self.used.get(record["id"])}

    def all(self, revoked: bool = False) -> list[dict]:
        with self._lock:
            return [self.public(r) for r in self._records if revoked or not r["revoked"]]
