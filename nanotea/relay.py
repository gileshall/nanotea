"""nanotea mcp: a relay between the harness and the MCP server, so an upgrade doesn't cost a session its tools.

The harness starts this and speaks MCP to it over stdio (JSON-RPC, a message a line). It runs the server
(nanotea.mcp_server) as a child and passes every line through. When the child exits with EXIT_UPGRADED (the
service runs newer code: docs/service.md#upgrading), the relay starts a new child on the code now on disk,
replays the session's initialize to it, fails the calls that were in flight with an error that says to make them
again, and tells the harness the tools changed. Any other exit ends the relay with the child's exit code.

Standard library only, and kept small: a session keeps this process, and so this code, for good.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading

EXIT_UPGRADED = 75
STATE_ENV = "NANOTEA_RELAY_STATE"  # a file the old child leaves for the new: versions and its instructions
REPLAY_ID = "nanotea-relay-initialize"
REPLAY_TIMEOUT_S = 60


def say(text: str) -> None:
    print(f"nanotea mcp relay: {text}", file=sys.stderr, flush=True)


def parse(line: bytes) -> dict | None:
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


class Relay:
    def __init__(self, argv: list[str], state: str):
        self.argv = argv
        self.state = state
        self.gate = threading.Lock()  # held to write to the child, and while one child replaces another
        self.book = threading.Lock()  # pending, asked, init
        self.out = threading.Lock()
        self.child: subprocess.Popen | None = None
        self.reader: threading.Thread | None = None
        self.pending: dict = {}  # the harness's requests the child hasn't answered: id -> method
        self.asked: set = set()  # the child's requests the harness hasn't answered
        self.init: bytes | None = None
        self.initialized: bytes | None = None
        self.replayed = threading.Event()
        self.replay_error: dict | None = None
        self.closing = False

    def emit(self, line: bytes) -> None:
        with self.out:
            sys.stdout.buffer.write(line if line.endswith(b"\n") else line + b"\n")
            sys.stdout.buffer.flush()

    def spawn(self) -> None:
        self.child = subprocess.Popen([sys.executable, "-m", "nanotea.mcp_server", *self.argv],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      env={**os.environ, STATE_ENV: self.state}, start_new_session=True)
        self.reader = threading.Thread(target=self.from_child, args=(self.child,), daemon=True)
        self.reader.start()

    def from_child(self, child: subprocess.Popen) -> None:
        for line in child.stdout:
            if not line.endswith(b"\n"):
                say(f"the server exited part way through a line; dropped {line[:200]!r}")
                break
            msg = parse(line)
            if msg is not None and "method" not in msg and "id" in msg:
                if msg["id"] == REPLAY_ID:
                    self.replay_error = msg.get("error")
                    self.replayed.set()
                    continue
                with self.book:
                    method = self.pending.pop(msg["id"], None)
                if method == "initialize" and "result" in msg:
                    # The relay sends notifications/tools/list_changed after an upgrade, so it says it will.
                    msg["result"].setdefault("capabilities", {}).setdefault("tools", {})["listChanged"] = True
                    line = json.dumps(msg).encode() + b"\n"
            elif msg is not None and "id" in msg:
                with self.book:
                    self.asked.add(msg["id"])
            self.emit(line)

    def from_harness(self) -> None:
        for line in sys.stdin.buffer:
            msg = parse(line)
            # Under the gate throughout, so a line is either for the old child (and failed with it) or the new.
            with self.gate:
                if not self.note(msg, line):
                    continue
                try:
                    self.child.stdin.write(line)
                    self.child.stdin.flush()
                except (BrokenPipeError, ValueError):
                    # The child is going: a request fails once it has; anything else is lost.
                    if msg is None or "id" not in msg or "method" not in msg:
                        say(f"the server exited before this reached it: {line[:200]!r}")
        with self.gate:
            self.shut()

    def note(self, msg: dict | None, line: bytes) -> bool:
        """Keeps what a handover needs from the harness's line; False if the line has nowhere to go."""
        with self.book:
            if msg is not None and "method" in msg:
                if msg["method"] == "initialize":
                    self.init = line
                elif msg["method"] == "notifications/initialized":
                    self.initialized = line
                elif msg["method"] == "notifications/cancelled":
                    if (msg.get("params") or {}).get("requestId") not in self.pending:
                        return False  # a call the upgrade already failed
                if "id" in msg:
                    self.pending[msg["id"]] = msg["method"]
            elif msg is not None and "id" in msg:
                if msg["id"] not in self.asked:
                    say(f"a reply to a request from the server before the upgrade, dropped: {line[:200]!r}")
                    return False
                self.asked.discard(msg["id"])
        return True

    def hand_over(self) -> None:
        """The child exited for an upgrade: fail what it left unanswered, start the next, and replay the handshake."""
        with self.book:
            failed, self.pending = self.pending, {}
            self.asked.clear()
        for rid, method in failed.items():
            self.emit(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32000, "message": f"nanotea was upgraded during this {method}; make it again"}}).encode())
        self.spawn()
        if self.init is not None:
            replay = json.loads(self.init)
            replay["id"] = REPLAY_ID
            self.replayed.clear()
            self.child.stdin.write(json.dumps(replay).encode() + b"\n")
            self.child.stdin.flush()
            if not self.replayed.wait(REPLAY_TIMEOUT_S):
                raise RuntimeError(f"the upgraded server didn't answer initialize within {REPLAY_TIMEOUT_S} s")
            if self.replay_error is not None:
                raise RuntimeError(f"the upgraded server refused initialize: {self.replay_error}")
            if self.initialized is not None:
                self.child.stdin.write(self.initialized)
                self.child.stdin.flush()
        self.emit(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}).encode())
        say("the server was upgraded; the session goes on")

    def shut(self) -> None:
        """Ends the session: the child sees its stdin close, closes the session and exits, and so does the relay."""
        self.closing = True
        try:
            self.child.stdin.close()
        except BrokenPipeError:
            pass  # the child has gone already; its exit ends the relay

    def stop(self, signum, frame) -> None:
        """The harness stops the relay (Claude Code: SIGINT, then SIGTERM 100 ms on). Not passed on as a signal:
        an interrupted server waits on its thread reading stdin, which only an end of stdin frees. The child has
        its own process group, so a signal to the harness's group doesn't reach it either."""
        if self.child is not None:
            self.shut()

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.spawn()
        threading.Thread(target=self.from_harness, daemon=True).start()
        while True:
            code = self.child.wait()
            self.reader.join()
            with self.gate:
                if code != EXIT_UPGRADED or self.closing:
                    return code
                try:
                    self.hand_over()
                except (RuntimeError, OSError) as err:
                    say(f"the upgrade failed, so this session's nanotea tools are gone: {err}. Reconnect the MCP "
                        f"server (Claude Code: /mcp)")
                    self.child.kill()
                    return 1
                if self.closing:  # stopped during the handover
                    self.shut()


def main(argv: list[str]) -> None:
    fd, state = tempfile.mkstemp(prefix="nanotea-relay-", suffix=".json")
    os.close(fd)
    try:
        code = Relay(argv, state).run()
    finally:
        os.unlink(state)
    # Not sys.exit: the thread reading stdin holds its lock, and finalizing the interpreter under it aborts.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
