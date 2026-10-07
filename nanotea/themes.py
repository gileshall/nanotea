"""How the app looks: a theme (its colors, light and dark) and an appearance (follow the device, or always light or
dark). The built-in themes are teas; the owner adds their own under [theme.custom.<name>], each starting from a
built-in and changing what it names. The choice is the owner's, kept in data/theme.json."""

import json
import os
import re
import threading
from pathlib import Path

from nanotea.config import ConfigError, Option, unknown_keys

# Every color a theme sets, as config keys; the page's CSS variable is the key with dashes. leaf and vein color
# the leaf marks: the leaf's body, and the veins cut through it.
KEYS = ("bg", "fg", "muted", "faint", "line", "hover", "card", "chip", "accent", "accent_fg", "accent_soft",
        "side", "side_fg", "side_mute", "side_hover", "side_on", "red", "amber", "green", "leaf", "vein")
APPEARANCES = ("auto", "light", "dark")
COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")
NAME = re.compile(r"[a-z0-9][a-z0-9 -]{0,30}\Z")
SHADOW = {"light": "0 1px 2px #0000000d, 0 6px 20px #00000012", "dark": "0 1px 2px #0007, 0 8px 24px #0006"}

_STATUS = {"light": {"red": "#d33b3b", "amber": "#a86b12", "green": "#2c9a69"},
           "dark": {"red": "#f06060", "amber": "#e2a64a", "green": "#4cc38a"}}


def _tea(light: str, dark: str) -> dict[str, dict[str, str]]:
    """A theme from two lines of "key=#color" pairs; status colors are shared."""
    def parse(text: str, mode: str) -> dict[str, str]:
        out = {**_STATUS[mode], **dict(p.split("=") for p in text.split())}
        assert set(out) == set(KEYS), set(KEYS) ^ set(out)
        return out
    return {"light": parse(light, "light"), "dark": parse(dark, "dark")}


BUILT_IN = {
    "sencha": _tea(
        "bg=#ffffff fg=#1d1c1d muted=#686769 faint=#9a999b line=#e7e7e8 hover=#f6f7f7 card=#ffffff chip=#f0f2f2 "
        "accent=#2f7d6d accent_fg=#ffffff accent_soft=#e1f1ec side=#163c35 side_fg=#cfe2dd side_mute=#8db2a9 "
        "side_hover=#ffffff14 side_on=#2f7d6d leaf=#367f57 vein=#e6f2ea",
        "bg=#17191c fg=#e4e5e6 muted=#a2a4a7 faint=#75787c line=#2a2d31 hover=#1e2124 card=#1f2225 chip=#272b2f "
        "accent=#4fb5a0 accent_fg=#0c1f1b accent_soft=#1b3531 side=#0f201c side_fg=#c8dbd6 side_mute=#7b9a93 "
        "side_hover=#ffffff10 side_on=#2f7d6d leaf=#6cbf8a vein=#14261d"),
    "oolong": _tea(
        "bg=#fffcf7 fg=#241c15 muted=#6e6359 faint=#a0958a line=#ece4da hover=#f8f2ea card=#ffffff chip=#f3ece3 "
        "accent=#9a5b1e accent_fg=#ffffff accent_soft=#f6e8d8 side=#3a2616 side_fg=#ead9c6 side_mute=#b39a80 "
        "side_hover=#ffffff14 side_on=#9a5b1e leaf=#8c6a2e vein=#f7eedd",
        "bg=#1b1713 fg=#ece4da muted=#b0a497 faint=#7d7266 line=#332b23 hover=#221d18 card=#241f1a chip=#2c251f "
        "accent=#e0a15e accent_fg=#22160a accent_soft=#3a2a1a side=#14100c side_fg=#e2d3c2 side_mute=#a08a73 "
        "side_hover=#ffffff10 side_on=#9a5b1e leaf=#c9a15a vein=#241b10"),
    "earl grey": _tea(
        "bg=#fbfcfe fg=#1c1f26 muted=#646a78 faint=#9aa0ac line=#e4e7ee hover=#f4f6fa card=#ffffff chip=#edf0f5 "
        "accent=#4a63a8 accent_fg=#ffffff accent_soft=#e3e9f7 side=#252b3a side_fg=#d3d9e8 side_mute=#96a0b8 "
        "side_hover=#ffffff14 side_on=#4a63a8 leaf=#5b6f8f vein=#e8edf6",
        "bg=#16181d fg=#e3e6ec muted=#a3a9b6 faint=#737a88 line=#2a2e37 hover=#1d2027 card=#1f232b chip=#262b34 "
        "accent=#8ea6e6 accent_fg=#10172a accent_soft=#222c44 side=#101319 side_fg=#cdd4e4 side_mute=#8590a8 "
        "side_hover=#ffffff10 side_on=#4a63a8 leaf=#9fb0d4 vein=#161c2a"),
    "rooibos": _tea(
        "bg=#fffbfa fg=#261917 muted=#6f5d59 faint=#a3928e line=#efe2df hover=#faf1ef card=#ffffff chip=#f5e9e6 "
        "accent=#b0412e accent_fg=#ffffff accent_soft=#f8e2dd side=#3b1712 side_fg=#f0d5cf side_mute=#c0948a "
        "side_hover=#ffffff14 side_on=#b0412e leaf=#a4492f vein=#fbe9e3",
        "bg=#1c1514 fg=#eee1de muted=#b3a19c faint=#80706c line=#352826 hover=#241b1a card=#261d1c chip=#2e2321 "
        "accent=#ec8a74 accent_fg=#2a0f09 accent_soft=#42221c side=#150e0d side_fg=#ead2cc side_mute=#a8857d "
        "side_hover=#ffffff10 side_on=#b0412e leaf=#e0896c vein=#2a1512"),
}


