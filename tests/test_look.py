"""How the app looks: themes from the config and the owner's pick, the leaf marks and the icons made from them."""

import http.client
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import ROOT, Case
from nanotea import leaf
from nanotea.config import ConfigError
from nanotea.themes import BUILT_IN, KEYS, css, from_config


class Config(unittest.TestCase):
    def test_built_ins_set_every_color(self):
        self.assertEqual(list(BUILT_IN), ["sencha", "oolong", "earl grey", "rooibos"])
        for name, theme in BUILT_IN.items():
            for mode in ("light", "dark"):
                self.assertEqual(set(theme[mode]), set(KEYS), (name, mode))

    def test_a_custom_theme_starts_from_a_built_in(self):
        themes, start = from_config({"theme": {"use": "lapsang", "appearance": "dark", "custom": {
            "lapsang": {"base": "oolong", "light": {"accent": "#7a3b1c"}, "dark": {"accent_fg": "#fff"}}}}})
        self.assertEqual(start, {"theme": "lapsang", "appearance": "dark"})
        self.assertEqual(themes["lapsang"]["light"]["accent"], "#7a3b1c")
        self.assertEqual(themes["lapsang"]["light"]["side"], BUILT_IN["oolong"]["light"]["side"])
        self.assertEqual(themes["lapsang"]["dark"]["accent_fg"], "#fff")
        self.assertEqual(from_config({})[1], {"theme": "sencha", "appearance": "auto"})

    def test_what_the_config_cannot_use_names_its_table_and_key(self):
        cases = [
            ({"colour": 1}, "[theme] has no key colour"),
            ({"use": "matcha"}, "[theme] use = 'matcha' is no theme"),
            ({"appearance": "dim"}, "[theme] appearance must be one of auto, light, dark"),
            ({"custom": {"x": {"light": {}}}}, "[theme.custom.x] base must be a built-in theme"),
            ({"custom": {"x": {"base": "sencha", "light": {"accnt": "#fff"}}}},
             "[theme.custom.x] light.accnt: no such color"),
            ({"custom": {"x": {"base": "sencha", "dark": {"bg": "white"}}}},
             "[theme.custom.x] dark.bg must be a color"),
            ({"custom": {"sencha": {"base": "oolong"}}}, "'sencha' is a built-in theme"),
            ({"custom": {"Big": {"base": "oolong"}}}, "a theme's name is lowercase"),
            ({"custom": {"x": {"base": "oolong", "mode": {}}}}, "[theme.custom.x] has no key 'mode'"),
        ]
        for table, says in cases:
            with self.assertRaises(ConfigError, msg=table) as got:
                from_config({"theme": table})
            self.assertIn(says, str(got.exception))

    def test_css_follows_the_device_or_holds_one_mode(self):
        auto = css(BUILT_IN["rooibos"], "auto")
        self.assertIn("color-scheme: light dark;", auto)
        self.assertIn("@media (prefers-color-scheme: dark)", auto)
        self.assertIn(f"--accent-soft: {BUILT_IN['rooibos']['dark']['accent_soft']};", auto)
        dark = css(BUILT_IN["rooibos"], "dark")
        self.assertNotIn("@media", dark)
        self.assertIn(f"--bg: {BUILT_IN['rooibos']['dark']['bg']};", dark)
        self.assertNotIn(BUILT_IN["rooibos"]["light"]["bg"], dark)


