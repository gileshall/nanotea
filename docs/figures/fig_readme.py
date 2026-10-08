"""Figures for README.md."""
import pathlib

import numpy as np
from matplotlib.patches import Circle, FancyBboxPatch

from kit import BOLD, GREEN, INK, KINDS, MUTED, RULE, axes, canvas, in_order, ref, sequence
from nanotea import plugins, quirks, setup
from nanotea.client import line_for
from nanotea.mcp_server import tool_definitions
from nanotea.settings import CORE_TOOLS, DEFAULTS, FEATURES, costs, on, session_total

M, S, W, ST, H, P = ("nanotea/mcp_server.py", "nanotea/server.py", "nanotea/worker.py", "nanotea/store.py",
                     "nanotea/hook.py", "nanotea/pages.py")
HERE = pathlib.Path(__file__).resolve().parent


def pill(kind):
    if kind not in plugins.BUILTIN:
        raise SystemExit(f"figure: no plugin kind {kind!r}")
    return kind


def path(facts):
    ref(M, "mcp.tool()(send)")
    ref(M, "mcp.tool()(ask)")
    ref(M, "async def check() -> dict")
    ref(M, 'out["waiting"] = w["total"]')
    ref(M, "async def status(text: str)")
    ref(H, "def held(self, client: Client, name: str, what: str | None)")
    ref(M, "async def wait(ctx: Context")
    in_order(S, "msg = self.app.store.create(text", "self.app.worker.submit(msg.id)")
    ref(ST, "# Build in a hidden dir, then rename")
    in_order(W, "spoken = self.rewriter.rewrite(", "verdict = self.hush.verdict(msg)", "note = self.note_for(msg)",
             "def speak(self, msg_id: str)")
    ref(W, "transcript = self.stt.transcribe(audio)")
    ref(S, "done = plugin.act(msg.control")
    ref(H, "def rewake(")
    ref(H, "def stop(")
    columns = [("agent", "The agent's session", "any harness, over MCP"),
               ("service", "The service", "on your machine"),
               ("phone", "Your phone", "the Home Screen app, or a browser")]
    steps = [
        dict(col=0, row=0, title="send or ask", where="mcp_server.py",
             sub="A tool call. ask returns at once; the answer comes later."),
        dict(col=0, row=1, span=3, title="Back to work", after=0, kind="plain", where="mcp_server.py",
             sub="Nothing blocks on you. Every nanotea tool result carries waiting, the count of your messages "
                 "ready for it, so it knows to check between steps.\n\nIts one-line status shows under its name. "
                 "If its harness stops it at a permission prompt, the held hook shows you that too, and what it "
                 "asked to do."),
        dict(col=1, row=0, title="On disk, first", after=0, where="store.py", say="nanotea mcp",
             sub="A directory of files, built hidden and renamed into place."),
        dict(col=1, row=1, title="Rewritten and titled", after=2, where="worker.py", pill=pill("rewrite"),
             sub="Titled, and made for listening or kept word for word, as you set it."),
        dict(col=1, row=2, title="Notify", where="worker.py", pill=pill("notify"),
             sub="Unless the agent is muted, or it is quiet hours."),
        dict(col=2, row=2, title="In the agent's line", say="push",
             sub="Read it, reply in its thread, or tap play to hear it."),
        dict(col=1, row=3, title="Voiced", after=5, say="tap play", where="worker.py", pill=pill("tts"),
             sub="Only when you play it, so reading costs no speech."),
        dict(col=2, row=3, title="You answer", after=5, where="server.py", pill=pill("control"),
             sub="Type, record, or tap what the agent put there."),
        dict(col=1, row=4, title="Transcribed", say="a recording", where="worker.py", pill=pill("stt"),
             sub="The recording is kept as made, beside its words."),
        dict(col=0, row=5, title="check, wait, or woken", after=[1, 8], say=[None, "its next tool call, or a hook"],
             where="mcp_server.py, hook.py",
             sub="Your words, what they answer, and any recording."),
    ]
    return sequence(columns, steps, "One message, out and back: where each step happens, and which a plugin decides")


NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "gemini": "Gemini CLI",
         "opencode": "OpenCode", "goose": "Goose", "vscode": "VS Code", "zed": "Zed", "cline": "Cline", "amp": "Amp"}


