"""Asking for a token: requests, the approver, and collecting the token once approved."""

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from harness import PROXY, Case
from nanotea import credentials
from nanotea.enroll import EnrollBook
from nanotea.tokens import TokenBook, TokenError


class Book(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tokens = TokenBook(self.root)
        self.book = EnrollBook(self.root, self.tokens)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_request_is_collected_once_approved_and_only_with_its_secret(self):
        req, secret = self.book.ask("agent", "ann", None, "/work/ann", "127.0.0.1")
        self.assertEqual(stat.S_IMODE((self.root / "enroll.json").stat().st_mode), 0o600)
        self.assertNotIn(secret, (self.root / "enroll.json").read_text())
        with self.assertRaises(TokenError) as err:
            self.book.collect(req["id"], "wrong")
        self.assertEqual(err.exception.status, 404)
        self.assertEqual(self.book.collect(req["id"], secret), ({**req}, None))
        self.book.decide(req["id"], True, "owner", None)
        done, token = self.book.collect(req["id"], secret)
        self.assertEqual(done["state"], "collected")
        self.assertEqual(self.tokens.check(token)["name"], "ann")
        self.assertEqual(self.book.collect(req["id"], secret)[1], None)  # made once
        with self.assertRaises(TokenError) as err:
            self.book.ask("agent", "ANN", None, None, "127.0.0.1")
        self.assertEqual(err.exception.status, 409)

    def test_asking_again_replaces_and_decided_requests_stay_decided(self):
        first, s1 = self.book.ask("agent", "bo", None, None, "127.0.0.1")
        second, s2 = self.book.ask("agent", "bo", None, None, "127.0.0.1")
        self.assertEqual(self.book.collect(first["id"], s1)[0]["state"], "replaced")
        self.assertEqual([r["id"] for r in self.book.waiting()], [second["id"]])
        self.book.decide(second["id"], False, "keeper", "not one of ours")
        out, token = self.book.collect(second["id"], s2)
        self.assertEqual((out["state"], out["by"], out["reason"], token), ("denied", "keeper", "not one of ours", None))
        with self.assertRaises(TokenError) as err:
            self.book.decide(second["id"], True, "owner", None)
        self.assertEqual(err.exception.status, 409)

    def test_an_event_source_names_its_line_and_requests_are_capped(self):
        with self.assertRaises(TokenError):
            self.book.ask("events", "ci", None, None, "127.0.0.1")
        req, secret = self.book.ask("events", "ci", "main", None, "127.0.0.1")
        self.book.decide(req["id"], True, "owner", None)
        self.assertEqual(self.tokens.check(self.book.collect(req["id"], secret)[1])["line"], "main")
        for i in range(19):
            self.book.ask("agent", f"a{i}", None, None, "127.0.0.1")
        self.book.ask("agent", "a0", None, None, "127.0.0.1")  # replacing one is not one more
        self.book.ask("agent", "a19", None, None, "127.0.0.1")
        with self.assertRaises(TokenError) as err:
            self.book.ask("agent", "a20", None, None, "127.0.0.1")
        self.assertEqual(err.exception.status, 429)

    def test_the_approver_follows_its_token(self):
        made, record, _ = self.tokens.issue("agent", "keeper")
        self.assertIsNone(self.tokens.approver())
        self.assertEqual(self.tokens.appoint(record["id"])["name"], "keeper")
        _, again, gone = self.tokens.issue("agent", "keeper", replace=True)
        self.assertEqual(gone, [record["id"]])
        self.assertEqual(self.tokens.approver()["id"], again["id"])
        self.assertEqual(TokenBook(self.root).approver()["id"], again["id"])  # kept in the file
        events = self.tokens.issue("events", "ci", "main")[1]
        with self.assertRaises(TokenError):
            self.tokens.appoint(events["id"])
        self.tokens.revoke(again["id"])
        self.assertIsNone(self.tokens.approver())
        self.assertEqual(self.tokens.revoked_at("agent", "keeper"), TokenBook(self.root).revoked_at("agent", "keeper"))


class Service(Case):
    PORT_OFFSET = 18

    def open_post(self, path, body, headers=None):
        status, out = self.request("POST", path, body, headers)
        return status, json.loads(out)

    def ask(self, name, kind="agent", line=None):
        status, out = self.open_post("/api/enroll", {"kind": kind, "name": name, "line": line, "dir": "/w"})
        self.assertEqual(status, 200, out)
        return out["request"]["id"], out["secret"]

    def collect(self, ident, secret):
        return self.open_post(f"/api/enroll/{ident}/collect", {"secret": secret})

    def appoint(self, ident):
        status, out = self.owner("POST", "/api/approver", {"token": ident})
        self.assertEqual(status, 200, out)

    def test_only_a_program_here_asks_and_only_the_approver_or_owner_decides(self):
        for headers in (PROXY, {"X-Forwarded-For": "203.0.113.9"}, {"Forwarded": "for=203.0.113.9"}):
            status, out = self.open_post("/api/enroll", {"name": "far"}, headers)
            self.assertEqual(status, 403, out)
            self.assertIn("through a proxy", out["error"])
        status, out = self.open_post("/api/enroll", {"name": "near"}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403, out)
        ident, secret = self.ask("Ola")
        # With no approver, the owner is told.
        self.wait_for(lambda: any(n["tag"] == f"enroll-{ident}" for n in self.notes()), "the owner's note")
        self.assertIn("Ola", self.call("GET", "/tokens")[1])
        self.assertEqual(self.collect(ident, secret)[1]["request"]["state"], "pending")
        # Another agent can't decide it, nor see it; nor can the token's would-be holder.
        self.assertEqual(self.local("POST", f"/api/enroll/{ident}/approve", {}, as_="bystander")[0], 403)
        self.assertEqual(self.local("GET", "/api/enroll", as_="bystander")[0], 403)
        self.assertEqual(json.loads(self.local("GET", "/api/enroll?new=1", as_="bystander")[1]), [])
        self.assertEqual(self.request("POST", f"/api/enroll/{ident}/approve", {})[0], 401)
        self.assertEqual(self.owner("POST", "/api/enroll/told", {"ids": [ident]})[0], 403)
        status, out = self.owner("POST", f"/api/enroll/{ident}/approve", {})
        self.assertEqual(status, 200, out)
        status, out = self.collect(ident, secret)
        self.assertEqual(out["request"]["state"], "collected")
        self.assertEqual(self.bearer("GET", "/api/voices", None, out["token"])[0], 200)
        self.assertEqual(self.collect(ident, secret)[1]["token"], None)
        status, out = self.open_post("/api/enroll", {"name": "Ola"})
        self.assertEqual(status, 409, out)

    def test_the_approver_is_told_and_decides_and_never_sees_the_token(self):
        self.token("keeper")
        keeper = next(t for t in json.loads(self.owner("GET", "/api/tokens")[1]) if t["name"] == "keeper")
        self.appoint(keeper["id"])
        try:
            self.assertIn("<b>keeper</b> approves", self.call("GET", "/tokens")[1])
            ident, secret = self.ask("Pip")
            def pips(path):
                return [r["id"] for r in json.loads(self.local("GET", path, as_="keeper")[1]) if r["name"] == "Pip"]
            self.assertEqual(pips("/api/enroll?new=1"), [ident])
            self.assertEqual(self.local("POST", "/api/enroll/told", {"ids": [ident]}, as_="keeper")[0], 200)
            self.assertEqual(pips("/api/enroll?new=1"), [])
            self.assertEqual(pips("/api/enroll"), [ident])
            self.assertFalse(any(n["tag"] == f"enroll-{ident}" for n in self.notes()))
            status, out = self.local("POST", f"/api/enroll/{ident}/approve", {"reason": "ours"}, as_="keeper")
            self.assertEqual(status, 200, out)
            self.assertNotIn("nt_", out)
            self.assertEqual((json.loads(out)["by"], json.loads(out)["state"]), ("keeper", "approved"))
            self.assertTrue(self.collect(ident, secret)[1]["token"].startswith("nt_"))
            # The approver can't choose the approver.
            self.assertEqual(self.local("POST", "/api/approver", {"token": None}, as_="keeper")[0], 403)
        finally:
            self.appoint(None)

    def test_nanotea_tell_without_a_token_waits_for_approval(self):
        file = Path(self.tmp.name) / "tokens" / "quin-ray.token"
        logs = Path(self.tmp.name) / "quin-ray-runs"
        logs.mkdir()
        runs = []
        for n in range(3):
            with open(logs / f"{n}.out", "w") as out, open(logs / f"{n}.err", "w") as err:
                runs.append(subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", "Quin Ray", "--voices"],
                                             env=self.env, cwd=self.tmp.name, stdout=out, stderr=err, text=True))
        waiting = self.wait_for(lambda: [r for r in json.loads(self.owner("GET", "/api/enroll")[1])
                                         if r["name"] == "Quin Ray"], "the request")
        kept = file.parent / "quin-ray.token.request"
        self.wait_for(kept.exists, "the request kept beside the token file")  # written once the service answers
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)
        # Approved before one has started, it would find the name already has a token; all three wait first.
        self.wait_for(lambda: all("waiting on the owner" in (logs / f"{n}.err").read_text() for n in range(3)),
                      "all three waiting")
        self.assertEqual(len([r for r in json.loads(self.owner("GET", "/api/enroll")[1])
                              if r["name"] == "Quin Ray"]), 1)  # programs that start together ask once
        self.assertEqual(self.owner("POST", f"/api/enroll/{waiting[0]['id']}/approve", {})[0], 200)
        for r in runs:
            r.wait(timeout=60)
        outs = [((logs / f"{n}.out").read_text(), (logs / f"{n}.err").read_text()) for n in range(3)]
        self.assertEqual([r.returncode for r in runs], [0, 0, 0], outs)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertFalse((file.parent / "quin-ray.token.request").exists())
        ids = [t["id"] for t in json.loads(self.owner("GET", "/api/tokens")[1]) if t["name"] == "Quin Ray"]
        self.assertEqual(len(ids), 1)
        self.assertTrue(file.read_text().startswith(f"nt_{ids[0]}_"))
        # Denied, it says so and by whom.
        run = subprocess.Popen([sys.executable, "-m", "nanotea.tell", "--from", "Rex", "--voices"], env=self.env,
                               cwd=self.tmp.name, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        (req,) = self.wait_for(lambda: [r for r in json.loads(self.owner("GET", "/api/enroll")[1])
                                        if r["name"] == "Rex"], "Rex's request")
        self.owner("POST", f"/api/enroll/{req['id']}/deny", {"reason": "who is this"})
        _, err = run.communicate(timeout=60)
        self.assertEqual(run.returncode, 1, err)
        self.assertIn("was denied by owner: who is this", err)

    def test_a_token_collected_while_asking_is_taken_not_asked_for(self):
        """A program finds no token file; another collects the token into it before the first asks. Asking would be
        refused, as the name has a token now: the first takes the file's."""
        file = Path(self.tmp.name) / "tokens" / "uma.token"
        credentials.write_token(file, "nt_0000000a_collected")
        cfg = {"port": self.port}
        with mock.patch.dict(os.environ, {credentials.TOKEN_FILE_ENV: str(file)}):
            req = credentials.ask(cfg, "agent", "Uma", None)
            self.assertIsNone(req["id"])
            self.assertEqual(credentials.collect(cfg, req)[0], "nt_0000000a_collected")
            self.assertEqual([r for r in json.loads(self.owner("GET", "/api/enroll")[1]) if r["name"] == "Uma"], [])
            # A token the service refused is replaced: with it in the file, the program asks.
            req = credentials.ask(cfg, "agent", "Uma", None, stale="nt_0000000a_collected")
            self.assertIsNotNone(req["id"])
            file.unlink()
            with self.assertRaisesRegex(credentials.CredentialError, "was removed after another program collected"):
                credentials.collect(cfg, {**req, "id": None, "secret": None})
        self.assertEqual([r["id"] for r in json.loads(self.owner("GET", "/api/enroll")[1]) if r["name"] == "Uma"],
                         [req["id"]])

    def test_nanotea_mcp_without_a_token_asks_then_joins_once_approved(self):
        env = {**self.env}
        env.pop("NANOTEA_TOKEN_FILE", None)
        server = StdioServerParameters(command=sys.executable, args=["-m", "nanotea", "mcp", "--name", "Sky"],
                                       env=env, cwd=self.tmp.name)

        async def run():
            async with Client(server, mode="legacy") as c:
                r = await c.call_tool("check", {})
                self.assertTrue(r.is_error)
                self.assertIn("waiting for approval", r.content[0].text)
                self.assertIn("the owner, in the app under Tokens", r.content[0].text)
                (req,) = [q for q in json.loads(self.owner("GET", "/api/enroll")[1]) if q["name"] == "Sky"]
                self.assertEqual(req["dir"], os.path.realpath(self.tmp.name))
                waiter = asyncio.create_task(c.call_tool("wait", {"timeout_s": 30}))
                await asyncio.sleep(1)
                self.assertEqual(self.owner("POST", f"/api/enroll/{req['id']}/approve", {})[0], 200)
                r = await waiter
                self.assertFalse(r.is_error, r.content)
                self.assertEqual((r.structured_content["approved"], r.structured_content["name"]), (True, "Sky"))
                # Joined as --name once the token came: check needs a joined agent.
                r = await c.call_tool("check", {})
                self.assertFalse(r.is_error, r.content)
        asyncio.run(run())
        self.assertEqual(stat.S_IMODE((Path(self.tmp.name) / "tokens" / "sky.token").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