class ThemeError(ValueError):
    pass


OPTIONS = (
    Option("use", str, "The theme a new install starts with: sencha, oolong, earl grey, rooibos, or one of yours. "
                       "After the first start the owner picks it in Settings > Look.", "sencha"),
    Option("appearance", str, "auto follows the device; light or dark holds one. Also the owner's to change in "
                              "Settings > Look.", "auto", choices=APPEARANCES),
    Option("custom", dict, "Your own themes: [theme.custom.<name>] with base (a built-in to start from) and light "
                           "and dark, tables of the colors to change.", {}),
)


def from_config(cfg: dict) -> tuple[dict, dict]:
    """The [theme] table: every theme (built-in and the owner's), and what a new install starts with,
    {theme, appearance}. ConfigError naming the table and key for anything it can't use."""
    table = cfg.get("theme", {})
    if not isinstance(table, dict):
        raise ConfigError("[theme] must be a table")
    unknown_keys(table, OPTIONS, "[theme]")
    themes = json.loads(json.dumps(BUILT_IN))
    custom = table.get("custom", {})
    if not isinstance(custom, dict):
        raise ConfigError("[theme.custom] must be a table of themes")
    for name, spec in custom.items():
        where = f"[theme.custom.{name}]" if " " not in name else f'[theme.custom."{name}"]'
        if not NAME.fullmatch(name):
            raise ConfigError(f"{where}: a theme's name is lowercase letters, digits, spaces and dashes")
        if name in BUILT_IN:
            raise ConfigError(f"{where}: {name!r} is a built-in theme; give yours another name")
        if not isinstance(spec, dict):
            raise ConfigError(f"{where} must be a table")
        for key in spec:
            if key not in ("base", "light", "dark"):
                raise ConfigError(f"{where} has no key {key!r}; it takes base, light and dark")
        base = spec.get("base")
        if base not in BUILT_IN:
            raise ConfigError(f"{where} base must be a built-in theme: {', '.join(BUILT_IN)}")
        theme = json.loads(json.dumps(BUILT_IN[base]))
        for mode in ("light", "dark"):
            colors = spec.get(mode, {})
            if not isinstance(colors, dict):
                raise ConfigError(f"{where} {mode} must be a table of colors")
            for key, value in colors.items():
                if key not in KEYS:
                    raise ConfigError(f"{where} {mode}.{key}: no such color; colors: {', '.join(KEYS)}")
                if not (isinstance(value, str) and COLOR.fullmatch(value)):
                    raise ConfigError(f"{where} {mode}.{key} must be a color like \"#2f7d6d\", not {value!r}")
                theme[mode][key] = value
        themes[name] = theme
    start = {"theme": table.get("use", "sencha"), "appearance": table.get("appearance", "auto")}
    if start["theme"] not in themes:
        raise ConfigError(f"[theme] use = {start['theme']!r} is no theme; themes: {', '.join(themes)}")
    if start["appearance"] not in APPEARANCES:
        raise ConfigError(f"[theme] appearance must be one of {', '.join(APPEARANCES)}")
    return themes, start


def _block(colors: dict[str, str], mode: str) -> str:
    return " ".join(f"--{k.replace('_', '-')}: {v};" for k, v in colors.items()) + f" --shadow: {SHADOW[mode]};"


def css(theme: dict, appearance: str) -> str:
    """The page's color variables: one mode, or with auto, the device's."""
    if appearance != "auto":
        return f":root {{ color-scheme: {appearance}; {_block(theme[appearance], appearance)} }}\n"
    return (f":root {{ color-scheme: light dark; {_block(theme['light'], 'light')} }}\n"
            f"@media (prefers-color-scheme: dark) {{ :root {{ {_block(theme['dark'], 'dark')} }} }}\n")


class Look:
    """The owner's theme and appearance, data/theme.json; a new install starts with the config's."""

    def __init__(self, root: Path, themes: dict, start: dict):
        self.path = root / "theme.json"
        self.themes = themes
        self._lock = threading.Lock()
        if self.path.exists():
            stored = json.loads(self.path.read_text())
            if set(stored) != {"theme", "appearance"}:
                raise ThemeError(f"{self.path} must hold theme and appearance, not {', '.join(stored)}")
            try:
                self._check(stored)
            except ThemeError as err:
                raise ThemeError(f"{self.path}: {err}") from None
            self._now = stored
        else:
            self._now = dict(start)

    def _check(self, patch) -> None:
        if not isinstance(patch, dict) or not patch:
            raise ThemeError("send {theme, appearance}, either or both")
        for key, value in patch.items():
            if key == "theme":
                if value not in self.themes:
                    raise ThemeError(f"no theme {value!r}; themes: {', '.join(self.themes)}")
            elif key == "appearance":
                if value not in APPEARANCES:
                    raise ThemeError(f"appearance must be one of {', '.join(APPEARANCES)}")
            else:
                raise ThemeError(f"unknown key {key!r}; send theme and appearance")

    def get(self) -> dict:
        with self._lock:
            return dict(self._now)

    def change(self, patch) -> dict:
        self._check(patch)
        with self._lock:
            self._now = {**self._now, **patch}
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._now, indent=2))
            os.replace(tmp, self.path)
            return dict(self._now)

    def colors(self) -> dict[str, dict[str, str]]:
        """The chosen theme's colors, light and dark."""
        return self.themes[self.get()["theme"]]

    def css(self) -> str:
        now = self.get()
        return css(self.themes[now["theme"]], now["appearance"])
