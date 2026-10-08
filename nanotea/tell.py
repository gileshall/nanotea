"""Send the owner a message through the local nanotea service, or ask them something."""

import argparse
import base64
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from nanotea import version
from nanotea.bang import OWNER_ONLY_NOTE, Host
from nanotea.client import Client, NanoteaError
from nanotea.config import load_config
from nanotea.credentials import obtain

_cfg = load_config()
OWNER, PORT = _cfg["app"]["owner"], _cfg["port"]
RESTART_GRACE_S = 120
APPROVAL_WAIT_S = 600  # without a token: how long to wait for the request for one to be approved
TOKEN: str | None = None  # set by main: the token this run presents
SERVICE: dict[str, str | None] = {"version": None}  # from the service's last response


def request(url: str, body: dict | None = None, patient: bool = False):
    """patient: ride out a service restart for up to RESTART_GRACE_S, then fail."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "Authorization": f"Bearer {TOKEN}"})
    deadline = None
    while True:
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                SERVICE["version"] = r.headers.get(version.HEADER)
                return json.load(r)
        except urllib.error.HTTPError as e:
            sys.exit(f"nanotea-tell: {json.loads(e.read()).get('error', e.reason)}")
        except (urllib.error.URLError, ConnectionError) as e:
            reason = getattr(e, "reason", e)
            if not patient:
                sys.exit(f"nanotea-tell: service not reachable at {url}: {reason}")
            if deadline is None:
                deadline = time.monotonic() + RESTART_GRACE_S
                print(f"nanotea-tell: service unreachable ({reason}); retrying for {RESTART_GRACE_S}s",
                      file=sys.stderr)
            if time.monotonic() > deadline:
                sys.exit(f"nanotea-tell: service not reachable at {url} for {RESTART_GRACE_S}s: {reason}")
            time.sleep(3)


def list_voices(base: str) -> None:
    for v in request(f"{base}/api/voices"):
        owner = f"taken by {v['taken_by']}" if v["taken_by"] else "free"
        print(f"{v['id']:<18} {v.get('name') or '':<12} {v.get('gender') or '':<8} {v.get('locale') or '':<7} {owner}")


def take_line(path: str, t: dict) -> str:
    """One recording as made: where, its type and size, and what the microphone did."""
    line = f"[take] {path} ({t['type']}, {t['size']} bytes"
    if c := t.get("capture"):
        heard = [f"{c[k]} {unit}" for k, unit in (("rate", "Hz"), ("channels", "channels")) if c[k] is not None]
        line += f"; {'raw' if c['raw'] else 'processed'}, {', '.join(heard) or 'rate and channels not reported'}"
        line += "".join(f", {k.replace('_', ' ')} {'on' if c[k] else 'off'}"
                        for k in ("echo_cancellation", "noise_suppression", "auto_gain_control") if c[k] is not None)
        line += f", mic: {c['mic']}" if c["mic"] else ""
    return line + ")"


def tap_line(tap: dict) -> str:
    return f"[tap on your {tap['control']} control: your words, picked, not the owner's] {json.dumps(tap['data'])}"


def wait_reply(base: str, msg_id: str) -> None:
    noted = False
    while True:
        msg = request(f"{base}/api/messages/{msg_id}", patient=True)
        reply = msg["reply"]
        if reply is None and msg["status"] == "failed":
            sys.exit(f"nanotea-tell: delivery failed: {msg['error']}")
        if reply is None and msg["set_aside"]:
            request(f"{base}/api/agents/{urllib.parse.quote(msg['sender'], safe='')}/answers/delivered",
                    {"ids": [f"aside-{msg_id}"], "listener": f"nanotea-tell pid {os.getpid()}"}, patient=True)
            sys.exit(f"nanotea-tell: {OWNER} set this question aside without answering it; ask again if you still "
                     "need the answer")
        if reply is not None and reply["transcript_status"] == "pending" and not noted:
            print(f"nanotea-tell: {OWNER} replied by voice; waiting for the transcript", file=sys.stderr)
            noted = True
        if reply is not None and reply["transcript_status"] != "pending":
            if reply["text"]:
                print(reply["text"])
            if reply.get("tap"):
                print(tap_line(reply["tap"]))
            for file in reply.get("files", []):
                print(f"[file] {msg['dir']}/{file['stored']} ({file['type']}, {file['size']} bytes, "
                      f"sent as {file['name']})")
            if reply["audio"]:
                audio = f"{msg['dir']}/{reply['audio']}"
                if reply["transcript_status"] == "failed":
                    sys.exit(f"nanotea-tell: {OWNER} replied by voice ({audio}) but transcription failed: "
                             f"{reply['transcript_error']}")
                print(f"[voice reply, transcribed] {reply['transcript']}")
                print(f"[voice reply audio] {audio}")
                for t in reply.get("takes", []):
                    print(take_line(f"{msg['dir']}/{t['stored'] or reply['audio']}", t))
            request(f"{base}/api/agents/{urllib.parse.quote(msg['sender'], safe='')}/answers/delivered",
                    {"ids": [msg_id], "listener": f"nanotea-tell pid {os.getpid()}"}, patient=True)
            return
        time.sleep(3)


INBOX_SEGMENT = {"main": "inbox"}


def inbox_segment(box: str) -> str:
    """The main inbox, or a direct line to one agent (dm-<name>), made when it first claims it."""
    if box in INBOX_SEGMENT:
        return INBOX_SEGMENT[box]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", box):
        sys.exit(f"nanotea-tell: inbox names are lowercase letters, digits and dashes, not {box!r}")
    return f"dm-{box}"


def print_words(m: dict) -> None:
    """The owner's text, files and voice."""
    if m["text"]:
        print(m["text"])
    for file in m.get("files", []):
        print(f"[file] {file['path']} ({file['type']}, {file['size']} bytes, sent as {file['name']})")
    if m["audio"]:
        if m["transcript_status"] == "done":
            print(f"[voice, transcribed] {m['transcript']}")
        else:
            print(f"[voice, transcription failed: {m['transcript_error']}]")
        print(f"[voice audio] {m['audio_path']}")
        for t in m["takes"]:
            print(take_line(t["path"], t))


