"""nanotea init: a starter config, from the example, for the way the owner's browsers reach the service."""

import argparse
import shutil
import sys
from urllib.parse import urlsplit

from nanotea.config import CONFIG, PACKAGE, ConfigError, check

LOCAL = "http://127.0.0.1:7447"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
ASK_URL = f"""
The address your browsers use to reach nanotea (docs/running.md has every way, step by step):

  {LOCAL}
      This computer only. Everything works in its browser; a phone can't reach it.
  https://...
      An https address that forwards to this machine: Tailscale Serve, a Cloudflare Tunnel, Caddy with your
      domain, ngrok. Everything works on a phone too.
  http://<this machine's address on your network>:7447
      Plain http on your network. A phone can read and type, but not record, get notifications or install the
      app, and the pairing key crosses the network unencrypted.

address [{LOCAL}]: """
# The say settings of the example's [tts.command], and espeak-ng's, line by line so its comments stay.
ESPEAK = (('argv = ["say", "-v", "{voice}", "-f", "{text_file}", "-o", "{out}"]',
           'argv = ["espeak-ng", "-v", "{voice}", "-f", "{text_file}", "-w", "{out}"]'),
          ('ext = "m4a"', 'ext = "wav"'),
          ('voices = ["Samantha", "Daniel", "Karen", "Moira"]', 'voices = ["en-us", "en-gb", "en-us+f3", "en-gb+f4"]'))


def edit(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"config.example.toml has changed: {old!r} is not in it once")
    return text.replace(old, new)


def config_for(owner: str, url: str, say: bool, espeak: bool) -> str:
    """The example config, set for owner and url; checked as the service would load it."""
    parts = urlsplit(url)
    https, host = parts.scheme == "https", parts.hostname
    text = (PACKAGE / "config.example.toml").read_text()
    text = edit(text, 'owner = "Alex"', f'owner = "{owner}"')
    text = edit(text, 'public_url = "https://nanotea.example.com"', f'public_url = "{url}"')
    text = edit(text, 'env_file = "~/.env"       # KEY=VALUE secrets, e.g. SPEECHIFY_API_KEY',
                '# env_file = "~/.env"     # KEY=VALUE secrets, e.g. SPEECHIFY_API_KEY')
    if https:
        # The push library refuses a contact with a port.
        text = edit(text, 'subject = "https://nanotea.example.com"', f'subject = "https://{host}"')
    else:
        # Browsers give push only to https pages and this machine's own; the push services want an https or
        # mailto: contact, which an http address isn't.
        text = edit(text, 'now = ["push"]            # when a message is ready: the web app\'s notifications',
                    'now = []                  # ["push"] needs an https public_url, or this machine\'s browser and a '
                    'mailto: subject')
        text = edit(text, 'subject = "https://nanotea.example.com"', 'subject = "mailto:you@example.com"')
        text = edit(text, 'trusted_proxies = ["127.0.0.1"]', 'trusted_proxies = []')
        if host not in LOOPBACK:
            text = edit(text, 'host = "127.0.0.1" ', 'host = "0.0.0.0"   ')
            if not parts.port:
                raise ConfigError(f"give the port, as http://{host}:7447")
            text = edit(text, "port = 7447", f"port = {parts.port}")
        elif parts.port:
            text = edit(text, "port = 7447", f"port = {parts.port}")
    if not say and espeak:
        for old, new in ESPEAK:
            text = edit(text, old, new)
    import tomllib
    check(tomllib.loads(text))
    return text


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="nanotea init", description="Write a starter config. Asks for what isn't given "
                                "when run at a terminal.")
    p.add_argument("--owner", help="your name: shown in the app, and how agents address you")
    p.add_argument("--url", help=f"the address your browsers use, as {LOCAL} or https://... (docs/running.md)")
    a = p.parse_args(argv)
    path = CONFIG.expanduser()
    if path.exists():
        sys.exit(f"nanotea: {path} exists; edit it, or remove it to start over")
    tty = sys.stdin.isatty()
    owner = a.owner
    if owner is None:
        if not tty:
            sys.exit("nanotea init: --owner is needed when not run at a terminal")
        owner = input("Your name, as agents should address you: ").strip()
    url = a.url
    if url is None:
        if not tty:
            sys.exit("nanotea init: --url is needed when not run at a terminal")
        url = input(ASK_URL).strip() or LOCAL
    if not owner or '"' in owner or "\\" in owner:
        sys.exit("nanotea init: the owner's name can't be empty or hold quotes or backslashes")
    say, espeak = shutil.which("say") is not None, shutil.which("espeak-ng") is not None
    try:
        text = config_for(owner, url.rstrip("/"), say, espeak)
    except ConfigError as err:
        sys.exit(f"nanotea init: {err}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"wrote {path}")
    parts = urlsplit(url)
    if not say and not espeak:
        print("\nNo voice engine on this machine: the service won't start until [tts] names one. Install espeak-ng "
              "and run init again, or see docs/plugins.md for Kokoro and Speechify.")
    if parts.scheme == "https":
        print(f"\nNext, make {url} forward to http://127.0.0.1:7447 (docs/running.md), then:")
    elif parts.hostname not in LOOPBACK:
        print("\nThe service will listen on every network address. Then:")
    else:
        print("\nThen:")
    print("  nanotea plugins --check     builds the voice, transcriber and rewriter, and says what's missing\n"
          "  nanotea serve\n"
          "  nanotea pair-link           open it in each browser you use")