class Look(Case):
    PORT_OFFSET = 8
    EXTRA = """
[theme]
use = "earl grey"

[theme.custom.lapsang]
base = "oolong"
light = { accent = "#7a3b1c" }
"""

    def pick(self, **patch):
        status, out = self.call("POST", "/api/theme", patch)
        self.assertEqual(status, 200, out)
        return json.loads(out)

    def test_the_owner_picks_a_theme_and_an_appearance(self):
        try:
            view = json.loads(self.call("GET", "/api/theme")[1])
            self.assertEqual((view["theme"], view["appearance"]), ("earl grey", "auto"))
            self.assertEqual(list(view["themes"]), ["sencha", "oolong", "earl grey", "rooibos", "lapsang"])
            page = self.call("GET", "/settings")[1]
            self.assertIn('aria-checked="true" data-theme="earl grey"', page)
            self.assertIn(f'--accent: {BUILT_IN["earl grey"]["light"]["accent"]};', page)
            self.assertIn(f'<meta name="theme-color" content="{BUILT_IN["earl grey"]["dark"]["bg"]}" '
                          'media="(prefers-color-scheme: dark)">', page)
            self.pick(theme="lapsang", appearance="dark")
            page = self.call("GET", "/")[1]
            self.assertIn("color-scheme: dark;", page)
            self.assertNotIn("prefers-color-scheme", page)
            self.assertIn(f'<meta name="theme-color" content="{BUILT_IN["oolong"]["dark"]["bg"]}">', page)
            manifest = json.loads(self.call("GET", f"/manifest.webmanifest?k={self.key}", paired=False)[1])
            self.assertEqual(manifest["theme_color"], BUILT_IN["oolong"]["dark"]["bg"])
            stored = json.loads((Path(self.tmp.name) / "data" / "theme.json").read_text())
            self.assertEqual(stored, {"theme": "lapsang", "appearance": "dark"})
            self.pick(theme="sencha")
            self.assertIn("color-scheme: dark;", self.call("GET", "/")[1])
        finally:
            self.pick(theme="earl grey", appearance="auto")

    def test_what_is_not_a_look(self):
        for patch, says in (({"theme": "matcha"}, "no theme 'matcha'"), ({"appearance": "dim"}, "appearance must"),
                            ({"font": "serif"}, "unknown key 'font'"), ({}, "send {theme, appearance}")):
            status, out = self.call("POST", "/api/theme", patch)
            self.assertEqual(status, 400, out)
            self.assertIn(says, json.loads(out)["error"])
        self.assertEqual(self.call("POST", "/api/theme", {"theme": "sencha"}, paired=False)[0], 401)
        self.assertEqual(self.local("POST", "/api/theme", {"theme": "sencha"})[0], 403)

    def test_leaves_are_public_svgs_seeded_by_name(self):
        status, body = self.local("GET", "/leaf/builder.svg?size=24")
        self.assertEqual(status, 200)
        self.assertEqual(body, leaf.svg("builder", size=24))
        self.assertEqual(self.local("GET", "/leaf/caf%C3%A9.svg?unfurl=0.5")[1], leaf.svg("café", unfurl=0.5))
        self.assertEqual(self.local("GET", "/leaf/x.svg?size=20")[0], 400)
        self.assertEqual(self.local("GET", "/leaf/x.svg?unfurl=2")[0], 400)
        self.assertEqual(self.call("GET", "/leaf/x.svg", paired=False)[0], 200)

    def test_agents_wear_their_leaves(self):
        self.send("leafy", "Hello.", title="Leafy hello")
        page = self.call("GET", "/")[1]
        self.assertIn('--src: url(&quot;/leaf/leafy.svg?size=48&quot;)', page)
        self.assertRegex(page, r'class="brand" href="/"><span class="mark"')
        self.assertIn("url(/leaf/Teapot.svg?size=32)", page)
        self.assertIn('<link rel="icon" href="/favicon.svg" type="image/svg+xml">', page)

    def test_icons_are_the_app_leaf_in_the_theme(self):
        try:
            for path, px in (("/apple-touch-icon.png", 180), ("/icon-192.png", 192), ("/icon-512.png", 512),
                             ("/icon-64.png", 64)):
                png = self.raw(path)
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n", path)
                self.assertEqual(int.from_bytes(png[16:20]), px, path)
            c = BUILT_IN["earl grey"]
            want = leaf.png("Teapot", 192, fg=c["dark"]["leaf"], bg=c["light"]["side"], scale=0.78)
            self.assertEqual(self.raw("/icon-192.png"), want)
            self.pick(theme="rooibos")
            c = BUILT_IN["rooibos"]
            self.assertEqual(self.raw("/icon-192.png"),
                             leaf.png("Teapot", 192, fg=c["dark"]["leaf"], bg=c["light"]["side"], scale=0.78))
            fav = self.local("GET", "/favicon.svg")[1]
            self.assertIn(f'fill="{c["light"]["leaf"]}"', fav)
        finally:
            self.pick(theme="earl grey")

    def raw(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", path)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        self.assertEqual(r.status, 200, path)
        return data


class OldConfig(unittest.TestCase):
    def test_theme_color_stops_the_service_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            text = (ROOT / "nanotea" / "config.example.toml").read_text()
            text = re.sub(r'(?m)^data_dir = .*$', f'data_dir = "{tmp}/data"', text)
            text = re.sub(r'(?m)^env_file = .*$', f'env_file = "{tmp}/env"', text)
            config.write_text(text.replace('owner = "Alex"', 'owner = "Alex"\ntheme_color = "#2f7d6d"'))
            (Path(tmp) / "env").write_text("")
            r = subprocess.run([sys.executable, "-m", "nanotea", "serve"], env={"NANOTEA_CONFIG": str(config),
                               "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("[app] theme_color is gone", r.stderr)