def replying_to(re_: dict, url: str) -> str:
    """The message a reply answers, and its thread. Old replies say no kind or thread: they answered an agent."""
    whose = OWNER if re_.get("kind") == "owner" else re_["sender"]
    thread = re_.get("thread") or f"m-{re_['id']}"
    return f"{whose}'s message \"{re_['title']}\" (id {re_['id']}, thread {thread}) {url}"


def print_bang(m: dict, b: dict) -> None:
    """A command the owner ran here with !: what it was and what it did."""
    how = f"exit {b['exit']}" + (f" (signal {b['signal']})" if b["signal"] else "")
    if b["timed_out"]:
        how += ", timed out"
    print(f"[bang from {OWNER}, {m['at']}, id {m['id']}, {how}, {b['duration_s']} s, cwd {b['cwd']}] $ {m['text']}")
    print(f"[bang note] {OWNER_ONLY_NOTE}")
    for stream in ("stdout", "stderr"):
        if b[stream]:
            print(f"[bang {stream}]")
            print(b[stream].rstrip("\n"))
    for stream, c in (b["truncated"] or {}).items():
        print(f"[bang {stream} cut] {c['total_bytes']} bytes in all; showing {c['shown_bytes']} ({c['kept']}); "
              f"{c['cut_bytes']} bytes are not shown here")
    if b["lingering"]:
        print("[bang] a process it started was still holding its output open when this was reported")
    print(f"[bang full output] {b['full_output']}\n")


