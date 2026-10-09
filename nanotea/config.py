"""Config and secrets loading."""

import copy
import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# py_vapid's own rule for a subject it will sign, copied: mailto:user@domain, or https://host with no port or path.
# tests/test_reach.py checks the two agree.
PUSH_SUBJECT = re.compile(r"^(mailto:.+@((localhost|[%\w-]+(\.[%\w-]+)+|([0-9a-f]{1,4}):+([0-9a-f]{1,4})?)))|"
                          r"https:\/\/(localhost|[\w-]+\.[\w\.-]+|([0-9a-f]{1,4}:+)+([0-9a-f]{1,4})?)$", re.I)

PACKAGE = Path(__file__).resolve().parent
# NANOTEA_CONFIG, or the user's config dir. Relative paths in the config (data_dir, env_file, command argv run
# there) are relative to the config file's directory.
CONFIG = Path(os.environ.get("NANOTEA_CONFIG") or
              Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "nanotea" / "config.toml")
BASE = CONFIG.expanduser().resolve().parent


class ConfigMissing(SystemExit):
    pass


class ConfigError(ValueError):
    """The config names something the service can't use."""


class ConfigInvalid(SystemExit):
    pass


REQUIRED = object()
TYPE = {str: "text", int: "a whole number", float: "a number", bool: "true or false", list: "a list",
        dict: "a table"}


@dataclass(frozen=True)
class Option:
    """One key of a config table: what it takes, its default (REQUIRED if there is none, None for "not set"), and
    what it does. The Settings page, the example config and the unknown-key check all come from these. kind: the
    type, or a tuple of types, a value may have; lo and hi: the range of a number; choices: the allowed values.
    machine: it picks a program the service runs, code it loads, or where it keeps its data or secrets, so the app
    changes it only while the owner allows that from the machine (configedit)."""
    key: str
    kind: type | tuple
    about: str
    default: object = REQUIRED
    choices: tuple = ()
    lo: float | None = None
    hi: float | None = None
    machine: bool = False

    @property
    def required(self) -> bool:
        return self.default is REQUIRED

    @property
    def takes(self) -> str:
        """What a value may be, in words."""
        if self.choices:
            return "one of " + ", ".join(map(str, self.choices))
        kinds = self.kind if isinstance(self.kind, tuple) else (self.kind,)
        what = " or ".join(TYPE[k] for k in kinds)
        if self.lo is not None and self.hi is not None:
            what += f", {self.lo:g} to {self.hi:g}"
        return what

    @property
    def field(self) -> dict:
        """How the Configuration page edits it."""
        kinds = self.kind if isinstance(self.kind, tuple) else (self.kind,)
        if self.choices:
            return {"input": "choice", "choices": list(self.choices)}
        if kinds == (bool,):
            return {"input": "bool"}
        if set(kinds) <= {int, float}:
            return {"input": "number", "step": "1" if kinds == (int,) else "any", "lo": self.lo, "hi": self.hi,
                    "float": kinds == (float,)}
        if kinds == (str,):
            return {"input": "text"}
        return {"input": "json"}


def defaults(options: tuple) -> dict:
    """The options that have a default, as a table."""
    return {o.key: o.default for o in options if o.default is not REQUIRED and o.default is not None}


def unknown_keys(table: dict, options: tuple, where: str) -> None:
    """A key the table's options don't have stops the service, naming the table and the key."""
    keys = [o.key for o in options]
    extra = sorted(set(table) - set(keys))
    if extra:
        raise ConfigError(f"{where} has no key {', '.join(extra)}; keys are {', '.join(keys) or 'none'}")


class Config(dict):
    """The config as the service runs with it: path, the file it was read from, and given, what that file said
    before the defaults were filled in."""
    path: Path
    given: dict


# The service's own keys. The plugin tables ([rewrite], [tts], [stt], [control], [notify.<name>], [phone]) and
# [theme], [audio], [bang], [delivery] and [settings] are checked by what reads them, each against its own options.
TOP = (
    Option("host", str, "The address the service listens on. 0.0.0.0 only if the proxy reaches it from another "
                        "host or container.", "127.0.0.1"),
    Option("port", int, "The port it listens on, and the one agents on this machine use.", 7447, lo=1, hi=65535),
    Option("public_url", str, "The HTTPS address browsers use, scheme and host only, as a browser sends it. A "
                              "reverse proxy holds the certificate and forwards here."),
    Option("trusted_proxies", list, "Addresses of the proxies whose X-Forwarded headers are believed. Requests "
                                    "from anywhere else are not proxied, and their pages say they are not paired.",
           []),
    Option("data_dir", str, "Where the service keeps everything it records: messages, recordings, settings. "
                            "Relative to the config's directory.", "data", machine=True),
    Option("env_file", str, "A file of KEY=VALUE secrets, such as SPEECHIFY_API_KEY. Read at start, never copied or "
                            "shown.", None, machine=True),
    Option("plugin_path", list, "Directories, relative to the config's, whose modules plugins named "
                                "\"module:Class\" may be in.", [], machine=True),
)
TABLES = ("app", "theme", "control", "rewrite", "tts", "stt", "notify", "audio", "settings", "bang", "delivery",
          "phone")
