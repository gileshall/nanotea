"""MCP server for agents: one per agent session, over stdio, talking to the local nanotea service.

The owner's settings, read from the service when the session starts, decide which tools it offers and what its
instructions say. While joined, it tells the service every HEARTBEAT_S that the session is open.

Without a token, it asks the service for one (nanotea/enroll.py) under its --name or the name it joins with, and
answers tool calls with what it waits on until the approver or the owner approves; it then collects the token and
goes on. Until then it offers the tools of the default settings.

Run: nanotea mcp [--name NAME [--line LINE] [--voice ID] [--channel NAME ...]] [--idle finish|wait|hook] [--wait-s N]
     [--bang]
"""

import argparse
import asyncio
import base64
import copy
import functools
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from nanotea.bang import OWNER_ONLY_NOTE, Host
from nanotea.client import Client, NanoteaError, line_for, segment
from nanotea.config import load_config
from nanotea.credentials import agent_token, ask, collect, token_file, waiting_on
from nanotea.settings import DEFAULTS, EVENT_SETTLE_S
from nanotea.sop import IDLE_MODES, render

POLL_S = 2
PROGRESS_S = 10
WAIT_DEFAULT_S = 50
WAIT_MAX_S = 3600
HEARTBEAT_S = 20
URGENT = {"message", "reaction", "tap", "answer", "failed", "set_aside", "agent", "request"}  # end a wait at once


class Waiting(ToolError):
    """This session has asked for a token, and the request waits on a decision."""