def print_inbox(pending: list[dict]) -> None:
    for m in pending:
        if b := m.get("bang"):
            print_bang(m, b)
            continue
        if ev := m.get("event"):
            print(f"[event from {ev['source']}, {m['at']}, id {m['id']}: {ev['kind']}] {ev['summary']}")
            if ev["data"] is not None:
                print(f"[event data] {json.dumps(ev['data'])}")
            print()
            continue
        re_ = m.get("re")
        target = replying_to(re_, m["re_url"]) if re_ else ""
        if m.get("reaction"):
            print(f"[from {OWNER}, {m['at']}, id {m['id']}: reacted {m['text']} to {target}]\n")
            continue
        if m.get("tap"):
            print(f"[from {OWNER}, {m['at']}, id {m['id']}: tapped {json.dumps(m['text'])} on {target}]")
            print(tap_line(m["tap"]) + "\n")
            continue
        print(f"[from {OWNER}, {m['at']}, id {m['id']}" + (f", replying to {target}]" if re_ else "]"))
        print_words(m)
        print()


def print_channel(channel: str, pending: list[dict]) -> None:
    for m in pending:
        re_ = m.get("re")
        if m["kind"] == "post":
            print(f"[#{channel}, from {m['who']}, {m['at']}, id {m['id']}" + (f", asks {OWNER}" if m["ask"] else "")
                  + (f", replying to {replying_to(re_, re_['url'])}]" if re_ else "]"))
            print(m["text"])
            for clip in m["clips"]:
                print(f"[audio clip] {clip}")
        elif m["kind"] == "answer":
            asked = f"{re_['sender']}'s question \"{re_['title']}\" {m['re_url']}"
            if m["reaction"]:
                print(f"[#{channel}, {m['at']}: {OWNER} answered {asked} with {m['text']}]\n")
                continue
            if m.get("tap"):
                print(f"[#{channel}, {m['at']}: {OWNER} answered {asked} by tapping {json.dumps(m['text'])}]")
                print(tap_line(m["tap"]) + "\n")
                continue
            print(f"[#{channel}, {m['at']}: {OWNER} answered {asked}]")
            print_words(m)
        else:
            target = replying_to(re_, m["re_url"]) if re_ else ""
            if m["reaction"]:
                print(f"[#{channel}, from {OWNER}, {m['at']}, id {m['id']}: reacted {m['text']} to {target}]\n")
                continue
            if m.get("tap"):
                print(f"[#{channel}, from {OWNER}, {m['at']}, id {m['id']}: tapped {json.dumps(m['text'])} on {target}]")
                print(tap_line(m["tap"]) + "\n")
                continue
            print(f"[#{channel}, from {OWNER}, {m['at']}, id {m['id']}" + (f", replying to {target}]" if re_ else "]"))
            print_words(m)
        print()


def listen(base: str, sender: str, about: str | None, box: str | None, channels: list[str], idle: bool,
           bang: bool = False) -> None:
    """Wait for anything new in the inbox (if box) and the channels; print one batch and exit. idle: the caller
    does nothing else meanwhile, so its holder no longer counts as working on the owner's last message. bang: this
    process is the session that runs the owner's bang commands, for as long as it waits; a command's result alone
    doesn't end the wait."""
    if not bang:
        return _listen(base, sender, about, box, channels, idle)
    session = secrets.token_hex(8)
    client = Client(PORT, TOKEN)

    def register() -> None:
        client.post("/api/sessions", {"name": sender, "session": session, "pid": os.getpid(), "line": box,
                                      "bang": True})

    try:
        register()
    except NanoteaError as err:
        sys.exit(f"nanotea-tell: {err}")
    host = Host(client, sender, session, register)
    host.start()
    try:
        _listen(base, sender, about, box, channels, idle)
    finally:
        host.stop()
        host.finish(None)
        try:
            client.post(f"/api/sessions/{session}/close", {})
        except NanoteaError as err:
            print(f"nanotea-tell: closing the session failed: {err}", file=sys.stderr)


