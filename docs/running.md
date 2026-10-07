# Reaching nanotea: from this computer to anywhere

Nanotea is one service on the machine your agents run on. Agents reach it on that machine. Your browsers reach
it at `public_url`. How they get there decides what works, because browsers keep the microphone, notifications
and the installed app for pages that came over https, or from the browser's own machine.

Pick the first way below that gives you what you want. Each one says what it needs, the steps, and how it was
checked.

## What each way gives you

| Way | Read and type | Record | Notifications | Home Screen app | Away from home | Needs |
|---|---|---|---|---|---|---|
| [This computer only](#1-this-computer-only) | this computer | this computer | this computer | no | no | nothing |
| [Plain http on your network](#2-plain-http-on-your-network) | yes | no | no | no | no | nothing |
| [Tailscale Serve](#3-tailscale-serve) | yes | yes | yes | yes | yes | Tailscale on this machine and the phone |
| [Cloudflare Tunnel](#4-cloudflare-tunnel) | yes | yes | yes | yes | yes | a domain on Cloudflare (or a quick tunnel, to try) |
| [Caddy and your domain](#5-caddy-and-your-domain) | yes | yes | yes | yes | yes | a domain, and ports 80 and 443 open, or DNS API access |
| [ngrok](#6-ngrok) | yes | yes | yes | yes | yes | an ngrok account; the free plan's limits |
| [A private certificate](#7-a-private-certificate) | yes | yes | likely | likely | no | trusting your own CA on every phone |
| [Traefik and a wildcard](#8-traefik-and-a-wildcard-certificate) | yes | yes | yes | yes | yes | a proxy you already run |

"This computer" means the browser on the machine running nanotea, at `http://127.0.0.1`. On an iPhone,
notifications come only to the Home Screen app, which needs https.

## Before any of them

```bash
uv tool install .                 # from a clone; or pipx install .
nanotea init                      # asks your name and the address; or --owner NAME --url URL
nanotea plugins --check           # builds the voice, transcriber and rewriter; says what is missing
nanotea serve                     # keep it running (launchd/ has a macOS template)
nanotea pair-link                 # open the link once in each browser you use
nanotea setup claude --name builder   # its first session asks for a token; approve it under Tokens
```

`nanotea init` writes the config for the address you give: `host`, `port`, `trusted_proxies` and whether
notifications are on follow from it. It picks a voice engine this machine has: `say` on macOS, `espeak-ng` if
installed, and otherwise says that none was found. The service needs Python 3.12+ and `ffmpeg`; recordings are
transcribed with openai-whisper by default ([plugins.md](plugins.md)).

The service stops at start, naming the table and key, when the config has a mistake or names a program this
machine doesn't have. It warns at start when `public_url` is plain http beyond this machine, or names a port
that only this machine can reach.

## 1. This computer only

For trying nanotea, or for agents you watch from the same desk.

```bash
nanotea init --owner Robin --url http://127.0.0.1:7447
```

Everything works in this computer's browser: browsers treat `http://127.0.0.1` and `http://localhost` as secure.
Use `127.0.0.1`, as in the config: the pairing cookie belongs to the address it was set on, and a page opened
at another one is sent to `public_url`. The cookie isn't marked Secure on plain http, because Safari drops
Secure cookies there even on localhost.

Notifications start off, since push services want a contact address and most people try this first. To get
them in this computer's browser, set `[notify] now = ["push"]` and `[notify.push] subject =
"mailto:you@example.com"`, restart, and tap "Turn on notifications".

Checked: run here, and driven with curl as a browser would (pairing, the cookie, the manifest, the not-paired
page, push turned off). Not opened in a browser.

## 2. Plain http on your network

The phone reads and types, but cannot record, get notifications or install the app. The pairing key crosses
your network unencrypted, so anyone on it who sees the traffic can act as you. Use it only on a network you
trust, and only until you set up one of the ways below.

```bash
nanotea init --owner Robin --url http://192.168.1.20:7447    # this machine's address on your network
```

That sets `host = "0.0.0.0"`, so the service listens on every network address. Every request still needs the
pairing key or a token. The service logs a warning at each start. macOS may ask whether Python may accept
incoming connections. The page tells the phone why recording and notifications are off.

Checked: the config and the warnings, and the service's answers with the phone's address in the Host header,
sent from this machine. Not from a phone.

## 3. Tailscale Serve

The easiest way to everything, at home and away. Tailscale gives this machine an https name with a real
certificate, reachable only from your own devices. No domain, no open ports, nothing to install on the phone
but the Tailscale app.

1. Install Tailscale on this machine and the phone, and sign in to the same account on both.
2. In the Tailscale admin console, under DNS, turn on MagicDNS and HTTPS Certificates.
3. On this machine:

   ```bash
   tailscale serve --bg 7447       # https://<machine>.<tailnet>.ts.net forwards to http://127.0.0.1:7447
   tailscale serve status          # shows the address
   nanotea init --owner Robin --url https://<machine>.<tailnet>.ts.net
   ```

4. `nanotea serve`, then open `nanotea pair-link` on the phone with Tailscale connected.

`init` sets `trusted_proxies = ["127.0.0.1"]`, since Tailscale connects from this machine. The machine and
tailnet names go into the public Certificate Transparency logs. The phone needs Tailscale connected to reach
nanotea; notifications arrive without it, through Apple's or Google's push service.

`tailscale funnel 7447` puts the same address on the open internet instead. Nanotea doesn't need that: every
request needs a credential, but nothing outside your devices needs to reach it.

Checked: the service driven here with the headers Tailscale Serve sends (from Tailscale's source), at an https
`public_url`: pairing, the Secure cookie, the owner's writes, a foreign Origin refused. Tailscale isn't
installed here, so the steps are from Tailscale's docs.

## 4. Cloudflare Tunnel

For a domain already on Cloudflare. `cloudflared` connects out to Cloudflare, so no ports open.

1. In the Cloudflare dashboard, Zero Trust, Networks, Tunnels: create a tunnel, install `cloudflared` as it shows.
2. Add a public hostname, `nanotea.example.com`, with the service `http://127.0.0.1:7447`.
3. `nanotea init --owner Robin --url https://nanotea.example.com`, then `nanotea serve` and pair.

Limits: an upload is at most 100 MB on the free plan, so set `[audio] max_upload_mb` at or under that. Cloudflare
gives up on an answer after about 125 seconds; the page's requests are all short, so that doesn't come up.

A quick tunnel (`cloudflared tunnel --url http://127.0.0.1:7447`) needs no account, but its address changes at
every start. `public_url` would have to change with it, and the Home Screen app and pairing would break each
time. Use it to try nanotea on a phone, not to keep.

Checked: from Cloudflare's docs only. `cloudflared` isn't installed here.

## 5. Caddy and your domain

For a domain you control, pointing at this machine, with ports 80 and 443 reachable from the internet (or a DNS
provider Caddy can use for the DNS challenge). Caddy gets and renews the certificate.

```
nanotea.example.com {
    reverse_proxy 127.0.0.1:7447
}
```

Then `nanotea init --owner Robin --url https://nanotea.example.com`. Nanotea answers anyone on the internet who
finds it, and every request needs a credential.

Checked: Caddy 2.10 with `tls internal` in front of the Docker service, by the end-to-end runs (`e2e/`), with
headless Chromium on Linux trusting Caddy's CA. Not with a public domain or a phone.

## 6. ngrok

`ngrok http 7447` gives an https address; the free plan has one fixed domain, which `public_url` names. Limits
to know: ngrok shows its own warning page to browsers until you tap through it once (per browser, and again
after a week), and the free plan allows 20,000 requests a month. An open conversation asks for news every 5
seconds, about 720 requests an hour, so the free plan suits a trial.

Checked: from ngrok's docs only. Whether the warning page gets in the way of the installed app or notifications
is not documented and was not tried.

## 7. A private certificate

On a network with no domain, `mkcert` or Caddy's `tls internal` makes a certificate from a CA of your own. Every
phone must install and fully trust that CA: on iPhone, install the profile, then turn it on under Settings,
General, About, Certificate Trust Settings. Android Chrome trusts a CA installed under Settings.

Then browsers treat the page as secure, so recording should work. Notifications travel through Apple's or
Google's push service, which doesn't see your certificate; one report has them working on iPhone this way, and
Apple documents nothing either way. Tailscale Serve gives the same result with less to install.

Checked: Caddy's `tls internal` with headless Chromium, by the end-to-end runs, including pairing, recording and
notifications through a stand-in push service. Phones and Apple's or Google's push service: from docs and reports
only.

## 8. Traefik and a wildcard certificate

If you already run Traefik with a wildcard certificate, `deploy/traefik/` adds a route for nanotea: Traefik holds
the certificate, and an nginx container forwards to the machine running nanotea, keeping Traefik's
`X-Forwarded-*` headers. `trusted_proxies` names the address nginx connects from, and `host` must be one that
address can reach. The log names each caller's address if they don't match.

Checked: this is how nanotea's own service runs, and the end-to-end runs put Traefik v3.5 and nginx with
`deploy/traefik/nginx.conf` in front of the Docker service.

## 9. Docker

`compose.yaml` runs the service in a container ([service.md](service.md#docker)). It publishes the port to this
machine's loopback only, so every way above works the same, with one difference: a proxy on this machine reaches
the container from the compose network's gateway, `172.30.77.1`, which the Docker example config trusts. That
holds for `tailscale serve`, `cloudflared` and Caddy on the host. Agents stay on the host and use the `nanotea`
command.

Checked: the end-to-end runs (`e2e/`) start `compose.yaml` with the example config, with and without Kokoro,
and drive it from headless Chromium and agents on Linux.

## 10. Beyond

**Agents on other machines.** Agents call the service at `http://127.0.0.1:<port>` on their own machine. On
another machine, forward that port over SSH, and give the agent its token file and a copy of the config (for the
port and your name):

```bash
ssh -N -L 7447:127.0.0.1:7447 the-nanotea-machine
```

The token alone decides what the agent may do; where it connects from grants nothing. Paths to your recordings
and attachments are paths on the service's machine, so a remote agent gets the transcript but not the files.

**More than one owner.** One service is one owner. Run one per person, each with its own config, `data_dir`,
`port` and address.

## When it doesn't work

| What you see | Why | Fix |
|---|---|---|
| The service stops at start, naming a key | the config has a mistake | change what it names |
| "is not on this service's PATH" | the voice, transcriber or rewriter program isn't installed, or launchd's PATH lacks it | install it, or give its full path |
| "Not paired" on the phone | the browser has no pairing cookie | open `nanotea pair-link` in that browser; on iPhone, again in the Home Screen app if it asks |
| The page loads, but recording and notifications are off | the page came over plain http | one of ways 3 to 8 |
| "Notifications are off: the service's config has no push" | `[notify] now` lacks `"push"` | add it and `[notify.push] subject`, and restart |
| No "Turn on notifications" on iPhone | Safari, not the Home Screen app | Share, Add to Home Screen, open it from there |
| The phone can't connect at all on your network | `host` is `127.0.0.1` | `host = "0.0.0.0"`; the start log warns about this |
| "a request from ... is refused" | the page's address isn't `public_url` | open `public_url` itself |

## How these were checked

"Run here" means a real service on this machine, driven with curl and the test suite, as a browser and a proxy
would drive it. `tests/test_reach.py` keeps those checks: the config's errors, `nanotea init` for each kind of
address, and a service on plain http with no proxy. "From docs" means the steps follow the tool's own
documentation and were not run here.
