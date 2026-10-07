"""Figures for docs/plugins.md, and the README's plugins section."""
from kit import in_order, ref, sequence
from nanotea import plugins

S, P, C, M = "nanotea/server.py", "nanotea/pages.py", "nanotea/controls.py", "nanotea/mcp_server.py"


def control(facts):
    need = set(plugins.REQUIRED["control"])
    if need != {"name", "about", "check", "render", "act"}:
        raise SystemExit(f"figure: a control now needs {sorted(need)}")
    ref(M, "async def controls() -> dict")
    ref(S, "checked = plugin.check(spec)")
    ref(P, "drawn = plugin.render(msg.control, store.control_state(msg.id), live)")
    in_order(S, "done = plugin.act(msg.control, store.control_state(msg_id), act, value)",
             "sent = self._tapped(msg, done.says.strip(), tap)", "store.set_control_state(msg_id, done.state)")
    ref(C, "class Act:")
    columns = [("agent", "The agent", "picks a control and describes it"),
               ("service", "The control's plugin", "on your machine"),
               ("phone", "Your phone", "where it is drawn and tapped")]
    steps = [
        dict(col=0, row=0, title="controls, then send", where="mcp_server.py",
             sub='Lists what is installed; sends {"type": "choice", ...}'),
        dict(col=1, row=0, title="check(spec)", where="server.py", pill="control",
             sub="Cleans the spec, or the agent gets an error saying what is wrong."),
        dict(col=1, row=1, title="render(spec, state, live)", where="pages.py", pill="control",
             sub="HTML under the message, drawn again after every tap."),
        dict(col=2, row=1, title="You tap", say="drawn",
             sub="A button, a box, a form: anything with data-act."),
        dict(col=1, row=2, title="act(spec, state, act, value)", say="the tap", where="server.py", pill="control",
             sub="Returns Act(state, says, data); the state is kept."),
        dict(col=0, row=3, title="The answer", say="says, data", where="controls.py",
             sub="The words, and the facts beside them as tap.data."),
    ]
    return sequence(columns, steps, "A control: the agent describes it, a plugin draws it, your tap comes back as "
                                    "words and data")


FIGURES = {"plugins-control": control}
