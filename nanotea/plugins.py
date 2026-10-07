"""Plugins: every backend the service uses, of each kind, found and built the same way.

Kinds: control (what an agent puts in front of the owner to tap), tts (the voice), stt (transcribes the owner's
recordings), rewrite (turns a message into what the voice reads), notify (reaches the owner: app push, iMessage),
phone (carries a live call: docs/voice-calls.md).
What each kind must have is REQUIRED; what it may also offer, such as streaming, is OPTIONAL. docs/plugins.md
has the whole contract.

A config table picks a kind's plugin by name, with that plugin's own table under it:

    [tts]
    backend = "piper"
    [tts.piper]
    voice_dir = "~/voices"

The name is a built-in, a plugin an installed package registers under the entry point group nanotea.<kind>,
or "module:Class" for a module on the path (plugin_path in the config adds directories to it). The plugin is
called as factory(table, ctx) and must say which plugin API it was written for: api = API. It may also declare
OPTIONS, a tuple of Option(key, kind, about, default), and SECRETS, the env_file keys it reads: then a key in its
table that it doesn't declare stops the service, and Settings > Configuration says what each option does.
"""

import importlib
import inspect
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

from nanotea.config import NOTIFY, ConfigError, resolve, unknown_keys

API = 1

BUILTIN = {
    "control": {"choice": "nanotea.controls:Choice", "checklist": "nanotea.controls:Checklist"},
    "tts": {"speechify": "nanotea.tts:Speechify", "command": "nanotea.tts:Command", "kokoro": "nanotea.tts:Kokoro",
            "kokoro-server": "nanotea.tts:KokoroServer"},
    "stt": {"command": "nanotea.stt:Command"},
    "rewrite": {"identity": "nanotea.rewrite:IdentityRewriter", "command": "nanotea.rewrite:CommandRewriter",
                "claude": "nanotea.rewrite:ClaudeRewriter"},
    "notify": {"push": "nanotea.notify:Push", "imessage": "nanotea.notify:IMessage"},
    "phone": {},
}

# What a plugin of each kind must have, and what it may also offer. docs/plugins.md says what each one does.
REQUIRED = {
    "control": ("name", "about", "check", "render", "act"),
    "tts": ("ext", "voices", "synthesize"),
    "stt": ("transcribe",),
    "rewrite": ("rewrite",),
    "notify": ("send",),
    "phone": ("media", "ring", "calls"),
}
OPTIONAL = {
    "control": (("style",), ("script",)),  # CSS and JS the page loads once
    "tts": (("stream", "stream_media"),),  # speech as it is made, for calls
    "stt": (("listen", "listen_media"),),  # words as they are spoken, for calls
    "rewrite": (),
    "notify": (),
    "phone": (),
}
MEDIA_KEYS = {"codec": str, "rate": int, "channels": int}


@dataclass(frozen=True)
class Context:
    """What the service gives every plugin it builds."""
    kind: str
    name: str
    env: dict[str, str]  # env_file's secrets
    owner: str
    app_name: str
    data: Path  # the service's data directory
    services: dict[str, Any] = field(default_factory=dict)  # notify: "push", the Web Push book

    def storage(self) -> Path:
        """A directory of the plugin's own under the data directory, made on first use."""
        d = self.data / "plugins" / f"{self.kind}-{self.name.replace(':', '-').replace('/', '-')}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def need(self, table: dict, key: str, kind_of: type | tuple = object):
        """table[key], or a ConfigError naming the table and key."""
        if key not in table:
            raise ConfigError(f"[{self.kind}.{self.name}] needs {key}")
        if not isinstance(table[key], kind_of):
            raise ConfigError(f"[{self.kind}.{self.name}] {key} has the wrong type: {type(table[key]).__name__}")
        return table[key]

    def program(self, table: dict, key: str = "argv", kind_of: type = list):
        """table[key], an argv list or (kind_of str) a command, whose program is on this machine: checked at start,
        not when the owner first plays or records something."""
        value = self.need(table, key, kind_of)
        argv = [value] if isinstance(value, str) else value
        if not argv or not all(isinstance(a, str) and a for a in argv):
            raise ConfigError(f"[{self.kind}.{self.name}] {key} must be a program and its arguments, as strings")
        first = argv[0]
        if "/" in first:
            found = resolve(first)
            if not (found.is_file() and os.access(found, os.X_OK)):
                raise ConfigError(f"[{self.kind}.{self.name}] {key}: {found} is not a program")
        elif shutil.which(first) is None:
            raise ConfigError(f"[{self.kind}.{self.name}] {key}: {first!r} is not on this service's PATH; install "
                              "it, or name it by its full path")
        return value

    def secret(self, key: str) -> str:
        if not self.env.get(key):
            raise ConfigError(f"[{self.kind}] backend {self.name} needs {key} in env_file")
        return self.env[key]


