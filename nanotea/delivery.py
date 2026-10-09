"""How the owner's messages reach each agent, from the [delivery] table.

pull: the agent takes them with check and wait; the stop and wake hooks send it back when it stops or sleeps.
push: a hook after each of the agent's tool calls takes them and adds them to its context (nanotea hook push), so
they reach it mid-task without it calling anything. The harness must run that hook (quirks.py).
listen: the agent keeps nanotea listen running in the background. It exits with them, and the harness tells the
agent its background task ended. The harness must say so (quirks.py).
In every mode check and wait still work, and each item is delivered once, by whichever takes it first."""

from nanotea.config import ConfigError, Option

MODES = ("pull", "push", "listen")
OPTIONS = (
    Option("mode", str, "How agents get your messages: pull, the agent calls check and wait; push, a hook after "
                        "each of its tool calls hands them over mid-task; listen, a background process the agent "
                        "keeps running exits with them. Push and listen need a harness that can (nanotea setup "
                        "installs the hook).", "pull", choices=MODES),
    Option("agents", dict, "A mode for particular agents, by name, in place of mode: { desk = \"push\" }.", {}),
)


def settings(cfg: dict) -> dict:
    """The [delivery] table, checked: a bad key or mode stops the service with a message."""
    table = cfg.get("delivery", {})
    if not isinstance(table, dict):
        raise ConfigError("[delivery] must be a table")
    known = [o.key for o in OPTIONS]
    for key in table:
        if key not in known:
            raise ConfigError(f"[delivery] {key}: unknown; known: {', '.join(known)}")
    mode = table.get("mode", "pull")
    if mode not in MODES:
        raise ConfigError(f"[delivery] mode must be one of {', '.join(MODES)}, not {mode!r}")
    agents = table.get("agents", {})
    if not isinstance(agents, dict):
        raise ConfigError("[delivery] agents must be a table of agent name = mode")
    for name, m in agents.items():
        if m not in MODES:
            raise ConfigError(f"[delivery] agents: {name!r} has mode {m!r}; modes are {', '.join(MODES)}")
    return {"mode": mode, "agents": dict(agents)}


def mode_for(table: dict, name: str | None) -> str:
    """name's mode; an agent not named in agents, or not yet joined, has the default."""
    return table["agents"].get(name, table["mode"]) if name else table["mode"]
