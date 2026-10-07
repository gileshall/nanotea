# Features survey

What people expect from a messaging-style app, checked against nanotea's code. Effort is easy (small
and local, with a test), medium (touches several files or needs a design choice) or hard (a new subsystem or a
platform limit). "Done here" means this branch added it, one commit each, with tests where the suite can reach it.

| feature | exists? | effort | notes |
|---|---|---|---|
| Search messages | done here | easy | `/search?q=`: every word, case blind, over sender, title, text, script and your answer. A scan of the files, so fine for a personal board. Does not search what you sent. |
| Search what you sent | no | medium | Add the owner's text and transcripts to the scan; the owner rows live in a separate table and render as thread items, not list rows. |
| Filter by agent | yes | easy | Each agent has its own conversation page; `/export.md?from=` and the sidebar groups cover the rest. |
| Filter by channel | yes | easy | Channel pages. |
| Unread filter and jump to unread | done here | easy | `/unread` with a count in the sidebar. Viewing it marks nothing seen. |
| Mark all read | done here | easy | `POST /api/read-all`, button on `/unread`. |
| Mark one message unread | no | easy | Needs a button in the message actions and a seen-flag delete; held back because message actions are being reworked. |
| Waiting on you (open questions) | done here | easy | `/waiting`: every unanswered question, any line or channel, with a sidebar count. |
| Mute per agent or channel | done here | easy | `/notifications`; checked in `Worker` and `Escalator`. Messages still arrive and count as unread. |
| Notify for questions only | done here | easy | Same page. Failures always notify. |
| Do not disturb hours | done here | easy | Same page. Holds notifications and escalations; one summary when they end. Service local time. |
| Digest or summary | done here, partly | medium | The quiet hours summary is one. A scheduled daily digest of unanswered questions needs a timer and a text format. |
| Per-agent notification sound or priority | no | hard | Web Push on iOS gives no per-notification sound control. |
| Notification controls for permission prompts | yes | easy | Notify when held in Settings. Mute and quiet hours do not cover it. |
| Escalation to iMessage | yes | easy | Exists; now respects mute, only questions and quiet hours. |
| Pin a message | partly | medium | "Pin as rule" makes a standing rule. Pinning a message to the top of a thread needs a flag, an index column and a render slot. |
| Archive | yes | n/a | Being removed by another change. |
| Delete | yes | n/a | Moves the message to `data/deleted/`. |
| Keyboard shortcuts | done here | easy | `/` for search; `g` then `w`, `u`, `m`, `n`, `s`. In the shell script, so on every page. Not covered by tests. |
| Export | done here | easy | `/export.md` as markdown, with filters. No audio. A zip that includes audio and files is medium: stream the archive, handle sizes. |
| Home Screen shortcuts | done here | easy | Manifest `shortcuts`. Android only; iOS ignores them. |
| Message links | yes | easy | `/m/<id>` and notification links with the key; no copy-link button. |
| Copy link or copy text button | partly | easy | Only code blocks copy. A button per message goes in `_acts`, which is being reworked. |
| Share sheet | no | easy | `navigator.share` on a message page; same `_acts` caveat. |
| Quick replies | partly | medium | Reactions answer a waiting question. Canned text answers per question would need a field on `ask` (suggested answers) and buttons that post a reply. |
| Dark mode | yes | n/a | `prefers-color-scheme` in the stylesheet and theme-color. |
| Read receipts | yes | n/a | "Picked up by" on what you sent; "seen" per agent in the sidebar; heard at 90% of the audio. |
| Typing indicators | yes | n/a | Both ways. |
| Reactions | yes | n/a | Both ways. |
| Attachments | yes | n/a | Pictures, PDFs, audio, kept byte for byte. |
| Voice replies and transcripts | yes | n/a | The core feature. |
| Drafts that survive a reload | yes | n/a | localStorage and IndexedDB. |
| Edit or unsend what you sent | no | medium | Agents may already have it. Needs a "replaced" state agents are told about. |
| Snooze a question | no | medium | Needs a per-message snooze time that the escalator and the Waiting page respect. |
| Per-conversation scroll and focus jump | partly | easy | Notification links anchor to the message. |
| Multi-device presence | partly | hard | Any paired device works; there is no per-device state beyond the Home Screen badge. |
| Account sharing or multiple owners | no | hard | One owner by design. |
| Offline reading | no | hard | No cache of pages in the service worker; a design decision, since pages carry the key. |
| Calendar or reminder integration | no | medium | A notify plugin could do it. |
| Voice calls | no | hard | Designed in `docs/voice-calls.md`, not built. |

## What is not verified

The suite starts a real service and drives it over HTTP. It does not run any page script, so the keyboard
shortcuts, the form handling on `/notifications`, the Mark all read button and the export and search buttons were
checked only by node parsing every inline script and by asserting the served HTML. Nothing here was looked at on
a phone or in a browser. The quiet hours summary waits on a 10 second check, so tests wait for it.