@dataclass(frozen=True)
class Found:
    kind: str
    name: str
    ref: str  # "module:attr"
    origin: str  # built-in, the distribution that registered it, or "module"

    def load(self) -> Callable:
        module, _, attr = self.ref.partition(":")
        if not module or not attr:
            raise ConfigError(f"[{self.kind}] {self.ref!r} is not module:Class")
        try:
            obj = importlib.import_module(module)
        except ImportError as err:
            raise ConfigError(f"[{self.kind}] can't import {module} for {self.name}: {err}") from err
        for part in attr.split("."):
            if not hasattr(obj, part):
                raise ConfigError(f"[{self.kind}] {module} has no {attr}")
            obj = getattr(obj, part)
        return obj

    def about(self) -> str:
        """The first paragraph of the plugin's docstring, on one line."""
        doc = inspect.getdoc(self.load()) or ""
        return " ".join(doc.split("\n\n")[0].split())


def add_path(dirs: list[str]) -> None:
    """plugin_path: directories, relative to the config's, whose modules "module:Class" can name."""
    for d in dirs:
        p = resolve(d)
        if not p.is_dir():
            raise ConfigError(f"plugin_path: {p} is not a directory")
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def available(kind: str) -> dict[str, Found]:
    """Every plugin of this kind by name: built-in, then installed. Two with one name stop the service."""
    if kind not in BUILTIN:
        raise ConfigError(f"no plugin kind {kind!r}; kinds: {', '.join(BUILTIN)}")
    found = {name: Found(kind, name, ref, "built-in") for name, ref in BUILTIN[kind].items()}
    for ep in entry_points(group=f"nanotea.{kind}"):
        dist = ep.dist.name if ep.dist else "an unknown package"
        if ep.name in found:
            raise ConfigError(f"[{kind}] {ep.name!r} is both {found[ep.name].origin} and from {dist}; uninstall one")
        found[ep.name] = Found(kind, ep.name, ep.value, dist)
    return found


def find(kind: str, name) -> Found:
    if not isinstance(name, str) or not name:
        raise ConfigError(f"[{kind}] backend must be named")
    if ":" in name:
        return Found(kind, name, name, "module")
    plugins = available(kind)
    if name not in plugins:
        raise ConfigError(f"[{kind}] backend must be one of {', '.join(plugins)}, or module:Class; not {name!r}")
    return plugins[name]


def build(kind: str, name: str, table: dict, ctx_args: dict):
    """One plugin of kind, built from its table, checked against the plugin API."""
    found = find(kind, name)
    factory = found.load()
    api = getattr(factory, "api", None)
    if api != API:
        raise ConfigError(f"[{kind}] {name} is for plugin API {api!r}; this nanotea has API {API} "
                          f"(a plugin says which with api = {API})")
    if not isinstance(table, dict):
        raise ConfigError(f"[{kind}.{name}] must be a table")
    plugin = factory(table, Context(kind=kind, name=name, **ctx_args))
    missing = [a for a in REQUIRED[kind] if not hasattr(plugin, a)]
    if missing:
        raise ConfigError(f"[{kind}] {name} is not a {kind} plugin: it has no {', '.join(missing)}")
    for group in OPTIONAL[kind]:
        has = [a for a in group if hasattr(plugin, a)]
        if has and len(has) < len(group):
            raise ConfigError(f"[{kind}] {name} has {', '.join(has)} without "
                              f"{', '.join(a for a in group if a not in has)}; a plugin offers all of them or none")
    for attr in ("media", "stream_media", "listen_media"):
        if hasattr(plugin, attr):
            check_media(f"[{kind}] {name} {attr}", getattr(plugin, attr))
    return plugin