def _listen(base: str, sender: str, about: str | None, box: str | None, channels: list[str], idle: bool) -> None:
    query = urllib.parse.urlencode({"name": sender})
    inbox_query = urllib.parse.urlencode({"name": sender, **({"idle": 1} if idle else {})})
    api = f"{base}/api/{inbox_segment(box)}" if box else None
    if api:
        info = request(api, {"name": sender, "about": about, "open": True})
        print(f"nanotea-tell: {sender} holds {OWNER}'s {box} inbox as listener pid {os.getpid()}; "
              f"they write at {info['url']}", file=sys.stderr)
    for channel in channels:
        info = request(f"{base}/api/channels/{channel}")
        print(f"nanotea-tell: {sender} listens to #{channel} as listener pid {os.getpid()}; {info['url']}",
              file=sys.stderr)
    print(f"nanotea-tell: set a status {OWNER} sees: nanotea-tell --from {json.dumps(sender)} --status \"...\"",
          file=sys.stderr)
    where = f"--inbox {box}" if box else f"--channel {channels[0]}"
    print(f"nanotea-tell: show {OWNER} you're typing before a long write: nanotea-tell --from {json.dumps(sender)} "
          f"--typing {where}", file=sys.stderr)
    listener = f"pid {os.getpid()}"
    while True:
        got = False
        if api:
            pending = request(f"{api}/pending?{inbox_query}", patient=True)
            if pending and all(m.get("bang") for m in pending):
                pending = []  # results of commands wait for something that is for the agent
            if pending:
                print_inbox(pending)
                request(f"{api}/delivered", {"name": sender, "ids": [m["id"] for m in pending], "listener": listener})
                got = True
        for channel in channels:
            ch = f"{base}/api/channels/{channel}"
            pending = request(f"{ch}/pending?{query}", patient=True)
            if pending:
                print_channel(channel, pending)
                request(f"{ch}/delivered", {"name": sender, "ids": [m["id"] for m in pending], "listener": listener})
                got = True
        if got:
            return
        if version.stale(SERVICE["version"]):
            # Ends like a batch, so whoever runs the listener starts it again, on the new code.
            print(f"[upgraded] nanotea was upgraded from version {version.RUNNING} to {SERVICE['version']}. Start "
                  f"this listener again to run the new code.\n")
            return
        time.sleep(3)


def set_status(base: str, sender: str, text: str) -> None:
    out = request(f"{base}/api/status", {"name": sender, "text": text})
    if out["status"]:
        print(f"nanotea-tell: status of {out['name']} set: {out['status']['text']}", file=sys.stderr)
    else:
        print(f"nanotea-tell: status of {out['name']} cleared", file=sys.stderr)


def list_channels(base: str) -> None:
    for c in request(f"{base}/api/channels"):
        listening = f"  listening: {', '.join(c['listening'])}" if c["listening"] else ""
        print(f"#{c['name']:<14} {c['topic'] or '-'}{listening}")


def arrange(base: str, sender: str, spec: str) -> None:
    """spec: "" to print the board's arrangement; JSON, or - for stdin, to change it as sender."""
    if spec == "":
        return print(json.dumps(request(f"{base}/api/board")["arrangement"], indent=2))
    raw = sys.stdin.read() if spec == "-" else spec
    try:
        patch = json.loads(raw)
    except json.JSONDecodeError as err:
        sys.exit(f"nanotea-tell: --arrange is not JSON: {err}")
    if not isinstance(patch, dict):
        sys.exit('nanotea-tell: --arrange takes an object: {"groups": [...], "hidden": [...]}')
    print(json.dumps(request(f"{base}/api/board/arrange", {**patch, "name": sender}), indent=2))


