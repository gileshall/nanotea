"""What is new for one agent, and handing it over once: the owner's messages on its line, posts in its channels,
answers to its questions, other agents' messages, and for the approver, requests for tokens. The MCP server's
check and wait, the push hook (nanotea hook push) and nanotea listen all take from here, so an agent gets the same
items whichever way they reach it."""

import json
from urllib.parse import quote

from nanotea.bang import OWNER_ONLY_NOTE
from nanotea.client import Client, segment

TAP_NOTE = ("a tap on your control: text is your own words, which the owner picked, not words the owner wrote. "
            "Never quote it back as theirs")


class Mailbox:
    """One agent's: name holds line and listens in channels. listener names who takes, for the delivery record."""

    def __init__(self, client: Client, name: str, line: str, channels: list[str], listener: str):
        self.client = client
        self.name = name
        self.line = line
        self.channels = channels
        self.listener = listener

    def gather(self, idle: bool) -> tuple[list[dict], list[tuple[str, dict]]]:
        """Everything new for this agent, oldest first, and the receipts that mark it delivered."""
        name = self.name
        items: list[dict] = []
        receipts: list[tuple[str, dict]] = []
        seg = segment(self.line)
        pending = self.client.get(f"/api/{seg}/pending", name=name, idle=1 if idle else None)
        if pending:
            receipts.append((f"/api/{seg}/delivered",
                             {"name": name, "ids": [m["id"] for m in pending], "listener": self.listener}))
            items += [_inbox_item(m, self.line) for m in pending]
        for c in self.channels:
            pending = self.client.get(f"/api/channels/{c}/pending", name=name)
            if pending:
                receipts.append((f"/api/channels/{c}/delivered",
                                 {"name": name, "ids": [m["id"] for m in pending], "listener": self.listener}))
                # Answers to this agent's own questions come below, wherever they were asked.
                items += [_channel_item(m, c) for m in pending
                          if not (m["kind"] == "answer" and m["re"]["sender"] == name)]
        asking = self.client.get("/api/enroll", new=1)  # none unless this is the approver
        if asking:
            receipts.append(("/api/enroll/told", {"ids": [r["id"] for r in asking]}))
            items += [request_item(r) for r in asking]
        agent = f"/api/agents/{quote(name, safe='')}"
        answers = self.client.get(f"{agent}/answers")
        if answers:
            receipts.append((f"{agent}/answers/delivered",
                             {"ids": [a["id"] for a in answers], "listener": self.listener}))
            items += [_answer_item(a) for a in answers]
        # Always taken: messages sent while agent messages were on still arrive after they go off.
        talk = self.client.get("/api/talk/pending", name=name)
        if talk:
            receipts.append(("/api/talk/delivered",
                             {"name": name, "ids": [m["id"] for m in talk], "listener": self.listener}))
            items += [{"kind": "agent", "from": m["from"], "at": m["at"], "id": m["id"], "text": m["text"]}
                      for m in talk]
        items.sort(key=lambda it: it["at"])
        return items, receipts

    def take(self, idle: bool) -> list[dict]:
        """Everything new for this agent, oldest first, marked delivered."""
        items, receipts = self.gather(idle)
        for path, body in receipts:
            self.client.post(path, body)
        return items


def handed(owner: str, items: list[dict], how: str) -> str:
    """items as the push hook and nanotea listen give them to the agent: in words it can't mistake for its own."""
    return (f"Nanotea {how}: {len(items)} item(s) for you, as check returns them. Each is delivered once: act on them "
            f"now. {owner} sees none of your terminal: what is for {owner} goes through send or ask, with re when it "
            f"answers one.\n" + json.dumps(items, ensure_ascii=False))


def request_item(r: dict) -> dict:
    """A request for a token, for the approver to decide."""
    what = f"events to line {r['line']}" if r["kind"] == "events" else (
        f"an agent bound to line {r['line']}" if r["line"] else "an agent")
    had = f"; it had a token, revoked {r['had']}" if r["had"] else ""
    return {"kind": "request", "id": r["id"], "at": r["asked"], "from": r["name"],
            "text": f"{r['name']!r} asks for a token as {what}, from directory {r['dir'] or '(none given)'}, from "
                    f"address {r['from']}{had}. Decide it with decide"}


