"""What the service is running with, for the Settings pages: each part of the config as the service has it, where
each value comes from (the file, or the default), what it does, and the prompts sent to models. Secrets are never
here: a key from env_file is only set or missing, and any secret's value is scrubbed from every string."""

import json
import tomllib

from nanotea import audio, bang, configedit, delivery, plugins, quirks, sop, themes
from nanotea.config import APP, NOTIFY, TOP, Option
from nanotea.prompts import MAX_CHARS, NAMES
from nanotea.rewrite import OUTPUT_CONTRACT, SCHEMA, ClaudeRewriter, CommandRewriter, request
from nanotea.settings import FEATURES, OPTIONAL_TOOLS
from nanotea.settings import SCHEMA as SWITCHES

KINDS = {"tts": "voice", "stt": "transcriber", "rewrite": "rewriter", "phone": "phone"}


def show(value) -> str:
    """A value as the config file writes it (TOML's strings, numbers, booleans and lists read the same in JSON)."""
    return "not set" if value is None else json.dumps(value)


def where(kind: str, name: str | None = None) -> str:
    if name is None:
        return f"[{kind}]"
    return f'[{kind}."{name}"]' if ":" in name else f"[{kind}.{name}]"


def row(path: tuple, key: str, have: bool, value, required: bool, default, about: str, takes: str, field: dict,
        machine: bool) -> dict:
    """One key as the page shows and edits it. path: its table, as configedit names it; raw and default_raw are
    the values themselves, for the inputs."""
    return {"key": key, "path": list(path), "table": configedit.where(path) if path else "top level",
            "value": show(value) if have or not required else "not set",
            "source": "config" if have else ("missing" if required else "default"),
            "about": about, "takes": takes, "required": required, "default": "" if required else show(default),
            "raw": value if have or not required else None, "default_raw": None if required else default,
            "field": field, "machine": machine}


def rows(options: tuple, given: dict, path: tuple) -> list[dict]:
    """The options of one table, as the config has them: given, or the default."""
    return [row(path, o.key, o.key in given, given[o.key] if o.key in given else (None if o.required else o.default),
                o.required, o.default, o.about, o.takes, o.field, o.machine) for o in options]


def plugin_rows(info: dict, path: tuple) -> list[dict]:
    return [row(path, o["key"], o["source"] == "config", o["value"], o["required"], o["default"], o["about"],
                o["takes"], o["field"], o["machine"]) for o in info["options"]]


def secrets(app, names: list[str]) -> list[dict]:
    source = app.cfg.get("env_file")
    return [{"name": n, "set": n in app.env_set, "editable": source is not None,
             "from": f"env_file {source}" if source else "env_file, which the config doesn't name"} for n in names]


def other_plugins(app, kind: str, skip, given: dict) -> list[dict]:
    """The plugins of kind not in use, each with what it would take, so choosing one can set it up in one save."""
    out = []
    for n, f in plugins.available(kind).items():
        if n in skip:
            continue
        info = plugins.describe(kind, n, given.get(n, {}) if isinstance(given.get(n), dict) else {})
        out.append({"name": n, "origin": f.origin, "about": f.about(), "rows": plugin_rows(info, (kind, n)),
                    "secrets": secrets(app, info["secrets"])})
    return out


def backend_section(app, kind: str, plugin, about: str) -> dict:
    """[tts], [stt] or [rewrite]: the plugin in use, its options, the others one could choose."""
    cfg, given = app.cfg[kind], app.cfg.given[kind]
    name = cfg["backend"]
    info = plugins.describe(kind, name, given.get(name, {}))
    chosen = [row((kind,), "backend", True, name, True, None,
                  f"Which {KINDS[kind]} plugin the service uses. Built-in: {', '.join(plugins.available(kind))}; or "
                  "module:Class for your own (docs/plugins.md), which needs Programs from this app. Set up the "
                  "one you pick under Others, and save both together.", "text",
                  {"input": "text", "suggest": list(plugins.available(kind))}, ":" in name)]
    return {"id": kind, "title": f"{KINDS[kind].capitalize()} ({kind})", "about": about, "where": where(kind),
            "rows": chosen + plugin_rows(info, (kind, name)),
            "plugin": {"name": name, "origin": info["origin"], "about": info["about"], "declared": info["declared"]},
            "secrets": secrets(app, info["secrets"]), "others": other_plugins(app, kind, {name}, given),
            "facts": []}


