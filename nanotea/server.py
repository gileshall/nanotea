"""HTTP service.

Who a request is comes from a credential, never from where it came from. The owner presents the pairing key: a
paired browser's cookie, a link's ?k=, or as a bearer token for the owner's own commands. An agent presents its
token (Authorization: Bearer nt_...), which is bound to its name; a program reporting events presents an events
token, bound to a source and a line. The owner's key reads and changes everything of the owner's and can't act as
an agent; a token acts only as its own agent, or reports only its own events. Nothing is open but the
app's static assets and leaves. The trusted proxy (which terminates TLS for public_url) only tells the service
how to log an address.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import socketserver
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from nanotea import (audio, bang, configedit, configview, controls, delivery, export, files, leaf, markdown, pages,
                     plugins, version)
from nanotea.board import Board, BoardError
from nanotea.channels import NAME, ChannelError, Channels
from nanotea.config import CONFIG, Config, ConfigError, load_config, load_env_file, parse_env, resolve
from nanotea.hush import HushBook, HushError
from nanotea.index import parse_cursor, thread_key, thread_of_owner
from nanotea.inbox import Inbox, InboxError, excerpt
from nanotea.mcp_server import tool_definitions
from nanotea.notify import Note
from nanotea.pages import API_SEGMENT, BOX_LABEL, CHAT_PATH, DM_NAME
from nanotea.push import PushBook
from nanotea.rules import RuleBook, RuleError
from nanotea.prompts import Prompts, PromptError
from nanotea.settings import FEATURES, SCHEMA, SECTIONS, SettingsBook, SettingsError, costs, session_total
from nanotea.status import ListenBook, StatusBook, StatusError
from nanotea.talk import TalkBook, TalkError
from nanotea.themes import Look, ThemeError
from nanotea.themes import from_config as themes_from_config
from nanotea.client import segment
from nanotea.enroll import EnrollBook
from nanotea.tokens import TOKEN, TokenBook, TokenError
from nanotea.store import (ID_PATTERN, KIND_OF_EXT, PENDING, Message, Store, check_markers, now_iso,
                                 show_markers)
from nanotea.voices import VoiceBook, VoiceError
from nanotea.worker import Escalator, Transcriber, Worker

log = logging.getLogger("nanotea")

AUDIO_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "aac": "audio/aac",
               "flac": "audio/flac", "aif": "audio/aiff", "aiff": "audio/aiff", "ogg": "audio/ogg",
               "opus": "audio/ogg", "webm": "audio/webm", "caf": "audio/x-caf"}
RECORDING_EXTS = {"audio/mp4": "m4a", "audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav"}
RECORDING_TYPES = {ext: ctype for ctype, ext in RECORDING_EXTS.items()}
# What the browser says the microphone did for a take: key -> allowed types. None: not reported.
CAPTURE = {"raw": (bool,), "rate": (int, type(None)), "channels": (int, type(None)),
           "echo_cancellation": (bool, type(None)), "noise_suppression": (bool, type(None)),
           "auto_gain_control": (bool, type(None)), "mic": (str, type(None))}
MAX_BODY = 200 * 1024 * 1024
RANGE = re.compile(r"bytes=(\d*)-(\d*)")
COOKIE = "nanotea_key"
M = ID_PATTERN
LISTENING_WINDOW_S = 15
RESPONDING_CAP_S = 15 * 60  # how long a holder that never says it is idle shows as responding, at most
RELISTEN_GRACE_S = 120      # how long after a delivery a holder may go without a listener and still show as responding
TYPING_S = 3 * 60           # how long --typing lasts without a post
PAGE = 50                   # thread items or list messages a page loads at a time
CONNECTED_S = 60 * 60       # an agent whose listener polled this recently counts as connected
SESSION_STALE_S = 60        # an MCP session that hasn't beaten this long is gone (it beats every 20 s)
HELD_CHECK_S = 15           # how often held agents are checked for notifying the owner
HUSH_CHECK_S = 10           # how often quiet hours are checked for being over
BANG_CHECK_S = 1            # how often queued and running bang commands are checked for expiring
UNSEEN = "delivered IS NOT NULL AND seen IS NULL"
WAITING = "delivered IS NOT NULL AND ask = 1 AND reply IS NULL AND set_aside IS NULL"
VIEW_MAX = 200              # messages the Waiting and Unread pages list, newest first
FAILURE_TEXT_MAX = 3000
TAKE_GAP_S = 0.3  # silence between recordings joined into one message
# Inbox API segments: the main inbox, then a direct line per agent, made when it first claims one.
SEGS = rf"inbox|dm-{DM_NAME}"


def box_of_segment(seg: str) -> str | None:
    return next((box for box, s in list(API_SEGMENT.items()) if s == seg), None)


# What only the owner's key does: acting as the owner, changing settings, rules and tokens. Every other POST is
# the agents' API, which needs a token.
OWNER_POSTS = re.compile(rf"/api/messages/{M}/(drafts|files|reply|delete|heard|react|act)"
                         rf"|/api/({SEGS})/(drafts|files|messages)|/api/push/subscribe"
                         rf"|/api/channels|/api/channels/{NAME}/(drafts|files|messages)"
                         rf"|/api/settings|/api/prompts|/api/rules|/api/rules/[0-9a-f]+/delete"
                         rf"|/api/read-all|/api/set-aside|/api/hush|/api/theme|/api/roster|/api/board"
                         rf"|/api/tokens|/api/tokens/[0-9a-f]{{8}}/revoke|/api/approver|/api/config"
                         rf"|/api/config/secret")
# What a token's holder reads as itself, named for it (?name=, or in the path): the owner's key may not.
AGENT_GETS = re.compile(rf"/api/agents/[^/]+/(answers|waiting|bang)|/api/agents/[^/]+/thread/[^/]+|/api/talk/pending"
                        rf"|/api/({SEGS})/(pending|history)|/api/channels/{NAME}/pending")
# What either reads: what agents need to know about the service, and a message, which an agent reads only if
# it sent it.
SHARED_GETS = re.compile(rf"/api/(settings|rules|channels|voices|controls|board)"
                         rf"|/api/channels/{NAME}|/api/messages/{M}")
# A bang command's full output, which only the owner reads.
BANG_OUT = re.compile(rf"/bang/({SEGS})/({M})/(stdout|stderr)")
# What an events token posts, to its own line.
EVENT_POST = re.compile(rf"/api/({SEGS})/events")
# What a program without a token posts, from this machine: asking for one, and collecting it once approved.
ENROLL_OPEN = re.compile(r"/api/enroll|/api/enroll/[0-9a-f]{8}/collect")
# What the owner and the approver do with requests.
ENROLL_DECIDE = re.compile(r"/api/enroll/([0-9a-f]{8})/(approve|deny)|/api/enroll/told")
# Headers a proxy adds; a program on this machine sends none.
FORWARDED = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host", "X-Real-IP")
# Pages: the owner's. Browsers use public_url, so one that arrives without the key is sent there.
THREAD = rf"[mg]-{M}"
PAGES = re.compile(rf"/|/chat|/chat/{DM_NAME}|/write|/m/{M}|/t/{THREAD}|/c/{NAME}|/settings|/config|/prompts|/rules|/talk"
                   rf"|/waiting|/unread|/search|/notifications|/export.md|/tokens")
# Public web-app assets; no secrets.
STATIC_DIR = Path(__file__).parent / "static"
STATIC = {"/sw.js": ("sw.js", "application/javascript")}
# The app's icons: the leaf named for the app, in the owner's theme, at each size in pixels.
ICONS = {"/apple-touch-icon.png": 180, "/icon-180.png": 180, "/icon-192.png": 192, "/icon-512.png": 512,
         "/icon-64.png": 64}
LEAF_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def _plain(s: str) -> str:
    return s.casefold()


class SessionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class BangError(Exception):
    """A bang command that can't be sent or reported on, and why."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _latest(*isos: str | None) -> str | None:
    known = [x for x in isos if x is not None]
    return max(known, key=datetime.fromisoformat) if known else None


def _cursor(query: dict, name: str) -> str | None:
    """A thread cursor from the query string, checked; absent or empty: None."""
    text = query.get(name, [""])[0]
    if not text:
        return None
    parse_cursor(text)
    return text


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _reply_key(path: Path) -> str:
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24))
    return path.read_text().strip()


def _attachment(a: dict) -> tuple[str, bytes]:
    name, data = a["name"], a["data"]
    if not (isinstance(name, str) and isinstance(data, str)):
        raise ValueError("each attachment needs a name and base64 data")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in KIND_OF_EXT:
        raise ValueError(f"attachment {name!r} is not audio, an image, a video or a PDF "
                         f"({', '.join(sorted(KIND_OF_EXT))})")
    raw = base64.b64decode(data, validate=True)
    if not raw:
        raise ValueError(f"attachment {name!r} is empty")
    return Path(name).name, raw


def _emoji(value) -> str:
    """One emoji (possibly a multi-codepoint sequence): short, no letters, no whitespace."""
    if not (isinstance(value, str) and 0 < len(value) <= 16
            and not any(c.isspace() or (c.isascii() and c.isalpha()) for c in value)):
        raise ValueError("a reaction must be a single emoji")
    return value


def _capture(draft: str, c) -> dict:
    if not (isinstance(c, dict) and set(c) == set(CAPTURE)):
        raise ValueError(f"capture of {draft} must have exactly {', '.join(CAPTURE)}")
    for key, kinds in CAPTURE.items():
        if not isinstance(c[key], kinds) or (isinstance(c[key], bool) and bool not in kinds):
            raise ValueError(f"capture of {draft}: bad {key} {c[key]!r}")
    return c


def _owner_says(body: dict, draft_audio, drafts_dir: Path, mix: audio.Mix) -> tuple[str | None, Path | None, list,
                                                                                    list, list]:
    """Validate {text, drafts, captures, files} from the recorder. Returns the text; one audio file (the
    recording, or several recordings joined in order); and the attached files' drafts. ("draft", a single
    id, is what pages from before multi-recording send; accepted so an open page doesn't lose its
    recording.) Also returns the recordings used, for the caller to keep with the message, and the takes:
    {stored, type, size, capture?} each, stored None for a single recording (it is the message's audio,
    as recorded), else where the caller keeps it. captures: {draft: what the microphone did}, from pages
    that report it."""
    text = body.get("text")
    if "draft" in body and "drafts" in body:
        raise ValueError("send 'drafts' or 'draft', not both")
    drafts = [body["draft"]] if body.get("draft") is not None else body.get("drafts", [])
    file_ids = body.get("files", [])
    if text is not None and not (isinstance(text, str) and text.strip()):
        raise ValueError("'text' must be a non-empty string or null")
    if not (isinstance(drafts, list) and all(isinstance(d, str) for d in drafts)):
        raise ValueError("'drafts' must be a list of recording ids")
    if not (isinstance(file_ids, list) and all(isinstance(f, str) for f in file_ids)):
        raise ValueError("'files' must be a list of file ids")
    captures = body.get("captures", {})
    if not (isinstance(captures, dict) and set(captures) <= set(drafts)):
        raise ValueError("'captures' must map recording ids sent in 'drafts' to what the microphone did")
    captures = {d: _capture(d, c) for d, c in captures.items()}
    if text is None and not drafts and not file_ids:
        raise ValueError("send text, a recording, or a file")
    attached = files.drafts_for(drafts_dir, file_ids)
    paths = []
    for draft in drafts:
        path = draft_audio(draft)
        if path is None:
            raise ValueError(f"recording {draft} was never uploaded")
        paths.append(path)
    used = list(paths)
    takes = [{"stored": None if len(paths) == 1 else f"takes/{path.name}",
              "type": RECORDING_TYPES[path.suffix[1:]], "size": path.stat().st_size,
              **({"capture": captures[draft]} if draft in captures else {})} for draft, path in zip(drafts, paths)]
    if len(paths) > 1:
        parts = [paths[0]]
        for path in paths[1:]:
            parts += [TAKE_GAP_S, path]
        # RuntimeError if ffmpeg can't read a recording
        paths = [mix.join(parts, paths[0].with_name(f"joined-{secrets.token_hex(4)}"))]
    # Leading spaces can be code's indent; blank lines before and spaces after are dropped.
    text = re.sub(r"^(?:[ \t]*\n)+", "", text).rstrip() if text else None
    return text, (paths[0] if paths else None), attached, used, takes


def _act_problem(done) -> str | None:
    """What is wrong with what a control plugin's act returned, if anything."""
    if not isinstance(done, controls.Act):
        return "act must return an Act"
    if not isinstance(done.state, dict):
        return "its state must be an object"
    try:
        size = len(json.dumps(done.state))
        json.dumps(done.data)
    except (TypeError, ValueError) as err:
        return f"its state and data must be JSON: {err}"
    if size > controls.STATE_MAX:
        return f"its state is {size} bytes; at most {controls.STATE_MAX}"
    if done.says is not None and not (isinstance(done.says, str) and done.says.strip()):
        return "what it says must be non-empty text, or None"
    return None


def _takes(item: dict, audio_path, path_of) -> list[dict]:
    """The owner's recordings as made, for an agent: {path, type, size, capture?} each."""
    return [{**{k: v for k, v in t.items() if k != "stored"},
             "path": str(audio_path if t["stored"] is None else path_of(t["stored"]))}
            for t in item.get("takes", [])]


