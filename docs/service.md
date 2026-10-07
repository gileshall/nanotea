# The service

The local service: what it stores, who can reach it, how it notifies, and its config.

## Running it

```bash
nanotea serve          # logs to stderr
nanotea pair-link      # open the link once on each phone or browser
```

Needs `ffmpeg` on the PATH. The default `tts.command` uses macOS `say`; any engine with a command line works.
`launchd/com.example.nanotea.plist` is a login-service template for macOS (replace `/Users/you`).

Data, under `data_dir`: `<id>/` holds an agent message's `meta.json`, `original.md`, `script.txt`, the audio,
`attachments/`, and `reply.json` plus any reply audio. `inbox/` is the main inbox and `inbox-<name>/` each direct
line. `voices.json` maps senders to voices; `reply_key` is the pairing key; `host_key` is the host key, read only by commands on this machine (`nanotea bang`); `tokens.json` holds the hashes of agents' tokens. `settings.json` holds the owner's
settings (only once one is changed; until then the defaults). `groups.json` is the board's groups and
`board-hidden.json` the names kept off it. `rules.json` is the standing rules and
`rules-log.jsonl` every rule added or removed. `talk/<id>.json` is one agent's message to another, with when it was
delivered. `nanotea.db` is an index, rebuilt from the files at each start; the files are the record. Agent sessions
and who is held at a permission prompt are kept in memory: MCP servers register their sessions again after a
restart.

## Web access

Browsers use `public_url`. Recording, notifications and the Home Screen app need it to be https, or this machine's own `http://127.0.0.1`: [running.md](running.md) goes through the ways to get there, from this computer alone to Tailscale, Cloudflare, Caddy and Traefik (`deploy/traefik/` is that example). Where a request comes from decides nothing about what it may do: every request carries a credential, from this machine or not. A page asked for with no credential is answered "not paired" at `public_url`, or through a trusted proxy, and is sent to `public_url` from any other address.

## Authentication

Three credentials, each a bearer secret.

| Credential | Who holds it | Opens |
|---|---|---|
| The pairing key (`data/reply_key`) | the owner | everything the owner does: pages, the owner's reads and writes, arranging the board, issuing and revoking tokens |
| An agent token | one agent, by name | that agent's calls to the agents' API: send, ask, hold a line, check, wait, status, history, read a thread, sessions, held, write to another agent; for a board manager, arranging the board |
| An events token | one program, by source name and one line | `POST /api/inbox/events` (and its `inbox-<name>` equivalents) for that line, and nothing else |

The owner's browser sends the key as an HttpOnly cookie (Secure when `public_url` is https), set by opening the link `nanotea pair-link` prints; notification links carry it too. Programs send it as `Authorization: Bearer <key>`. Whoever holds a link, or reads `data/reply_key`, can act as the owner.

A token is `nt_<8 hex>_<43 characters>`: an id you can quote and 256 random bits. The service keeps only the SHA-256 of it, in `data/tokens.json` (mode 0600), with `tokens-log.jsonl` recording each issue and revocation. A copy of `data/` therefore holds no usable token. Tokens are compared in constant time. The secret is shown once, when issued.

A token acts only as its own name. The name in the path, in `?name=`, and in a JSON `from`, `name` or `source` must be the token's, or the service answers 403 naming both. An agent reads a message only if it sent it, and ends only its own sessions. The owner's key can't act as an agent, and an agent's token can't act as the owner. Through the proxy the agents' API needs the same token as on this machine; the proxy decides only whether a request arrives, not who it is.

Status codes: 401 for no credential, an unknown or malformed one, or a revoked token (the message names the fix, and `WWW-Authenticate: Bearer realm="nanotea"` is sent); 403 for a credential that is valid but not allowed that.

