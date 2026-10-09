"""nanotea listen: what an agent in listen mode ([delivery], nanotea/delivery.py) keeps running in the background.

It shows the agent as listening, and when something of the owner's is ready, takes it, prints it and exits. The
harness tells the agent its background task ended, which brings it back mid-task or from idle; the agent reads
what was printed and starts this again. Events alone wait EVENT_SETTLE_S for others with batch_events on, and a
bang command's result never ends it, as for the wake hook (nanotea/hook.py). It refuses to run while the agent's
mode is something else, and on an upgrade runs on as the new code.

nanotea listen --name NAME [--line LINE]
"""

import argparse
import os
import shlex
import sys
import time

from nanotea import version
from nanotea.client import Client, NanoteaError, line_for
from nanotea.config import load_config
from nanotea.credentials import TOKEN_FILE_ENV, agent_token
from nanotea.hook import POLL_S, waiting
from nanotea.mailbox import Mailbox, handed
from nanotea.settings import EVENT_SETTLE_S


def command(name: str, line: str) -> str:
    """The command that starts listening for name, as this process was started: its config and token file."""
    env = {k: os.environ[k] for k in ("NANOTEA_CONFIG", TOKEN_FILE_ENV) if os.environ.get(k)}
    argv = [sys.executable, "-m", "nanotea", "listen", "--name", name]
    if line != line_for(name):
        argv += ["--line", line]
    return "".join(f"{k}={shlex.quote(v)} " for k, v in env.items()) + shlex.join(argv)


def listen(client: Client, owner: str, name: str, line: str) -> str:
    """Waits until something wakes the agent; returns what to print."""
    batch = client.get("/api/settings")["settings"]["batch_events"]
    first_event = None
    while True:
        w = waiting(client, name, line, listen=True)
        if version.stale(client.version):
            os.execv(sys.executable, [sys.executable, "-m", "nanotea", "listen", "--name", name, "--line", line])
        if w["delivery"] != "listen":
            raise ValueError(f"{name}'s delivery mode is {w['delivery']}, not listen ([delivery] in the service's "
                             f"config): don't start nanotea listen again")
        if w["total"]:
            only_events = w["total"] == w["events"]
            if only_events and batch:
                first_event = first_event or time.monotonic()
            if not (only_events and batch) or time.monotonic() - first_event >= EVENT_SETTLE_S:
                items = Mailbox(client, name, line, w["channels"], f"listen pid {os.getpid()}").take(False)
                if items:
                    return (handed(owner, items, "listen") + "\nStart nanotea listen again now, in the background: "
                            + command(name, line) + "\n")
                first_event = None  # another took them first: check, or a second listen
        time.sleep(POLL_S)


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="nanotea listen",
                                description="For an agent in listen mode, run in the background: exits with the "
                                            "owner's messages when they come.")
    p.add_argument("--name", required=True, help="the agent's name, as it joins")
    p.add_argument("--line", help="its line (default: the name, slugged)")
    a = p.parse_args(argv)
    cfg = load_config()
    try:
        out = listen(Client(cfg["port"], agent_token(a.name)), cfg["app"]["owner"], a.name,
                     a.line or line_for(a.name))
    except (NanoteaError, ValueError) as err:
        sys.exit(f"nanotea listen: {err}")
    print(out, end="", flush=True)