APP = (
    Option("name", str, "What the app calls itself: its title, home screen name and leaf.", "Nanotea"),
    Option("owner", str, "Who the messages are for: shown in the app, and what the rewriter and the agents call "
                         "you."),
    Option("channels", dict, "The channels a new install starts with: name = \"topic\". You can make more in the "
                             "app.", {}),
    Option("groups", list, "The sidebar's groups on a new install: { name, members }, members being agents' names. "
                           "Kept in data/groups.json after the first start.", []),
)
NOTIFY = (
    Option("now", list, "Notifiers that run when a message is ready: [\"push\"] for the web app's notifications.",
           []),
    Option("escalate", list, "Notifiers that run when a message goes escalate_after_min without a response: "
                             "[\"imessage\"] texts once. [] is off.", []),
    Option("escalate_after_min", int, "Minutes before an unanswered message escalates. A question needs an "
                                      "answer; anything else needs opening.", 30),
)


def _fill(table: dict, options: tuple, where: str) -> None:
    for o in options:
        name = f"{where}{o.key}"
        kinds = o.kind if isinstance(o.kind, tuple) else (o.kind,)
        if o.key not in table:
            if o.required:
                raise ConfigError(f"{name} is required")
            if o.default is not None:
                table[o.key] = type(o.default)(o.default) if isinstance(o.default, (list, dict)) else o.default
        elif type(table[o.key]) not in kinds:
            raise ConfigError(f"{name} must be {TYPE[kinds[0]]}, not {table[o.key]!r}")


def check(cfg: dict) -> dict:
    """cfg with the service's own keys checked and their defaults filled in."""
    for key, value in cfg.items():
        if key not in (o.key for o in TOP) and key not in TABLES:
            raise ConfigError(f"unknown key {key!r}; known: "
                              f"{', '.join([*(o.key for o in TOP), *(f'[{t}]' for t in TABLES)])}")
        if key in TABLES and not isinstance(value, dict):
            raise ConfigError(f"{key} must be a table, [{key}]")
    _fill(cfg, TOP, "")
    url = urlsplit(cfg["public_url"])
    if url.scheme not in ("http", "https") or not url.hostname or url.path or url.query or url.fragment:
        raise ConfigError(f"public_url must be the address browsers use, scheme and host only, such as "
                          f"https://nanotea.example.com or http://127.0.0.1:7447; not {cfg['public_url']!r}")
    if url.port == {"http": 80, "https": 443}[url.scheme] or url.netloc != url.netloc.lower():
        raise ConfigError(f"public_url must be written as browsers send it, in lower case and without a default "
                          f"port: not {cfg['public_url']!r}")
    if not 0 < cfg["port"] < 65536:
        raise ConfigError(f"port must be 1 to 65535, not {cfg['port']}")
    for proxy in cfg["trusted_proxies"]:
        try:
            ipaddress.ip_address(proxy)
        except ValueError as err:
            raise ConfigError(f"trusted_proxies: {err}") from err
    app = cfg.setdefault("app", {})
    if "theme_color" in app:
        raise ConfigError("[app] theme_color is gone: the theme sets the app's colors. Remove it, and pick a "
                          "theme in Settings or with [theme] use (docs/service.md)")
    for key in app:
        if key not in (o.key for o in APP):
            raise ConfigError(f"unknown key [app] {key!r}; known: {', '.join(o.key for o in APP)}")
    _fill(app, APP, "[app] ")
    notify = cfg.setdefault("notify", {})
    _fill(notify, NOTIFY, "[notify] ")
    # Beside these keys, [notify] holds a table for each plugin it names; plugins.make_many checks those.
    if "push" in notify["now"] + notify["escalate"]:
        subject = notify.get("push", {}).get("subject")
        if not isinstance(subject, str) or not subject.startswith(("mailto:", "https://")):
            raise ConfigError("[notify.push] subject is required while push is in [notify] now or escalate: a "
                              "contact the push services see, mailto:you@example.com or an https address")
        if not PUSH_SUBJECT.match(subject):
            raise ConfigError(f"[notify.push] subject {subject!r} is refused by the push library, so every push "
                              "would fail: use mailto:you@example.com, or https://host with no port or path")
    return cfg


def load_config(path: Path = CONFIG) -> dict:
    path = path.expanduser()
    if not path.exists():
        raise ConfigMissing(f"nanotea: no config at {path}; write one with: nanotea init "
                            "(or name another with NANOTEA_CONFIG)")
    with path.open("rb") as f:
        try:
            raw = tomllib.load(f)
            given = copy.deepcopy(raw)
            cfg = Config(check(raw))
        except (ConfigError, tomllib.TOMLDecodeError) as err:
            raise ConfigInvalid(f"nanotea: config {path}: {err}") from err
    cfg.path, cfg.given = path.resolve(), given
    return cfg


def resolve(p: str) -> Path:
    """A path from the config: ~ expanded, relative to the config file's directory."""
    return BASE / Path(p).expanduser()


def load_env_file(path: str | None) -> dict[str, str]:
    """Parse KEY=VALUE lines, allowing `export` prefixes, quoted values, and # comments. None: no env_file."""
    return {} if path is None else parse_env(resolve(path).read_text(), path)


def parse_env(text: str, source: str) -> dict[str, str]:
    """env_file's KEY=VALUE lines; source names it in errors."""
    env = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if not sep:
            raise ValueError(f"{source}:{n}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value
    return env
