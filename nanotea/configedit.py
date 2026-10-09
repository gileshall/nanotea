"""Changing the config from the app. An edit is written into config.toml in place, keeping its comments and
layout, and only after the whole changed config has been checked the way the service checks it at start: in a
separate process, so a plugin it names is built there and not in the service. The service then restarts to use
it, and the file as it was is kept in data/config-history/.

What would change the programs the service runs, the code it loads, or where it keeps its data and secrets is
the machine's to allow (Option.machine): the app changes it only while the owner has turned Programs from this
app on, with the host key (nanotea config programs on).

nanotea config check [FILE]          check a config as the service would start with it; - reads JSON on stdin
nanotea config programs on|off|status
"""

import copy
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BARE = re.compile(r"[A-Za-z0-9_-]+")
SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
CHECK_TIMEOUT_S = 180  # building a local voice loads its model


class EditError(ValueError):
    """An edit that can't be made: says why, and what to do instead."""


# Writing TOML

def key(k: str) -> str:
    return k if BARE.fullmatch(k) else json.dumps(k, ensure_ascii=False)


def where(table: tuple, k: str | None = None) -> str:
    """How errors and the page name a key: [tts.command] argv, or argv for a top-level key."""
    head = f"[{'.'.join(key(t) for t in table)}]" if table else ""
    return f"{head} {k}".strip() if k is not None else head or "the top of the file"


def value(v) -> str:
    """v as a TOML value, on one line."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise EditError(f"a number must be finite, not {v}")
        return repr(v)
    if isinstance(v, str):  # JSON's escapes are TOML's, but JSON leaves DEL raw
        return json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(v, list):
        return "[" + ", ".join(value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{key(k)} = {value(x)}" for k, x in v.items()) + " }" if v else "{}"
    raise EditError(f"a config value is text, a number, true or false, a list or a table; not {v!r}")


def dump_table(path: tuple, table: dict) -> list[str]:
    """table as [path] and its subtables, for a value that was written as sections."""
    out = [f"[{'.'.join(key(t) for t in path)}]\n"]
    out += [f"{key(k)} = {value(v)}\n" for k, v in table.items() if not isinstance(v, dict)]
    for k, v in table.items():
        if isinstance(v, dict):
            out += ["\n", *dump_table(path + (k,), v)]
    return out


# Reading the file's layout

@dataclass(frozen=True)
class Stmt:
    """One statement of the file: a [table] header, or key = value over lines start to end."""
    start: int
    end: int
    table: tuple  # the table it is in; a header's own path
    keys: tuple  # a key's path within its table; () for a header
    array: bool  # in, or the header of, an array of tables [[...]]


def _path(doc) -> tuple:
    """The one path a single header or key parses to."""
    out = []
    while isinstance(doc, dict) and len(doc) == 1:
        (k, doc), = doc.items()
        out.append(k)
    return tuple(out)


def _key_text(chunk: str) -> str:
    """What comes before the = of a key = value statement."""
    quote = None
    for i, c in enumerate(chunk):
        if quote:
            if c == "\\" and quote == '"':
                continue
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "=":
            return chunk[:i]
    raise EditError(f"no = in {chunk.strip()!r}")


def layout(lines: list[str]) -> list[Stmt]:
    out, table, array, i = [], (), False, 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        if s.startswith("["):
            array = s.startswith("[[")
            doc = tomllib.loads(lines[i])
            table = _path(doc)
            out.append(Stmt(i, i, table, (), array))
            i += 1
            continue
        for j in range(i, len(lines)):
            try:
                tomllib.loads("".join(lines[i:j + 1]))
                break
            except tomllib.TOMLDecodeError:
                continue
        else:
            raise EditError(f"line {i + 1} of the config doesn't parse")
        keys = _path(tomllib.loads(_key_text("".join(lines[i:j + 1])) + "= 0\n"))
        out.append(Stmt(i, j, table, keys, array))
        i = j + 1
    return out


def _comment(line: str) -> str:
    """A one-line statement's trailing comment, with the space before it."""
    for m in re.finditer(r"\s*#", line):
        try:
            tomllib.loads(line[:m.start()])
            return line[m.start():].rstrip("\r\n")
        except tomllib.TOMLDecodeError:
            continue
    return ""


def _eol(lines: list[str]) -> str:
    return "\r\n" if lines and lines[0].endswith("\r\n") else "\n"


