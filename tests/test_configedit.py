"""Changing the config from the app: the file edited in place, the change checked as the service would start with
it, the machine's own settings behind the host key, and the service restarting with the new file."""

import json
import sys
import time
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Case  # noqa: E402

from nanotea import configedit as ce  # noqa: E402
from nanotea.configedit import EditError  # noqa: E402

FILE = """# The service.
host = "127.0.0.1"   # only this machine
port = 7447
trusted_proxies = [
  "127.0.0.1",  # caddy
]

[app]
name = "Teapot"  # what it calls itself
groups = [{ name = "Builders", members = ["builder"] }]

# Voices.
[tts]
backend = "command"

[tts.command]
argv = ["say", "{text_file}"]
voices = ["a", "b"]

[[phone.calls]]
to = "x"
"""


def change(table, key, value=None, unset=False):
    return (tuple(table), key, value, unset)


class Edit(unittest.TestCase):
    def edited(self, *changes):
        new, before, after = ce.edit(FILE, list(changes))
        self.assertEqual(tomllib.loads(new), after)
        return new, after

    def test_a_value_changes_in_place_and_its_comment_stays(self):
        new, after = self.edited(change([], "host", "0.0.0.0"), change(["app"], "name", "Kettle"))
        self.assertIn('host = "0.0.0.0"   # only this machine\n', new)
        self.assertIn('name = "Kettle"  # what it calls itself\n', new)
        self.assertEqual(new.count("\n"), FILE.count("\n"))
        self.assertTrue(new.startswith("# The service.\n"))

    def test_a_multiline_value_is_replaced_whole(self):
        new, after = self.edited(change([], "trusted_proxies", ["10.0.0.1"]))
        self.assertEqual(after["trusted_proxies"], ["10.0.0.1"])
        self.assertNotIn("caddy", new)
        self.assertIn("[app]", new)

    def test_a_new_key_goes_in_its_table_and_a_new_table_after_its_kin(self):
        new, after = self.edited(change(["tts", "command"], "timeout_s", 30), change(["tts", "speechify"], "speed", 1.5),
                                 change(["audio"], "format", "flac"))
        self.assertEqual(after["tts"]["command"]["timeout_s"], 30)
        self.assertEqual(after["tts"]["speechify"], {"speed": 1.5})
        self.assertEqual(after["audio"], {"format": "flac"})
        self.assertLess(new.index("timeout_s"), new.index("[[phone.calls]]"))

    def test_unset_removes_the_line(self):
        new, after = self.edited(change(["tts", "command"], "voices", unset=True))
        self.assertNotIn("voices", new)
        self.assertEqual(after["tts"]["command"], {"argv": ["say", "{text_file}"]})

    def test_strings_survive_quotes_newlines_and_control_characters(self):
        tricky = 'say "hi"\n\tthen \\ go \x7f é'
        new, after = self.edited(change(["app"], "name", tricky))
        self.assertEqual(after["app"]["name"], tricky)

    def test_what_it_cannot_change_in_place_is_refused_by_name(self):
        with self.assertRaisesRegex(EditError, r"\[app\.groups\]|groups"):
            ce.edit(FILE, [change(["app", "groups"], "name", "x")])
        with self.assertRaisesRegex(EditError, "phone"):
            ce.edit(FILE, [change(["phone", "calls"], "to", "y")])

    def test_changes_are_checked_in_shape(self):
        for bad in ([], [{"table": "app", "key": "name", "value": 1}], [{"table": ["app"], "key": "name"}],
                    [{"table": ["app"], "key": "name", "value": 1, "unset": True}], "x"):
            with self.subTest(bad=bad), self.assertRaises(EditError):
                ce.parse_changes(bad)
        self.assertEqual(ce.parse_changes([{"table": ["tts", "kokoro"], "key": "speed", "value": 2, "float": True}]),
                         [(("tts", "kokoro"), "speed", 2.0, False)])


