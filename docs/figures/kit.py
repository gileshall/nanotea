"""House style and drawing primitives for the docs' figures (docs/figures/README.md)."""
import pathlib
import textwrap

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as font_manager  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
FONT = "Avenir Next"
INK, MUTED, PAPER, RULE = "#1d1c1d", "#686769", "#f6f7f3", "#d9ddd6"
# One color per place a thing happens, from the built-in themes: the agent earl grey, the service sencha, the
# owner's phone oolong, a plugin rooibos.
KINDS = {
    "agent": ("#e3e9f7", "#4a63a8"),
    "service": ("#e1f1ec", "#2f7d6d"),
    "phone": ("#f6e8d8", "#9a5b1e"),
    "plugin": ("#f8e2dd", "#b0412e"),
    "plain": ("#eceee9", "#686769"),
}
BLUE, GREEN, AMBER, RED = (KINDS[k][1] for k in ("agent", "service", "phone", "plugin"))
BOLD = 600

plt.rcParams.update({
    "font.size": 10.5,
    "text.color": INK,
    "axes.labelcolor": MUTED,
    "xtick.color": MUTED,
    "ytick.color": INK,
})


def use_font() -> None:
    """The committed pictures are drawn in FONT; a missing one stops the build rather than drawing in another."""
    font_manager.findfont(FONT, fallback_to_default=False)
    plt.rcParams["font.family"] = FONT


_cache = {}


def source(rel):
    if rel not in _cache:
        _cache[rel] = (ROOT / rel).read_text()
    return _cache[rel]


def anchor(rel, text):
    """The 1-based line of `text`, which must occur once in a source file; else the build fails."""
    hits = [n for n, line in enumerate(source(rel).splitlines(), 1) if text in line]
    if len(hits) != 1:
        where = "not found" if not hits else "ambiguous (lines " + ", ".join(map(str, hits)) + ")"
        raise SystemExit(f"figure anchor {where}: {rel}: {text!r}")
    return hits[0]


def ref(rel, text, label=None):
    """Checks that `text` is in `rel` and returns the label a figure prints under a box."""
    anchor(rel, text)
    return label if label is not None else text


def in_order(rel, *texts):
    """Fails the build unless the anchors occur in `rel` in the order given: a figure's sequence is the code's."""
    lines = [anchor(rel, x) for x in texts]
    if lines != sorted(lines) or len(set(lines)) != len(lines):
        raise SystemExit(f"figure order wrong in {rel}: " + ", ".join(f"{x!r}@{n}" for x, n in zip(texts, lines)))


def canvas(width, height):
    fig = plt.figure(figsize=(width, height))
    fig.patch.set_facecolor(PAPER)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.axis("off")
    return fig, ax


def axes(figsize=(9, 4.6), **kw):
    fig, ax = plt.subplots(figsize=figsize, **kw)
    fig.patch.set_facecolor(PAPER)
    for a in (ax if hasattr(ax, "__len__") else [ax]):
        a.set_facecolor(PAPER)
        for side in ("top", "right", "left"):
            a.spines[side].set_visible(False)
        a.spines["bottom"].set_color(RULE)
        a.tick_params(axis="y", length=0)
    return fig, ax


def box(ax, x, y, w, h, kind, title, sub="", size=10, pill=None):
    """A rounded box: a bold title and a muted line or two under it. pill names the plugin kind that decides it."""
    fill, edge = KINDS[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.07", fc=fill, ec=edge, lw=1.3))
    ax.text(x + 0.14, y + 0.2, title, va="center", fontsize=size, fontweight=BOLD)
    if sub:
        sub = "\n".join(textwrap.fill(p, int((w - 0.3) * 17)) if p else "" for p in sub.split("\n"))
        ax.text(x + 0.14, y + 0.37, sub, va="top", fontsize=size - 2, color=MUTED,
                linespacing=1.3)
    if pill:
        tag(ax, x + w - 0.1, y, pill)


def tag(ax, right, y, text, size=7.6):
    """A plugin's pill, its right edge at `right`, straddling the top edge of a box at `y`."""
    fill, edge = KINDS["plugin"]
    w = 0.16 + 0.062 * len(text)
    ax.add_patch(FancyBboxPatch((right - w, y - 0.09), w, 0.18, boxstyle="round,pad=0,rounding_size=0.09",
                                fc=edge, ec=edge, lw=0))
    ax.text(right - w / 2, y, text, ha="center", va="center", fontsize=size, color=fill, fontweight=BOLD)


def arrow(ax, a, b, colour=MUTED, rad=0.0, style="-|>", ls="-"):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle=style, mutation_scale=11, lw=1.3, color=colour, shrinkA=0,
                                 shrinkB=0, connectionstyle=f"arc3,rad={rad}", linestyle=ls))


