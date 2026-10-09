"""The configuration on the Settings pages: every option declared and documented, unknown keys refused, the prompts
shown and changed, and no secret on a page."""

import json
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from harness import ROOT, Case
from nanotea import audio, bang, configview, delivery, plugins, settings, themes
from nanotea.config import APP, NOTIFY, TOP, ConfigError
from nanotea.prompts import Prompts, PromptError

EXAMPLE = ROOT / "nanotea" / "config.example.toml"
HEADER = re.compile(r"#?\s*\[([^\]]+)\]")
KEY = re.compile(r"#?\s*([a-z_]+)\s*=")
BUILT_IN_KINDS = ("tts", "stt", "rewrite", "notify")


def documented() -> set[tuple[str, str]]:
    """(table, key) for every key the example config shows, live or commented out."""
    table, found = "", set()
    for line in EXAMPLE.read_text().splitlines():
        if m := HEADER.fullmatch(line.strip()):
            table = m[1].replace('"', "")
        elif m := KEY.match(line):
            found.add((table, m[1]))
    return found


def declared() -> set[tuple[str, str]]:
    """(table, key) for every key the service takes in a table it documents."""
    out = {("", o.key) for o in TOP} | {("app", o.key) for o in APP} | {("notify", o.key) for o in NOTIFY}
    out |= {("theme", o.key) for o in themes.OPTIONS} | {("audio", o.key) for o in audio.OPTIONS}
    out |= {("bang", o.key) for o in bang.OPTIONS} | {("delivery", o.key) for o in delivery.OPTIONS}
    out -= {("theme", "custom")}
    out |= {("settings", s.key) for s in settings.SCHEMA} | {("settings", "tools")}
    out |= {("control", "use"), ("rewrite", "backend"), ("tts", "backend"), ("stt", "backend"),
            ("theme.custom.lapsang", "base"), ("theme.custom.lapsang", "light"), ("theme.custom.lapsang", "dark")}
    for kind in BUILT_IN_KINDS:
        for name in plugins.BUILTIN[kind]:
            out |= {(f"{kind}.{name}", o.key) for o in plugins.find(kind, name).load().OPTIONS}
    return out


class Documented(unittest.TestCase):
    def test_every_built_in_plugin_says_what_it_takes(self):
        for kind in BUILT_IN_KINDS:
            for name in plugins.BUILTIN[kind]:
                self.assertTrue(hasattr(plugins.find(kind, name).load(), "OPTIONS"), f"{kind} {name}")

    def test_the_example_config_shows_every_key_and_only_those(self):
        shown, taken = documented(), declared()
        self.assertEqual(sorted(taken - shown), [], "keys the example config doesn't show")
        self.assertEqual(sorted(shown - taken), [], "keys the example config shows that nothing takes")

    def test_the_docs_name_every_option(self):
        text = (ROOT / "docs" / "service.md").read_text() + (ROOT / "docs" / "plugins.md").read_text() + (ROOT / "docs" / "agents.md").read_text()
        for table, key in sorted(declared()):
            if not table.startswith("theme.custom"):
                self.assertIn(f"`{key}`", text, f"[{table}] {key}")

    def test_the_example_loads(self):
        from nanotea.config import check
        check(tomllib.loads(EXAMPLE.read_text()))


