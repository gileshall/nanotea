"""nanotea: the service, its setup, and the agent-side MCP server and hooks."""

import sys

USAGE = """usage: nanotea COMMAND [ARGS]

  serve        run the service
  init         write a starter config (to $NANOTEA_CONFIG, or ~/.config/nanotea/config.toml)
  pair-link    print the link that pairs a phone or browser with the service
  token        issue, list, revoke and rotate the tokens agents connect with (nanotea token --help)
  bang         turn the owner's !commands on or off, on this machine only: nanotea bang on|off|status
  config       check a config as the service would start with it; let the app change what the service runs:
               nanotea config check [FILE], nanotea config programs on|off|status
  setup        print what to paste into a harness (claude, codex, cursor, gemini, ...) to connect an agent
  sop          print the working agreement for agents: an AGENTS.md section, or an Agent Skill (--skill)
  mcp          the MCP server for one agent session, over stdio (nanotea mcp --help)
  hook         harness hooks that bring an agent back to the owner's messages (nanotea hook --help)
  listen       for an agent in listen mode: run in the background, exits with the owner's messages
  leaf         a name's leaf, as the app draws it: SVG, PNG or traits (nanotea leaf --help)
  kokoro       fetch the local Kokoro voice's model files: nanotea kokoro download
  plugins      list the control, voice, transcription, rewrite, notify and phone plugins; --check builds the configured ones
"""


def main() -> None:
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else (None, [])
    if cmd in ("serve", "pair-link") and rest:
        sys.exit(f"nanotea {cmd} takes no arguments")
    if cmd == "serve":
        from nanotea.server import serve
        serve()
    elif cmd == "pair-link":
        from nanotea.server import pair_link
        pair_link()
    elif cmd == "init":
        from nanotea.init_cmd import main as init
        init(rest)
    elif cmd == "mcp":
        from nanotea.relay import main as relay
        relay(rest)
    elif cmd == "token":
        from nanotea.token_cmd import main as token
        token(rest)
    elif cmd == "bang":
        from nanotea.bang_cmd import main as bang
        bang(rest)
    elif cmd == "config":
        from nanotea.configedit import main as config
        config(rest)
    elif cmd == "setup":
        from nanotea.setup import setup
        setup(rest)
    elif cmd == "sop":
        from nanotea.setup import sop
        sop(rest)
    elif cmd == "hook":
        from nanotea.hook import main as hook
        hook(rest)
    elif cmd == "listen":
        from nanotea.listen import main as listen
        listen(rest)
    elif cmd == "leaf":
        from nanotea.leaf import main as leaf
        leaf(rest)
    elif cmd == "kokoro":
        from nanotea.kokoro import main as kokoro
        kokoro(rest)
    elif cmd == "plugins":
        from nanotea.plugins import main as plugins
        plugins(rest)
    elif cmd in ("-h", "--help"):
        print(USAGE, end="")
    else:
        sys.exit(USAGE.rstrip())


if __name__ == "__main__":
    main()