class Agent:
    """This session's identity, what it is waiting on, and its heartbeat to the service. bang: this process was started
    with the local opt-in to run the owner's bang commands, which then run here (nanotea/bang.py). cfg and preset:
    for asking for a token, and joining as --name once it comes."""

    def __init__(self, client: Client, owner: str, settings: dict, bang: bool = False, cfg: dict | None = None,
                 preset: argparse.Namespace | None = None):
        self.client = client
        self.owner = owner
        self.settings = settings
        self.bang = bang
        self.cfg = cfg
        self.preset = preset
        self.request: dict | None = None  # the request for a token this session waits on
        self.stale: str | None = None  # a token the service refused, which a new one replaces
        self._token_lock = threading.Lock()
        self.host: Host | None = None
        self.name: str | None = None
        self.line: str | None = None
        self.voice: str | None = None
        self.channels: list[str] = []
        self.rules_v: str | None = None  # the version of the rules this session was last given
        self.session = secrets.token_hex(8)
        self.listener = f"mcp pid {os.getpid()}"
        self._beat_stop = threading.Event()
        self._beat: threading.Thread | None = None
        self._lock = threading.Lock()  # join and the heartbeat's re-register

    def need(self) -> str:
        if self.client.credential is None:
            self.ready()
        if self.name is None:
            raise ToolError("call join first")
        return self.name

    def ask_token(self, name: str) -> None:
        self.request = ask(self.cfg, "agent", name, None, self.stale)

    def ready(self) -> None:
        """Called before the service is reached without a token: collects it if approved; else raises Waiting while
        the request waits, or ToolError if it was denied."""
        with self._token_lock:
            if self.client.credential is not None:
                return
            if self.request is None:
                raise ToolError("this session has no token yet: call join with your name, which asks for one")
            req = self.request
            token, state = collect(self.cfg, req, self.stale)
            if token is None:
                if state["state"] == "pending":
                    req["approver"] = state["approver"]
                    raise Waiting(f"waiting for approval: request {req['id']} for a token for {req['name']!r} is "
                                  f"with {waiting_on(state)}. Call wait to wait for it")
                if state["state"] == "denied":
                    self.request = None
                    raise ToolError(f"request {req['id']} for a token for {req['name']!r} was denied by "
                                    f"{state['by']}" + (f": {state['reason']}" if state["reason"] else "")
                                    + f". Ask {self.owner} what to do")
                # Expired, replaced, or gone: nothing was decided against it.
                self.request = ask(self.cfg, "agent", req["name"], None, self.stale)
                if self.request["id"] is not None:
                    raise Waiting(f"request {req['id']} for {req['name']!r} was {state['state']}; asked again: "
                                  f"request {self.request['id']}, with {waiting_on(self.request)}. Call wait to wait "
                                  f"for it")
                token, _ = collect(self.cfg, self.request, self.stale)
            self.client.credential = token
            self.request = self.stale = None
            fresh = self.client.get("/api/settings")["settings"]
            fresh["bang"] = fresh["bang"] and self.bang
            self.settings.clear()
            self.settings.update(fresh)
        p = self.preset
        if p is not None and p.name and self.name is None:
            self.join(p.name, p.line, p.voice, None, p.channel)

    @property
    def talk(self) -> bool:
        return self.settings["agent_messages"] != "off"

    def _register(self, name: str, line: str) -> None:
        self.client.post("/api/sessions", {"name": name, "session": self.session, "pid": os.getpid(), "line": line,
                                           "bang": self.bang})

    def _reregister(self) -> None:
        with self._lock:
            self._register(self.name, self.line)

    def join(self, name: str, line: str | None, voice: str | None, about: str | None,
             channels: list[str]) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("name must not be empty")
        line = line or line_for(name)
        if self.client.credential is None and (self.request is None or self.request["name"] != name):
            self.ask_token(name)
        voices = self.client.get("/api/voices")
        if voice is not None:
            match = [v for v in voices if v["id"] == voice]
            if not match:
                raise ValueError(f"unknown voice {voice!r}; call voices for the list")
            if match[0]["taken_by"] not in (None, name):
                raise ValueError(f"voice {voice!r} belongs to {match[0]['taken_by']!r}; pick a free one")
        known = {c["name"] for c in self.client.get("/api/channels")}
        for c in channels:
            if c not in known:
                raise ValueError(f"no channel #{c}; call channels for the list")
        with self._lock:
            # The session first: with one session per agent on, a second one stops here, before taking the line.
            if (self.name, self.line) != (name, line):
                if self.name is not None and self.name != name:
                    self.client.post(f"/api/sessions/{self.session}/close", {})
                self._register(name, line)
            if self.line not in (None, line):
                self.client.post(f"/api/{segment(self.line)}", {"name": self.name, "open": False})
            info = self.client.post(f"/api/{segment(line)}", {"name": name, "about": about, "open": True})
            self.name, self.line, self.channels = name, line, list(channels)
        for c in channels:
            # Starts this agent's cursor there, so what is posted after the join reaches it, not only after its
            # first check.
            self.client.get(f"/api/channels/{c}/pending", name=name)
        self.voice = voice or next((v["id"] for v in voices if v["taken_by"] == name), None)
        self.start_heartbeat()
        if self.bang:
            if self.host is None:
                self.host = Host(self.client, name, self.session, self._reregister)
                self.host.start()
            self.host.name = name
        out = {"name": name, "line": line, "owner": self.owner, "voice": self.voice, "channels": self.channels,
               "url": info["url"],
               **({} if self.voice else {"note": "no voice yet: call voices, then join again with voice"})}
        if self.settings["rules"]:
            out["rules"] = self.take_rules()
        return out

    def start_heartbeat(self) -> None:
        if self._beat is None:
            self._beat = threading.Thread(target=self._heartbeat, name="nanotea-heartbeat", daemon=True)
            self._beat.start()

    def _heartbeat(self) -> None:
        while not self._beat_stop.wait(HEARTBEAT_S):
            try:
                self.client.post(f"/api/sessions/{self.session}/beat", {})
            except NanoteaError as err:
                if err.status != 404:
                    print(f"nanotea mcp: heartbeat failed: {err}", file=sys.stderr, flush=True)
                    continue
                # The service restarted and forgot the session.
                try:
                    with self._lock:
                        self._register(self.name, self.line)
                    print("nanotea mcp: the service forgot this session; registered it again", file=sys.stderr,
                          flush=True)
                except NanoteaError as again:
                    print(f"nanotea mcp: registering the session again failed: {again}", file=sys.stderr,
                          flush=True)

    def close(self) -> None:
        """The session ends: stop the heartbeat and tell the service."""
        self._beat_stop.set()
        if self.host is not None:
            self.host.stop()
            self.host.finish(5)
        if self.name is not None:
            self.client.post(f"/api/sessions/{self.session}/close", {})

    def take_rules(self) -> list[dict]:
        out = self.client.get("/api/rules", name=self.need())
        self.rules_v = out["version"]
        return [{"id": r["id"], "text": r["text"], "for": r["for"]} for r in out["rules"]]

    def waiting(self) -> dict:
        """The owner's messages on this line, answers to this agent's questions, and agents' messages to it,
        ready but not taken; and the version of its rules."""
        if self.name is None:
            return {"total": 0, "rules_v": None}
        return self.client.get(f"/api/agents/{quote(self.name, safe='')}/waiting", line=segment(self.line))

    def gather(self, idle: bool) -> tuple[list[dict], list[tuple[str, dict]]]:
        """Everything new for this agent, oldest first, and the receipts that mark it delivered."""
        name = self.need()
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
            items += [_request_item(r) for r in asking]
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

    def wakes(self, items: list[dict]) -> bool:
        """Whether what has come ends a wait now: the owner's words and answers, agents' messages, and (with the
        wake filter off) any post; a post that @mentions this agent always does."""
        mention = re.compile(rf"@{re.escape(self.name)}\b", re.I)
        for it in items:
            if it["kind"] in URGENT:
                return True
            if it["kind"] == "post" and (not self.settings["wake_filter"]
                                         or mention.search(f"{it['title'] or ''} {it['text']}")):
                return True
        return False


