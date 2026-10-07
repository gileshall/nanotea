"""A SQLite index of every message, so pages, polls and listeners never scan the message files.

data/nanotea.db is rebuilt from the files at each start. After that, whatever writes a message's files re-reads
that message into its row (sync_msg, sync_owner), so a row always says what its files say. The files stay the
record; the index holds nothing they don't.

A thread is named by its first message's row, "m-<id>" for an agent's or "g-<id>" for the owner's; every reply
in it carries that name in its thread column (reactions, taps and events are not replies).

Rows sort by (t, kind, id): t the message's time in epoch seconds, kind 0 for an agent's message and 1 for
something the owner sent, so the two interleave as they always have. A cursor is that triple as text.
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

ID = r"\d{8}-\d{6}-[0-9a-f]{4}"
FLAGS = ("seen", "heard", "escalated", "answer_delivered", "set_aside", "aside_delivered")
CURSOR = re.compile(rf"(\d+(?:\.\d+)?)~([01])~({ID})")
SCHEMA = """
CREATE TABLE msg (
  id TEXT PRIMARY KEY, t REAL NOT NULL, sender TEXT NOT NULL, channel TEXT, ask INTEGER NOT NULL,
  status TEXT NOT NULL, delivered TEXT, seen TEXT, heard TEXT, escalated TEXT,
  answer_delivered TEXT, reply TEXT, reply_status TEXT, thread TEXT, meta TEXT NOT NULL, sig TEXT NOT NULL,
  set_aside TEXT, aside_delivered TEXT);
CREATE INDEX msg_t ON msg(t, id);
CREATE INDEX msg_thread ON msg(thread, t);
CREATE INDEX msg_sender ON msg(sender, channel, t);
CREATE INDEX msg_channel ON msg(channel, t);
CREATE TABLE owner (
  folder TEXT NOT NULL, id TEXT NOT NULL, t REAL NOT NULL, delivered_to TEXT, delivered_at TEXT,
  transcript_status TEXT NOT NULL, reaction INTEGER NOT NULL, thread TEXT, meta TEXT NOT NULL, sig TEXT NOT NULL,
  PRIMARY KEY (folder, id));
CREATE INDEX owner_t ON owner(folder, t, id);
CREATE INDEX owner_thread ON owner(thread, t);
"""


def thread_key(re_: dict) -> str:
    """The thread a reply is in. Replies from before threads existed answered an agent's message, its first."""
    return re_.get("thread") or f"m-{re_['id']}"


def thread_of_owner(m: dict) -> str | None:
    """The thread something the owner sent is a reply in, if it is one."""
    if m.get("re") and not (m.get("reaction") or m.get("tap") or m.get("event")):
        return thread_key(m["re"])
    return None


def cursor(t: float, kind: int, msg_id: str) -> str:
    return f"{t!r}~{kind}~{msg_id}"


def parse_cursor(text: str) -> tuple[float, int, str]:
    m = CURSOR.fullmatch(text)
    if not m:
        raise ValueError(f"bad cursor: {text!r}")
    return float(m[1]), int(m[2]), m[3]


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _sig(*parts: str | None) -> str:
    return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:16]