@dataclass(frozen=True)
class Who:
    """What a request's credential makes it: the owner, an agent (name), or an events source (name, line). host:
    the owner, with the host key, which only a shell on this machine reads. token: an agent's or source's token id."""
    kind: str
    name: str | None = None
    line: str | None = None
    host: bool = False
    token: str | None = None


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, cfg: Config, env: dict[str, str]):
        self.cfg = cfg
        self.started = now_iso()  # tells the Configuration page this process is a new one
        self.config_lock = threading.Lock()  # one change to config.toml or env_file at a time
        self.server: ThreadingHTTPServer | None = None
        self.restarting = False
        # Only that a key is set, and values to scrub from pages; the secrets themselves are shown nowhere.
        self.env_set = {k for k, v in env.items() if v}
        self.secret_values = sorted({v for v in env.values() if len(v) >= 6}, key=len, reverse=True)
        self.env_path = resolve(cfg["env_file"]) if cfg.get("env_file") else None
        self.store = Store(resolve(cfg["data_dir"]))  # builds the index from the files
        self.prompts = Prompts(self.store.root, cfg["app"]["owner"])
        self.app_cfg = cfg["app"]
        self.look = Look(self.store.root, *themes_from_config(cfg))
        self.icons: dict[tuple, bytes] = {}
        self.icon_lock = threading.Lock()
        # "main" is the default inbox; every other agent gets a direct line by name.
        self.dms_path = self.store.root / "dms.json"  # direct lines: {name: label}
        for box, label in (json.loads(self.dms_path.read_text()) if self.dms_path.exists() else {}).items():
            pages.add_dm(box, label)
        self.boxes = {box: Inbox(self.store.root / ("inbox" if box == "main" else f"inbox-{box}"), self.store.index)
                      for box in API_SEGMENT}
        self.dm_lock = threading.Lock()
        self.claim_lock = threading.Lock()  # one line per agent, across lines
        self.voices = VoiceBook(self.store.root / "voices.json")
        self.status = StatusBook(self.store.root)
        self.listened = ListenBook(self.store.root)
        self.board = Board(self.store.root, cfg["app"]["groups"])
        self.key = _reply_key(self.store.root / "reply_key")
        # Never in a link, cookie or page: what only a shell on this machine has, for what the key alone can't do.
        self.host_key = _reply_key(self.store.root / "host_key")
        self.tokens = TokenBook(self.store.root)
        self.enroll = EnrollBook(self.store.root, self.tokens)
        self.public_url = cfg["public_url"]
        url = urlsplit(self.public_url)
        self.public_origin = f"{url.scheme}://{url.netloc}"
        self.public_host = url.netloc
        self.local_origins = {f"http://{h}:{cfg['port']}" for h in ("127.0.0.1", "localhost", "[::1]")}
        self.proxies = set(cfg["trusted_proxies"])
        ncfg = cfg["notify"]
        self.push = PushBook(self.store.root, ncfg.get("push", {}).get("subject"))
        self.push_on = "push" in ncfg["now"] + ncfg["escalate"]
        plugins.add_path(cfg.get("plugin_path", []))
        ctx = {"env": env, "owner": cfg["app"]["owner"], "app_name": cfg["app"]["name"], "data": self.store.root,
               "services": {"push": self.push}}
        built = plugins.build_all(cfg, ctx)  # as nanotea config check builds them
        self.tts = built["tts"]
        self.transcriber = Transcriber(built["stt"])
        self.notify_now = built["notify_now"]
        self.mix, self.max_upload = audio.settings(cfg)
        self.uploads = self.store.root / "uploads"  # files on their way in; any here at start were cut off
        if self.uploads.exists():
            shutil.rmtree(self.uploads)
        self.uploads.mkdir()
        self.hush = HushBook(self.store.root)
        self.rewriter = built["rewrite"]
        self.worker = Worker(self.store, self.rewriter, self.tts, self.notify_now,
                             self.note_for, pages.who, self.mix, self.hush, lambda: self.settings.get())
        # An empty escalate list turns escalation off.
        self.escalator = (Escalator(self.store, built["notify_escalate"],
                                    ncfg["escalate_after_min"], self.note_for, self.hush) if ncfg["escalate"] else None)
        self.idle_at = {box: None for box in self.boxes}  # when each holder last waited with idle=1
        self.typing: dict[str, dict[str, float]] = {}  # an inbox, or "#channel" -> agent -> monotonic time
        self.typing_lock = threading.Lock()
        self.last_listen = {box: 0.0 for box in self.boxes}  # monotonic time of each holder's last poll
        self.channels = Channels(self.store.root, self.store.index, self.app_cfg["name"], self.app_cfg["channels"])
        self.settings = SettingsBook(self.store.root, cfg.get("settings"))
        # Bang commands: queued ones by message id -> {box, state, queued, expire_s, timeout_s, session, started}.
        self.bang_cfg = bang.settings(cfg)
        self.delivery = delivery.settings(cfg)
        self.bang_dir = self.store.root / "bang"
        self.bang_log = self.store.root / "bang-log.jsonl"
        self.bang_cond = threading.Condition()
        self.bang_jobs: dict[str, dict] = {}
        self.bang_log_lock = threading.Lock()
        self.rules = RuleBook(self.store.root)
        self.talk = TalkBook(self.store.root)
        # Open MCP sessions, in memory: session id -> {name, line, pid, since, beat (monotonic)}.
        self.sessions: dict[str, dict] = {}
        # Agents at a harness permission prompt: name -> {at, what, pushed}.
        self.held: dict[str, dict] = {}
        self.presence_lock = threading.Lock()
        self.controls = built["controls"]
        self.control_lock = threading.Lock()
        self.tool_defs = tool_definitions(cfg)
        self.plain_tool_defs = tool_definitions(cfg, controls=False)
        pages.configure(vapid_public_key=self.push.public_key if self.push_on else "", key=self.key,
                        sidebar=self.sidebar, names=self.voices.names, app=self.app_cfg, presence=self.presence,
                        controls=self.controls, look=self.look, bang=lambda: self.settings.get()["bang"])

    def restart(self) -> None:
        """Stop serving; serve() then starts this process again with the config as it now is."""
        self.restarting = True
        threading.Thread(target=self.server.shutdown, name="restart", daemon=True).start()

    # Sessions and holds: whether anyone is there to pick up what the owner sends.

    def open_sessions(self, name: str | None = None) -> list[dict]:
        """Live sessions, oldest first (one agent's, with name); dead and silent ones are dropped."""
        now = time.monotonic()
        with self.presence_lock:
            for sid, s in list(self.sessions.items()):
                if now - s["beat"] >= SESSION_STALE_S or not _pid_alive(s["pid"]):
                    log.info("session %s of %s (pid %d) is gone", sid, s["name"], s["pid"])
                    del self.sessions[sid]
            live = [{**s, "session": sid} for sid, s in self.sessions.items()]
        return sorted((s for s in live if name is None or s["name"] == name), key=lambda s: s["since"])

    def open_session(self, name: str, sid: str, pid: int, line: str, bang: bool = False,
                     channels: list[str] | None = None) -> dict:
        """Registers a session; refused while another of the same agent's is live and one_session is on. bang: its
        process was started with the local opt-in to run the owner's bang commands. channels: those it listens in,
        for the push hook and nanotea listen, which take for it from outside the session."""
        with self.presence_lock:
            held = self.sessions.get(sid)
        if held is not None and held["name"] != name:
            raise SessionError(403, f"session {sid} is another agent's; start yours with a new id")
        others = [s for s in self.open_sessions(name) if s["session"] != sid]
        if others and self.settings.get()["one_session"]:
            o = others[0]
            raise SessionError(409, f"{name!r} already has a live session (pid {o['pid']}, since {o['since']}). "
                                    f"One session per agent is on: end that session first, or turn it off in "
                                    f"Settings.")
        with self.presence_lock:
            s = self.sessions.get(sid)
            if s is not None and s["name"] != name:
                raise SessionError(403, f"session {sid} is another agent's; start yours with a new id")
            s = self.sessions[sid] = {"name": name, "line": line, "pid": pid, "bang": bang,
                                      "channels": list(channels or []),
                                      "since": s["since"] if s and s["name"] == name else now_iso(),
                                      "beat": time.monotonic()}
        log.info("session %s of %s opened (pid %d, line %s%s)", sid, name, pid, line, ", bang" if bang else "")
        return dict(s)

    def channels_of(self, name: str) -> list[str]:
        """The channels name's open sessions listen in."""
        return sorted({c for s in self.open_sessions(name) for c in s["channels"]})

    def beat(self, sid: str) -> None:
        with self.presence_lock:
            if sid not in self.sessions:
                raise SessionError(404, f"no session {sid}; register it again")
            self.sessions[sid]["beat"] = time.monotonic()

    def close_session(self, sid: str) -> None:
        with self.presence_lock:
            s = self.sessions.pop(sid, None)
        if s is None:
            raise SessionError(404, f"no session {sid}")
        log.info("session %s of %s closed", sid, s["name"])

    # Bang commands. The owner's !command is queued as a message on a direct line and handed to the line's one
    # session, whose own process runs it (nanotea/bang.py); nothing runs in the service.

    def bang_audit(self, event: str, **fields) -> None:
        line = json.dumps({"at": now_iso(), "event": event, "by": "owner", **fields})
        with self.bang_log_lock, self.bang_log.open("a") as f:
            f.write(line + "\n")

    def bang_send(self, inbox: Inbox, box: str, command: str) -> dict:
        """Queues command, typed by the owner on line box, for that line's one session. BangError says why not. Returns
        the owner's message."""
        holder = inbox.info()["holder"]
        who = {"box": box, "agent": holder, "command": command}
        # Only the holder's sessions count: another agent's session on the line neither runs nor blocks a command.
        on_line = [s for s in self.open_sessions() if s["line"] == box]
        sessions = [s for s in on_line if s["name"] == holder]
        try:
            if not sessions and on_line:
                others = ", ".join(f"{s['name']!r} pid {s['pid']}" for s in on_line)
                raise BangError(409, f"the sessions open on this line are not the holder's ({others}), and "
                                     f"{holder or 'nobody'} holds the line. A command runs only in the holder's own "
                                     "session.")
            if not sessions:
                raise BangError(409, f"no session is open on {pages.BOX_LABEL[box]}'s line. A command runs only in a "
                                     "live session started with --bang.")
            if len(sessions) > 1:
                pids = ", ".join(f"{s['name']} pid {s['pid']}" for s in sessions)
                raise BangError(409, f"{len(sessions)} sessions are open on this line ({pids}). A command runs only "
                                     "when exactly one is, so that it is clear where it runs.")
            if not sessions[0]["bang"]:
                raise BangError(403, f"the session ({sessions[0]['name']} pid {sessions[0]['pid']}) wasn't started "
                                     "with --bang, so it can't run commands. Start its nanotea mcp or "
                                     "nanotea-tell --listen with --bang.")
        except BangError as err:
            self.bang_audit("refused", reason=str(err), **who)
            raise
        s, c = sessions[0], self.bang_cfg
        spec = {"state": "queued", "command": command, "box": box, "session": s["session"], "pid": s["pid"],
                "queued_at": now_iso(), "timeout_s": c["timeout_s"], "expire_s": c["expire_s"],
                "output_kb": c["output_kb"], "keep_mb": c["keep_mb"]}
        self.bang_audit("queued", session=s["session"], pid=s["pid"], **who)
        msg = inbox.add(command, None, holder, None, bang=spec)
        with self.bang_cond:
            self.bang_jobs[msg["id"]] = {"box": box, "state": "queued", "queued": time.monotonic(),
                                         "expire_s": c["expire_s"], "timeout_s": c["timeout_s"],
                                         "session": s["session"], "agent": s["name"], "output_kb": c["output_kb"]}
            self.bang_cond.notify_all()
        return msg

    def _bang_session(self, name: str, sid: str) -> dict:
        s = next((x for x in self.open_sessions(name) if x["session"] == sid), None)
        if s is None:
            raise SessionError(404, f"no session {sid}; register it again")
        if not s["bang"]:
            raise SessionError(403, f"session {sid} wasn't started with --bang")
        return s

    def bang_take(self, name: str, sid: str, wait_s: float) -> dict | None:
        """The next command for session sid of agent name, waiting up to wait_s for one: {id, command, timeout_s,
        keep_bytes}, now marked running, or None. Asking is the session's heartbeat."""
        deadline = time.monotonic() + wait_s
        with self.bang_cond:
            while True:
                s = self._bang_session(name, sid)
                self.beat(sid)
                if job := self._bang_claim(s):
                    return job
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.bang_cond.wait(min(left, 1.0))

    def _bang_claim(self, s: dict) -> dict | None:
        """Called holding bang_cond. The oldest queued command that was vetted for this session, if it is still the
        line's only one and the owner still has bang commands on."""
        for msg_id, job in sorted(self.bang_jobs.items()):
            if job["state"] != "queued" or job["box"] != s["line"] or job["session"] != s["session"]:
                continue
            if not self.settings.get()["bang"]:
                self._bang_end(msg_id, "expired", "not run: bang commands were turned off in Settings")
                continue
            if len([x for x in self.open_sessions() if x["line"] == s["line"] and x["name"] == s["name"]]) != 1:
                continue  # a second session opened since: it expires rather than run somewhere unclear
            inbox = self.boxes[job["box"]]
            if (holder := inbox.info()["holder"]) != s["name"]:
                self._bang_end(msg_id, "expired", f"not run: {holder or 'nobody'} holds the line now, not "
                                                  f"{s['name']}")
                continue
            (m,) = inbox.messages("id = ?", (msg_id,))
            inbox.update(msg_id, bang={**m["bang"], "state": "running", "started_at": now_iso()})
            job["state"], job["started"] = "running", time.monotonic()
            self.bang_audit("started", id=msg_id, box=job["box"], agent=s["name"], session=s["session"], pid=s["pid"],
                            command=m["text"])
            return {"id": msg_id, "command": m["text"], "timeout_s": m["bang"]["timeout_s"],
                    "keep_bytes": m["bang"]["keep_mb"] << 20}
        return None

    def _bang_end(self, msg_id: str, state: str, reason: str | None = None, **fields) -> None:
        """Called holding bang_cond. A queued or running command ends as state, with what happened."""
        job = self.bang_jobs.pop(msg_id)
        inbox = self.boxes[job["box"]]
        (m,) = inbox.messages("id = ?", (msg_id,))
        inbox.update(msg_id, bang={**m["bang"], "state": state, "reason": reason, "finished_at": now_iso(), **fields})
        kept = {k: v for k, v in fields.items() if k in ("exit", "signal", "timed_out", "duration_s", "cwd")}
        self.bang_audit(state, id=msg_id, box=job["box"], session=job["session"], reason=reason, command=m["text"],
                        **kept)
        self.bang_cond.notify_all()

    def bang_report(self, name: str, msg_id: str, body: dict) -> None:
        """The session's result for a command it was handed: {session, error} or what bang.run returned, with the
        streams in base64. ValueError for a body that isn't one."""
        sid = body.get("session")
        if not isinstance(sid, str):
            raise ValueError("'session' must be the session id")
        with self.bang_cond:
            job = self.bang_jobs.get(msg_id)
            if job is None or job["state"] != "running":
                raise BangError(409, f"{msg_id} isn't running; it may have ended already (expired, or lost when its "
                                     f"session stopped reporting)")
            if job["session"] != sid or job["agent"] != name:  # the session may have closed since it took it
                raise BangError(403, f"{msg_id} was handed to another session")
            if "error" in body:
                if not isinstance(body["error"], str):
                    raise ValueError("'error' must be a string")
                return self._bang_end(msg_id, "failed", f"not run: the session couldn't start it: {body['error']}")
            fields = self._bang_result(msg_id, body)
            self._bang_end(msg_id, "done", **fields)

    def _bang_result(self, msg_id: str, body: dict) -> dict:
        """The result as filed: output capped for the model and the owner's page, and kept whole under data/bang."""
        want = {"exit": int, "timed_out": bool, "duration_s": (int, float), "cwd": str, "shell": str, "lingering": bool,
                "stdout_bytes": int, "stderr_bytes": int, "stdout": str, "stderr": str}
        for key, kind in want.items():
            if not isinstance(body.get(key), kind) or (kind is int and isinstance(body[key], bool)):
                raise ValueError(f"{key!r} is missing or the wrong type")
        if body.get("signal") is not None and not isinstance(body["signal"], str):
            raise ValueError("'signal' must be a string or null")
        raw = {k: base64.b64decode(body[k], validate=True) for k in ("stdout", "stderr")}
        out_dir = self.bang_dir / msg_id
        out_dir.mkdir(parents=True, exist_ok=True)
        shown, cut = {}, {}
        for k, data in raw.items():
            tmp = out_dir / f"{k}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, out_dir / k)
            shown[k], info = bang.cap(data, self.bang_jobs[msg_id]["output_kb"] * 1024, body[f"{k}_bytes"])
            if info:
                cut[k] = info
        return {"exit": body["exit"], "signal": body.get("signal"), "timed_out": body["timed_out"],
                "duration_s": body["duration_s"], "cwd": body["cwd"], "shell": body["shell"],
                "lingering": body["lingering"], "stdout": shown["stdout"], "stderr": shown["stderr"],
                "truncated": cut or None, "full_output": str(out_dir)}

    def bang_sweep(self) -> None:
        """Ends the commands that can no longer run or report: queued too long or with bang turned off, or running in
        a session that has stopped beating."""
        now = time.monotonic()
        live = {x["session"] for x in self.open_sessions()}
        on = self.settings.get()["bang"]
        with self.bang_cond:
            for msg_id, job in list(self.bang_jobs.items()):
                if job["state"] == "queued" and not on:
                    self._bang_end(msg_id, "expired", "not run: bang commands were turned off in Settings")
                elif job["state"] == "queued" and now - job["queued"] >= job["expire_s"]:
                    busy = any(j["state"] == "running" and j["box"] == job["box"] for j in self.bang_jobs.values())
                    self._bang_end(msg_id, "expired", "not run: the session was still busy with an earlier command"
                                   if busy else f"not run: no session picked it up within {job['expire_s']} s")
                elif job["state"] == "running" and (job["session"] not in live
                                                    or now - job["started"] >= job["timeout_s"] + bang.RUNNING_GRACE_S):
                    self._bang_end(msg_id, "lost", "the session stopped reporting while it ran, so what happened is "
                                                   "unknown")

    def watch_bang(self) -> None:
        while True:
            time.sleep(BANG_CHECK_S)
            try:
                self.bang_sweep()
            except Exception:
                log.exception("checking bang commands failed")

    def bang_settle(self) -> None:
        """At start: whatever was queued or running when the service stopped is over, and says so."""
        for box, inbox in list(self.boxes.items()):
            for m in inbox.messages("json_extract(meta, '$.bang.state') IN ('queued', 'running')"):
                state = "expired" if m["bang"]["state"] == "queued" else "lost"
                reason = "the service restarted" + (" before it ran" if state == "expired"
                                                    else " while it ran, so what happened is unknown")
                inbox.update(m["id"], bang={**m["bang"], "state": state, "reason": reason, "finished_at": now_iso()})
                self.bang_audit(state, id=m["id"], box=box, reason=reason, command=m["text"])

    def hold(self, name: str, what: str | None) -> None:
        with self.presence_lock:
            self.held.setdefault(name, {"at": now_iso(), "what": what, "pushed": False})["what"] = what
        log.info("%s is held at a permission prompt: %s", name, what or "(no detail)")

    def active(self, name: str) -> None:
        """The agent did something, so it isn't held at a prompt any more."""
        with self.presence_lock:
            gone = self.held.pop(name, None)
        if gone is not None:
            log.info("%s is no longer held (held since %s)", name, gone["at"])

    def held_of(self, name: str | None) -> dict | None:
        if name is None:
            return None
        with self.presence_lock:
            h = self.held.get(name)
            return {"at": h["at"], "what": h["what"]} if h else None

    def presence(self, box: str) -> tuple[str, str]:
        """Who is there to pick up what the owner sends on a line: (state, words). state: held, listening, open
        (a session is open but not checking now), closed, or nobody (no holder)."""
        holder = self.boxes[box].info()["holder"]
        if holder is None:
            return "nobody", "nobody holds this line"
        if held := self.held_of(holder):
            return "held", f"held at a permission prompt since {pages.clock(held['at'])}"
        if self.listening(box):
            return "listening", "listening now"
        if self.open_sessions(holder):
            last = self.listened.all().get(holder)
            return "open", ("session open, " + (f"last checked {pages.ago(last)}" if last else "hasn't checked yet"))
        return "closed", "no session open; messages wait until it checks"

    def known_agents(self) -> dict[str, str]:
        """Every agent name the service knows, casefolded -> as written."""
        names = ([h for h in self.holders().values() if h] + list(self.listened.all())
                 + list(self.voices.owners().values()) + [s["name"] for s in self.open_sessions()])
        return {_plain(n): n for n in names}

    def claim(self, box: str, name: str, about: str | None) -> dict:
        """An agent holds one line: two would give it two rows in the sidebar, each showing all its messages, and
        nothing to say which line its own messages belong to."""
        with self.claim_lock:
            self.one_line(box, name)
            return self.boxes[box].claim(name, about)

    def one_line(self, box: str, name: str) -> None:
        if held := sorted(b for b, inbox in list(self.boxes.items()) if b != box and inbox.info()["holder"] == name):
            other = held[0]
            raise InboxError(409, f"{name!r} already holds the line {other!r}, and an agent holds one line. Join that "
                                  f"one (nanotea mcp --line {other}), or release it first (nanotea-tell --from "
                                  f"{shlex.quote(name)} --close-inbox --inbox {other})")

    def doubled(self) -> dict[str, list[str]]:
        """Agents holding more than one line, from before an agent could hold only one."""
        lines: dict[str, list[str]] = {}
        for box, holder in self.holders().items():
            if holder is not None:
                lines.setdefault(holder, []).append(box)
        return {name: sorted(boxes) for name, boxes in lines.items() if len(boxes) > 1}

    def tokenless(self) -> list[str]:
        """Agents the service has seen that hold no live token: they were here before tokens, or lost theirs."""
        covered = {t["name"].casefold() for t in self.tokens.all() if t["kind"] == "agent"}
        return sorted((n for k, n in self.known_agents().items() if k not in covered), key=_plain)

    def watch_held(self) -> None:
        """Notifies the owner once per hold when an agent stays at a permission prompt held_push_min."""
        while True:
            time.sleep(HELD_CHECK_S)
            try:
                after = self.settings.get()["held_push_min"]
                if not after:
                    continue
                now = datetime.now().astimezone()
                with self.presence_lock:
                    due = [(n, h) for n, h in self.held.items() if not h["pushed"]
                           and (now - datetime.fromisoformat(h["at"])).total_seconds() >= after * 60]
                    for _, h in due:
                        h["pushed"] = True
                for name, h in due:
                    box = self.box_of(name)
                    path = CHAT_PATH[box] if box else "/"
                    note = Note(title=f"{pages.who(name)} is waiting at a permission prompt",
                                text=h["what"] or "Its harness is asking for permission.", path=path,
                                link=f"{self.public_url}{path}?k={self.key}", tag=f"held-{name}",
                                unseen=self.unseen_count())
                    for n in self.notify_now:
                        try:
                            n.send(note)
                        except Exception:
                            log.exception("notifying the owner that %s is held via %s failed", name,
                                          type(n).__name__)
            except Exception:
                log.exception("checking held agents failed")

    def watch_quiet(self) -> None:
        """After quiet hours, one notification for what was held, if any of it still needs the owner."""
        while True:
            time.sleep(HUSH_CHECK_S)
            try:
                held = self.hush.take_held()
                if held:
                    self._summarize(held)
            except Exception:
                log.exception("summarizing what quiet hours held failed")

    def _summarize(self, ids: list[str]) -> None:
        msgs = [self.store.load(i) for i in ids if self.store.exists(i)]
        asks = [m for m in msgs if m.ask and self.store.reply(m.id) is None]
        unseen = [m for m in msgs if not m.ask and self.store.seen(m.id) is None]
        if not (asks or unseen):
            log.info("quiet hours held %d messages; none still needs the owner", len(ids))
            return
        parts = ([f"{len(asks)} question{'' if len(asks) == 1 else 's'} waiting"] if asks else []) \
            + ([f"{len(unseen)} unread"] if unseen else [])
        senders = sorted({pages.who(m.sender) for m in asks + unseen}, key=str.casefold)
        path = "/waiting" if asks else "/unread"
        note = Note(title=f"While you were away: {', '.join(parts)}", text=f"From {', '.join(senders)}.", path=path,
                    link=f"{self.public_url}{path}?k={self.key}", tag="quiet-summary", unseen=self.unseen_count())
        for n in self.notify_now:
            try:
                n.send(note)
            except Exception:
                log.exception("sending the quiet hours summary via %s failed", type(n).__name__)

    def hush_view(self) -> dict:
        """The notifications page's data: the settings, and every agent and channel a mute can name."""
        now = self.hush.get()
        agents = sorted(self.known_agents().values(), key=_plain)
        channels = [f"#{c.name}" for c in self.channels.all()]
        known = {n.casefold() for n in agents + channels}
        return {"hush": now, "agents": agents, "channels": channels,
                "stale": [n for n in now["muted"] if n.casefold() not in known]}

    def settings_view(self) -> dict:
        c = costs(self.app_cfg["owner"], self.tool_defs, self.rules.all(), self.plain_tool_defs)
        now = self.settings.get()
        cap = self.bang_cfg["output_kb"]
        about = {"bang": f" Output reaches the agent's context, up to about {cap * 2 * 1024 // 4} tokens a command "
                         f"({cap} KB a stream; \"[bang] output_kb\" in the config). Turned on only from the "
                         f"machine nanotea runs on, with nanotea bang on; off from anywhere.",
                 "config_programs": " Turned on only from the machine nanotea runs on, with nanotea config programs "
                                    "on; off from anywhere."}
        return {"settings": now, "costs": c, "session_tokens": session_total(c, now), "delivery": self.delivery,
                "bang_sessions": sorted({s["name"] for s in self.open_sessions() if s["bang"]}, key=_plain),
                "features": [{"key": k, "label": label, "about": about_ + about.get(k, "")}
                             for k, label, about_ in FEATURES],
                "sections": [{"key": k, "label": label, "about": about_} for k, label, about_ in SECTIONS],
                "schema": [{"key": x.key, "section": x.section, "label": x.label, "about": x.about + about.get(x.key, ""),
                            "kind": x.kind, "default": x.default, "choices": [list(c) for c in x.choices], "lo": x.lo,
                            "hi": x.hi} for x in SCHEMA]}

    def icon(self, px: int) -> bytes:
        """The app's icon at px: its leaf in the theme's dark-side leaf color on its light-side sidebar color.
        Kept per theme and size; the theme can change while the service runs."""
        c = self.look.colors()
        key = (c["light"]["side"], c["dark"]["leaf"], px)
        with self.icon_lock:
            if key not in self.icons:
                self.icons[key] = leaf.png(self.app_cfg["name"], px, fg=c["dark"]["leaf"],
                                           bg=c["light"]["side"], scale=0.78)
            return self.icons[key]

    def favicon(self) -> str:
        """The app's leaf for a browser tab: the theme's light-side leaf color."""
        return leaf.svg(self.app_cfg["name"], size=32).replace(
            'fill="currentColor"', f'fill="{self.look.colors()["light"]["leaf"]}"', 1)

    def look_view(self) -> dict:
        """The owner's theme and appearance, and every theme's colors, light and dark."""
        return {**self.look.get(), "themes": self.look.themes}

    def add_dm(self, box: str, label: str) -> None:
        """A new direct line, named by the agent that first claims it."""
        with self.dm_lock:
            if box in self.boxes:
                return
            dms = json.loads(self.dms_path.read_text()) if self.dms_path.exists() else {}
            dms[box] = label
            tmp = self.dms_path.with_name("dms.json.tmp")
            tmp.write_text(json.dumps(dms, indent=2))
            os.replace(tmp, self.dms_path)
            pages.add_dm(box, label)
            self.last_listen[box] = 0.0
            self.idle_at[box] = None
            self.boxes[box] = Inbox(self.store.root / f"inbox-{box}", self.store.index)
        log.info("new direct line %s (%s)", box, label)

    def direct_senders(self) -> set[str]:
        return {s for (s,) in self.store.index.rows("SELECT DISTINCT sender FROM msg WHERE channel IS NULL")}

    def senders_of(self, box: str, info: dict, direct: set[str] | None = None) -> list[str]:
        """Whose messages make the line's conversation: its holder, or for a line nobody holds, whoever goes by
        its label. direct: every sender of a direct message, if known."""
        if info["holder"]:
            return [info["holder"]]
        label = _plain(BOX_LABEL[box])
        return sorted(s for s in (self.direct_senders() if direct is None else direct) if _plain(s) == label)

    def sidebar(self) -> dict:
        """channels: rows of path, name, role, unread, listening, and typing (agents typing there). groups:
        [{name, rows}], a row for each direct line and for each agent with direct messages and no line: path,
        name, role, who, agent (its holder or sender; None for a line nobody holds), unread, listening, status,
        typing (its holder or nobody), responding, listened (its last listener poll, or None), connected, and
        seen (for one not connected, when it was last seen: listened, wrote, or set its status; or None),
        session ({since, count} of its open MCP sessions, or None) and held ({at, what} while it is at a
        permission prompt, or None). Every message not yet seen counts in exactly one row; unread: their total,
        for the tab. waiting: questions nobody has answered. talk: whether the owner sees agents' messages to each other."""
        idx = self.store.index
        unread = {(s, c): n for s, c, n in idx.rows(
            "SELECT sender, channel, count(*) FROM msg WHERE delivered IS NOT NULL AND seen IS NULL"
            " GROUP BY sender, channel")}
        last_direct = dict(idx.rows("SELECT sender, max(t) FROM msg WHERE channel IS NULL GROUP BY sender"))
        last_any = dict(idx.rows("SELECT sender, max(t) FROM msg GROUP BY sender"))
        counted: set[tuple[str, str | None]] = set()

        def count(keys) -> int:
            keys = [k for k in keys if k in unread]
            counted.update(keys)
            return sum(unread[k] for k in keys)

        channels = [{"path": f"/c/{ch.name}", "name": ch.name, "role": ch.info["topic"],
                     "unread": count(k for k in unread if k[1] == ch.name),
                     "listening": bool(self.channel_listening(ch.name)), "typing": self.typing_in(f"#{ch.name}")}
                    for ch in self.channels.all()]
        names, statuses, listened = self.voices.names(), self.status.all(), self.listened.all()
        now = datetime.now().astimezone()
        recent = lambda iso: iso is not None and (now - datetime.fromisoformat(iso)).total_seconds() < CONNECTED_S
        wrote = lambda s: datetime.fromtimestamp(last_any[s]).astimezone().isoformat(timespec="seconds")
        lines = [(box, self.boxes[box].info()) for box in list(self.boxes)]
        lines = [(box, info, self.senders_of(box, info, set(last_direct))) for box, info in lines]
        owner: dict[str, str] = {}  # a sender -> the line its direct messages count on; holders first
        for box, info, senders in sorted(lines, key=lambda line: line[1]["holder"] is None):
            for s in senders:
                owner.setdefault(s, box)
        rows = []
        for box, info, senders in lines:
            holder = info["holder"]
            sender = holder or BOX_LABEL[box]  # a line nobody holds goes by whoever opened it
            # The first name, then the sender and the label where each says something new.
            title = [names.get(sender) or sender]
            for bit in (sender, BOX_LABEL[box]):
                if bit and _plain(bit) not in {_plain(x) for x in title}:
                    title.append(bit)
            sent = datetime.fromtimestamp(last_direct[holder]).astimezone() if holder in last_direct else None
            typing = [a for a in self.typing_in(box) if a == holder]
            row = {"path": CHAT_PATH[box], "name": title[0], "role": " · ".join(title[1:]), "who": pages.who(sender),
                   "mark": sender, "agent": holder, "unread": count((s, None) for s in senders if owner[s] == box),
                   "listening": self.listening(box), "status": statuses.get(holder) if holder else None,
                   "typing": typing, "responding": bool(holder) and self.responding(box, info, sent),
                   "listened": listened.get(holder) if holder else None,
                   "session": self.session_of(holder), "held": self.held_of(holder)}
            row["connected"] = bool(holder) and (row["listening"] or row["responding"] or bool(typing)
                                                 or bool(row["session"]) or recent(listened.get(holder)))
            row["seen"] = None if row["connected"] else _latest(
                listened.get(holder) if holder else None, info.get("since"),
                *(wrote(s) for s in senders if s in last_any), *(statuses[s]["at"] for s in senders if s in statuses))
            keys = {box} | {_plain(s) for s in ([holder] if holder else [BOX_LABEL[box], *senders])}
            rows.append((row, keys, box == "main"))
        for s in sorted(set(last_direct) - set(owner)):
            plain = s
            row = {"path": f"/?from={quote(s)}", "name": names.get(s) or s,
                   "role": plain if _plain(plain) != _plain(names.get(s) or s) else "", "who": pages.who(s),
                   "mark": s, "agent": s, "unread": count([(s, None)]), "listening": False, "status": statuses.get(s),
                   "typing": [], "responding": False, "listened": listened.get(s), "session": self.session_of(s),
                   "held": self.held_of(s)}
            row["connected"] = bool(row["session"]) or recent(listened.get(s))
            row["seen"] = None if row["connected"] else _latest(
                listened.get(s), wrote(s), statuses[s]["at"] if s in statuses else None)
            rows.append((row, {_plain(s)}, False))
        if missed := {k: n for k, n in unread.items() if k not in counted}:
            raise RuntimeError(f"unseen messages with no sidebar row: {missed}")
        board = self.board.get()
        groups, hidden = board["groups"], {_plain(n) for n in board["hidden"]}
        named: dict[str, list] = {g["name"]: [] for g in groups}
        other, gone = [], []
        for row, keys, main in rows:
            rank = next(((i, j) for i, g in enumerate(groups) for j, m in enumerate(g["members"])
                         if _plain(m) in keys), None)
            if not (row["connected"] or main):
                if not (keys & hidden and not row["unread"]):
                    gone.append(row)
            elif rank is not None:
                named[groups[rank[0]]["name"]].append((rank[1], row))
            else:
                other.append(row)
        # The main line leads the first group it is in; others keep their group's order.
        out = [{"name": g["name"], "rows": [r for _, r in sorted(named[g["name"]], key=lambda x: (
            x[1]["path"] != CHAT_PATH["main"], x[0]))]} for g in groups]
        out.append({"name": "Other", "rows": sorted(other, key=lambda r: (r["path"] != CHAT_PATH["main"],
                                                                          not r["listening"]))})
        gone.sort(key=lambda r: r["seen"] or "", reverse=True)
        out.append({"name": "Not connected", "rows": gone})
        total = sum(c["unread"] for c in channels) + sum(r["unread"] for r, _, _ in rows)
        return {"channels": channels, "groups": [g for g in out if g["rows"]], "unread": total,
                "waiting": self.waiting_count(), "talk": self.settings.get()["agent_messages"] == "shown"}

    def session_of(self, name: str | None) -> dict | None:
        live = self.open_sessions(name) if name else []
        return {"since": live[0]["since"], "count": len(live)} if live else None

    def responding(self, box: str, info: dict, sent: datetime | None) -> bool:
        """The owner's last message reached the holder and it hasn't written back: until its next message in the
        conversation, or until it waits again saying it is idle (an MCP wait, or a listener run with --idle). A
        holder that never says so shows as responding for at most RESPONDING_CAP_S. Either way, only while a
        listener holds the inbox again, or for RELISTEN_GRACE_S after the delivery."""
        got = info.get("owner_delivered_at")
        if got is None:
            return False
        got = datetime.fromisoformat(got)
        if sent is not None and sent >= got.replace(microsecond=0):  # message times are to the second
            return False
        since = (datetime.now().astimezone() - got).total_seconds()
        if not self.listening(box) and since >= RELISTEN_GRACE_S:
            return False  # nothing came back for the inbox: the delivery may never have been read
        idle = self.idle_at[box]
        if idle is not None and idle > got:
            return False
        return since < RESPONDING_CAP_S

    def set_typing(self, where: str, agent: str) -> None:
        with self.typing_lock:
            self.typing.setdefault(where, {})[agent] = time.monotonic()

    def typing_done(self, where: str, agent: str) -> None:
        with self.typing_lock:
            self.typing.get(where, {}).pop(agent, None)

    def typing_in(self, where: str) -> list[str]:
        """Who is typing in an inbox or "#channel" now, by name."""
        now = time.monotonic()
        with self.typing_lock:
            here = self.typing.get(where, {})
            for agent in [a for a, t in here.items() if now - t >= TYPING_S]:
                del here[agent]
            return sorted(here)

    def holders(self) -> dict[str, str | None]:
        return {box: inbox.info()["holder"] for box, inbox in list(self.boxes.items())}

    def box_of(self, sender: str) -> str | None:
        return next((box for box, holder in self.holders().items() if holder == sender), None)

    def path_for(self, msg: Message) -> str:
        """Where a message opens in the app: its channel, or its conversation for an inbox holder's messages."""
        if msg.channel is not None:
            return f"/c/{msg.channel}#m-{msg.id}"
        box = self.box_of(msg.sender)
        return f"{CHAT_PATH[box]}#m-{msg.id}" if box else f"/m/{msg.id}"

    def link(self, msg: Message) -> str:
        """Absolute, for the owner only: opening it pairs the device."""
        path, _, anchor = self.path_for(msg).partition("#")
        return f"{self.public_url}{path}?k={self.key}" + (f"#{anchor}" if anchor else "")

    def unseen_count(self) -> int:
        return self.store.index.rows(f"SELECT count(*) FROM msg WHERE {UNSEEN}")[0][0]

    def waiting_count(self) -> int:
        return self.store.index.rows(f"SELECT count(*) FROM msg WHERE {WAITING}")[0][0]

    def note_for(self, msg: Message, waited_min: int | None = None) -> Note:
        if msg.status == "failed":
            title = f"{pages.who(msg.sender)}: message failed"
            body = self.store.read_text(msg.id, "script.txt") or self.store.read_text(msg.id, "original.md")
            text = f"{msg.error}\n\n{show_markers(body, msg.attachments)}"
            if len(text) > FAILURE_TEXT_MAX:
                text = text[:FAILURE_TEXT_MAX] + "\n(cut short; the full text is on the page)"
        else:
            title = f"{pages.who(msg.sender)}{' asks' if msg.ask else ''}: {msg.title_hint or msg.title}"
            text = show_markers(markdown.plain(self.store.read_text(msg.id, "script.txt")), msg.attachments)
        if msg.channel is not None:
            title = f"#{msg.channel} · {title}"
        if waited_min is not None:
            title = f"No response for {waited_min} min. {title}"
        return Note(title=title, text=text, path=self.path_for(msg), link=self.link(msg), tag=msg.id,
                    unseen=self.unseen_count())

    def notify_enroll(self, request: dict) -> None:
        """With no approver, the owner is told of each request for a token."""
        what = f"events to {request['line']}" if request["kind"] == "events" else "an agent"
        note = Note(title=f"{request['name']} asks for a token", text=f"{what}, from {request['dir'] or 'no dir given'}",
                    path="/tokens", link=f"{self.public_url}/tokens?k={self.key}", tag=f"enroll-{request['id']}",
                    unseen=self.unseen_count())
        for n in self.notify_now:
            try:
                n.send(note)
            except Exception:
                log.exception("notifying the owner of request %s via %s failed", request["id"], type(n).__name__)

    def reaction_note(self, page: str, by: str, emoji: str, owner_msg: dict) -> Note:
        """An agent reacted to one of the owner's messages, on page (a conversation or channel)."""
        about = markdown.plain(owner_msg["text"] or "") or owner_msg["transcript"] or "your voice message"
        anchor = f"#g-{owner_msg['id']}"
        return Note(title=f"{by} reacted {emoji}", text=about, path=f"{page}{anchor}",
                    link=f"{self.public_url}{page}?k={self.key}{anchor}", tag=f"react-{owner_msg['id']}",
                    unseen=self.unseen_count())

    def channel_listening(self, name: str) -> list[str]:
        """The agents listening to the channel now."""
        now = time.monotonic()
        return [a for a, t in self.channels.get(name).last_listen.items() if now - t < LISTENING_WINDOW_S]

    # A thread is what a conversation or channel page shows, oldest first: the agent side and the owner's side as
    # index filters (Index.thread), read a page at a time.

    def thread_of(self, where: str) -> tuple[str | None, tuple | None, tuple]:
        """where: an inbox, "#channel", or "~<thread>" for one thread (its first message and every reply). Returns
        the holder (None for a channel, a thread or a line nobody holds), and the two filters."""
        if where.startswith("~"):
            key = where[1:]
            if key.startswith("m-"):
                return None, ("id = ? OR thread = ?", (key[2:], key)), ("thread = ?", (key,))
            folder = self.thread_root_folder(key)
            return None, ("thread = ?", (key,)), ("(folder = ? AND id = ?) OR thread = ?", (folder, key[2:], key))
        if where.startswith("#"):
            name = self.channels.get(where[1:]).name
            return None, ("channel = ?", (name,)), ("folder = ? AND reaction = 0",
                                                                          (f"channel-{name}",))
        inbox = self.boxes[where]
        info = inbox.info()
        senders = self.senders_of(where, info)
        marks = ",".join("?" * len(senders))
        agents = (f"channel IS NULL AND sender IN ({marks})", tuple(senders)) if senders else None
        # What the owner sent here that reached these agents, or that nobody has picked up yet.
        owner_side = (f"folder = ? AND (delivered_to IS NULL OR delivered_to IN ({marks}))", (inbox.folder, *senders))
        return info["holder"], agents, owner_side

    def thread_root_folder(self, key: str) -> str | None:
        """The folder of a thread the owner started, or None once that message is gone."""
        folders = self.store.index.owner_folders(key[2:])
        if len(folders) > 1:
            raise ValueError(f"{key[2:]} names messages in {', '.join(folders)}; a thread can't start at both")
        return folders[0] if folders else None

    def thread_root(self, key: str):
        """A thread's first message, ("agent", Message) or ("owner", dict with its folder), or None once deleted."""
        if not re.fullmatch(THREAD, key):
            raise ValueError(f"no thread {key!r}: a thread is m-<id> or g-<id>")
        if key.startswith("m-"):
            return ("agent", self.store.load(key[2:])) if self.store.exists(key[2:]) else None
        folder = self.thread_root_folder(key)
        if folder is None:
            return None
        (meta,), = self.store.index.rows("SELECT meta FROM owner WHERE folder = ? AND id = ?", (folder, key[2:]))
        return "owner", {**json.loads(meta), "folder": folder}

    def owner_open_to(self, agent: str, m: dict) -> bool:
        """Whether agent may see the owner's message m (its meta, with folder): any agent one in a channel; on a line,
        the agent it was given to, or while nobody has been given it, the line's holder."""
        if m["folder"].startswith("channel-"):
            return True
        given = {m.get("delivered_to")} | {d["to"] for d in m.get("deliveries", [])}
        if given != {None}:
            return agent in given
        return any(inbox.folder == m["folder"] and inbox.info()["holder"] == agent
                   for inbox in list(self.boxes.values()))

    def thread_open_to(self, agent: str, key: str) -> tuple[list, str | None]:
        """Thread key's items and its channel, if agent may see it: any agent a channel's, only the agents in it (who
        wrote in it, or may see one of the owner's messages in it) a direct line's. On a line, the owner's messages
        agent may not see are left out. LookupError for no such thread, PermissionError for one on another agent's
        line."""
        items, _ = self.thread(f"~{key}", limit=None)
        if not items:
            raise LookupError(f"no thread {key}")
        channels = {m.channel for k, m, _, _ in items if k == "agent"} | {
            m["folder"].removeprefix("channel-") if m["folder"].startswith("channel-") else None
            for k, m, _, _ in items if k == "owner"}
        channel = next(iter(channels - {None}), None)
        if channel is not None:
            return items, channel
        items = [item for item in items if item[0] == "agent" or self.owner_open_to(agent, item[1])]
        if not any(k == "owner" or m.sender == agent for k, m, _, _ in items):
            raise PermissionError(f"thread {key} is on another agent's line")
        return items, channel

    def thread_for(self, agent: str, ref: str) -> dict:
        """A thread as an agent reads it, first to last. ref: a thread (m-<id>, g-<id>) or a message in one. A
        channel's threads are open to every agent; a direct line's, only to the agents in it. LookupError for no
        such thread, PermissionError for one on another agent's line."""
        key = ref if re.fullmatch(THREAD, ref) else self.reference(ref)[0]["thread"]
        root = self.thread_root(key)
        items, channel = self.thread_open_to(agent, key)
        out = []
        for k, m, _, _ in items:
            if k == "owner" and m.get("bang"):
                out.append({"id": m["id"], "from": "owner", "at": m["at"], "kind": "bang", "text": m["text"],
                            "state": m["bang"]["state"], "exit": m["bang"].get("exit"), "re": None})
                continue
            if k == "owner":
                out.append({"id": m["id"], "from": "owner", "at": m["at"], "kind": "message", "text": m["text"],
                            "transcript": m["transcript"], "files": [f["name"] for f in m.get("files", [])],
                            "re": m["re"]["id"] if m.get("re") else None})
                continue
            out.append({"id": m.id, "from": m.sender, "at": m.created, "kind": "question" if m.ask else "message",
                        "title": m.title_hint or m.title, "text": self.store.read_text(m.id, "original.md"),
                        "re": m.re["id"] if m.re else None})
            reply = self.store.reply(m.id)
            if reply is not None and reply["transcript_status"] != "pending":
                out.append({"id": f"answer-{m.id}", "from": "owner", "at": reply["at"], "kind": "answer",
                            "text": reply["text"], "transcript": reply["transcript"], "re": m.id})
        out.sort(key=lambda it: datetime.fromisoformat(it["at"]))
        return {"thread": key, "channel": channel, "first_deleted": root is None,
                "url": f"{self.public_url}/t/{key}", "messages": out}

    def owner_meta(self, folder: str, msg_id: str) -> dict:
        """The owner's message msg_id in folder, with its folder."""
        (meta,), = self.store.index.rows("SELECT meta FROM owner WHERE folder = ? AND id = ?", (folder, msg_id))
        return {**json.loads(meta), "folder": folder}

    def answerable_by(self, agent: str, re_id) -> bool:
        """False when re_id names only owner's messages agent may not see, so refusing it reads as no such message."""
        if not isinstance(re_id, str) or self.store.exists(re_id):
            return True
        folders = self.store.index.owner_folders(re_id)
        return not folders or any(self.owner_open_to(agent, self.owner_meta(f, re_id)) for f in folders)

    def reference(self, re_id) -> tuple[dict, str | None]:
        """What a reply to message re_id (an agent's or the owner's) says it answers, {id, kind, sender, title,
        thread}, and the channel that thread is in (None: a direct line). ValueError for no such message, or one that
        isn't a message to reply to."""
        if not (isinstance(re_id, str) and re.fullmatch(ID_PATTERN, re_id)):
            raise ValueError(f"'re' must be a message id, not {re_id!r}")
        if self.store.exists(re_id):
            t = self.store.load(re_id)
            return ({"id": t.id, "kind": "agent", "sender": t.sender, "title": t.title_hint or t.title,
                     "thread": thread_key(t.re) if t.re else f"m-{t.id}"}, t.channel)
        folders = self.store.index.owner_folders(re_id)
        if not folders:
            raise ValueError(f"no message {re_id!r} to reply to")
        if len(folders) > 1:
            raise ValueError(f"{re_id} names messages in {', '.join(folders)}; reply to another in its thread")
        m = self.owner_meta(folders[0], re_id)
        if what := ("a reaction" if m.get("reaction") else "a tap" if m.get("tap") else
                    "an event" if m.get("event") else "a command" if m.get("bang") else None):
            raise ValueError(f"{re_id} is {what}, not a message; reply to the message it was on")
        channel = folders[0].removeprefix("channel-") if folders[0].startswith("channel-") else None
        return ({"id": re_id, "kind": "owner", "sender": "owner", "title": excerpt(m),
                 "thread": thread_of_owner(m) or f"g-{re_id}"}, channel)

    def re_url(self, re_: dict | None) -> str | None:
        """Where a reply's target shows, in its thread. Old replies say no kind: they answered an agent's message."""
        if re_ is None:
            return None
        row = ("g-" if re_.get("kind") == "owner" else "m-") + re_["id"]
        return f"{self.public_url}/t/{thread_key(re_)}#{row}"

    def thread(self, where: str, before: str | None = None, after: str | None = None,
               limit: int | None = PAGE) -> tuple[list, bool]:
        """Items as (kind, Message or dict, sig, cursor), oldest first, and whether older ones remain: the newest
        limit, those older than before, or with after, all from that cursor on."""
        _, agents, owner_side = self.thread_of(where)
        items, more = self.store.index.thread(agents, owner_side, before, after, None if after else limit)
        return [(k, Message(**m) if k == "agent" else m, sig, c) for k, m, sig, c in items], more

    def folds(self, where: str):
        """For a conversation or channel under the owner's grouped view of replies: a function giving, for a thread
        whose first message shows in where, the page rows of its replies that show there, oldest first, so they
        fold under it; empty if the first message is elsewhere. A reply from another line (an agent answering
        a message on this one) is not in this view and is left out. None: every reply shows where it was sent
        (the linear view, and a thread's own page)."""
        if self.settings.get()["thread_view"] != "grouped" or where.startswith("~"):
            return None
        _, agents, owner_side = self.thread_of(where)
        index = self.store.index

        def shown(key: str) -> list[str]:
            table, side = ("msg", agents) if key.startswith("m-") else ("owner", owner_side)
            if side is None or not index.rows(f"SELECT 1 FROM {table} WHERE id = ? AND ({side[0]})",
                                              (key[2:], *side[1])):
                return []
            replies = []
            for table, side, kind in (("msg", agents, 0), ("owner", owner_side, 1)):
                if side is not None:
                    replies += [(t, kind, i) for t, i in index.rows(
                        f"SELECT t, id FROM {table} WHERE thread = ? AND ({side[0]})", (key, *side[1]))]
            return [("m-" if kind == 0 else "g-") + i for _, kind, i in sorted(replies)]
        return shown

    def older_exists(self, where: str, before: str) -> bool:
        _, agents, owner_side = self.thread_of(where)
        return self.store.index.thread(agents, owner_side, before=before, limit=0)[1]

    def thread_unseen(self, where: str) -> list[str]:
        """The thread's agent messages the owner hasn't seen: opening it shows them."""
        _, agents, _ = self.thread_of(where)
        if agents is None:
            return []
        return [i for (i,) in self.store.index.rows(f"SELECT id FROM msg WHERE ({agents[0]}) AND seen IS NULL",
                                                    agents[1])]

    def thread_version(self, where: str, items: list) -> str:
        """Changes with anything the page shows: the items (from the oldest it has), its header and the sidebar."""
        if where.startswith("#"):
            statuses = self.status.all()
            state = [[(a, statuses.get(a)) for a in self.channel_listening(where[1:])]]
        elif where.startswith("~"):
            state = []
        else:
            state = [self.boxes[where].info()["holder"]]
        # A reply elsewhere changes how many replies a row here says it has.
        roots = [f"m-{m.id}" if k == "agent" else f"g-{m['id']}" for k, m, _, _ in items]
        state += [self.sidebar(), [(c, sig) for _, _, sig, c in items], self.store.index.threads(roots),
                  self.settings.get()["thread_view"]]
        return hashlib.sha256(json.dumps(state).encode()).hexdigest()[:16]

    def channel_pending(self, name: str, agent: str) -> list[dict]:
        """What the agent hasn't been given yet since it first listened here, oldest first: other agents'
        posts, the owner's posts (once transcribed), and their answers to questions asked here."""
        channel = self.channels.get(name)
        cursor = channel.cursor(agent)
        since, given = datetime.fromisoformat(cursor["since"]), set(cursor["delivered"])
        out = []
        for meta, reply in self.store.index.rows("SELECT meta, reply FROM msg WHERE channel = ? AND "
                                                 "(t >= ? OR reply IS NOT NULL) ORDER BY t, id",
                                                 (name, since.timestamp())):
            msg = Message(**json.loads(meta))
            if msg.sender != agent and msg.id not in given and datetime.fromisoformat(msg.created) >= since:
                out.append({"kind": "post", "id": msg.id, "at": msg.created, "from": msg.sender,
                            "who": pages.who(msg.sender), "ask": msg.ask, "title": msg.title_hint or msg.title,
                            "text": self.store.read_text(msg.id, "original.md"),
                            "clips": [str(self.store.path(msg.id, "attachments") / a) for a in msg.attachments],
                            "url": f"{self.public_url}/m/{msg.id}",
                            "re": {**msg.re, "url": self.re_url(msg.re)} if msg.re else None})
            reply = json.loads(reply) if reply is not None else None
            if (reply is not None and reply["transcript_status"] != "pending" and f"answer-{msg.id}" not in given
                    and datetime.fromisoformat(reply["at"]) >= since):
                out.append({"kind": "answer", "id": f"answer-{msg.id}", "at": reply["at"],
                            "re": {"id": msg.id, "sender": msg.sender, "title": msg.title_hint or msg.title},
                            "re_url": f"{self.public_url}/c/{name}#m-{msg.id}", "reaction": bool(reply.get("reaction")),
                            "tap": reply.get("tap"),
                            "text": reply["text"], "audio": reply["audio"],
                            "audio_path": str(self.store.path(msg.id, reply["audio"])) if reply["audio"] else None,
                            "takes": _takes(reply, self.store.path(msg.id, reply["audio"] or ""),
                                            partial(self.store.path, msg.id)),
                            "transcript": reply["transcript"], "transcript_status": reply["transcript_status"],
                            "transcript_error": reply["transcript_error"],
                            "files": [{**f, "path": str(self.store.path(msg.id, f["stored"]))}
                                      for f in reply.get("files", [])]})
        for g in channel.messages("t >= ?", (since.timestamp(),)):
            if g["id"] in given or g["transcript_status"] == "pending" or datetime.fromisoformat(g["at"]) < since:
                continue
            out.append({**g, "kind": "owner",
                        "audio_path": str(channel.audio_path(g["id"])) if g["audio"] else None,
                        "takes": _takes(g, channel.audio_path(g["id"]), partial(channel.path_of, g["id"])),
                        "files": [{**f, "path": str(channel.path_of(g["id"], f["stored"]))} for f in g["files"]],
                        "re_url": self.re_url(g["re"])})
        out.sort(key=lambda it: datetime.fromisoformat(it["at"]))
        return out

    def transcribe_channel(self, name: str, msg_id: str) -> None:
        channel = self.channels.get(name)
        self.transcriber.submit((f"#{name} message {msg_id}", channel.audio_path(msg_id),
                                 partial(channel.update, msg_id)))

    def answers(self, agent: str) -> list[dict]:
        """Outcomes of the agent's questions it hasn't been given yet, oldest first: the owner's answer once
        transcribed, the question failing to send, or the owner setting it aside unanswered. A set-aside question
        answered later is delivered again, as the answer."""
        out = []
        for meta, reply, aside in self.store.index.rows(
                "SELECT meta, reply, set_aside FROM msg WHERE sender = ? AND ask = 1 AND answer_delivered IS NULL AND "
                "((reply IS NOT NULL AND reply_status != 'pending') OR (reply IS NULL AND status = 'failed') "
                "OR (reply IS NULL AND set_aside IS NOT NULL AND aside_delivered IS NULL)) "
                "ORDER BY t, id", (agent,)):
            msg = Message(**json.loads(meta))
            re_ = {"id": msg.id, "title": msg.title_hint or msg.title, "channel": msg.channel,
                   "url": f"{self.public_url}/m/{msg.id}"}
            if reply is None and aside is not None:
                # Shaped as an answer, so MCP servers from before set_aside existed still pass it on whole.
                owner = self.app_cfg["owner"]
                out.append({"kind": "set_aside", "set_aside": True, "id": f"aside-{msg.id}", "at": aside, "re": re_,
                            "text": f"{owner} set this question aside without answering it. Don't wait on it; "
                                    f"ask again if you still need the answer. {owner} can still answer it, and "
                                    "that answer would arrive as kind \"answer\".",
                            "reaction": False, "tap": None, "audio": None, "audio_path": None, "takes": [],
                            "transcript": None, "transcript_status": "done", "transcript_error": None, "files": []})
                continue
            if reply is None:
                out.append({"kind": "failed", "id": msg.id, "at": now_iso(), "re": re_, "error": msg.error})
                continue
            reply = json.loads(reply)
            out.append({"kind": "answer", "id": msg.id, "at": reply["at"], "re": re_, "text": reply["text"],
                        "reaction": bool(reply.get("reaction")), "tap": reply.get("tap"), "audio": reply["audio"],
                        "audio_path": str(self.store.path(msg.id, reply["audio"])) if reply["audio"] else None,
                        "takes": _takes(reply, self.store.path(msg.id, reply["audio"] or ""),
                                        partial(self.store.path, msg.id)),
                        "transcript": reply["transcript"], "transcript_status": reply["transcript_status"],
                        "transcript_error": reply["transcript_error"],
                        "files": [{**f, "path": str(self.store.path(msg.id, f["stored"]))}
                                  for f in reply.get("files", [])]})
        return out

    def listening(self, box: str) -> bool:
        return time.monotonic() - self.last_listen[box] < LISTENING_WINDOW_S

    def transcribe_reply(self, msg_id: str) -> None:
        recording = self.store.path(msg_id, self.store.reply(msg_id)["audio"])
        self.transcriber.submit((f"reply to {msg_id}", recording, partial(self.store.update_reply, msg_id)))

    def transcribe_inbox(self, box: str, msg_id: str) -> None:
        inbox = self.boxes[box]
        self.transcriber.submit((f"{box} inbox message {msg_id}", inbox.audio_path(msg_id),
                                 partial(inbox.update, msg_id)))

    def resume(self) -> None:
        self.bang_settle()
        marks = ",".join("?" * len(PENDING))
        for msg in reversed(self.store.query(f"status IN ({marks})", tuple(sorted(PENDING)))):
            log.info("resuming %s (was %s)", msg.id, msg.status)
            self.worker.submit(msg.id)
        for msg in reversed(self.store.query("reply_status = 'pending'")):
            log.info("resuming transcription of the reply to %s", msg.id)
            self.transcribe_reply(msg.id)
        for box, inbox in list(self.boxes.items()):
            for m in inbox.messages("transcript_status = 'pending'"):
                log.info("resuming transcription of %s inbox message %s", box, m["id"])
                self.transcribe_inbox(box, m["id"])
        for channel in self.channels.all():
            for m in channel.messages("transcript_status = 'pending'"):
                log.info("resuming transcription of #%s message %s", channel.name, m["id"])
                self.transcribe_channel(channel.name, m["id"])


