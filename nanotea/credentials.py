"""Where the programs that talk to the service keep their credentials: files, never config.

An agent's token is a file, by default <config dir>/tokens/<name, slugged>.token, or the one NANOTEA_TOKEN_FILE
names; an events token is <slug>.events.token there. They are written with mode 0600, and these refuse a file others
can read. Harness configs name the file, never the secret, so they can be committed.

A program without a token asks the service for one (nanotea/enroll.py) and keeps the request beside the file the
token will go to, as <token file>.request (mode 0600: it holds the secret that collects the token). Once the
approver or the owner approves it, the program collects the token into its file.

The owner's own commands (nanotea token, nanotea sop) present the pairing key, from data/reply_key or the file
NANOTEA_KEY_FILE names. nanotea bang presents the host key, data/host_key, which has no such override: it works
only where the data is."""

import fcntl
import json
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from nanotea.client import Client, NanoteaError
from nanotea.config import BASE, resolve
from nanotea.tokens import TOKEN

TOKEN_FILE_ENV = "NANOTEA_TOKEN_FILE"
KEY_FILE_ENV = "NANOTEA_KEY_FILE"


class CredentialError(NanoteaError):
    """A credential this machine should have is missing or unusable. The message says how to get one."""

    def __init__(self, message: str):
        super().__init__(None, message)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "agent"


def token_path(name: str, kind: str = "agent") -> Path:
    """Where nanotea token add puts the token of name."""
    return BASE / "tokens" / f"{slug(name)}{'.events' if kind == 'events' else ''}.token"


def _read(path: Path, what: str, fix: str) -> str:
    try:
        mode = path.stat().st_mode
        secret = path.read_text().strip()
    except FileNotFoundError:
        raise CredentialError(f"no {what} at {path}; {fix}") from None
    except OSError as err:
        raise CredentialError(f"can't read the {what} at {path}: {err.strerror}") from err
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialError(f"the {what} at {path} can be read by other users (mode {stat.S_IMODE(mode):o}); "
                              f"run: chmod 600 {path}")
    if not secret:
        raise CredentialError(f"the {what} at {path} is empty; {fix}")
    return secret


def write_token(path: Path, token: str) -> None:
    """The token as a file readable by its owner alone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)


def token_file(name: str | None, kind: str = "agent") -> Path | None:
    """Where the token of name is: the file NANOTEA_TOKEN_FILE names, or its default. None: no name, and no
    NANOTEA_TOKEN_FILE to say."""
    if file := os.environ.get(TOKEN_FILE_ENV):
        return Path(file).expanduser()
    return token_path(name, kind) if name else None


@contextmanager
def _locked(path: Path):
    """Programs that start together ask once, and collect once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _request_path(path: Path) -> Path:
    return path.with_name(path.name + ".request")


def ask(cfg: dict, kind: str, name: str, line: str | None, stale: str | None = None) -> dict:
    """name's request for a token, the one kept beside its token file or a new one: {id, secret, kind, name, line,
    approver}. approver: who decides it, or None for the owner. id None: the token is already in its file, collected
    by another program asking for the same name since this one looked; collect gives it. stale: as in collect."""
    path = token_file(name, kind)
    with _locked(path):
        if path.exists() and _read(path, "token", "") != stale:
            return {"id": None, "secret": None, "kind": kind, "name": name, "line": line, "approver": None}
        req = _request_path(path)
        if req.exists():
            kept = json.loads(_read(req, "token request", "remove it to ask again"))
            if (kept["kind"], kept["name"], kept["line"]) == (kind, name, line):
                return kept
        out = Client(cfg["port"], None).post_open("/api/enroll", {"kind": kind, "name": name, "line": line,
                                                                  "dir": os.getcwd()})
        kept = {"id": out["request"]["id"], "secret": out["secret"], "kind": kind, "name": name, "line": line,
                "approver": out["approver"]}
        fd = os.open(req, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(kept))
        return kept


