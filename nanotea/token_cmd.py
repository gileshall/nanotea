"""nanotea token: issue, list, revoke and rotate the tokens agents present to the service.

It acts as the owner, with the pairing key from data/reply_key, so it runs on the machine the service runs on (or
with NANOTEA_KEY_FILE naming a copy of the key). A token the service issues is shown here once and written to a
file only its user can read: where nanotea mcp, nanotea hook and nanotea-tell look for it.

nanotea token add NAME [--line LINE] [--events --line LINE] [--file PATH] [--force]
nanotea token rotate NAME [--line LINE] [--events --line LINE] [--file PATH]
nanotea token list [--revoked]
nanotea token revoke ID
nanotea token path NAME [--events]
"""

import argparse
import sys
from pathlib import Path

from nanotea.client import Client, NanoteaError
from nanotea.config import load_config
from nanotea.credentials import owner_key, token_path, write_token


def _issue(client: Client, a: argparse.Namespace, replace: bool) -> None:
    kind = "events" if a.events else "agent"
    if a.events and not a.line:
        sys.exit(f"nanotea token {a.cmd}: --events needs --line: an events token reports to one line")
    path = Path(a.file).expanduser() if a.file else token_path(a.name, kind)
    if path.exists() and not (replace or a.force):
        sys.exit(f"nanotea token add: {path} exists and may hold a working token. To replace it and revoke the "
                 f"old one: nanotea token rotate {a.name}. To keep both: --file PATH. To overwrite it: --force")
    out = client.post("/api/tokens", {"kind": kind, "name": a.name, "line": a.line, "replace": replace})
    write_token(path, out["token"])
    record = out["record"]
    what = (f"events token for {a.name!r} (line {a.line})" if a.events else
            f"token for {a.name!r}, bound to line {a.line}" if a.line else f"token for {a.name!r}")
    print(f"{what} {record['id']} written to {path}")
    for ident in out["revoked"]:
        print(f"revoked the old token {ident}")
    if a.file:
        who = "the program that posts the events" if a.events else "the agent"
        print(f"point {who} at it with NANOTEA_TOKEN_FILE={path}")


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="nanotea token", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd, help_ in (("add", "issue a token for NAME and write it to its file"),
                       ("rotate", "issue a new token for NAME, write it, and revoke NAME's other tokens")):
        s = sub.add_parser(cmd, help=help_)
        s.add_argument("name", help="the agent's name, as it joins; or with --events, the source name")
        s.add_argument("--events", action="store_true", help="an events token: it can only post events to --line")
        s.add_argument("--line", help="main, or a direct line's name. An agent's token then works on that line "
                                      "alone, and no other agent's can claim it. With --events: the line it "
                                      "reports to")
        s.add_argument("--file", help="write the token here instead of the default file")
        if cmd == "add":
            s.add_argument("--force", action="store_true", help="overwrite the file if it exists")
    s = sub.add_parser("list", help="the live tokens: who, which line, when made and last used")
    s.add_argument("--revoked", action="store_true", help="include revoked tokens")
    s = sub.add_parser("revoke", help="refuse a token from now on")
    s.add_argument("id", help="the token's id, from nanotea token list")
    s = sub.add_parser("path", help="print where NAME's token file is by default")
    s.add_argument("name")
    s.add_argument("--events", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "path":
        return print(token_path(a.name, "events" if a.events else "agent"))
    cfg = load_config()
    try:
        client = Client(cfg["port"], owner_key(cfg))
        if a.cmd in ("add", "rotate"):
            _issue(client, a, replace=a.cmd == "rotate")
        elif a.cmd == "list":
            rows = client.get("/api/tokens", revoked="1" if a.revoked else None)
            for t in rows:
                scope = (f"events to {t['line']}" if t["kind"] == "events" else
                         f"agent on {t['line']}" if t["line"] else "agent")
                used = t["last_used"] or "not since start"
                state = f"revoked {t['revoked']}" if t["revoked"] else f"last used {used}"
                print(f"{t['id']}  {t['name']:<24} {scope:<18} made {t['created']}  {state}")
            if not rows:
                print("no tokens; issue one with: nanotea token add NAME")
        else:
            out = client.post(f"/api/tokens/{a.id}/revoke", {})
            print(f"revoked {out['id']}, the token of {out['name']!r}")
    except NanoteaError as err:
        sys.exit(f"nanotea token: {err}")