class Index:
    def __init__(self, data: Path):
        self.data = data
        self.path = data / "nanotea.db"
        # No journal on disk: a crash leaves nothing to replay, and the next start rebuilds anyway.
        new = data / "nanotea.db.new"
        new.unlink(missing_ok=True)
        db = self._connect(new)
        db.executescript(SCHEMA)
        for d in data.iterdir():
            if re.fullmatch(ID, d.name):
                db.execute(*self._msg_row(d))
        for folder in sorted(data.iterdir()):
            if re.fullmatch(r"inbox|inbox-.+|channel-.+", folder.name) and (folder / "messages").is_dir():
                for d in (folder / "messages").iterdir():
                    if re.fullmatch(ID, d.name):
                        db.execute(*self._owner_row(folder.name, d))
        db.commit()
        db.close()
        os.replace(new, self.path)
        self.db = self._connect(self.path)
        self.lock = threading.RLock()

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA journal_mode=MEMORY")
        db.execute("PRAGMA synchronous=OFF")
        return db

    def _msg_row(self, d: Path) -> tuple[str, tuple]:
        meta_text = (d / "meta.json").read_text()
        meta = json.loads(meta_text)  # messages from before a field existed go without it; Message has defaults
        flags = {f: _read(d / f) for f in FLAGS}
        reply_text = _read(d / "reply.json")
        reactions = _read(d / "reactions.json")
        control = _read(d / "control.json")
        reply = json.loads(reply_text) if reply_text is not None else None
        # What the page shows of it; seen only changes its "new" tag, which a view sets itself.
        sig = _sig(meta_text, reply_text, reactions, flags["heard"], control, flags["set_aside"])
        return ("INSERT OR REPLACE INTO msg VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (meta["id"], datetime.fromisoformat(meta["created"]).timestamp(), meta["sender"], meta.get("channel"),
                 int(meta.get("ask", False)), meta["status"], meta.get("delivered"), flags["seen"], flags["heard"],
                 flags["escalated"], flags["answer_delivered"], reply_text, reply and reply["transcript_status"],
                 thread_key(meta["re"]) if meta.get("re") else None, meta_text, sig, flags["set_aside"],
                 flags["aside_delivered"]))

    @staticmethod
    def _owner_row(folder: str, d: Path) -> tuple[str, tuple]:
        meta_text = (d / "meta.json").read_text()
        m = json.loads(meta_text)
        return ("INSERT OR REPLACE INTO owner VALUES (?,?,?,?,?,?,?,?,?,?)",
                (folder, m["id"], datetime.fromisoformat(m["at"]).timestamp(), m.get("delivered_to"),
                 m.get("delivered_at"), m["transcript_status"], int(bool(m.get("reaction"))), thread_of_owner(m),
                 meta_text, _sig(meta_text)))

    def sync_msg(self, d: Path) -> None:
        """Re-read an agent's message from its files, d: data/<id>/."""
        with self.lock:
            self.db.execute(*self._msg_row(d))

    def drop_msg(self, msg_id: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM msg WHERE id = ?", (msg_id,))

    def sync_owner(self, folder: str, d: Path) -> None:
        """Re-read something the owner sent, d: data/<folder>/messages/<id>/."""
        with self.lock:
            self.db.execute(*self._owner_row(folder, d))

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    # Threads: an agent filter on msg and an owner filter on owner, each a SQL condition with its parameters
    # (None: that side has nothing).

    def thread(self, agents: tuple[str, tuple] | None, owner: tuple[str, tuple] | None,
               before: str | None = None, after: str | None = None, limit: int | None = None,
               newest_first: bool = False) -> tuple[list[tuple[str, dict, str, str]], bool]:
        """Items as (kind, meta, sig, cursor), oldest first (newest first with newest_first). before: only items
        older than that cursor. after: only items at or newer than it. limit: the newest that many. Returns them
        and whether older ones remain. The owner's items say which folder they are in, as "folder"."""
        sides = []
        if agents is not None:
            sides.append(("msg", 0, agents))
        if owner is not None:
            sides.append(("owner", 1, owner))
        out = []
        for table, kind, (where, params) in sides:
            folder = "folder" if table == "owner" else "NULL"
            sql, args = f"SELECT t, id, meta, sig, {folder} FROM {table} WHERE ({where})", list(params)
            if before is not None:
                sql += " AND (t, ?, id) < (?, ?, ?)"
                args += [kind, *parse_cursor(before)]
            if after is not None:
                sql += " AND (t, ?, id) >= (?, ?, ?)"
                args += [kind, *parse_cursor(after)]
            sql += " ORDER BY t DESC, id DESC"
            if limit is not None:
                sql += " LIMIT ?"
                args.append(limit + 1)
            out += [((t, kind, i), meta, sig, f) for t, i, meta, sig, f in self.rows(sql, tuple(args))]
        out.sort(key=lambda r: r[0], reverse=True)
        more = limit is not None and len(out) > limit
        if limit is not None:
            out = out[:limit]
        items = [("agent", json.loads(meta), sig, cursor(*key)) if key[1] == 0
                 else ("owner", {**json.loads(meta), "folder": f}, sig, cursor(*key)) for key, meta, sig, f in out]
        return (items if newest_first else items[::-1]), more

    def threads(self, keys: list[str]) -> dict[str, dict]:
        """For each of these threads with replies: {n, last (epoch seconds), agents (who replied, first reply
        first), owner (whether the owner replied), ids (the agents' replies, oldest first)}."""
        out: dict[str, dict] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            found: dict[str, list] = {}
            for key, t, sender, msg_id in self.rows(f"SELECT thread, t, sender, id FROM msg WHERE thread IN "
                                                    f"({marks})", tuple(chunk)):
                found.setdefault(key, []).append((t, 0, msg_id, sender))
            for key, t, msg_id in self.rows(f"SELECT thread, t, id FROM owner WHERE thread IN ({marks})",
                                            tuple(chunk)):
                found.setdefault(key, []).append((t, 1, msg_id, None))
            for key, replies in found.items():
                replies.sort()  # the order pages show: time, then an agent's before the owner's, then id
                agents = []
                for _, kind, _, sender in replies:
                    if kind == 0 and sender not in agents:
                        agents.append(sender)
                out[key] = {"n": len(replies), "last": replies[-1][0], "agents": agents,
                            "owner": any(r[1] == 1 for r in replies),
                            "ids": [r[2] for r in replies if r[1] == 0]}
        return out

    def owner_folders(self, msg_id: str) -> list[str]:
        """The folders holding something the owner sent with this id: one, unless ids ever collide."""
        return [f for (f,) in self.rows("SELECT folder FROM owner WHERE id = ?", (msg_id,))]

    def position(self, row: str, folder: str | None) -> str | None:
        """The cursor of a page row ("m-<id>" for an agent's message, "g-<id>" for the owner's in folder), or None
        once it's gone."""
        kind, _, msg_id = row.partition("-")
        if kind not in ("m", "g") or not re.fullmatch(ID, msg_id):
            raise ValueError(f"bad row: {row!r}")
        if kind == "m":
            got = self.rows("SELECT t FROM msg WHERE id = ?", (msg_id,))
        else:
            got = self.rows("SELECT t FROM owner WHERE folder = ? AND id = ?", (folder, msg_id))
        return cursor(got[0][0], 0 if kind == "m" else 1, msg_id) if got else None
