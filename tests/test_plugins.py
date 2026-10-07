"""Plugins: found by name, by module:Class, or from installed packages; a config the service can't use stops it
with a message saying what to fix."""

import tempfile
import unittest
from importlib.metadata import EntryPoint
from pathlib import Path
from unittest import mock

from nanotea import plugins
from nanotea.config import ConfigError

PLUGIN = '''
from nanotea.plugins import API


class Tone:
    """A test voice: a sine per voice."""
    api = API
    ext = "wav"

    def __init__(self, cfg, ctx):
        self.hz = ctx.need(cfg, "hz", int)
        self.where = ctx.storage()

    def voices(self):
        return [{"id": "low"}, {"id": "high"}]

    def synthesize(self, script, out, voice):
        out.write_bytes(b"RIFF")


class Old:
    """Written for another API."""
    api = 0


class Mute:
    """Says nothing: no synthesize."""
    api = API
    ext = "wav"

    def __init__(self, cfg, ctx):
        pass

    def voices(self):
        return []


class Streamer(Tone):
    """A voice that streams as it speaks."""
    stream_media = {"codec": "pcm_s16le", "rate": 24000, "channels": 1}

    def stream(self, script, voice):
        yield b"\\x00\\x00"


class Half(Tone):
    """Streams, but doesn't say in what."""
    def stream(self, script, voice):
        yield b""


class Line:
    """A test phone line."""
    api = API
    media = {"codec": "pcm_mulaw", "rate": 8000, "channels": 1}

    def __init__(self, cfg, ctx):
        self.to = ctx.need(cfg, "to", str)

    async def ring(self, to):
        raise NotImplementedError

    async def calls(self):
        yield None


class Crackle(Line):
    """A phone line that doesn't say its media right."""
    media = {"codec": "pcm_mulaw", "rate": "8k", "channels": 1}
'''