def _request_item(r: dict) -> dict:
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


RE_DOC = """
re: the id of the message this answers (the owner's to you, yours or another agent's): it goes in that message's
thread, in the thread's channel or line."""

SEND_DOC = """Send the owner a message, or post it in a channel. Write it to be read: clear, short markdown. Nanotea
makes the spoken version when the owner taps play; don't write for speech.
attach: paths of files: audio, images, video (mp4, mov) or PDFs. Place the Nth with [[audio N]], [[image N]],
[[video N]] or [[pdf N]]; audio plays in the voice, the rest shows on the page. Unplaced ones go at the end."""

ASK_DOC = """Ask the owner something. Returns at once with the question's id; the answer arrives later through check
or wait as kind "answer" with re.id the question's id. Keep working on what doesn't depend on it.
raw: ask for the answer recorded with the microphone's processing off (no echo cancelling, noise suppression or
level control), in stereo where the mic has it: for a voice sample or an instrument. The answer's voice.takes
are the recordings as made, each with what the microphone did; check capture.raw, since the owner can turn it
off."""

CONTROL_DOC = """
control: something for the owner to tap instead of typing, e.g. {"type": "choice", "options": ["Ship it",
"Hold"]}; the controls tool lists them. A tap arrives as the answer, or else as kind "tap", with tap.data."""


def build(cfg: dict, preset: argparse.Namespace) -> tuple[MCPServer, Agent]:
    """preset: main's arguments. With a name, the agent is joined before the first tool call, or asks for a token
    if it has none and joins once it is approved."""
    path = token_file(preset.name)
    client = Client(cfg["port"], agent_token(preset.name) if path is not None and path.exists() else None)
    settings, stale = None, None
    if client.credential is not None:
        try:
            settings = client.get("/api/settings")["settings"]
        except NanoteaError as err:
            if err.status != 401:
                raise
            print(f"nanotea mcp: {err}", file=sys.stderr, flush=True)
            stale, client.credential = client.credential, None
    if settings is None:
        settings = copy.deepcopy(DEFAULTS)
    # Without the opt-in this session can't run a command, so its agreement says nothing of them.
    settings["bang"] = settings["bang"] and preset.bang
    agent = Agent(client, cfg["app"]["owner"], settings, preset.bang, cfg, preset)
    agent.stale = stale
    client.before = agent.ready
    if preset.name and client.credential is not None:
        agent.join(preset.name, preset.line, preset.voice, None, preset.channel)
    elif preset.name:
        agent.ask_token(preset.name)
        print(f"nanotea mcp: asked for a token for {preset.name!r} (request {agent.request['id']}); waiting on "
              f"{waiting_on(agent.request)}", file=sys.stderr, flush=True)
    return make(cfg, agent, preset), agent


