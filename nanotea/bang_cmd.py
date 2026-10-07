"""nanotea bang: turn the owner's bang commands on or off, or see whether they are on.

Turning them on needs the host key (data/host_key), so it runs on the machine the service runs on: the pairing
key, which links and browsers carry, can turn them off and never on.

nanotea bang on
nanotea bang off
nanotea bang status
"""

import argparse
import sys

from nanotea.client import Client, NanoteaError
from nanotea.config import load_config
from nanotea.credentials import host_key


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="nanotea bang", description=__doc__.split("\n\n")[0])
    p.add_argument("cmd", choices=("on", "off", "status"))
    a = p.parse_args(argv)
    cfg = load_config()
    try:
        client = Client(cfg["port"], host_key(cfg))
        if a.cmd == "status":
            now = client.get("/api/settings")
        else:
            now = client.post("/api/settings", {"bang": a.cmd == "on"})
    except NanoteaError as err:
        sys.exit(f"nanotea bang: {err}")
    ready = ", ".join(now["bang_sessions"]) or "none right now"
    print(f"bang commands are {'on' if now['settings']['bang'] else 'off'}. Sessions started with --bang: {ready}")