class Plugins(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "mine").mkdir()
        (root / "mine" / "tone_plugin.py").write_text(PLUGIN)
        cls.data = root / "data"
        cls.data.mkdir()
        with mock.patch("nanotea.plugins.resolve", lambda p: root / p):
            plugins.add_path(["mine"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ctx(self, **env):
        return {"env": env, "owner": "Robin", "app_name": "Teapot", "data": self.data, "services": {"push": None}}

    def test_config_errors(self):
        cases = [
            (lambda: plugins.make("tts", {"backend": "speechify", "speechify": {}}, self.ctx()),
             "needs SPEECHIFY_API_KEY"),
            (lambda: plugins.make("tts", {"backend": "speechify", "speechify": {}}, self.ctx(SPEECHIFY_API_KEY="k")),
             r"\[tts.speechify\] needs model"),
            (lambda: plugins.make("tts", {"backend": "nope"}, self.ctx()),
             r"\[tts\] backend must be one of speechify, command, kokoro, kokoro-server, or module:Class; not 'nope'"),
            (lambda: plugins.make("stt", {}, self.ctx()), r"\[stt\] backend must be named"),
            (lambda: plugins.make("stt", {"backend": "command", "command": {"argv": "whisper"}}, self.ctx()),
             r"\[stt.command\] argv has the wrong type: str"),
            (lambda: plugins.make("rewrite", {"backend": "gpt"}, self.ctx()),
             r"\[rewrite\] backend must be one of identity, command, claude"),
            (lambda: plugins.make_many("notify", ["push", "pager"], {}, self.ctx()),
             r"\[notify\] backend must be one of push, imessage, or module:Class; not 'pager'"),
            (lambda: plugins.make("tts", {"backend": "tone_plugin:Old"}, self.ctx()),
             r"\[tts\] tone_plugin:Old is for plugin API 0; this nanotea has API 1"),
            (lambda: plugins.make("tts", {"backend": "tone_plugin:Mute"}, self.ctx()),
             r"\[tts\] tone_plugin:Mute is not a tts plugin: it has no synthesize"),
            (lambda: plugins.make("tts", {"backend": "tone_plugin:Gone"}, self.ctx()), "tone_plugin has no Gone"),
            (lambda: plugins.make("tts", {"backend": "no_such_module:X"}, self.ctx()), "can't import no_such_module"),
            (lambda: plugins.make("tts", {"backend": "tone_plugin:Tone"}, self.ctx()),
             r"\[tts.tone_plugin:Tone\] needs hz"),
            (lambda: plugins.make("telepathy", {"backend": "x"}, self.ctx()), "no plugin kind 'telepathy'"),
            (lambda: plugins.make("tts", {"backend": "tone_plugin:Half", "tone_plugin:Half": {"hz": 1}}, self.ctx()),
             r"\[tts\] tone_plugin:Half has stream without stream_media; a plugin offers all of them or none"),
            (lambda: plugins.make("phone", {"backend": "tone_plugin:Crackle", "tone_plugin:Crackle": {"to": "+1"}},
                                  self.ctx()),
             r"\[phone\] tone_plugin:Crackle media must be \{\"codec\": str, \"rate\": int, \"channels\": int\}"),
            (lambda: plugins.make("phone", {"backend": "tone_plugin:Tone", "tone_plugin:Tone": {"hz": 1}}, self.ctx()),
             r"\[phone\] tone_plugin:Tone is not a phone plugin: it has no media, ring, calls"),
        ]
        for make, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ConfigError, message):
                make()

    def test_module_plugin(self):
        tone = plugins.make("tts", {"backend": "tone_plugin:Tone", "tone_plugin:Tone": {"hz": 440}}, self.ctx())
        self.assertEqual((tone.hz, tone.ext, [v["id"] for v in tone.voices()]), (440, "wav", ["low", "high"]))
        self.assertTrue(tone.where.is_dir())
        self.assertEqual(tone.where, self.data / "plugins" / "tts-tone_plugin-Tone")
        self.assertEqual(plugins.find("tts", "tone_plugin:Tone").about(), "A test voice: a sine per voice.")

    def test_optional_and_phone(self):
        tone = plugins.make("tts", {"backend": "tone_plugin:Tone", "tone_plugin:Tone": {"hz": 1}}, self.ctx())
        streamer = plugins.make("tts", {"backend": "tone_plugin:Streamer", "tone_plugin:Streamer": {"hz": 1}},
                                self.ctx())
        self.assertEqual((plugins.offers("tts", tone), plugins.offers("tts", streamer)), ([], ["stream"]))
        line = plugins.make("phone", {"backend": "tone_plugin:Line", "tone_plugin:Line": {"to": "+15550100"}},
                            self.ctx())
        self.assertEqual((line.to, plugins.media_str(line.media)), ("+15550100", "pcm_mulaw 8000 Hz x1"))

    def test_installed_plugins(self):
        tone = EntryPoint("tone", "tone_plugin:Tone", "nanotea.tts")
        with mock.patch("nanotea.plugins.entry_points", return_value=[tone]):
            found = plugins.available("tts")
            self.assertEqual(list(found), ["speechify", "command", "kokoro", "kokoro-server", "tone"])
            plugin = plugins.make("tts", {"backend": "tone", "tone": {"hz": 220}}, self.ctx())
            self.assertEqual(plugin.hz, 220)
        clash = EntryPoint("command", "tone_plugin:Tone", "nanotea.tts")
        with mock.patch("nanotea.plugins.entry_points", return_value=[clash]), \
                self.assertRaisesRegex(ConfigError, r"'command' is both built-in and from"):
            plugins.available("tts")

    def test_builtins_load(self):
        for kind, names in plugins.BUILTIN.items():
            for name in names:
                with self.subTest(kind=kind, name=name):
                    factory = plugins.find(kind, name).load()
                    self.assertEqual(factory.api, plugins.API)
                    self.assertTrue(plugins.find(kind, name).about())


if __name__ == "__main__":
    unittest.main()


class IMessageArgv(unittest.TestCase):
    def test_the_recipient_and_text_never_reach_the_process_table(self):
        from nanotea.notify import IMessage
        seen = {}

        def run(argv, **kw):
            d = Path(argv[-1])
            seen.update(argv=argv, to=(d / "to").read_text(), text=(d / "text").read_text(),
                        mode=d.stat().st_mode & 0o777)
            return mock.Mock(returncode=0, stderr="")
        sender = IMessage.__new__(IMessage)
        sender.to = "+15550100"
        with mock.patch("nanotea.notify.subprocess.run", run):
            sender.send_text("Ready\n\nhttps://x.test/m/1?k=secretkey")
        self.assertEqual((seen["to"], seen["text"]), ("+15550100", "Ready\n\nhttps://x.test/m/1?k=secretkey"))
        self.assertEqual(seen["mode"], 0o700)
        self.assertFalse(any("secretkey" in a or "15550100" in a for a in seen["argv"]))