class Unknown(unittest.TestCase):
    def ctx(self):
        return {"env": {"SPEECHIFY_API_KEY": "k"}, "owner": "Robin", "app_name": "Teapot",
                "data": Path(tempfile.gettempdir()), "services": {"push": None}}

    def test_a_plugin_table_refuses_a_key_it_does_not_take(self):
        speechify = {"model": "m", "timeout_s": 5}
        for kind, cfg, says in (
                ("tts", {"backend": "speechify", "speechify": {**speechify, "mdoel": 1}},
                 r"\[tts.speechify\] has no key mdoel; keys are model, timeout_s"),
                ("tts", {"backend": "speechify", "speechify": speechify, "spechify": {}},
                 r"unknown key \[tts\] 'spechify'"),
                ("stt", {"backend": "command", "command": {"argv": [sys.executable], "timeout_s": 1, "timeout": 1}},
                 r"\[stt.command\] has no key timeout; keys are argv, timeout_s"),
                ("rewrite", {"backend": "identity", "identity": {"x": 1}}, r"\[rewrite.identity\] has no key x"),
                ("rewrite", {"backend": "claude", "claud": {}}, r"unknown key \[rewrite\] 'claud'"),
                ("rewrite", {"backend": "identity", "bakend": "x"}, r"unknown key \[rewrite\] 'bakend'")):
            with self.assertRaisesRegex(ConfigError, says):
                plugins.make(kind, cfg, self.ctx())

    def test_notify_and_control_tables_too(self):
        with self.assertRaisesRegex(ConfigError, r"\[notify.imessage\] has no key number; keys are to"):
            plugins.make_many("notify", ["imessage"], {"imessage": {"to": "x", "number": 1}}, self.ctx())
        with self.assertRaisesRegex(ConfigError, r"unknown key \[notify\] 'now_'"):
            plugins.make_many("notify", [], {"now_": []}, self.ctx())
        with self.assertRaisesRegex(ConfigError, r"unknown key \[control\] 'chooser'"):
            plugins.make_many("control", ["choice"], {"use": ["choice"], "chooser": {}}, self.ctx())

    def test_describe_says_given_default_and_missing(self):
        info = plugins.describe("tts", "kokoro-server", {"url": "http://x:1", "speed": 1.5})
        by = {o["key"]: o for o in info["options"]}
        self.assertEqual((by["url"]["source"], by["url"]["value"]), ("config", "http://x:1"))
        self.assertEqual((by["speed"]["source"], by["speed"]["value"]), ("config", 1.5))
        self.assertEqual((by["format"]["source"], by["format"]["value"]), ("default", "wav"))
        self.assertEqual(plugins.describe("tts", "speechify", {})["options"][0]["source"], "missing")
        self.assertEqual(plugins.describe("tts", "speechify", {})["secrets"], ["SPEECHIFY_API_KEY"])


class Scrub(unittest.TestCase):
    def test_a_secret_never_survives_in_a_string(self):
        out = configview.scrub({"argv": ["tool", "--key", "sk-abcdef123"], "n": 3, "deep": [{"x": "a sk-abcdef123 b"}]},
                               ["sk-abcdef123"])
        self.assertNotIn("sk-abcdef123", json.dumps(out))
        self.assertEqual(out["n"], 3)