class Handler(BaseHTTPRequestHandler):
    server_version = "nanotea"
    timeout = 60
    who: Who | None = None  # set once a request's credential is read

    def setup(self) -> None:
        super().setup()
        self.extra_headers: list[tuple[str, str]] = []

    def end_headers(self) -> None:
        # Every response, errors included: another site framing a paired page could steer the owner's taps.
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header(version.HEADER, version.RUNNING)  # long-lived clients hand over when it changes
        super().end_headers()

    @property
    def app(self) -> App:
        return self.server.app

    def _proxied(self) -> bool:
        """Arrived over HTTPS through a trusted proxy. Only the log reads this: it is the proxy's word, and who the
        request is comes from its credential. Headers are absent on malformed requests."""
        headers = getattr(self, "headers", None)
        return (self.client_address[0] in self.app.proxies and headers is not None
                and headers.get("X-Forwarded-Proto") == "https")

    def _foreign(self) -> str | None:
        """Why a request is refused before it is read: a browser's Origin that isn't this service's, a cross-site
        request. The Host decides nothing: who a request is comes from its credential, which a page on another
        site doesn't hold."""
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.app.public_origin and origin not in self.app.local_origins:
            return f"a request from {origin} is refused; open {self.app.public_url}"
        return None

    def _refuse_foreign(self) -> bool:
        """Answer 403 to a foreign request; True if it was refused."""
        if why := self._foreign():
            self.close_connection = True
            self._json(403, {"error": why})
            return True
        return False

    def address_string(self) -> str:
        if self._proxied():
            return f"{self.headers.get('X-Forwarded-For', '?').split(',')[0].strip()} via proxy"
        return super().address_string()

    def _has_key(self, query: dict) -> bool:
        """The pairing key, from a link (?k=, which also sets the cookie) or the cookie."""
        if _same(query.get("k", [""])[0], self.app.key):
            # Secure only over https: browsers drop a Secure cookie sent over plain http, and Safari even on localhost.
            secure = "; Secure" if self.app.public_url.startswith("https://") else ""
            self.extra_headers.append(("Set-Cookie", f"{COOKIE}={self.app.key}; Path=/; Max-Age=315360000; "
                                                     f"HttpOnly; SameSite=Lax{secure}"))
            return True
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        return COOKIE in cookie and _same(cookie[COOKIE].value, self.app.key)

    def _who(self, query: dict) -> Who:
        """Who this request is, from its credential alone. A bearer token is an agent's or an events source's, or
        the pairing key (the owner's own commands); else the pairing key in a link (query: its ?k=) or the
        cookie. AuthError 401 for none, or one that is wrong."""
        header = self.headers.get("Authorization")
        if header is not None:
            scheme, _, secret = header.partition(" ")
            secret = secret.strip()
            if scheme.lower() != "bearer" or not secret:
                raise AuthError(401, "send the credential as Authorization: Bearer <token>")
            if TOKEN.fullmatch(secret):
                try:
                    record = self.app.tokens.check(secret)
                except TokenError as err:
                    raise AuthError(err.status, str(err)) from None
                return Who(record["kind"], record["name"], record["line"], token=record["id"])
            if _same(secret, self.app.key):
                return Who("owner")
            if _same(secret, self.app.host_key):
                return Who("owner", host=True)
            raise AuthError(401, "unknown credential. An agent without a token asks for one: nanotea mcp does when "
                                 "it starts")
        if self._has_key(query):
            return Who("owner")
        raise AuthError(401, "no credential. An agent sends Authorization: Bearer <token>; without one, nanotea mcp "
                             "asks for approval of a token when it starts. The owner opens the pairing link: "
                             "nanotea pair-link")

    def _bind(self, who: Who, path: str, query: dict, body) -> None:
        """A token acts only as itself: every name the request gives for the actor, in the path, the query or the
        JSON body, must be the token's. An events source is bound by the source it reports as."""
        claims = [unquote(m[1]) for m in [re.match(r"/api/agents/([^/]+)/", path)] if m] + query.get("name", [])
        if isinstance(body, bytes):
            try:
                obj = json.loads(body)
            except ValueError:
                obj = None  # the handler answers a body that isn't JSON
            if isinstance(obj, dict):
                keys = ("source",) if who.kind == "events" else ("from", "name", "source")
                claims += [obj[k] for k in keys if isinstance(obj.get(k), str)]
        for claim in claims:
            if claim.strip() != who.name:
                other = claim.strip()
                if who.kind == "events":
                    raise AuthError(403, f"this events token is for source {who.name!r}, not {other!r}. Use "
                                         f"{other!r}'s own token")
                raise AuthError(403, f"this token is for {who.name!r}, not {other!r}. Use {other!r}'s own token")

    def _check_lines(self, who: Who, path: str, query: dict, body) -> None:
        """An agent's token bound to a line uses that line alone; any other agent's leaves it alone. The lines a
        request names: its path's, its ?line=, and a session's."""
        segs = [m[1] for m in [re.match(rf"/api/({SEGS})(?:/|$)", path)] if m] + query.get("line", [])
        lines = ["main" if s == "inbox" else s.removeprefix("dm-") for s in segs]
        if path == "/api/sessions" and isinstance(body, bytes):
            try:
                obj = json.loads(body)
            except ValueError:
                obj = None  # the handler answers a body that isn't JSON
            if isinstance(obj, dict) and isinstance(obj.get("line"), str):
                lines.append(obj["line"])
        for line in lines:
            if who.line is not None and line != who.line:
                raise AuthError(403, f"this token of {who.name!r} is bound to line {who.line!r}, not {line!r}")
            if (other := self.app.tokens.bound(line)) not in (None, who.name):
                raise AuthError(403, f"line {line!r} is bound to {other!r}'s token; {who.name!r} can't use it. "
                                     f"Use another line, or have the owner revoke {other!r}'s token")

    def _check_session(self, who: Who, path: str) -> None:
        """An agent beats and closes only its own sessions."""
        if m := re.fullmatch(r"/api/sessions/([0-9a-f]{1,64})/(beat|close)", path):
            with self.app.presence_lock:
                held = self.app.sessions.get(m[1])
            if held is not None and held["name"] != who.name:
                raise AuthError(403, f"session {m[1]} is {held['name']!r}'s, not {who.name!r}'s")

    def _refuse(self, err: AuthError) -> None:
        if err.status == 401:
            self.extra_headers.append(("WWW-Authenticate", 'Bearer realm="nanotea"'))
        self._json(err.status, {"error": str(err)})

    def _msg(self, pattern: str, path: str) -> str | None:
        m = re.fullmatch(pattern, path)
        return m[1] if m and self.app.store.exists(m[1]) else None

    def _folds(self) -> frozenset[str]:
        """The groups this device folds, from its nanotea-folds cookie (SHELL_SCRIPT writes it). A bad one is logged
        and the groups drawn open."""
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if "nanotea-folds" not in cookie:
                return frozenset()
            names = json.loads(unquote(cookie["nanotea-folds"].value))
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ValueError(f"not a list of names: {names!r}")
        except (CookieError, ValueError) as err:
            log.error("GET %s: bad nanotea-folds cookie: %s", self.path, err)
            return frozenset()
        return frozenset(names)

    def do_GET(self) -> None:
        self.who = None
        if self._refuse_foreign():
            return
        folded = pages.FOLDED.set(self._folds())
        try:
            self._get()
        finally:
            pages.FOLDED.reset(folded)

    def _get(self) -> None:
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/favicon.ico":
            return self._send(204, "image/x-icon", b"")
        if path in STATIC:
            name, ctype = STATIC[path]
            return self._send(200, ctype, (STATIC_DIR / name).read_bytes(), [("Cache-Control", "no-cache")])
        if path in ICONS:
            return self._send(200, "image/png", self.app.icon(ICONS[path]), [("Cache-Control", "no-cache")])
        if path == "/favicon.svg":
            return self._send(200, "image/svg+xml", self.app.favicon().encode(), [("Cache-Control", "no-cache")])
        if m := re.fullmatch(r"/leaf/([^/]+)\.svg", path):
            return self._leaf(unquote(m[1]), query)
        try:
            who = self._who(query)
            if who.kind == "events":
                raise AuthError(403, f"this events token only reports events; it can't read {path}")
            if path == "/api/enroll":
                # An agent asks every time it waits whether it has requests to decide: none, unless it is the approver.
                if not (who.kind == "agent" and query.get("new", [""])[0] == "1"):
                    self._check_approver(who, False)
            elif who.kind == "agent":
                if not (AGENT_GETS.fullmatch(path) or SHARED_GETS.fullmatch(path)):
                    raise AuthError(403, f"{path} is the owner's; an agent's token reads only what is its own")
                self._bind(who, path, query, None)
                self._check_lines(who, path, query, None)
                if m := re.fullmatch(rf"/api/messages/({M})", path):
                    sender = self.app.store.load(m[1]).sender if self.app.store.exists(m[1]) else who.name
                    if sender != who.name:
                        raise AuthError(403, f"message {m[1]} is {sender!r}'s; an agent reads only its own")
            elif AGENT_GETS.fullmatch(path):
                raise AuthError(403, f"{path} is for an agent's token; the owner's key can't act as an agent")
        except AuthError as err:
            if self.headers.get("Authorization") is None and err.status == 401 and (
                    PAGES.fullmatch(path) or path == "/manifest.webmanifest"):
                # A browser: tell it to pair, or if it came by another address than public_url, send it there,
                # where its cookie will be.
                at_public = self._proxied() or self.headers.get("Host") == self.app.public_host
                return self._html(pages.unpaired(), 403) if at_public else \
                    self._redirect(self.app.public_url + self.path)
            return self._refuse(err)
        self.who = who
        app, store = self.app, self.app.store
        is_owner = who.kind == "owner"  # a paired browser, so page views count as seen
        holders = app.holders()
        if path in ("/", "/api/list"):
            return self._list(path, query, holders)
        if path == "/manifest.webmanifest":
            if not is_owner:  # it carries the key; only a paired browser installs the app
                return self._json(403, {"error": "the manifest is for paired browsers"})
            return self._send(200, "application/manifest+json", json.dumps(pages.manifest()).encode())
        if path == "/write":
            return self._redirect("/chat")
        if box := next((b for b, p in list(CHAT_PATH.items()) if p == path), None):
            return self._thread_page(box, query, holders)
        if m := re.fullmatch(rf"/t/({THREAD})", path):
            return self._thread_view(m[1], query, holders)
        if m := re.fullmatch(rf"/c/({NAME})", path):
            try:
                app.channels.get(m[1])
            except ChannelError as err:
                return self._html(pages.not_found(str(err)), err.status)
            return self._thread_page(f"#{m[1]}", query, holders)
        if path in ("/api/chat/version", "/api/thread"):
            if "thread" in query:
                where = "~" + query["thread"][0]
                if not re.fullmatch(THREAD, where[1:]):
                    return self._json(400, {"error": f"no thread {where[1:]!r}"})
            elif "channel" in query:
                name = query["channel"][0]
                if name not in {ch.name for ch in app.channels.all()}:
                    return self._json(400, {"error": f"unknown channel {name!r}"})
                where = f"#{name}"
            else:
                where = query.get("box", [""])[0]
                if where not in app.boxes:
                    return self._json(400, {"error": f"unknown inbox {where!r}"})
            if path == "/api/thread":
                return self._older(where, query, holders)
            try:
                after = _cursor(query, "after")
            except ValueError as err:
                return self._json(400, {"error": str(err)})
            items, _ = app.thread(where, after=after)
            return self._json(200, {"version": app.thread_version(where, items), "unread": app.unseen_count()})
        if path == "/api/channels":
            return self._json(200, [self._channel_info(ch) for ch in app.channels.all()])
        if m := re.fullmatch(rf"/api/channels/({NAME})(/pending)?", path):
            try:
                channel = app.channels.get(m[1])
            except ChannelError as err:
                return self._json(err.status, {"error": str(err)})
            if m[2] is None:
                return self._json(200, self._channel_info(channel))
            return self._channel_pending(channel.name, query)
        if m := re.fullmatch(rf"/audio/c/({NAME})/({M})", path):
            try:
                return self._file(app.channels.get(m[1]).audio_path(m[2]))
            except ChannelError as err:
                return self._json(err.status, {"error": str(err)})
        if m := re.fullmatch(rf"/files/c/({NAME})/({M})/([A-Za-z0-9._-]+)", path):
            return self._owner_file(f"c/{m[1]}", m[2], m[3])
        if path == "/api/messages":
            return self._json(200, [store.as_json(m) for m in store.all()])
        if path == "/api/controls":
            return self._json(200, [{"type": t, "about": c.about} for t, c in self.app.controls.items()])
        if path == "/api/voices":
            return self._voices()
        if path == "/api/status":
            return self._json(200, app.status.all())
        if m := BANG_OUT.fullmatch(path):
            if box_of_segment(m[1]) is None:
                return self._json(404, {"error": f"no inbox {m[1]!r}"})
            return self._file(app.bang_dir / m[2] / m[3] if app.boxes[box_of_segment(m[1])].messages(
                "id = ? AND json_extract(meta, '$.bang.state') = 'done'", (m[2],)) else None,
                "text/plain; charset=utf-8")
        if m := re.fullmatch(r"/api/agents/([^/]+)/bang", path):
            return self._bang_poll(unquote(m[1]), query)
        if m := re.fullmatch(r"/api/agents/([^/]+)/(answers|waiting)", path):
            return self._agent_get(unquote(m[1]), m[2], query)
        if m := re.fullmatch(r"/api/agents/([^/]+)/thread/([^/]+)", path):
            try:
                return self._json(200, app.thread_for(unquote(m[1]), unquote(m[2])))
            except LookupError as err:
                return self._json(404, {"error": str(err)})
            except PermissionError as err:
                return self._json(403, {"error": str(err)})
            except ValueError as err:
                return self._json(400, {"error": str(err)})
        if path == "/waiting":
            # Only what was asked before the page was drawn is swept.
            now = time.time()
            count = lambda before: store.index.rows(  # noqa: E731
                f"SELECT count(*) FROM msg WHERE {WAITING} AND t < ?", (before,))[0][0]
            return self._html(pages.waiting(store, holders, store.query(WAITING, limit=VIEW_MAX), app.waiting_count(),
                                            [(now - 86400, count(now - 86400)), (now, count(now))]))
        if path == "/unread":
            return self._html(pages.unread(store, holders, store.query(UNSEEN, limit=VIEW_MAX), app.unseen_count()))
        if path == "/search":
            q = query.get("q", [""])[0].strip()
            found, n = store.search(q, VIEW_MAX) if q else ([], 0)
            return self._html(pages.search(store, holders, q, found, n))
        if path == "/export.md":
            return self._export(query)
        if path == "/notifications":
            return self._html(pages.notifications_page(app.hush_view()))
        if path == "/settings":
            return self._html(pages.settings_page(app.settings_view(), app.look_view(), app.board.get(),
                                                    configview.summary(app)))
        if path == "/config":
            return self._html(pages.config_page(configview.build(app)))
        if path == "/prompts":
            return self._html(pages.prompts_page(configview.prompts(app)))
        if path == "/api/config":
            return self._json(200, configview.build(app))
        if path == "/api/config/started":  # the Configuration page waits on this through a restart
            return self._json(200, {"started": app.started})
        if path == "/api/config/voices":
            return self._json(200, configview.voices(app))
        if path == "/api/prompts":
            return self._json(200, configview.prompts(app))
        if path == "/tokens":
            return self._html(pages.tokens_page(app.tokens.all(), app.tokenless(), app.enroll.waiting(),
                                                app.tokens.approver()))
        if path == "/api/enroll":
            if who.kind == "agent" and not self._is_approver(who):
                return self._json(200, [])
            return self._json(200, app.enroll.waiting(new=query.get("new", [""])[0] == "1"))
        if path == "/api/tokens":
            return self._json(200, app.tokens.all(revoked=query.get("revoked", [""])[0] == "1"))
        if path == "/rules":
            try:
                pin = self._pin(query)
            except ValueError as err:
                return self._html(pages.not_found(str(err)), 404)
            return self._html(pages.rules_page(app.rules.all(), sorted(app.known_agents().values(), key=_plain),
                                               app.settings.get()["rules"], pin))
        if path == "/talk":
            mode = app.settings.get()["agent_messages"]
            return self._html(pages.talk_page(app.talk.recent(PAGE) if mode == "shown" else [], mode))
        if path == "/api/settings":
            return self._json(200, app.settings_view())
        if path == "/api/theme":
            return self._json(200, app.look_view())
        if path == "/api/rules":
            name = query.get("name", [""])[0].strip() or (who.name if who.kind == "agent" else "")
            rules = app.rules.for_agent(name) if name else app.rules.all()
            return self._json(200, {"rules": rules, "version": app.rules.version(rules)})
        if path == "/api/talk/pending":
            name = query.get("name", [""])[0].strip()
            if not name:
                return self._json(400, {"error": "say whose messages (?name=)"})
            app.active(name)
            return self._json(200, app.talk.pending(name))
        if path == "/api/board":  # nanotea-tell --board
            return self._json(200, {"sidebar": app.sidebar(), "beacons": app.status.all(), "connected_s": CONNECTED_S,
                                    "now": datetime.now().astimezone().isoformat(timespec="microseconds"),
                                    "arrangement": app.board.get(),
                                    "managers": app.settings.get()["board_managers"]})
        if (m := re.fullmatch(rf"/api/({SEGS})(/pending|/history)?", path)) and box_of_segment(m[1]) is None:
            return self._json(404, {"error": f"no inbox {m[1]!r}; an agent opens one with nanotea-tell --listen"})
        if m := re.fullmatch(rf"/api/({SEGS})", path):
            box = box_of_segment(m[1])
            inbox = app.boxes[box]
            return self._json(200, {**inbox.info(), "listening": app.listening(box),
                                    "waiting": len(inbox.pending()), "url": f"{app.public_url}{CHAT_PATH[box]}"})
        if m := re.fullmatch(rf"/api/({SEGS})/pending", path):
            return self._pending(box_of_segment(m[1]), query)
        if m := re.fullmatch(rf"/api/({SEGS})/history", path):
            return self._history(box_of_segment(m[1]), query)
        if i := self._msg(rf"/m/({M})", path):
            html = pages.message(store, i, holders)
            if is_owner:
                store.mark_seen(i)
            return self._html(html)
        if i := self._msg(rf"/api/messages/({M})", path):
            return self._json(200, store.as_json(store.load(i)))
        if m := re.fullmatch(rf"/api/messages/({M})", path):
            return self._json(404, {"error": f"message {m[1]} no longer exists; the owner may have deleted it"})
        if i := self._msg(rf"/audio/({M})", path):
            name = store.load(i).audio
            if is_owner:
                store.mark_seen(i)
                # Voiced on request, the owner's reading settings as they are now: the owner tapped the speaker.
                try:
                    name = app.worker.speak(i)
                except (ValueError, RuntimeError) as err:
                    log.exception("voicing %s on request failed", i)
                    return self._json(502, {"error": f"couldn't voice it: {err}"})
            return self._file(store.path(i, name) if name else None)
        if i := self._msg(rf"/audio/({M})/reply", path):
            reply = store.reply(i)
            return self._file(store.path(i, reply["audio"]) if reply and reply["audio"] else None)
        if (m := re.fullmatch(rf"/audio/({SEGS})/({M})", path)) and box_of_segment(m[1]):
            return self._file(app.boxes[box_of_segment(m[1])].audio_path(m[2]))
        if m := re.fullmatch(rf"/files/({SEGS}|reply)/({M})/([A-Za-z0-9._-]+)", path):
            return self._owner_file(m[1], m[2], m[3])
        if (m := re.fullmatch(rf"/attachment/({M})/(\d+)", path)) and store.exists(m[1]):
            stored = store.load(m[1]).attachments
            n = int(m[2])
            if not 1 <= n <= len(stored):
                return self._file(None)
            ext = stored[n - 1].rsplit(".", 1)[-1].lower()
            return self._file(store.path(m[1], "attachments") / stored[n - 1], AUDIO_TYPES.get(ext) or files.BY_EXT[ext])
        return self._json(404, {"error": f"not found: {path}"})

    def do_POST(self) -> None:
        self.who = None
        # Read the whole body before answering: replying mid-upload resets the connection, and
        # Safari then shows only "Load failed" instead of the error. A file the owner attaches goes to disk as it
        # arrives, up to [audio] max_upload_mb; anything else is read into memory, up to MAX_BODY.
        if self._refuse_foreign():
            return
        path = urlsplit(self.path).path
        upload = path.endswith("/files")
        limit = self.app.max_upload if upload else MAX_BODY
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= limit:
            self.close_connection = True
            return self._json(413, {"error": f"the request body is empty or over {limit // 2**20} MB"})
        if not upload:
            return self._post(path, self.rfile.read(length))
        fd, name = tempfile.mkstemp(dir=self.app.uploads)
        body = Path(name)
        try:
            with os.fdopen(fd, "wb") as f:
                left = length
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ConnectionError(f"upload to {path} ended after {length - left} of {length} bytes")
                    f.write(chunk)
                    left -= len(chunk)
            self._post(path, body)
        finally:
            body.unlink(missing_ok=True)  # moved into drafts unless the request was refused

    def _post(self, path: str, body: bytes | Path) -> None:
        """body: the request's bytes, or for a file upload, the file it was written to."""
        if ENROLL_OPEN.fullmatch(path):
            return self._enroll_open(path, body)
        try:
            who = self._who({})
            if ENROLL_DECIDE.fullmatch(path):
                self._check_approver(who, path == "/api/enroll/told")
            elif OWNER_POSTS.fullmatch(path):
                if who.kind != "owner":
                    raise AuthError(403, "only the owner can do that; an agent's token can't")
            elif EVENT_POST.fullmatch(path) and who.kind == "events":
                self._bind(who, path, {}, body)
                seg = EVENT_POST.fullmatch(path)[1]
                if seg != segment(who.line):
                    line = "main" if seg == "inbox" else seg.removeprefix("dm-")
                    raise AuthError(403, f"this events token reports to line {who.line!r}, not {line!r}. A source "
                                         f"reporting to {line!r} asks for its own token, naming that line")
            elif who.kind == "events":
                raise AuthError(403, "an events token can only post events to its line")
            elif who.kind == "owner":
                raise AuthError(403, f"{path} is for an agent's token; the owner's key can't act as an agent")
            else:
                self._bind(who, path, {}, body)
                self._check_lines(who, path, {}, body)
                self._check_session(who, path)
        except AuthError as err:
            return self._refuse(err)
        self.who = who
        if path == "/api/messages":
            return self._create(body)
        if path == "/api/settings":
            return self._change_settings(body)
        if path == "/api/config":
            return self._change_config(body)
        if path == "/api/config/secret":
            return self._change_secret(body)
        if path == "/api/prompts":
            return self._change_prompt(body)
        if path == "/api/board":
            return self._arrange(body, None)
        if path == "/api/board/arrange":
            return self._arrange(body, "agent")
        if path == "/api/hush":
            return self._change_hush(body)
        if path == "/api/theme":
            return self._change_theme(body)
        if path == "/api/rules":
            return self._add_rule(body)
        if path == "/api/tokens":
            return self._issue_token(body)
        if m := ENROLL_DECIDE.fullmatch(path):
            return self._decide(m[1], m[2], body)
        if path == "/api/approver":
            return self._appoint(body)
        if m := re.fullmatch(r"/api/tokens/([0-9a-f]{8})/revoke", path):
            try:
                record = self.app.tokens.revoke(m[1])
            except TokenError as err:
                return self._json(err.status, {"error": str(err)})
            log.info("the owner revoked the token %s of %s", m[1], record["name"])
            return self._json(200, record)
        if m := re.fullmatch(r"/api/rules/([0-9a-f]+)/delete", path):
            try:
                self.app.rules.delete(m[1])
            except RuleError as err:
                return self._json(err.status, {"error": str(err)})
            log.info("the owner removed rule %s", m[1])
            return self._json(200, {"ok": True})
        if path == "/api/sessions":
            return self._open_session(body)
        if m := re.fullmatch(r"/api/sessions/([0-9a-f]{1,64})/(beat|close)", path):
            try:
                self.app.beat(m[1]) if m[2] == "beat" else self.app.close_session(m[1])
            except SessionError as err:
                return self._json(err.status, {"error": str(err)})
            return self._json(200, {"ok": True})
        if m := re.fullmatch(rf"/api/agents/([^/]+)/bang/({M})", path):
            return self._bang_report(unquote(m[1]), m[2], body)
        if m := re.fullmatch(r"/api/agents/([^/]+)/held", path):
            return self._held(unquote(m[1]), body)
        if path == "/api/talk":
            return self._tell(body)
        if path == "/api/talk/delivered":
            return self._talk_delivered(body)
        if path == "/api/roster":
            return self._roster(body)
        if path == "/api/status":
            return self._set_status(body)
        if m := re.fullmatch(r"/api/agents/([^/]+)/answers/delivered", path):
            return self._answers_delivered(unquote(m[1]), body)
        if path == "/api/channels":
            return self._new_channel(body)
        if m := re.fullmatch(rf"/api/channels/({NAME})/(delivered|drafts|files|messages|react|typing)", path):
            try:
                channel = self.app.channels.get(m[1])
            except ChannelError as err:
                return self._json(err.status, {"error": str(err)})
            if m[2] == "delivered":
                return self._channel_delivered(channel.name, body)
            if m[2] == "drafts":
                return self._save_draft(body, channel.new_draft)
            if m[2] == "files":
                return self._save_file(channel.drafts, body)
            if m[2] == "react":
                return self._agent_reacts_in_channel(channel.name, body)
            if m[2] == "typing":
                return self._typing(f"#{channel.name}", None, body)
            return self._write_to_channel(channel.name, body)
        if m := re.fullmatch(rf"/api/({SEGS})(?:/(delivered|drafts|files|messages|react|events|typing))?", path):
            box = box_of_segment(m[1])
            if box is None:
                if m[2] is not None or not m[1].startswith("dm-"):
                    return self._json(404, {"error": f"no inbox {m[1]!r}; an agent opens one with nanotea-tell --listen"})
                box = m[1][3:]
                if box == "main":
                    return self._json(400, {"error": f"{box!r} is the main inbox; use --inbox {box}"})
                try:
                    data = json.loads(body)
                    label = data["name"]
                    if not (isinstance(label, str) and label.strip()):
                        raise ValueError("'name' must be a non-empty string")
                    if data.get("open", True):
                        self.app.one_line(box, label.strip())  # refused before the line is made
                except InboxError as err:
                    return self._json(err.status, {"error": str(err)})
                except (KeyError, TypeError, ValueError) as err:
                    return self._json(400, {"error": f"{type(err).__name__}: {err}"})
                self.app.add_dm(box, label.strip())
            if m[2] is None:
                return self._claim(box, body)
            if m[2] == "delivered":
                return self._delivered(box, body)
            if m[2] == "drafts":
                return self._save_draft(body, self.app.boxes[box].new_draft)
            if m[2] == "react":
                return self._agent_reacts(box, body)
            if m[2] == "events":
                return self._event(box, body)
            if m[2] == "typing":
                return self._typing(box, self.app.boxes[box], body)
            if m[2] == "files":
                return self._save_file(self.app.boxes[box].drafts, body)
            return self._write_to(box, body)
        if i := self._msg(rf"/api/messages/({M})/heard", path):
            self.app.store.mark_heard(i)
            return self._json(200, {"ok": True})
        if i := self._msg(rf"/api/messages/({M})/act", path):
            return self._act(i, body)
        if i := self._msg(rf"/api/messages/({M})/react", path):
            return self._owner_reacts(i, body)
        if i := self._msg(rf"/api/messages/({M})/files", path):
            store = self.app.store
            if not store.load(i).ask or store.reply(i) is not None:
                return self._json(409, {"error": "this message is not waiting for a reply"})
            return self._save_file(store.path(i, "drafts"), body)
        if i := self._msg(rf"/api/messages/({M})/drafts", path):
            store = self.app.store
            if not store.load(i).ask or store.reply(i) is not None:
                return self._json(409, {"error": "this message is not waiting for a reply"})
            return self._save_draft(body, partial(store.new_draft, i))
        if i := self._msg(rf"/api/messages/({M})/reply", path):
            return self._reply(i, body)
        if i := self._msg(rf"/api/messages/({M})/delete", path):
            self.app.store.delete(i)
            log.info("the owner deleted %s", i)
            return self._json(200, {"ok": True})
        if path == "/api/push/subscribe":
            return self._subscribe(body)
        if path == "/api/read-all":
            return self._read_all()
        if path == "/api/set-aside":
            return self._set_aside(body)
        return self._json(404, {"error": f"not found: {path}"})

    def _owner_reacts(self, msg_id: str, raw: bytes) -> None:
        """The owner reacts to an agent's message. On a question still waiting, the emoji is the answer;
        otherwise it goes to the inbox of the sender (if it holds one) or the main inbox, as a reaction."""
        try:
            body = json.loads(raw)
            emoji, on = _emoji(body["emoji"]), body.get("on", True)
            if not isinstance(on, bool):
                raise ValueError("'on' must be true or false")
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        app, store = self.app, self.app.store
        msg = store.load(msg_id)
        store.set_reaction(msg_id, emoji, on)
        store.mark_seen(msg_id)
        if not on:
            return self._json(200, {"ok": True, "sent": None})
        if msg.ask and store.reply(msg_id) is None:
            reply = {"at": now_iso(), "text": emoji, "reaction": True, "audio": None, "transcript": None,
                     "transcript_status": "none", "transcript_error": None}
            try:
                store.save_reply(msg_id, reply, None)
            except FileExistsError:
                log.info("reaction %s on %s arrived after it was answered; kept as a reaction", emoji, msg_id)
                return self._json(200, {"ok": True, "sent": None})
            log.info("the owner answered %s with %s", msg_id, emoji)
            return self._json(200, {"ok": True, "sent": "answer"})
        re_ = {"id": msg.id, "sender": msg.sender, "title": msg.title}
        if msg.channel is not None:
            app.channels.get(msg.channel).add(emoji, None, re_, reaction=True)
            log.info("the owner reacted %s to %s in #%s", emoji, msg_id, msg.channel)
            return self._json(200, {"ok": True, "sent": f"#{msg.channel}"})
        box = app.box_of(msg.sender) or "main"
        inbox = app.boxes[box]
        inbox.add(emoji, None, inbox.info()["holder"], re_, reaction=True)
        log.info("the owner reacted %s to %s; sent to the %s inbox", emoji, msg_id, box)
        self._json(200, {"ok": True, "sent": box})

    def _agent_reacts(self, box: str, raw: bytes) -> None:
        """The inbox holder reacts to one of the owner's messages: {name, id, emoji}. The owner gets notified."""
        inbox = self.app.boxes[box]
        try:
            body = json.loads(raw)
            name, msg_id, emoji = body["name"], body["id"], _emoji(body["emoji"])
            inbox.check_holder(name)
            owner_msg = inbox.react(msg_id, name, emoji)
        except InboxError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError, FileNotFoundError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("%s reacted %s to the owner's %s", name, emoji, msg_id)
        self._notify_reaction(CHAT_PATH[box], name, emoji, owner_msg)
        self._json(200, {"ok": True})

    def _agent_reacts_in_channel(self, channel_name: str, raw: bytes) -> None:
        """Any agent reacts to one of the owner's posts in a channel: {name, id, emoji}. The owner gets notified."""
        channel = self.app.channels.get(channel_name)
        try:
            body = json.loads(raw)
            name, msg_id, emoji = body["name"], body["id"], _emoji(body["emoji"])
            if not (isinstance(name, str) and name.strip()):
                raise ValueError("'name' must be a non-empty string")
            owner_msg = channel.react(msg_id, name.strip(), emoji)
        except (KeyError, TypeError, ValueError, FileNotFoundError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("%s reacted %s to the owner's %s in #%s", name, emoji, msg_id, channel_name)
        self._notify_reaction(f"/c/{channel_name}", f"{name.strip()} in #{channel_name}", emoji, owner_msg)
        self._json(200, {"ok": True})

    def _notify_reaction(self, page: str, by: str, emoji: str, owner_msg: dict) -> None:
        note = self.app.reaction_note(page, by, emoji, owner_msg)
        for n in self.app.notify_now:
            try:
                n.send(note)
            except Exception:
                log.exception("notifying the owner of %s's reaction via %s failed", by, type(n).__name__)

    def _save_file(self, drafts_dir: Path, upload: Path) -> None:
        """The owner attaches a picture, PDF or audio file while composing: the body, Content-Type, and ?name=.
        Kept as uploaded, byte for byte."""
        name = parse_qs(urlsplit(self.path).query).get("name", [""])[0].strip()
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if not name:
            return self._json(400, {"error": "the file needs a name (?name=)"})
        try:
            file_id, ctype, size = files.save_draft(drafts_dir, name, ctype, upload)
        except files.Unsupported as err:
            return self._json(415, {"error": str(err)})
        log.info("file %s uploaded (%s, %d bytes, %s)", file_id, ctype, size, name)
        self._json(200, {"file": file_id})

    def _event(self, box: str, raw: bytes) -> None:
        """A program on this machine reports something, e.g. that the owner tapped Done on its page:
        {kind, source, summary, data?}. It reaches the holder labeled as an event, never as his words."""
        try:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as err:
                raise ValueError(f"the body is not JSON ({err}); an event is {{kind, source, summary, data?}}")
            if not isinstance(body, dict):
                raise ValueError("an event is a JSON object: {kind, source, summary, data?}")
            if missing := [k for k in ("kind", "source", "summary") if k not in body]:
                raise ValueError(f"an event needs {', '.join(repr(k) for k in missing)}: {{kind, source, summary, "
                                 f"data?}}")
            kind, source, summary, data = body["kind"], body["source"], body["summary"], body.get("data")
            if not (isinstance(kind, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", kind)):
                raise ValueError("'kind' must be a short lowercase slug, like round-done")
            for name, value, limit in (("source", source, 80), ("summary", summary, 500)):
                if not (isinstance(value, str) and value.strip() and len(value) <= limit):
                    raise ValueError(f"'{name}' must be a non-empty string of at most {limit} characters")
        except ValueError as err:
            return self._json(400, {"error": str(err)})
        inbox = self.app.boxes[box]
        event = {"kind": kind, "source": source.strip(), "summary": summary.strip(), "data": data}
        msg = inbox.add(None, None, inbox.info()["holder"], None, event=event)
        log.info("event %s from %s for the %s inbox: %s", kind, event["source"], box, event["summary"])
        self._json(202, {"id": msg["id"]})

    def _pin(self, query: dict) -> dict | None:
        """?pin=<id>&where=<segment>: one of the owner's messages to make a rule of, as {text, for, from}: for its line's
        holder, or everyone for a channel."""
        if "pin" not in query:
            return None
        msg_id, seg = query["pin"][0], query.get("where", [""])[0]
        if seg.startswith("c/"):
            try:
                folder, holder = self.app.channels.get(seg[2:]), None
            except ChannelError as err:
                raise ValueError(str(err))
        elif (box := box_of_segment(seg)) is not None:
            folder = self.app.boxes[box]
            holder = folder.info()["holder"]
        else:
            raise ValueError(f"no line {seg!r}")
        found = folder.messages("id = ?", (msg_id,))
        if not found:
            raise ValueError(f"no message {msg_id!r} there")
        text = found[0]["text"] or found[0]["transcript"]
        if not text:
            raise ValueError("that message has no words yet")
        return {"text": text, "for": holder, "from": msg_id}

    def _thread_page(self, where: str, query: dict, holders: dict) -> None:
        """A conversation ("app") or channel ("#general") page: its newest PAGE items, or with ?after=, all from
        that cursor on; ?part=1, only what a refresh patches. Opening it as the owner marks every message in it
        seen."""
        app, store = self.app, self.app.store
        try:
            after = _cursor(query, "after")
        except ValueError as err:
            return self._html(pages.not_found(str(err)), 400)
        part = query.get("part", [""])[0] == "1"
        unseen = None
        if self.who.kind == "owner":
            unseen = set(app.thread_unseen(where))
            for msg_id in unseen:
                store.mark_seen(msg_id)
        items, more = app.thread(where, after=after)
        version = app.thread_version(where, items)
        shown = [(k, m) for k, m, _, _ in items]
        oldest = items[0][3] if items else ""
        if where.startswith("#"):
            name = where[1:]
            html = pages.channel(store, app.channels.get(name), app.channel_listening(name), shown, oldest, more,
                                 version, holders, app.status.all(), app.typing_in(where), unseen, part,
                                 app.folds(where))
        else:
            holder = app.boxes[where].info()["holder"]
            html = pages.chat(store, where, app.boxes[where].info(), app.presence(where)[1],
                              app.voices.voice_of(holder) if holder else None, shown, oldest, more, version, holders,
                              app.status.all().get(holder) if holder else None,
                              [a for a in app.typing_in(where) if a == holder], unseen, part, app.folds(where))
        self._html(html)

    def _thread_view(self, key: str, query: dict, holders: dict) -> None:
        """One thread's page, as _thread_page is a conversation's: its newest PAGE items, ?after=, ?part=1."""
        app, store = self.app, self.app.store
        where = f"~{key}"
        try:
            after = _cursor(query, "after")
            root = app.thread_root(key)
        except ValueError as err:
            return self._html(pages.not_found(str(err)), 400)
        part = query.get("part", [""])[0] == "1"
        unseen = None
        if self.who.kind == "owner":
            unseen = set(app.thread_unseen(where))
            for msg_id in unseen:
                store.mark_seen(msg_id)
        items, more = app.thread(where, after=after)
        if root is None and not items:
            return self._html(pages.not_found(f"No thread {key}: nothing in it is left."), 404)
        # The owner's replies answer the first message, or once it is gone the newest; they go where the thread is.
        target = root or items[-1][:2]
        re_id = target[1].id if target[0] == "agent" else target[1]["id"]
        if target[0] == "agent" and target[1].ask and store.reply(re_id) is None and target[1].channel is not None:
            re_id = None  # a question in a channel is answered on its own page before anyone replies
        re_, channel = app.reference(target[1].id if target[0] == "agent" else target[1]["id"])
        if channel is not None:
            place = f"#{channel}"
        elif re_["kind"] == "agent":
            place = pages.reply_box(target[1], holders)
        else:
            folder = target[1]["folder"]
            place = next(box for box, inbox in list(app.boxes.items()) if inbox.folder == folder)
        version = app.thread_version(where, items)
        oldest = items[0][3] if items else ""
        self._html(pages.thread_page(store, key, root, place, re_id, [(k, m) for k, m, _, _ in items], oldest, more,
                                     version, holders, unseen, part))

    def _older(self, where: str, query: dict, holders: dict) -> None:
        """A thread's items older than ?before=: the PAGE before it, or with ?until=<row>, every one back to
        that row. {html, before (the oldest's cursor), more, found (with until: whether the row is there)}."""
        app = self.app
        try:
            before = _cursor(query, "before")
            if before is None:
                raise ValueError("say ?before=<cursor>")
            until = query.get("until", [None])[0]
            if where.startswith("~"):
                folders = app.store.index.owner_folders(until[2:]) if until and until.startswith("g-") else [None]
                if len(folders) != 1:
                    raise ValueError(f"no single message {until!r} in this thread")
                folder = folders[0]
            else:
                folder = f"channel-{where[1:]}" if where.startswith("#") else app.boxes[where].folder
            stop = app.store.index.position(until, folder) if until else None
        except ValueError as err:
            return self._json(400, {"error": str(err)})
        if until and (stop is None or parse_cursor(stop) >= parse_cursor(before)):
            # Gone, or already on the page.
            return self._json(200, {"html": "", "before": before, "more": True, "found": stop is not None})
        if stop:
            items, _ = app.thread(where, before=before, after=stop)
            more = app.older_exists(where, items[0][3]) if items else app.older_exists(where, before)
        else:
            items, more = app.thread(where, before=before)
        html = pages.thread_rows(app.store, [(k, m) for k, m, _, _ in items], holders,
                                 root=where[1:] if where.startswith("~") else None, nested=app.folds(where))
        self._json(200, {"html": html, "before": items[0][3] if items else before, "more": more,
                         "found": True if until else None})

    def _list(self, path: str, query: dict, holders: dict) -> None:
        """Messages (/), newest first a page at a time; ?from=<sender>: one sender's direct messages, which the
        owner opening it sees, as they do a conversation's. /api/list[?from=]&before=<cursor>: the next page, as
        {html, before, more, url (the next page's)}."""
        store = self.app.store
        try:
            before = None
            if path == "/api/list":
                before = _cursor(query, "before")
                if before is None:
                    raise ValueError("say ?before=<cursor>")
            sender = query.get("from", [None])[0]
            if sender is not None and not sender.strip():
                raise ValueError("?from= is a sender")
        except ValueError as err:
            if path == "/api/list":
                return self._json(400, {"error": str(err)})
            return self._html(pages.not_found(str(err)), 400)
        where, params = "1", ()
        unseen = None
        if sender is not None:
            where, params = "channel IS NULL AND sender = ?", (sender,)
            if path == "/" and self.who.kind == "owner":  # a paired browser opening it; later pages show what it marked
                unseen = {i for (i,) in store.index.rows(f"SELECT id FROM msg WHERE {where} AND {UNSEEN}", params)}
                for msg_id in unseen:
                    store.mark_seen(msg_id)
        items, more = store.index.thread((where, params), None, before=before, limit=PAGE, newest_first=True)
        msgs = [Message(**m) for _, m, _, _ in items]
        last = items[-1][3] if items else before
        more_url = ("/api/list?" + (f"from={quote(sender)}&" if sender is not None else "")
                    + f"before={quote(last)}") if more else ""
        if path == "/api/list":
            return self._json(200, {"html": pages.list_rows(store, msgs, holders), "before": last, "more": more,
                                    "url": more_url})
        n, fresh = store.index.rows(f"SELECT count(*), coalesce(sum({UNSEEN}), 0) FROM msg WHERE {where}", params)[0]
        self._html(pages.inbox(store, holders, msgs, n, len(unseen) if unseen is not None else fresh, more_url,
                               sender, unseen))

    def _export(self, query: dict) -> None:
        """Agents' messages as markdown, oldest first: all, or those from ?from=<agent>, in ?channel=<name>, or
        matching ?q=<words>."""
        store = self.app.store
        sender, channel, q = (query.get(k, [""])[0].strip() for k in ("from", "channel", "q"))
        where, params, parts = ["1"], [], []
        if sender:
            where.append("sender = ? COLLATE NOCASE")
            params.append(sender)
            parts.append(f"from {pages.who(sender)}")
        if channel:
            where.append("channel = ?")
            params.append(channel)
            parts.append(f"in #{channel}")
        cond = " AND ".join(where)
        msgs = store.search(q, None, cond, tuple(params))[0] if q else store.query(cond, tuple(params))
        if q:
            parts.append(f"matching {q}")
        heading = f"{pages._app_name()} messages" + (f" {', '.join(parts)}" if parts else "")
        text = export.markdown_of(store, msgs[::-1], heading, pages.who)
        log.info("the owner exported %d messages", len(msgs))
        self._send(200, "text/markdown; charset=utf-8", text.encode(),
                   [("Content-Disposition", 'attachment; filename="nanotea-export.md"')])

    def _read_all(self) -> None:
        """The owner marks every message they haven't seen as seen."""
        store = self.app.store
        unseen = store.query(UNSEEN)
        for msg in unseen:
            store.mark_seen(msg.id)
        log.info("the owner marked %d messages read", len(unseen))
        self._json(200, {"marked": len(unseen)})

    def _set_aside(self, raw: bytes) -> None:
        """{before}: the owner sets aside every question still waiting that was asked before then (seconds since
        the epoch). Each asker is told; each question stays answerable."""
        try:
            before = json.loads(raw)["before"]
            if not isinstance(before, (int, float)) or isinstance(before, bool):
                raise ValueError("'before' must be seconds since the epoch")
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        store = self.app.store
        ids = [i for (i,) in store.index.rows(f"SELECT id FROM msg WHERE {WAITING} AND t < ?", (before,))]
        for i in ids:
            store.mark_set_aside(i)
        log.info("the owner set aside %d questions asked before %s", len(ids),
                 datetime.fromtimestamp(before).isoformat(timespec="seconds"))
        self._json(200, {"set_aside": len(ids)})

    def _subscribe(self, raw: bytes) -> None:
        """A browser turned on notifications. Saves it and sends it a first notification to prove it."""
        if not self.app.push_on:
            return self._json(409, {"error": "notifications are off: the service's config has no \"push\" in "
                                             "[notify] now or escalate"})
        try:
            got = json.loads(raw)
            endpoint, keys = got["endpoint"], got["keys"]
            if not (isinstance(endpoint, str) and endpoint.startswith("https://")
                    and isinstance(keys.get("p256dh"), str) and isinstance(keys.get("auth"), str)):
                raise ValueError("not a push subscription")
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        sub = {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}
        self.app.push.add(sub)
        log.info("push subscription added: %s", endpoint[:60])
        try:
            self.app.push.send(sub, {"title": "Notifications are on", "body": "New messages will show up here.",
                                     "url": "/", "tag": "welcome", "unseen": self.app.unseen_count()})
        except Exception as err:
            log.exception("first push to %s failed", endpoint[:60])
            return self._json(502, {"error": f"saved, but the first notification failed: {type(err).__name__}: {err}"})
        self._json(200, {"ok": True})

    def _create(self, raw: bytes) -> None:
        try:
            body = json.loads(raw)
            text, sender = body["text"], body["from"]
            title, ask, voice = body.get("title"), body.get("ask", False), body.get("voice")
            raw = body.get("raw", False)
            if not (isinstance(text, str) and text.strip() and isinstance(sender, str) and sender.strip()):
                raise ValueError("'text' and 'from' must be non-empty strings")
            if title is not None and not isinstance(title, str):
                raise ValueError("'title' must be a string")
            if not isinstance(ask, bool):
                raise ValueError("'ask' must be true or false")
            if not isinstance(raw, bool):
                raise ValueError("'raw' must be true or false")
            if raw and not ask:
                raise ValueError("'raw' asks for the answer to be recorded raw; it goes with 'ask'")
            if (channel := body.get("channel")) is not None:
                if not isinstance(channel, str):
                    raise ValueError("'channel' must be a channel name")
                self.app.channels.get(channel)  # ChannelError if there is none
            re_ = None
            if (re_id := body.get("re")) is not None:
                # A reply goes in its thread, where the thread is.
                if not self.app.answerable_by(sender.strip(), re_id):
                    raise ValueError(f"no message {re_id!r} to reply to")  # as for an id that doesn't exist
                re_, place = self.app.reference(re_id)
                if channel is not None and channel != place:
                    raise ValueError(f"{re_id} is {'in #' + place if place else 'on a direct line'}, and a reply goes "
                                     f"where its thread is: leave out channel")
                channel = place
            files = [_attachment(a) for a in body.get("attachments", [])]
            check_markers(text, [name for name, _ in files])
            control = self._check_control(body.get("control"))
            if voice is not None and voice not in {v["id"] for v in self.app.tts.voices()}:
                raise ValueError(f"unknown voice {voice!r}; list voices with nanotea-tell --voices")
            voice = self.app.voices.resolve(sender.strip(), voice)
        except (VoiceError, ChannelError) as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        except RuntimeError as err:
            return self._json(502, {"error": str(err)})
        msg = self.app.store.create(text, sender.strip(), title, ask, voice, files, channel, raw, control, re_)
        self.app.active(msg.sender)
        if channel is not None:
            self.app.typing_done(f"#{channel}", msg.sender)
        elif box := self.app.box_of(msg.sender):
            self.app.typing_done(box, msg.sender)
        self.app.worker.submit(msg.id)
        log.info("queued %s from %s%s", msg.id, msg.sender, f" in #{channel}" if channel else "")
        self._json(202, {"id": msg.id, "url": f"{self.app.public_url}/m/{msg.id}"})

    def _check_control(self, spec) -> dict | None:
        """An agent's control spec, {"type": <control>, ...}, as its plugin checks it; None for none."""
        if spec is None:
            return None
        if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
            raise ValueError("'control' must be an object with a 'type'")
        plugin = self.app.controls.get(spec["type"])
        if plugin is None:
            raise ValueError(f"no control {spec['type']!r}; controls: {', '.join(self.app.controls) or 'none'}")
        checked = plugin.check(spec)
        if not (isinstance(checked, dict) and checked.get("type") == spec["type"]):
            raise RuntimeError(f"control {spec['type']!r} returned a bad spec from check")
        return checked

    def _act(self, msg_id: str, raw: bytes) -> None:
        """The owner taps a message's control: {act, value}. The plugin says what changed and what the agent is
        told. On a question still waiting, that is the answer; otherwise it goes where a reaction would."""
        app, store = self.app, self.app.store
        try:
            body = json.loads(raw)
            act, value = body["act"], body.get("value")
            if not (isinstance(act, str) and act):
                raise ValueError("'act' must be a non-empty string")
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        msg = store.load(msg_id)
        if msg.control is None:
            return self._json(409, {"error": "this message has no control"})
        kind = msg.control["type"]
        plugin = app.controls.get(kind)
        if plugin is None:
            return self._json(409, {"error": f"the {kind} control is not installed"})
        with app.control_lock:
            if msg.ask and store.reply(msg_id) is not None:
                return self._json(409, {"error": "this question is already answered"})
            try:
                done = plugin.act(msg.control, store.control_state(msg_id), act, value)
            except ValueError as err:
                return self._json(400, {"error": str(err)})
            except Exception as err:
                log.exception("control %s on %s failed", kind, msg_id)
                return self._json(500, {"error": f"the {kind} control failed: {type(err).__name__}: {err}"})
            if problem := _act_problem(done):
                log.error("control %s on %s: %s", kind, msg_id, problem)
                return self._json(500, {"error": f"the {kind} control is broken: {problem}"})
            sent = None
            if done.says is not None:
                tap = {"control": kind, "act": act, "value": value, "data": done.data}
                try:
                    sent = self._tapped(msg, done.says.strip(), tap)
                except FileExistsError:  # a typed answer or a reaction got there first
                    return self._json(409, {"error": "this question is already answered"})
            store.set_control_state(msg_id, done.state)
            store.mark_seen(msg_id)
        log.info("the owner tapped %s on %s's %s control: %s", act, msg_id, kind, sent or "kept")
        msg = store.load(msg_id)
        self._json(200, {"html": pages.control(store, msg), "sent": sent, "answered": sent == "answer"})

    def _tapped(self, msg: Message, says: str, tap: dict) -> str:
        """Where a tap's words go: the answer to a question still waiting, else the post's channel, else the
        sender's line (or the main inbox), as a reaction would."""
        app, store = self.app, self.app.store
        if msg.ask:
            store.save_reply(msg.id, {"at": now_iso(), "text": says, "tap": tap, "audio": None, "transcript": None,
                                      "transcript_status": "none", "transcript_error": None}, None)
            return "answer"
        re_ = {"id": msg.id, "sender": msg.sender, "title": msg.title}
        if msg.channel is not None:
            app.channels.get(msg.channel).add(says, None, re_, tap=tap)
            return f"#{msg.channel}"
        box = app.box_of(msg.sender) or "main"
        inbox = app.boxes[box]
        inbox.add(says, None, inbox.info()["holder"], re_, tap=tap)
        return box

    def _change_prompt(self, raw: bytes) -> None:
        """The owner changes a prompt: {name, text} keeps their text, {name, reset: true} goes back to the built-in."""
        try:
            got = json.loads(raw)
            name = got["name"]
            if got.get("reset") is True:
                self.app.prompts.reset(name)
                log.info("the owner went back to the built-in %s prompt", name)
            else:
                self.app.prompts.change(name, got["text"])
                log.info("the owner changed the %s prompt: %d characters", name, len(got["text"]))
        except (KeyError, TypeError, ValueError) as err:  # PromptError and bad JSON are ValueErrors
            return self._json(400, {"error": f"{type(err).__name__}: {err}" if not isinstance(err, PromptError)
                                    else str(err)})
        self._json(200, configview.prompts(self.app))

    def _change_settings(self, raw: bytes) -> None:
        """The owner changes settings: {key: value, ...}, only those changing."""
        try:
            patch = json.loads(raw)
            # Even while on: a request that saw it on could otherwise land just after the owner turns it off.
            if isinstance(patch, dict) and patch.get("bang") is True and not self.who.host:
                return self._json(403, {"error": "bang commands are turned on from the machine nanotea runs on: "
                                                 "nanotea bang on. The pairing key alone can't, so a leaked link "
                                                 "can't run commands. Turning them off works anywhere."})
            if isinstance(patch, dict) and patch.get("config_programs") is True and not self.who.host:
                return self._json(403, {"error": "Programs from this app is turned on from the machine nanotea "
                                                 "runs on: nanotea config programs on. The pairing key alone can't, "
                                                 "so a leaked link can't change what the service runs. Turning it "
                                                 "off works anywhere."})
            was = self.app.settings.get()["bang"]
            self.app.settings.change(patch)
        except (TypeError, ValueError) as err:  # SettingsError is a ValueError
            return self._json(400, {"error": str(err) if isinstance(err, SettingsError) else
                                    f"{type(err).__name__}: {err}"})
        via = "the host key" if self.who.host else "the pairing key"
        if (now := self.app.settings.get()["bang"]) != was:
            self.app.bang_audit("switched on" if now else "switched off", via=via)
        if "config_programs" in patch:
            log.warning("the owner turned Programs from this app %s, with %s",
                        "on" if patch["config_programs"] else "off", via)
        log.info("the owner changed settings, with %s: %s", via, json.dumps(patch))
        self._json(200, self.app.settings_view())

    def _may_change_programs(self) -> bool:
        return self.who.host or self.app.settings.get()["config_programs"]

    def _change_config(self, raw: bytes) -> None:
        """The owner changes the config file: {changes: [{table, key, value} or {table, key, unset: true}]}. Checked
        as the service starts, written in place, and then the service restarts with it."""
        app = self.app
        path = app.cfg.path
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise configedit.EditError("send {changes: [...]}")
            changes = configedit.parse_changes(body.get("changes"))
            with app.config_lock:
                old = path.read_text()
                new, before, after = configedit.edit(old, changes)
                if after == before:
                    raise configedit.EditError("nothing to change: the file already says that")
                if (machine := configedit.machine_changes(before, after)) and not self._may_change_programs():
                    return self._json(403, {"error": f"{', '.join(machine)}: the programs the service runs, the code "
                                                     "it loads, and where it keeps its data and secrets change from "
                                                     "this app only while Programs from this app is on. Turn it on "
                                                     "on the machine nanotea runs on: nanotea config programs on. "
                                                     f"Or change it in {path}."})
                if error := configedit.verify(new, None):
                    return self._json(400, {"error": configview.scrub(error, app.secret_values)})
                if (before.get("host"), before.get("port")) != (after.get("host"), after.get("port")):
                    _can_listen(after.get("host", "127.0.0.1"), after.get("port", 7447),
                                (app.cfg["host"], app.cfg["port"]))
                kept = configedit.save(path, old, new, app.store.root / "config-history")
        except (configedit.EditError, ValueError, TypeError) as err:  # bad JSON is a ValueError
            return self._json(400, {"error": str(err) if isinstance(err, ValueError) else
                                    f"{type(err).__name__}: {err}"})
        except OSError as err:  # a read-only mount, as under Docker
            log.error("the owner's config change was not saved: %s", err)
            return self._json(409, {"error": f"the service can't change {err.filename or path}: {err.strerror}. "
                                             "Change it on the machine nanotea runs on"})
        names = [configedit.where(t, k) for t, k, _, _ in changes]
        log.warning("the owner changed the config, with %s: %s; the file as it was is %s. Restarting",
                    "the host key" if self.who.host else "the pairing key", ", ".join(names), kept)
        self._json(200, {"changed": names, "file": str(path), "kept": str(kept), "restarting": True})
        app.restart()

    def _change_secret(self, raw: bytes) -> None:
        """The owner sets or removes a secret in env_file: {name, value} or {name, unset: true}. It is checked with
        the config as the service starts, written, and the service restarts; its value is never shown."""
        app = self.app
        try:
            body = json.loads(raw)
            if not (isinstance(body, dict) and isinstance(body.get("name"), str)):
                raise configedit.EditError("send {name, value}, or {name, unset: true}")
            name = body["name"]
            unset = body.get("unset") is True
            v = None if unset else body.get("value")
            if not unset and not (isinstance(v, str) and v):
                raise configedit.EditError(f"{name}: send its value, or unset: true")
            known = configedit.secret_names()
            if name not in known:
                raise configedit.EditError(f"no plugin reads a secret named {name}; secrets: {', '.join(known)}")
            if app.env_path is None:
                raise configedit.EditError("the config names no env_file to keep secrets in. Set env_file (a "
                                           "Programs from this app setting), or add it in the config file")
            with app.config_lock:
                old = app.env_path.read_text()
                new = configedit.edit_env(old, name, v)
                env = parse_env(new, str(app.env_path))
                if error := configedit.verify(app.cfg.path.read_text(), env):
                    hide = sorted({x for x in [*app.secret_values, v or ""] if len(x) >= 6}, key=len, reverse=True)
                    return self._json(400, {"error": configview.scrub(error, hide)})
                configedit._write(app.env_path, new)
        except (configedit.EditError, ValueError) as err:
            return self._json(400, {"error": str(err)})
        except OSError as err:
            log.error("the owner's secret %s was not saved: %s", name, err)
            return self._json(409, {"error": f"the service can't change {err.filename or app.env_path}: "
                                             f"{err.strerror}. Change it on the machine nanotea runs on"})
        log.warning("the owner %s the secret %s in %s. Restarting", "removed" if unset else "set", name, app.env_path)
        self._json(200, {"name": name, "set": not unset, "restarting": True})
        app.restart()

    def _arrange(self, raw: bytes, by: str | None) -> None:
        """Arranges the board: {groups?, hidden?}, each replacing what is there. by: None for the owner; for an
        agent, its token's name, which must be a board manager's. A name in the body is bound to the token."""
        try:
            patch = json.loads(raw)
            if not isinstance(patch, dict):
                raise BoardError("send an object: {groups?, hidden?}")
            who = "the owner"
            if by is not None:
                patch.pop("name", None)
                name = self.who.name
                if not self.app.settings.is_manager(name):
                    managers = self.app.settings.get()["board_managers"]
                    return self._json(403, {"error": f"{name.strip()} is not a board manager; the owner names them "
                                                     f"in Settings, under Board (now: {', '.join(managers) or 'none'})"})
                who = name.strip()
            out = self.app.board.change(patch, who)
        except (BoardError, json.JSONDecodeError) as err:
            return self._json(400, {"error": str(err)})
        self._json(200, out)

    def _leaf(self, name: str, query: dict) -> None:
        """A name's leaf as an SVG, drawn in currentColor with its veins cut through; public, as the name is all it
        is made from. ?size= one of LEAF_SIZES (default 64): the size it is drawn for, which sets how fine its
        veins go. ?unfurl= 0 (a bud) to 1 (open, the default)."""
        try:
            size = int(query.get("size", ["64"])[0])
            if size not in LEAF_SIZES:
                raise ValueError(f"size must be one of {', '.join(map(str, LEAF_SIZES))}")
            unfurl = float(query.get("unfurl", ["1"])[0])
            body = leaf.svg(name, unfurl=unfurl, size=size)
        except ValueError as err:
            return self._json(400, {"error": str(err)})
        self._send(200, "image/svg+xml", body.encode(), [("Cache-Control", "public, max-age=604800")])

    def _change_theme(self, raw: bytes) -> None:
        """The owner picks how the app looks: {theme, appearance}, either or both."""
        try:
            patch = json.loads(raw)
            self.app.look.change(patch)
        except (TypeError, ValueError) as err:  # ThemeError is a ValueError
            return self._json(400, {"error": str(err) if isinstance(err, ThemeError) else
                                    f"{type(err).__name__}: {err}"})
        log.info("the owner changed the theme: %s", json.dumps(patch))
        self._json(200, self.app.look_view())

    def _change_hush(self, raw: bytes) -> None:
        """The owner changes what notifies them: {muted, questions_only, quiet}, only those changing."""
        try:
            patch = json.loads(raw)
            self.app.hush.change(patch)
        except (TypeError, ValueError) as err:  # HushError is a ValueError
            return self._json(400, {"error": str(err) if isinstance(err, HushError) else
                                    f"{type(err).__name__}: {err}"})
        log.info("the owner changed notifications: %s", json.dumps(patch))
        self._json(200, self.app.hush_view())

    def _add_rule(self, raw: bytes) -> None:
        """The owner adds a standing rule: {text, for (an agent, or null for every agent), from (a message id)}."""
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("send {text, for, from}")
            rule = self.app.rules.add(body.get("text"), body.get("for"), body.get("from"))
        except RuleError as err:
            return self._json(err.status, {"error": str(err)})
        except (TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("the owner added rule %s for %s: %s", rule["id"], rule["for"] or "every agent", rule["text"])
        self._json(200, rule)

    def _issue_token(self, raw: bytes) -> None:
        """The owner issues a token: {kind, name, line?, replace?}. The secret is in this response and nowhere
        else."""
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("send {kind, name, line?, replace?}")
            if not isinstance(body.get("replace", False), bool):
                raise ValueError("'replace' must be true or false")
            line, name = body.get("line"), body.get("name")
            if body.get("kind", "agent") == "agent" and line is not None:
                # Bound, a token uses no other line, so neither line may be left held by someone who can't release it.
                if line in self.app.boxes and (holder := self.app.boxes[line].info()["holder"]) not in (None, name):
                    raise TokenError(409, f"line {line!r} is held by {holder!r}; binding it to {name!r} would leave "
                                          f"it stuck. {holder!r} must release it first")
                if held := sorted(b for b, inbox in list(self.app.boxes.items())
                                  if b != line and inbox.info()["holder"] == name):
                    raise TokenError(409, f"{name!r} holds {', '.join(held)}; bound to {line!r} it couldn't release "
                                          f"{'them' if len(held) > 1 else 'it'}. Release first")
            token, record, gone = self.app.tokens.issue(body.get("kind", "agent"), body.get("name"), body.get("line"),
                                                        body.get("replace", False))
        except TokenError as err:
            return self._json(err.status, {"error": str(err)})
        except (TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("the owner issued token %s: %s for %s%s", record["id"], record["kind"], record["name"],
                 f", revoking {', '.join(gone)}" if gone else "")
        self._json(200, {"token": token, "record": record, "revoked": gone})

    def _from_here(self) -> str | None:
        """Why a request for a token is refused: it must come from a program on this machine (or, under Docker, on
        its host, through the compose gateway), never through a proxy."""
        addr = self.client_address[0]
        try:
            loopback = ipaddress.ip_address(addr).is_loopback
        except ValueError:
            loopback = False
        if not loopback and addr not in self.app.proxies:
            return f"a token is asked for from the machine nanotea runs on, not from {addr}"
        if sent := [h for h in FORWARDED if h in self.headers]:
            return f"a token is asked for from the machine nanotea runs on, not through a proxy ({', '.join(sent)})"
        return None

    def _enroll_open(self, path: str, raw) -> None:
        """A program without a token: asks for one, {kind, name, line?, dir?}, and is answered {request, secret,
        approver}; or collects it, {secret}, and is answered {request, token} once approved."""
        if why := self._from_here():
            return self._json(403, {"error": why})
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("send a JSON object")
            if path == "/api/enroll":
                request, secret = self.app.enroll.ask(body.get("kind", "agent"), body.get("name"), body.get("line"),
                                                      body.get("dir"), self.client_address[0])
            else:
                request, token = self.app.enroll.collect(path.split("/")[3], body.get("secret"))
        except TokenError as err:
            return self._json(err.status, {"error": str(err)})
        except (TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        approver = self.app.tokens.approver()
        if path == "/api/enroll":
            log.info("%s %r asks for a token: request %s, from %s, dir %s", request["kind"], request["name"],
                     request["id"], request["from"], request["dir"])
            if approver is None:
                self.app.notify_enroll(request)
            return self._json(200, {"request": request, "secret": secret,
                                    "approver": approver["name"] if approver else None})
        if token is not None:
            log.info("%r collected token %s (request %s)", request["name"], request["token"], request["id"])
            return self._json(200, {"request": request, "token": token})
        return self._json(200, {"request": request, "token": None,
                                "approver": approver["name"] if approver else None})

    def _is_approver(self, who: Who) -> bool:
        approver = self.app.tokens.approver()
        return who.kind == "agent" and approver is not None and approver["id"] == who.token

    def _check_approver(self, who: Who, told: bool) -> None:
        """The owner and the approver decide requests; only the approver is told of them."""
        if who.kind == "owner" and not told:
            return
        if not self._is_approver(who):
            raise AuthError(403, "only the approver does that; the owner chooses it in the app, under Tokens")

    def _decide(self, ident: str | None, verdict: str | None, raw: bytes) -> None:
        """approve or deny {reason?}; or told {ids}, from the approver: it has been given these."""
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("send a JSON object")
            if ident is None:
                ids = body.get("ids")
                if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                    raise ValueError("send {ids}: the requests given to the approver")
                self.app.enroll.told(ids)
                return self._json(200, {"ok": True})
            by = "owner" if self.who.kind == "owner" else self.who.name
            request = self.app.enroll.decide(ident, verdict == "approve", by, body.get("reason"))
        except TokenError as err:
            return self._json(err.status, {"error": str(err)})
        except (TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("%s %s request %s: %s %r%s", by, "approved" if verdict == "approve" else "denied", ident,
                 request["kind"], request["name"], f" ({request['reason']})" if request["reason"] else "")
        self._json(200, request)

    def _appoint(self, raw: bytes) -> None:
        """The owner chooses the approver: {token: an agent's token id, or null for none}."""
        try:
            body = json.loads(raw)
            if not (isinstance(body, dict) and "token" in body):
                raise ValueError("send {token}: an agent's token id, or null for no approver")
            approver = self.app.tokens.appoint(body["token"])
        except TokenError as err:
            return self._json(err.status, {"error": str(err)})
        except (TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        if approver:
            self.app.enroll.untell()
        log.info("the owner made %s the approver", f"{approver['name']!r} (token {approver['id']})" if approver
                 else "no one")
        self._json(200, {"approver": approver})

    def _open_session(self, raw: bytes) -> None:
        """An MCP server's session starts, or registers again: {name, session, pid, line, bang, channels}. bang: it
        was started with the local opt-in to run the owner's bang commands. channels: those it listens in."""
        try:
            body = json.loads(raw)
            name, sid, pid, line = body["name"], body["session"], body["pid"], body["line"]
            if not (isinstance(name, str) and name.strip()):
                raise ValueError("'name' must be a non-empty string")
            if not (isinstance(sid, str) and re.fullmatch(r"[0-9a-f]{1,64}", sid)):
                raise ValueError("'session' must be a hex id")
            if type(pid) is not int or pid <= 0:
                raise ValueError("'pid' must be a process id")
            if not isinstance(line, str):
                raise ValueError("'line' must be a line name")
            opted = body.get("bang", False)
            if not isinstance(opted, bool):
                raise ValueError("'bang' must be true or false")
            channels = body.get("channels", [])
            if not (isinstance(channels, list) and all(isinstance(c, str) for c in channels)):
                raise ValueError("'channels' must be a list of channel names")
            session = self.app.open_session(name.strip(), sid, pid, line, opted, channels)
        except SessionError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        self.app.active(name.strip())
        self._json(200, {"session": sid, "since": session["since"]})

    def _bang_poll(self, agent: str, query: dict) -> None:
        """A session asks for its next command: ?session=<id>&wait=<seconds, at most 60>. {job: ...} or {job: null}."""
        try:
            sid = query.get("session", [""])[0]
            if not re.fullmatch(r"[0-9a-f]{1,64}", sid):
                raise ValueError("say which session (?session=)")
            wait = float(query.get("wait", ["0"])[0])
            if not 0 <= wait <= 60:
                raise ValueError("wait must be 0 to 60 seconds")
            job = self.app.bang_take(agent, sid, wait)
        except SessionError as err:
            return self._json(err.status, {"error": str(err)})
        except ValueError as err:
            return self._json(400, {"error": f"ValueError: {err}"})
        self._json(200, {"job": job})

    def _bang_report(self, agent: str, msg_id: str, raw: bytes) -> None:
        """The session's result for a command: see App.bang_report."""
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("send an object")
            self.app.bang_report(agent, msg_id, body)
        except BangError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        self._json(200, {"ok": True})

    def _held(self, agent: str, raw: bytes) -> None:
        """A harness hook: {what} when the agent is stopped at a permission prompt, {clear: true} when it isn't."""
        try:
            body = json.loads(raw)
            if body.get("clear") is True:
                self.app.active(agent)
                return self._json(200, {"held": None})
            what = body.get("what")
            if what is not None and not isinstance(what, str):
                raise ValueError("'what' must be a string")
        except (TypeError, ValueError, AttributeError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        if not self.app.settings.get()["held"]:
            return self._json(200, {"held": None, "note": "permission prompts are off in Settings"})
        self.app.hold(agent, what.strip()[:300] if what and what.strip() else None)
        self._json(200, {"held": self.app.held_of(agent)})

    def _tell(self, raw: bytes) -> None:
        """One agent writes to another: {from, to, text}."""
        app = self.app
        mode = app.settings.get()["agent_messages"]
        if mode == "off":
            return self._json(403, {"error": f"agent to agent messages are off; {app.app_cfg['owner']} can turn "
                                             f"them on in Settings"})
        try:
            body = json.loads(raw)
            sender, to, text = body["from"], body["to"], body["text"]
            if not (isinstance(sender, str) and sender.strip() and isinstance(to, str) and to.strip()):
                raise ValueError("'from' and 'to' must be agents' names")
            known = app.known_agents()
            if _plain(to.strip()) not in known:
                raise TalkError(404, f"no agent {to.strip()!r}; known agents: "
                                     f"{', '.join(sorted(known.values(), key=_plain)) or 'none'}")
            if _plain(to.strip()) == _plain(sender.strip()):
                raise TalkError(400, "you can't tell yourself; to is another agent's name")
            msg = app.talk.add(sender.strip(), known[_plain(to.strip())], text)
        except TalkError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        app.active(msg["from"])
        log.info("%s wrote to %s (%s)%s", msg["from"], msg["to"], msg["id"], "" if mode == "shown" else ", hidden")
        self._json(200, {"id": msg["id"], "to": msg["to"]})

    def _talk_delivered(self, raw: bytes) -> None:
        try:
            body = json.loads(raw)
            name, ids, listener = body["name"], body["ids"], body.get("listener")
            if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                raise ValueError("'ids' must be a list of message ids")
            if listener is not None and not (isinstance(listener, str) and len(listener) <= 80):
                raise ValueError("'listener' must be a short string")
            self.app.talk.mark_delivered(name, ids, listener)
        except TalkError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        self._json(200, {"ok": True})

    def _roster(self, raw: bytes) -> None:
        """Agents' voices and first names in one go: {"roster": [{from, voice, name}, ...]}. All or none."""
        try:
            roster = json.loads(raw)["roster"]
            if not (isinstance(roster, list) and roster and all(isinstance(r, dict) for r in roster)):
                raise ValueError("'roster' must be a non-empty list of {from, voice, name}")
            known = {v["id"] for v in self.app.tts.voices()}
            self.app.voices.apply_roster(roster, known)
        except VoiceError as err:
            return self._json(err.status, {"error": str(err)})
        except RuntimeError as err:
            return self._json(502, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("roster applied: %s", ", ".join(f"{r['name']} ({r['from']}, {r['voice']})" for r in roster))
        self._json(200, {"applied": len(roster)})

    def _set_status(self, raw: bytes) -> None:
        """{name, text}: the agent's status, shown in the owner's sidebar and its conversation; text "" clears it."""
        try:
            req = json.loads(raw)
            status = self.app.status.set(req["name"], req["text"])
        except (KeyError, TypeError, ValueError) as err:  # StatusError is a ValueError
            return self._json(400, {"error": str(err) if isinstance(err, StatusError) else f"{type(err).__name__}: {err}"})
        self.app.active(req["name"].strip())
        log.info("status of %s: %s", req["name"].strip(), status["text"] if status else "cleared")
        self._json(200, {"name": req["name"].strip(), "status": status})

    def _typing(self, where: str, inbox: Inbox | None, raw: bytes) -> None:
        """{name}: the agent is writing a post in a channel, or in the inbox it holds, until it posts there or
        TYPING_S passes."""
        try:
            name = json.loads(raw)["name"]
            if not (isinstance(name, str) and name.strip()):
                raise ValueError("'name' must be a non-empty string")
            if inbox is not None:
                inbox.check_holder(name.strip())
        except InboxError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        self.app.set_typing(where, name.strip())
        self.app.active(name.strip())
        log.info("%s typing in %s", name.strip(), where)
        self._json(200, {"name": name.strip(), "where": where, "for_s": TYPING_S})

    def _voices(self) -> None:
        try:
            voices = self.app.tts.voices()
        except RuntimeError as err:
            return self._json(502, {"error": str(err)})
        owners = self.app.voices.owners()
        self._json(200, [{**v, "taken_by": owners.get(v["id"])} for v in voices])

    def _save_draft(self, raw: bytes, new_draft) -> None:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        ext = RECORDING_EXTS.get(ctype)
        if ext is None:
            return self._json(415, {"error": f"unsupported recording type {ctype!r}"})
        draft, path = new_draft(ext)
        path.write_bytes(raw)
        log.info("recording %s uploaded (%d bytes, %s)", draft, len(raw), ctype)
        self._json(200, {"draft": draft})

    def _reply(self, msg_id: str, raw: bytes) -> None:
        store = self.app.store
        if not store.load(msg_id).ask:
            return self._json(409, {"error": "this message doesn't take replies"})
        try:
            text, audio_src, attached, _, takes = _owner_says(json.loads(raw), partial(store.draft_audio, msg_id),
                                                           store.path(msg_id, "drafts"), self.app.mix)
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        except RuntimeError as err:
            return self._json(422, {"error": f"couldn't join the recordings: {err}"})
        reply = {
            "at": now_iso(),
            "text": text,
            "audio": f"reply{audio_src.suffix}" if audio_src else None,
            "transcript": None,
            "transcript_status": "pending" if audio_src else "none",
            "transcript_error": None,
            "takes": takes,
        }
        try:
            store.save_reply(msg_id, reply, audio_src, attached)
        except FileExistsError:
            return self._json(409, {"error": "already answered"})
        if audio_src:
            self.app.transcribe_reply(msg_id)
        log.info("reply received for %s", msg_id)
        self._json(200, {"ok": True})

    def _write_to(self, box: str, raw: bytes) -> None:
        inbox = self.app.boxes[box]
        self._owner_writes(inbox, raw, None,
                           lambda text, audio_src, re_, attached: inbox.add(text, audio_src, inbox.info()["holder"],
                                                                            re_, file_drafts=attached),
                           partial(self.app.transcribe_inbox, box), f"{box} inbox", box)

    def _write_to_channel(self, name: str, raw: bytes) -> None:
        channel = self.app.channels.get(name)
        self._owner_writes(channel, raw, name,
                           lambda text, audio_src, re_, attached: channel.add(text, audio_src, re_,
                                                                              file_drafts=attached),
                           partial(self.app.transcribe_channel, name), f"#{name}")

    def _bang_text(self, text: str | None, channel: str | None, box: str | None, re_: dict | None, drafts: list,
                   audio_src, attached: list) -> tuple[str | None, str | None]:
        """With bang commands on, what the owner typed that starts with !: (None, the command), or with \\!, the
        text without the backslash. BangError for a command that can't be one. Only typed text counts: a recording's
        transcript is never looked at, and never run."""
        if not text or not self.app.settings.get()["bang"]:
            return text, None
        if text.startswith("\\!"):
            return text[1:], None
        if not text.startswith("!"):
            return text, None
        command = text[1:].strip()
        where = {"box": box or (f"#{channel}" if channel else None), "command": command}
        try:
            if channel is not None:
                raise BangError(400, f"a command runs only on an agent's own line, not in #{channel}. Start the "
                                     "message with \\! to send a literal !")
            if box == "main":
                raise BangError(400, "a command runs only on an agent's own line, not the main inbox. Start the "
                                     "message with \\! to send a literal !")
            if audio_src or drafts or attached or re_:
                raise BangError(400, "a command is typed text alone: no recording, file or reply. Start the message "
                                     "with \\! to send a literal !")
            if not command:
                raise BangError(400, "nothing after the !. Start the message with \\! to send a literal !")
        except BangError as err:
            self.app.bang_audit("refused", reason=str(err), **where)
            raise
        return None, command

    def _owner_writes(self, folder, raw: bytes, channel: str | None, add, transcribe, where: str,
                      box: str | None = None) -> None:
        """The owner writes to an inbox (box) or a channel: {text, drafts, files, re}. re: the message the owner
        answers, an agent's or their own, in a thread here. With bang commands on, text that starts with ! is a
        command for the line's session (see App.bang_send)."""
        store = self.app.store
        try:
            body = json.loads(raw)
            re_ = None
            if (re_id := body.get("re")) is not None:
                re_, place = self.app.reference(re_id)
                if place != channel:
                    raise ValueError(f"{re_id} is {'in #' + place if place else 'on a direct line'}; reply there")
                if (channel is not None and re_["kind"] == "agent" and store.load(re_id).ask
                        and store.reply(re_id) is None):
                    raise ValueError(f"{re_id} is a question waiting for its answer; answer it on /m/{re_id}")
            text, audio_src, attached, used, takes = _owner_says(body, folder.draft_audio, folder.drafts,
                                                                 self.app.mix)
            drafts = [body["draft"]] if body.get("draft") is not None else body.get("drafts", [])
            text, command = self._bang_text(text, channel, box, re_, drafts, audio_src, attached)
        except BangError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        except RuntimeError as err:
            return self._json(422, {"error": f"couldn't join the recordings: {err}"})
        if command is not None:
            try:
                msg = self.app.bang_send(folder, box, command)
            except BangError as err:
                return self._json(err.status, {"error": str(err)})
            log.info("%s command %s from the owner, for session %s", where, msg["id"], msg["bang"]["session"])
            return self._json(200, {"ok": True, "bang": msg["id"]})
        msg = add(text, audio_src, re_, attached)
        # Keep this message's original takes with it (a joined recording is a re-encoded copy). Only its own:
        # other composers may hold unsent ones in the same folder.
        kept = folder.path_of(msg["id"], "takes")
        for path in used:
            if path.exists():  # a single recording was already moved into the message
                kept.mkdir(exist_ok=True)
                os.replace(path, kept / path.name)
        if audio_src:
            folder.update(msg["id"], takes=takes)  # before the transcript: nothing is delivered until then
            transcribe(msg["id"])
        log.info("%s message %s from the owner", where, msg["id"])
        self._json(200, {"ok": True})

    def _new_channel(self, raw: bytes) -> None:
        """The owner makes a channel: {name}."""
        try:
            channel = self.app.channels.create(json.loads(raw)["name"], self.app.app_cfg["owner"])
        except ChannelError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("the owner made #%s", channel.name)
        self._json(200, self._channel_info(channel))

    def _channel_info(self, channel) -> dict:
        return {"name": channel.name, **channel.info, "listening": self.app.channel_listening(channel.name),
                "posts": self.app.store.index.rows("SELECT count(*) FROM msg WHERE channel = ?", (channel.name,))[0][0],
                "url": f"{self.app.public_url}/c/{channel.name}"}

    def _channel_pending(self, name: str, query: dict) -> None:
        """An agent's listener polls: what it hasn't been given yet. Any agent may listen to a channel."""
        agent = query.get("name", [""])[0].strip()
        if not agent:
            return self._json(400, {"error": "say who is listening (?name=)"})
        self.app.channels.get(name).last_listen[agent] = time.monotonic()
        self.app.listened.saw(agent)
        self.app.active(agent)
        self._json(200, self.app.channel_pending(name, agent))

    def _channel_delivered(self, name: str, raw: bytes) -> None:
        try:
            body = json.loads(raw)
            agent, ids, listener = body["name"], body["ids"], body.get("listener")
            if not (isinstance(agent, str) and agent.strip()):
                raise ValueError("'name' must be a non-empty string")
            if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                raise ValueError("'ids' must be a list of item ids")
            if listener is not None and not (isinstance(listener, str) and len(listener) <= 80):
                raise ValueError("'listener' must be a short string")
            self.app.channels.get(name).mark_delivered(agent.strip(), ids, listener)
        except ChannelError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("#%s items delivered to %s: %s", name, agent, ", ".join(ids))
        self._json(200, {"ok": True})

    def _claim(self, box: str, raw: bytes) -> None:
        """Agent opens (claims) or closes (releases) an inbox: {name, about, open}."""
        inbox = self.app.boxes[box]
        try:
            body = json.loads(raw)
            name, about, is_open = body["name"], body.get("about"), body.get("open", True)
            if not (isinstance(name, str) and name.strip()):
                raise ValueError("'name' must be a non-empty string")
            if about is not None and not isinstance(about, str):
                raise ValueError("'about' must be a string")
            if not isinstance(is_open, bool):
                raise ValueError("'open' must be true or false")
            if is_open:
                info = self.app.claim(box, name.strip(), about)
                log.info("%s inbox claimed by %s", box, name.strip())
            else:
                inbox.release(name.strip())
                info = inbox.info()
                log.info("%s inbox released by %s", box, name.strip())
        except InboxError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        self._json(200, {**info, "url": f"{self.app.public_url}{CHAT_PATH[box]}"})

    def _pending(self, box: str, query: dict) -> None:
        inbox = self.app.boxes[box]
        name = query.get("name", [""])[0]
        try:
            inbox.check_holder(name)
        except InboxError as err:
            return self._json(err.status, {"error": str(err)})
        self.app.last_listen[box] = time.monotonic()
        if query.get("idle", [""])[0] == "1":
            self.app.idle_at[box] = datetime.now().astimezone()
        self.app.listened.saw(name)
        self.app.active(name)
        pending = inbox.pending()
        for m in pending:
            m["audio_path"] = str(inbox.audio_path(m["id"])) if m["audio"] else None
            m["takes"] = _takes(m, inbox.audio_path(m["id"]), partial(inbox.path_of, m["id"]))
            for f in m.get("files", []):
                f["path"] = str(inbox.path_of(m["id"], f["stored"]))
            m["re_url"] = self.app.re_url(m.get("re"))
        self._json(200, pending)

    def _agent_get(self, agent: str, what: str, query: dict) -> None:
        """answers: the agent's undelivered answers (see answers). waiting: how much of the owner's is ready for
        it, without taking it: {line, answers, bang, total, ids, delivery, channels}; bang counts the commands'
        results, which are in neither total nor ids: they don't wake an agent. delivery: its mode ([delivery]);
        channels: those its sessions listen in. ?line=<segment> counts that inbox too, if it holds it; with &idle=1
        says the agent is idle and listening there (a wake hook waiting on its behalf), and with &listen=1 only
        that something listens for it there (nanotea listen, while the agent may be at work)."""
        if what == "answers":
            return self._json(200, self.app.answers(agent))
        idle, listening = query.get("idle", [""])[0] == "1", query.get("listen", [""])[0] == "1"
        line: list[str] = []
        if seg := query.get("line", [""])[0]:
            box = box_of_segment(seg)
            if box is None:
                return self._json(404, {"error": f"no inbox {seg!r}"})
            try:
                self.app.boxes[box].check_holder(agent)
            except InboxError as err:
                return self._json(err.status, {"error": str(err)})
            pending = self.app.boxes[box].pending()
            # A command's result is ready for the agent's next check, but isn't a reason to wake it.
            bangs = sum(1 for m in pending if m.get("bang"))
            pending = [m for m in pending if not m.get("bang")]
            line = [m["id"] for m in pending]
            events = sum(1 for m in pending if m.get("event"))
            if idle or listening:
                self.app.last_listen[box] = time.monotonic()
                self.app.listened.saw(agent)
            if idle:
                self.app.idle_at[box] = datetime.now().astimezone()
        elif idle or listening:
            return self._json(400, {"error": "idle=1 and listen=1 need line"})
        else:
            events = bangs = 0
        if not (idle or listening):  # a wake hook or listener polls on the agent's behalf; that isn't the agent
            self.app.active(agent)
        answers = [a["id"] for a in self.app.answers(agent)]
        talk = [m["id"] for m in self.app.talk.pending(agent)]
        rules = self.app.rules.for_agent(agent)
        self._json(200, {"line": len(line), "answers": len(answers), "talk": len(talk), "events": events,
                         "bang": bangs,
                         "total": len(line) + len(answers) + len(talk), "ids": line + answers + talk,
                         "rules_v": self.app.rules.version(rules),
                         "delivery": delivery.mode_for(self.app.delivery, agent),
                         "channels": self.app.channels_of(agent)})

    def _answers_delivered(self, agent: str, raw: bytes) -> None:
        """{ids, listener}: the asker was given these questions' outcomes. aside-<id>: told it was set aside."""
        try:
            body = json.loads(raw)
            ids, listener = body["ids"], body.get("listener")
            if not (isinstance(ids, list) and all(isinstance(i, str) and re.fullmatch(f"(aside-)?{M}", i)
                                                  for i in ids)):
                raise ValueError("'ids' must be a list of message ids, or aside-<id>")
            if listener is not None and not (isinstance(listener, str) and len(listener) <= 80):
                raise ValueError("'listener' must be a short string")
            store = self.app.store
            for i in ids:
                q = i.removeprefix("aside-")
                if not store.exists(q):
                    return self._json(404, {"error": f"no message {q}"})
                msg = store.load(q)
                if msg.sender != agent or not msg.ask:
                    return self._json(409, {"error": f"{q} is not a question {agent!r} asked"})
                if i != q and store.set_aside(q) is None:
                    return self._json(409, {"error": f"{q} was not set aside"})
            for i in ids:
                if i.startswith("aside-"):
                    store.mark_aside_delivered(i.removeprefix("aside-"), listener)
                else:
                    store.mark_answer_delivered(i, listener)
        except (KeyError, TypeError, ValueError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("answers delivered to %s: %s", agent, ", ".join(ids))
        self._json(200, {"ok": True})

    def _history(self, box: str, query: dict) -> None:
        """The holder's recent inbox messages, newest first, with every delivery: when, to whom, which listener."""
        inbox = self.app.boxes[box]
        try:
            inbox.check_holder(query.get("name", [""])[0])
            n = int(query.get("n", ["20"])[0])
            if not 1 <= n <= 200:
                raise ValueError(f"n must be 1 to 200, not {n}")
        except InboxError as err:
            return self._json(err.status, {"error": str(err)})
        except ValueError as err:
            return self._json(400, {"error": f"ValueError: {err}"})
        out = []
        for m in inbox.messages(limit=n):
            if ev := m.get("event"):
                kind, summary = f"event {ev['kind']} from {ev['source']}", ev["summary"]
            elif m.get("reaction"):
                kind, summary = "reaction", f"{m['text']} to {m['re']['sender']}'s \"{m['re']['title']}\""
            elif m.get("tap"):
                kind, summary = "tap", f"{m['text']} on {m['re']['sender']}'s \"{m['re']['title']}\""
            elif b := m.get("bang"):
                kind = "bang"
                summary = f"$ {m['text']} ({b['state']}{', exit ' + str(b['exit']) if 'exit' in b else ''})"
            else:
                kind = "voice" if m["audio"] else "text" if m["text"] else "files"
                heard = {"none": None, "done": m["transcript"], "pending": "(transcribing)",
                         "failed": "(transcription failed)"}[m["transcript_status"]]
                summary = " ".join(p for p in (m["text"], heard) if p)
            out.append({"id": m["id"], "at": m["at"], "kind": kind, "summary": summary,
                        "files": len(m.get("files", [])), "delivered_to": m["delivered_to"],
                        "delivered_at": m["delivered_at"], "deliveries": m.get("deliveries")})
        self._json(200, out)

    def _delivered(self, box: str, raw: bytes) -> None:
        try:
            body = json.loads(raw)
            name, ids, listener = body["name"], body["ids"], body.get("listener")
            if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                raise ValueError("'ids' must be a list of message ids")
            if listener is not None and not (isinstance(listener, str) and len(listener) <= 80):
                raise ValueError("'listener' must be a short string")
            self.app.boxes[box].mark_delivered(name, ids, listener)
        except InboxError as err:
            return self._json(err.status, {"error": str(err)})
        except (KeyError, TypeError, ValueError, FileNotFoundError) as err:
            return self._json(400, {"error": f"{type(err).__name__}: {err}"})
        log.info("%s inbox messages delivered to %s: %s", box, name, ", ".join(ids))
        self._json(200, {"ok": True})

    def _owner_file(self, where: str, msg_id: str, basename: str) -> None:
        """A file the owner attached, from an inbox message (its API segment), a channel post ("c/<name>"),
        or a reply ("reply")."""
        if where == "reply":
            item = self.app.store.reply(msg_id) if self.app.store.exists(msg_id) else None
            base = self.app.store.path(msg_id) if item else None
        elif where.startswith("c/"):
            try:
                channel = self.app.channels.get(where[2:])
            except ChannelError as err:
                return self._json(err.status, {"error": str(err)})
            item = next(iter(channel.messages("id = ?", (msg_id,))), None)
            base = channel.path_of(msg_id, "") if item else None
        else:
            box = box_of_segment(where)
            if box is None:
                return self._json(404, {"error": f"no inbox {where!r}"})
            inbox = self.app.boxes[box]
            item = next(iter(inbox.messages("id = ?", (msg_id,))), None)
            base = inbox.path_of(msg_id, "") if item else None
        record = next((f for f in (item or {}).get("files", []) if Path(f["stored"]).name == basename), None)
        if record is None:
            return self._json(404, {"error": "no such file"})
        self._file(base / record["stored"], record["type"])

    def _file(self, path: Path | None, ctype: str | None = None) -> None:
        # Byte ranges are required for playback in iOS Safari.
        if not path or not path.is_file():
            return self._json(404, {"error": "no audio"})
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        if header := self.headers.get("Range"):
            m = RANGE.fullmatch(header.strip())
            if not m or m[1] == m[2] == "":
                return self._unsatisfiable(size)
            if m[1] == "":
                start = max(0, size - int(m[2]))
            else:
                start = int(m[1])
                if m[2]:
                    end = min(int(m[2]), size - 1)
            if start > end:
                return self._unsatisfiable(size)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype or AUDIO_TYPES[path.suffix[1:].lower()])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            try:
                while remaining:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        raise RuntimeError(f"{path} shrank while serving")
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                log.info("client closed %s early", path.name)

    def _unsatisfiable(self, size: int) -> None:
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect(self, url: str) -> None:
        self._send(302, "text/plain", b"", [("Location", url)])

    def _html(self, page: str, status: int = 200) -> None:
        self._send(status, "text/html; charset=utf-8", page.encode())

    def _json(self, status: int, obj) -> None:
        if status >= 400:
            log.warning("%s %s answered %d: %s", self.command, re.sub(r"k=[^&\s\"]+", "k=...", self.path), status,
                        obj.get("error") if isinstance(obj, dict) else obj)
        self._send(status, "application/json", json.dumps(obj, indent=2).encode())

    def _send(self, status: int, ctype: str, body: bytes, headers: list[tuple[str, str]] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in [*self.extra_headers, *headers]:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code="-", size="-") -> None:
        # Waiting agents poll /api/ every few seconds; log only their failures.
        if self.command == "GET" and self.who and self.who.kind == "agent" and int(code) < 400:
            return
        super().log_request(code, size)

    def log_message(self, fmt: str, *args) -> None:
        # Keep the reply key out of the log.
        log.info("%s %s", self.address_string(), re.sub(r"k=[^&\s\"]+", "k=...", fmt % args))


class Server(ThreadingHTTPServer):
    def __init__(self, addr: tuple[str, int], app: App):
        super().__init__(addr, Handler)
        self.app = app

    def server_bind(self) -> None:
        # HTTPServer's also asks DNS for the host's name before it listens, which can stall the start for half a
        # minute; nothing here uses the name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address) -> None:
        err = sys.exception()
        if isinstance(err, (ConnectionError, TimeoutError)):
            log.info("connection from %s dropped: %s", client_address[0], err)
        else:
            super().handle_error(request, client_address)


def pair_link() -> None:
    """The link that pairs a device with the owner's key."""
    cfg = load_config()
    root = resolve(cfg["data_dir"])
    root.mkdir(parents=True, exist_ok=True)
    print(f"{cfg['public_url']}/?k={_reply_key(root / 'reply_key')}")


def reach_warnings(cfg: dict) -> list[str]:
    """What will keep browsers from the service, or from its features, as configured."""
    url = urlsplit(cfg["public_url"])
    host, out = url.hostname, []
    local = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")
    if url.scheme == "http" and not local:
        out.append(f"public_url is plain http on {host}: browsers there turn off recording, notifications and the "
                   "Home Screen app, and the pairing key crosses the network unencrypted. docs/running.md shows how "
                   "to put https in front.")
    if cfg["host"] in ("localhost", "127.0.0.1", "::1") and not local and url.port == cfg["port"]:
        out.append(f"public_url names this service directly, but host {cfg['host']} listens only on this machine, "
                   f"so {host} can't reach it. Set host = \"0.0.0.0\", or put a proxy in front (docs/running.md).")
    return out


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    try:
        app = App(cfg, load_env_file(cfg.get("env_file")))
    except ConfigError as err:
        sys.exit(f"nanotea: config {CONFIG}: {err}")
    except ThemeError as err:
        sys.exit(f"nanotea: {err}")
    app.resume()
    threading.Thread(target=app.watch_held, name="held", daemon=True).start()
    threading.Thread(target=app.watch_quiet, name="quiet", daemon=True).start()
    threading.Thread(target=app.watch_bang, name="bang", daemon=True).start()
    server = Server((cfg["host"], cfg["port"]), app)
    app.server = server
    if missing := app.tokenless():
        log.error("These agents have no token: %s. Each asks for one when its nanotea mcp next starts, and works "
                  "once the approver or the owner approves it.", ", ".join(missing))
    for name, boxes in app.doubled().items():
        log.error("%s holds %d lines (%s), and shows as a row for each; an agent holds one. Release all but one with "
                  "nanotea-tell --from %s --close-inbox --inbox LINE", name, len(boxes), ", ".join(boxes),
                  shlex.quote(name))
    if app.tokens.approver() is None:
        log.warning("No approver yet: the owner approves each agent that asks for a token, in the app under Tokens, "
                    "and can make one agent the approver there.")
    log.info("listening on %s:%d; public %s; proxies %s; version %s", cfg["host"], cfg["port"], app.public_url,
             ", ".join(sorted(app.proxies)) or "none", version.RUNNING)
    for warning in reach_warnings(cfg):
        log.warning("%s", warning)
    server.serve_forever()
    if app.restarting:  # the owner changed the config: the same command again, in this process
        server.server_close()
        log.warning("restarting with %s", cfg.path)
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def _can_listen(host: str, port: int, current: tuple[str, int]) -> None:
    """Refuse a host and port the service couldn't listen on once it restarts."""
    if (host, port) == current:
        return
    import socket
    # The port this service holds is free once it stops; then only the address needs to be one of this machine's.
    try:
        with socket.create_server((host, 0 if port == current[1] else port)):
            pass
    except OSError as err:
        raise configedit.EditError(f"the service couldn't listen on {host}:{port}: {err.strerror or err}")