class Machine(unittest.TestCase):
    def gated(self, *changes):
        _, before, after = ce.edit(FILE, list(changes))
        return ce.machine_changes(before, after)

    def test_programs_code_and_data_are_the_machines(self):
        self.assertEqual(self.gated(change(["tts", "command"], "argv", ["rm", "-rf", "/"])), ["[tts.command] argv"])
        self.assertEqual(self.gated(change([], "data_dir", "/tmp/x")), ["data_dir"])
        self.assertEqual(self.gated(change(["tts"], "backend", "evil:Plugin")), ["[tts] backend"])
        self.assertEqual(self.gated(change(["notify"], "now", ["evil:Notify"])), ["[notify] now"])
        self.assertEqual(self.gated(change(["settings"], "bang", True)), ["[settings] bang"])
        self.assertEqual(self.gated(change(["settings"], "config_programs", True)), ["[settings] config_programs"])

    def test_the_rest_is_the_owners(self):
        self.assertEqual(self.gated(change(["tts"], "backend", "speechify"), change(["tts", "command"], "voices", ["c"]),
                                    change([], "port", 8000), change(["app"], "name", "K"),
                                    change(["settings"], "thread_view", "grouped")), [])


class Env(unittest.TestCase):
    def test_a_secret_is_set_replaced_and_removed_and_other_lines_stay(self):
        text = "# keys\nexport A=1\nB='two'\n"
        added = ce.edit_env(text, "SPEECHIFY_API_KEY", "s3cr3t value")
        self.assertTrue(added.startswith(text))
        self.assertIn("SPEECHIFY_API_KEY=", added)
        again = ce.edit_env(added, "B", "3")
        self.assertIn("B=3\n", again)
        self.assertNotIn("two", again)
        gone = ce.edit_env(again, "A", None)
        self.assertNotIn("A=1", gone)
        self.assertIn("# keys\n", gone)
        with self.assertRaises(EditError):
            ce.edit_env(text, "lower", "x")
        with self.assertRaises(EditError):
            ce.edit_env(text, "C", None)