def _sections(stmts: list[Stmt], lines: list[str], path: tuple) -> list[tuple[int, int]]:
    """The line ranges of path's own header and every subtable's, each to the next header."""
    heads = [s for s in stmts if not s.keys]
    out = []
    for n, h in enumerate(heads):
        if h.table[:len(path)] == path:
            end = heads[n + 1].start if n + 1 < len(heads) else len(lines)
            out.append((h.start, end))
    return out


def _set(lines: list[str], table: tuple, k: str, v) -> list[str]:
    stmts = layout(lines)
    eol = _eol(lines)
    if any(s.array and table[:len(s.table)] == s.table for s in stmts if not s.keys):
        raise EditError(f"{where(table, k)} is in an array of tables, which this page doesn't change")
    kv = next((s for s in stmts if s.keys == (k,) and s.table == table), None)
    if kv:
        first = lines[kv.start]
        indent = first[:len(first) - len(first.lstrip())]
        name = _key_text(first).strip()
        comment = _comment(first) if kv.start == kv.end else ""
        return lines[:kv.start] + [f"{indent}{name} = {value(v)}{comment}{eol}"] + lines[kv.end + 1:]
    own = _sections(stmts, lines, table + (k,))
    if own and isinstance(v, dict):  # written as [table.k] sections: write them again
        new = [x.replace("\n", eol) for x in dump_table(table + (k,), v)] + [eol]
        out = list(lines)
        for start, end in reversed(own):
            del out[start:end]
        return out[:own[0][0]] + new + out[own[0][0]:]
    line = f"{key(k)} = {value(v)}{eol}"
    if not table:
        tops = [s for s in stmts if s.keys and not s.table]
        at = tops[-1].end + 1 if tops else next((s.start for s in stmts if not s.keys), len(lines))
        return lines[:at] + [line] + lines[at:]
    head = next((s for s in stmts if not s.keys and s.table == table), None)
    if head:
        mine = [s for s in stmts if s.keys and s.table == table and s.start > head.start]
        at = mine[-1].end + 1 if mine else head.start + 1
        return lines[:at] + [line] + lines[at:]
    # A new table: after the last section of its top-level table, or at the end.
    kin = _sections(stmts, lines, table[:1])
    at = kin[-1][1] if kin else len(lines)
    while at > 0 and not lines[at - 1].strip():
        at -= 1
    if at and not lines[at - 1].endswith(("\n", "\r")):
        lines = lines[:at - 1] + [lines[at - 1] + eol] + lines[at:]
    return lines[:at] + [eol, f"[{'.'.join(key(t) for t in table)}]{eol}", line] + lines[at:]


def _unset(lines: list[str], table: tuple, k: str) -> list[str]:
    stmts = layout(lines)
    kv = next((s for s in stmts if s.keys == (k,) and s.table == table), None)
    if kv:
        return lines[:kv.start] + lines[kv.end + 1:]
    own = _sections(stmts, lines, table + (k,))
    if own:
        out = list(lines)
        for start, end in reversed(own):
            del out[start:end]
        return out
    raise EditError(f"{where(table, k)} is not set in the file, so it already has its default")


def _apply(doc: dict, table: tuple, k: str, v, unset: bool) -> None:
    for t in table:
        doc = doc.setdefault(t, {})
        if not isinstance(doc, dict):
            raise EditError(f"{where(table)} is not a table in the file")
    if unset:
        doc.pop(k, None)
    else:
        doc[k] = v


def parse_changes(changes) -> list[tuple[tuple, str, object, bool]]:
    """[{table: [...], key, value} or {table, key, unset: true}, ...], checked."""
    if not isinstance(changes, list) or not changes:
        raise EditError("send changes: [{table: [...], key, value}, or {table, key, unset: true}]")
    out = []
    for c in changes:
        if not (isinstance(c, dict) and isinstance(c.get("table"), list)
                and all(isinstance(t, str) and t for t in c["table"]) and isinstance(c.get("key"), str) and c["key"]):
            raise EditError(f"a change is {{table: [names], key, value}}; not {c!r}")
        unset = c.get("unset") is True
        if unset == ("value" in c):
            raise EditError(f"{where(tuple(c['table']), c['key'])}: send value, or unset: true")
        v = c.get("value")
        if c.get("float") is True and type(v) is int:  # JSON writes 2.0 as 2; this key takes only a float
            v = float(v)
        out.append((tuple(c["table"]), c["key"], v, unset))
    return out