def harnesses(facts):
    if set(setup.HARNESSES) != set(NAMES) | {"other"}:
        raise SystemExit(f"figure: harnesses changed: {sorted(set(setup.HARNESSES) ^ (set(NAMES) | {'other'}))}")
    agent = setup.Agent("nanotea", "config.toml", "builder", line_for("builder"), ["mcp"],
                        "tokens/builder.token")
    cols = [("hook stop", "Sent back to check\nwhen it stops early"),
            ("hook rewake", "Woken while idle\nwhen you write"),
            ("hook held", "Shows you it is held\nat a permission prompt")]
    rows = []
    for key, name in NAMES.items():
        text = setup.HARNESSES[key](agent)
        rows.append((name, quirks.QUIRKS[key].wait_s, [f" {mode} " in text for mode, _ in cols]))
    width, top, pitch = 9.0, 1.45, 0.36
    height = top + len(rows) * pitch + 0.75
    fig, ax = canvas(width, height)
    ax.text(0.15, 0.3, "Every harness gets the MCP tools; hooks add the rest", fontsize=11.5, fontweight=BOLD)
    x_wait, x_cols = 3.05, [4.55, 6.15, 7.75]
    ax.text(x_wait, 1.0, "wait holds\nfor up to", ha="center", va="center", fontsize=8.5, color=MUTED, linespacing=1.2)
    for x, (_, head) in zip(x_cols, cols):
        ax.text(x, 1.0, head, ha="center", va="center", fontsize=8.5, color=MUTED, linespacing=1.2)
    ax.plot([0.15, width - 0.15], [top - 0.12, top - 0.12], color=RULE, lw=1)
    for r, (name, wait_s, has) in enumerate(rows):
        y = top + 0.1 + r * pitch
        if r % 2:
            ax.add_patch(FancyBboxPatch((0.15, y - pitch / 2), width - 0.3, pitch, boxstyle="round,pad=0,rounding_size=0.05",
                                        fc=KINDS["plain"][0], ec="none", alpha=0.6))
        ax.text(0.3, y, name, va="center", fontsize=10)
        ax.text(x_wait, y, f"{wait_s} s", ha="center", va="center", fontsize=9.5, color=INK)
        for x, yes in zip(x_cols, has):
            if yes:
                ax.add_patch(Circle((x, y), 0.085, fc=GREEN, ec=GREEN))
            else:
                ax.add_patch(Circle((x, y), 0.06, fc="none", ec=RULE, lw=1.4))
    ref("docs/agents.md", "| Live, Claude Code")
    ax.text(0.15, height - 0.3, "Without a hook, the working agreement has the agent check before it finishes. "
            "From nanotea setup; checked live with Claude Code,\nthe others from their docs.", fontsize=8.5,
            color=MUTED, va="center", linespacing=1.3)
    return fig


def _feature_names():
    names = {k: label for k, label, _ in FEATURES}
    for t in DEFAULTS["tools"]:
        names[f"tools.{t}"] = "Controls" if t == "controls" else f"The {t} tool"
    return names


def tokens(facts):
    cfg = {"port": 1, "app": {"owner": "you", "name": "nanotea"}}
    c = costs("you", tool_definitions(cfg), [], tool_definitions(cfg, controls=False))
    total = session_total(c, DEFAULTS)
    names = _feature_names()
    paid = sorted(((n, f) for f, n in c["by"].items() if n), reverse=True)
    free = [names[f] for f, n in c["by"].items() if not n]
    bars = [("Always: the agreement, " + ", ".join(CORE_TOOLS), c["base"], GREEN, True)]
    bars += [(names[f], n, KINDS["service"][1], on(DEFAULTS, f)) for n, f in paid]
    fig, ax = axes((9, 0.6 + 0.3 * len(bars)))
    ys = np.arange(len(bars))
    for y, (name, n, colour, default) in zip(ys, bars):
        ax.barh(y, n, height=0.62, color=colour if default else "none", ec=colour, lw=1.2, hatch=None if default else "////")
        ax.text(n + 18, y, f"{n:,}" + ("" if default else "  off unless you turn it on"), va="center", fontsize=8.5,
                color=INK if default else MUTED)
    ax.set_yticks(ys, [b[0] for b in bars], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, c["base"] * 1.25)
    ax.set_xlabel("tokens in every session's context, estimated at four characters each", fontsize=9)
    fig.text(0.01, 0.97, f"What nanotea costs an agent: {total:,} tokens a session as it comes, {c['base']:,} with "
             "every switch off", fontsize=11.5, fontweight=BOLD, va="top")
    fig.text(0.01, 0.01, "Free, because they change what happens rather than what the agent reads: "
             + ", ".join(free).lower() + ".", fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.04, 1, 0.92))
    return fig


FIGURES = {"readme-path": path, "readme-harnesses": harnesses, "readme-tokens": tokens}
