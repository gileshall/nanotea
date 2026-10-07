# Look

<img src="assets/agents.png" alt="Six leaves, for builder, tester, docs, ios, release and search">

Every agent wears a leaf grown from its name: builder, tester, docs, ios, release and search, above. The name picks
a variety (camellia, elm, oak, maple, ginkgo or fern) and every trait of its outline: teeth, lobes, tip, base,
spread. Veins then grow into the outline by space colonization, so no two names share a leaf, and one name always
gets the same one. The app's own mark is the leaf grown from its name, `[app] name`; nanotea's is the leaf
"nanotea". It is the app's icon, its favicon and the mark beside its name in the sidebar.

## Themes

The theme sets every color in the app, the leaves included. Pick one, and how it follows light and dark, under
Settings > Look. The built-in themes are teas:

| Theme | Accent |
|---|---|
| `sencha` | green, the default |
| `oolong` | toasted amber |
| `earl grey` | cornflower blue on grey |
| `rooibos` | red-brown |

Each has a light and a dark side. Appearance `Match the device` follows the phone's or computer's setting; `Light`
or `Dark` holds one. The browser's bar and the home screen app take the theme's background.

Your own theme goes in the config: a built-in to start from, and the colors to change on each side. A new install
can start with it, and it shows under Settings > Look beside the built-ins.

```toml
[theme]
use = "lapsang"
appearance = "auto"

[theme.custom.lapsang]
base = "oolong"
light = { accent = "#7a3b1c", side = "#2a1810", leaf = "#6d4a22" }
dark = { accent = "#d08a5c" }
```

The colors, each `"#rrggbb"`:

| Key | Colors |
|---|---|
| `bg`, `fg` | the page and its text |
| `muted`, `faint` | secondary text, and times and hints |
| `line`, `hover`, `card`, `chip` | rules, hovered rows, cards, and buttons and tags |
| `accent`, `accent_fg`, `accent_soft` | links and primary buttons, text on them, and soft highlights |
| `side`, `side_fg`, `side_mute`, `side_hover`, `side_on` | the sidebar, its text, its quieter text, a hovered row, the open conversation |
| `red`, `amber`, `green` | unread counts and errors, warnings, listening |
| `leaf`, `vein` | the leaves: their color, and the tile the veins show through |

The app icon is the app's leaf in the theme's dark-side `leaf` color on its light-side `side` color. A config
error, such as a color that isn't one or a key no theme has, stops the service with the table and key.

## Leaves outside the app

`nanotea leaf` draws a name's leaf as the app does, for a README, a slide or a sticker:

```bash
nanotea leaf builder > builder.svg                                    # in currentColor
nanotea leaf builder tester api --color "#367f57" > team.svg          # a row, in one color
nanotea leaf nanotea --png 512 --color "#6cbf8a" --bg "#163c35" --out icon.png
nanotea leaf builder tester --png 128 --color "#367f57" --out team.png  # on nothing, a quarter apart
nanotea leaf builder --unfurl 0.4 > bud.svg                           # 0 a curled bud, 1 the open leaf
nanotea leaf builder tester --traits                                  # the variety and traits of each
```

The page serves the same at `/leaf/<name>.svg` (`?size=` one of 16, 24, 32, 48, 64, 128, 256 and 512, which sets how fine
the veins go, and `?unfurl=`), with no pairing needed: a name is all a leaf is made from.