class Service(Case):
    PORT_OFFSET = 17

    def path(self):
        return Path(self.env["NANOTEA_CONFIG"])

    def host_key(self):
        return (Path(self.tmp.name) / "data" / "host_key").read_text().strip()

    def started(self):
        return json.loads(self.call("GET", "/api/config/started")[1])["started"]

    def wait_restart(self, was):
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(0.2)
            try:
                status, out = self.call("GET", "/api/config/started")
            except OSError:
                continue
            if status == 200 and json.loads(out)["started"] != was:
                return
        self.fail("the service did not come back:\n" + "".join(self.log[-30:]))

    def save(self, changes, via=None):
        body = {"changes": changes}
        return (self.call("POST", "/api/config", body) if via is None
                else self.bearer("POST", "/api/config", body, via))

    def test_a_change_is_written_in_place_and_the_service_restarts_with_it(self):
        text = self.path().read_text()
        self.path().write_text("# kept\n" + text)
        was = self.started()
        status, out = self.save([{"table": ["app"], "key": "owner", "value": "Sam"},
                                 {"table": ["audio"], "key": "format", "value": "flac"}])
        self.assertEqual(status, 200, out)
        got = json.loads(out)
        self.assertEqual(got["changed"], ["[app] owner", "[audio] format"])
        self.wait_restart(was)
        now = self.path().read_text()
        self.assertTrue(now.startswith("# kept\n"))
        self.assertEqual(tomllib.loads(now)["app"]["owner"], "Sam")
        self.assertEqual(Path(got["kept"]).read_text(), "# kept\n" + text)
        view = json.loads(self.call("GET", "/api/config")[1])
        fmt = next(o for s in view["sections"] if s["id"] == "audio" for o in s["rows"] if o["key"] == "format")
        self.assertEqual((fmt["source"], fmt["raw"]), ("config", "flac"))
        self.assertIsNone(view["changed"])
        was = self.started()
        status, out = self.save([{"table": ["app"], "key": "owner", "value": "Robin"},
                                 {"table": ["audio"], "key": "format", "unset": True}])
        self.assertEqual(status, 200, out)
        self.wait_restart(was)
        status, out = self.save([{"table": ["app"], "key": "owner", "value": "Robin"}])
        self.assertEqual((status, json.loads(out)["error"]), (400, "nothing to change: the file already says that"))

    def test_a_config_the_service_would_stop_with_is_refused_and_the_file_kept(self):
        text = self.path().read_text()
        for changes, says in (([{"table": [], "key": "port", "value": 0}], "port"),
                              ([{"table": ["audio"], "key": "format", "value": "wma"}], "format"),
                              ([{"table": ["tts", "command"], "key": "nope", "value": 1}], "nope"),
                              ([{"table": [], "key": "public_url", "value": "https://x.test/path"}], "public_url")):
            with self.subTest(changes=changes):
                status, out = self.save(changes)
                self.assertEqual(status, 400, out)
                self.assertIn(says, json.loads(out)["error"])
                self.assertEqual(self.path().read_text(), text)

    def test_a_port_in_use_is_refused(self):
        import socket
        with socket.create_server(("127.0.0.1", 0)) as s:
            status, out = self.save([{"table": [], "key": "port", "value": s.getsockname()[1]}])
        self.assertEqual(status, 400, out)
        self.assertIn("couldn't listen", json.loads(out)["error"])

    def test_programs_change_only_with_the_machines_say_so(self):
        text = self.path().read_text()
        argv = [sys.executable, "-c", "print('transcript')", "{audio_file}", "again"]
        status, out = self.save([{"table": ["stt", "command"], "key": "argv", "value": argv}])
        self.assertEqual(status, 403, out)
        self.assertIn("nanotea config programs on", json.loads(out)["error"])
        self.assertEqual(self.path().read_text(), text)
        status, out = self.call("POST", "/api/settings", {"config_programs": True})
        self.assertEqual(status, 403, out)
        status, out = self.save([{"table": ["settings"], "key": "config_programs", "value": True}])
        self.assertEqual(status, 403, out)
        status, out = self.bearer("POST", "/api/settings", {"config_programs": True}, self.host_key())
        self.assertEqual(status, 200, out)
        try:
            page = self.call("GET", "/config")[1]
            self.assertIn("Programs from this app is on", page)
            was = self.started()
            status, out = self.save([{"table": ["stt", "command"], "key": "timeout_s", "value": 61},
                                     {"table": ["stt", "command"], "key": "argv", "value": argv}])
            self.assertEqual(status, 200, out)
            self.wait_restart(was)
        finally:
            self.assertEqual(self.call("POST", "/api/settings", {"config_programs": False})[0], 200)
        self.assertEqual(tomllib.loads(self.path().read_text())["stt"]["command"]["timeout_s"], 61)
        self.assertIn("locked here", self.call("GET", "/config")[1])

    def test_a_secret_is_written_to_env_file_and_never_shown(self):
        env = Path(self.tmp.name) / "env"
        status, out = self.call("POST", "/api/config/secret", {"name": "NOT_A_SECRET", "value": "x"})
        self.assertEqual(status, 400, out)
        was = self.started()
        status, out = self.call("POST", "/api/config/secret", {"name": "SPEECHIFY_API_KEY", "value": "sk-test-123456"})
        self.assertEqual(status, 200, out)
        self.wait_restart(was)
        self.assertIn("SPEECHIFY_API_KEY=sk-test-123456", env.read_text())
        self.assertTrue(env.read_text().startswith("X=1\n"))
        self.assertNotIn("sk-test-123456", self.call("GET", "/api/config")[1])
        self.assertNotIn("sk-test-123456", self.call("GET", "/config")[1])
        was = self.started()
        status, out = self.call("POST", "/api/config/secret", {"name": "SPEECHIFY_API_KEY", "unset": True})
        self.assertEqual(status, 200, out)
        self.wait_restart(was)
        self.assertEqual(env.read_text(), "X=1\n")

    def test_the_page_edits_every_row(self):
        page = self.call("GET", "/config")[1]
        view = json.loads(self.call("GET", "/api/config")[1])
        keys = [o for s in view["sections"] for o in s["rows"]]
        self.assertGreater(len(keys), 30)
        self.assertEqual(page.count('class="opt" data-path='), page.count('class="edit"'))
        for o in keys:
            self.assertIn(f'data-key="{o["key"]}"', page)
        self.assertIn('id="cfg-save"', page)
        self.assertIn("Others you could use", page)

    def test_only_the_owner(self):
        self.assertEqual(self.call("POST", "/api/config", {"changes": []}, paired=False)[0], 401)
        status, _ = self.local("POST", "/api/config", {"changes": [{"table": ["app"], "key": "name", "value": "x"}]},
                               as_="builder")
        self.assertIn(status, (401, 403))


if __name__ == "__main__":
    unittest.main()
