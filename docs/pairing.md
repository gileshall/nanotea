# Pairing devices: what it would take

Status: design. Today one pairing key (`data/reply_key`) is the owner. Every browser holds a copy in a cookie,
links carry it, and `nanotea pair-link` prints it.

## The problem

The key is handed out whole. A pairing link, a notification link or the Home Screen manifest carries it, and
whoever reads one of them is the owner from then on. Nothing lists which devices hold it. Unpairing one device
means changing the key, which unpairs all of them. Pairing needs a shell on the machine running nanotea, or a
link copied off a device that already has one. And on iOS a Home Screen app keeps its cookies apart from
Safari, so the key also rides in the manifest's `start_url`.

## Devices, not a key

Each browser gets its own credential: a random secret in an HttpOnly cookie, kept hashed in the token book as a
token of kind `device`, beside agents' tokens. A device has a name ("iPhone, Home Screen"), when it was paired,
how it was paired, and when it was last seen. Settings, Devices lists them; any device can revoke any other,
or itself. `nanotea devices` does the same from the shell.

The pairing key stays, as the root: the owner's own commands send it as a bearer token, and the shell uses it
to pair. It never goes to a browser, a link or a manifest again. Changing it (`nanotea key rotate`) unpairs
nothing.

Pairing is trading a ticket for a device credential. A ticket is single use, expires in ten minutes, and is
shown as a link, a QR code and a short code (`K7P-4QD`: no 0, O, 1, I or L). Tickets live in memory; a restart
drops them.

## The ways in

**The first device, on a new install.** While no device is paired, `nanotea serve` logs that, and the unpaired
page says what to run: `nanotea pair` on the machine (or `docker compose exec nanotea nanotea pair`). It prints
a ticket's QR code in the terminal, the link, and the code. Scan it with the phone's camera, or type the code
on the unpaired page.

**Another device, with a paired one at hand.** The paired device opens Settings, Devices, Pair a device. It
shows a ticket's QR code and code. The new device scans the QR code, or opens the public address and types the
code. Either way it asks for a name, suggested from the browser, and lands on the conversation list.

**The other direction.** The unpaired page shows its own code and waits. On a paired device, Settings, Devices,
Approve a device: type that code, see what is asking (browser, system, when), approve. For a desktop when only
the phone is paired, or a device with no camera. Requests are shown only to an owner who goes looking: they
never notify, so anyone who can reach the page can't fill the phone with them. Unanswered requests expire
with their code.

**The iOS Home Screen app.** Added from a paired Safari page, the manifest's `start_url` carries a ticket made
for that page instead of the key; the first launch trades it and gets its own device. Launches after that
find the cookie and ignore the spent ticket. If the ticket has expired before the first launch, the app opens
unpaired and either way above works. Needs checking on a real phone: how long iOS keeps the manifest it read
at Add to Home Screen, and whether newer iOS copies Safari's cookies into the app.

**A private window, or a borrowed computer.** "Only this session" at pairing time: a session cookie, and a
device that expires after a day unseen. It is listed until then, and can be revoked like any other.

**A lost phone.** Revoke it from any other device, or `nanotea devices revoke NAME`. Its push subscription goes
with it.

**Every device lost.** `nanotea pair` on the machine. Nothing in the browser can recover the owner without
it, by design.

**Notification links.** They carry no credential: the device that subscribed is paired already. Opened on a
device that isn't (the link forwarded, or the device revoked), it's the unpaired page.

## Identity

Pairing proves the owner by having a paired device or the machine. An identity service proves it by who the
owner is, and pairs a device in one step. Each is a plugin of a new kind, `identity`; the owner turns on any of
them in `[identity] use = [...]`. A sign-in makes a device like any other, listed and revocable.

**Passkeys (recommended, built in).** The owner adds a passkey from a paired device, Settings, Passkeys. After
that, the unpaired page has Sign in with a passkey: Face ID or Touch ID, and the device is paired. Passkeys
sync through iCloud Keychain, Google Password Manager or 1Password, so a new phone signs in at once. A
desktop without one gets the browser's own "use a phone" QR code: scan it with the phone, Face ID, and the
desktop is paired. It works in iOS Home Screen apps, which solves their separate cookies too. No third party
is involved; nanotea checks the signatures itself. The cost: a small CBOR reader (stdlib) and `cryptography`
as a direct dependency (installed already, through `pywebpush`), for ES256 and RS256. The relying party is
`public_url`'s host, so changing that address means adding passkeys again.

**A proxy that knows who you are.** Tailscale Serve, Cloudflare Access and oauth2-proxy put the signed-in user
in a header. With `[identity.proxy] header` and `owner` set, a request through a trusted proxy whose header
names the owner pairs the device. Cloudflare's header is a signed JWT, and is checked against their keys.
For people who already sign in at the door.

**Sign in with Google, GitHub or Apple (OIDC).** `[identity.oidc]` with the issuer, client id, the secret's
name in `env_file`, and the owner's subject or email. Familiar, but it needs an app registered with the
provider, a secret to keep, and the provider up at sign-in time; Apple's needs a paid developer account.

## The order

1. Devices and tickets: the device kind, `nanotea pair` and `nanotea devices`, the Devices settings, both
   directions of the code, keys out of links and the manifest. The unpaired page shows no key, no
   notifications button and no stale instructions. QR codes come from a stdlib encoder, drawn as SVG for
   pages and as text for the terminal, like `leaf.py`.
2. Passkeys.
3. The proxy header, then OIDC if wanted.

Moving over: a browser that comes with the old key in its cookie is turned into a device on its first
request, named from its browser, and its cookie replaced. Once the owner turns that off in Settings, the key in
a cookie or a link is refused.
