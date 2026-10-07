# Figures

The charts and diagrams in the docs are drawn from the code by matplotlib, into `docs/assets/<name>.png`:

```bash
uv run python docs/figures/run.py                       # draw them all
uv run python docs/figures/run.py --only readme-path    # one
uv run python docs/figures/run.py --check               # draw in memory; write nothing
```

`--check` runs in the tests (`tests/test_figures.py`). It fails when a figure no longer matches the code, or when
no page shows it.

## Drawn from the code

A figure reads its facts from nanotea rather than restating them:

- The token chart calls `settings.costs` on the real tool definitions.
- The harness chart reads `setup.HARNESSES` and what `nanotea setup` prints for each.
- The plugin pills check their kind against `plugins.BUILTIN`, and the control figure checks `plugins.REQUIRED`.

A box that says where a step happens is anchored to the source. `kit.ref(file, text)` fails the build unless
`text` occurs on exactly one line of `file`. `kit.in_order(file, a, b, ...)` fails unless the anchors occur in
that order, so a sequence drawn top to bottom is the order the code runs it. When a refactor moves or renames
an anchor, the figure fails until someone looks at it again.

## Adding one

1. Write a function in `fig_readme.py` or `fig_plugins.py`, or a new `fig_<page>.py` added to `MODULES` in
   `run.py`. It takes `facts` and returns a matplotlib figure.
2. Anchor every claim it makes about the code with `ref` or `in_order`.
3. Add it to the module's `FIGURES`, and show it on a page: `run.py` fails while no page does.
4. Draw it, and commit the PNG with the code.

## Style

`kit.py` holds the house style. One color per place a thing happens, taken from the built-in themes: the agent
earl grey, the service sencha, your phone oolong, and a plugin rooibos. The ground is parchment, the text is
ink, and secondary text is muted. Titles state the figure's claim. A figure says something the prose can't: a
sequence, a comparison, a count over time.

The committed pictures are drawn in Avenir Next at 200 dpi. A machine without that font stops with an error
rather than drawing in another. `--check` needs no font, so the tests run anywhere. PNG, not SVG, because not
every place the docs are read serves SVG as an image.