def board(base: str, stale: int | None) -> None:
    """The status board, grouped as the owner's sidebar: each line's agent, whether it counts as connected and why (a
    listener holding it, responding, typing, or a listener poll within the hour) or when it was last seen, and its
    beacon. Then beacons with no line. stale: only the agents whose beacon is older than that many minutes or
    missing: connected ones first, each oldest first."""
    out = request(f"{base}/api/board")
    now = datetime.fromisoformat(out["now"])

    def ago(iso: str) -> str:
        at = datetime.fromisoformat(iso).astimezone()
        s = max(0, (now - at).total_seconds())
        n = "just now" if s < 60 else f"{s // 60:.0f}m ago" if s < 3600 else (
            f"{s // 3600:.0f}h ago" if s < 86400 else f"{s // 86400:.0f}d ago")
        return f"{n} ({at:%a %H:%M})"

    def state(r: dict) -> str:
        if r["connected"]:
            why = [w for w, on in (("listening", r["listening"]), ("responding", r["responding"]),
                                   ("typing", r["typing"])) if on]
            return "connected: " + (", ".join(why) or f"listened {ago(r['listened'])}")
        return f"not connected, last seen {ago(r['seen'])}" if r["seen"] else "not connected, never seen"

    def beacon(b: dict | None) -> str:
        return f"beacon {ago(b['at'])}: {b['text']}" if b else "no beacon"

    def who(r: dict) -> str:
        return r["name"] if r["agent"] in (None, r["name"]) else f"{r['name']} ({r['agent']})"

    rows = [(g["name"], r) for g in out["sidebar"]["groups"] for r in g["rows"]]
    shown = {r["agent"] for _, r in rows}
    loose = [(name, b) for name, b in sorted(out["beacons"].items()) if name not in shown]
    if stale is not None:
        cut = now.timestamp() - stale * 60
        late = [(group, r) for group, r in rows if r["agent"] is not None
                and (r["status"] is None or datetime.fromisoformat(r["status"]["at"]).timestamp() < cut)]
        late.sort(key=lambda x: (not x[1]["connected"], x[1]["status"]["at"] if x[1]["status"] else ""))
        late_loose = [(name, b) for name, b in loose if datetime.fromisoformat(b["at"]).timestamp() < cut]
        if not late and not late_loose:
            return print(f"every agent's beacon is newer than {stale} minutes")
        width = max(len(who(r)) for _, r in late) if late else 0
        for group, r in late:
            where = "" if group == "Not connected" else f"; in {group}"
            print(f"{who(r):<{width}}  {beacon(r['status'])}  [{state(r)}{where}; {r['path']}]")
        for name, b in late_loose:
            print(f"{name:<{width}}  {beacon(b)}  [no line]")
        return
    width = max([len(who(r)) for _, r in rows] + [len(n) for n, _ in loose] + [1])
    print("Channels")
    for c in out["sidebar"]["channels"]:
        bits = [x for x, on in (("listening", c["listening"]), (f"typing: {', '.join(c['typing'])}", c["typing"]),
                                (f"{c['unread']} unread", c["unread"])) if on]
        print(f"  #{c['name']:<{width - 1}}  {', '.join(bits) or 'nobody listening'}")
    group = None
    for g, r in rows:
        if g != group:
            group = g
            print(group)
        unread = f"  {r['unread']} unread" if r["unread"] else ""
        print(f"  {who(r):<{width}}  {state(r)}  {beacon(r['status'])}  [{r['path']}]{unread}")
    if loose:
        print("Beacons with no line")
        for name, b in loose:
            print(f"  {name:<{width}}  {beacon(b)}")


def history(base: str, sender: str, box: str, n: int) -> None:
    def stamp(iso: str) -> str:
        return iso[:19].replace("T", " ")

    query = urllib.parse.urlencode({"name": sender, "n": n})
    messages = request(f"{base}/api/{inbox_segment(box)}/history?{query}")
    if not messages:
        return print(f"nanotea-tell: the {box} inbox has no messages", file=sys.stderr)
    for m in messages:
        if m["delivered_at"] is None:
            went = ["not delivered yet"]
        elif m["deliveries"] is None:
            went = [f"delivered {stamp(m['delivered_at'])} to {m['delivered_to']} (listener not recorded)"]
        else:
            went = [f"delivered {stamp(d['at'])} to {d['to']} ({d['by'] or 'listener not recorded'})"
                    for d in m["deliveries"]]
        summary = " ".join(m["summary"].split())
        summary = summary if len(summary) <= 100 else summary[:97] + "..."
        files = f" [+{m['files']} file(s)]" if m["files"] else ""
        print(f"{m['id']}  sent {stamp(m['at'])}  {'; '.join(went)}  {m['kind']}: {summary}{files}")


