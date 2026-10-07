# Contributing

```bash
uv sync
uv run python -m unittest discover -s tests
```

The tests start a real service on port 17447 with a temporary data directory and a stand-in voice engine
(`tests/fake_tts.py`), and drive it through `nanotea-tell`, the hooks, and MCP clients on both the 2025-11-25
and 2026-07-28 protocols. They need `ffmpeg` on the PATH.

- Nothing fails silently: an error reaches whoever can act on it (the agent as a tool error, the owner in the
  app, the operator in the log), and the service never guesses past a bad config.
- The message files under `data/` are the record; `nanotea.db` is an index rebuilt from them at each start.
- Agents are harness-neutral: anything harness-specific lives in `nanotea/setup.py` and `nanotea/hook.py`.
- New backends (voice, transcription, rewrite, notification, phone) are plugins: see
  [docs/plugins.md](docs/plugins.md). Agents working on the code: [AGENTS.md](AGENTS.md).
- A change that touches a harness adapter says how it was checked: against the live harness, or only from its
  docs.