def collect(cfg: dict, request: dict, stale: str | None = None) -> tuple[str | None, dict]:
    """(the token, once approved, written to its file; else None) and the request as the service has it, with
    approver. A request that is closed without a token, or that the service no longer has, is removed. stale: a
    token in the file that the service refused, which a new one replaces."""
    path = token_file(request["name"], request["kind"])
    with _locked(path):
        req = _request_path(path)
        if path.exists() and (token := _read(path, "token", "")) != stale:
            # Another program asking for the same name collected it first.
            req.unlink(missing_ok=True)
            return token, {**request, "state": "collected"}
        if request["id"] is None:
            raise CredentialError(f"the token file {path} was removed after another program collected it; run again "
                                  f"to ask for a new one")
        try:
            out = Client(cfg["port"], None).post_open(f"/api/enroll/{request['id']}/collect",
                                                      {"secret": request["secret"]})
        except NanoteaError as err:
            if err.status != 404:
                raise
            req.unlink(missing_ok=True)
            return None, {**request, "state": "gone", "reason": str(err)}
        if out["token"] is not None:
            write_token(path, out["token"])
            req.unlink(missing_ok=True)
            return out["token"], out["request"]
        if out["request"]["state"] != "pending":
            req.unlink(missing_ok=True)
        return None, {**out["request"], "approver": out["approver"]}


def waiting_on(request: dict) -> str:
    return request["approver"] or "the owner, in the app under Tokens"


def obtain(cfg: dict, kind: str, name: str, line: str | None, wait_s: float, poll_s: float = 2) -> str:
    """name's token: from its file, or asked for and collected once approved, waiting up to wait_s. For programs
    that can wait, such as nanotea-tell; nanotea mcp asks and answers its tool calls meanwhile."""
    path = token_file(name, kind)
    if path.exists():
        return agent_token(name, kind)
    request = ask(cfg, kind, name, line)
    if request["id"] is None:
        return agent_token(name, kind)
    print(f"nanotea: asked for a token for {name!r} (request {request['id']}); waiting on {waiting_on(request)}",
          file=sys.stderr, flush=True)
    deadline = time.monotonic() + wait_s
    while True:
        token, state = collect(cfg, request)
        if token is not None:
            return agent_token(name, kind)
        if state["state"] == "denied":
            raise CredentialError(f"request {request['id']} for {name!r} was denied by {state['by']}"
                                  + (f": {state['reason']}" if state["reason"] else ""))
        if state["state"] != "pending":
            raise CredentialError(f"request {request['id']} for {name!r} is {state['state']}"
                                  + (f" ({state['reason']})" if state.get("reason") else "") + "; run again to ask "
                                  "again")
        if time.monotonic() > deadline:
            raise CredentialError(f"request {request['id']} for {name!r} is still waiting on {waiting_on(state)}. "
                                  f"It stays open: run this again to collect the token once it is approved")
        time.sleep(poll_s)


def agent_token(name: str | None, kind: str = "agent") -> str:
    """The token for the agent (or events source) name, from NANOTEA_TOKEN_FILE or its default file. name None:
    only NANOTEA_TOKEN_FILE can say."""
    label = "token" if kind == "agent" else "events token"
    issue = "nanotea mcp asks for one when it starts without it, as does nanotea-tell"
    if file := os.environ.get(TOKEN_FILE_ENV):
        token = _read(Path(file).expanduser(), f"{label} that {TOKEN_FILE_ENV} names", issue)
    elif name is None:
        raise CredentialError(f"no {label}: {TOKEN_FILE_ENV} isn't set, and no name was given to find it by")
    else:
        token = _read(token_path(name, kind), f"{label} for {name!r}", issue)
    if not TOKEN.fullmatch(token):
        raise CredentialError(f"that {label} file doesn't hold a token; remove it, and {issue}")
    return token


def owner_key(cfg: dict) -> str:
    """The pairing key, for the owner's own commands."""
    file = os.environ.get(KEY_FILE_ENV)
    path = Path(file).expanduser() if file else resolve(cfg["data_dir"]) / "reply_key"
    return _read(path, "pairing key", f"start the service once (nanotea serve) to make it, or set {KEY_FILE_ENV} "
                                      f"to a copy of data/reply_key")


def host_key(cfg: dict) -> str:
    """The host key, for what only a shell on the service's machine may do."""
    return _read(resolve(cfg["data_dir"]) / "host_key", "host key",
                 "run this on the machine nanotea runs on (under Docker: docker compose exec nanotea nanotea ...), "
                 "after the service has started once")