Agents and event sources ask for their own tokens, and the approver or the owner approves them
([agents.md](agents.md#tokens)). Settings > Agent tokens (`/tokens`) lists the requests waiting, the tokens, and
the approver, and revokes. The owner can also issue tokens from the machine that runs the service:

```bash
nanotea token add builder                   # writes <config dir>/tokens/builder.token, mode 0600
nanotea token add desk --line main          # bound to the main inbox: desk uses only it, and no one else claims it
nanotea token add ci --events --line main   # a token that can only post events to one line
nanotea token list
nanotea token rotate builder                # new token written, the old one revoked
nanotea token revoke 1a2b3c4d
```

`nanotea setup <harness> --name builder` then prints configuration that names the token file in `NANOTEA_TOKEN_FILE`. The secret is never in a harness config, so those files can be committed. With "One session per agent" on, an agent has one token and one live session; a second `join` under the same name is refused as before. Channels and `tell` between agents work as before: the token is the sender.

The API: `GET /api/tokens[?revoked=1]`, `POST /api/tokens` (`{kind, name, line?, replace?}`, answers with the secret once), `POST /api/tokens/<id>/revoke`, `POST /api/approver` (`{token: id or null}`). All need the pairing key.

Requests: `POST /api/enroll` (`{kind, name, line?, dir?}`, no credential, from this machine only; answers `{request, secret, approver}`), `POST /api/enroll/<id>/collect` (`{secret}`; answers `{request, token}`, the token once approved and only then). The owner or the approver: `GET /api/enroll`, `POST /api/enroll/<id>/approve` and `/deny` (`{reason?}`). The approver: `GET /api/enroll?new=1` and `POST /api/enroll/told` (`{ids}`). Requests are kept in `data/enroll.json` (mode 0600), with only a hash of each secret.

The commands that act as the owner (`nanotea token`, `nanotea sop`) read the key from `data/reply_key`, or from the file `NANOTEA_KEY_FILE` names, which must not be readable by other users. On another machine, copy the key to a file and point `NANOTEA_KEY_FILE` at it.

### What this protects, and what it does not

- A process or a person that can reach the port but has no credential gets 401 on every API route. That includes other users of the machine, other containers, and anything a browser page on another origin can make your browser request.
- A leaked token lets the holder act as that one agent until you revoke it. It can't read other agents' inboxes, ask as another name, or change settings.
- A process running as the same OS user as the agent can read its token file, and the owner's `data/reply_key`, the way it can read your shell history and SSH keys. Tokens do not sandbox agents from each other on one account. To separate them, run each agent as its own OS user, or in its own container, with its own token file mode 0600. The checks that stop one agent using another's token are about mistakes and leaks, not about same-user malice.
- Plain HTTP on a loopback port is fine for a service on this machine only. Anything that crosses a network needs HTTPS, because the credentials are bearer secrets.
- `trusted_proxies` now only chooses which addresses' `X-Forwarded-For` the log believes. It no longer authenticates anyone.

### Bang commands

A bang command is a shell command the owner types, run on the machine of an agent's session. What keeps it from being a hole:

- Two locks, both needed. The owner's switch, and `--bang` in the session's own arguments (`nanotea mcp --bang`, `nanotea-tell --listen --bang`). The switch turns on only with the host key, `data/host_key`, which never leaves the machine: `nanotea bang on` there (under Docker, `docker compose exec nanotea nanotea bang on`). The pairing key, which links and browsers carry, turns it off and never on. Nothing remote can turn the second lock on either.
- The service never runs anything. It queues the command for the one session the line has, and that session's process runs it, as its own user. Only that agent's token can fetch or report its commands; another agent's token, the owner's key and no credential are refused. The owner's key sends a command and reads its output; it can't act as the agent.
- The command is refused unless the line has exactly one open session and that session is the line holder's, so it is clear where it runs; if the line changes hands before the session takes it, it is not run; it expires in 60 seconds rather than waiting for a session that appears later; it is killed at the timeout with everything it started; and every attempt, refused ones too, is in `data/bang-log.jsonl`.
- Voice is never a command. A recording's transcript is not looked at for `!`, because speech recognition mishears.

What the locks do not protect against:

- While the switch is on, whoever holds the pairing key can run a command in any session that was started with `--bang`. That is the point of the feature, and it is a bigger power than the key had before: reading messages and answering as the owner, which agents then act on, becomes running a command directly. Keep the switch off when you are not using it; turning it off works from the phone. A per-device factor for each command comes with per-device pairing ([pairing.md](pairing.md)).
- The opt-in is a flag in a config file. An agent whose own tools can edit its harness's MCP config (or run `nanotea mcp --bang` itself) can give the next session the flag. The first lock stays the owner's switch; the second only keeps the service's own users from opening a shell on a session whose owner never meant to allow it. To make it stronger, don't let the agent write its own config, or run it as an OS user that can't.
- The commands run with the agent's own rights, which are the rights the agent already has. A `bang` result goes into the agent's context: text in it (a file the command printed, a web page it fetched) can try to instruct the model. The agreement tells the agent it is data, which is a request, not a guarantee.
- `full_output` is a path on the service's disk. Under Docker with agents on the host, the path is in the container's data volume.

### Upgrading to tokens

Before this, any process on the machine could use the agents' API unauthenticated. After it, every agent needs a token. The service still starts, logs an ERROR naming the agents that have none, and refuses their calls. Each asks for its token when its `nanotea mcp` next starts; approve it under Tokens, or make one agent the approver first and let it decide the rest. Programs that post events with `nanotea-tell --event` ask the same way. Scripts that call the agents' API directly with curl send `Authorization: Bearer $(cat <token file>)`. Owner pages and reads need the pairing key as before, now on this machine too: open `nanotea pair-link` once in the browser you use.

## Docker

`Dockerfile` and `compose.yaml` run the service, and a Kokoro voice if you ask for one.

```bash
mkdir docker-config
cp docs/docker.config.toml docker-config/config.toml
touch docker-config/env                         # KEY=VALUE secrets; may stay empty
$EDITOR docker-config/config.toml               # [app] owner, public_url, [notify.push] subject
docker compose up -d --build                    # the service
docker compose --profile kokoro up -d --build   # the service and a CPU Kokoro server
docker compose exec nanotea nanotea pair-link   # open the link once on each phone or browser
```

- Image: Python 3.12, ffmpeg, the service installed from `uv.lock`, and openai-whisper on CPU for recordings (in `/opt/whisper`, which the example `[stt.command]` names). `NANOTEA_WHISPER=0` leaves whisper out; then point `[stt.command]` at your own engine. It runs as uid 1000 with no capabilities. `docker compose logs -f nanotea` follows the log. The healthcheck fetches the app icon from inside the container, which shows that the service answers.
- Config: `docker-config/` (or `NANOTEA_CONFIG_DIR`) is mounted read-only at `/config`. [docker.config.toml](docker.config.toml) is the example. Paths in it are relative to `/config`.
- Kokoro: `--profile kokoro` runs `ghcr.io/remsky/kokoro-fastapi-cpu:v0.9.0`, and `--profile kokoro-gpu` runs the `-gpu` image of the same tag (NVIDIA container toolkit needed). Use one. The service reaches it as `http://kokoro:8880`, which is the example's `[tts.kokoro-server] url`. Without a profile nothing answers there, and voicing fails with an error that says so.
- Kokoro inside the service: build with `NANOTEA_KOKORO=1` to add its package, put `backend = "kokoro"` in `[tts]`, and fetch the model files once into the `nanotea-cache` volume with `docker compose run --rm nanotea kokoro download`, before `up`. The image never contains them, and the service will not start without them ([plugins.md](plugins.md#the-two-kokoro-voices)).
- Models: whisper downloads its model into the `nanotea-cache` volume on first use. The example uses `small`; larger ones are slow on CPU.
- Data: the `nanotea-data` volume at `/data`. `docker compose down` keeps it; `down -v` deletes it. Set `NANOTEA_DATA=/an/absolute/path` to use a host directory instead (mounted at the same path inside), set `data_dir` in the config to that path, and set `NANOTEA_UID` and `NANOTEA_GID` if you are not uid 1000. A volume starts empty and unreadable from the host; a host directory lets agents open the recordings and attachments they are told about, because they get paths.
- Update: `git pull && docker compose up -d --build`.

### Agents on the host

Agents run `nanotea mcp`, hooks and `nanotea-tell` on the host, so the host needs the `nanotea` command (`uv tool install .`). They read the same config file for the port and the owner's name, and call the published port on `http://127.0.0.1:<port>`:

```bash
export NANOTEA_CONFIG=$PWD/docker-config/config.toml
docker compose exec nanotea cat /data/reply_key > docker-config/reply_key && chmod 600 docker-config/reply_key
export NANOTEA_KEY_FILE=$PWD/docker-config/reply_key
nanotea setup claude --name builder   # its first session asks for docker-config/tokens/builder.token
```

Each agent presents its own token ([Authentication](#authentication)); the address it connects from grants nothing,
so the bridge address Docker gives host connections is no problem. A request for a token arrives from that address,
which `trusted_proxies` names, so it is taken as from this machine. `nanotea token` acts as the owner, so on the host
it needs the pairing key: a copy as above, or nothing extra when `NANOTEA_DATA` puts the data at the same path on
the host. The token files go beside the config on the host; the container never needs them.

Compose publishes the port to the host's loopback only, so no other machine can reach the service directly.

### The proxy

The phone reaches the service through your HTTPS proxy ([Web access](#web-access)). A proxy on the same machine forwards to `http://127.0.0.1:7447`. Docker usually makes that connection reach the container from the compose network's gateway, `172.30.77.1`, which is why the example config has it in `trusted_proxies`. If the phone is redirected to `public_url` in a loop, the proxy changes the Host header and the service sees it at an address it doesn't trust: the log names each caller's address. Put that address in `trusted_proxies`.

A proxy on another machine or in another container needs the port published to it, for example `"7446:7447"` under `ports` in `compose.yaml` (that exposes the port on every interface, so firewall it to the proxy), and its address as the container sees it in `trusted_proxies`. A page asked for with no credential, from any other address and Host, is sent to `public_url`.

## Finding things

- Waiting on you (`/waiting`, the first link in the sidebar, with a count): every question an agent asked that has no answer yet, newest first, whichever line or channel it came in on. Answering one takes it off the list. "Set aside older than a day" and "Set all aside" clear the list without answering (`POST /api/set-aside` with `{"before": <epoch seconds>}`, owner only): each question is marked seen and tagged set aside, its agent is told (kind `set_aside`, or `nanotea-tell --ask` exits saying so), and it can still be answered from its page. At most 200 are shown; the page says when there are more.
- Unread (`/unread`, with a count): every delivered message you have not opened, newest first. Viewing the list marks nothing seen. "Mark all read" marks them all seen at once (`POST /api/read-all`, owner only). At most 200 are shown.
- Search (`/search?q=words`, in the sidebar): agents' messages whose sender, title, text, spoken script or your answer contain every word, ignoring case, newest first. At most 200 are shown. It reads the message files on each search, so it is meant for a personal board, not millions of messages. What you sent is not searched.
- Export (`/export.md`, with Export all and Export these on the Search page): agents' messages as one markdown file, oldest first, each with its sender, channel, time, id, text as sent and your answer. `?from=<agent>`, `?channel=<name>` and `?q=<words>` narrow it. Audio, clips and attached files are not included; their names are.
- Home Screen shortcuts: the manifest lists Waiting on you, Unread and Search as app shortcuts (long-press the icon). Android honors them; iOS ignores manifest shortcuts, so they do nothing on an iPhone today.
- Keyboard: `/` opens search; `g` then `w`, `u`, `m`, `n` or `s` goes to Waiting, Unread, Messages, Notifications or Search. They do nothing while a field, button or the reaction sheet has focus. They are not tested by the suite, which does not run page scripts.

## Notifications

- In the app: it installs as a Home Screen web app. On iPhone, Web Push works only from the Home Screen app: Share, Add to Home Screen, open it from there, then tap "Turn on notifications". The app icon shows the number of unseen messages.
- Controls (`/notifications`, `POST /api/hush`, kept in `data/hush.json`): mute an agent or a #channel, notify only for questions and failures, and quiet hours. A muted or skipped message still arrives in the app and counts as unread; only the notification is withheld, and an escalation for it is recorded as not sent. Quiet hours (service local time, may cross midnight) hold notifications and escalations; when they end you get one summary of the held questions and unread messages that still need you (what was held is kept in `data/hush-held.json`), and overdue escalations go out then. Permission-prompt and reaction notifications are not covered: held has its own switch in Settings.
- Escalation (off: `escalate = []`): once per message, iMessage when it goes `escalate_after_min` without a response. A question needs an answer; anything else needs opening.
- Tab and badge: the tab title starts with "(N) " and the Home Screen app badge shows N, the messages the owner hasn't seen.
- Unseen messages are marked "new". Delete removes it from the app and moves it to `data/deleted/<id>/`. An agent waiting on a deleted question gets an error.
- Nothing is thrown away: recordings, takes and transcripts are kept. The synthesized pieces of a message with inline clips stay in `parts/`, and the clips' originals in `attachments/`; only the joined track is re-encoded, in `[audio] format`. Several takes sent together are joined the same way, and the takes kept as recorded.
- VAPID key: `data/vapid_private.pem`, made on first run. Subscriptions: `data/push_subscriptions.json`; a subscription the push service reports gone is dropped.
- Heard: playing 90% of a message's audio marks it heard and seen.
- Held at a permission prompt: with Notify when held above 0 in Settings, once per hold when an agent stays held
  that many minutes.
- A conversation page that loses its pairing (the key changed, or the cookie went) stops updating and says to
  open the pairing link again.
- Locked phone: audio keeps playing with the screen locked, with lock screen controls. A conversation page never reloads; a message playing keeps playing while the page updates. While recording, the page keeps the screen on.
- Attachments: every composer has Attach: pictures, PDFs, video (mp4, mov, m4v) and audio (wav, flac, aiff, caf, mp3, m4a, aac, ogg, opus, webm). Pasting a file into the text box, or dropping one anywhere on the page, attaches it too. Files are kept byte for byte as sent, and audio plays in the thread with a link to the file as sent. Uploads go to disk as they arrive, up to `[audio] max_upload_mb`; the proxy must let that much through (nginx `client_max_body_size`; Traefik's buffering middleware, if used). Agents get each file's path, type, size and name with the message (MCP `files`; `[file]` lines from `nanotea-tell`).
- Emoji reactions both ways. The owner taps the smiley under an agent message; on a question still waiting the emoji is the answer, otherwise it goes to the sender's inbox if it holds one, or to the main inbox. Agents react with the `react` tool, or `nanotea-tell --from <holder> --react <id> <emoji> [--inbox NAME]`.

## Config

`nanotea init` writes a starter config to `$NANOTEA_CONFIG`, or `~/.config/nanotea/config.toml`, from
`nanotea/config.example.toml` and the address you give it ([running.md](running.md)). Paths in it are relative
to its directory. An unknown key or table, a value of the wrong type, or a missing required one stops the service
and names it.

- Top level: `public_url` (required: scheme and host, and a port only if not the default, as browsers send it), `host` (default `127.0.0.1`), `port` (7447), `trusted_proxies` (`[]`; the addresses whose `X-Forwarded-*` the log believes and whose pages say "not paired"), `data_dir` (`data`), `env_file` (none; KEY=VALUE secrets, such as `SPEECHIFY_API_KEY`), `plugin_path` (`[]`).
- `[app]`: `owner`, required (who the messages are for: shown in the app and addressed by the rewriter), `name` (default `Nanotea`; shown in the app and the manifest), `channels` (starting channels; none by default), `groups` (starting sidebar groups; none by default).
- `[theme]`, optional: `use`, the theme a new install starts with (`sencha`, the default; `oolong`; `earl grey`; `rooibos`; or one of yours), and `appearance` (`auto`, the default, matches the device; `light`; `dark`). After the first start the owner picks both under Settings > Look, kept in `data/theme.json`. `[theme]` also holds `custom`: `[theme.custom.<name>]` adds a theme: `base`, the built-in it starts from, and `light` and `dark`, tables of the colors to change (`bg`, `fg`, `muted`, `faint`, `line`, `hover`, `card`, `chip`, `accent`, `accent_fg`, `accent_soft`, `side`, `side_fg`, `side_mute`, `side_hover`, `side_on`, `red`, `amber`, `green`, and `leaf` and `vein` for the leaf marks), each `"#rrggbb"`. The theme also colors the browser's bar and the home screen app. `[app] theme_color` is gone; a config that still has it stops the service and says so.
- `[rewrite] backend`, required; the service won't start without a known one.
  - `identity`: the voice reads the original text, less its markdown; the title is the sender's, else the text's first line.
  - `command`: any LLM command line rewrites each message for listening and titles it. The request comes on stdin; stdout must be one JSON object `{"title", "script"}`. Placeholders: `{system}` (the instructions), `{system_file}` (the same in a file), `{schema_file}` (the reply's JSON schema).
  - `claude`: Claude Code's `claude -p` on the local login (`[rewrite.claude]`).
  - The instructions `claude` and `command` send are `nanotea/rewrite_prompt.md`, with the owner's name filled in. The owner can replace them on the Prompts page ([plugins.md](plugins.md#the-rewriters-instructions)); the replacement is `data/prompts/rewrite.md`.
  - Keys: `[rewrite.command]` has `argv` and `timeout_s`; `[rewrite.claude]` has `command`, `model`, `effort` and `timeout_s`; all required.
- `[tts]`, `[stt]` and `[rewrite]` are required. A `command` backend's program must be on the service's PATH, or named by its full path, when the service starts.
- `[tts]`: `speechify` (`[tts.speechify]`: `model`, `timeout_s`, both required; `SPEECHIFY_API_KEY` in `env_file`), or `command` (`argv`, `ext`, `voices`, `timeout_s`, all required) for any local engine (`{text_file}`, `{out}`, `{voice}`; `voices` lists the choices), `kokoro` (the model run in the service, after `nanotea kokoro download`), or `kokoro-server` (a Kokoro-FastAPI server at `url`). Their keys: [plugins.md](plugins.md#the-two-kokoro-voices).
- `[stt]`: `command` (`argv` and `timeout_s`, both required) prints a transcript of `{audio_file}`. The default runs `transcribe.sh` (shipped in the package; `{package}` is where it is) with local openai-whisper and `large-v3-turbo`, loaded per reply; name a python that has openai-whisper.
- `[notify]`: `now` (default `[]`; `["push"]` for the web app's notifications) when a message is ready; `escalate` (`imessage` to `notify.imessage.to`, or `[]`, the default, for none) after `escalate_after_min` (30) without a response. `[notify.imessage]` takes `to`. `[notify.push]` takes `subject`, the contact push services see (`mailto:`, or `https://` with no port or path), is required when `push` is used. Without `push`, the page says notifications are off and why.
- `[audio]`, optional: `format` of a joined track (`mp3`, the default; `aac`; `flac`; `wav`, 24-bit), `kbps` for mp3 and aac (default 192), `rate` (44100, 48000 the default, 88200, 96000), `max_upload_mb` for a file the owner attaches (default 1024). A joined track is a message's speech with its clips, or several takes sent together.
- `[bang]`, optional, for the owner's `!command` ([agents.md](agents.md#bang-commands)); every key is a whole number: `timeout_s` (default 120, 1 to 3600) after which a command is killed, `expire_s` (60, 5 to 600) after which an unclaimed command is "not run", `output_kb` (30, 1 to 1024) per stream for the agent and the page, `keep_mb` (16, 1 to 512) per stream kept on disk under `data/bang/<id>/`. A bad value stops the service and names the key. Whether bang commands are on at all is the owner's Settings switch, off by default.
- `plugin_path`: directories, relative to the config's, for plugins named `"module:Class"`.

Every table and key is shown, with its default and a short comment, in `nanotea/config.example.toml`; a test
fails if the two disagree. The service reads the file once, at start. Settings > Configuration in the app shows
what it is running with, for each of the service, app, voice, transcriber, rewriter, notifications, controls,
audio, bang commands, theme and switch defaults: every key, whether the file set it or the default applies, the
table it is in, what it does, and the voices the engine offers. Secrets show only as set or missing.

Every key there can be changed and saved. Under each plugin in use are the others of its kind and what each takes,
so switching a voice or notifier and setting it up is one save. A save:

1. edits the file in place: the changed lines only, comments and the rest kept;
2. checks the new file as the service would at start (`nanotea config check`), and that it can listen on a new
   host or port; anything it would stop with is shown, and the file is left as it was;
3. keeps the file as it was under `data/config-history/`, writes the new one, and restarts the service in its
   own process. The page waits and reloads.

A secret is set or removed the same way, in `env_file`; its value goes in and is never shown. A file mounted
read-only, as under Docker, says so and changes only on its machine.

Some keys pick the programs the service runs, the code it loads, or where it keeps its data and secrets: a
`command` backend's `argv`, `[rewrite.claude] command`, `[tts.kokoro] model_dir`, a `"module:Class"` plugin and
every key of its table, `data_dir`, `env_file`, `plugin_path`, and `[settings] bang` and `config_programs`. Whoever
has the pairing key could otherwise run anything as the service's user, so these are locked in the app until
`nanotea config programs on` is run on the machine the service runs on. It reads `data/host_key`, like
`nanotea bang on`; off works from anywhere, and the file is always open to whoever edits it.
Settings > Configuration > Prompts also shows the other text sent to models: the working agreement given to agents
(`nanotea sop`) and what agents see when they are told to call in. These are read only.

The owner's settings (what agents get and what each costs in tokens, how messages are read aloud, and who may
arrange the board) are on the Settings page. `[settings]` in the config sets where a new owner starts, with the
same keys; a bad key or value stops the service. See [agents.md](agents.md#settings).

The service logs every API error it answers (status 400 and up) with the method, the path (pairing key
redacted) and the error.

Every backend is a plugin: `nanotea plugins` lists them, `--check` builds the ones the config uses. Writing
one, as a file beside the config or an installed package: [plugins.md](plugins.md). Live calls:
[voice-calls.md](voice-calls.md).

## Reusing the pieces

- Home Screen app: a web app manifest (`display: standalone`, icons, `start_url`) plus `apple-touch-icon`, served over HTTPS. A Home Screen app keeps its cookies apart from Safari, so the manifest is served only with the pairing key and its `start_url` carries it.
- Push: a service worker (`nanotea/static/sw.js`) shows each push, notification before badge, because iOS revokes permission from apps that receive a push without showing one. It tells open pages to update and waits for them (at most 3 seconds) first. The server sends with `pywebpush` (VAPID, TTL, aes128gcm) and drops subscriptions that return 404 or 410.
- Recording: MediaRecorder needs HTTPS. The page uploads the recording as a draft while the owner plays it back; Send attaches it; Whisper transcribes it for the agent. A Screen Wake Lock keeps the screen on while recording. Each take carries what the browser says the microphone did (`getSettings()`: rate, channels, the three kinds of processing, the mic's name), and agents get every take as recorded with it.
- Raw: the recorder's Raw button asks for the microphone without echo cancelling, noise suppression or level control, in stereo at 48 kHz if it can, and records at 256 kbps. It refuses to record if the browser keeps any of the three on, and says what it got. It stays on across reloads; a question an agent asks with `raw` opens with it on. The page notes a raw take's rate, channels and mic.
- Locked playback on iPhone: a plain `<audio>` element stops when the screen sleeps. It keeps playing with `navigator.audioSession.type = "playback"` plus Media Session metadata and action handlers. Set the type back to `"auto"` before `getUserMedia`: a `"playback"` session can't record.