def build(app) -> dict:
    """The whole effective configuration, in sections."""
    cfg, given = app.cfg, app.cfg.given
    out = []
    env_file = cfg.get("env_file")
    out.append({
        "id": "service", "title": "Service", "where": "the top of the config file", "plugin": None, "others": [],
        "about": "Where the service listens, the address browsers use, and where it keeps everything.",
        "rows": rows(TOP, given, ()),
        "secrets": [],
        "facts": [("Data directory in use", str(app.store.root)),
                  ("Secrets file", f"{app.env_path} ({len(app.env_set)} keys set; their names and values are "
                                   "never shown)" if env_file else "none: the config names no env_file"),
                  ("Pairing key", "data/reply_key, never shown; nanotea pair-link prints the link that pairs a device"),
                  ("Host key", "data/host_key, never shown or sent to a browser; nanotea bang on reads it"),
                  ("Agent tokens", f"{len(app.tokens.all())} issued, listed at /tokens")]})
    out.append({
        "id": "app", "title": "App", "where": where("app"), "plugin": None, "others": [], "secrets": [], "facts": [],
        "about": "Its name and whose messages these are. Channels and groups here only start a new install; "
                 "after that the app keeps them in data/.",
        "rows": rows(APP, given.get("app", {}), ("app",))})
    out.append(backend_section(app, "tts", app.tts,
                               "What reads a message aloud when you tap play. Agents pick one of its voices."))
    out[-1]["facts"] = [("Audio it makes", f".{app.tts.ext}")]
    out[-1]["voices"] = True
    out.append(backend_section(app, "stt", app.transcriber.stt,
                               "What turns your recordings into the words an agent gets. The recording itself is "
                               "kept and sent too."))
    out.append(backend_section(app, "rewrite", app.rewriter,
                               "What turns a message into the script the voice reads, and titles it. Its "
                               "instructions are on the Prompts page."))
    out[-1]["prompt"] = True
    out.append(notify_section(app))
    out.append(control_section(app))
    out.append({"id": "audio", "title": "Audio", "where": where("audio"), "plugin": None, "others": [],
                "secrets": [], "facts": [],
                "about": "How a message's speech and clips, or your takes sent together, are joined into one "
                         "track. Clips and takes sent alone are never re-encoded.",
                "rows": rows(audio.OPTIONS, given.get("audio", {}), ("audio",))})
    out.append({"id": "bang", "title": "Bang commands", "where": where("bang"), "plugin": None, "others": [],
                "secrets": [], "facts": [],
                "about": "Limits on the !command you type on an agent's line. Whether they are on at all is the "
                         "Bang commands switch under Settings, off by default.",
                "rows": rows(bang.OPTIONS, given.get("bang", {}), ("bang",))})
    out.append({"id": "delivery", "title": "Delivery", "where": where("delivery"), "plugin": None, "others": [],
                "secrets": [], "facts": [],
                "about": "How your messages reach each agent. Every mode keeps check and wait; push and listen also "
                         "hand them over while the agent works. Agents hear of a change with their next tool call.",
                "rows": rows(delivery.OPTIONS, given.get("delivery", {}), ("delivery",))})
    out.append({"id": "theme", "title": "Theme", "where": where("theme"), "plugin": None, "others": [],
                "secrets": [], "facts": [],
                "about": "What a new install starts with. You choose in Settings > Look, kept in data/theme.json; "
                         "that choice wins from then on.",
                "rows": rows(themes.OPTIONS, given.get("theme", {}), ("theme",))})
    owned = given.get("settings", {})
    out.append({"id": "defaults", "title": "Switch defaults", "where": where("settings"), "plugin": None,
                "others": [], "secrets": [], "facts": [],
                "about": "Your switches on the Settings page start from these on a new install; what you change "
                         "there is kept in data/settings.json and wins.",
                "rows": switch_rows(owned)})
    if "phone" in given:
        out.append(backend_section(app, "phone", None, "What places and takes your voice calls."))
    now = app.settings.get()
    view = {"file": str(cfg.path), "sections": out, "programs": now["config_programs"], "started": app.started,
            "changed": file_changed(cfg)}
    return scrub(view, app.secret_values)


