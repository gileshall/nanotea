"""Controls: what an agent puts in front of the owner to tap, on a message or pinned to its line.

A control is a plugin of kind "control" (docs/plugins.md). The agent sends a spec, {"type": <name>, ...}; the
plugin checks it, renders it as HTML, and turns each tap into an Act: the control's new state, and the words the
agent is told. On a question still waiting, those words are the answer.

The page posts a tap for any element inside the control with data-act (and data-value), or a form with data-act
(its fields as the value). The service renders the control again with the new state and swaps it in.
"""

import html
from dataclasses import dataclass

TYPE = r"[a-z][a-z0-9-]{0,31}"
LABEL_MAX = 80
OPTIONS_MAX = 12
STATE_MAX = 64 * 1024  # bytes of JSON a control may keep


@dataclass(frozen=True)
class Act:
    """state: what the control keeps after this tap (JSON). says: what the agent is told, in words, or None to tell
    it nothing yet (a tick on a checklist before Done). data: facts beside the words, as JSON."""
    state: dict
    says: str | None
    data: dict | None = None


def e(s) -> str:
    return html.escape(str(s), quote=True)


def button(label: str, act: str, value=None, on: bool = False, live: bool = True, strong: bool = False) -> str:
    """A tappable button for a control. on: shown as chosen. live: False once the control takes no more taps."""
    cls = "btn small" + (" primary" if strong else "") + (" on" if on else "")
    val = f' data-value="{e(value)}"' if value is not None else ""
    return (f'<button type="button" class="{cls}" data-act="{e(act)}"{val} aria-pressed="{str(on).lower()}"'
            f'{"" if live else " disabled"}>{e(label)}</button>')


def labels(spec: dict, key: str, most: int = OPTIONS_MAX) -> list[str]:
    """spec[key]: a list of 1 to most short, distinct, non-empty strings, or a ValueError the agent can act on."""
    got = spec.get(key)
    if not (isinstance(got, list) and 1 <= len(got) <= most):
        raise ValueError(f"'{key}' must be a list of 1 to {most} labels")
    out = []
    for x in got:
        if not (isinstance(x, str) and x.strip() and len(x) <= LABEL_MAX):
            raise ValueError(f"each of '{key}' must be a non-empty string of at most {LABEL_MAX} characters")
        out.append(x.strip())
    if len(set(out)) != len(out):
        raise ValueError(f"'{key}' has the same label twice")
    return out


def only(spec: dict, *keys: str) -> None:
    extra = set(spec) - {"type", *keys}
    if extra:
        raise ValueError(f"unknown key {sorted(extra)[0]!r}; this control takes {', '.join(keys) or 'nothing'}")


def index(value, n: int) -> int:
    try:
        i = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"not an option: {value!r}") from None
    if not 0 <= i < n:
        raise ValueError(f"not an option: {value!r}")
    return i


class Choice:
    """Buttons, one of which the owner taps. On a question, the tap is the answer.

    Spec: {"type": "choice", "options": ["Ship it", "Hold"]}, 1 to 12 labels."""
    api = 1
    name = "choice"
    about = 'Buttons; the owner taps one. {"type": "choice", "options": ["Ship it", "Hold"]}'

    def __init__(self, table: dict, ctx):
        pass

    def check(self, spec: dict) -> dict:
        only(spec, "options")
        return {"type": self.name, "options": labels(spec, "options")}

    def render(self, spec: dict, state: dict, live: bool) -> str:
        picked = state.get("picked")
        return '<div class="row">' + "".join(button(o, "pick", i, on=o == picked, live=live)
                                             for i, o in enumerate(spec["options"])) + "</div>"

    def act(self, spec: dict, state: dict, act: str, value) -> Act:
        if act != "pick":
            raise ValueError(f"choice has no act {act!r}")
        picked = spec["options"][index(value, len(spec["options"]))]
        return Act({"picked": picked}, picked, {"picked": picked})


class Checklist:
    """Items the owner ticks, then Done. The agent hears which were ticked and which weren't.

    Spec: {"type": "checklist", "items": ["Tests", "Docs"], "done": "Done"}; done, the button's label, is optional."""
    api = 1
    name = "checklist"
    about = ('Items the owner ticks, then taps Done; you get ticked and unticked. '
             '{"type": "checklist", "items": ["Tests", "Docs"], "done"?: "Looks good"}')

    def __init__(self, table: dict, ctx):
        pass

    def check(self, spec: dict) -> dict:
        only(spec, "items", "done")
        done = spec.get("done", "Done")
        if not (isinstance(done, str) and done.strip() and len(done) <= LABEL_MAX):
            raise ValueError(f"'done' must be a button label of at most {LABEL_MAX} characters")
        return {"type": self.name, "items": labels(spec, "items", 30), "done": done.strip()}

    def render(self, spec: dict, state: dict, live: bool) -> str:
        ticked = set(state.get("ticked", []))
        rows = "".join(
            f'<label class="tick"><input type="checkbox" data-act="tick" data-value="{i}"'
            f'{" checked" if i in ticked else ""}{"" if live else " disabled"}> {e(item)}</label>'
            for i, item in enumerate(spec["items"]))
        sent = '<span class="meta">Sent</span>' if state.get("sent") else ""
        done = button(spec["done"], "done", live=live, strong=True)
        return f'<div class="ticks">{rows}</div><div class="row">{done}{sent}</div>'

    def act(self, spec: dict, state: dict, act: str, value) -> Act:
        items = spec["items"]
        ticked = set(state.get("ticked", []))
        if act == "tick":
            ticked ^= {index(value, len(items))}
            return Act({**state, "ticked": sorted(ticked)}, None)
        if act == "done":
            yes = [items[i] for i in sorted(ticked)]
            no = [x for i, x in enumerate(items) if i not in ticked]
            says = f"{spec['done']}. Ticked: {', '.join(yes) or 'none'}. Not ticked: {', '.join(no) or 'none'}."
            return Act({"ticked": sorted(ticked), "sent": True}, says, {"ticked": yes, "unticked": no})
        raise ValueError(f"checklist has no act {act!r}")
