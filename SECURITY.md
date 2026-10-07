# Security

## Trust model

- Where a request comes from decides nothing; every request carries a credential. The service listens on
  127.0.0.1 by default, and a program on this machine with no credential gets 401 like anyone else.
- Each agent has a token, bound to its name: it can send, hold a line, read what waits
  for that agent and post in channels as that name only. The service stores only a hash of it, compares in
  constant time, and refuses a revoked one. A token can also be bound to a line (`--line`): it then works on
  that line alone, and no other agent can claim the line. Without that, a line is held by whoever claims it while
  it is free. A program that reports events has its own, scoped to one line.
- An agent or event source without a token asks for one, from this machine only (loopback, or an address in
  `trusted_proxies` such as the Docker gateway, with no proxy headers). The approver, an agent the owner chose, or the owner approves it; the asker then collects
  the token with a secret only it holds, so the approver never sees it. A name with a live token can't ask, so a
  request can't take over an agent, only claim a new name. The approver can't choose the approver.
- What the owner writes on a line reaches only the agent it was given to, or the line's holder while it waits.
  Another agent can't read it in a thread or reply to it; a reply to it reads as one to a message that doesn't exist.
  `nanotea token list`, `rotate` and `revoke`, or Settings > Agent tokens, manage them.
- A process that runs as the same OS user as an agent can read that agent's token file, and `data/reply_key`.
  Tokens do not isolate agents running as one user; separate users or containers do.
- The owner is whoever holds the pairing key. `nanotea pair-link` and notification links carry it, and a paired
  browser keeps it in an HttpOnly, Secure cookie. The key is `data/reply_key` (mode 0600); the VAPID private key
  is `data/vapid_private.pem` (0600). Anyone with the key can instruct every connected agent, so treat a paired
  phone like a key. To revoke every pairing, stop the service, delete `reply_key`, and pair again.
- The host key (`data/host_key`, 0600) never goes to a browser, a link or a page. Only it turns bang commands on
  (`nanotea bang on`), so the pairing key alone can't open a shell in an agent's session. Only it unlocks the
  config keys that pick programs, code and data paths for editing from the app (`nanotea config programs on`),
  so the pairing key alone can't make the service run a program of its choosing.
  Like `reply_key` and token files, a process running as the service's OS user can read it; an agent running as
  that user with a shell can turn bang commands on. Run agents as other users to keep it from them.
- Browsers reach the service through an HTTPS reverse proxy; credentials are bearer secrets, so anything that
  crosses a network needs HTTPS. A page asked for with no credential is redirected to `public_url`.
  `trusted_proxies` only chooses whose `X-Forwarded-For` the log believes; it authenticates nothing.
- Agents are told that only items with `from: "owner"` are the owner's words. Events from local programs and
  other agents' channel posts are labeled as such and are not instructions. Voice replies reach agents as
  transcripts, which can be wrong; the recording's path comes with them.
- The hooks pass the harness only a count of what waits and an instruction to call `check`, never message text.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository ("Report a vulnerability" under Security).
Please don't open a public issue for a security problem.