def check_media(what: str, media) -> None:
    """Audio on a live call is described as {"codec": "pcm_s16le" or "pcm_mulaw" or "opus", "rate": Hz,
    "channels": n}."""
    if not (isinstance(media, dict) and set(media) == set(MEDIA_KEYS)
            and all(isinstance(media[k], t) and not isinstance(media[k], bool) for k, t in MEDIA_KEYS.items())):
        raise ConfigError(f"{what} must be {{\"codec\": str, \"rate\": int, \"channels\": int}}; not {media!r}")


def offers(kind: str, plugin) -> list[str]:
    """The optional capabilities a built plugin has, by their first attribute."""
    return [group[0] for group in OPTIONAL[kind] if hasattr(plugin, group[0])]


def check_tables(kind: str, cfg: dict, own: tuple) -> None:
    """Every key of [kind] besides its own is a table for a plugin that exists, with only the keys it declares."""
    for key, value in cfg.items():
        if key in own:
            continue
        if ":" not in key and key not in available(kind):
            raise ConfigError(f"unknown key [{kind}] {key!r}; keys are {', '.join(own)}, and a table for a plugin: "
                              f"{', '.join(available(kind))}")
        if not isinstance(value, dict):
            raise ConfigError(f"[{kind}.{key}] must be a table")
        declared = getattr(find(kind, key).load(), "OPTIONS", None)
        if declared is not None:
            unknown_keys(value, declared, f"[{kind}.{key}]")


def describe(kind: str, name: str, table: dict) -> dict:
    """What the Settings page says of a plugin: where it is from, what it does, and each of its options as this
    config has it (given in the table, or the default)."""
    found = find(kind, name)
    factory = found.load()
    declared = getattr(factory, "OPTIONS", None)
    if declared is None:  # a plugin that doesn't say what it takes: only what the config gave it
        options = [{"key": k, "value": v, "source": "config", "about": "", "takes": "", "required": False,
                    "default": None, "field": {"input": "json"}, "machine": True} for k, v in table.items()]
    else:
        options = [{"key": o.key, "value": table[o.key] if o.key in table else (None if o.required else o.default),
                    "source": "config" if o.key in table else ("missing" if o.required else "default"),
                    "about": o.about, "takes": o.takes, "required": o.required,
                    "default": None if o.required else o.default, "field": o.field,
                    "machine": o.machine or ":" in name} for o in declared]
    return {"kind": kind, "name": name, "origin": found.origin, "about": found.about(),
            "declared": declared is not None, "options": options, "secrets": list(getattr(factory, "SECRETS", ()))}


def make(kind: str, cfg: dict, ctx_args: dict):
    """The plugin a [kind] table names with backend, built from [kind.<backend>]."""
    if not isinstance(cfg, dict):
        raise ConfigError(f"[{kind}] is missing: name a backend, as backend = \"...\"; nanotea plugins lists them")
    check_tables(kind, cfg, ("backend",))
    name = cfg.get("backend")
    return build(kind, name, cfg.get(name, {}) if isinstance(name, str) else {}, ctx_args)


def make_many(kind: str, names: list, cfg: dict, ctx_args: dict) -> list:
    """Several plugins of kind by name, each from its table in cfg (as [notify] now and escalate)."""
    if not isinstance(names, list):
        raise ConfigError(f"[{kind}] lists plugins by name: [\"push\"]")
    check_tables(kind, cfg, ("use",) if kind == "control" else tuple(o.key for o in NOTIFY))
    return [build(kind, n, cfg.get(n, {}) if isinstance(n, str) else {}, ctx_args) for n in names]


def make_controls(cfg: dict, ctx_args: dict) -> dict:
    """[control] use: the controls agents may attach, by the type they use; each built from [control.<name>]."""
    from nanotea.controls import TYPE
    table = cfg.get("control")
    if not isinstance(table, dict) or "use" not in table:
        raise ConfigError('[control] needs use, the controls agents may attach: use = ["choice", "checklist"], '
                          'or [] for none')
    out = {}
    for plugin in make_many("control", table["use"], table, ctx_args):
        name = plugin.name
        if not (isinstance(name, str) and re.fullmatch(TYPE, name)):
            raise ConfigError(f"[control] a control's name is a short lowercase slug, not {name!r}")
        if name in out:
            raise ConfigError(f"[control] two controls are named {name!r}; use one")
        for attr in ("about", "style", "script"):
            if hasattr(plugin, attr) and not isinstance(getattr(plugin, attr), str):
                raise ConfigError(f"[control] {name}.{attr} must be a string")
        out[name] = plugin
    return out