def switch_rows(owned: dict) -> list[dict]:
    """[settings]: each switch's starting value. bang and config_programs start off; only the host key turns them
    on."""
    out = []
    for s in SWITCHES:
        field = {"bool": {"input": "bool"}, "choice": {"input": "choice", "choices": [v for v, _ in s.choices]},
                 "int": {"input": "number", "step": "1", "lo": s.lo, "hi": s.hi}}.get(s.kind, {"input": "json"})
        takes = {"bool": "true or false", "choice": "one of " + ", ".join(v for v, _ in s.choices),
                 "int": f"a whole number, {s.lo} to {s.hi}", "names": "a list of agents' names"}[s.kind]
        have = s.key in owned
        out.append(row(("settings",), s.key, have, owned[s.key] if have else s.default, False, s.default,
                       f"{s.label}: {s.about}", takes, field, s.key in configedit.HOST_ONLY))
    have = "tools" in owned
    out.append(row(("settings",), "tools", have, owned["tools"] if have else {t: True for t in OPTIONAL_TOOLS},
                   False, {t: True for t in OPTIONAL_TOOLS}, "Optional tools agents are shown: tool = true or false.",
                   f"a table of {', '.join(OPTIONAL_TOOLS)}", {"input": "json"}, False))
    return out


def file_changed(cfg) -> str | None:
    """What the page says when the file is no longer what the service started with: a save edits the file as it is
    now."""
    try:
        now = tomllib.loads(cfg.path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        return f"The file can't be read now ({err}): saving here will fail until it is fixed."
    if now != cfg.given:
        return ("The file has changed since the service started. Below is what the service runs with; saving "
                "edits the file as it is now and restarts with it.")
    return None


def notify_section(app) -> dict:
    cfg, given = app.cfg["notify"], app.cfg.given.get("notify", {})
    table = where("notify")
    names = list(dict.fromkeys(cfg["now"] + cfg["escalate"]))
    listed = []
    for n in names:
        info = plugins.describe("notify", n, given.get(n, {}))
        listed.append({"name": n, "origin": info["origin"], "about": info["about"],
                       "rows": plugin_rows(info, ("notify", n)), "secrets": secrets(app, info["secrets"])})
    return {"id": "notify", "title": "Notifications", "where": table, "plugin": None,
            "about": "How you are reached when a message is ready. Which agents and hours notify is yours to set "
                     "on the Notifications page.",
            "rows": rows(NOTIFY, given, ("notify",)), "secrets": [], "listed": listed,
            "others": other_plugins(app, "notify", names, given),
            "facts": [("Browsers subscribed to push", str(len(app.push.subscriptions())) if app.push_on
                       else "notifications are off: no push in now or escalate")]}


def control_section(app) -> dict:
    table = app.cfg["control"]
    given = app.cfg.given.get("control", {})
    listed = []
    for name in table["use"]:
        info = plugins.describe("control", name, given.get(name, {}))
        about = getattr(plugins.find("control", name).load(), "about", info["about"])
        listed.append({"name": name, "origin": info["origin"], "about": about,
                       "rows": plugin_rows(info, ("control", name)), "secrets": []})
    return {"id": "control", "title": "Controls", "where": where("control"), "plugin": None,
            "about": "What agents may put in front of you to tap, on a message or pinned to their line.",
            "rows": rows((Option("use", list, "The controls agents may attach, by plugin name. [] turns controls "
                                              "off.", ["choice", "checklist"]),), given, ("control",)),
            "secrets": [], "listed": listed, "facts": [],
            "others": other_plugins(app, "control", table["use"], given)}


def voices(app) -> dict:
    """The voice engine's voices, asked when the page is open: a server may be down, which is said, not hidden."""
    try:
        found = app.tts.voices()
    except Exception as err:
        return {"error": f"{type(err).__name__}: {err}", "voices": []}
    return {"error": None, "voices": [{"id": v["id"], "name": v.get("name"), "gender": v.get("gender"),
                                       "locale": v.get("locale")} for v in found]}


def prompts(app) -> dict:
    """Every text the service sends to a model: the rewriter's instructions (the owner's to change), the working
    agreement agents get, the tools they are shown, and the other lines the service writes to agents."""
    from nanotea.hook import note
    from nanotea.mcp_server import on_call_text
    owner, now = app.cfg["app"]["owner"], app.settings.get()
    backend = app.cfg["rewrite"]["backend"]
    if isinstance(app.rewriter, (ClaudeRewriter, CommandRewriter)):
        used = f"Used: the {backend} rewriter sends it with every message."
    elif backend == "identity":
        used = ("Not used now: the rewriter is identity, which reads the original text and sends nothing to a "
                "model. Set [rewrite] backend to claude or command in the config file, and restart.")
    else:
        used = f"The rewriter is {backend}, your own plugin; it may or may not use this."
    p = app.prompts
    custom = p.custom("rewrite")
    example = request("The build passed. Want me to deploy?", "Builder (builder)", None, True)
    settings_label = {k: label for k, label, _ in FEATURES}
    settings_label.update({f"tools.{t}": f"tool {t}" for t in OPTIONAL_TOOLS})
    lines = [{"text": text, "added_by": "always" if f in sop.ALWAYS else settings_label.get(f, f),
              "on": f in sop.ALWAYS or sop.on(now, f)} for f, text in sop.lines(owner, now, "finish")]
    out = {
        "owner": owner,
        "rewrite": {"used": used, "custom": custom is not None, "template": p.template("rewrite"),
                    "builtin": p.builtin("rewrite"), "text": p.text("rewrite"), "kept_at": str(p.path("rewrite")),
                    "max_chars": MAX_CHARS, "contract": OUTPUT_CONTRACT.strip(), "request": example,
                    "schema": json.dumps(json.loads(SCHEMA), indent=2), "name": "rewrite", "names": list(NAMES)},
        "agreement": {"text": sop.render(owner, now), "brief": sop.render(owner, now, brief=True),
                      "brief_max": sop.BRIEF_MAX, "lines": lines,
                      "brief_for": [h for h, q in quirks.QUIRKS.items() if q.instructions_max],
                      "idle": {k: v.format(owner=owner) for k, v in sop.IDLE.items()},
                      "delivery": {k: v.format(owner=owner) for k, v in sop.DELIVERY.items()}},
        "tools": [{"name": n, "description": d["description"]} for n, d in app.tool_defs.items()],
        "other": [{"what": "The on_call prompt agents can invoke", "text": on_call_text(owner, None)},
                  {"what": "What the stop and wake hooks say to an agent with messages waiting",
                   "text": note(owner, {"line": 2, "events": 0, "answers": 1, "talk": 0})}],
    }
    return scrub(out, app.secret_values)


def scrub(obj, secret_values: list[str]):
    """obj with every secret's value in any string replaced, so a secret that strayed into a command line or a
    prompt still doesn't reach a page."""
    if isinstance(obj, str):
        for v in secret_values:
            obj = obj.replace(v, "[hidden]")
        return obj
    if isinstance(obj, dict):
        return {k: scrub(v, secret_values) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v, secret_values) for v in obj]
    return obj


def summary(app) -> list[tuple[str, str, str]]:
    """One line for each part of the config, for the top of Settings: (label, what is in use, where to read more)."""
    cfg = app.cfg
    mix = app.mix
    now = ", ".join(cfg["notify"]["now"]) or "nothing"
    esc = ", ".join(cfg["notify"]["escalate"])
    return [("Voice", f"{cfg['tts']['backend']}, makes .{app.tts.ext}", "/config#tts"),
            ("Hears your recordings", cfg["stt"]["backend"], "/config#stt"),
            ("Rewriter", cfg["rewrite"]["backend"], "/config#rewrite"),
            ("Rewriter instructions", "yours" if app.prompts.custom("rewrite") is not None else "built in",
             "/prompts"),
            ("Notifications", now + (f", then {esc}" if esc else ""), "/config#notify"),
            ("Joined audio", f"{mix.format}, {mix.rate} Hz" + (f", {mix.kbps} kbps" if mix.format in
                                                                ("mp3", "aac") else ""), "/config#audio"),
            ("Config file", str(cfg.path), "/config#service")]
