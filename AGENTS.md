# AGENTS.md

For agents working on nanotea itself. (Agents that *use* nanotea from another project get their instructions
from the MCP server, or from `nanotea sop` pasted into that project's AGENTS.md: see [docs/agents.md](docs/agents.md).)

## What this is

A tool for managing coding agents from a phone, through an interface that feels like a messaging app. Agents
send the owner messages and questions over MCP; the service files them in a web app, voices them on request,
and notifies the phone. The owner answers by typing or recording, often recording, because speaking is how
they say what they mean most clearly. The agent gets the words, a transcript, and the recording itself.

The app borrows a chat app's shape because it is familiar and calm. Features serve the owner's control and
clarity: knowing what each agent is doing, deciding, correcting, and being understood, in as few tokens as the
work allows. Audio matters beyond speech: the owner does audio work, so clips, attached files and raw
recordings are kept at full quality.

## Commands

```bash
uv sync
uv run python -m unittest discover -s tests            # all tests; needs ffmpeg on the PATH
uv run python -m unittest tests.test_server -k raw      # one test by name
uv run --with pyflakes python -m pyflakes nanotea tests docs e2e  # lint; must be clean
uv run nanotea plugins --check                          # build the configured plugins and report
uv run python docs/figures/run.py                       # redraw the docs' figures (docs/figures/README.md)
docker build -t nanotea-video -f docs/assets/video.Dockerfile .
docker run --rm -v "$PWD:/src" nanotea-video            # film docs/assets/board.mp4 and the README's board.gif
python3 e2e/run.py linux                                # deployments end to end, headless (e2e/README.md)
```

The page scripts live in Python strings in `nanotea/pages.py`; nothing runs them in the tests.
`test_every_page_script_parses_as_served` (needs node) checks the pages' scripts as served, where a Python escape
like `\"` has already become a bare quote. While editing, this quicker check reads the source:

```bash
python3 - <<'EOF'
import re, subprocess, tempfile
src = open("nanotea/pages.py").read()
for i, m in enumerate(re.finditer(r"<script>(.*?)</script>", src, re.S)):
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
    f.write(m.group(1).replace("__", "x").replace("\\\\", "\\")); f.close()
    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    print(i, "ok" if r.returncode == 0 else r.stderr)
EOF
```

## Layout

```
agent --MCP--> mcp_server.py --HTTP (client.py)--> server.py --> worker.py: rewrite, title, notify; voice on request
                                                       |          (rewrite.py, tts.py, notify.py: plugins.py)
owner's phone <--pages.py (HTML, CSS, JS)--------------+--> store.py, inbox.py, channels.py, index.py
owner's recording --drafts--> server.py --> worker.Transcriber (stt.py) --> delivered to the agent
```

| file | does |
|---|---|
| `server.py` | the HTTP service (stdlib `ThreadingHTTPServer`, no framework): routes, the owner's and agents' APIs, `App` |
| `pages.py` | every page: Python strings of HTML, CSS and inline JS, including the recorder |
| `mcp_server.py` | one MCP server per agent session, over stdio; the tools and what each returns |
| `relay.py` | `nanotea mcp`: runs `mcp_server.py` as a child and starts it again on new code after an upgrade |
| `version.py` | the code's version, a hash of the package; sent on every response, so long-lived clients hand over |
| `tell.py` | `nanotea-tell`, the CLI for scripts and event sources |
| `store.py` | agents' messages: `data/messages/<id>/` with `meta.json`, `original.md`, `reply.json`, audio |
| `inbox.py`, `channels.py` | what the owner writes: to a line (`Folder`, `Inbox`) or a channel |
| `index.py` | SQLite index over the message files, rebuilt at start; never the record |
| `worker.py` | the queue: rewrite, title, notify; voicing when the owner taps play; transcription |
| `plugins.py` | the registry: finds, builds and checks every backend ([docs/plugins.md](docs/plugins.md)) |
| `controls.py` | what agents put in front of the owner to tap; `Act`, the helpers, and the built-in controls |
| `tts.py`, `stt.py`, `rewrite.py`, `notify.py` | the built-in plugins of each kind |
| `audio.py` | `[audio]` settings and `Mix.join`, which splices speech, clips and takes with ffmpeg |
| `tokens.py`, `credentials.py`, `token_cmd.py` | agents' tokens: the hashed book in `data/tokens.json`; the files agents read them from; `nanotea token` |
| `enroll.py` | requests for a first token: asked for without one, decided by the approver or the owner, collected by the asker |
| `files.py` | the owner's attachments: allowed types, drafts, kept byte for byte |
| `markdown.py` | markdown to HTML for pages, and to plain text for speech and notifications |
| `themes.py` | the built-in themes, the owner's from `[theme.custom]`, and the pick in `data/theme.json` |
| `leaf.py` | a name's leaf as SVG or PNG (stdlib only), and `nanotea leaf`; seeded and drawn exactly as the JS original |
| `configview.py` | the whole effective config for Settings > Configuration and Prompts: values, where each comes from, secrets as set or missing |
| `configedit.py` | the owner's changes to config.toml and env_file: edited in place, checked as at start, the machine's keys gated; `nanotea config` |
| `prompts.py` | the text sent to models: the built-in rewrite prompt and the owner's override in `data/prompts/` |
| `settings.py`, `rules.py` | the owner's switches, and standing rules agents must follow |
| `hush.py` | the owner's notification choices: muted agents and channels, only questions, quiet hours |
| `export.py` | messages as markdown, for `/export.md` |
| `bang.py` | the owner's `!command`: `[bang]` settings, `run`, and `Host`, the session-side loop that runs commands |
| `sop.py` | the working agreement agents are given, and its brief form |
| `quirks.py` | what differs between harnesses, as data: idle mode, wait, how much of the agreement each keeps |
| `setup.py`, `hook.py` | per-harness wiring: what `nanotea setup` prints, and the hooks |

Docs: [docs/agents.md](docs/agents.md) (harnesses, tools, what arrives), [docs/cli.md](docs/cli.md),
[docs/running.md](docs/running.md) (how browsers reach it, every way, and how each was checked),
[docs/service.md](docs/service.md) (running it, config, data), [docs/plugins.md](docs/plugins.md),
[docs/look.md](docs/look.md) (themes, leaves), [e2e/README.md](e2e/README.md) (deployments end to end), [docs/voice-calls.md](docs/voice-calls.md) (a design, not built).

## Rules

- **Nothing fails silently, and nothing falls back.** An error reaches whoever can act on it: the agent as a
  tool error, the owner in the app, the operator in the log. A bad config stops the service with a message
  naming the table and key. Never catch and continue, never substitute a default for something that failed.
- **The files under `data/` are the record.** The index is derived and rebuilt. Write files atomically
  (temp file, then `os.replace`), and build a message in a hidden directory before renaming it into place.
- **What the owner sends is kept as sent.** Recordings, takes and attachments are never re-encoded in place.
  A joined track is a copy; the originals stay beside it.
- **Harness-neutral.** Anything specific to one harness lives in `quirks.py` (how nanotea behaves for it), `setup.py`
  and `hook.py` (how it is wired). `nanotea setup` prints and never writes.
- **Agents get facts, not guesses.** If the browser didn't report something (a sample rate, a mic), it is
  `null`, not a plausible value.
- **Every new backend is a plugin.** See [docs/plugins.md](docs/plugins.md); nothing registers by editing a dict.
- **Every config key is an `Option`.** The unknown-key check, the Configuration page and `config.example.toml` come from
  the same declarations; `tests/test_configview.py` fails when the example or the docs miss one.
- A change to a harness adapter says how it was checked: against the live harness, or only from its docs.

## Style

- Python 3.12, standard library first. Dependencies are `mcp` and `pywebpush`; adding one needs a reason.
- Lines up to 120 characters. Match the code around you.
- Comments and docstrings are terse and say why, not what. Docstrings of plugin classes are shown to users by
  `nanotea plugins`: the first paragraph must stand alone.
- User-facing text, page copy and docs: plain, short sentences. No em dashes, no emoji in prose.
- Errors say what to do: `"[stt.command] needs argv"`, `"'raw' ... goes with 'ask'"`.
- Commit messages are terse: a subject line, and a short bullet list when there is more than one change.

## Tests

- `tests/test_server.py` starts one real service per test class on port 17447 (`NANOTEA_TEST_PORT`) with a temporary data directory,
  a fake voice engine (`tests/fake_tts.py`) and a fake transcriber that prints `transcript`. It drives the
  service through HTTP, `nanotea-tell`, hooks and MCP clients.
- `tests/harness.py` gives newer test classes their own service on their own port.
- That service is shared by every test in the class. Voices belong to one sender for good: give a new test
  sender a voice no other test uses. Tests that change settings put them back (`self.restore()`).
- The owner's API goes through the proxy headers and pairing cookie (`self.call`); the agents' API is local
  only (`self.local`). Through the proxy, agent routes are refused.
- An owner's recording is delivered to agents only after transcription finishes; poll with `self.wait_for`.
- Agents' messages are voiced only when played (`GET /audio/<id>`), not when sent.
- Tests that need ffmpeg say so with `skipUnless`. CI runs on Linux and macOS (`.github/workflows/test.yml`); the deployments
  end to end in `.github/workflows/e2e.yml`.
- When a test fails because the server dropped the connection, the traceback is in the service's stderr. The
  harness drains it into `cls.log` (a list of lines) and doesn't print it: read that rather than guessing. A pipe
  nobody drains fills and stops the service at its next log line, so start services with `harness.spawn`.
- Agents' calls need tokens: `Tokens.token(name)` issues one through `nanotea token add`, `self.local(...)` sends
  it (inferred from the request's name, or `as_=`), `self.owner(...)` sends the key, and `env_for(name)` is the
  environment for a subprocess that acts as that agent. `tests/test_auth.py` covers the rules.
