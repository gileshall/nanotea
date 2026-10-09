"""A request that fails unexpectedly is answered 500 with the error, not dropped: dropped, a proxy shows a bare 502."""

import contextlib
import http.client
import io
import json
import threading
import time
import unittest
from types import SimpleNamespace

from nanotea.server import Handler, Server


class Boom(Handler):
    def _do_get(self) -> None:
        if self.path == "/answered":
            self._send(200, "text/plain", b"ok")
        raise IndexError("list index out of range")

    _do_post = _do_get


class Unanswered(unittest.TestCase):
    def setUp(self):
        self.srv = Server.__new__(Server)
        super(Server, self.srv).__init__(("127.0.0.1", 0), Boom)
        self.srv.app = SimpleNamespace(proxies=())
        self.stderr = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.stderr)
        self.redirect.__enter__()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.redirect.__exit__(None, None, None)

    def get(self, method, url):
        c = http.client.HTTPConnection(*self.srv.server_address, timeout=10)
        c.request(method, url, body=b"{}" if method == "POST" else None)
        r = c.getresponse()
        return r.status, r.getheader("Content-Type"), r.read().decode()

    def test_an_error_is_answered_and_logged(self):
        status, ctype, body = self.get("GET", "/main")
        self.assertEqual((status, ctype), (500, "text/plain; charset=utf-8"))
        self.assertTrue(body.startswith("nanotea failed on GET /main: IndexError: list index out of range\n"), body)
        status, ctype, body = self.get("POST", "/api/x?k=secret")
        self.assertEqual((status, ctype), (500, "application/json"))
        self.assertEqual(json.loads(body)["error"], "nanotea failed on POST /api/x: IndexError: list index out of range")
        self.assertEqual((self.get("GET", "/answered")[0], self.get("GET", "/answered")[2]), (200, "ok"))
        deadline = time.monotonic() + 10  # the traceback is logged after the answer is sent
        while self.stderr.getvalue().count("IndexError: list index out of range") < 4 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.stderr.getvalue().count("IndexError: list index out of range"), 4)


if __name__ == "__main__":
    unittest.main()