def edit(text: str, changes: list[tuple[tuple, str, object, bool]]) -> tuple[str, dict, dict]:
    """text with changes made in place: (new text, the config before, the config after). Each change is checked
    by reading the file back: one the file writes in a form this can't change in place is refused."""
    before = tomllib.loads(text)
    want = copy.deepcopy(before)
    lines = text.splitlines(keepends=True)
    for table, k, v, unset in changes:
        _apply(want, table, k, v, unset)
        lines = _unset(lines, table, k) if unset else _set(lines, table, k, v)
        try:
            got = tomllib.loads("".join(lines))
        except tomllib.TOMLDecodeError as err:
            raise EditError(f"{where(table, k)}: the file writes it, or its table, in a form this page can't change "
                            f"in place ({err}); change it in the file") from err
        if got != want:
            raise EditError(f"{where(table, k)}: the file writes it, or its table, as a dotted key or an inline "
                            "table, which this page can't change in place; change it in the file")
    return "".join(lines), before, want


# What only the machine allows

def machine_changes(before: dict, after: dict) -> list[str]:
    """The changed keys that pick the programs the service runs, the code it loads, or where it keeps its data and
    secrets."""
    from nanotea import plugins
    from nanotea.config import NOTIFY, TOP
    out = [o.key for o in TOP if o.machine and before.get(o.key) != after.get(o.key)]
    own = {"notify": tuple(o.key for o in NOTIFY), "control": ("use",)}
    for kind in ("tts", "stt", "rewrite", "notify", "control", "phone"):
        old, new = before.get(kind, {}), after.get(kind, {})
        if not (isinstance(old, dict) and isinstance(new, dict)):
            if old != new:
                out.append(where((kind,)))
            continue
        mine = own.get(kind, ("backend",))
        for k in mine:
            was, now = old.get(k), new.get(k)
            names = now if isinstance(now, list) else [now]
            had = was if isinstance(was, list) else [was]
            if any(isinstance(n, str) and ":" in n and n not in had for n in names):
                out.append(where((kind,), k))
        for name in sorted((set(old) | set(new)) - set(mine)):
            if old.get(name) == new.get(name):
                continue
            a, b = old.get(name, {}), new.get(name, {})
            declared = None if ":" in name or not (isinstance(a, dict) and isinstance(b, dict)) else \
                getattr(plugins.find(kind, name).load(), "OPTIONS", None)
            if declared is None:  # code of its own, or a plugin that doesn't say what it takes
                out.append(where((kind, name)))
                continue
            out += [where((kind, name), o.key) for o in declared if o.machine and a.get(o.key) != b.get(o.key)]
    old, new = before.get("settings", {}), after.get("settings", {})
    out += [where(("settings",), k) for k in HOST_ONLY if old.get(k) != new.get(k)]
    return out


HOST_ONLY = ("bang", "config_programs")  # switches only the host key turns on, wherever they are set


# Checking, as the service would start