def build_all(cfg: dict, ctx: dict) -> dict:
    """Every plugin a checked config names, built as the service starts with them: tts, stt, rewrite, notify_now,
    notify_escalate and controls."""
    ncfg = cfg["notify"]
    return {"tts": make("tts", cfg.get("tts"), ctx), "stt": make("stt", cfg.get("stt"), ctx),
            "rewrite": make("rewrite", cfg.get("rewrite"), ctx),
            "notify_now": make_many("notify", ncfg["now"], ncfg, ctx),
            "notify_escalate": make_many("notify", ncfg["escalate"], ncfg, ctx),
            "controls": make_controls(cfg, ctx)}


def media_str(media: dict) -> str:
    return f"{media['codec']} {media['rate']} Hz x{media['channels']}"


def main(argv: list[str]) -> None:
    """nanotea plugins [--options] [--check]: every plugin of every kind, which the config uses, with --options what
    each takes, and with --check, builds those."""
    import argparse

    from nanotea.config import load_config, load_env_file
    p = argparse.ArgumentParser(prog="nanotea plugins",
                                description="List the plugins of every kind, and which ones the config uses.")
    p.add_argument("--options", action="store_true",
                   help="list each plugin's config options: what each takes, its default, and what it does")
    p.add_argument("--check", action="store_true",
                   help="build the plugins the config uses, as the service would, and report what each offers")
    a = p.parse_args(argv)
    cfg = load_config()
    add_path(cfg.get("plugin_path", []))
    used = {kind: [cfg.get(kind, {}).get("backend")] for kind in ("tts", "stt", "rewrite")}
    used["control"] = list(cfg.get("control", {}).get("use", []))
    used["notify"] = list(dict.fromkeys(cfg["notify"]["now"] + cfg["notify"]["escalate"]))
    if "phone" in cfg:
        used["phone"] = [cfg["phone"].get("backend")]
    for kind in BUILTIN:
        print(f"{kind}:")
        plugins = available(kind)
        for name in used.get(kind, []):
            if isinstance(name, str) and ":" in name:
                plugins[name] = find(kind, name)
        if not plugins:
            print("  (none installed)")
        for name, f in plugins.items():
            mark = "*" if name in used.get(kind, []) else " "
            print(f" {mark} {name:<12} {f.origin:<12} {f.about()}")
            if a.options:
                info = describe(kind, name, {})
                if not info["declared"]:
                    print("      (does not say what it takes)")
                for o in info["options"] if info["declared"] else []:
                    need = "required" if o["required"] else f"default {json.dumps(o['default'])}"
                    print(f"      {o['key']}: {o['takes']}; {need}. {o['about']}")
                for key in info["secrets"]:
                    print(f"      {key}: a secret in env_file")
    if not a.check:
        return
    from nanotea.push import PushBook
    data = resolve(cfg["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    ctx = {"env": load_env_file(cfg.get("env_file")), "owner": cfg["app"]["owner"], "app_name": cfg["app"]["name"],
           "data": data, "services": {"push": PushBook(data, cfg["notify"].get("push", {}).get("subject"))}}
    failed = False
    for kind, names in used.items():
        for name in names:
            table = cfg.get(kind, {}).get(name, {}) if isinstance(name, str) else {}
            try:
                plugin = build(kind, name, table, ctx)
                said = [f"{len(plugin.voices())} voices", f".{plugin.ext}"] if kind == "tts" else []
                if kind == "control":
                    said.append(f"type {plugin.name!r}")
                if kind == "phone":
                    said.append(media_str(plugin.media))
                said += (offers(kind, plugin) if kind == "control"
                         else [f"{cap} {media_str(getattr(plugin, cap + '_media'))}" for cap in offers(kind, plugin)])
                print(f"ok      {kind} {name}{': ' + ', '.join(said) if said else ''}")
            except Exception as err:
                failed = True
                print(f"FAILED  {kind} {name or '(no backend)'}: {type(err).__name__}: {err}")
    if failed:
        sys.exit(1)
