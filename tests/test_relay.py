"""Upgrades: a session's nanotea mcp keeps its tools across a service upgrade, through the relay.

The service and the MCP server run from a copy of the package, so the test can change the code on disk as a deploy
does, then restart the service on it."""

import http.client
import json
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import harness
from harness import ROOT, Case

LINE = "A line the upgrade adds to the agreement."


class Relay(Case):
    PORT_OFFSET = 21

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        harness.stop(cls.proc, cls.reader)
        cls.pkg = Path(cls.tmp.name) / "pkg"
        shutil.copytree(ROOT / "nanotea", cls.pkg / "nanotea", ignore=shutil.ignore_patterns("__pycache__"))
        server = cls.pkg / "nanotea" / "mcp_server.py"
        # The heartbeat finds the upgrade. A wait polls too slowly to meet the restart, so it is still in flight.
        code = server.read_text()
        for was, now in (("HEARTBEAT_S = 20\n", "HEARTBEAT_S = 1\n"), ("POLL_S = 2\n", "POLL_S = 600\n")):
            assert was in code, was
            code = code.replace(was, now)
        server.write_text(code)
        cls.env = {**cls.env, "PYTHONPATH": str(cls.pkg)}
        cls.restart()

    @classmethod
    def restart(cls):
        if cls.proc.poll() is None:
            harness.stop(cls.proc, cls.reader)
        cls.proc, cls.log, cls.reader = harness.spawn(cls.env, cls.tmp.name)
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/api/voices")
                version = conn.getresponse().getheader("X-Nanotea-Version")
                conn.close()
                return version
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("service did not start")

    def start(self, name):
        self.token(name)
        relay = subprocess.Popen([sys.executable, "-m", "nanotea", "mcp", "--name", name], env=self.env_for(name),
                                 cwd=self.tmp.name, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        lines, errors = queue.Queue(), []
        threading.Thread(target=lambda: [lines.put(json.loads(x)) for x in relay.stdout], daemon=True).start()
        threading.Thread(target=lambda: errors.extend(x.decode() for x in relay.stderr), daemon=True).start()
        self.addCleanup(self.finish, relay)
        return relay, lines, errors

    def finish(self, relay):
        if relay.poll() is None:
            relay.kill()
        relay.wait(timeout=30)
        relay.stdout.close()
        relay.stderr.close()

    def send(self, relay, msg):
        relay.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}).encode() + b"\n")
        relay.stdin.flush()

    def read(self, lines, timeout_s=30):
        try:
            return lines.get(timeout=timeout_s)
        except queue.Empty:
            self.fail(f"nothing from the relay in {timeout_s} s")

    def test_an_upgrade_keeps_the_session(self):
        relay, lines, errors = self.start("Uma")
        self.send(relay, {"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        init = self.read(lines)
        self.assertEqual(init["id"], 1, init)
        self.assertNotIn(LINE, init["result"]["instructions"])
        self.assertTrue(init["result"]["capabilities"]["tools"]["listChanged"])
        self.send(relay, {"method": "notifications/initialized"})
        self.send(relay, {"id": 2, "method": "tools/call", "params": {"name": "wait", "arguments": {"timeout_s": 120}}})

        # Mid-deploy: the code on disk is new, the service still old. Nothing hands over.
        sop = self.pkg / "nanotea" / "sop.py"
        sop.write_text(sop.read_text().replace("LINES = [\n", f'LINES = [\n    (None, "{LINE}"),\n'))
        time.sleep(3)
        self.assertTrue(lines.empty(), lines.queue)
        self.assertFalse(any("handing over" in e for e in errors), errors)

        new = self.restart()
        failed = self.read(lines)
        self.assertEqual(failed["id"], 2, failed)
        self.assertIn("nanotea was upgraded during this tools/call; make it again", failed["error"]["message"])
        self.assertEqual(self.read(lines), {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

        self.send(relay, {"id": 3, "method": "tools/call", "params": {"name": "check", "arguments": {}}})
        out = self.read(lines)
        self.assertEqual(out["id"], 3, out)
        items = out["result"]["structuredContent"]["items"]
        self.assertEqual([(i["kind"], i["new"]) for i in items], [("upgraded", new)], items)
        self.assertNotEqual(items[0]["old"], new)
        self.assertIn(LINE, items[0]["instructions"])
        self.assertIn("The working agreement changed", items[0]["text"])

        # Taken once. With one session per agent on, the new server joining shows the old one closed.
        self.send(relay, {"id": 4, "method": "tools/call", "params": {"name": "check", "arguments": {}}})
        out = self.read(lines)
        self.assertEqual((out["id"], out["result"]["structuredContent"]["items"]), (4, []), out)

        relay.stdin.close()
        self.assertEqual(relay.wait(timeout=30), 0, errors)
        self.assertTrue(any("handing over" in e for e in errors), errors)
        # The restarted service forgot the session; the old server handed over rather than register on new code.
        self.assertFalse(any("registered it again" in e for e in errors), errors)

    def test_listeners_hand_over(self):
        self.token("Wes")
        self.assertEqual(self.local("POST", "/api/dm-wes", {"name": "Wes", "open": True})[0], 200)
        listener = subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", "Wes", "--listen", "--inbox",
                                     "wes"], env=self.env, cwd=self.tmp.name, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        rewake = subprocess.Popen([sys.executable, "-m", "nanotea", "hook", "rewake", "--name", "Wes"],
                                  env=self.env, cwd=self.tmp.name, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        for proc in (listener, rewake):
            self.addCleanup(lambda p=proc: (p.poll() is None and p.kill(), p.communicate()))
        time.sleep(4)
        self.assertIsNone(listener.poll())
        markdown = self.pkg / "nanotea" / "markdown.py"
        markdown.write_text(markdown.read_text() + "\n# upgraded\n")
        new = self.restart()

        out, err = listener.communicate(timeout=30)
        self.assertEqual(listener.returncode, 0, err)
        self.assertIn("[upgraded] nanotea was upgraded from version", out)
        self.assertIn(f"to {new}. Start this listener again", out)

        # The wake hook goes on waiting, as itself on the new code: same process, the time it has left.
        def again():
            command = subprocess.run(["ps", "-o", "command=", "-p", str(rewake.pid)], capture_output=True,
                                     text=True).stdout
            return "--max-s" in command
        self.wait_for(again, "the wake hook to start again on the new code")
        self.assertIsNone(rewake.poll())
        self.assertEqual(self.call("POST", "/api/dm-wes/messages", {"text": "wake up"})[0], 200)
        out, err = rewake.communicate(timeout=30)
        self.assertEqual(rewake.returncode, 2, err)
        self.assertIn("1 message(s) from Robin waiting", err)

    def begin(self, name):
        """A relay with its session joined and a wait in flight."""
        relay, lines, errors = self.start(name)
        self.send(relay, {"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(self.read(lines)["id"], 1)
        self.send(relay, {"method": "notifications/initialized"})
        self.send(relay, {"id": 2, "method": "tools/call", "params": {"name": "wait", "arguments": {"timeout_s": 120}}})
        self.wait_for(lambda: any(f"of {name} opened" in x for x in self.log), f"{name}'s session to open")
        time.sleep(1)
        return relay, lines, errors

    def closed(self, name):
        self.wait_for(lambda: any(f"of {name} closed" in x for x in self.log), f"{name}'s session to close")

    def test_closing_stdin_ends_a_wait(self):
        relay, lines, errors = self.begin("Xan")
        start = time.monotonic()
        relay.stdin.close()
        self.assertEqual(relay.wait(timeout=10), 0, errors)
        self.assertLess(time.monotonic() - start, 5)
        self.closed("Xan")
        self.assertFalse(any("Fatal Python error" in e for e in errors), errors)

    def test_a_stop_closes_the_session(self):
        # As Claude Code stops a server: SIGINT, then SIGTERM 100 ms on if it hasn't exited.
        relay, lines, errors = self.begin("Yul")
        start = time.monotonic()
        relay.send_signal(signal.SIGINT)
        time.sleep(0.1)
        relay.send_signal(signal.SIGTERM)
        self.assertEqual(relay.wait(timeout=10), 0, errors)
        self.assertLess(time.monotonic() - start, 5)
        self.closed("Yul")
        self.assertFalse(any("Fatal Python error" in e or "Traceback" in e for e in errors), errors)

    def test_a_server_that_fails_at_start_ends_the_relay(self):
        # With stdin held open, as a harness holds it: the thread reading it is still in a read at exit.
        relay = subprocess.Popen([sys.executable, "-m", "nanotea", "mcp", "--wait-s", "0"], env=self.env,
                                 cwd=self.tmp.name, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(relay.stdin.close)
        self.assertEqual(relay.wait(timeout=30), 2)
        err = relay.stderr.read()
        relay.stdout.close()
        relay.stderr.close()
        self.assertIn("--wait-s must be 1 to", err)
        self.assertNotIn("Fatal Python error", err)
