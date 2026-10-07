"""Draws every figure into docs/assets/<name>.png.

    uv run python docs/figures/run.py            # draw them all
    uv run python docs/figures/run.py --only readme-path
    uv run python docs/figures/run.py --check    # anchors and embeds only; writes nothing"""
import argparse
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent.parent)]

import kit  # noqa: E402
import fig_plugins  # noqa: E402
import fig_readme  # noqa: E402

MODULES = [fig_readme, fig_plugins]
OUT = kit.ROOT / "docs" / "assets"
PAGES = [kit.ROOT / "README.md", *sorted((kit.ROOT / "docs").glob("*.md"))]


def embedded() -> set[str]:
    """Every docs/assets picture a page shows, by name."""
    return {m for page in PAGES for m in re.findall(r"assets/([a-z0-9-]+)\.png", page.read_text())}


def main():
    p = argparse.ArgumentParser(description="Draw the docs' figures from the code.")
    p.add_argument("--check", action="store_true", help="draw each in memory: anchors hold and every one is shown")
    p.add_argument("--only", help="one figure by name")
    a = p.parse_args()
    figures = {name: draw for m in MODULES for name, draw in m.FIGURES.items()}
    if a.only and a.only not in figures:
        sys.exit(f"no figure {a.only!r}; figures: {', '.join(figures)}")
    if not a.check:
        kit.use_font()
    for name, draw in figures.items():
        if a.only and name != a.only:
            continue
        fig = draw({})
        if a.check:
            kit.plt.close(fig)
        else:
            print(kit.save(fig, name, OUT).relative_to(kit.ROOT))
    unshown = set(figures) - embedded()
    if unshown:
        sys.exit(f"figures no page shows: {', '.join(sorted(unshown))}")


if __name__ == "__main__":
    main()