class Prompt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prompts = Prompts(self.root, "Robin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_built_in_until_the_owner_changes_it_and_back(self):
        built = self.prompts.builtin("rewrite")
        self.assertIn("{owner}", built)
        self.assertEqual(self.prompts.text("rewrite"), built.replace("{owner}", "Robin"))
        self.assertIsNone(self.prompts.custom("rewrite"))
        self.prompts.change("rewrite", "Read it to {owner} plainly. Clips: [[audio 1]] stay put.")
        self.assertEqual(self.prompts.text("rewrite"), "Read it to Robin plainly. Clips: [[audio 1]] stay put.\n")
        self.assertEqual((self.root / "prompts" / "rewrite.md").read_text(), self.prompts.template("rewrite"))
        self.prompts.reset("rewrite")
        self.assertEqual(self.prompts.text("rewrite"), built.replace("{owner}", "Robin"))
        self.prompts.reset("rewrite")  # already the built-in

    def test_what_cannot_be_used_is_refused_and_says_why(self):
        for text, says in (("", "is empty"), ("   \n", "is empty"), (3, "as text"), ("x" * 20001, "the most is"),
                           ("Read it aloud.", r"audio markers such as \[\[audio 1\]\]")):
            with self.assertRaisesRegex(PromptError, says):
                self.prompts.change("rewrite", text)
        with self.assertRaisesRegex(PromptError, "no prompt 'nope'"):
            self.prompts.change("nope", "x")
        self.assertIsNone(self.prompts.custom("rewrite"))

    def test_the_rewriter_reads_a_change_on_its_next_message(self):
        echo = ("import json, pathlib, sys; print(json.dumps({'title': 't', 'script': "
                "pathlib.Path(sys.argv[1]).read_text().splitlines()[0]}))")
        rewriter = plugins.build("rewrite", "command", {"argv": [sys.executable, "-c", echo, "{system_file}"],
                                                        "timeout_s": 30},
                                 {"env": {}, "owner": "Robin", "app_name": "T", "data": self.root})
        before = rewriter.rewrite("hi", "B (b)", None, False).script
        self.assertTrue(before.startswith("You rewrite messages that software agents send to Robin"))
        self.prompts.change("rewrite", "Sing it to {owner}. [[audio 1]]")
        self.assertEqual(rewriter.rewrite("hi", "B (b)", None, False).script, "Sing it to Robin. [[audio 1]]")
        self.prompts.reset("rewrite")
        self.assertEqual(rewriter.rewrite("hi", "B (b)", None, False).script, before)


class Pages(Case):
    PORT_OFFSET = 15

    def page(self, path):
        status, out = self.call("GET", path)
        self.assertEqual(status, 200, path)
        return out

    def test_the_config_page_shows_what_the_service_runs_with(self):
        page = self.page("/config")
        for part in ("Voice (tts)", "Transcriber (stt)", "Rewriter (rewrite)", "[tts.command]", "[stt.command]",
                     "set in the file", "default", "Notifications", "Joined audio".replace("Joined audio", "Audio"),
                     "config.toml", "/prompts"):
            self.assertIn(part, page)
        self.assertIn('value="m4a"', page)  # tts.command ext, as the file has it
        self.assertIn("Others you could use", page)

    def test_the_settings_page_summarizes_it(self):
        page = self.page("/settings")
        for part in ("Configuration", 'href="/config#tts"', 'href="/prompts"', "command, makes .m4a", "built in"):
            self.assertIn(part, page)

    def test_the_voices_come_from_the_engine(self):
        out = json.loads(self.call("GET", "/api/config/voices")[1])
        self.assertIsNone(out["error"])
        self.assertEqual([v["id"] for v in out["voices"]][:2], ["v1", "v2"])

    def test_the_api_config_names_where_each_value_comes_from(self):
        view = json.loads(self.call("GET", "/api/config")[1])
        by = {s["id"]: s for s in view["sections"]}
        self.assertTrue(view["file"].endswith("config.toml"))
        ext = next(o for o in by["tts"]["rows"] if o["key"] == "ext")
        self.assertEqual((ext["table"], ext["source"], ext["value"]), ("[tts.command]", "config", '"m4a"'))
        fmt = next(o for o in by["audio"]["rows"] if o["key"] == "format")
        self.assertEqual((fmt["source"], fmt["value"]), ("default", '"mp3"'))
        self.assertEqual(by["tts"]["plugin"]["name"], "command")
        self.assertIn("1 keys set", " ".join(v for _, v in by["service"]["facts"]))
        self.assertNotIn("X=1", json.dumps(view))

    def test_owner_only(self):
        for method, path in (("GET", "/api/config"), ("GET", "/api/prompts"), ("GET", "/api/config/voices"),
                             ("POST", "/api/prompts")):
            status, _ = self.local(method, path, {"name": "rewrite", "reset": True} if method == "POST" else None,
                                   as_="builder")
            self.assertEqual(status, 403, path)

    def test_the_prompts_page_shows_the_text_sent_to_models(self):
        page = self.page("/prompts")
        self.assertIn("You rewrite messages that software agents send to Robin", page)
        self.assertIn("Not used now: the rewriter is identity", page)
        self.assertIn("Nanotea connects you to Robin", page)  # the working agreement, owner filled in
        self.assertIn("Speaker: Builder (builder)", page)

    def test_the_owner_changes_the_prompt_and_goes_back(self):
        status, out = self.call("POST", "/api/prompts", {"name": "rewrite", "text": "Short for {owner}. [[audio 1]]"})
        self.assertEqual(status, 200, out)
        view = json.loads(out)["rewrite"]
        self.assertTrue(view["custom"])
        self.assertEqual(view["text"], "Short for Robin. [[audio 1]]\n")
        self.assertIn("Yours, kept in", self.page("/prompts"))
        self.assertEqual(self.call("POST", "/api/prompts", {"name": "rewrite", "text": "no markers"})[0], 400)
        self.assertEqual(json.loads(self.call("GET", "/api/prompts")[1])["rewrite"]["template"],
                         "Short for {owner}. [[audio 1]]\n")
        status, out = self.call("POST", "/api/prompts", {"name": "rewrite", "reset": True})
        self.assertFalse(json.loads(out)["rewrite"]["custom"])
        self.assertEqual(self.call("POST", "/api/prompts", {"name": "nope", "text": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/prompts", {"text": "x"})[0], 400)
        self.assertTrue(any("changed the rewrite prompt" in line for line in self.log))
        self.assertTrue(any("went back to the built-in rewrite prompt" in line for line in self.log))


if __name__ == "__main__":
    unittest.main()
