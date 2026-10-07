"""The service's API, for the agents and programs that hold a credential for it (nanotea/credentials.py)."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

LINE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


class NanoteaError(RuntimeError):
    """The service refused a request or couldn't be reached. status: the HTTP status, or None if unreachable."""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


def segment(line: str) -> str:
    """A line's API segment: the main inbox, or a direct line (dm-<name>)."""
    if line == "main":
        return "inbox"
    if not LINE.fullmatch(line):
        raise ValueError(f"line names are lowercase letters, digits and dashes (at most 40), not {line!r}")
    return f"dm-{line}"


def line_for(name: str) -> str:
    """The direct line an agent named name opens by default: its name, slugged."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    if not slug:
        raise ValueError(f"no line name can be made from {name!r}; pick one")
    return slug


class Client:
    def __init__(self, port: int, credential: str | None, timeout_s: float = 120, before=None):
        """credential: an agent's token, or for the owner's commands the pairing key. None: before, if given, is
        called to set it or raise; else the client calls only what needs no credential (post_open)."""
        self.base = f"http://127.0.0.1:{port}"
        self.credential = credential
        self.timeout_s = timeout_s
        self.before = before

    def get(self, path: str, **query):
        q = {k: v for k, v in query.items() if v is not None}
        return self._call(f"{path}?{urllib.parse.urlencode(q)}" if q else path, None)

    def post(self, path: str, body: dict):
        return self._call(path, body)

    def post_open(self, path: str, body: dict):
        """What a program without a token posts: asking for one, and collecting it."""
        return self._call(path, body, credential=False)

    def _call(self, path: str, body: dict | None, credential: bool = True):
        if credential and self.credential is None and self.before is not None:
            self.before()
        if credential and self.credential is None:
            raise NanoteaError(None, f"no token to call {path} with; nanotea mcp asks for one when it starts")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {self.credential}"
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                message = json.loads(raw)["error"]
            except (ValueError, KeyError, TypeError):
                message = f"HTTP {e.code} {e.reason}: {raw[:200]!r}"
            raise NanoteaError(e.code, message) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise NanoteaError(None, f"service not reachable at {self.base}: {getattr(e, 'reason', e)}") from e
