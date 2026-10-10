"""The client's outlast mode: a listener waits out a service that is down, however long."""

import contextlib
import io
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from nanotea import client
from nanotea.client import Client, NanoteaError


class Answer(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Outlast(unittest.TestCase):
    def test_without_it_a_down_service_fails_at_once(self):
        with self.assertRaises(NanoteaError) as caught:
            Client(free_port(), "t").get("/x")
        self.assertIsNone(caught.exception.status)

    def test_a_listener_waits_until_the_service_is_back(self):
        port = free_port()
        servers = []

        def come_up():
            time.sleep(1)
            servers.append(HTTPServer(("127.0.0.1", port), Answer))
            servers[0].serve_forever()

        threading.Thread(target=come_up, daemon=True).start()
        err = io.StringIO()
        try:
            with mock.patch.object(client, "OUTLAST_POLL_S", 0.1), contextlib.redirect_stderr(err):
                self.assertEqual(Client(port, "t", outlast=True).get("/x"), {"ok": True})
        finally:
            for s in servers:
                s.shutdown()
                s.server_close()
        said = err.getvalue().splitlines()
        self.assertEqual(len(said), 2, said)
        self.assertIn("waiting until it is back", said[0])
        self.assertIn(f"service back at http://127.0.0.1:{port}", said[1])

    def test_a_refusal_is_not_waited_out(self):
        class Refuse(Answer):
            def do_GET(self):
                self.send_error(403, "no")

        server = HTTPServer(("127.0.0.1", 0), Refuse)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(NanoteaError) as caught:
                Client(server.server_address[1], "t", outlast=True).get("/x")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(caught.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