def label(ax, x, y, text, ha="center", bg=PAPER):
    ax.text(x, y, text, ha=ha, va="center", fontsize=8, color=MUTED, style="italic",
            bbox=dict(fc=bg, ec="none", pad=1.5))


def sequence(columns, steps, title, width=9.0, legend=True):
    """Where each step happens, top to bottom: one lane per column, a box per step, an arrow from each step's
    `after` (the one before, by default) to it.

    columns: [(kind, name, sub)]. A step: {"col", "row", "span", "title", "sub", "where", "pill", "kind", "after",
    "say"}; `where` is a source location printed in the box, `say` labels the arrow into it, `after=None` draws
    none, a list of `after` takes a list of `say`, and `span` rows make a tall box."""
    pad, gap, head, pitch, box_h = 0.15, 0.14, 0.95, 1.16, 0.86
    rows = max(s["row"] + s.get("span", 1) for s in steps)
    foot = 0.45 if legend else 0.1
    height = head + rows * pitch + foot + pad
    fig, ax = canvas(width, height)
    ax.text(pad, 0.3, title, fontsize=11.5, fontweight=BOLD)
    cw = (width - 2 * pad - (len(columns) - 1) * gap) / len(columns)
    xs = [pad + c * (cw + gap) for c in range(len(columns))]
    for x, (kind, name, sub) in zip(xs, columns):
        fill, edge = KINDS[kind]
        ax.add_patch(Rectangle((x, 0.55), cw, height - 0.55 - foot, fc=fill, ec="none", alpha=0.42))
        ax.text(x + 0.14, 0.72, name, fontsize=9.5, fontweight=BOLD, color=edge, va="center")
        ax.text(x + 0.14, 0.9, sub, fontsize=8, color=edge, va="center")
    bw = cw - 0.36
    at = []
    for s in steps:
        x, y = xs[s["col"]] + 0.18, head + 0.12 + s["row"] * pitch
        h = box_h + (s.get("span", 1) - 1) * pitch
        box(ax, x, y, bw, h, s.get("kind", columns[s["col"]][0]), s["title"], s.get("sub", ""), pill=s.get("pill"))
        if s.get("where"):
            ax.text(x + bw - 0.12, y + h - 0.11, s["where"], ha="right", va="center", fontsize=7, color=MUTED,
                    family="monospace")
        at.append((x, y, h))
    links = []
    for i, s in enumerate(steps):
        after, say = s.get("after", i - 1), s.get("say")
        if isinstance(after, list):
            links += [(j, i, w) for j, w in zip(after, say or [None] * len(after))]
        elif after is not None and after >= 0:
            links.append((after, i, say))
    for j, i, say in links:
        s = {"col": steps[i]["col"], "say": say}
        (x1, y1, h1), (x2, y2, _) = at[j], at[i]
        if y1 == y2:
            a, b = (x1 + bw, x2) if x2 > x1 else (x1, x2 + bw)
            arrow(ax, (a + 0.02, y1 + box_h / 2), (b - 0.02 if x2 > x1 else b + 0.02, y2 + box_h / 2))
            if s.get("say"):
                label(ax, (a + b) / 2, y1 + box_h / 2 - 0.2, s["say"])
        elif x1 == x2:
            cx = x1 + bw / 2
            arrow(ax, (cx, y1 + h1 + 0.02), (cx, y2 - 0.02))
            if s.get("say"):
                label(ax, cx + 0.08, (y1 + h1 + y2) / 2, s["say"], ha="left", bg=KINDS[columns[s["col"]][0]][0])
        else:
            # Down, across, down: the elbow sits in the gap above the target's row.
            c1, c2 = x1 + bw * (0.7 if x2 > x1 else 0.3), x2 + bw / 2
            ym = y2 - (pitch - box_h) / 2 + 0.02
            ax.plot([c1, c1, c2], [y1 + h1 + 0.02, ym, ym], color=MUTED, lw=1.3, solid_capstyle="round")
            arrow(ax, (c2, ym), (c2, y2 - 0.02))
            if s.get("say"):
                # Between the turn and the target's near edge, clear of its plugin pill.
                edge = x2 if x2 > x1 else x2 + bw
                label(ax, (c1 + edge) / 2, ym, s["say"])
    if legend:
        y = height - foot / 2 - 0.05
        tag(ax, pad + 0.62, y, "plugin")
        ax.text(pad + 0.72, y, "a plugin does this step: a built-in, an installed package, or a file of your own",
                fontsize=8.5, color=MUTED, va="center")
    return fig


def save(fig, name, out, dpi=200):
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.png"
    fig.savefig(path, facecolor=PAPER, dpi=dpi, metadata={"Software": None})
    plt.close(fig)
    return path