def make(cfg: dict, agent: Agent, preset: argparse.Namespace) -> MCPServer:
    """The server for agent's settings. Talks to the service only when a tool is called."""
    owner = agent.owner
    settings = agent.settings
    wait_default = preset.wait_s
    mcp = MCPServer(name="nanotea", title=cfg["app"]["name"],
                    instructions=render(owner, settings, agent.name, agent.line, preset.idle))

    def optional(name: str):
        """Registers the tool only if the owner has it on. Settings can change once a token comes."""
        def register(fn):
            if not settings["tools"][name]:
                return fn

            @functools.wraps(fn)
            async def guarded(*args, **kwargs):
                if not settings["tools"][name]:
                    raise ToolError(f"{owner} has the {name} tool off")
                return await fn(*args, **kwargs)
            return mcp.tool()(guarded)
        return register

    async def call(fn, *args, **kwargs):
        """fn in a thread; what an agent can act on (a refusal, a bad argument, an unreadable file) reaches it
        as the tool's error."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except (NanoteaError, ValueError, OSError) as err:
            raise ToolError(str(err)) from err

    async def result(**out) -> dict[str, Any]:
        w = await call(agent.waiting)
        out["waiting"] = w["total"] + w.get("bang", 0)
        if settings["rules"] and agent.name is not None and w["rules_v"] != agent.rules_v and "rules" not in out:
            out["rules"] = await call(agent.take_rules)
            out["rules_changed"] = True
        return out

    @mcp.tool()
    async def join(name: str, voice: str | None = None, about: str | None = None, line: str | None = None,
                   channels: list[str] | None = None) -> dict[str, Any]:
        """Say who you are for this session and open your direct line to the owner. name: your role, the same
        every session (e.g. "builder"). voice: a voice id from voices, needed once before your first send.
        about: what you are working on, shown to the owner. line: the line to hold, by default your name;
        "main" is the shared main inbox. channels: channels to receive posts from (see channels)."""
        out = await call(agent.join, name, line, voice, about, channels or [])
        return await result(**out)

    if settings["tools"]["controls"]:
        async def send(text: str, title: str | None = None, channel: str | None = None, re: str | None = None,
                       attach: list[str] | None = None, control: dict[str, Any] | None = None) -> dict[str, Any]:
            out = await call(_send, agent, text, title, channel, attach or [], False, False, control, re)
            return await result(**out)

        async def ask(question: str, title: str | None = None, channel: str | None = None, re: str | None = None,
                      raw: bool = False, control: dict[str, Any] | None = None) -> dict[str, Any]:
            out = await call(_send, agent, question, title, channel, [], True, raw, control, re)
            return await result(**out)
    else:
        async def send(text: str, title: str | None = None, channel: str | None = None, re: str | None = None,
                       attach: list[str] | None = None) -> dict[str, Any]:
            out = await call(_send, agent, text, title, channel, attach or [], False, re_=re)
            return await result(**out)

        async def ask(question: str, title: str | None = None, channel: str | None = None, re: str | None = None,
                      raw: bool = False) -> dict[str, Any]:
            out = await call(_send, agent, question, title, channel, [], True, raw, re_=re)
            return await result(**out)
    send.__doc__ = SEND_DOC + RE_DOC + (CONTROL_DOC if settings["tools"]["controls"] else "")
    ask.__doc__ = ASK_DOC + RE_DOC + (CONTROL_DOC if settings["tools"]["controls"] else "")
    mcp.tool()(send)
    mcp.tool()(ask)

    @mcp.tool()
    async def check() -> dict[str, Any]:
        """Take everything new for you without waiting: the owner's messages and reactions on your line,
        answers to your questions, and posts in your channels. Each is delivered once."""
        items = await call(agent.take, False)
        return await result(items=items)

    @mcp.tool()
    async def wait(ctx: Context, timeout_s: int | None = None) -> dict[str, Any]:
        """Wait for something new, up to timeout_s seconds (default: this server's setting, at most 3600),
        then return what came, or no items on timeout. Call it in a loop when you have nothing else to do; it
        tells the owner you are idle."""
        timeout_s = wait_default if timeout_s is None else timeout_s
        if not 1 <= timeout_s <= WAIT_MAX_S:
            raise ToolError(f"timeout_s must be 1 to {WAIT_MAX_S}")
        start = time.monotonic()
        if agent.client.credential is None:
            while True:
                try:
                    await call(agent.ready)
                    break
                except Waiting:
                    if time.monotonic() - start >= timeout_s:
                        raise
                    await asyncio.sleep(POLL_S)
            # What the wait was for has come.
            return await result(items=[], approved=True, name=agent.name)
        last_progress = start
        first_event: float | None = None
        while True:
            # Looked at, not taken: what doesn't end the wait stays undelivered until it does.
            items, _ = await call(agent.gather, True)
            now = time.monotonic()
            if first_event is None and any(it["kind"] == "event" for it in items):
                first_event = now
            settled = first_event is not None and (not settings["batch_events"]
                                                   or now - first_event >= EVENT_SETTLE_S)
            if agent.wakes(items) or settled or now - start >= timeout_s:
                items = await call(agent.take, True)
                return await result(items=items, timed_out=not items)
            if now - last_progress >= PROGRESS_S:
                await ctx.report_progress(now - start, timeout_s, "waiting for the owner")
                last_progress = now
            await asyncio.sleep(min(POLL_S, timeout_s - (now - start)))

    @mcp.tool()
    async def requests() -> dict[str, Any]:
        """For the approver only: the agents and programs asking for a token, waiting on your decision. Each also
        arrives once through check or wait, as kind "request"."""
        out = await call(agent.client.get, "/api/enroll")
        return await result(requests=[_request_item(r) for r in out])

    @mcp.tool()
    async def decide(id: str, approve: bool, reason: str | None = None) -> dict[str, Any]:
        """For the approver only: approve or deny a request for a token (id from requests). Approve an agent or
        program you expect the owner to want, by its name and directory, which are its own word, and the address it
        asked from; when unsure, ask the owner first. Approved, it collects its token itself; you never see it.
        reason: told to the asker."""
        out = await call(agent.client.post, f"/api/enroll/{quote(id, safe='')}/{'approve' if approve else 'deny'}",
                         {"reason": reason})
        return await result(id=out["id"], name=out["name"], state=out["state"])

    @optional("react")
    async def react(id: str, emoji: str, channel: str | None = None) -> dict[str, Any]:
        """React to one of the owner's messages (its id from check or wait) with an emoji. channel: the
        channel it was in, if it was."""
        name = agent.need()
        path = f"/api/channels/{channel}/react" if channel else f"/api/{segment(agent.line)}/react"
        await call(agent.client.post, path, {"name": name, "id": id, "emoji": emoji})
        return await result(ok=True)

    @optional("status")
    async def status(text: str) -> dict[str, Any]:
        """Set your one-line status (at most 140 characters), shown to the owner beside your name until you
        change it; "" clears it. It sends nothing."""
        out = await call(agent.client.post, "/api/status", {"name": agent.need(), "text": text})
        return await result(status=out["status"])

    @optional("typing")
    async def typing(channel: str | None = None) -> dict[str, Any]:
        """Show the owner you are writing to them (or in a channel), until you send there or 3 minutes pass."""
        name = agent.need()
        path = f"/api/channels/{channel}/typing" if channel else f"/api/{segment(agent.line)}/typing"
        out = await call(agent.client.post, path, {"name": name})
        return await result(where=out["where"], for_s=out["for_s"])

    @optional("channels")
    async def channels() -> dict[str, Any]:
        """The channels, their topics, and who listens to each."""
        out = await call(agent.client.get, "/api/channels")
        return await result(channels=[{"name": c["name"], "topic": c["topic"], "listening": c["listening"]}
                                      for c in out])

    @mcp.tool()
    async def voices() -> dict[str, Any]:
        """The voices, and which agent has each. Pick a free one that fits how you identify."""
        out = await call(agent.client.get, "/api/voices")
        return await result(voices=out)

    @optional("controls")
    async def controls() -> dict[str, Any]:
        """The controls you can attach to send or ask, each with its spec."""
        out = await call(agent.client.get, "/api/controls")
        return await result(controls=out)

    @optional("board")
    async def board() -> dict[str, Any]:
        """Every agent the owner sees: grouped as in their sidebar, whether each is connected, listening,
        responding or typing, and its status."""
        out = await call(agent.client.get, "/api/board")
        return await result(board=out)

    if agent.name is not None and agent.name.casefold() in {n.casefold() for n in settings["board_managers"]}:
        @mcp.tool()
        async def arrange(groups: list[dict[str, Any]] | None = None, hidden: list[str] | None = None) -> dict[str, Any]:
            """You are a board manager: arrange the owner's board (their sidebar). With neither argument, how it is
            arranged now. groups: [{"name": ..., "members": [agents' names or lines' labels]}], in order, each
            member in one group; replaces every group. hidden: names kept off the board while they are not
            connected and nothing of theirs is unread; replaces the list."""
            if groups is None and hidden is None:
                out = await call(agent.client.get, "/api/board")
                return await result(**out["arrangement"])
            patch = {k: v for k, v in (("groups", groups), ("hidden", hidden)) if v is not None}
            out = await call(agent.client.post, "/api/board/arrange", {**patch, "name": agent.name})
            return await result(**out)

    @optional("thread")
    async def thread(id: str) -> dict[str, Any]:
        """A whole thread, first message to last: what a reply is about. id: a message's id, or a thread's name
        (re.thread). A channel's threads, and those on your own line."""
        name = agent.need()
        out = await call(agent.client.get, f"/api/agents/{quote(name, safe='')}/thread/{quote(id, safe='')}")
        return await result(**out)

    @optional("history")
    async def history(n: int = 20) -> dict[str, Any]:
        """Your line's last n messages (1 to 200), newest first, with when each was delivered and to whom."""
        name = agent.need()
        out = await call(agent.client.get, f"/api/{segment(agent.line)}/history", name=name, n=n)
        return await result(messages=out)

    if agent.talk:
        @mcp.tool()
        async def tell(to: str, text: str) -> dict[str, Any]:
            """Write to another agent by its name. It arrives through its check or wait as kind "agent". For
            coordinating with other agents, not for reporting to the owner."""
            out = await call(agent.client.post, "/api/talk", {"from": agent.need(), "to": to, "text": text})
            return await result(id=out["id"])

    @mcp.prompt()
    def on_call() -> str:
        """Stay reachable: join if needed, then act on what the owner sends until told to stop."""
        return on_call_text(owner, agent.name)

    return mcp


def on_call_text(owner: str, name: str | None) -> str:
    """The on_call prompt agents can invoke."""
    who = f"as {name}" if name else "with your role's name"
    return (f"Go on call for {owner} through nanotea. Join {who} if you haven't, then call wait, and call it "
            f"again each time it times out. Act on what {owner} sends, reporting with send, until {owner} "
            f"tells you to stop.")


def tool_definitions(cfg: dict, controls: bool = True) -> dict[str, dict]:
    """Every tool as a model is shown it (name, description, input schema), with every setting on, or every one
    but controls."""
    everything = {**DEFAULTS, "agent_messages": "shown", "tools": {**DEFAULTS["tools"], "controls": controls}}
    agent = Agent(Client(cfg["port"], None), cfg["app"]["owner"], everything)
    preset = argparse.Namespace(name=None, line=None, voice=None, channel=[], idle="finish", wait_s=WAIT_DEFAULT_S,
                              bang=False)
    tools = asyncio.run(make(cfg, agent, preset).list_tools())
    return {t.name: t.model_dump(by_alias=True, exclude_none=True, include={"name", "description", "input_schema"})
            for t in tools}


def _send(agent: Agent, text: str, title: str | None, channel: str | None, attach: list[str],
          ask: bool, raw: bool = False, control: dict | None = None, re_: str | None = None) -> dict:
    name = agent.need()
    if agent.voice is None:
        raise ValueError("you have no voice yet: call voices, then join again with the same name and a free voice")
    files = [{"name": Path(p).name, "data": base64.b64encode(Path(p).read_bytes()).decode()} for p in attach]
    body = {"text": text, "from": name, "title": title, "voice": agent.voice, "ask": ask, "raw": raw,
            "attachments": files}
    if channel:
        body["channel"] = channel
    if control is not None:
        body["control"] = control
    if re_ is not None:
        body["re"] = re_
    out = agent.client.post("/api/messages", body)
    return {"id": out["id"], "url": out["url"]}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nanotea mcp", description="Nanotea MCP server for one agent session, over stdio.")
    p.add_argument("--name", help="join as this agent at start, instead of waiting for the join tool")
    p.add_argument("--line", help="with --name: the line to hold (default: the name)")
    p.add_argument("--voice", help="with --name: the voice id")
    p.add_argument("--channel", action="append", default=[], help="with --name: a channel to receive posts from")
    p.add_argument("--idle", choices=IDLE_MODES, default="finish",
                   help="what the agent is told to do with nothing left to do: check, then finish unless put on "
                        "call (default); wait in a loop; or end its turn because a hook (nanotea hook) brings it "
                        "back")
    p.add_argument("--wait-s", type=int, default=WAIT_DEFAULT_S,
                   help=f"the wait tool's default timeout (default {WAIT_DEFAULT_S}: under the 60 s many harnesses "
                        f"allow a tool call)")
    p.add_argument("--bang", action="store_true",
                   help="run the owner's bang commands (!command on this agent's line) in this session, if they "
                        "have turned them on in Settings. Only someone who can edit the harness's MCP config can "
                        "give this; it is what lets a command run on this machine")
    a = p.parse_args(argv)
    if not a.name and (a.line or a.voice or a.channel):
        p.error("--line, --voice and --channel go with --name")
    if not 1 <= a.wait_s <= WAIT_MAX_S:
        p.error(f"--wait-s must be 1 to {WAIT_MAX_S}")
    cfg = load_config()
    try:
        server, agent = build(cfg, a)
    except (NanoteaError, ValueError) as err:
        sys.exit(f"nanotea mcp: {err}")
    try:
        server.run()
    finally:
        try:
            agent.close()
        except NanoteaError as err:
            sys.exit(f"nanotea mcp: closing the session: {err}")


if __name__ == "__main__":
    main()