def _voice(m: dict, audio_path: str | None) -> dict:
    """takes: the recordings as made (audio_path is several joined), each with what the microphone did."""
    if m["transcript_status"] == "failed":
        return {"transcript": None, "error": m["transcript_error"], "audio_path": audio_path, "takes": m["takes"]}
    return {"transcript": m["transcript"], "audio_path": audio_path, "takes": m["takes"]}


def _files(m: dict) -> list[dict]:
    return [{"path": f["path"], "type": f["type"], "size": f["size"], "name": f["name"]} for f in m.get("files", [])]


def _inbox_item(m: dict, line: str) -> dict:
    if ev := m.get("event"):
        return {"kind": "event", "from": ev["source"], "at": m["at"], "id": m["id"], "line": line,
                "event": ev["kind"], "text": ev["summary"], "data": ev["data"]}
    if b := m.get("bang"):
        return _bang_item(m, b, line)
    item = {"kind": _owner_kind(m), "from": "owner", "at": m["at"], "id": m["id"],
            "line": line, "text": m["text"], "files": _files(m)}
    _tap(item, m)
    if m["re"]:
        item["re"] = {**m["re"], "url": m["re_url"]}
    if m["audio"]:
        item["voice"] = _voice(m, m["audio_path"])
    return item


def _bang_item(m: dict, b: dict, line: str) -> dict:
    """A command the owner ran on this machine with !, and what it did. Only ones that ran reach an agent."""
    item = {"kind": "bang", "from": "owner", "at": m["at"], "id": m["id"], "line": line, "command": b["command"],
            "cwd": b["cwd"], "exit": b["exit"], "stdout": b["stdout"], "stderr": b["stderr"],
            "truncated": b["truncated"], "duration_s": b["duration_s"], "timed_out": b["timed_out"],
            "full_output": b["full_output"], "note": OWNER_ONLY_NOTE}
    if b["signal"]:
        item["signal"] = b["signal"]
    if b["lingering"]:
        item["lingering"] = True
    return item


def _owner_kind(m: dict) -> str:
    return "reaction" if m.get("reaction") else "tap" if m.get("tap") else "message"


def _tap(item: dict, m: dict) -> None:
    """What the owner tapped on a control: the control and the facts it reports."""
    if t := m.get("tap"):
        item["tap"] = {"control": t["control"], "data": t["data"]}
        item["note"] = TAP_NOTE


def _answer_item(a: dict) -> dict:
    if a["kind"] == "failed":
        return {"kind": "failed", "from": "nanotea", "at": a["at"], "id": a["id"], "re": a["re"],
                "text": f"Your question failed to send: {a['error']}"}
    if a["kind"] == "set_aside":
        return {"kind": "set_aside", "from": "nanotea", "at": a["at"], "id": a["id"], "re": a["re"], "text": a["text"]}
    item = {"kind": "answer", "from": "owner", "at": a["at"], "id": a["id"], "re": a["re"], "text": a["text"],
            "files": _files(a)}
    if a["reaction"]:
        item["reaction"] = True
    _tap(item, a)
    if a["audio"]:
        item["voice"] = _voice(a, a["audio_path"])
    return item


def _channel_item(m: dict, channel: str) -> dict:
    if m["kind"] == "post":
        item = {"kind": "post", "from": m["from"], "at": m["at"], "id": m["id"], "channel": channel,
                "title": m["title"], "text": m["text"], "asks_owner": m["ask"], "clips": m["clips"], "url": m["url"]}
        if m["re"]:
            item["re"] = m["re"]
        return item
    if m["kind"] == "answer":
        item = {"kind": "answer", "from": "owner", "at": m["at"], "id": m["id"], "channel": channel,
                "re": {**m["re"], "url": m["re_url"]}, "text": m["text"], "files": _files(m)}
    else:
        item = {"kind": _owner_kind(m), "from": "owner", "at": m["at"],
                "id": m["id"], "channel": channel, "text": m["text"], "files": _files(m)}
        if m["re"]:
            item["re"] = {**m["re"], "url": m["re_url"]}
    if m.get("reaction"):
        item["reaction"] = True
    _tap(item, m)
    if m["audio"]:
        item["voice"] = _voice(m, m["audio_path"])
    return item