def wait_delivery(base: str, msg_id: str) -> None:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(2)
        msg = request(f"{base}/api/messages/{msg_id}", patient=True)
        if msg["status"] == "done":
            return
        if msg["status"] == "failed":
            sys.exit(f"nanotea-tell: failed: {msg['error']}")
    sys.exit("nanotea-tell: gave up waiting after 600s")


def main() -> None:
    p = argparse.ArgumentParser(
        description=f"Send {OWNER} a message. The text comes from the argument or stdin. Write it to be read, in "
                    f"markdown; the service makes the spoken version when {OWNER} taps play.")
    p.add_argument("text", nargs="?")
    p.add_argument("--from", dest="sender", default=Path.cwd().name,
                   help="who you are; keep it the same every time (default: current directory name)")
    p.add_argument("--voice", help="your voice; required the first time, then remembered (see --voices)")
    p.add_argument("--title", help="short subject; the service writes one if omitted")
    p.add_argument("--attach", action="append", default=[], metavar="FILE",
                   help="audio, an image, a video (mp4, mov) or a PDF; put [[audio N]], [[image N]], "
                        "[[video N]] or [[pdf N]] in the text where the Nth --attach goes, or it goes at the end")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--ask", action="store_true",
                      help=f"wait for {OWNER}'s reply and print it; can take hours, so run it in the background")
    mode.add_argument("--wait", action="store_true", help="wait until the message is delivered")
    p.add_argument("--raw", action="store_true",
                   help=f"with --ask: ask {OWNER} to record the answer raw, the microphone's processing off (a "
                        "voice sample, an instrument); [take] lines say what the microphone did")
    p.add_argument("--control", metavar="JSON",
                   help=f'something for {OWNER} to tap, e.g. \'{{"type": "checklist", "items": ["Tests", "Docs"]}}\' '
                        "(see --controls); on --ask the tap is the answer")
    p.add_argument("--choice", action="append", default=[], metavar="LABEL",
                   help=f"a button for {OWNER} to tap; repeat for each (a choice control)")
    mode.add_argument("--controls", action="store_true", help="list the controls and their specs, then exit")
    mode.add_argument("--wait-reply", metavar="ID", help="wait for the reply to an earlier --ask")
    mode.add_argument("--voices", action="store_true", help="list voices and who has each, then exit")
    mode.add_argument("--listen", action="store_true",
                      help=f"wait for {OWNER}'s messages in an inbox you hold (--inbox) and new posts in channels "
                           "(--channel); prints them and exits, so run it in the background and again after each "
                           "batch. With neither, holds the main inbox")
    mode.add_argument("--close-inbox", action="store_true", help="inbox holder only: release the inbox")
    mode.add_argument("--react", nargs=2, metavar=("ID", "EMOJI"),
                      help=f"react to {OWNER}'s message ID (from --listen) with an emoji: in your inbox, or with "
                           "--channel, their post in that channel")
    mode.add_argument("--history", nargs="?", const=20, type=int, metavar="N",
                      help="inbox holder only: the last N (default 20) messages to your inbox, newest first, "
                           "with when and to which listener each was delivered")
    mode.add_argument("--channels", action="store_true", help="list the channels, then exit")
    mode.add_argument("--board", action="store_true",
                      help=f"print the status board, grouped as {OWNER}'s sidebar: each line's agent, connected or when "
                           "last seen, and its beacon; then exit. Read-only")
    mode.add_argument("--arrange", nargs="?", const="", metavar="JSON",
                      help=f"board managers only (the owner names them in Settings): alone, print how {OWNER}'s "
                           "board is arranged, as JSON; with JSON, or - for stdin, change it: {\"groups\": [{\"name\": "
                           "..., \"members\": [names]}], \"hidden\": [names]}, either key alone; each replaces what "
                           "is there. Hidden names stay off the board while not connected with nothing unread")
    mode.add_argument("--typing", action="store_true",
                      help=f"show {OWNER} you're writing to your inbox (--inbox) or in #NAME (--channel): your dot "
                           "pulses until you send there, or for 3 minutes")
    mode.add_argument("--event", metavar="KIND",
                      help="report an event (e.g. round-done) to the inbox holder: the text is its summary and "
                           f"--from its source; it's shown as an event, never as {OWNER}'s words")
    p.add_argument("--status", metavar="TEXT",
                   help=f"what you're doing, one line of at most 140 characters, shown to {OWNER} beside your name "
                        "until you change it; \"\" clears it. Sends nothing to them. Alone, or with --listen to set "
                        "it and then listen")
    p.add_argument("--stale", type=int, metavar="MINUTES",
                   help="with --board: only the agents whose beacon is older than MINUTES or missing, connected "
                        "ones first, each oldest first")
    p.add_argument("--about", help=f"with --listen: what you're working on, shown to {OWNER} when they write")
    p.add_argument("--idle", action="store_true",
                   help=f"with --listen: you do nothing else until it returns (it runs in the foreground), so "
                        f"{OWNER} sees you idle rather than still working on their last message")
    p.add_argument("--bang", action="store_true",
                   help=f"with --listen --inbox NAME: run the commands {OWNER} types with ! on your line, here, while "
                        "you wait (they must also be on in Settings). Only whoever starts the listener can give "
                        "this. A command's result is printed with the next batch and doesn't end the wait")
    p.add_argument("--inbox", metavar="NAME",
                   help="with --listen, --close-inbox, --history, --react, or --event: main (the default), or any other "
                        f"name (lowercase, digits, dashes) for your own direct line to {OWNER}, opened by your first "
                        "--listen")
    p.add_argument("--re", metavar="ID",
                   help="the message this answers (its id): it goes in that message's thread, where the thread is")
    p.add_argument("--channel", action="append", default=[], metavar="NAME",
                   help=f"post to #NAME instead of to {OWNER} directly; with --listen (repeatable), also wait for new "
                        "posts there; with --react, react in #NAME. See --channels")
    a = p.parse_args()
    a.channel = [c.removeprefix("#") for c in a.channel]
    for c in a.channel:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", c):
            p.error(f"channel names are lowercase letters, digits and dashes, not {c!r}")
    if a.raw and not a.ask:
        p.error("--raw goes with --ask")
    if a.control and a.choice:
        p.error("--control or --choice, not both")
    control = None
    if a.control:
        try:
            control = json.loads(a.control)
        except json.JSONDecodeError as err:
            p.error(f"--control is not JSON: {err}")
    elif a.choice:
        control = {"type": "choice", "options": a.choice}
    if len(a.channel) > 1 and not a.listen:
        p.error("more than one --channel only goes with --listen")
    if a.typing and (a.text is not None or a.attach or a.title or a.voice or a.status is not None
                     or (a.channel and a.inbox)):
        p.error("--typing goes alone, with --inbox or one --channel")
    if a.channel and (a.close_inbox or a.history is not None or a.event or a.voices or a.channels or a.wait_reply):
        p.error("--channel goes with sending, --listen, or --react")
    if a.channel and a.inbox and not a.listen:
        p.error("--inbox and --channel together only go with --listen")
    listen_box = a.inbox or (None if a.channel else "main")
    if a.about is not None and a.listen and not listen_box:
        p.error("--about goes with --listen on an inbox; channels have no --about")
    if a.status is not None and (a.text is not None or a.attach or a.title or a.voice or a.ask or a.wait
                                 or a.wait_reply or a.voices or a.close_inbox or a.react or a.history is not None
                                 or a.channels or a.event or (a.channel and not a.listen)):
        p.error("--status goes alone or with --listen")
    if a.stale is not None and not a.board:
        p.error("--stale goes with --board")
    if a.idle and not a.listen:
        p.error("--idle goes with --listen")
    if a.bang and not (a.listen and listen_box and listen_box != "main"):
        p.error("--bang goes with --listen --inbox NAME: commands run on an agent's own line, not the main inbox")
    if a.arrange is not None and (a.text is not None or a.attach or a.title or a.voice or a.status is not None
                                  or a.channel or a.inbox or a.about is not None):
        p.error("--arrange goes alone")
    if a.board and (a.text is not None or a.attach or a.title or a.voice or a.status is not None or a.channel
                    or a.inbox or a.about is not None):
        p.error("--board goes alone, or with --stale")
    a.inbox = a.inbox or "main"

    global TOKEN
    try:
        # Without a token file, it asks for a token and waits for the approver or the owner to approve it.
        TOKEN = obtain(_cfg, "events" if a.event else "agent", a.sender, a.inbox if a.event else None,
                       APPROVAL_WAIT_S)
    except NanoteaError as err:  # CredentialError is one
        sys.exit(f"nanotea-tell: {err}")
    base = f"http://127.0.0.1:{PORT}"
    if a.status is not None:
        set_status(base, a.sender, a.status)
        if not a.listen:
            return
    if a.voices:
        return list_voices(base)
    if a.controls:
        for c in request(f"{base}/api/controls"):
            print(f"{c['type']:<12} {c['about']}")
        return
    if a.wait_reply:
        return wait_reply(base, a.wait_reply)
    if a.channels:
        return list_channels(base)
    if a.board:
        return board(base, a.stale)
    if a.arrange is not None:
        return arrange(base, a.sender, a.arrange)
    if a.listen:
        return listen(base, a.sender, a.about, listen_box, a.channel, a.idle, a.bang)
    if a.typing:
        where = f"channels/{a.channel[0]}" if a.channel else inbox_segment(a.inbox)
        out = request(f"{base}/api/{where}/typing", {"name": a.sender})
        return print(f"nanotea-tell: {a.sender} shows as typing in {out['where']} for up to {out['for_s'] // 60} "
                     f"minutes, or until you send there", file=sys.stderr)
    if a.close_inbox:
        request(f"{base}/api/{inbox_segment(a.inbox)}", {"name": a.sender, "open": False})
        return print(f"nanotea-tell: {a.sender} released {OWNER}'s {a.inbox} inbox", file=sys.stderr)
    if a.history is not None:
        return history(base, a.sender, a.inbox, a.history)
    if a.react and a.channel:
        msg_id, emoji = a.react
        request(f"{base}/api/channels/{a.channel[0]}/react", {"name": a.sender, "id": msg_id, "emoji": emoji})
        return print(f"nanotea-tell: reacted {emoji} to {msg_id} in #{a.channel[0]}", file=sys.stderr)
    if a.react:
        msg_id, emoji = a.react
        request(f"{base}/api/{inbox_segment(a.inbox)}/react", {"name": a.sender, "id": msg_id, "emoji": emoji})
        return print(f"nanotea-tell: reacted {emoji} to {msg_id}", file=sys.stderr)
    if a.event:
        if not a.text:
            p.error("--event needs the summary as the text")
        out = request(f"{base}/api/{inbox_segment(a.inbox)}/events",
                      {"kind": a.event, "source": a.sender, "summary": a.text})
        return print(f"nanotea-tell: event {out['id']} sent to the {a.inbox} inbox", file=sys.stderr)

    text = a.text if a.text is not None else sys.stdin.read()
    if not text.strip():
        p.error("empty message")
    attachments = []
    for name in a.attach:
        try:
            data = Path(name).read_bytes()
        except OSError as e:
            p.error(f"cannot read {name}: {e.strerror}")
        attachments.append({"name": Path(name).name, "data": base64.b64encode(data).decode()})

    out = request(f"{base}/api/messages", {"text": text, "from": a.sender, "title": a.title, "voice": a.voice,
                                           "ask": a.ask, "raw": a.raw, "attachments": attachments,
                                           **({"control": control} if control is not None else {}),
                                           **({"re": a.re} if a.re else {}),
                                           **({"channel": a.channel[0]} if a.channel else {})})
    print(out["url"])
    if a.ask:
        print(f"nanotea-tell: waiting for {OWNER}'s reply (resume with --wait-reply {out['id']})", file=sys.stderr)
        wait_reply(base, out["id"])
    elif a.wait:
        wait_delivery(base, out["id"])


if __name__ == "__main__":
    main()