def check_all(raw: dict, env: dict) -> None:
    """Everything the service checks of its config before it listens, without starting it. Raises what it would
    stop with."""
    from nanotea import audio, bang, delivery, plugins, settings
    from nanotea.board import BoardError, check_groups
    from nanotea.config import ConfigError, check, resolve
    from nanotea.push import PushBook
    from nanotea.themes import from_config as themes_from_config
    cfg = check(raw)
    themes_from_config(cfg)
    audio.settings(cfg)
    bang.settings(cfg)
    delivery.settings(cfg)
    if cfg.get("settings"):
        try:
            settings.check(cfg["settings"], "[settings] ")
        except settings.SettingsError as err:
            raise ConfigError(str(err)) from err
    try:
        check_groups(copy.deepcopy(cfg["app"]["groups"]))
    except BoardError as err:
        raise ConfigError(f"[app] groups: {err}") from err
    data = resolve(cfg["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    plugins.add_path(cfg.get("plugin_path", []))
    ctx = {"env": env, "owner": cfg["app"]["owner"], "app_name": cfg["app"]["name"], "data": data,
           "services": {"push": PushBook(data, cfg["notify"].get("push", {}).get("subject"))}}
    plugins.build_all(cfg, ctx)


def verify(text: str, env: dict | None) -> str | None:
    """Checks text as a config in another process: None if the service would start with it, or what it would say.
    env: the secrets to check with; None reads the env_file it names."""
    r = subprocess.run([sys.executable, "-m", "nanotea", "config", "check", "-"],
                       input=json.dumps({"toml": text, "env": env}), capture_output=True, text=True,
                       timeout=CHECK_TIMEOUT_S)
    if r.returncode == 0:
        return None
    if r.stdout.strip():
        return r.stdout.strip()
    err = r.stderr.strip().splitlines()
    return f"the check stopped: {err[-1] if err else f'exit {r.returncode}'}"


def _write(path: Path, text: str) -> None:
    """text into path, atomically, keeping its mode."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def save(path: Path, old: str, new: str, history: Path) -> Path:
    """new into the config at path, after keeping old in history. Returns where old was kept."""
    history.mkdir(parents=True, exist_ok=True)
    kept = history / f"{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')}.toml"
    _write(kept, old)
    _write(path, new)
    return kept


# Secrets in env_file

def env_line(name: str, v: str) -> str:
    if "\n" in v or "\r" in v:
        raise EditError(f"{name}: a secret is one line")
    if v and not re.search(r"[\s#'\"]", v):
        return f"{name}={v}"
    for q in "\"'":
        if q not in v:
            return f"{name}={q}{v}{q}"
    raise EditError(f"{name}: a value with both kinds of quote can't be written to env_file; set it in the file")


def edit_env(text: str, name: str, v: str | None) -> str:
    """env_file's text with name set to v, or removed (None). Other lines stay as they are."""
    if not SECRET_NAME.fullmatch(name):
        raise EditError(f"{name!r} is not a secret's name: capital letters, digits and _")
    lines = text.splitlines(keepends=True)
    at = [i for i, line in enumerate(lines)
          if line.strip().removeprefix("export ").partition("=")[0].strip() == name and "=" in line]
    if v is None:
        if not at:
            raise EditError(f"{name} is not in env_file")
        return "".join(line for i, line in enumerate(lines) if i not in at)
    new = env_line(name, v)
    if at:
        for i in at:
            lines[i] = new + ("\n" if lines[i].endswith("\n") else "")
        lines = [line for i, line in enumerate(lines) if i not in at[1:]]
        return "".join(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return "".join(lines) + new + "\n"


def secret_names() -> dict[str, list[str]]:
    """Every secret a plugin of any kind reads: name -> the plugins that read it."""
    from nanotea import plugins
    out: dict[str, list[str]] = {}
    for kind in plugins.BUILTIN:
        for name, found in plugins.available(kind).items():
            for s in getattr(found.load(), "SECRETS", ()):
                out.setdefault(s, []).append(f"{kind} {name}")
    return out


def main(argv: list[str]) -> None:
    import argparse
    p = argparse.ArgumentParser(prog="nanotea config", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check a config as the service would start with it")
    c.add_argument("file", nargs="?", help="the config; by default the service's. -: JSON {toml, env} on stdin")
    s = sub.add_parser("programs", help="let the app change what the service runs (needs the host key)")
    s.add_argument("state", choices=("on", "off", "status"))
    a = p.parse_args(argv)
    from nanotea.config import CONFIG, ConfigError, load_env_file
    if a.cmd == "check":
        if a.file == "-":
            got = json.load(sys.stdin)
            text, env, path = got["toml"], got["env"], CONFIG
        else:
            path = Path(a.file).expanduser() if a.file else CONFIG
            try:
                text, env = path.read_text(), None
            except OSError as err:
                sys.exit(f"nanotea config check: {err}")
        try:
            try:
                raw = tomllib.loads(text)
            except tomllib.TOMLDecodeError as err:
                raise ConfigError(f"{path} is not TOML: {err}") from err
            if env is None:
                env = load_env_file(raw.get("env_file"))
            check_all(raw, env)
        except Exception as err:  # what the service would stop with, said as it would say it
            print(str(err) if isinstance(err, (ConfigError, ValueError)) else f"{type(err).__name__}: {err}")
            sys.exit(1)
        if a.file != "-":
            print("ok: the service would start with this config")
        return
    from nanotea.client import Client, NanoteaError
    from nanotea.config import load_config
    from nanotea.credentials import host_key
    cfg = load_config()
    try:
        client = Client(cfg["port"], host_key(cfg))
        now = (client.get("/api/settings") if a.state == "status"
               else client.post("/api/settings", {"config_programs": a.state == "on"}))
    except NanoteaError as err:
        sys.exit(f"nanotea config programs: {err}")
    print(f"Programs from this app are {'on' if now['settings']['config_programs'] else 'off'}: the app "
          f"{'can' if now['settings']['config_programs'] else 'cannot'} change what the service runs, the code it "
          "loads, or where it keeps its data and secrets")
