"""HTML pages: messages, one message, conversations with agents, channels, unpaired."""

import hashlib
import html
import json
import logging
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from urllib.parse import quote, quote_plus

from nanotea import markdown
from nanotea.index import thread_key
from nanotea.markdown import NEW_TAB, URL, trim, urls
from nanotea.inbox import excerpt
from nanotea.store import Message, Store, media_kind

e = html.escape
log = logging.getLogger("nanotea")

STYLE = """
:root {
  font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  -webkit-text-size-adjust: 100%;
  --gut: 1rem;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { background: var(--bg); color: var(--fg); }
body { margin: 0; -webkit-tap-highlight-color: transparent; }
a { color: var(--accent); }
button { font: inherit; color: inherit; cursor: pointer; }
button:disabled { opacity: 0.5; cursor: default; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* Sidebar: a drawer on the phone, always there on a wide screen. */
#side { position: fixed; z-index: 30; top: 0; bottom: 0; left: 0; width: 17rem; max-width: 85vw; overflow-y: auto;
        background: var(--side); color: var(--side-fg);
        padding: calc(0.8rem + env(safe-area-inset-top)) 0.6rem calc(2rem + env(safe-area-inset-bottom))
                 calc(0.6rem + env(safe-area-inset-left));
        transform: translateX(-102%); transition: transform 0.26s cubic-bezier(0.2, 0.8, 0.2, 1); }
body.drawer #side { transform: none; box-shadow: 0 0 40px #0008; }
#scrim { position: fixed; inset: 0; z-index: 25; background: #0007; opacity: 0; pointer-events: none;
         transition: opacity 0.26s; }
body.drawer #scrim { opacity: 1; pointer-events: auto; }
body.dragging #side, body.dragging #scrim { transition: none; }
body.dragging #side { box-shadow: 0 0 40px #0008; }
.brand { display: block; color: #fff; font-weight: 800; font-size: 1.25rem; letter-spacing: -0.02em;
         text-decoration: none; padding: 0.2rem 0.6rem 0.8rem; }
.sect { display: flex; align-items: center; justify-content: space-between; margin: 1.1rem 0.6rem 0.25rem;
        font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: var(--side-mute); }
.sect .add { border: 0; background: transparent; color: var(--side-mute); font-size: 1.2rem; line-height: 1;
             padding: 0.1rem 0.45rem; border-radius: 6px; }
.sect .add:hover { background: var(--side-hover); color: #fff; }
/* A group folds from its header; folded, the header shows the group's unread total. */
.fold { flex: 1; min-width: 0; display: flex; align-items: center; gap: 0.4rem; border: 0; background: transparent;
        padding: 0.15rem 0; color: inherit; text-align: left; text-transform: inherit; letter-spacing: inherit; }
.fold:hover { color: #fff; }
.caret { flex: none; width: 0; height: 0; border-left: 0.28rem solid transparent; border-right: 0.28rem solid transparent;
         border-top: 0.36rem solid currentColor; transition: transform 0.15s; }
.grp.folded .caret { transform: rotate(-90deg); }
.grp.folded .rows { display: none; }
.fold .count { text-transform: none; letter-spacing: 0; }
.grp:not(.folded) .fold .count { display: none; }
/* How long ago an agent not connected was last seen. */
.conv .seen { flex: none; margin-left: auto; font-size: 0.68rem; color: var(--side-mute); }
.conv .seen + .count { margin-left: 0.3rem; }
.grp[data-grp="Not connected"] .conv:not(.unread):not([aria-current]) { opacity: 0.72; }
.conv { display: flex; align-items: center; gap: 0.55rem; padding: 0.36rem 0.6rem; border-radius: 8px;
        color: inherit; text-decoration: none; min-height: 2.25rem; transition: background 0.12s; }
.conv:hover { background: var(--side-hover); }
.conv[aria-current] { background: var(--side-on); color: #fff; }
.conv.unread { color: #fff; font-weight: 700; }
.conv .hash { width: 1.5rem; text-align: center; font-size: 1.05rem; opacity: 0.65; font-weight: 500; }
.conv .avatar { width: 1.5rem; height: 1.5rem; border-radius: 6px; font-size: 0.6rem; }
.who { flex: 1; min-width: 0; line-height: 1.2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.role { display: block; font-size: 0.72rem; font-weight: 400; color: var(--side-mute); overflow: hidden;
        text-overflow: ellipsis; }
.conv[aria-current] .role, .conv[aria-current] .stl { color: #ffffffb0; }
.nm { display: block; overflow: hidden; text-overflow: ellipsis; }
/* An agent's status, under its role: the text gives way, its age doesn't. */
.stl { display: flex; gap: 0.3rem; font-size: 0.66rem; font-weight: 400; color: var(--side-mute); opacity: 0.8; }
.stl .st-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stl .age { flex: none; }
.conv.ch .who { flex: 0 1 auto; }
.conv > .dot { margin: 0; }
.conv .count { flex: none; margin-left: auto; }
/* A direct line's listening dot sits on the avatar's corner, clear of the ellipsis. */
.face { position: relative; flex: none; display: flex; }
.face .dot { position: absolute; right: -0.22rem; bottom: -0.22rem; margin: 0; width: 0.6rem; height: 0.6rem;
             box-shadow: 0 0 0 2px var(--side); }
.conv[aria-current] .face .dot { box-shadow: 0 0 0 2px var(--side-on); }
.count { background: var(--red); color: #fff; border-radius: 999px; padding: 0 0.4rem; min-width: 1.25rem;
         font-size: 0.72rem; font-weight: 700; line-height: 1.25rem; text-align: center; }
.dot { flex: none; display: inline-block; width: 0.5rem; height: 0.5rem; border-radius: 50%; background: #3fd08a;
       margin-left: 0.35rem; vertical-align: middle; position: relative; }
/* Pulses while its agent works on the owner's message or types; with reduced motion, a steady ring. */
.dot.pulse::after { content: ""; position: absolute; inset: -0.2rem; border-radius: 50%; border: 2px solid #3fd08a;
                    animation: ring 1.4s ease-out infinite; pointer-events: none; }
@keyframes ring { from { transform: scale(0.5); opacity: 0.9; } to { transform: scale(1.3); opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .dot.pulse::after { opacity: 0.75; } }
.ear .dot { margin: 0 0.25rem 0 0; }
/* Held at a permission prompt: amber, steady. */
.dot.held { background: var(--amber); }
.stl.held { color: var(--amber); opacity: 1; }
.foot { margin-top: 1.2rem; padding-top: 0.6rem; border-top: 1px solid var(--side-hover); }
/* Settings, Rules and Agent talk. */
.form { max-width: 42rem; }
.form .row { display: flex; gap: 0.9rem; align-items: flex-start; padding: 0.8rem 0; border-bottom: 1px solid var(--line); }
.form .row .about { flex: 1; min-width: 0; }
.form .row .about b { display: block; }
.form .row .about p { margin: 0.15rem 0 0; }
.form .row .ctl { flex: none; display: flex; flex-direction: column; align-items: flex-end; gap: 0.2rem; }
.form input[type=number] { width: 5rem; }
.form input, .form select, .form textarea { font: inherit; color: inherit; background: var(--card);
                                           border: 1px solid var(--line); border-radius: 8px; padding: 0.3rem 0.5rem; }
.form textarea { width: 100%; min-height: 4.5rem; resize: vertical; }
.form h2 { font-size: 1rem; margin: 1.4rem 0 0.2rem; }
/* Configuration and Prompts. */
.cfg { max-width: 42rem; }
.cfg h2 { font-size: 1.05rem; margin: 1.6rem 0 0.2rem; }
.cfg h3 { font-size: 0.95rem; margin: 1.1rem 0 0.1rem; }
.cfg p code { overflow-wrap: anywhere; }
.cfg .where { font-size: 0.8rem; font-weight: 400; color: var(--muted); margin: 0.1rem 0 0.3rem; }
.cfg .jump { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.4rem 0 0.6rem; }
.cfg details { margin: 0.5rem 0; }
.cfg summary { cursor: pointer; padding: 0.2rem 0; }
.cfg pre { max-height: 28rem; overflow: auto; }
.cfg textarea { width: 100%; font: 0.82rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
.opt { padding: 0.6rem 0; border-bottom: 1px solid var(--line); }
.opt .k { display: flex; flex-wrap: wrap; gap: 0.2rem 0.6rem; align-items: baseline; }
.opt .key { font-weight: 600; }
.opt .val { font: 0.85rem/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere;
            white-space: pre-wrap; background: var(--chip); border-radius: 6px; padding: 0.1rem 0.4rem;
            display: inline-block; max-width: 100%; }
.opt .src { font-size: 0.72rem; border-radius: 999px; padding: 0 0.5rem; background: var(--chip); color: var(--muted); }
.opt .src.config { background: var(--accent-soft); color: var(--accent); }
.opt .src.missing { color: var(--red); }
.opt p { margin: 0.25rem 0 0; }
.opt .field { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: flex-start; margin-top: 0.3rem; }
.opt .edit, .opt .secret { font: 0.85rem/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; color: inherit;
                           background: var(--card); border: 1px solid var(--line); border-radius: 8px;
                           padding: 0.3rem 0.5rem; min-width: 0; flex: 1 1 14rem; max-width: 100%; }
.opt select.edit, .opt input[type=number].edit { flex: 0 1 auto; }
.opt textarea.edit { flex-basis: 100%; resize: vertical; }
.opt .edit:disabled { opacity: 0.6; }
.opt.dirty .edit { border-color: var(--accent); }
.opt.unset .edit { text-decoration: line-through; }
.opt .bad, .opt .lock { color: var(--red); }
.opt .lock { color: var(--muted); font-style: italic; }
#cfg-save { position: sticky; bottom: 0; display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center;
            padding: 0.6rem 0.75rem; margin-top: 1rem; background: var(--card); border: 1px solid var(--accent);
            border-radius: 12px; box-shadow: 0 4px 18px #0003; }
#cfg-save[hidden] { display: none; }
#cfg-count { flex: 1; }
#cfg-note { flex-basis: 100%; }
table.voices, table.lines { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
table.voices td, table.lines td { padding: 0.25rem 0.5rem 0.25rem 0; border-bottom: 1px solid var(--line);
                                  vertical-align: top; }
table.lines tr.off { opacity: 0.55; }
.themes { display: flex; flex-wrap: wrap; gap: 0.6rem; padding: 0.6rem 0 0.8rem; }
.theme-pick { font: inherit; color: var(--fg); background: none; border: 2px solid transparent; border-radius: 0.8rem;
  padding: 0.25rem; cursor: pointer; display: flex; flex-direction: column; align-items: center; gap: 0.3rem; }
.theme-pick[aria-checked=true] { border-color: var(--accent); }
.theme-pick .swatch { display: flex; width: 6.5rem; height: 3.6rem; border-radius: 0.55rem; overflow: hidden;
  box-shadow: 0 0 0 1px var(--line); }
.theme-pick .half { flex: 1; display: flex; }
.theme-pick .half i { width: 0.7rem; }
.theme-pick .half b { flex: 1; display: grid; place-items: center; }
.theme-pick .half b::after { content: ""; width: 1rem; height: 1rem; border-radius: 50%; background: var(--dot); }
.theme-pick .name { font-size: 0.85rem; }
.tok { font-size: 0.75rem; color: var(--muted); white-space: nowrap; }
.total { font-size: 1.05rem; }
.rule { display: flex; gap: 0.8rem; align-items: flex-start; padding: 0.7rem 0; border-bottom: 1px solid var(--line); }
.rule .txt { flex: 1; min-width: 0; }
.talk { padding: 0.6rem 0; border-bottom: 1px solid var(--line); }

/* Header */
#top { position: sticky; top: 0; z-index: 20; display: flex; flex-wrap: wrap; align-items: center; gap: 0.25rem 0.6rem;
       padding: calc(0.5rem + env(safe-area-inset-top)) calc(1rem + env(safe-area-inset-right)) 0.5rem
                calc(0.7rem + env(safe-area-inset-left));
       background: color-mix(in srgb, var(--bg) 86%, transparent); border-bottom: 1px solid var(--line);
       -webkit-backdrop-filter: saturate(1.6) blur(14px); backdrop-filter: saturate(1.6) blur(14px); }
#menu { position: relative; border: 0; background: transparent; padding: 0.35rem; border-radius: 8px;
        display: grid; place-items: center; }
#menu:hover { background: var(--hover); }
#menu svg { width: 1.45rem; height: 1.45rem; }
#menu .count { position: absolute; top: -0.15rem; right: -0.4rem; box-shadow: 0 0 0 2px var(--bg); }
.titles { flex: 1; min-width: 0; }
h1 { font-size: 1.1rem; font-weight: 750; letter-spacing: -0.01em; margin: 0; white-space: nowrap; overflow: hidden;
     text-overflow: ellipsis; }
.sub { font-size: 0.78rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.titles .status { font-size: 0.82rem; line-height: 1.3; margin-top: 0.1rem; overflow-wrap: anywhere; }
.titles .status .ago { color: var(--muted); white-space: nowrap; }
#screen-note { flex-basis: 100%; margin: 0; font-size: 0.85rem; }
main { max-width: 52rem; margin: 0 auto;
       padding: 0.25rem calc(1rem + env(safe-area-inset-right)) 1.5rem calc(1rem + env(safe-area-inset-left)); }
.notice { font-size: 0.85rem; margin: 0.6rem 0; padding: 0.5rem 0.75rem; border-radius: 8px; background: var(--chip); }
.notice:empty { display: none; }
.older, .more { text-align: center; margin: 0.8rem 0; }
.more.failed { color: var(--red); cursor: pointer; }
#unsent { background: color-mix(in srgb, var(--red) 12%, transparent); color: var(--red); }
.about { white-space: pre-wrap; font-size: 0.9rem; color: var(--muted); margin: 0.7rem 0 0; padding: 0.55rem 0.75rem;
         border-left: 3px solid var(--accent); background: var(--accent-soft); border-radius: 0 8px 8px 0; }

/* Buttons */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 0.4rem; border: 1px solid var(--line);
       background: var(--card); color: var(--fg); border-radius: 8px; padding: 0.45rem 0.9rem; font-size: 0.9rem;
       font-weight: 650; text-decoration: none; transition: transform 0.08s, background 0.15s; }
.btn:hover { background: var(--hover); }
.btn:active { transform: scale(0.97); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
.btn.small { padding: 0.25rem 0.65rem; font-size: 0.8rem; }

/* Controls an agent attached: buttons, checklists, whatever a control plugin draws. */
.control { margin: 0.5rem 0 0.3rem; display: flex; flex-direction: column; gap: 0.45rem; }
.control .row { display: flex; flex-wrap: wrap; align-items: center; gap: 0.45rem; }
.control .btn.small { padding: 0.4rem 0.85rem; font-size: 0.88rem; }
.control .btn.on { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); font-weight: 600; }
.control .ticks { display: flex; flex-direction: column; gap: 0.15rem; }
.control .tick { display: flex; align-items: center; gap: 0.55rem; padding: 0.2rem 0; }
.control .tick input { width: 1.15rem; height: 1.15rem; accent-color: var(--accent); }
.control.busy { opacity: 0.6; pointer-events: none; }
.control .error { margin: 0; }

/* The thread */
.day { display: flex; align-items: center; gap: 0.75rem; margin: 1.1rem 0 0.3rem; color: var(--muted);
       font-size: 0.76rem; font-weight: 700; }
.day::before, .day::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.msg { display: grid; grid-template-columns: 2.4rem minmax(0, 1fr); gap: 0 0.65rem; padding: 0.45rem 0.5rem;
       margin: 0 -0.5rem; border-radius: 10px; scroll-margin-top: 5rem; transition: background 0.15s, transform 0.2s; }
.msg:hover, .msg.picked { background: var(--hover); }
.msg.swiping { transition: background 0.15s; }
html.convo .msg:not(.activity) { touch-action: pan-y; }
.msg.cont { padding-top: 0.05rem; }
.msg:target { animation: flash 2.4s ease-out; }
.gutter .t { display: block; visibility: hidden; font-size: 0.66rem; color: var(--faint); text-align: right;
             padding-top: 0.25rem; white-space: nowrap; }
.msg.cont:hover .gutter .t { visibility: visible; }
.avatar { flex: none; width: 2.4rem; height: 2.4rem; border-radius: 9px; color: #fff; font-size: 0.85rem;
          font-weight: 700; letter-spacing: 0.02em; display: grid; place-items: center; }
.avatar.g { background: var(--accent); color: var(--accent-fg); }
/* An agent's leaf: its shape from its name, cut out of the theme's leaf color, tinted toward a hue of its own. */
.avatar.leaf { background: var(--vein); }
.avatar.leaf i { width: 86%; height: 86%; background: color-mix(in oklab, var(--leaf) 65%, var(--tint));
  -webkit-mask: var(--src) center / contain no-repeat; mask: var(--src) center / contain no-repeat; }
.brand { display: flex; align-items: center; gap: 0.45rem; }
/* On the sidebar the leaf goes toward the sidebar's text: the leaf color alone is too close to it. */
.brand .mark { flex: none; width: 1.7rem; height: 1.7rem;
  background: color-mix(in oklab, var(--leaf) 60%, var(--side-fg));
  -webkit-mask: url(/leaf/__MARK__.svg?size=32) center / contain no-repeat;
  mask: url(/leaf/__MARK__.svg?size=32) center / contain no-repeat; }
.line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 0.45rem; line-height: 1.35; }
.name { font-weight: 750; }
.time { color: var(--faint); font-size: 0.75rem; text-decoration: none; }
a.time:hover { text-decoration: underline; }
.where { color: var(--muted); font-size: 0.8rem; font-weight: 650; text-decoration: none; }
.subject { font-weight: 650; line-height: 1.35; margin-top: 0.05rem; }
.subject a { color: inherit; text-decoration: none; }
.subject a:hover { text-decoration: underline; }
.subject .tool { min-height: 0; padding: 0 0.3rem; margin-left: 0.2rem; vertical-align: -0.2em; }
.subject .tool svg { width: 1.05rem; height: 1.05rem; }
.got { display: inline-flex; align-self: center; color: var(--faint); }
.got svg { width: 0.85rem; height: 0.85rem; }
.tag { display: inline-block; font-size: 0.66rem; font-weight: 750; letter-spacing: 0.02em; border-radius: 999px;
       padding: 0.05rem 0.45rem; margin-left: 0.35rem; vertical-align: 0.12em; }
.tag.new { background: var(--red); color: #fff; }
.tag.heard { background: var(--green); color: #fff; }
.tag.asks { background: color-mix(in srgb, var(--amber) 18%, transparent); color: var(--amber); }
.tag.status { background: var(--chip); color: var(--amber); }
.failed .tag.status { color: var(--red); }
.text { white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 0.1rem; }
.list .text { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
/* Markdown in .text: no whitespace between tags, line breaks kept by pre-wrap. */
.text p, .text ul, .text ol, .text blockquote, .text .code, .text .table, .text hr { margin: 0 0 0.55em; }
.text > :last-child, .text li > :last-child, .text blockquote > :last-child { margin-bottom: 0; }
.text h1, .text h2, .text h3, .text h4, .text h5, .text h6 { line-height: 1.3; margin: 0.35em 0 0.3em; }
.text h1 { font-size: 1.3em; } .text h2 { font-size: 1.18em; } .text h3 { font-size: 1.06em; }
.text h4, .text h5, .text h6 { font-size: 1em; }
.text > h1:first-child, .text > h2:first-child, .text > h3:first-child { margin-top: 0; }
.text ul, .text ol { padding-left: 1.4em; }
.text li + li { margin-top: 0.15em; }
.text li > ul, .text li > ol { margin: 0.15em 0 0; }
.text li.task { list-style: none; margin-left: -1.3em; }
.text li.task > input { margin: 0 0.4em 0 0; vertical-align: -0.1em; }
.text blockquote { border-left: 3px solid var(--line); padding-left: 0.7em; color: var(--muted); }
.text code { font: 0.86em/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--chip);
             border-radius: 5px; padding: 0.08em 0.32em; }
.text .code { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--chip); }
.text .code-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
                   padding: 0.1rem 0.2rem 0 0.7rem; font-size: 0.72rem; color: var(--muted); }
.text .code-head .tool { min-height: 1.4rem; font-size: 0.72rem; }
.text .code pre { white-space: pre; overflow-wrap: normal; overflow-x: auto; margin: 0; border-radius: 0;
                  padding: 0.2rem 0.75rem 0.6rem; }
.text .code pre code { font: inherit; background: none; padding: 0; border-radius: 0; }
.text .table { overflow-x: auto; }
.text table { border-collapse: collapse; font-size: 0.92em; white-space: normal; }
.text th, .text td { border: 1px solid var(--line); padding: 0.25em 0.55em; vertical-align: top; }
.text th { background: var(--chip); font-weight: 700; }
.text hr { border: 0; border-top: 1px solid var(--line); }
.text a.img::before { content: "Image: "; color: var(--muted); }
.transcript .label { color: var(--muted); font-size: 0.78rem; font-weight: 650; margin-right: 0.3rem; }
.big { font-size: 1.9rem; line-height: 1.25; }
audio { display: block; width: 100%; max-width: 30rem; height: 2.5rem; margin: 0.4rem 0 0.2rem; }
.links { list-style: none; margin: 0.2rem 0; padding: 0; font-size: 0.9rem; }
.links a, pre a { overflow-wrap: anywhere; }
details { margin-top: 0.25rem; }
summary { display: inline-block; list-style: none; color: var(--muted); font-size: 0.8rem; cursor: pointer; }
summary::-webkit-details-marker { display: none; }
summary::before { content: "\\25B8  "; }
details[open] > summary::before { content: "\\25BE  "; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font: 0.82rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
      background: var(--chip); border-radius: 8px; padding: 0.6rem 0.75rem; margin: 0.35rem 0; }
.clip { font-size: 0.8rem; color: var(--muted); margin-top: 0.3rem; }
.quote { display: block; border-left: 3px solid var(--line); padding: 0 0 0 0.55rem; margin: 0.15rem 0 0.2rem;
         color: var(--muted); font-size: 0.82rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.quote a { color: inherit; text-decoration: none; }
.quote:hover { border-left-color: var(--accent); }
.quote span { color: var(--fg); }
.quote .in-thread { margin-left: 0.4rem; color: var(--accent); }
.quote .in-thread:hover { text-decoration: underline; }
.replies { display: inline-flex; align-items: center; gap: 0.45rem; max-width: 100%; margin-top: 0.35rem;
           padding: 0.2rem 0.6rem 0.2rem 0.3rem; border: 1px solid transparent; border-radius: 9px; color: var(--muted);
           font-size: 0.8rem; text-decoration: none; white-space: nowrap; }
.replies:hover { border-color: var(--line); background: var(--card); }
.replies b { color: var(--accent); font-weight: 700; }
.replies .tag { margin: 0; }
.replies .faces { display: inline-flex; }
.replies .avatar { width: 1.35rem; height: 1.35rem; border-radius: 5px; font-size: 0.55rem; margin-right: -0.25rem;
                   box-shadow: 0 0 0 2px var(--bg); }
.replies .faces .avatar:last-child { margin-right: 0; }
.replies .open { display: none; }
.replies:hover .open { display: inline; }
.replies:hover .last { display: none; }
.msg.activity .what { display: block; color: inherit; text-decoration: none; white-space: nowrap; overflow: hidden;
                      text-overflow: ellipsis; }
.msg.activity .what:hover { text-decoration: underline; }
.msg.activity.hit { animation: flash 2.4s ease-out; }
.answer-first { display: flex; flex-wrap: wrap; align-items: center; gap: 0.6rem; color: var(--muted);
                font-size: 0.85rem; margin: 0.5rem 0; }
.state { color: var(--faint); font-size: 0.75rem; margin-top: 0.15rem; }
.bang .cmd { margin: 0.1rem 0 0.25rem; }
.bang .cmd::before { content: "$ "; color: var(--muted); }
.bang .result { color: var(--muted); font-size: 0.8rem; margin: 0.2rem 0; }
.bang .result.bad { color: var(--red); }
.bang .out { margin: 0.2rem 0; }
.bang .out .label { color: var(--muted); font-size: 0.75rem; }
.bang .out pre { max-height: 24rem; overflow: auto; margin: 0.1rem 0 0.2rem; }
.bang .out.err pre { border-left: 3px solid var(--red); }
.bang .cut { color: var(--muted); font-size: 0.75rem; margin: 0 0 0.3rem; }
.bang-hint { margin: 0.2rem 0.35rem 0; font-size: 0.78rem; color: var(--muted); }
.answer { display: grid; grid-template-columns: 1.6rem minmax(0, 1fr); gap: 0 0.55rem; margin-top: 0.5rem;
          padding: 0.5rem 0.65rem; border-radius: 10px; background: var(--accent-soft); }
.answer .avatar { width: 1.6rem; height: 1.6rem; border-radius: 6px; font-size: 0.62rem; }
.thumb { display: block; max-width: min(100%, 22rem); max-height: 16rem; border-radius: 8px; margin: 0.35rem 0;
         border: 1px solid var(--line); }
video.media { display: block; max-width: min(100%, 32rem); max-height: 70vh; border-radius: 8px; margin: 0.35rem 0;
              background: #000; }
.file { display: inline-block; margin: 0.3rem 0; padding: 0.35rem 0.7rem; border: 1px solid var(--line);
        border-radius: 8px; font-size: 0.88rem; text-decoration: none; }
.acts { display: flex; flex-wrap: wrap; align-items: center; gap: 0.25rem; margin-top: 0.3rem; }
.acts:not(:has(.pill, .tool)) { display: none; }
.reacts { display: contents; }
.pill { display: inline-flex; align-items: center; gap: 0.3rem; border: 1px solid var(--line); background: var(--chip);
        border-radius: 999px; padding: 0 0.5rem; font-size: 0.95rem; line-height: 1.6; transition: transform 0.12s; }
button.pill:active { transform: scale(0.9); }
.pill.on { border-color: var(--accent); background: var(--accent-soft); }
.pill small { font-size: 0.72rem; color: var(--muted); }
.tool { display: inline-flex; align-items: center; gap: 0.25rem; border: 0; background: transparent; color: var(--muted);
        font-size: 0.78rem; font-weight: 650; padding: 0.2rem 0.45rem; border-radius: 6px; text-decoration: none;
        min-height: 1.7rem; }
.tool:hover { background: var(--chip); color: var(--fg); }
.tool svg { width: 1.1rem; height: 1.1rem; }
.tool.strong { color: var(--accent); }
.tool.clipbtn { background: var(--chip); color: var(--accent); vertical-align: baseline; margin: 0.1rem 0; }
.tool.clipbtn.on { background: var(--accent-soft); }
.event { display: flex; justify-content: center; align-items: center; flex-wrap: wrap; gap: 0.35rem; text-align: center;
         color: var(--muted); font-size: 0.8rem; margin: 0.5rem 0; scroll-margin-top: 5rem; }
.event .emoji { font-size: 1.1rem; }
.typing { display: flex; align-items: center; gap: 0.5rem; color: var(--muted); font-size: 0.8rem; margin: 0.3rem 0 0.2rem;
          padding-left: 0.3rem; }
.typing .dot { margin: 0; }
.meta { color: var(--muted); font-size: 0.85rem; }
.error { color: var(--red); }
.empty { text-align: center; color: var(--muted); padding: 3rem 1rem; }
.empty .mark { font-size: 2.6rem; font-weight: 800; color: var(--line); line-height: 1; }
#chat-status { font-size: 0.8rem; color: var(--muted); margin: 0.4rem 0 0; }
#chat-status:empty { display: none; }

/* Conversations: the pane fills the visible viewport; the list scrolls between header and composer. */
html.convo, html.convo body { height: 100%; overflow: hidden; }
html.convo #pane { position: fixed; left: 0; right: 0; top: var(--vv-top); height: var(--vv-h);
                   display: flex; flex-direction: column; }
html.convo #top { position: relative; flex: none; }
html.convo main { flex: 1; min-height: 0; max-width: none; margin: 0; overflow-y: auto; overscroll-behavior: contain;
                  -webkit-overflow-scrolling: touch;
                  padding: 0.25rem max(calc(var(--gut) + env(safe-area-inset-right)), calc(50% - 25rem)) 0.6rem
                           max(calc(var(--gut) + env(safe-area-inset-left)), calc(50% - 25rem)); }
#composer { flex: none; max-height: 65%; overflow-y: auto; background: var(--bg);
            padding: 0.4rem max(calc(var(--gut) + env(safe-area-inset-right)), calc(50% - 25rem))
                     calc(0.5rem + env(safe-area-inset-bottom)) max(calc(var(--gut) + env(safe-area-inset-left)), calc(50% - 25rem)); }
html.kb #composer { padding-bottom: 0.5rem; }
/* A wide screen keeps the sidebar and gives the page all the width beside it, lined up under the header. The
   shell's script tests the same width. */
@media (min-width: 48rem) {
  #side { transform: none; }
  #menu, #scrim { display: none; }
  #pane { margin-left: 17rem; }
  #top { padding-left: 1.5rem; padding-right: 1.5rem; }
  main { max-width: none; margin: 0; padding: 0.25rem 1.5rem 1.5rem; }
  html.convo main { padding: 0.25rem 1.5rem 0.6rem; }
  #composer { padding: 0.4rem 1.5rem 0.75rem; }
  .form, .cfg { max-width: 60rem; }
}
#composer #chat-status { margin: 0 0.2rem 0.4rem; }
.pill-anchor { position: relative; flex: none; height: 0; z-index: 5; }
#new-pill { position: absolute; bottom: 0.6rem; left: 50%; transform: translateX(-50%); white-space: nowrap;
            border: 0; border-radius: 999px; padding: 0.4rem 0.95rem; font-size: 0.85rem; font-weight: 600;
            background: var(--accent); color: var(--accent-fg); box-shadow: var(--shadow); }

/* The composer */
.page .recorder { margin-top: 0.8rem; }
.recorder { border: 1px solid var(--line); border-radius: 12px; background: var(--card); box-shadow: var(--shadow);
            padding: 0.3rem 0.45rem 0.45rem; transition: border-color 0.15s; }
.recorder:focus-within { border-color: color-mix(in srgb, var(--accent) 60%, var(--line)); }
.composer-label { font-size: 0.8rem; font-weight: 700; color: var(--muted); margin: 0.2rem 0.35rem 0; }
#composer .composer-label { display: none; }
.replying { display: flex; align-items: center; gap: 0.5rem; font-size: 0.8rem; color: var(--muted);
            padding: 0.15rem 0.1rem 0.25rem 0.4rem; border-bottom: 1px solid var(--line); }
.replying span { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                 border-left: 3px solid var(--accent); padding-left: 0.55rem; color: var(--fg); }
.note { display: block; width: 100%; border: 0; outline: 0; resize: none; background: transparent; color: inherit;
        font: inherit; font-size: 16px; line-height: 1.4; padding: 0.45rem 0.35rem; min-height: 2.6rem; max-height: 40vh; }
.takes { display: flex; flex-direction: column; gap: 0.1rem; padding: 0 0.35rem; max-height: 30vh; overflow-y: auto; }
.takes .meta { font-size: 0.75rem; }
.takes audio { margin: 0.1rem 0 0.2rem; }
.attached { display: flex; flex-wrap: wrap; gap: 0.3rem; padding: 0 0.35rem 0.2rem; }
.takes:empty, .attached:empty { display: none; }
.chip { display: inline-flex; align-items: center; background: var(--chip); border-radius: 999px;
        padding: 0.1rem 0.2rem 0.1rem 0.65rem; font-size: 0.82rem; }
.chip button { border: 0; background: transparent; color: var(--muted); padding: 0 0.4rem; font-size: 0.85rem; }
.bar { display: flex; align-items: center; gap: 0.35rem; margin-top: 0.1rem; }
.bar .grow { flex: 1; }
.fmt { display: flex; flex-wrap: wrap; gap: 0.1rem; padding: 0.1rem 0.1rem 0.2rem; border-bottom: 1px solid var(--line); }
.fmt .tool { min-width: 2rem; justify-content: center; font-size: 0.82rem; }
.fmt .b { font-weight: 850; }
.fmt .i { font-style: italic; font-family: Georgia, serif; }
.fmt .s { text-decoration: line-through; }
.fmt .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.76rem; }
.btn.fmt-toggle[aria-pressed="true"], .btn.raw[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.recorder.dropping { outline: 2px dashed var(--accent); outline-offset: 2px; }
.tool.paste-code { background: var(--chip); color: var(--accent); margin: 0 0.2rem 0.25rem; }
.record.live { background: var(--red); border-color: var(--red); color: #fff; }
.record.live::before { content: ""; width: 0.55rem; height: 0.55rem; border-radius: 50%; background: #fff;
                       animation: pulse 1.2s ease-in-out infinite; }
.rec-status { margin: 0.3rem 0.35rem 0; font-size: 0.8rem; color: var(--muted); }
.rec-status:empty { display: none; }
.rec-status.error { color: var(--red); }

/* A message's menu: reactions, then what the owner can do with it */
#act-sheet { position: fixed; inset: 0; z-index: 50; background: #0006; display: flex; align-items: flex-end;
                  justify-content: center; animation: fade 0.15s; }
.sheet { width: 100%; max-width: 26rem; background: var(--card); border-radius: 18px 18px 0 0; box-shadow: var(--shadow);
         padding: 1rem 1rem calc(1rem + env(safe-area-inset-bottom)); animation: rise 0.24s cubic-bezier(0.2, 0.8, 0.2, 1); }
@media (min-width: 40rem) { #act-sheet { align-items: center; } .sheet { border-radius: 18px; } }
/* With a mouse the menu is a popup at the pointer, and a hovered message offers its quick actions. */
#act-sheet.pop { display: block; background: none; animation: none; }
#act-sheet.pop .sheet { position: fixed; width: 19rem; max-height: calc(100% - 16px); overflow-y: auto;
  border-radius: 12px; padding: 0.6rem 0.7rem; animation: fade 0.1s;
  box-shadow: 0 10px 30px #0003, 0 0 0 1px var(--line); }
#act-sheet.pop .row { display: none; }
#act-sheet.pop .emojis button { font-size: 1.1rem; padding: 0.25rem 0; }
#act-sheet.pop .actions > a, #act-sheet.pop .actions > button { padding: 0.45rem 0.3rem; font-size: 0.9rem; }
#act-sheet.pop .actions > a:hover, #act-sheet.pop .actions > button:hover { background: var(--hover); }
#msg-tools { position: fixed; z-index: 15; display: flex; gap: 0.1rem; padding: 0.15rem; background: var(--card);
             border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); }
#msg-tools button { border: 0; background: none; border-radius: 6px; padding: 0.15rem 0.45rem; font-size: 0.95rem;
                    color: var(--muted); }
#msg-tools button:hover { background: var(--hover); color: var(--fg); }
.sheet .quoted { margin: 0 0 0.7rem; font-size: 0.85rem; color: var(--muted); white-space: nowrap; overflow: hidden;
                 text-overflow: ellipsis; border-left: 3px solid var(--accent); padding-left: 0.55rem; }
.emojis { display: grid; grid-template-columns: repeat(9, 1fr); gap: 0.3rem; margin-bottom: 0.5rem; }
.emojis button { font-size: 1.35rem; border: 0; background: var(--chip); border-radius: 10px; padding: 0.4rem 0;
                 color: var(--muted); transition: transform 0.1s; }
.sheet .actions > a, .sheet .actions > button { display: flex; width: 100%; border: 0;
  border-top: 1px solid var(--line); background: none; color: var(--fg); font: inherit; font-size: 1rem;
  text-align: left; text-decoration: none; padding: 0.75rem 0.3rem; }
.sheet .actions > .danger { color: var(--red); }
.sheet .facts { margin: 0; border-top: 1px solid var(--line); padding: 0.6rem 0.3rem 0; color: var(--muted);
                font-size: 0.8rem; }
.emojis button:active { transform: scale(0.88); }
.sheet .row { display: flex; gap: 0.5rem; margin-top: 0.7rem; }
.sheet .row .btn { flex: 1; }

/* Phone */
@media (max-width: 40rem) {
  :root { --gut: 0.7rem; }
  main { padding-left: calc(0.7rem + env(safe-area-inset-left)); padding-right: calc(0.7rem + env(safe-area-inset-right)); }
  .msg { grid-template-columns: 2.1rem minmax(0, 1fr); gap: 0 0.55rem; padding: 0.4rem 0.4rem; margin: 0 -0.4rem; }
  .msg > .gutter > .avatar { width: 2.1rem; height: 2.1rem; font-size: 0.75rem; border-radius: 8px; }
  .msg:hover { background: transparent; }
  .gutter .t { display: none; }
  /* The status gets two lines; its age stays in view at the end of them. */
  .titles .status { display: flex; align-items: flex-end; gap: 0.3rem; }
  .titles .status .st-text { flex: 1; min-width: 0; display: -webkit-box; -webkit-box-orient: vertical;
                             -webkit-line-clamp: 2; overflow: hidden; }
  .titles .status .ago { flex: none; }
}

/* Motion */
.fresh { animation: fresh 0.45s ease-out; }
@keyframes fresh { from { opacity: 0; transform: translateY(8px); } }
@keyframes flash { from { background: var(--accent-soft); } }
@keyframes rise { from { opacity: 0; transform: translateY(24px); } }
@keyframes fade { from { opacity: 0; } }
@keyframes pulse { 50% { opacity: 0.25; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
"""

# The drawer, folding groups, and a new channel.
SHELL_SCRIPT = """
<script>
(() => {
  const side = document.getElementById("side");
  const menu = document.getElementById("menu");
  const setOpen = (open) => {
    document.body.classList.toggle("drawer", open);
    menu.setAttribute("aria-expanded", String(open));
  };
  const scrim = document.getElementById("scrim");
  menu.onclick = () => setOpen(!document.body.classList.contains("drawer"));
  scrim.onclick = () => setOpen(false);
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") setOpen(false); });
  // Shortcuts: / searches; g then w, u, m, n or s goes to Waiting, Unread, Messages, Notifications or Search.
  const GO = { w: "/waiting", u: "/unread", m: "/", n: "/notifications", s: "/search" };
  let goAt = 0;
  document.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.isComposing || ev.repeat) return;
    if (ev.target.closest("audio, input, textarea, select, button, [contenteditable], #act-sheet")) return;
    if (ev.key === "/") { ev.preventDefault(); location.href = "/search"; return; }
    if (goAt && ev.timeStamp - goAt < 1000 && GO[ev.key]) { goAt = 0; location.href = GO[ev.key]; return; }
    goAt = ev.key === "g" ? ev.timeStamp : 0;
  });
  // On a phone the drawer follows a finger: swiped right it comes out, left it goes away. Let go past halfway, or
  // flick, and it goes the rest of the way; otherwise it springs back.
  const wide = window.matchMedia("(min-width: 48rem)");  // the sidebar stays: as in the style
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  let drag = null;
  const place = (x) => {
    side.style.transform = `translateX(${x}px)`;
    scrim.style.opacity = String(1 + x / drag.w);
  };
  const settle = (open) => {
    document.body.classList.remove("dragging");
    side.style.transform = scrim.style.opacity = "";
    setOpen(open);
  };
  document.addEventListener("touchstart", (ev) => {
    if (drag && drag.axis === "x") settle(drag.open);  // a second finger
    drag = null;
    if (wide.matches || ev.touches.length !== 1) return;
    if (ev.target.closest("audio, input, textarea, select, [contenteditable], #act-sheet")) return;
    const t = ev.touches[0];
    if (!standalone && t.clientX < 24) return;  // Safari's own swipe back
    drag = { x: t.clientX, y: t.clientY, open: document.body.classList.contains("drawer"), w: side.offsetWidth,
             axis: null, at: 0, trail: [] };
    drag.at = drag.open ? 0 : -drag.w;
    drag.trail.push([ev.timeStamp, drag.at]);
  }, { passive: true });
  document.addEventListener("touchmove", (ev) => {
    if (!drag) return;
    const t = ev.touches[0];
    const dx = t.clientX - drag.x, dy = t.clientY - drag.y;
    if (drag.axis === null) {
      if (Math.hypot(dx, dy) < 10) return;
      // Sideways the way the drawer can go is the drawer's; anything else is the page's.
      drag.axis = Math.abs(dx) > 1.5 * Math.abs(dy) && (drag.open ? dx < 0 : dx > 0) ? "x" : "y";
      if (drag.axis === "x") document.body.classList.add("dragging");
    }
    if (drag.axis !== "x") return;
    ev.preventDefault();
    const at = Math.min(0, Math.max(-drag.w, drag.open ? dx : dx - drag.w));
    drag.at = at;
    drag.trail.push([ev.timeStamp, at]);
    place(at);
  }, { passive: false });
  document.addEventListener("touchend", (ev) => {
    if (!drag || drag.axis !== "x") { drag = null; return; }
    // Its speed over the last 100 ms: a finger held still before letting go didn't flick.
    const [t0, at0] = drag.trail.findLast(([t]) => t <= ev.timeStamp - 100) ?? drag.trail[0];
    const v = (drag.at - at0) / Math.max(1, ev.timeStamp - t0);
    const flick = Math.abs(v) > 0.4 ? v > 0 : null;  // px per ms
    settle(flick ?? drag.at > -drag.w / 2);
    drag = null;
  });
  document.addEventListener("touchcancel", () => {
    if (drag && drag.axis === "x") settle(drag.open);
    drag = null;
  });
  // A sender's row is Messages filtered to it: /?from=<sender>.
  window.nanoteaCurrent = (root) => root.querySelectorAll("a").forEach((a) => {
    const u = new URL(a.href);
    if (u.pathname === location.pathname
        && u.searchParams.get("from") === new URLSearchParams(location.search).get("from")) {
      a.setAttribute("aria-current", "page");
    }
  });
  window.nanoteaCurrent(side);
  // Which groups are folded is this device's, kept in a cookie so the server draws them folded (FOLDED).
  const folds = () => {
    const c = document.cookie.split("; ").find((x) => x.startsWith("nanotea-folds="));
    return new Set(c ? JSON.parse(decodeURIComponent(c.slice("nanotea-folds=".length))) : []);
  };
  const keepFolds = (folded) => {
    // Escaped past encodeURIComponent: the server's cookie parser takes none of !'()*.
    const v = encodeURIComponent(JSON.stringify([...folded]))
      .replace(/[!'()*]/g, (ch) => `%${ch.charCodeAt(0).toString(16).toUpperCase()}`);
    document.cookie = `nanotea-folds=${v}; Path=/; Max-Age=315360000; SameSite=Lax`
      + (location.protocol === "https:" ? "; Secure" : "");
  };
  // After the sidebar is patched: a fold made while its refresh was on the way.
  window.nanoteaFolds = () => {
    const folded = folds();
    side.querySelectorAll(".grp").forEach((g) => {
      g.classList.toggle("folded", folded.has(g.dataset.grp));
      g.querySelector(".fold").setAttribute("aria-expanded", String(!folded.has(g.dataset.grp)));
    });
  };
  // Folds kept in localStorage before the cookie.
  const before = localStorage.getItem("nanotea-folds");
  if (before !== null) {
    keepFolds(new Set([...folds(), ...JSON.parse(before)]));
    localStorage.removeItem("nanotea-folds");
    window.nanoteaFolds();
  }
  side.addEventListener("click", (ev) => {
    const fold = ev.target.closest(".fold");
    if (!fold) return;
    const name = fold.closest(".grp").dataset.grp;
    const folded = folds();
    if (folded.has(name)) folded.delete(name); else folded.add(name);
    keepFolds(folded);
    window.nanoteaFolds();
  });
  // Ages, from their time: the server's version leaves them out, so they tick here. As _age writes them.
  const age = (el) => {
    const s = Math.max(0, (Date.now() - Date.parse(el.dataset.at)) / 1000);
    const d = Math.floor(s / 86400);
    let text;
    if (el.dataset.age === "seen") {
      text = s < 60 ? "now" : s < 3600 ? `${Math.floor(s / 60)}m` : d < 1 ? `${Math.floor(s / 3600)}h`
        : d < 7 ? `${d}d` : d < 30 ? `${Math.floor(d / 7)}w` : d < 365 ? `${Math.floor(d / 30)}mo`
        : `${Math.floor(d / 365)}y`;
    } else {
      const n = s < 60 ? "" : s < 3600 ? `${Math.floor(s / 60)}m` : s < 86400 ? `${Math.floor(s / 3600)}h` : `${d}d`;
      text = el.dataset.age === "ago" ? (n ? `${n} ago` : "just now") : (n || "now");
    }
    if (el.textContent !== text) el.textContent = text;
  };
  const ages = () => document.querySelectorAll("[data-at]").forEach(age);
  setInterval(ages, 30000);
  document.addEventListener("thread-patched", ages);
  document.getElementById("new-channel").onclick = async () => {
    const name = (prompt("New channel name: lowercase letters, digits and dashes") || "").trim().replace(/^#/, "");
    if (!name) return;
    try {
      const r = await fetch("/api/channels", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      location.href = `/c/${out.name}`;
    } catch (err) {
      alert(`Couldn't make the channel: ${err.message}`);
    }
  };
})();
</script>
"""

# Delete buttons, reactions, the app badge, and turning on notifications (iPhone: from the Home
# Screen app).
APP_SCRIPT = """
<script>
(() => {
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-delete]");
    if (!btn) return;
    if (!confirm("Delete this message from the app? Its files are kept in deleted/.")) return;
    btn.disabled = true;
    try {
      const r = await fetch(`/api/messages/${btn.dataset.delete}/delete`,
                            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      if (location.pathname.startsWith("/m/")) location.href = "/";
      else if (document.getElementById("list")) unlist(document.getElementById(`m-${btn.dataset.delete}`));
      else location.reload();
    } catch (err) {
      btn.disabled = false;
      alert(`Couldn't delete: ${err.message}`);
    }
  });

  // On Messages, a deleted message leaves the list where it stands.
  function unlist(article) {
    const wasNew = Boolean(article.querySelector(".tag.new"));
    const before = article.previousElementSibling, after = article.nextElementSibling;
    article.remove();
    if (before && before.classList.contains("day") && (!after || after.classList.contains("day"))) before.remove();
    const sub = document.getElementById("list-sub");
    const n = Number(sub.dataset.n) - 1, fresh = Number(sub.dataset.new) - (wasNew ? 1 : 0);
    Object.assign(sub.dataset, { n, new: fresh });
    sub.textContent = `${n} message${n === 1 ? "" : "s"}` + (fresh ? `, ${fresh} new` : "");
  }

  // Messages loads a page at a time, older ones as the owner nears the end.
  const more = document.getElementById("more");
  if (more) {
    const list = document.getElementById("list");
    let busy = false;
    const near = () => more.isConnected && more.getBoundingClientRect().top < innerHeight + 1200;
    async function next() {
      if (busy) return;
      busy = true;
      try {
        const r = await fetch(more.dataset.url, { cache: "no-store" });
        const out = await r.json();
        if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
        const tpl = document.createElement("template");
        tpl.innerHTML = out.html;
        const rows = [...tpl.content.children];
        const lastDay = [...list.children].filter((el) => el.classList.contains("day")).at(-1);
        if (rows[0]?.classList.contains("day") && lastDay?.textContent === rows[0].textContent) rows.shift();
        list.append(...rows);
        more.classList.remove("failed");
        more.textContent = "Loading older messages...";
        if (out.more) more.dataset.url = out.url; else more.remove();
        document.dispatchEvent(new CustomEvent("thread-patched", { detail: { thread: list } }));
      } catch (err) {
        more.classList.add("failed");
        more.textContent = `Couldn't load older messages: ${err.message}. Tap to try again.`;
        return;
      } finally {
        busy = false;
      }
      if (near()) next();
    }
    more.onclick = () => { if (more.classList.contains("failed")) next(); };
    new IntersectionObserver((seen) => { if (seen.some((x) => x.isIntersecting)) next(); },
                             { rootMargin: "0px 0px 1200px 0px" }).observe(more);
    // The observer can miss a fast scroll in the same frame as a load.
    addEventListener("scroll", () => { if (!busy && near() && !more.classList.contains("failed")) next(); },
                     { passive: true });
  }

  // A message's menu, with reactions on top where the owner can react. A finger opens it by tapping the message, as
  // a sheet. A mouse opens it as a popup, by right-clicking the message or from the actions shown on hover, and a
  // plain click leaves the message alone. Taps on what the message holds (links, players, controls) and selecting
  // its text do what they always do.
  const sheet = document.getElementById("act-sheet");
  const box = sheet.querySelector(".sheet");
  const actions = sheet.querySelector(".actions");
  let target = null, picked = null;
  const closeSheet = () => {
    sheet.hidden = true;
    target = null;
    picked?.classList.remove("picked");
    picked = null;
  };
  const menuOf = (art) => art.querySelector(":scope > .body > template.menu");
  const OWN_TAPS = "a, button, input, textarea, select, label, audio, video, summary, pre, img, .control, .recorder, "
                   + ".answer, .quote, .replies";
  // at: where a mouse opened it, {x, y}; right: x is the popup's right edge. Without at, the sheet.
  const openMenu = (art, at) => {
    const tpl = menuOf(art);
    hideTools();
    target = tpl.dataset.react || null;
    sheet.querySelector(".quoted").textContent = tpl.dataset.label;
    sheet.querySelector(".emojis").hidden = !target;
    actions.replaceChildren(tpl.content.cloneNode(true));
    picked = art;
    art.classList.add("picked");
    sheet.classList.toggle("pop", Boolean(at));
    box.style.left = box.style.top = "";
    sheet.hidden = false;
    if (!at) return;
    const w = box.offsetWidth, h = box.offsetHeight;
    box.style.left = `${Math.max(8, Math.min(at.right ? at.x - w : at.x, innerWidth - w - 8))}px`;
    box.style.top = `${Math.max(8, at.y + h + 8 > innerHeight ? at.y - h : at.y)}px`;
  };
  let pointer = "";  // the kind of the last pointer pressed: mouse, pen or touch
  let swiped = false;  // a swipe ends in a click that must not open the menu
  document.addEventListener("click", (ev) => {
    if (swiped) { swiped = false; return; }
    if (pointer === "mouse") return;
    const art = ev.target.closest("article.msg");
    if (!art || ev.target.closest(OWN_TAPS) || String(getSelection()) || !menuOf(art)) return;
    openMenu(art, null);
  });
  document.addEventListener("contextmenu", (ev) => {
    const art = ev.target.closest("article.msg");
    if (pointer !== "mouse" || !art || ev.target.closest(OWN_TAPS) || String(getSelection()) || !menuOf(art)) return;
    ev.preventDefault();
    openMenu(art, { x: ev.clientX, y: ev.clientY });
  });
  // An action closes the menu; its own handler, on the document, runs after.
  actions.addEventListener("click", (ev) => { if (ev.target.closest("a, button")) closeSheet(); });

  // The hovered message's quick actions: the first reactions, Reply where there is one, and the whole menu.
  const tools = Object.assign(document.createElement("div"), { id: "msg-tools", hidden: true });
  tools.setAttribute("role", "toolbar");
  document.body.append(tools);
  let toolsFor = null;
  function hideTools() { tools.hidden = true; toolsFor = null; }
  const showTools = (art) => {
    const tpl = menuOf(art);
    const id = tpl.dataset.react;
    const reply = tpl.content.querySelector("[data-reply-to]")?.cloneNode(true);
    if (reply) reply.title = reply.textContent;
    if (reply) reply.textContent = "Reply";
    const more = Object.assign(document.createElement("button"), { type: "button", textContent: "\u22ef",
                                                                    title: "More" });
    more.setAttribute("aria-label", "More");
    more.dataset.more = "";
    const quick = id ? [...sheet.querySelectorAll(".emojis [data-emoji]")].slice(0, 4).map((b) => b.cloneNode(true))
                     : [];
    tools.dataset.id = id || "";
    tools.replaceChildren(...quick, ...(reply ? [reply] : []), more);
    toolsFor = art;
    tools.hidden = false;
    const r = art.getBoundingClientRect();
    tools.style.top = `${Math.max(4, r.top - tools.offsetHeight / 2)}px`;
    tools.style.left = `${r.right - tools.offsetWidth - 12}px`;
  };
  document.addEventListener("pointerover", (ev) => {
    if (ev.pointerType !== "mouse" || ev.target.closest("#msg-tools")) return;
    const art = ev.target.closest("article.msg");
    if (art === toolsFor) return;
    if (art && sheet.hidden && menuOf(art)) showTools(art); else hideTools();
  });
  addEventListener("scroll", hideTools, { capture: true, passive: true });
  tools.addEventListener("click", (ev) => {
    const more = ev.target.closest("[data-more]");
    if (!more || !toolsFor) return;
    const r = more.getBoundingClientRect();
    openMenu(toolsFor, { x: r.right, y: r.bottom + 4, right: true });
  });

  // Swipe a message left to reply to it, in a conversation. Right is the drawer's.
  let swipe = null;
  document.addEventListener("pointerdown", (ev) => {
    pointer = ev.pointerType;
    swiped = false;
    swipe = null;
    if (ev.pointerType !== "touch" || !document.getElementById("composer")) return;
    const art = ev.target.closest("article.msg");
    const btn = art && !ev.target.closest(OWN_TAPS) && menuOf(art)?.content.querySelector("[data-reply-to]");
    if (btn) swipe = { art, btn, x: ev.clientX, y: ev.clientY, dx: 0, on: false };
  });
  document.addEventListener("pointermove", (ev) => {
    if (!swipe) return;
    const dx = ev.clientX - swipe.x, dy = ev.clientY - swipe.y;
    if (!swipe.on) {
      if (Math.abs(dy) > 12 || dx > 12) { swipe = null; return; }
      if (dx > -12) return;
      swipe.on = true;
      swipe.art.classList.add("swiping");
    }
    swipe.dx = Math.min(0, Math.max(dx, -90));
    swipe.art.style.transform = `translateX(${swipe.dx}px)`;
  });
  const endSwipe = (ev) => {
    if (!swipe?.on) { swipe = null; return; }
    const { art, btn, dx } = swipe;
    swipe = null;
    swiped = true;
    art.classList.remove("swiping");
    art.style.transform = "";
    if (ev.type === "pointerup" && dx <= -64) {
      document.dispatchEvent(new CustomEvent("reply-to",
                                             { detail: { id: btn.dataset.replyTo, label: btn.dataset.replyLabel } }));
    }
  };
  document.addEventListener("pointerup", endSwipe);
  document.addEventListener("pointercancel", endSwipe);

  // Emoji reactions: a pill toggles one, the menu adds one. On a question still waiting, a reaction is the
  // answer, so the page updates to show it.
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !sheet.hidden) closeSheet(); });
  document.addEventListener("click", async (ev) => {
    if (ev.target === sheet || ev.target.closest("[data-close-sheet]")) { closeSheet(); return; }
    const choice = ev.target.closest("[data-emoji]");
    const pill = ev.target.closest("[data-react]");
    if (!choice && !pill) return;
    let id, emoji, on;
    if (choice) {
      id = choice.closest("#msg-tools")?.dataset.id || target;
      closeSheet();
      emoji = choice.dataset.emoji === "+" ? (prompt("React with any emoji") || "").trim() : choice.dataset.emoji;
      on = true;
    } else {
      ({ id, react: emoji } = pill.dataset);
      on = !pill.classList.contains("on");
    }
    if (!emoji || !id) return;
    const bar = document.querySelector(`.reacts[data-for="${id}"]`);
    let el = [...bar.querySelectorAll("[data-react]")].find((b) => b.dataset.react === emoji);
    if (el) el.disabled = true;
    try {
      const r = await fetch(`/api/messages/${id}/react`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ emoji, on }) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      if (out.sent === "answer") {
        if (window.liveThread) await window.liveThread.refresh(); else location.reload();
        return;
      }
      if (!on) { el.remove(); return; }
      if (!el) {
        el = Object.assign(document.createElement("button"), { type: "button", className: "pill fresh", textContent: emoji });
        Object.assign(el.dataset, { react: emoji, id });
        bar.append(el);
      }
      el.classList.add("on");
    } catch (err) {
      alert(`Couldn't react: ${err.message}`);
    } finally {
      if (el) el.disabled = false;
    }
  });

  // Heard: played at least 90% of an agent's message.
  const tracked = new WeakSet();
  const trackHeard = (player) => {
    if (tracked.has(player)) return;
    tracked.add(player);
    let sent = Boolean(player.dataset.heardAlready);
    player.addEventListener("timeupdate", async () => {
      if (sent || !player.duration || player.currentTime / player.duration < 0.9) return;
      sent = true;
      try {
        const r = await fetch(player.dataset.heard, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const tag = document.getElementById(`heard-${player.dataset.id}`);
        if (tag) tag.hidden = false;
      } catch (err) {
        sent = false;
        console.error("marking heard failed", err);
      }
    });
  };
  document.querySelectorAll("audio[data-heard]").forEach(trackHeard);
  document.addEventListener("thread-patched",
                            (ev) => ev.detail.thread.querySelectorAll("audio[data-heard]").forEach(trackHeard));

  // Speak: voice a message on request. The player starts inside the tap, which iOS requires; the server
  // voices the message when the player first asks for it.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-speak]");
    if (!btn) return;
    const { speak: id, title, artist } = btn.dataset;
    const player = Object.assign(document.createElement("audio"), { controls: true, preload: "auto" });
    Object.assign(player.dataset, { id, title, artist, heard: `/api/messages/${id}/heard` });
    player.src = `/audio/${id}`;
    const note = Object.assign(document.createElement("div"), { className: "meta", textContent: "Voicing..." });
    const article = btn.closest("article");
    article.querySelector(".subject").after(player, note);
    btn.remove();
    trackHeard(player);
    player.addEventListener("playing", () => note.remove(), { once: true });
    player.addEventListener("error", async () => {
      const r = await fetch(`/audio/${id}`);
      const out = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
      note.className = "error";
      note.textContent = out.error || `HTTP ${r.status}`;
    }, { once: true });
    player.play().catch((err) => { note.textContent = `Tap play: ${err.message}`; });
  });

  // A clip named in the text plays, or pauses, in its player under the text.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-clip]");
    if (!btn) return;
    const player = btn.closest("article").querySelector(`audio[data-n="${btn.dataset.clip}"]`);
    player.closest("details").open = true;
    if (!player.paused) { player.pause(); return; }
    player.play().catch((err) => {
      const note = Object.assign(document.createElement("div"), { className: "error",
                                                                  textContent: `Couldn't play: ${err.message}` });
      player.after(note);
    });
  });
  for (const kind of ["play", "pause", "ended"]) {
    document.addEventListener(kind, (ev) => {
      const n = ev.target.dataset?.n;
      if (!n) return;
      const btn = ev.target.closest("article")?.querySelector(`[data-clip="${n}"]`);
      if (btn) btn.classList.toggle("on", kind === "play");
    }, true);
  }

  // Copy in a message's menu: the owner's words as sent, or an agent's original, not its rewrite.
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-copy-text], [data-copy-original]");
    if (!btn) return;
    const { copyText, copyOriginal } = btn.dataset;
    const text = copyText ?? document.querySelector(`#m-${copyOriginal} details > pre`).textContent;
    try {
      await navigator.clipboard.writeText(text);
    } catch (err) {
      alert(`Couldn't copy: ${err.message}`);
    }
  });

  // A code block's Copy button copies the code as written.
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-copy]");
    if (!btn) return;
    clearTimeout(btn.reset);
    try {
      await navigator.clipboard.writeText(btn.closest(".code").querySelector("code").textContent);
      btn.textContent = "Copied";
    } catch (err) {
      btn.textContent = `Copy failed: ${err.message}`;
    }
    btn.reset = setTimeout(() => { btn.textContent = "Copy"; }, 1500);
  });

  const link = (href, text) => Object.assign(document.createElement("a"), { href, textContent: text });

  // Unsent recordings this device kept for other pages.
  if (window.indexedDB) {
    const req = indexedDB.open("nanotea", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("chunks");
    req.onerror = () => console.error("checking for unsent recordings failed", req.error);
    req.onsuccess = () => {
      const keys = req.result.transaction("chunks", "readonly").objectStore("chunks").getAllKeys();
      keys.onerror = () => console.error("checking for unsent recordings failed", keys.error);
      keys.onsuccess = () => {
        const pages = [...new Set(keys.result.map((key) => String(key).split("|")[0]))]
          .filter((page) => page !== location.pathname);
        if (!pages.length) return;
        const notice = document.getElementById("unsent");
        notice.replaceChildren("Unsent recording kept on this device: ",
                               ...pages.flatMap((page, i) => (i ? [" · ", link(page, page)] : [link(page, page)])));
        notice.hidden = false;
      };
    };
  }

  // A push: bring this conversation up to date now. A tapped notification: show it here if it's this
  // conversation; otherwise go there, unless that would abandon a recording or half-typed message.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", async (ev) => {
      if (!ev.data) return;
      if (ev.data.type === "update") {
        if (window.liveThread) await window.liveThread.refresh();
        if (ev.ports[0]) ev.ports[0].postMessage("updated");  // the notification waits for this
        return;
      }
      if (ev.data.type !== "open") return;
      const url = new URL(ev.data.url, location.href);
      if (window.liveThread && url.pathname === location.pathname) {
        window.liveThread.show(url);
      } else if (window.composing && window.composing()) {
        const notice = document.getElementById("elsewhere");
        notice.replaceChildren("A notification is waiting: ", link(ev.data.url, "open it"), " when you're done here.");
        notice.hidden = false;
      } else {
        location.href = ev.data.url;
      }
    });
  }

  // The tab and the Home Screen badge: every message not yet seen, kept current by a conversation's poll.
  const titled = document.title.replace(/^\\(\\d+\\) /, "");
  let shown = null;
  window.nanoteaUnread = (n) => {
    if (n === shown) return;
    shown = n;
    document.title = (n ? `(${n}) ` : "") + titled;
    if (n && "setAppBadge" in navigator) navigator.setAppBadge(n).catch((err) => console.error("setAppBadge failed", err));
    else if (!n && "clearAppBadge" in navigator) navigator.clearAppBadge().catch((err) => console.error("clearAppBadge failed", err));
  };
  window.nanoteaUnread(Number(document.body.dataset.unread));
  const button = document.getElementById("notify-on");
  const hint = document.getElementById("notify-hint");
  const say = (text) => { hint.textContent = text; };
  if (!window.isSecureContext) {
    say("This page came over plain http, so recording, notifications and the Home Screen app are off. They need an "
        + "https address; nanotea's docs/running.md shows the ways.");
    return;
  }
  if (!document.body.dataset.vapid) {
    say(`Notifications are off: the service's config has no "push" in [notify] now.`);
    return;
  }
  if (!("serviceWorker" in navigator)) {
    say("This browser has no service workers, so notifications are off.");
    return;
  }
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  if (!("PushManager" in window)) {
    if (/iPhone|iPad|iPod/.test(navigator.userAgent) && !standalone) {
      say("For notifications: Share, Add to Home Screen, then open it from the Home Screen.");
    }
    return;
  }
  const keyBytes = (s) => {
    const raw = atob((s + "=".repeat((4 - (s.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  };
  async function subscribe(reg) {
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: keyBytes(document.body.dataset.vapid) });
    const r = await fetch("/api/push/subscribe", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sub) });
    const out = await r.json();
    if (!r.ok) throw new Error(out.error || r.statusText);
  }
  const registration = navigator.serviceWorker.register("/sw.js");
  registration.catch((err) => say(`Notifications unavailable: ${err.message}`));
  if (Notification.permission === "denied") {
    say("Notifications are blocked for this site in Settings.");
  } else if (Notification.permission === "granted") {
    registration.then(async (reg) => {
      if (!(await reg.pushManager.getSubscription())) await subscribe(reg);
    }).catch((err) => say(`Couldn't turn on notifications: ${err.message}`));
  } else {
    button.hidden = false;
    button.onclick = async () => {
      button.disabled = true;
      try {
        if ((await Notification.requestPermission()) !== "granted") throw new Error("permission not given");
        await subscribe(await registration);
        button.hidden = true;
        say("Notifications are on.");
      } catch (err) {
        button.disabled = false;
        say(`Couldn't turn on notifications: ${err.message}`);
      }
    };
  }
})();
</script>
"""

SITE = {"vapid_public_key": "", "key": "", "sidebar": dict, "names": dict, "app": {}, "presence": None, "controls": {},
        "look": None}
# The groups folded on the device being answered (its nanotea-folds cookie), so they arrive folded.
FOLDED: ContextVar[frozenset[str]] = ContextVar("FOLDED", default=frozenset())
# Inboxes: where each conversation lives, its API segment, and its name on screen.
CHAT_PATH = {"main": "/chat"}
API_SEGMENT = {"main": "inbox"}
BOX_LABEL = {"main": "main agent"}
DM_NAME = r"[a-z0-9][a-z0-9-]{0,39}"  # a direct line's name: its URL and API segment

MENU_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
             'aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>')
SPEAK_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
              'stroke-linejoin="round" aria-hidden="true"><path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/>'
              '<path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/></svg>')
CHECK_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" '
              'stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>')
PLAY_ICON = ('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l10.5-6.5z"/></svg>')
GROUP_GAP = timedelta(minutes=5)  # a sender's posts this close together share one header


def add_dm(box: str, label: str) -> None:
    """A direct line to one agent, made when it first claims an inbox by that name."""
    CHAT_PATH[box], API_SEGMENT[box], BOX_LABEL[box] = f"/chat/{box}", f"dm-{box}", label


def configure(vapid_public_key: str, key: str, sidebar, names, app: dict, presence, controls: dict, look,
              bang) -> None:
    """sidebar(): {"channels": [...], "groups": [{name, rows}], "unread": every message not yet seen, "talk":
    whether Agent talk is shown}, each row a dict of path, name, role, unread, listening, and typing (who is typing
    there); a group row also has who, for its hover, mark, the name its leaf grows from, status, the agent's
    {text, at} or None, responding, seen: for one not connected, when it was last seen, or None, and held: {at,
    what} while it is at a permission prompt. names(): sender -> first name. app: the [app] config table.
    presence(box): (state, words) for who is there to pick up what the owner sends on a line. controls: the control plugins by type. look: the owner's
    theme (themes.Look). bang(): whether the owner has bang commands on."""
    SITE.update(vapid_public_key=vapid_public_key, key=key, sidebar=sidebar, names=names, app=app,
                presence=presence, controls=controls, look=look, bang=bang)


def _bar_color(mode: str | None = None) -> str:
    """The browser's bar and the home screen app's splash: the page's background, in the mode the owner chose
    (light, when it follows the device and no mode is named)."""
    look = SITE["look"].get()
    return SITE["look"].colors()[mode or ("light" if look["appearance"] == "auto" else look["appearance"])]["bg"]


def _style() -> str:
    return STYLE.replace("__MARK__", quote(_app_name(), safe=""))


def _theme_metas() -> str:
    if SITE["look"].get()["appearance"] != "auto":
        return f'<meta name="theme-color" content="{_bar_color()}">'
    return "\n".join(f'<meta name="theme-color" content="{_bar_color(m)}" media="(prefers-color-scheme: {m})">'
                     for m in ("light", "dark"))


def _app_name() -> str:
    return SITE["app"]["name"]


def _owner() -> str:
    return SITE["app"]["owner"]


def _owner_avatar() -> str:
    return f'<span class="avatar g" aria-hidden="true">{e(_owner()[0].upper())}</span>'


def who(sender: str) -> str:
    """An agent as the owner sees it: "Oliver (builder)", or just the sender until it has a name."""
    name = SITE["names"]().get(sender)
    return f"{name} ({sender})" if name else sender


def _avatar(sender: str, size: int = 48) -> str:
    """An agent's leaf, seeded by its sender name (not its first name, which can change). size: the pixels it is
    drawn for, which sets how fine its veins go."""
    hue = int(hashlib.sha1(sender.encode()).hexdigest()[:4], 16) % 360
    src = f"/leaf/{quote(sender, safe='')}.svg?size={size}"
    return (f'<span class="avatar leaf" style="--tint: hsl({hue} 55% 45%); --src: url(&quot;{e(src)}&quot;)" '
            f'aria-hidden="true"><i></i></span>')


def _seg_of_folder(folder: str) -> str:
    """Where something the owner sent lives, as its URLs name it: an inbox's API segment, or "c/<channel>"."""
    if folder.startswith("channel-"):
        return f"c/{folder.removeprefix('channel-')}"
    return "inbox" if folder == "inbox" else f"dm-{folder.removeprefix('inbox-')}"


def _row(re_: dict) -> str:
    """The page row of the message a reply answers. Old replies say no kind: they answered an agent's message."""
    return ("g-" if re_.get("kind") == "owner" else "m-") + re_["id"]


def _re_line(re_: dict) -> str:
    """The message a reply answers, as the agent's `re` names it (sender, title, thread), linked where it shows in
    its thread. An answer to a reply says so: its thread is the first message's."""
    if re_.get("kind") == "owner":
        name, title = _owner(), re_["title"] or "a recording"
    else:
        name, title = who(re_["sender"]), re_["title"] or "message"
    key = thread_key(re_)
    more = f'<a class="in-thread" href="/t/{e(key)}">in thread</a>' if key != _row(re_) else ""
    return (f'<div class="quote"><a href="/t/{e(key)}#{_row(re_)}">Replying to {e(name)}: '
            f'<span>{e(title)}</span></a>{more}</div>')


def _replies(key: str, summary: dict | None, fresh: int = 0, row: str = "") -> str:
    """Under a thread's first message: how many replies, from whom, and the last one's time; opens the thread, at
    row if given. fresh: how many of them are new to the owner."""
    if not summary:
        return ""
    n = summary["n"]
    new = f'<span class="tag new">{fresh} new</span>' if fresh else ""
    faces = "".join(_avatar(a, 24) for a in summary["agents"][:4]) + (_owner_avatar() if summary["owner"] else "")
    last = datetime.fromtimestamp(summary["last"]).astimezone().isoformat(timespec="seconds")
    return (f'<a class="replies" href="/t/{e(key)}{"#" + row if row else ""}"><span class="faces">{faces}</span>'
            f'<b>{n} repl{"y" if n == 1 else "ies"}</b>{new}<span class="last">Last reply {_age_span(last, "ago")}</span>'
            f'<span class="open">View thread</span></a>')


def _age(iso: str, mode: str = "short") -> str:
    """As the page's script writes it from data-at. short: "now", "12m", "3h", "2d"; ago: "just now", "12m ago";
    seen: "12m", "3h", "2d", "3w", "4mo", "2y"."""
    s = max(0.0, (datetime.now().astimezone() - datetime.fromisoformat(iso)).total_seconds())
    d = s // 86400
    if mode == "seen":
        return ("now" if s < 60 else f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h" if d < 1
                else f"{int(d)}d" if d < 7 else f"{int(d // 7)}w" if d < 30 else f"{int(d // 30)}mo" if d < 365
                else f"{int(d // 365)}y")
    n = "" if s < 60 else f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h" if s < 86400 else f"{int(d)}d"
    return (f"{n} ago" if n else "just now") if mode == "ago" else (n or "now")


def ago(iso: str) -> str:
    return _age(iso, "ago")


def _age_span(iso: str, mode: str = "short", cls: str = "age") -> str:
    return f'<span class="{cls}" data-at="{e(iso)}" data-age="{mode}">{_age(iso, mode)}</span>'


def _sidebar(bar: dict) -> str:
    def row(c: dict, channel: bool) -> str:
        unread = f'<span class="count">{c["unread"]}</span>' if c["unread"] else ""
        pulse = bool(c["typing"]) or c.get("responding", False)
        held = c.get("held")
        why = ("held at a permission prompt" if held
               else f'{", ".join(who(a) for a in c["typing"])} typing' if c["typing"]
               else "working on your message" if pulse else "listening")
        dot = (f'<span class="dot{" held" if held else " pulse" if pulse else ""}" title="{e(why)}"></span>'
               if c["listening"] or pulse or held else "")
        cls = " unread" if c["unread"] else ""
        if channel:
            topic = f' title="{e(c["role"])}"' if c["role"] else ""
            return (f'<a class="conv ch{cls}" href="{e(c["path"])}"{topic}><span class="hash" aria-hidden="true">#</span>'
                    f'<span class="who">{e(c["name"])}</span>{dot}{unread}</a>')
        role = f'<span class="role">{e(c["role"])}</span>' if c["role"] else ""
        if held:
            role += f'<span class="stl held"><span class="st-text">held at a permission prompt</span>{_age_span(held["at"])}</span>'
        st = c["status"]
        if st:
            role += f'<span class="stl"><span class="st-text">{e(st["text"])}</span>{_age_span(st["at"])}</span>'
        seen, hover = "", c["who"]
        if c["seen"]:
            seen = _age_span(c["seen"], "seen", "seen")
            hover += f", last seen {_when(c['seen'])}"
        return (f'<a class="conv dm{cls}" href="{e(c["path"])}" title="{e(hover)}">'
                f'<span class="face">{_avatar(c["mark"], 24)}{dot}</span>'
                f'<span class="who"><span class="nm">{e(c["name"])}</span>{role}</span>{seen}{unread}</a>')

    def group(name: str, rows: str, unread: int, add: str = "") -> str:
        # Folded, a group shows its unread total.
        total = f'<span class="count">{unread}</span>' if unread else ""
        folded = name in FOLDED.get()
        return (f'<div class="grp{" folded" if folded else ""}" data-grp="{e(name)}"><div class="sect">'
                f'<button type="button" class="fold" aria-expanded="{str(not folded).lower()}">'
                f'<span class="caret" aria-hidden="true">'
                f'</span><span class="gname">{e(name)}</span>{total}</button>{add}</div>'
                f'<div class="rows">{rows}</div></div>')

    add = '<button type="button" class="add" id="new-channel" aria-label="New channel">+</button>'
    waiting = f'<span class="count">{bar["waiting"]}</span>' if bar["waiting"] else ""
    unread = f'<span class="count">{bar["unread"]}</span>' if bar["unread"] else ""
    return ('<aside id="side" aria-label="Conversations"><a class="brand" href="/">'
            '<span class="mark" aria-hidden="true"></span>' + e(_app_name()) + '</a>'
            '<a class="conv" href="/waiting"><span class="hash" aria-hidden="true">?</span>'
            f'<span class="who">Waiting on you</span>{waiting}</a>'
            '<a class="conv" href="/unread"><span class="hash" aria-hidden="true">&#9679;</span>'
            f'<span class="who">Unread</span>{unread}</a>'
            '<a class="conv" href="/search"><span class="hash" aria-hidden="true">&#8981;</span>'
            '<span class="who">Search</span></a>'
            '<a class="conv" href="/"><span class="hash" aria-hidden="true">&#9776;</span>'
            '<span class="who">Messages</span></a>'
            + group("Channels", "".join(row(c, True) for c in bar["channels"]),
                    sum(c["unread"] for c in bar["channels"]), add)
            + "".join(group(g["name"], "".join(row(c, False) for c in g["rows"]), sum(c["unread"] for c in g["rows"]))
                      for g in bar["groups"])
            + '<nav class="foot"><a class="conv" href="/notifications"><span class="who">Notifications</span></a>'
            + '<a class="conv" href="/rules"><span class="who">Rules</span></a>'
            + ('<a class="conv" href="/talk"><span class="who">Agent talk</span></a>' if bar.get("talk") else "")
            + '<a class="conv" href="/settings"><span class="who">Settings</span></a></nav>'
            + "</aside>")


def manifest() -> dict:
    # start_url carries the pairing key: a Home Screen app keeps cookies apart from Safari.
    return {
        "name": _app_name(),
        "short_name": _app_name(),
        "start_url": f"/?k={SITE['key']}",
        "scope": "/",
        "display": "standalone",
        "background_color": _bar_color(),
        "theme_color": _bar_color(),
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
        # Long-press on the icon, where the platform supports it (Android; iOS ignores them).
        "shortcuts": [{"name": name, "url": f"{path}?k={SITE['key']}"}
                      for name, path in (("Waiting on you", "/waiting"), ("Unread", "/unread"), ("Search", "/search"))],
    }

# Record, play back, then send. Addresses come from data attributes, never from string substitution
# into the script. Nothing unsent is lost: typed text stays in localStorage, and audio goes into
# IndexedDB one second at a time, so a screen lock, crash, or reload costs at most a second. A lock or
# a trip away from the app ends the recording cleanly (iPhone web apps can't record in the
# background); "Record more" continues it, and the recordings are joined when sent. In a thread, Reply
# on a message makes the next send answer it.
RECORDER = """
<section class="recorder" data-drafts="__DRAFTS__" data-send="__SEND__" data-re="__RE__" data-thread="__THREAD__"
         data-files="__FILES__" data-raw="__RAW__" data-bang="__BANG__" data-to="__TO__">
<div class="composer-label">__HEADING__</div>
<p class="bang-hint" hidden></p>
<div class="replying" hidden><span></span><button type="button" class="tool cancel-reply">Cancel</button></div>
<div class="fmt" role="toolbar" aria-label="Formatting" hidden>
<button type="button" class="tool b" data-fmt="bold" title="Bold (Cmd+B)" aria-label="Bold">B</button>
<button type="button" class="tool i" data-fmt="italic" title="Italic (Cmd+I)" aria-label="Italic">I</button>
<button type="button" class="tool s" data-fmt="strike" title="Strikethrough (Cmd+Shift+X)" aria-label="Strikethrough">S</button>
<button type="button" class="tool mono" data-fmt="code" title="Code (Cmd+Shift+C)">Code</button>
<button type="button" class="tool mono" data-fmt="block" title="Code block (Cmd+Alt+Shift+C)">Block</button>
<button type="button" class="tool" data-fmt="quote" title="Quote (Cmd+Shift+9)">Quote</button>
<button type="button" class="tool" data-fmt="bullet" title="Bulleted list (Cmd+Shift+8)">List</button>
<button type="button" class="tool" data-fmt="number" title="Numbered list (Cmd+Shift+7)">1. 2.</button>
</div>
<textarea class="note" rows="1" placeholder="__HEADING__" aria-label="__HEADING__"></textarea>
<button type="button" class="tool paste-code" hidden>Format the paste as code</button>
<div class="takes"></div>
<div class="attached"></div>
<div class="bar"><button type="button" class="btn small record" disabled>Record</button>
<button type="button" class="btn small raw" aria-pressed="false" title="Raw: records without echo cancelling, noise suppression or level control, in stereo where the mic has it, at a higher bitrate">Raw</button>
<button type="button" class="btn small attach">Attach</button>
<button type="button" class="btn small fmt-toggle" aria-pressed="false" title="Formatting" aria-label="Formatting">Aa</button>
<button type="button" class="btn small discard" hidden>Discard recording</button>
<span class="grow"></span><button type="button" class="btn primary send">Send</button></div>
<input type="file" class="picker" accept="image/*,application/pdf,audio/*,video/mp4,video/quicktime,.wav,.flac,.aif,.aiff,.caf,.m4a,.mp3,.ogg,.opus,.mp4,.mov,.m4v" multiple hidden>
<p class="rec-status" role="status"></p>
</section>
<script>
(() => {
  const form = document.querySelector(".recorder");
  const { drafts: draftsUrl, send: sendUrl, files: filesUrl } = form.dataset;
  const fixedRe = form.dataset.re;  // a message page's recorder always answers its message
  const pick = (selector) => form.querySelector(selector);
  const note = pick(".note");
  const recordBtn = pick(".record");
  const sendBtn = pick(".send");
  const discardBtn = pick(".discard");
  const takesEl = pick(".takes");
  const status = pick(".rec-status");
  const attachBtn = pick(".attach");
  const picker = pick(".picker");
  const attachedEl = pick(".attached");
  const replying = pick(".replying");
  const storeKey = `${location.pathname}|${sendUrl}|${fixedRe || ""}`;
  const noteKey = `note|${storeKey}`;
  const filesKey = `files|${storeKey}`;
  const reKey = `re|${storeKey}`;
  const captureKey = (n) => `capture|${storeKey}|${n}`;  // what the microphone did for take n
  let attached = JSON.parse(localStorage.getItem(filesKey) || "[]");  // uploaded, unsent: {id, name, type}
  let takes = [];     // finished recordings: {n, blob, url, draft, capture}
  let active = null;  // the recording in progress
  let nextTake = 0;
  window.composing = () => Boolean(note.value.trim() || active || takes.length || attached.length);
  window.recording = () => Boolean(active);

  const show = (text, isError = false) => {
    status.textContent = text;
    status.className = isError ? "rec-status error" : "rec-status";
  };

  async function post(url, body, type) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": type }, body });
    const out = await r.json();
    if (!r.ok) throw new Error(out.error || r.statusText);
    return out;
  }

  const db = new Promise((resolve, reject) => {
    const req = indexedDB.open("nanotea", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("chunks");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  async function idb(mode, work) {
    const store = (await db).transaction("chunks", mode).objectStore("chunks");
    return new Promise((resolve, reject) => {
      const req = work(store);
      store.transaction.oncomplete = () => resolve(req.result);
      store.transaction.onerror = () => reject(store.transaction.error);
      store.transaction.onabort = () => reject(store.transaction.error);
    });
  }
  const pad = (n, width) => String(n).padStart(width, "0");
  const chunkKey = (n, i) => `${storeKey}|${pad(n, 4)}|${pad(i, 6)}`;
  const kept = () => IDBKeyRange.bound(`${storeKey}|`, `${storeKey}|\\uffff`);

  function render() {
    takesEl.replaceChildren(...takes.map((take, i) => {
      const row = document.createElement("div");
      const label = document.createElement("div");
      label.className = "meta";
      label.textContent = (takes.length > 1 ? `Recording ${i + 1}` : "Recording") + (take.capture?.raw ? ", raw" : "");
      const player = document.createElement("audio");
      player.controls = true;
      player.src = take.url;
      Object.assign(player.dataset, { title: "Your recording", artist: document.body.dataset.owner });
      row.append(label, player);
      return row;
    }));
    discardBtn.hidden = !takes.length || Boolean(active);
    recordBtn.textContent = active ? "Stop" : takes.length ? "Record more" : "Record";
    recordBtn.classList.toggle("live", Boolean(active));
    sendBtn.disabled = Boolean(active);
    document.dispatchEvent(new Event("recorder-state"));  // the screen stays on while recording
  }

  function upload(take) {
    take.draft = post(draftsUrl, take.blob, take.blob.type).then((out) => out.draft);
    take.draft.catch((err) => show(`Upload failed: ${err.message}. The recording is kept here; Send will try again.`, true));
  }

  async function draftId(take) {
    try {
      return await take.draft;
    } catch {
      upload(take);  // the first failure is already on screen
      return take.draft;
    }
  }

  function addTake(n, blob, saving = Promise.resolve()) {
    const take = { n, blob, url: URL.createObjectURL(blob), saving,
                   capture: JSON.parse(localStorage.getItem(captureKey(n)) || "null") };
    upload(take);
    takes.push(take);
    render();
  }

  function settle(take) {
    if (take.settled) return;
    take.settled = true;
    take.stream.getTracks().forEach((track) => track.stop());
    if (active === take) active = null;
    const why = take.reason ? ` (${take.reason})` : "";
    if (!take.chunks.length) {
      localStorage.removeItem(captureKey(take.n));
      render();
      show(`Recording stopped${why} before anything was captured.`, true);
      return;
    }
    addTake(take.n, new Blob(take.chunks, { type: take.type }), take.saving);
    show(take.reason ? `Recording stopped${why}. Kept what was captured: play it back, record more, or send.`
                     : "Play it back. Record more, or send when it's right.");
  }

  function finish(reason) {
    const take = active;
    if (!take) return;
    take.reason = take.reason || reason;
    if (take.recorder.state !== "inactive") take.recorder.stop();  // fires dataavailable, then stop
    else settle(take);
  }

  // Raw: the microphone as it is, without the processing that helps speech: for a voice sample or an
  // instrument. Kept across reloads; a question that asks for a raw recording turns it on.
  const rawBtn = pick(".raw");
  const PROCESSING = ["echoCancellation", "noiseSuppression", "autoGainControl"];
  const RAW = { echoCancellation: false, noiseSuppression: false, autoGainControl: false,
                channelCount: { ideal: 2 }, sampleRate: { ideal: 48000 } };
  const RAW_BPS = 256000;
  const rawOn = () => rawBtn.getAttribute("aria-pressed") === "true";
  const showRaw = (on, keep = true) => {
    rawBtn.setAttribute("aria-pressed", String(on));
    if (!keep) return;
    if (on) localStorage.setItem("recorder-raw", "1");
    else localStorage.removeItem("recorder-raw");
  };
  if (form.dataset.raw) {
    showRaw(true, false);
    show("This question asks for a raw recording: Raw is on, so the microphone's processing is off.");
  } else {
    showRaw(Boolean(localStorage.getItem("recorder-raw")));
  }
  rawBtn.onclick = () => {
    if (active) { show("Stop the recording first; Raw applies to the next one.", true); return; }
    showRaw(!rawOn());
    show(rawOn() ? "Raw on: the next recording keeps the sound as it is." : "Raw off: recording speech.");
  };
  const reported = (v) => (v === undefined ? null : v);
  const described = (c) =>
    `${c.rate ? `${c.rate / 1000} kHz` : "rate not reported"}, ` +
    `${c.channels ? `${c.channels} channel${c.channels === 1 ? "" : "s"}` : "channels not reported"}` +
    (c.mic ? `, ${c.mic}` : "");

  recordBtn.onclick = async () => {
    if (active) { finish(""); return; }
    if (!window.MediaRecorder || !navigator.mediaDevices) {
      show(window.isSecureContext ? "This browser can't record."
           : "Recording needs https, or this computer's own http://127.0.0.1; this page came over plain http.", true);
      return;
    }
    const type = ["audio/mp4", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t));
    if (!type) { show("This browser can't record mp4 or webm audio.", true); return; }
    // The microphone would pick up anything playing, and the "playback" audio session can't record.
    document.querySelectorAll("audio").forEach((player) => player.pause());
    if (navigator.audioSession) navigator.audioSession.type = "auto";
    const raw = rawOn();
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia(raw ? { audio: RAW } : { audio: true });
    } catch (err) {
      show(`Microphone unavailable: ${err.message}`, true);
      return;
    }
    // What the browser actually did. Raw with the processing still on is not raw: refuse rather than pretend.
    const track = stream.getAudioTracks()[0];
    const got = track.getSettings();
    const kept = PROCESSING.filter((k) => got[k] === true);
    if (raw && kept.length) {
      stream.getTracks().forEach((t) => t.stop());
      show(`This browser kept ${kept.join(", ")} on, so it can't record raw here. Turn Raw off to record speech.`,
           true);
      return;
    }
    const capture = { raw, rate: reported(got.sampleRate), channels: reported(got.channelCount),
                      echo_cancellation: reported(got.echoCancellation),
                      noise_suppression: reported(got.noiseSuppression),
                      auto_gain_control: reported(got.autoGainControl), mic: track.label || null };
    const recorder = new MediaRecorder(stream, raw ? { mimeType: type, audioBitsPerSecond: RAW_BPS }
                                                   : { mimeType: type });
    const take = { n: nextTake++, recorder, stream, type: recorder.mimeType || type, chunks: [],
                   saving: Promise.resolve() };
    localStorage.setItem(captureKey(take.n), JSON.stringify(capture));
    recorder.ondataavailable = (ev) => {
      if (!ev.data.size) return;
      const i = take.chunks.length;
      take.chunks.push(ev.data);
      take.saving = take.saving
        .then(async () => {
          const data = await ev.data.arrayBuffer();
          await idb("readwrite", (store) => store.put({ type: take.type, data }, chunkKey(take.n, i)));
        })
        .catch((err) => show(`Couldn't keep a copy on this device: ${err.message}`, true));
    };
    recorder.onstop = () => settle(take);
    recorder.onerror = (ev) => {
      take.reason = `recording error: ${ev.error ? ev.error.message : "unknown"}`;
      settle(take);
    };
    stream.getAudioTracks().forEach((track) => { track.onended = () => finish("the microphone was taken away"); });
    active = take;
    recorder.start(1000);
    render();
    const unsaid = raw ? PROCESSING.filter((k) => got[k] === undefined) : [];
    show(raw ? `Recording raw: ${described(capture)}` +
               `${unsaid.length ? `; ${unsaid.join(", ")} not reported` : ""}. Tap Stop when you're done.`
             : "Recording. Tap Stop when you're done.");
  };

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) finish("the screen locked or you left the app");
  });
  window.addEventListener("pagehide", () => finish("the page closed"));

  discardBtn.onclick = async () => {
    if (!confirm("Discard the recording? It can't be recovered.")) return;
    const pending = takes.map((take) => take.saving);
    takes.forEach((take) => { URL.revokeObjectURL(take.url); localStorage.removeItem(captureKey(take.n)); });
    takes = [];
    render();
    try {
      await Promise.all(pending);  // a late chunk save must not outlive the delete
      await idb("readwrite", (store) => store.delete(kept()));
      show("Recording discarded.");
    } catch (err) {
      show(`Discarded here, but a saved copy remains on this device: ${err.message}`, true);
    }
  };

  const keepAttached = () => localStorage.setItem(filesKey, JSON.stringify(attached));
  function renderAttached() {
    attachedEl.replaceChildren(...attached.map((file, i) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "x";
      remove.setAttribute("aria-label", `Remove ${file.name}`);
      remove.onclick = () => { attached.splice(i, 1); keepAttached(); renderAttached(); };
      chip.append(`${file.name} `, remove);
      return chip;
    }));
  }
  attachBtn.onclick = () => picker.click();
  // XHR rather than fetch: a long recording takes a while to send, and only XHR reports how far the upload has got.
  const sendFile = (file) => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${filesUrl}?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) show(`Uploading ${file.name}: ${Math.floor((100 * ev.loaded) / ev.total)}%`);
    };
    xhr.onload = () => {
      let out;
      try {
        out = JSON.parse(xhr.responseText);
      } catch {
        reject(new Error(`${xhr.status} ${xhr.statusText}: the server's answer wasn't JSON (a proxy's size limit?)`));
        return;
      }
      if (xhr.status === 200) resolve(out);
      else reject(new Error(out.error || `${xhr.status} ${xhr.statusText}`));
    };
    xhr.onerror = () => reject(new Error("the connection failed"));
    xhr.send(file);
  });
  async function attachFiles(chosen) {
    for (const file of chosen) {
      show(`Uploading ${file.name}...`);
      try {
        const out = await sendFile(file);
        attached.push({ id: out.file, name: file.name, type: file.type });
        keepAttached();
        renderAttached();
        show(`Attached ${file.name}.`);
      } catch (err) {
        show(`Couldn't attach ${file.name}: ${err.message}`, true);
      }
    }
  }
  picker.onchange = () => {
    const chosen = [...picker.files];
    picker.value = "";
    attachFiles(chosen);
  };
  // A pasted picture or file, or one dropped anywhere on the page, is attached as if picked.
  note.addEventListener("paste", (ev) => {
    const pasted = [...(ev.clipboardData?.files || [])];
    if (!pasted.length) return;
    ev.preventDefault();
    attachFiles(pasted);
  });
  const carriesFiles = (ev) => [...(ev.dataTransfer?.types || [])].includes("Files");
  let dragDepth = 0;
  document.addEventListener("dragenter", (ev) => {
    if (!carriesFiles(ev)) return;
    dragDepth += 1;
    form.classList.add("dropping");
  });
  document.addEventListener("dragleave", (ev) => {
    if (!carriesFiles(ev)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) form.classList.remove("dropping");
  });
  document.addEventListener("dragover", (ev) => {
    if (!carriesFiles(ev)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
  });
  document.addEventListener("drop", (ev) => {
    if (!carriesFiles(ev)) return;
    ev.preventDefault();
    dragDepth = 0;
    form.classList.remove("dropping");
    attachFiles([...ev.dataTransfer.files]);
  });

  // Reply: the next send answers that message. Kept across reloads. Where the page says where a send goes
  // (data-to), the box says what it will be there: a new thread, a reply in this thread, or a reply to a message.
  function replyTo(target) {
    form.dataset.re = target ? target.id : "";
    replying.hidden = !target;
    replying.querySelector("span").textContent = target ? `Replying to ${target.label}` : "";
    const { to } = form.dataset;
    if (to) {
      note.placeholder = target ? `Reply ${to}` : form.dataset.thread ? `Reply in this thread, ${to}`
        : `New thread ${to}`;
      note.setAttribute("aria-label", note.placeholder);
    }
    if (target) localStorage.setItem(reKey, JSON.stringify(target));
    else localStorage.removeItem(reKey);
  }
  if (!fixedRe) {
    document.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-reply-to]");
      if (!btn) return;
      replyTo({ id: btn.dataset.replyTo, label: btn.dataset.replyLabel });
      note.focus();
    });
    document.addEventListener("reply-to", (ev) => { replyTo(ev.detail); note.focus(); });
    pick(".cancel-reply").onclick = () => replyTo(null);
    const saved = localStorage.getItem(reKey);
    replyTo(saved ? JSON.parse(saved) : null);
  }

  sendBtn.onclick = async () => {
    // Leading spaces can be code's indent; blank lines before and spaces after are dropped.
    const text = note.value.replace(/^(?:[ \\t]*\\n)+/, "").trimEnd();
    if (!text && !takes.length && !attached.length) { show("Type something, record, or attach first.", true); return; }
    sendBtn.disabled = recordBtn.disabled = true;
    show("Sending...");
    try {
      const drafts = [];
      const captures = {};
      for (const take of takes) {
        const id = await draftId(take);
        drafts.push(id);
        if (take.capture) captures[id] = take.capture;
      }
      const payload = { text: text || null, drafts, captures, files: attached.map((file) => file.id) };
      // In a thread, a send answers the message picked with Reply, or else the thread.
      if (form.dataset.re || form.dataset.thread) payload.re = form.dataset.re || form.dataset.thread;
      await post(sendUrl, JSON.stringify(payload), "application/json");
    } catch (err) {
      show(`Not sent: ${err.message}. Nothing is lost; try again.`, true);
      sendBtn.disabled = recordBtn.disabled = false;
      return;
    }
    const pending = takes.map((take) => take.saving);
    takes.forEach((take) => { URL.revokeObjectURL(take.url); localStorage.removeItem(captureKey(take.n)); });
    takes = [];
    attached = [];
    note.value = "";
    pasteBtn.hidden = true;
    localStorage.removeItem(noteKey);
    localStorage.removeItem(filesKey);
    localStorage.removeItem(reKey);
    try {
      await Promise.all(pending);  // a late chunk save must not outlive the delete
      await idb("readwrite", (store) => store.delete(kept()));
    } catch (err) {
      alert(`Sent, but this device still holds a copy of the recording: ${err.message}`);
    }
    if (!window.liveThread) { location.reload(); return; }  // a message page: its answer shows on the reload
    // A conversation: clear the composer and show the sent message in place.
    render();
    renderAttached();
    if (!fixedRe) replyTo(null);
    grow();
    show("");
    sendBtn.disabled = recordBtn.disabled = false;
    window.liveThread.refresh({ toBottom: true });
  };

  // Hold the form's height while measuring, so the list above never sees it shrink and loses its place.
  const grow = () => {
    form.style.minHeight = `${form.offsetHeight}px`;
    note.style.height = "auto";
    note.style.height = `${note.scrollHeight}px`;
    form.style.minHeight = "";
  };
  renderAttached();
  note.value = localStorage.getItem(noteKey) || note.value;
  // With bang commands on, a message that starts with ! runs on the agent's machine; say so as it is typed.
  const bangHint = pick(".bang-hint");
  const bangShow = () => {
    const runs = form.dataset.bang && /^!\\s*\\S/.test(note.value) && !takes.length && !attached.length;
    bangHint.hidden = !runs;
    if (runs) bangHint.textContent = `Sending this runs it as a command on ${form.dataset.bang}'s machine. `
      + "To send a literal !, put a backslash before it.";
  };
  note.addEventListener("input", () => { localStorage.setItem(noteKey, note.value); grow(); bangShow(); });
  grow();
  bangShow();
  // Markdown formatting, on Slack's keys: an edit as if typed, so Undo takes it back.
  const fmtRow = pick(".fmt");
  const fmtToggle = pick(".fmt-toggle");
  const pasteBtn = pick(".paste-code");
  const showFormat = (on) => {
    fmtRow.hidden = !on;
    fmtToggle.setAttribute("aria-pressed", String(on));
    if (on) localStorage.setItem("composer-format", "1");
    else localStorage.removeItem("composer-format");
  };
  showFormat(Boolean(localStorage.getItem("composer-format")));
  fmtToggle.onclick = () => { showFormat(fmtRow.hidden); grow(); };
  // Tapping a format button keeps the keyboard up and the selection where it is.
  fmtRow.addEventListener("pointerdown", (ev) => ev.preventDefault());
  pasteBtn.addEventListener("pointerdown", (ev) => ev.preventDefault());

  function edit(start, end, text, selStart, selEnd) {
    note.focus();
    note.setSelectionRange(start, end);
    if (!document.execCommand(text ? "insertText" : "delete", false, text)) {
      show("This browser won't let the page edit the text, so formatting is off here.", true);
      return;
    }
    note.setSelectionRange(selStart, selEnd);
  }

  // The whole lines the selection touches; a selection ending at a line's start doesn't take that line.
  function lineSpan() {
    const v = note.value;
    let a = note.selectionStart, b = note.selectionEnd;
    a = v.lastIndexOf("\\n", a - 1) + 1;
    if (b > a && v[b - 1] === "\\n") b--;
    const end = v.indexOf("\\n", b);
    return [a, end < 0 ? v.length : end];
  }

  const run = (v, at, step) => { let n = 0; while (v[at + n * step + (step < 0 ? -1 : 0)] === "*") n++; return n; };
  function wrap(mark) {
    const v = note.value, a = note.selectionStart, b = note.selectionEnd, m = mark.length;
    const sel = v.slice(a, b);
    if (mark === "`" && sel.includes("\\n")) { block(); return; }
    let outside = v.slice(a - m, a) === mark && v.slice(b, b + m) === mark;
    let inside = sel.length >= 2 * m && sel.startsWith(mark) && sel.endsWith(mark);
    if (mark[0] === "*") {
      // Bold is two stars and italic one, so count the run: ***x*** is both.
      const want = (n) => (m === 2 ? n >= 2 : n % 2 === 1);
      outside = want(run(v, a, -1)) && want(run(v, b, 1));
      inside = inside && want(run(sel, 0, 1)) && want(run(sel, sel.length, -1));
    }
    if (outside) { edit(a - m, b + m, sel, a - m, b - m); return; }
    if (inside) { edit(a, b, sel.slice(m, -m), a, b - 2 * m); return; }
    // Spaces at the edges stay outside the marks, where markdown needs them.
    const lead = sel.length - sel.trimStart().length;
    const core = sel.trim();
    const open = mark === "`" && core.includes("`") ? "`` " : mark;
    const close = open === "`` " ? " ``" : mark;
    const start = a + lead + open.length;
    edit(a + lead, a + lead + core.length, open + core + close, start, start + core.length);
  }

  const FENCE_LINE = /^ {0,3}(`{3,}|~{3,})/;
  // Inside a code block (its fences included), takes the fences away; otherwise fences the lines.
  function block() {
    const v = note.value;
    const rows = v.split("\\n");
    const lineOf = (at) => v.slice(0, at).split("\\n").length - 1;
    const first = lineOf(note.selectionStart), last = lineOf(note.selectionEnd);
    let open = -1;
    for (let i = 0; i <= rows.length; i++) {
      const fenceRow = i < rows.length && FENCE_LINE.test(rows[i]);
      if (open < 0) { if (fenceRow) open = i; continue; }
      if (!fenceRow && i < rows.length) continue;
      if (open <= first && last <= i) {
        const starts = rows.map((r, j) => rows.slice(0, j).join("\\n").length + (j ? 1 : 0));
        const a = starts[open], b = i < rows.length ? starts[i] + rows[i].length : v.length;
        const inner = rows.slice(open + 1, i).join("\\n");
        edit(a, b, inner, a, a + inner.length);
        return;
      }
      open = -1;
    }
    fence(...lineSpan());
  }
  // Fences a..b in a code block on lines of its own, with a fence longer than any inside.
  function fence(a, b) {
    const v = note.value;
    let body = v.slice(a, b);
    const tail = body.endsWith("\\n");
    if (tail) body = body.slice(0, -1);
    const longest = Math.max(2, ...[...body.matchAll(/^ {0,3}(`{3,})/gm)].map((x) => x[1].length));
    const bar = "`".repeat(longest + 1);
    const before = a > 0 && v[a - 1] !== "\\n" ? "\\n" : "";
    const after = tail ? "\\n" : b < v.length && v[b] !== "\\n" ? "\\n" : "";
    const start = a + before.length + bar.length + 1;
    edit(a, b, `${before}${bar}\\n${body}\\n${bar}${after}`, start, start + body.length);
  }

  const PREFIX = { quote: /^> ?/, bullet: /^\\s*[-*+] (\\[[ xX]\\] )?/, number: /^\\s*\\d{1,9}[.)] / };
  function prefix(kind) {
    const [a, b] = lineSpan();
    const rows = note.value.slice(a, b).split("\\n");
    const filled = rows.filter((r) => r.trim());
    const off = filled.length > 0 && filled.every((r) => PREFIX[kind].test(r));
    let n = 0;
    const out = rows.map((r) => {
      if (off) return r.replace(PREFIX[kind], "");
      if (!r.trim() && rows.length > 1) return r;
      if (kind === "quote") return `> ${r}`;
      const bare = r.replace(/^\\s*([-*+]|\\d{1,9}[.)]) /, "");
      return kind === "bullet" ? `- ${bare}` : `${++n}. ${bare}`;
    }).join("\\n");
    const caret = note.selectionStart === note.selectionEnd;
    edit(a, b, out, caret ? a + out.length : a, a + out.length);
  }

  const FORMAT = { bold: () => wrap("**"), italic: () => wrap("*"), strike: () => wrap("~~"), code: () => wrap("`"),
                   block, quote: () => prefix("quote"), bullet: () => prefix("bullet"), number: () => prefix("number") };
  fmtRow.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-fmt]");
    if (btn) FORMAT[btn.dataset.fmt]();
  });
  const KEYS = { "KeyB": "bold", "KeyI": "italic", "shift KeyX": "strike", "shift KeyC": "code",
                 "alt shift KeyC": "block", "shift Digit9": "quote", "shift Digit8": "bullet", "shift Digit7": "number" };

  // A paste of several lines can become a code block, as pasted.
  let pasted = null;
  note.addEventListener("paste", () => { pasted = note.selectionStart; });
  note.addEventListener("input", (ev) => {
    const range = ev.inputType === "insertFromPaste" && pasted !== null ? [pasted, note.selectionEnd] : null;
    pasted = null;
    const text = range ? note.value.slice(...range) : "";
    pasteBtn.hidden = !(text.trim().includes("\\n") && !/^ {0,3}(```|~~~)/m.test(text));
    pasteBtn.range = range;
  });
  pasteBtn.onclick = () => {
    pasteBtn.hidden = true;
    fence(...pasteBtn.range);
  };

  // Whether the caret is inside a code block that's still open.
  const inFence = () => {
    const rows = note.value.slice(0, note.selectionStart).split("\\n");
    return rows.filter((r) => FENCE_LINE.test(r)).length % 2 === 1;
  };
  // A new line in a list starts the next item; on an empty item, it ends the list.
  function newLine() {
    const v = note.value, a = note.selectionStart;
    if (a !== note.selectionEnd || inFence()) return false;
    const start = v.lastIndexOf("\\n", a - 1) + 1;
    const line = v.slice(start, a);
    const m = line.match(/^(\\s*)([-*+]|(\\d{1,9})([.)])) (\\[[ xX]\\] )?/);
    if (!m || v.slice(a).split("\\n")[0].trim()) return false;
    if (!line.slice(m[0].length).trim()) { edit(start, a, "", start, start); return true; }
    const next = m[3] ? `${Number(m[3]) + 1}${m[4]}` : m[2];
    const item = `\\n${m[1]}${next} ${m[5] ? "[ ] " : ""}`;
    edit(a, a, item, a + item.length, a + item.length);
    return true;
  }

  // With a mouse or trackpad, Enter sends and Shift-Enter is a new line; on the phone, and inside an open code
  // block, Enter is a new line. Cmd/Ctrl-Enter sends everywhere. Never mid-IME. Sending is the Send button's own path.
  const finePointer = matchMedia("(pointer: fine)");
  note.addEventListener("keydown", (ev) => {
    if (ev.isComposing || ev.keyCode === 229) return;
    const command = ev.metaKey || ev.ctrlKey;
    const name = command && KEYS[[ev.altKey && "alt", ev.shiftKey && "shift", ev.code].filter(Boolean).join(" ")];
    if (name) { ev.preventDefault(); FORMAT[name](); return; }
    if (ev.key !== "Enter") return;
    if (!command && (ev.shiftKey || ev.altKey || !finePointer.matches || inFence())) {
      if (!ev.altKey && newLine()) ev.preventDefault();
      return;
    }
    ev.preventDefault();
    sendBtn.click();
  });

  async function restore() {
    const [keys, values] = await Promise.all([
      idb("readonly", (store) => store.getAllKeys(kept())),
      idb("readonly", (store) => store.getAll(kept())),
    ]);
    if (!keys.length) return;
    const byTake = new Map();
    keys.forEach((key, i) => {
      const n = Number(String(key).split("|").at(-2));
      if (!byTake.has(n)) byTake.set(n, []);
      byTake.get(n).push(values[i]);
    });
    for (const [n, parts] of [...byTake].sort((a, b) => a[0] - b[0])) {
      addTake(n, new Blob(parts.map((part) => part.data), { type: parts[0].type }));
      nextTake = Math.max(nextTake, n + 1);
    }
    show("Recovered an unsent recording. Play it back, then send, record more, or discard.");
  }
  // Record stays off until recovery finishes, so a new recording can't reuse a kept one's slot.
  restore()
    .catch((err) => show(`Couldn't check this device for an unsent recording: ${err.message}`, true))
    .finally(() => { recordBtn.disabled = false; render(); });
})();
</script>
"""

# A "playback" audio session and lock screen controls, so a locked iPhone keeps playing. While the owner
# records, the screen stays on: the recording stops if the phone sleeps.
PLAYBACK = """
<script>
(() => {
  const note = document.getElementById("screen-note");
  const warn = (text) => { note.textContent = text; note.hidden = false; };
  const recording = () => Boolean(window.recording && window.recording());
  let held = null;  // the wake lock while it's held
  let asking = false;
  async function sync() {
    // The recorder switches to "auto" before it opens the microphone, which "playback" doesn't allow.
    if (navigator.audioSession) navigator.audioSession.type = recording() ? "auto" : "playback";
    const want = recording() && !document.hidden;
    if (want && !held && !asking) {
      if (!("wakeLock" in navigator)) {
        warn("This browser can't keep the screen on while you record: the recording stops if the phone sleeps.");
        return;
      }
      asking = true;
      try {
        const lock = await navigator.wakeLock.request("screen");
        // The browser lets go by itself when the page is hidden.
        lock.addEventListener("release", () => { if (held === lock) held = null; });
        held = lock;
      } finally {
        asking = false;
      }
      await sync();  // the recording may have stopped while asking
    } else if (!want && held) {
      const lock = held;
      held = null;
      await lock.release();
    }
  }
  const onChange = () => sync().catch((err) =>
    warn(`Couldn't keep the screen on while you record (${err.message}): the recording stops if the phone sleeps.`));
  document.addEventListener("recorder-state", onChange);
  document.addEventListener("visibilitychange", onChange);
  onChange();

  // The lock screen shows what's playing, and plays, pauses, and seeks it.
  if (!("mediaSession" in navigator)) return;
  const session = navigator.mediaSession;
  document.addEventListener("play", (ev) => {
    const player = ev.target;
    session.metadata = new MediaMetadata({
      title: player.dataset.title, artist: player.dataset.artist, album: document.body.dataset.app,
      artwork: [{ src: "/icon-512.png", sizes: "512x512", type: "image/png" }] });
    session.setActionHandler("play", () => player.play().catch((err) => warn(`Couldn't play: ${err.message}`)));
    session.setActionHandler("pause", () => player.pause());
    session.setActionHandler("seekbackward", (d) => {
      player.currentTime = Math.max(0, player.currentTime - (d.seekOffset ?? 10)); });
    session.setActionHandler("seekforward", (d) => {
      player.currentTime = Math.min(player.duration, player.currentTime + (d.seekOffset ?? 10)); });
    session.setActionHandler("seekto", (d) => { player.currentTime = d.seekTime; });
  }, true);
})();
</script>
"""

# Under the thread: whether it could be kept current.
CHAT_STATUS = '<p id="chat-status"></p>'

# Conversation layout: size the pane to the visible viewport (the iPhone keyboard shrinks only that),
# land on the newest message, and keep the newest in view while the composer or the list grows.
CONVO_SCRIPT = """
<script>
(() => {
  const root = document.documentElement;
  const vv = window.visualViewport;
  const fit = () => {
    root.style.setProperty("--vv-h", `${vv.height}px`);
    root.style.setProperty("--vv-top", `${vv.offsetTop}px`);
    root.classList.toggle("kb", innerHeight - vv.height * vv.scale > 80);
  };
  fit();
  vv.addEventListener("resize", fit);
  vv.addEventListener("scroll", fit);
  const scroller = document.getElementById("scroller");
  // A link to one message lands on it; the browser's own jump ran before the list could scroll. One older than
  // the page loaded is fetched first (LIVE_THREAD), so until then nothing pins.
  const want = location.hash ? decodeURIComponent(location.hash.slice(1)) : "";
  // In the grouped view a reply folded into its thread's activity row is found there (data-covers).
  const rowFor = (id) => document.getElementById(id) || document.querySelector(`#thread [data-covers~="${id}"]`);
  const target = want && rowFor(want);
  const fetchRow = Boolean(want && !target && /^[mg]-/.test(want));
  let pinned = !target && !fetchRow;
  const pin = () => { if (pinned) scroller.scrollTop = scroller.scrollHeight; };
  const atBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 4;
  // Only a scroll up lets go of the bottom. A picture above that loads after the pin grows the list, and the
  // scroll event that follows would read as one, leaving the view part way up.
  let lastTop = scroller.scrollTop;
  scroller.addEventListener("scroll", () => {
    if (atBottom()) pinned = true;
    else if (scroller.scrollTop < lastTop) pinned = false;
    lastTop = scroller.scrollTop;
  }, { passive: true });
  const watch = new ResizeObserver(pin);
  watch.observe(scroller);
  watch.observe(document.getElementById("thread"));
  // Landing on a row that ends the list counts as being at the bottom.
  const land = (row) => {
    row.scrollIntoView();
    pinned = atBottom();
    if (row.dataset.covers) {  // :target can't mark a row that stands in for the one linked
      row.classList.remove("hit");
      void row.offsetWidth;
      row.classList.add("hit");
    }
  };
  if (target) land(target); else pin();
  window.convo = { pinned: () => pinned, land, rowFor, toBottom: () => { pinned = true; pin(); },
                   wanted: fetchRow ? want : null };
})();
</script>
"""

# Keep an open conversation current without reloading: fetch the page, patch the thread row by row, and keep
# the composer, any player in use, and the scroll. At the bottom, the newest stays in view; scrolled up, a pill
# offers the way down. Checks on a push (from the service worker), on coming back to the page, and every few
# seconds while it's in view.
LIVE_THREAD = """
<script>
(() => {
  const thread = document.getElementById("thread");
  const status = document.getElementById("chat-status");
  const pill = document.getElementById("new-pill");
  const scroller = document.getElementById("scroller");
  const older = document.getElementById("older");
  const { versionUrl, olderUrl } = thread.dataset;
  const EVERY_MS = 5000;
  const keyOf = (el) => el.id || (el.classList.contains("day") ? `day|${el.textContent}` : el.className);
  // What the server sent for each row, so rows the page itself changed (fresh, heard, a new pill) compare right.
  const served = new WeakMap();
  [...thread.children].forEach((el) => served.set(el, el.outerHTML));

  const lastKey = `last|${location.pathname}`;
  const idOf = (el) => (/^[mg]-/.test(el.id) ? el.id.slice(2) : null);
  const newestId = () => [...thread.children].map(idOf).filter(Boolean).sort().at(-1);
  const last = sessionStorage.getItem(lastKey);
  if (last) [...thread.children].forEach((el) => { if (idOf(el) > last) el.classList.add("fresh"); });
  const remember = () => { const id = newestId(); if (id) sessionStorage.setItem(lastKey, id); };
  remember();

  const inUse = (a) => !a.paused || (a.currentTime > 0 && !a.ended);
  // A row with a player in use keeps that player: moved into the new row within one task, it plays on.
  // Until the new row has the same player, the old row stays.
  function carry(old, row) {
    const pairs = [...old.querySelectorAll("audio")].filter(inUse).map((player) => [player,
      [...row.querySelectorAll("audio")].find((a) => a.getAttribute("src") === player.getAttribute("src"))]);
    if (pairs.some(([, twin]) => !twin)) return false;
    pairs.forEach(([player, twin]) => twin.replaceWith(player));
    return true;
  }

  function patchThread(next) {
    // Into this document first: a player carried across documents reloads and starts over.
    document.adoptNode(next);
    const old = new Map([...thread.children].map((el) => [keyOf(el), el]));
    const added = [];
    const rows = [...next.children].map((row) => {
      const key = keyOf(row);
      const was = old.get(key);
      old.delete(key);
      const html = row.outerHTML;
      if (was && (served.get(was) === html || !carry(was, row))) return was;
      served.set(row, html);
      if (was) was.replaceWith(row);
      else if (idOf(row)) { row.classList.add("fresh"); added.push(row); }
      return row;
    });
    old.forEach((el) => el.remove());
    rows.forEach((row, i) => { if (thread.children[i] !== row) thread.insertBefore(row, thread.children[i] || null); });
    return added;
  }

  // The header line, the menu badge, and the sidebar's groups, counts and dots.
  function patchShell(doc) {
    const swap = (sel) => {
      const a = document.querySelector(sel), b = doc.querySelector(sel);
      if (a && b && a.innerHTML !== b.innerHTML) a.innerHTML = b.innerHTML;
    };
    swap("#top .titles");
    swap("#menu");
    const side = document.getElementById("side");
    const fresh = doc.getElementById("side");
    if (!side || !fresh) return;
    const order = (root) => [...root.querySelectorAll(".grp, a.conv")]
      .map((x) => (x.matches(".grp") ? `[${x.dataset.grp}]` : x.getAttribute("href"))).join(" ");
    // Rebuilt when a row or group comes, goes or moves; otherwise each row and group header is patched in place.
    if (order(fresh) !== order(side)) {
      const add = document.getElementById("new-channel");
      const slot = fresh.querySelector("#new-channel");
      if (!add || !slot) throw new Error(`the sidebar ${slot ? "on this page is" : "came back"} without #new-channel`);
      slot.replaceWith(add);
      side.replaceChildren(...fresh.childNodes);
      window.nanoteaFolds();
    } else {
      const mine = new Map([...side.querySelectorAll("a.conv")].map((a) => [a.getAttribute("href"), a]));
      fresh.querySelectorAll("a.conv").forEach((a) => {
        const b = mine.get(a.getAttribute("href"));
        if (b.innerHTML !== a.innerHTML) b.innerHTML = a.innerHTML;
        if (b.className !== a.className) b.className = a.className;
        if (b.title !== a.title) b.title = a.title;
      });
      const heads = new Map([...side.querySelectorAll(".grp")].map((g) => [g.dataset.grp, g.querySelector(".fold")]));
      fresh.querySelectorAll(".grp").forEach((g) => {
        const a = g.querySelector(".fold"), b = heads.get(g.dataset.grp);
        if (b.innerHTML !== a.innerHTML) b.innerHTML = a.innerHTML;
      });
    }
    window.nanoteaCurrent(side);
  }

  // A refresh and a load of older items each change which rows the thread holds: one at a time, in turn.
  let lane = Promise.resolve();
  const inLane = (fn) => (lane = lane.catch(() => {}).then(fn));

  // Older items, as the owner scrolls up: the page before the oldest shown, or with until (a row's id), every one
  // back to that row. The view stays where it was.
  let loadingOlder = false;
  function loadOlder(until) {
    const run = async () => {
      if (!thread.dataset.from || (!until && !thread.dataset.more)) return;
      const u = new URL(olderUrl, location.href);
      u.searchParams.set("before", thread.dataset.from);
      if (until) u.searchParams.set("until", until);
      const r = await fetch(u, { cache: "no-store" });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
      if (out.html) {
        const tpl = document.createElement("template");
        tpl.innerHTML = out.html;
        const rows = [...tpl.content.children];
        // Measured first: everything below the view stays put.
        const fromBottom = scroller.scrollHeight - scroller.scrollTop;
        // The day these end on is the day the page starts with: one line for it.
        const first = thread.firstElementChild;
        const lastDay = rows.filter((el) => el.classList.contains("day")).at(-1);
        if (first?.classList.contains("day") && lastDay?.textContent === first.textContent) first.remove();
        thread.prepend(...rows);
        rows.forEach((el) => served.set(el, el.outerHTML));
        scroller.scrollTop = scroller.scrollHeight - fromBottom;
        thread.dataset.from = out.before;
        document.dispatchEvent(new CustomEvent("thread-patched", { detail: { thread } }));
      }
      thread.dataset.more = out.more ? "1" : "";
      older.hidden = !out.more;
    };
    return inLane(run);
  }
  const nearTop = () => !older.hidden && older.getBoundingClientRect().bottom > scroller.getBoundingClientRect().top - 800;
  async function olderWhileNear() {
    if (loadingOlder) return;
    loadingOlder = true;
    try {
      while (nearTop()) await loadOlder();
      older.textContent = "Loading earlier messages...";
    } catch (err) {
      older.textContent = `Couldn't load earlier messages: ${err.message}. Scroll up to try again.`;
    } finally {
      loadingOlder = false;
    }
  }
  // A link to a row the page didn't load: load back to it, then land there.
  async function reach(id) {
    await loadOlder(id);
    return window.convo.rowFor(id);
  }

  let version = thread.dataset.version;
  let running = null;
  let again = false;
  let wantBottom = false;
  async function refresh({ toBottom = false } = {}) {
    wantBottom = wantBottom || toBottom;
    // Joins the one under way; that one reports any failure.
    if (running) { again = true; return running.then(() => {}, () => {}); }
    running = (async () => {
      do {
        again = false;
        await inLane(patchOnce);
      } while (again);
    })();
    try {
      await running;
      status.textContent = "";
    } catch (err) {
      status.textContent = `Couldn't update the conversation: ${err.message}. Trying again.`;
    } finally {
      running = null;
    }
  }

  async function patchOnce() {
    // Everything from the oldest row shown, and only what gets patched.
    const u = new URL(location.pathname, location.href);
    if (thread.dataset.from) u.searchParams.set("after", thread.dataset.from);
    u.searchParams.set("part", "1");
    const r = await fetch(u, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    const next = doc.getElementById("thread");
    if (!next) throw new Error("the page came back without its conversation");
    const bottom = window.convo.pinned() || wantBottom;
    const added = patchThread(next);
    patchShell(doc);
    version = next.dataset.version;
    thread.dataset.from = next.dataset.from;
    remember();
    document.dispatchEvent(new CustomEvent("thread-patched", { detail: { thread } }));
    if (bottom) { window.convo.toBottom(); pill.hidden = true; }
    else if (added.length) pill.hidden = false;
    wantBottom = false;
  }

  let unpaired = false;
  async function check() {
    if (unpaired) return;
    try {
      const after = thread.dataset.from ? `&after=${encodeURIComponent(thread.dataset.from)}` : "";
      const r = await fetch(versionUrl + after, { cache: "no-store" });
      if (r.status === 403) {
        // The pairing is gone (a lost cookie): asking again won't bring it back.
        unpaired = true;
        status.textContent = "This device is no longer paired, so the conversation has stopped updating. "
          + "Open your pairing link again.";
        return;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const out = await r.json();
      window.nanoteaUnread(out.unread);
      // Hidden, the page only counts: refreshing would mark what it shows as seen.
      if (document.visibilityState !== "visible") return;
      if (out.version !== version) await refresh();
      else if (!running) status.textContent = "";
    } catch (err) {
      status.textContent = `Couldn't check for new messages: ${err.message}. Trying again.`;
    }
  }

  pill.onclick = () => { window.convo.toBottom(); pill.hidden = true; };
  scroller.addEventListener("scroll", () => { if (window.convo.pinned()) pill.hidden = true; }, { passive: true });
  // A command still waiting or running is looked at every second, so its result shows as it lands.
  const tick = () => check().finally(() => setTimeout(tick, thread.querySelector(".bang.live") ? 1000 : EVERY_MS));
  setTimeout(tick, EVERY_MS);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") check(); });
  window.addEventListener("focus", check);
  window.addEventListener("online", check);
  window.addEventListener("pageshow", (ev) => { if (ev.persisted) check(); });

  // A notification for this conversation: update, then show that row.
  async function show(url) {
    await refresh();
    const id = url.hash && decodeURIComponent(url.hash.slice(1));
    let row = id && window.convo.rowFor(id);
    if (id && !row && /^[mg]-/.test(id)) row = await reach(id);
    if (!row) return;
    if (row === thread.lastElementChild) window.convo.toBottom(); else window.convo.land(row);
    pill.hidden = window.convo.pinned() || pill.hidden;
  }
  window.liveThread = { refresh, check, show };

  // The observer alone can miss a scroll back up in the same frame as a load; each scroll checks too.
  const watchTop = () => {
    new IntersectionObserver((seen) => { if (seen.some((x) => x.isIntersecting)) olderWhileNear(); },
                             { root: scroller, rootMargin: "800px 0px 0px 0px" }).observe(older);
    scroller.addEventListener("scroll", () => { if (!loadingOlder && nearTop()) olderWhileNear(); }, { passive: true });
  };
  if (window.convo.wanted) {
    reach(window.convo.wanted).then((row) => {
      if (row) window.convo.land(row); else window.convo.toBottom();
    }, (err) => {
      status.textContent = `Couldn't load that message: ${err.message}.`;
      window.convo.toBottom();
    }).finally(watchTop);
  } else {
    watchTop();
  }
})();
</script>
"""

REACTIONS = ["👍", "👎", "❤️", "😂", "🎉", "👀", "✅", "❓"]


def _recorder(heading: str, drafts_url: str, send_url: str, files_url: str, re_: str = "",
              raw: bool = False, thread: str = "", bang: str = "", to: str = "") -> str:
    """re_: the message every send answers. thread: the one a send answers unless the owner picks another with
    Reply. raw: the answer to a question that asks for a raw recording; the recorder starts with Raw on. bang: who
    a message starting with ! would run a command for, when it would. to: where a send goes, as "to Ana" or
    "in #ops"; the box then says whether it starts a thread or replies."""
    return (RECORDER.replace("__HEADING__", e(heading))
            .replace("__BANG__", e(bang))
            .replace("__TO__", e(to))
            .replace("__THREAD__", e(thread))
            .replace("__RAW__", "1" if raw else "")
            .replace("__DRAFTS__", e(drafts_url))
            .replace("__SEND__", e(send_url))
            .replace("__FILES__", e(files_url))
            .replace("__RE__", e(re_)))


def _when(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a %b %-d, %-I:%M %p")


def _stamp(iso: str) -> str:
    """To the second, for the menu's facts."""
    return datetime.fromisoformat(iso).strftime("%a %b %-d, %-I:%M:%S %p")


def _clock(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%-I:%M %p")


clock = _clock


def _day_label(day: date) -> str:
    today = datetime.now().astimezone().date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%A, %B} {day.day}" + (f", {day.year}" if day.year != today.year else "")


def _plugin_scripts() -> str:
    return "".join(f"<script>{c.script}</script>" for c in SITE["controls"].values() if hasattr(c, "script"))


CONTROL_SCRIPT = """
<script>
(() => {
  // A tap on a control: any element in it with data-act (and data-value), or a form with data-act, whose fields
  // are the value. The service answers with the control drawn again.
  async function act(box, name, value) {
    box.classList.add("busy");
    try {
      const r = await fetch(`/api/messages/${box.dataset.control}/act`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ act: name, value }) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      const tpl = document.createElement("template");
      tpl.innerHTML = out.html.trim();
      box.replaceWith(tpl.content.firstElementChild);
      if (out.answered && !window.liveThread) location.reload();
      else if (out.sent && window.liveThread) await window.liveThread.refresh();
    } catch (err) {
      box.classList.remove("busy");
      alert(`That didn't go through: ${err.message}`);
    }
  }
  document.addEventListener("click", (ev) => {
    const el = ev.target.closest(".control [data-act]");
    if (!el || el.tagName === "FORM") return;
    if (el.tagName !== "INPUT") ev.preventDefault();
    act(el.closest(".control"), el.dataset.act, el.dataset.value ?? null);
  });
  document.addEventListener("submit", (ev) => {
    const form = ev.target.closest(".control form[data-act]");
    if (!form) return;
    ev.preventDefault();
    act(form.closest(".control"), form.dataset.act, Object.fromEntries(new FormData(form)));
  });
})();
</script>
"""


def _page(title: str, body: str, heading: str | None = None, sub: str = "", actions: str = "",
          paired: bool = True, footer: str = "", status: dict | None = None, sub_html: str = "",
          part: bool = False) -> str:
    """heading and sub: the header's title and the line under it (sub_html: that line as markup). status: an
    agent's {text, at}, under those. actions: header buttons.
    footer: a conversation's composer; with one, the body scrolls on its own above it. part: only the sidebar,
    the header's menu and titles, and the body, for a conversation's refresh to patch from."""
    heading = heading or _app_name()
    bar = SITE["sidebar"]() if paired else {"channels": [], "groups": [], "unread": 0}
    if sub and not sub_html:
        sub_html = e(sub)
    unread = bar["unread"]
    badge = f'<span class="count">{unread}</span>' if unread else ""
    side = _sidebar(bar) + '<div id="scrim"></div>' if paired else ""
    status_line = (f'<div class="status" title="{e(status["text"])}"><span class="st-text">{e(status["text"])}</span> '
                   f'<span class="ago">updated {_age_span(status["at"], "ago")}</span></div>' if status else "")
    sub_div = f'<div class="sub">{sub_html}</div>' if sub_html else ""
    titles = f'<div class="titles"><h1>{e(heading)}</h1>{sub_div}{status_line}</div>'
    menu = (f'<button type="button" id="menu" aria-label="Conversations" aria-controls="side" aria-expanded="false"'
            f'{"" if paired else " hidden"}>{MENU_ICON}{badge}</button>')
    if part:
        return f'<!doctype html><html><body>{side}<header id="top">{menu}{titles}</header><main>{body}</main></body></html>'
    sheet = "".join(f'<button type="button" data-emoji="{e(x)}">{e(x)}</button>' for x in REACTIONS)
    return f"""<!doctype html>
<html lang="en"{' class="convo"' if footer else ""}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{f"({bar['unread']}) " if bar["unread"] else ""}{e(title)}</title>
{f'<link rel="manifest" href="/manifest.webmanifest?k={e(SITE["key"])}">' if paired else ""}
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/icon-64.png" type="image/png" sizes="64x64">
{_theme_metas()}
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{e(_app_name())}">
<style>{SITE["look"].css()}{_style()}{"".join(getattr(c, "style", "") for c in SITE["controls"].values())}</style>
</head>
<body data-vapid="{e(SITE['vapid_public_key'])}" data-app="{e(_app_name())}" data-owner="{e(_owner())}" data-unread="{bar['unread']}">
{side}
<div id="pane">
<header id="top">
{menu}
{titles}
{actions}
{'<button type="button" class="btn small" id="notify-on" hidden>Turn on notifications</button>' if paired else ""}
<p id="screen-note" class="error" hidden></p>
</header>
<main{' id="scroller"' if footer else ""}>
<p id="notify-hint" class="notice"></p>
<p id="unsent" class="notice" hidden></p>
<p id="elsewhere" class="notice" hidden></p>
{body}
</main>
{'<div class="pill-anchor"><button type="button" id="new-pill" hidden>New messages</button></div>' if footer else ""}
{f'<footer id="composer">{footer}</footer>' if footer else ""}
</div>
{CONVO_SCRIPT + LIVE_THREAD if footer else ""}
<div id="act-sheet" hidden><div class="sheet" role="dialog" aria-label="Message">
<p class="quoted"></p>
<div class="emojis">{sheet}<button type="button" data-emoji="+" aria-label="Another emoji">+</button></div>
<div class="actions"></div>
<div class="row"><button type="button" class="btn" data-close-sheet>Cancel</button></div></div></div>
{SHELL_SCRIPT if paired else ""}
{APP_SCRIPT if paired else ""}
{CONTROL_SCRIPT if paired else ""}
{_plugin_scripts() if paired else ""}
{PLAYBACK if paired else ""}
</body>
</html>
"""


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def _spoken_by_owner(item: dict, audio_url: str, files_base: str) -> str:
    """Text, recording, transcript state, and attached files of something the owner sent."""
    parts = []
    if item["text"]:
        parts.append(f'<div class="text">{markdown.to_html(item["text"])}</div>')
    if item["audio"]:
        parts.append(f'<audio controls preload="none" src="{e(audio_url)}" data-title="Your recording" '
                     f'data-artist="{e(_owner())}"></audio>')
        state = item["transcript_status"]
        if state == "done":
            parts.append(f'<div class="text transcript"><span class="label">Transcript</span>'
                         f'{e(item["transcript"])}</div>')
        elif state == "pending":
            parts.append('<div class="meta">Transcribing...</div>')
        else:
            parts.append(f'<p class="error">Transcription failed: {e(item["transcript_error"])}</p>')
        takes = item.get("takes", [])
        for n, take in enumerate(takes, 1):
            if (c := take.get("capture")) and c["raw"]:
                heard = [f"{c['rate'] / 1000:g} kHz" if c["rate"] else "rate not reported",
                         f"{c['channels']} channel{'' if c['channels'] == 1 else 's'}" if c["channels"]
                         else "channels not reported"] + ([c["mic"]] if c["mic"] else [])
                which = f"Recording {n}, recorded raw" if len(takes) > 1 else "Recorded raw"
                parts.append(f'<div class="meta">{which}: {e(", ".join(heard))}</div>')
    for file in item.get("files", []):
        url = files_base + file["stored"].split("/")[-1]
        if file["type"].startswith("image/"):
            parts.append(f'<a href="{e(url)}"{NEW_TAB}><img class="thumb" src="{e(url)}" alt="{e(file["name"])}"></a>')
        elif file["type"].startswith("audio/"):
            parts.append(f'<audio controls preload="none" src="{e(url)}" data-title="{e(file["name"])}" '
                         f'data-artist="{e(_owner())}"></audio>'
                         f'<a class="file" href="{e(url)}" download="{e(file["name"])}">{e(file["name"])}, '
                         f'{_size(file["size"])}, as sent</a>')
        else:
            parts.append(f'<a class="file" href="{e(url)}"{NEW_TAB}>PDF: {e(file["name"])}</a>')
    return "\n".join(parts)


def reply_box(msg: Message, holders: dict[str, str | None]) -> str:
    """Replies to an inbox holder's message go to that holder; anything else goes through the main inbox."""
    return next((box for box, holder in holders.items() if holder == msg.sender), "main")


def _linkify(text: str) -> str:
    """The text, escaped, with its links made tappable."""
    out, pos = [], 0
    for m in URL.finditer(text):
        url = trim(m[0])
        out += [e(text[pos:m.start()]), f'<a href="{e(url)}"{NEW_TAB}>{e(url)}</a>']
        pos = m.start() + len(url)
    out.append(e(text[pos:]))
    return "".join(out)


def _acts(store: Store, msg: Message, mode: str, grouped: bool = False) -> str:
    """The owner's reactions and, on a question still waiting, Answer; then the message's menu, which a tap on it
    opens, with everything else the owner can do with it. grouped: replies fold under their thread's first
    message, so Reply says it goes in the thread."""
    title = msg.title_hint or msg.title or "New message"
    pills = "".join(f'<button type="button" class="pill on" data-react="{e(x)}" data-id="{msg.id}">{e(x)}</button>'
                    for x in store.reactions(msg.id))
    answer, tools = "", []
    if msg.ask and store.reply(msg.id) is None:
        if mode != "page":
            strong = "" if store.set_aside(msg.id) else " strong"
            answer = f'<a class="tool{strong}" href="/m/{msg.id}">Answer</a>'
            tools.append(f'<a href="/m/{msg.id}">Answer</a>')
    elif mode == "chat":
        tools.append(f'<button type="button" data-reply-to="{msg.id}" data-reply-label="{e(who(msg.sender))}: '
                     f'{e(title)}">{"Reply in thread" if grouped else "Reply"}</button>')
    elif mode == "list":
        tools.append(f'<a href="/m/{msg.id}">Reply</a>')
    tools.append(f'<button type="button" data-copy-original="{msg.id}">Copy the original</button>')
    if mode == "chat":
        tools.append(f'<a href="/m/{msg.id}">Open on its own page</a>')
    if mode in ("list", "page"):
        tools.append(f'<button type="button" class="danger" data-delete="{msg.id}">Delete</button>')
    return (f'<div class="acts"><span class="reacts" data-for="{msg.id}">{pills}</span>{answer}</div>'
            + _menu(f"{who(msg.sender)}: {title}", tools, f"Sent {_stamp(msg.created)}", msg.id))


def _menu(label: str, tools: list[str], facts: str, react: str = "") -> str:
    """A message's menu, drawn when a tap opens it. label: which message, as the sheet heads it. react: its id,
    where the owner can react to it."""
    return (f'<template class="menu" data-label="{e(label)}" data-react="{react}">{"".join(tools)}'
            f'<p class="facts">{e(facts)}</p></template>')


def control(store: Store, msg: Message) -> str:
    """The message's control as its plugin draws it now. It takes no more taps once its question is answered."""
    kind = msg.control["type"]
    plugin = SITE["controls"].get(kind)
    head = f'<div class="control" data-control="{msg.id}" data-type="{e(kind)}">'
    if plugin is None:
        return f'{head}<p class="error">This message has a {e(kind)} control, which is not installed.</p></div>'
    live = not (msg.ask and store.reply(msg.id) is not None)
    try:
        drawn = plugin.render(msg.control, store.control_state(msg.id), live)
    except Exception as err:
        log.exception("control %s on %s failed to render", kind, msg.id)
        return f'{head}<p class="error">The {e(kind)} control failed: {e(type(err).__name__)}: {e(err)}</p></div>'
    return f"{head}{drawn}</div>"


def _with_clips(msg: Message, script: str, preload: str) -> str:
    """The script's markdown as HTML: each [[audio N]] a button that plays attachment N, and each [[image N]],
    [[video N]] or [[pdf N]] the file itself. Pictures, video and PDFs not placed in the text follow it."""
    placed = set()

    def media(kind: str, n: int) -> str:
        placed.add(n)
        name = msg.attachments[n - 1].split("-", 1)[1]
        if kind == "audio":
            return (f'<button type="button" class="tool clipbtn" data-clip="{n}" aria-label="Play {e(name)}">'
                    f'{PLAY_ICON}{e(name)}</button>')
        return _media(msg, kind, n, preload)
    html = markdown.to_html(script, media)
    rest = [_media(msg, media_kind(a), n, preload) for n, a in enumerate(msg.attachments, 1)
            if n not in placed and media_kind(a) != "audio"]
    return html + "".join(rest)


def _media(msg: Message, kind: str, n: int, preload: str) -> str:
    url = f"/attachment/{msg.id}/{n}"
    name = e(msg.attachments[n - 1].split("-", 1)[1])
    if kind == "image":
        return f'<a href="{url}"{NEW_TAB}><img class="thumb" src="{url}" alt="{name}" loading="lazy"></a>'
    if kind == "video":
        return f'<video class="media" controls playsinline preload="{preload}" src="{url}" title="{name}"></video>'
    return f'<a class="file" href="{url}"{NEW_TAB}>PDF: {name}</a>'


def _article(store: Store, msg: Message, mode: str, holders: dict[str, str | None], cont: bool = False,
             unseen: set[str] | None = None, replies: str = "", grouped: bool = False) -> str:
    """mode: "list" (inbox), "page" (one message), or "chat" (a conversation, channel or thread). cont: it follows
    the same sender's message, so it goes without avatar and name. unseen: what was new to the owner before this
    view marked it seen (None: as the files say now). replies: its thread's summary, if it starts one. grouped:
    as in _acts."""
    title = msg.title_hint or msg.title or "New message"
    sender = who(msg.sender)
    heard = store.heard(msg.id) is not None
    tags = [f'<span class="tag heard" id="heard-{msg.id}"{"" if heard else " hidden"}>heard</span>']
    if msg.delivered and (msg.id in unseen if unseen is not None else store.seen(msg.id) is None):
        tags.insert(0, '<span class="tag new">new</span>')
    aside = msg.ask and store.set_aside(msg.id)
    if aside and store.reply(msg.id) is None:
        tags.append(f'<span class="tag" title="{_when(aside)}">set aside</span>')
    elif msg.ask:
        tags.append('<span class="tag asks">asks you</span>')
    if msg.status != "done":
        tags.append(f'<span class="tag status">{e(msg.status)}</span>')
    where = (f'<a class="where" href="/c/{e(msg.channel)}">#{e(msg.channel)}</a>'
             if msg.channel is not None and mode != "chat" else "")
    voice = f'<span class="time">voice {e(msg.voice)}</span>' if msg.voice and mode == "page" else ""
    gutter = f'<span class="t">{_clock(msg.created)}</span>' if cont else _avatar(msg.sender)
    parts = [f'<article class="msg {e(msg.status)}{" cont" if cont else ""}" id="m-{msg.id}">',
             f'<div class="gutter">{gutter}</div><div class="body">']
    if not cont:
        parts.append(f'<div class="line"><span class="name">{e(sender)}</span>{where}'
                     f'<a class="time" href="/m/{msg.id}" title="{_when(msg.created)}">{_clock(msg.created)}</a>'
                     f'{voice}</div>')
    if msg.re:
        parts.append(_re_line(msg.re))
    speak = ""
    if msg.audio is None and msg.status == "done":
        speak = (f'<button type="button" class="tool" data-speak="{msg.id}" data-title="{e(title)}" '
                 f'data-artist="{e(sender)}" aria-label="Voice it" title="Voice it">{SPEAK_ICON}</button>')
    parts.append(f'<div class="subject"><a href="/m/{msg.id}">{e(title)}</a>{"".join(tags)}{speak}</div>')
    if msg.error:
        parts.append(f'<p class="error">{e(msg.error)}</p>')
    if msg.notify_error and mode == "page":
        parts.append(f'<p class="meta">Notification failed: {e(msg.notify_error)}</p>')
    original = store.read_text(msg.id, "original.md")
    # Every link in the original is listed, tappable: a rewritten script never reads them aloud.
    if links := urls(original):
        parts.append('<ul class="links">' + "".join(f'<li><a href="{e(u)}"{NEW_TAB}>{e(u)}</a></li>' for u in links)
                     + "</ul>")
    if msg.audio:
        preload = "auto" if mode == "page" else "none"
        already = ' data-heard-already="1"' if heard else ""
        parts.append(f'<audio controls preload="{preload}" src="/audio/{msg.id}" data-id="{msg.id}" '
                     f'data-heard="/api/messages/{msg.id}/heard"{already} '
                     f'data-title="{e(title)}" data-artist="{e(sender)}"></audio>')
    script = store.read_text(msg.id, "script.txt")
    if script is not None:
        parts.append(f'<div class="text">{_with_clips(msg, script, "metadata" if mode == "page" else "none")}</div>')
    if msg.control is not None:
        parts.append(control(store, msg))
    parts.append(f'<details><summary>Original</summary><pre>{_linkify(original)}</pre></details>')
    if msg.attachments:
        def original(n: int, stored: str) -> str:
            name = e(stored.split("-", 1)[1])
            if media_kind(stored) != "audio":
                return f'<div class="clip">{n}. <a href="/attachment/{msg.id}/{n}" download="{name}">{name}</a></div>'
            return (f'<div class="clip">{n}. {name}</div><audio controls preload="none" src="/attachment/{msg.id}/{n}" '
                    f'data-n="{n}" data-title="{name}" data-artist="{e(sender)}"></audio>')
        kinds = {media_kind(a) for a in msg.attachments}
        summary = "Attached audio, original files" if kinds == {"audio"} else "Attached files, as sent"
        parts.append(f'<details{" open" if "audio" in kinds else ""}><summary>{summary}</summary>'
                     f'{"".join(original(n, a) for n, a in enumerate(msg.attachments, 1))}</details>')
    parts.append(_acts(store, msg, mode, grouped))
    parts.append(replies)
    reply = store.reply(msg.id)
    if reply is not None:
        body = (f'<div class="big">{e(reply["text"])}</div>' if reply.get("reaction")
                else _spoken_by_owner(reply, f"/audio/{msg.id}/reply", f"/files/reply/{msg.id}/"))
        parts.append(f'<div class="answer">{_owner_avatar()}<div><div class="line"><span class="name">Your answer</span>'
                     f'<span class="time" title="{_when(reply["at"])}">{_clock(reply["at"])}</span></div>{body}</div></div>')
    elif msg.ask and mode == "page":
        parts.append(_recorder(f"Answer {sender}" + (", recorded raw" if msg.raw else "")
                               + (" (set aside; they were told)" if aside else ""),
                               f"/api/messages/{msg.id}/drafts", f"/api/messages/{msg.id}/reply",
                               f"/api/messages/{msg.id}/files", raw=msg.raw))
    elif mode == "page" and msg.channel is not None:
        parts.append(f'<p><a href="/c/{e(msg.channel)}#m-{msg.id}">Open #{e(msg.channel)}</a></p>')
        api = f"/api/channels/{msg.channel}"
        parts.append(_recorder(f"Reply in #{msg.channel}", f"{api}/drafts", f"{api}/messages", f"{api}/files",
                               msg.id))
    elif mode == "page":
        box = reply_box(msg, holders)
        if holders[box] == msg.sender:
            parts.append(f'<p><a href="{CHAT_PATH[box]}#m-{msg.id}">Open the conversation</a></p>')
        to = holders[box]
        heading = f"Reply (goes to {to})" if to else f"Reply (waits for the {BOX_LABEL[box]})"
        seg = API_SEGMENT[box]
        parts.append(_recorder(heading, f"/api/{seg}/drafts", f"/api/{seg}/messages", f"/api/{seg}/files", msg.id))
    parts.append("</div></article>")
    return "\n".join(parts)


def _bytes(n: int) -> str:
    return f"{n:,} bytes"


def _bang_stream(m: dict, where: str, name: str, text: str, cut: dict | None) -> str:
    """One of a command's streams as shown: capped, with what was cut and where the whole of it is."""
    if not text and not cut:
        return ""
    body = f'<pre>{e(text)}</pre>' if text else ""
    note = ""
    if cut:
        saved = (f"; only the first {_bytes(cut['saved_bytes'])} were kept" if cut["saved_bytes"] < cut["total_bytes"]
                 else "")
        note = (f'<div class="cut">Showing {_bytes(cut["shown_bytes"])} of {_bytes(cut["total_bytes"])} '
                f'({e(cut["kept"])}){saved}. <a href="/bang/{e(where)}/{m["id"]}/{name}">Whole {name}</a></div>')
    return f'<div class="out{" err" if name == "stderr" else ""}"><span class="label">{name}</span>{body}{note}</div>'


def _bang_item(m: dict, where: str, state: str, cont: bool, chips: str) -> str:
    """A command the owner typed with !, and what became of it: waiting, running, its result, or why it didn't run."""
    b = m["bang"]
    gutter = f'<span class="t">{_clock(m["at"])}</span>' if cont else _owner_avatar()
    head = "" if cont else (f'<div class="line"><span class="name">{e(_owner())}</span>'
                            f'<span class="time" title="{_when(m["at"])}">{_clock(m["at"])}</span></div>')
    live = b["state"] in ("queued", "running")
    bad = b["state"] != "done" or b["exit"] != 0 or b["timed_out"]
    if b["state"] == "queued":
        result = f'Waiting for the session to pick it up (gives up after {b["expire_s"]} s)'
    elif b["state"] == "running":
        result = "Running..."
    elif b["state"] == "done":
        result = f'Exit {b["exit"]} in {b["duration_s"]:g} s, in {e(b["cwd"])}'
        if b["timed_out"]:
            result = f'Timed out after {b["timeout_s"]} s and was killed. {result}'
        if b["signal"] and not b["timed_out"]:
            result += f' ({e(b["signal"])})'
        if b["lingering"]:
            result += ". A background process still held its output open; later output isn't here"
    else:
        result = e(b["reason"] or b["state"])
    streams = ""
    if b["state"] == "done":
        cut = b["truncated"] or {}
        streams = (_bang_stream(m, where, "stdout", b["stdout"], cut.get("stdout"))
                   + _bang_stream(m, where, "stderr", b["stderr"], cut.get("stderr")))
        if not streams:
            streams = '<div class="cut">No output.</div>'
    seen = ""
    if b["state"] == "done":
        seen = (f'Picked up by {m["delivered_to"]} {_when(m["delivered_at"])}' if m["delivered_at"]
                else "Waiting for the agent's next check")
    return (f'<article class="msg owner bang{" live" if live else ""}{" cont" if cont else ""}" id="g-{m["id"]}">'
            f'<div class="gutter">{gutter}</div><div class="body">{head}<pre class="cmd">{e(m["text"])}</pre>'
            f'<div class="result{" bad" if bad else ""}">{result}</div>{streams}'
            f'{f"<div class=acts>{chips}</div>" if chips else ""}<div class="state">{e(seen)}</div></div></article>')


def _owner_item(m: dict, cont: bool = False, replies: str = "") -> str:
    """m: as the index gives it, with its folder. cont and replies: as in _article."""
    where = _seg_of_folder(m["folder"])
    if ev := m.get("event"):  # a program's report (e.g. a Done tap), shown as a quiet line
        return (f'<div class="event" id="g-{m["id"]}">{_clock(m["at"])} · {e(ev["source"])}: '
                f'{e(ev["summary"])}</div>')
    got = True  # picked up: a check by the time, and the details in the menu
    if where.startswith("c/"):
        given = list(dict.fromkeys(d["to"] for d in m["deliveries"]))
        got = bool(given)
        state = (f"Picked up by {', '.join(given)}" if given
                 else "Transcribing" if m["transcript_status"] == "pending" else "Not picked up yet")
    elif m["delivered_at"]:
        state = f"Picked up by {m['delivered_to']} {_stamp(m['delivered_at'])}"
    elif m["transcript_status"] == "pending":
        got = False
        state = "Transcribing"
    else:
        got = False
        state = "Waiting to be picked up"
        box = next((b for b, seg in API_SEGMENT.items() if seg == where), None)
        if box is not None:
            state += f" · {SITE['presence'](box)[1]}"
    re_ = m.get("re")
    target = ""
    if re_:  # what a reaction or tap was on: always an agent's message
        href = f"/c/{where[2:]}#m-{re_['id']}" if where.startswith("c/") else f"/m/{re_['id']}"
        target = (f'<a href="{e(href)}">{e(who(re_["sender"]))}: {e(re_["title"] or "message")}</a>')
    chips = "".join(f'<span class="pill">{e(r["emoji"])} <small>{e(who(r["by"]))}</small></span>'
                    for r in m.get("reactions", []))
    if m.get("bang"):
        return _bang_item(m, where, state, cont, chips)
    if m.get("reaction"):
        return (f'<div class="event" id="g-{m["id"]}">You reacted <span class="emoji">{e(m["text"])}</span> to '
                f'{target} · {_clock(m["at"])} · {e(state)}{chips}</div>')
    if m.get("tap"):
        return (f'<div class="event" id="g-{m["id"]}">You tapped: <b>{e(m["text"])}</b> on {target} · '
                f'{_clock(m["at"])} · {e(state)}{chips}</div>')
    gutter = f'<span class="t">{_clock(m["at"])}</span>' if cont else _owner_avatar()
    mark = f'<span class="got" title="{e(state)}" aria-label="{e(state)}">{CHECK_ICON}</span>' if got else ""
    head = "" if cont else (f'<div class="line"><span class="name">{e(_owner())}</span>'
                            f'<span class="time" title="{_when(m["at"])}">{_clock(m["at"])}</span>{mark}</div>')
    quote = _re_line(re_) if re_ else ""
    words = _spoken_by_owner(m, f"/audio/{where}/{m['id']}", f"/files/{where}/{m['id']}/")
    label = f"{_owner()}: {excerpt(m) or 'a recording'}"
    tools = [f'<button type="button" data-reply-to="{m["id"]}" data-reply-label="{e(label)}">Reply</button>']
    said = m["text"] or (m["transcript"] if m["transcript_status"] == "done" else None)
    if said:
        tools.append(f'<a href="/rules?pin={e(m["id"])}&amp;where={e(where)}">Pin as rule</a>')
        tools.append(f'<button type="button" data-copy-text="{e(said)}">Copy text</button>')
    menu = _menu(label, tools, f"Sent {_stamp(m['at'])}. {state}.")
    acts = f'<div class="acts">{chips}</div>' if chips else ""
    state_line = "" if got else f'<div class="state">{e(state)}</div>'
    return (f'<article class="msg owner{" cont" if cont else ""}" id="g-{m["id"]}"><div class="gutter">{gutter}</div>'
            f'<div class="body">{head}{quote}{words}{acts}{state_line}{menu}{replies}</div></article>')


def _root_ref(store: Store, key: str) -> dict | None:
    """A thread's first message as a reply's `re` names it, or None once it is deleted."""
    if key.startswith("m-"):
        if not store.exists(key[2:]):
            return None
        t = store.load(key[2:])
        return {"id": t.id, "kind": "agent", "sender": t.sender, "title": t.title_hint or t.title}
    found = store.index.rows("SELECT meta FROM owner WHERE id = ?", (key[2:],))
    return {"id": key[2:], "kind": "owner", "sender": "owner", "title": excerpt(json.loads(found[0][0]))} \
        if found else None


def _activity(store: Store, key: str, kind: str, item, summary: dict, fresh: int) -> str:
    """The grouped view's one row for a thread whose replies fold: its newest reply's place in the timeline.
    It carries that reply's row id, and the ids of every reply here it stands for in data-covers, so a link to
    any of them lands here. The counts are the whole thread's."""
    if kind == "agent":
        row, at, name = f"m-{item.id}", item.created, who(item.sender)
        said, face = item.title_hint or item.title or "New message", _avatar(item.sender)
    else:
        row, at, name = f"g-{item['id']}", item["at"], _owner()
        said, face = excerpt(item) or "a recording", _owner_avatar()
    ref = _root_ref(store, key)
    if ref is None:
        about = "Its first message was deleted"
    else:
        rname, title = (_owner(), ref["title"] or "a recording") if ref["kind"] == "owner" else \
            (who(ref["sender"]), ref["title"] or "message")
        about = f"In thread {e(rname)}: <span>{e(title)}</span>"
    return (f'<article class="msg activity" id="{row}" data-covers="{" ".join(summary["rows"])}">'
            f'<div class="gutter">{face}</div><div class="body">'
            f'<div class="line"><span class="name">{e(name)}</span>'
            f'<span class="time" title="{_when(at)}">{_clock(at)}</span></div>'
            f'<div class="quote"><a href="/t/{e(key)}">{about}</a></div>'
            f'<a class="what" href="/t/{e(key)}#{row}">{e(said)}</a>'
            f'{_replies(key, summary, fresh, row)}</div></article>')


def _thread(store: Store, items: list, mode: str, holders: dict[str, str | None], unseen: set[str] | None = None,
            root: str | None = None, nested=None) -> str:
    """items: ("agent", Message) or ("owner", dict), in the order shown, with a line for each day. unseen: as in
    _article. root: on a thread's own page, the thread, whose first message needs no summary. nested(key): for
    the grouped view, the rows of a thread's replies shown here, oldest first, if its first message shows here
    (else empty); they fold under it, and the summary there says how many are new. Each folded thread keeps one
    row, at its newest reply shown here (_activity), so what was just said is where the owner is looking. None: every reply shows where it was sent, quoting what
    it answers."""
    folded: dict[str, list[str]] = {}

    def item_row(kind: str, item) -> str:
        return f"m-{item.id}" if kind == "agent" else f"g-{item['id']}"

    def folds(kind: str, item) -> str | None:
        """The thread a reply folds into, if it does."""
        if nested is None or root is not None:
            return None
        if kind == "agent":
            re_ = item.re
        else:
            re_ = None if item.get("event") or item.get("reaction") or item.get("tap") else item.get("re")
        if not re_:
            return None
        key = thread_key(re_)
        if key not in folded:
            folded[key] = nested(key)
        return key if item_row(kind, item) in folded[key] else None

    into = {item_row(kind, item): folds(kind, item) for kind, item in items}
    wanted = [row for row, thread in into.items() if thread is None] + [t for t in into.values() if t]
    summaries = store.index.threads(list(dict.fromkeys(wanted)))
    shown = []
    for kind, item in items:
        row, thread = item_row(kind, item), into[item_row(kind, item)]
        if thread is None:
            shown.append((kind, item, None))
        elif folded[thread][-1] == row:
            shown.append((kind, item, thread))

    def fresh_in(summary: dict) -> int:
        return sum(1 for i in summary["ids"] if (i in unseen if unseen is not None else store.seen(i) is None))

    out, day, prev = [], None, None
    for kind, item, folds_into in shown:
        at = datetime.fromisoformat(item.created if kind == "agent" else item["at"])
        if at.date() != day:
            day, prev = at.date(), None
            out.append(f'<div class="day" role="separator">{_day_label(day)}</div>')
        if folds_into is not None:
            summary = summaries[folds_into]
            out.append(_activity(store, folds_into, kind, item, {**summary, "rows": folded[folds_into]},
                                 fresh_in(summary)))
            prev = None
            continue
        if kind == "agent":
            sender = (item.sender, item.channel)
        else:
            sender = None if item.get("event") or item.get("reaction") or item.get("tap") else (_owner(), None)
        cont = sender is not None and prev is not None and prev[0] == sender and abs(at - prev[1]) <= GROUP_GAP
        key = item_row(kind, item)
        summary = summaries.get(key)
        fresh = fresh_in(summary) if nested is not None and summary else 0
        replies = "" if key == root else _replies(key, summary, fresh)
        out.append(_article(store, item, mode, holders, cont, unseen, replies, nested is not None) if kind == "agent"
                   else _owner_item(item, cont, replies))
        if key == root:
            n = summaries.get(root, {"n": 0})["n"]
            out.append(f'<div class="day replies-sep" role="separator" id="sep-{e(key)}">'
                       f'{n or "No"} repl{"y" if n == 1 else "ies"}</div>')
            prev = None
        if key != root:
            prev = (sender, at) if sender is not None else None
    return "\n".join(out)


def list_rows(store: Store, msgs: list[Message], holders: dict[str, str | None],
              unseen: set[str] | None = None) -> str:
    """A page of Messages, newest first. unseen: as in _article."""
    return _thread(store, [("agent", m) for m in msgs], "list", holders, unseen)


def _list(store: Store, holders: dict[str, str | None], msgs: list[Message], more_url: str,
          unseen: set[str] | None = None) -> str:
    """The first page of a list; the page's script fetches the rest from more_url as the owner scrolls."""
    more = (f'<p class="more meta" id="more" data-url="{e(more_url)}">Loading older messages...</p>'
            if more_url else "")
    return f'<div class="thread list" id="list">{list_rows(store, msgs, holders, unseen)}</div>{more}'


def inbox(store: Store, holders: dict[str, str | None], msgs: list[Message], n: int, fresh: int, more_url: str,
          sender: str | None = None, unseen: set[str] | None = None) -> str:
    """Every message, newest first: msgs, the first page of n (fresh of them new). sender: only that
    sender's direct messages. unseen: as in _article."""
    sub = f"{n} message{'' if n == 1 else 's'}" + (f", {fresh} new" if fresh else "")
    sub_html = f'<span id="list-sub" data-n="{n}" data-new="{fresh}">{e(sub)}</span>'
    if not msgs:
        body = ('<div class="empty"><div class="mark">0</div>No messages yet.</div>'
                if sender is None else '<div class="empty">Nothing here yet.</div>')
    else:
        body = _list(store, holders, msgs, more_url, unseen)
    if sender is None:
        return _page(_app_name(), body, "Messages", sub_html=sub_html)
    return _page(f"Messages from {who(sender)}", body, who(sender), sub_html=sub_html)


def message(store: Store, msg_id: str, holders: dict[str, str | None]) -> str:
    # No auto-refresh: every message page has a recorder that a reload would wipe.
    msg = store.load(msg_id)
    title = msg.title_hint or msg.title or "New message"
    sub = who(msg.sender) + (f" in #{msg.channel}" if msg.channel is not None else "")
    key = f"m-{msg.id}"
    replies = _replies(key, store.index.threads([key]).get(key))
    return _page(title, f'<div class="thread page">{_article(store, msg, "page", holders, replies=replies)}</div>',
                 title, sub)


def _typing(typing: list[str]) -> str:
    """Under the last message: who is writing a post here."""
    if not typing:
        return ""
    names = [who(a) for a in typing]
    text = f"{names[0]} is typing" if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]} are typing"
    return f'<div class="typing" id="typing"><span class="dot pulse" aria-hidden="true"></span>{e(text)}</div>'


def thread_rows(store: Store, items: list, holders: dict[str, str | None], unseen: set[str] | None = None,
                root: str | None = None, nested=None) -> str:
    """Rows of a conversation, channel or thread (root), oldest first. nested: as in _thread."""
    return _thread(store, items, "chat", holders, unseen, root, nested)


def _conversation(store: Store, items: list, oldest: str, more: bool, holders: dict[str, str | None],
                  query: str, version: str, composer: str, top: str, empty: str, typing: list[str],
                  unseen: set[str] | None, root: str | None = None, nested=None) -> tuple[str, str]:
    """The scrolling body and the footer under it. oldest: the first item's cursor; more: whether older items
    remain, which the page's script loads as the owner scrolls up. query: what the page shows, "box=builder",
    "channel=general" or "thread=m-<id>". root: a thread page's thread. nested: as in _thread."""
    thread = thread_rows(store, items, holders, unseen, root, nested) or f'<div class="empty">{empty}</div>'
    older = f'<p class="older meta" id="older"{"" if more else " hidden"}>Loading earlier messages...</p>'
    return (f'{top}{older}<div class="thread" id="thread" data-version="{e(version)}" '
            f'data-version-url="/api/chat/version?{e(query)}" data-older-url="/api/thread?{e(query)}" '
            f'data-from="{e(oldest)}" data-more="{"1" if more else ""}">{thread}{_typing(typing)}</div>',
            CHAT_STATUS + composer)


def chat(store: Store, box: str, info: dict, presence: str, voice: str | None, items: list, oldest: str,
         more: bool, version: str, holders: dict[str, str | None], status: dict | None, typing: list[str],
         unseen: set[str] | None = None, part: bool = False, nested=None) -> str:
    """One inbox's conversation. presence: who is there to pick up what the owner sends (App.presence). items:
    ("agent", Message) or ("owner", dict), oldest first; oldest and more: as in _conversation. status: the holder's
    {text, at}, or None. typing: the holder, while it is typing here. unseen: as in _article. part: as in _page.
    nested: as in _thread."""
    holder, label = info["holder"], BOX_LABEL[box]
    seg = API_SEGMENT[box]
    if holder is None:
        heading, sub, top = label[0].upper() + label[1:], "Nobody holds this inbox right now", ""
        empty = "No agent holds this inbox. Messages wait here for the next one."
    else:
        heading = who(holder)
        sub = " · ".join(([label] if holder.lower() != label.lower() else []) + ([f"voice {voice}"] if voice else [])
                         + [presence])
        top = f'<p class="about">{e(info["about"])}</p>' if info["about"] else ""
        empty = "Nothing yet."
    composer = _recorder(f"Message {who(holder) if holder else 'the ' + label}", f"/api/{seg}/drafts",
                         f"/api/{seg}/messages", f"/api/{seg}/files",
                         bang=who(holder) if holder and box != "main" and SITE["bang"]() else "",
                         to=f"to {who(holder)}" if holder else f"to the {label}")
    body, footer = _conversation(store, items, oldest, more, holders, f"box={box}", version, composer,
                                 top, empty, typing, unseen, nested=nested)
    return _page(f"Conversation with {holder or 'the ' + label}", body, heading, sub, footer=footer,
                 status=status, part=part)


def channel(store: Store, ch, listening: list[str], items: list, oldest: str, more: bool, version: str,
            holders: dict[str, str | None], statuses: dict[str, dict], typing: list[str],
            unseen: set[str] | None = None, part: bool = False, nested=None) -> str:
    """One channel's thread. items: ("agent", Message) or ("owner", dict), oldest first; oldest and more: as in
    _conversation. statuses: sender -> {text, at}, shown with who's listening. typing: agents writing a post here.
    unseen: as in _article. part: as in _page. nested: as in _thread."""
    def ear(a: str) -> str:
        dot = f'<span class="dot{" pulse" if a in typing else ""}" title="{"typing" if a in typing else "listening"}"></span>'
        return f'<span class="ear">{dot}{e(who(a) + (": " + statuses[a]["text"] if a in statuses else ""))}</span>'

    ears = "; ".join(ear(a) for a in listening) + " listening" if listening else "no agent listening right now"
    sub = " · ".join(([e(ch.info["topic"])] if ch.info["topic"] else []) + [ears])
    api = f"/api/channels/{ch.name}"
    composer = _recorder(f"Message #{ch.name}", f"{api}/drafts", f"{api}/messages", f"{api}/files",
                         to=f"in #{ch.name}")
    empty = (f'<div class="mark">#</div><b>#{e(ch.name)}</b> is quiet. What agents post here, and what you '
             'post, shows up here.')
    body, footer = _conversation(store, items, oldest, more, holders, f"channel={ch.name}",
                                 version, composer, "", empty, typing, unseen, nested=nested)
    return _page(f"#{ch.name}", body, f"#{ch.name}", "", footer=footer, sub_html=sub,
                 part=part)


def thread_page(store: Store, key: str, root, place: str, re_id: str | None, items: list, oldest: str, more: bool,
                version: str, holders: dict[str, str | None], unseen: set[str] | None = None,
                part: bool = False) -> str:
    """One thread: its first message, then every reply, wherever each was sent from. root: ("agent", Message) or
    ("owner", dict), or None once it is deleted. place: where the owner's replies go, "#<channel>" or an inbox.
    re_id: what they answer unless the owner picks a message (None: nothing left to answer). items, oldest, more:
    as in _conversation."""
    if root is None:
        about = "Its first message was deleted"
    elif root[0] == "agent":
        about = f"{who(root[1].sender)}: {root[1].title_hint or root[1].title or 'New message'}"
    else:
        about = f"{_owner()}: {excerpt(root[1]) or 'a recording'}"
    if place.startswith("#"):
        api, where = f"/api/channels/{place[1:]}", f"in #{place[1:]}"
        heading, to = f"Reply in this thread, in {place}", f"in {place}"
    else:
        api, holder = f"/api/{API_SEGMENT[place]}", holders[place]
        where = f"with {who(holder)}" if holder else f"in the {BOX_LABEL[place]}'s inbox"
        heading = f"Reply in this thread (goes to {who(holder)})" if holder else \
            f"Reply in this thread (waits for the {BOX_LABEL[place]})"
        to = f"to {who(holder)}" if holder else f"to the {BOX_LABEL[place]}"
    # Only a question asked in a channel and not yet answered leaves nothing to reply to: answer it first.
    composer = (_recorder(heading, f"{api}/drafts", f"{api}/messages", f"{api}/files", thread=re_id, to=to)
                if re_id
                else f'<p class="answer-first"><a class="btn primary" href="/m/{e(key[2:])}">Answer the question</a> '
                     'Replies go in its thread once it is answered.</p>')
    body, footer = _conversation(store, items, oldest, more, holders, f"thread={key}", version, composer, "",
                                 "Nothing in this thread.", [], unseen, key)
    return _page(f"Thread: {about}", body, "Thread", f"{about} · {where}", footer=footer, part=part)


SETTINGS_SCRIPT = """
<script>
(() => {
  const form = document.getElementById("settings");
  const note = document.getElementById("settings-note");
  const show = (view) => {
    document.getElementById("session-tokens").textContent = view.session_tokens;
    document.getElementById("rules-now").textContent = view.costs.rules_now;
  };
  form.addEventListener("change", async (ev) => {
    const el = ev.target;
    const [key, tool] = el.name.split(".");
    const names = (text) => text.split(",").map((n) => n.trim()).filter((n) => n);
    let value = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value)
      : el.dataset.kind === "names" ? names(el.value) : el.value;
    const body = JSON.stringify(tool ? { tools: { [tool]: value } } : { [key]: value });
    el.disabled = true;
    note.textContent = "";
    try {
      const r = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      show(out);
    } catch (err) {
      if (el.type === "checkbox") el.checked = !el.checked;
      note.textContent = `Couldn't change ${el.name}: ${err.message}`;
    } finally {
      el.disabled = false;
    }
  });
  const board = document.getElementById("board");
  const boardNote = document.getElementById("board-note");
  const names = (text) => text.split(",").map((n) => n.trim()).filter((n) => n);
  board.addEventListener("submit", async () => {
    const groups = [];
    for (const [i, line] of board.elements.groups.value.split("\\n").entries()) {
      if (!line.trim()) continue;
      const at = line.indexOf(":");
      if (at < 0) {
        boardNote.textContent = `Line ${i + 1} needs a colon after the group's name.`;
        return;
      }
      groups.push({ name: line.slice(0, at).trim(), members: names(line.slice(at + 1)) });
    }
    const btn = board.querySelector("button");
    btn.disabled = true;
    boardNote.textContent = "";
    try {
      const r = await fetch("/api/board", { method: "POST", headers: { "Content-Type": "application/json" },
                                            body: JSON.stringify({ groups, hidden: names(board.elements.hidden.value) }) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      boardNote.textContent = "Saved.";
    } catch (err) {
      boardNote.textContent = `Couldn't save the board: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });
})();
</script>
"""

TOKENS_SCRIPT = """
<script>
(() => {
  const note = document.getElementById("tokens-note");
  const post = async (url, body) => {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body) });
    const out = await r.json();
    if (!r.ok) throw new Error(out.error || r.statusText);
    return out;
  };
  const act = async (btn, ask, url, body, what) => {
    if (ask && !confirm(ask)) return;
    btn.disabled = true;
    try {
      await post(url, body);
      location.reload();
    } catch (err) {
      note.textContent = `Couldn't ${what}: ${err.message}`;
      btn.disabled = false;
    }
  };
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    if (btn.dataset.act === "approve")
      act(btn, null, `/api/enroll/${id}/approve`, {}, "approve it");
    else if (btn.dataset.act === "deny")
      act(btn, null, `/api/enroll/${id}/deny`, {}, "deny it");
    else if (btn.dataset.act === "appoint")
      act(btn, `Make ${btn.dataset.name} the approver? It approves the agents and programs that ask for a token, ` +
               "in place of you.", "/api/approver", { token: id }, "make it the approver");
    else if (btn.dataset.act === "unappoint")
      act(btn, "Have no approver? You then approve every request yourself.", "/api/approver", { token: null },
          "change the approver");
    else if (btn.dataset.act === "revoke")
      act(btn, "Revoke this token? The agent using it is refused from its next request.",
          `/api/tokens/${id}/revoke`, {}, "revoke the token");
  });
})();
</script>
"""


RULES_SCRIPT = """
<script>
(() => {
  const note = document.getElementById("rules-note");
  const post = async (url, body) => {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(body) });
    const out = await r.json();
    if (!r.ok) throw new Error(out.error || r.statusText);
    return out;
  };
  document.getElementById("add-rule").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    f.querySelector("button").disabled = true;
    try {
      await post("/api/rules", { text: f.text.value, for: f.for.value || null, from: f.from.value || null });
      location.href = "/rules";
    } catch (err) {
      note.textContent = `Couldn't add the rule: ${err.message}`;
      f.querySelector("button").disabled = false;
    }
  });
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-rule]");
    if (!btn || !confirm("Remove this rule? Agents stop following it from their next result.")) return;
    btn.disabled = true;
    try {
      await post(`/api/rules/${btn.dataset.rule}/delete`, {});
      btn.closest(".rule").remove();
    } catch (err) {
      note.textContent = `Couldn't remove the rule: ${err.message}`;
      btn.disabled = false;
    }
  });
})();
</script>
"""


NOTIFICATIONS_SCRIPT = """
<script>
(() => {
  const form = document.getElementById("hush");
  const note = document.getElementById("hush-note");
  form.addEventListener("change", async () => {
    const quiet = form.quiet_on.checked ? { start: form.quiet_start.value, end: form.quiet_end.value } : null;
    const body = { muted: [...form.querySelectorAll("[data-mute]:checked")].map((x) => x.dataset.mute),
                   questions_only: form.questions_only.checked, quiet };
    note.textContent = "";
    try {
      const r = await fetch("/api/hush", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify(body) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      note.textContent = "Saved.";
    } catch (err) {
      note.textContent = `Couldn't save: ${err.message}. Reload to see what is saved.`;
    }
  });
})();
</script>
"""


def notifications_page(view: dict) -> str:
    """view: App.hush_view()."""
    now = view["hush"]
    quiet = now["quiet"] or {"start": "22:00", "end": "07:00"}
    muted = {n.casefold() for n in now["muted"]}

    def row(label: str, about: str, ctl: str) -> str:
        return (f'<div class="row"><div class="about"><b>{e(label)}</b><p class="meta">{e(about)}</p></div>'
                f'<div class="ctl">{ctl}</div></div>')

    def mute(name: str) -> str:
        shown = name if name.startswith("#") else who(name)
        on = name.casefold() in muted
        box = f'<input type="checkbox" data-mute="{e(name)}"{" checked" if on else ""} aria-label="Mute {e(shown)}">'
        return row(shown, "Muted: no notifications for its messages." if on else "", box)

    lines = ['<p class="meta">These decide whether your phone is notified. Every message still arrives in the '
             'app. Notifications for permission prompts and reactions have their own switches.</p>'
             '<p id="hush-note" class="error"></p>',
             row("Only questions", "Notify only for questions and failures. Other messages wait in the app.",
                 f'<input type="checkbox" name="questions_only"{" checked" if now["questions_only"] else ""} '
                 f'aria-label="Only questions">'),
             row("Quiet hours", "Hold notifications between these times, service local time. When they end you get "
                 "one summary of what still needs you, and overdue escalations go out then.",
                 f'<input type="checkbox" name="quiet_on"{" checked" if now["quiet"] else ""} aria-label="Quiet hours">'
                 f'<input type="time" name="quiet_start" value="{e(quiet["start"])}" aria-label="From">'
                 f'<input type="time" name="quiet_end" value="{e(quiet["end"])}" aria-label="Until">')]
    for title, names in (("Mute an agent", view["agents"]), ("Mute a channel", view["channels"]),
                         ("Muted, not seen lately", view["stale"])):
        if names:
            lines.append(f"<h2>{e(title)}</h2>" + "".join(mute(n) for n in names))
    return _page("Notifications", f'<form class="form" id="hush" onsubmit="return false">{"".join(lines)}</form>'
                 + NOTIFICATIONS_SCRIPT, "Notifications")


def _tokens(n: int) -> str:
    return f'<span class="tok">+{n} tokens</span>' if n else ""


def _look_form(look: dict) -> str:
    """The theme, as swatches of its light and dark, and the appearance."""
    def half(c: dict) -> str:
        return (f'<span class="half" style="background: {c["bg"]}"><i style="background: {c["side"]}"></i>'
                f'<b style="--dot: {c["accent"]}"></b></span>')
    picks = "".join(
        f'<button type="button" class="theme-pick" role="radio" '
        f'aria-checked="{"true" if name == look["theme"] else "false"}" data-theme="{e(name)}"><span class="swatch" aria-hidden="true">{half(t["light"])}{half(t["dark"])}</span>'
        f'<span class="name">{e(name)}</span></button>' for name, t in look["themes"].items())
    opts = "".join(f'<option value="{a}"{" selected" if a == look["appearance"] else ""}>{label}</option>'
                   for a, label in (("auto", "Match the device"), ("light", "Light"), ("dark", "Dark")))
    return (f'<form class="form" id="look" onsubmit="return false"><h2>Look</h2>'
            f'<div class="themes" role="radiogroup" aria-label="Theme">{picks}</div>'
            f'<div class="row"><div class="about"><b>Appearance</b><p class="meta">Each theme has a light and a dark '
            f'side. Your own themes go in the config ([theme.custom]).</p></div><div class="ctl">'
            f'<select name="appearance" aria-label="appearance">{opts}</select></div></div>'
            f'<p id="look-note" class="error"></p></form>{LOOK_SCRIPT}')


LOOK_SCRIPT = """
<script>
(() => {
  const form = document.getElementById("look");
  const note = document.getElementById("look-note");
  const pick = async (patch) => {
    note.textContent = "";
    try {
      const r = await fetch("/api/theme", { method: "POST", headers: { "Content-Type": "application/json" },
                                            body: JSON.stringify(patch) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      location.reload();
    } catch (err) {
      note.textContent = `Couldn't change the look: ${err.message}`;
    }
  };
  form.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-theme]");
    if (b && b.getAttribute("aria-checked") !== "true") pick({ theme: b.dataset.theme });
  });
  form.appearance.addEventListener("change", () => pick({ appearance: form.appearance.value }));
})();
</script>
"""


def settings_page(view: dict, look: dict, board: dict, summary: list[tuple[str, str, str]]) -> str:
    """view: App.settings_view(). look: App.look_view(). board: the board's arrangement, {groups, hidden}. summary:
    configview.summary()."""
    now, by = view["settings"], view["costs"]["by"]

    def control(x: dict) -> str:
        key, value = x["key"], now[x["key"]]
        label = f' aria-label="{e(x["label"])}"'
        if x["kind"] == "choice":
            opts = "".join(f'<option value="{e(v)}"{" selected" if v == value else ""}>{e(say)}</option>'
                           for v, say in x["choices"])
            return f'<select name="{key}"{label}>{opts}</select>'
        if x["kind"] == "int":
            return (f'<input type="number" name="{key}" min="{x["lo"]}" max="{x["hi"]}" step="1" value="{value}"'
                    f'{label}>')
        if x["kind"] == "names":
            return (f'<input type="text" name="{key}" data-kind="names" value="{e(", ".join(value))}" '
                    f'placeholder="none" autocomplete="off" autocapitalize="off"{label}>')
        return f'<input type="checkbox" name="{key}"{" checked" if value else ""}{label}>'

    def row(label: str, about: str, ctl: str, tok: int | None, extra: str = "") -> str:
        return (f'<div class="row"><div class="about"><b>{e(label)}</b><p class="meta">{e(about)}</p></div>'
                f'<div class="ctl">{ctl}{"" if tok is None else _tokens(tok)}{extra}</div></div>')

    sections = []
    for sec in view["sections"]:
        rows = []
        for x in (x for x in view["schema"] if x["section"] == sec["key"]):
            agents = sec["key"] == "agents"
            extra = ('<span class="tok">+<span id="rules-now">{}</span> for your rules now</span>'
                     .format(view["costs"]["rules_now"]) if x["key"] == "rules" else "")
            ctl = control(x)
            if x["key"] == "bang":
                ready = ", ".join(view["bang_sessions"])
                extra = ('<span class="tok">Sessions started with --bang: '
                         f'{e(ready) if ready else "none right now"}</span>')
                if not now["bang"]:  # on needs the host key; this page has only the pairing key
                    ctl = ctl.replace("<input ", "<input disabled ", 1)
                    extra += '<span class="tok">On: run <code>nanotea bang on</code> on its machine</span>'
            if x["key"] == "config_programs" and not now["config_programs"]:
                ctl = ctl.replace("<input ", "<input disabled ", 1)
                extra = '<span class="tok">On: run <code>nanotea config programs on</code> on its machine</span>'
            rows.append(row(x["label"], x["about"], ctl, by.get(x["key"], 0) if agents else None, extra))
        sections.append(f'<h2>{e(sec["label"])}</h2><p class="meta">{e(sec["about"])}</p>' + "".join(rows))
    tools = "".join(
        row(t, TOOL_ABOUT[t],
            f'<input type="checkbox" name="tools.{t}"{" checked" if on else ""} aria-label="{t}">', by[f"tools.{t}"])
        for t, on in now["tools"].items())
    groups = "\n".join(f'{g["name"]}: {", ".join(g["members"])}' for g in board["groups"])
    arrange = ('<form class="form" id="board" onsubmit="return false"><h2>Board groups</h2>'
               '<p class="meta">One group a line, in order: its name, a colon, then its members, separated by '
               'commas. A member is an agent&#39;s name or a line&#39;s label. Agents in no group go under Other.</p>'
               f'<textarea name="groups" rows="{max(4, len(board["groups"]) + 2)}" autocomplete="off" '
               f'autocapitalize="off" spellcheck="false" aria-label="groups">{e(groups)}</textarea>'
               '<p class="meta">Hidden: names kept off the board while they are not connected and nothing of '
               'theirs is unread. Separated by commas.</p>'
               f'<input type="text" name="hidden" value="{e(", ".join(board["hidden"]))}" placeholder="none" '
               'autocomplete="off" autocapitalize="off" aria-label="hidden">'
               '<p><button type="submit">Save the board</button> <span id="board-note" class="meta"></span></p>'
               '</form>')
    body = (f'<div class="cfg">{config_summary(summary)}</div>'
            f'<form class="form" id="settings" onsubmit="return false">'
            f'<p class="total">A session starts with about <b id="session-tokens">{view["session_tokens"]}</b> '
            f'tokens of Nanotea: <span class="meta">{view["costs"]["base"]} always, the rest from what is on '
            f'under Agents and Optional tools. Estimates, at about four characters a token.</span></p>'
            f'<p id="settings-note" class="error"></p>'
            + sections[0] + '<p class="meta"><a href="/tokens">Agent tokens</a>: who may connect.</p>'
            '<h2>Optional tools</h2><p class="meta">join, send, ask, check, wait and voices '
            'are always there.</p>' + tools + "".join(sections[1:]) + "</form>" + arrange + SETTINGS_SCRIPT
            + _look_form(look))
    return _page("Settings", body, "Settings")


TOOL_ABOUT = {
    "thread": "Agents read a whole thread, to see what a reply is about.",
    "react": "Agents react to your messages with an emoji.",
    "status": "Agents keep a one-line status beside their name.",
    "typing": "Agents show they are writing something long.",
    "channels": "Agents list the channels, their topics and who listens to each.",
    "board": "Agents see every agent in your sidebar: connected, listening, typing, and its status.",
    "history": "Agents read back their own conversation with you.",
    "controls": "Agents attach buttons or a checklist to a message for you to tap; your tap goes back to them.",
}


CONFIG_SCRIPT = """
<script>
(() => {
  const box = document.getElementById("voices");
  if (!box) return;
  const cell = (v) => v === null || v === undefined ? "" : String(v);
  fetch("/api/config/voices").then(async (r) => {
    const out = await r.json();
    if (!r.ok) throw new Error(out.error || r.statusText);
    return out;
  }).then((out) => {
    if (out.error) {
      box.innerHTML = "";
      const p = document.createElement("p");
      p.className = "error";
      p.textContent = `The voice engine could not list its voices: ${out.error}`;
      box.append(p);
      return;
    }
    box.innerHTML = "";
    const p = document.createElement("p");
    p.className = "meta";
    p.textContent = `${out.voices.length} voices. Agents pick one by its id.`;
    const table = document.createElement("table");
    table.className = "voices";
    for (const v of out.voices) {
      const tr = table.insertRow();
      for (const text of [v.id, v.name, v.gender, v.locale]) tr.insertCell().textContent = cell(text);
    }
    box.append(p, table);
  }).catch((err) => {
    box.innerHTML = "";
    const p = document.createElement("p");
    p.className = "error";
    p.textContent = `Could not ask for the voices: ${err.message}`;
    box.append(p);
  });
})();
</script>
"""


def _field(o: dict, locked: bool) -> str:
    """The input for one config key, holding its value as the service runs with it."""
    f, v = o["field"], o["raw"]
    attrs = (f' class="edit" aria-label="{e(o["table"])} {e(o["key"])}"' + (" disabled" if locked else "")
             + ' autocomplete="off" autocapitalize="off" spellcheck="false"')
    kind = f["input"]
    if kind in ("bool", "choice"):
        choices = [True, False] if kind == "bool" else f["choices"]
        have = any(c == v and type(c) is type(v) for c in choices)
        opts = "".join(f'<option value="{e(json.dumps(c))}"{" selected" if c == v and type(c) is type(v) else ""}>'
                       f'{e(c if isinstance(c, str) else json.dumps(c))}</option>' for c in choices)
        blank = "" if have else '<option value="" selected>not set</option>'
        return f'<select{attrs}>{blank}{opts}</select>'
    if kind == "number":
        bounds = "".join(f' {a}="{f[b]:g}"' for a, b in (("min", "lo"), ("max", "hi")) if f.get(b) is not None)
        return (f'<input type="number" step="{f["step"]}"{bounds} value="{"" if v is None else e(json.dumps(v))}"'
                f'{attrs}>')
    if kind == "text":
        suggest = ""
        if f.get("suggest"):
            dl = f'dl-{"-".join(o["path"])}-{o["key"]}'
            suggest = (f' list="{e(dl)}"><datalist id="{e(dl)}">'
                       + "".join(f'<option value="{e(x)}">' for x in f["suggest"]) + "</datalist")
        return f'<input type="text" value="{"" if v is None else e(v)}"{attrs}{suggest}>'
    text = "" if v is None else json.dumps(v, indent=2, ensure_ascii=False)
    return f'<textarea rows="{min(12, text.count(chr(10)) + 2)}"{attrs}>{e(text)}</textarea>'


def _option(o: dict, programs: bool) -> str:
    """One config key: its name, its value in an input, where the value comes from, and what it does."""
    src = {"config": "set in the file", "default": "default", "missing": "missing"}[o["source"]]
    takes = f" Takes {e(o['takes'])}." if o["takes"] else ""
    default = f' Default {e(o["default"])}.' if o["source"] == "config" and o["default"] and not o["required"] else ""
    need = " Required." if o["required"] and o["source"] != "missing" else ""
    locked = o["machine"] and not programs
    lock = ('<p class="meta lock">Picks a program the service runs, code it loads, or where it keeps its data or '
            'secrets: it changes here only while Programs from this app is on (Settings, Service).</p>'
            if locked else "")
    reset = ('<button type="button" class="btn small reset">Use the default</button>'
             if o["source"] == "config" and not o["required"] and not locked else "")
    data = (f' data-path="{e(json.dumps(o["path"]))}" data-key="{e(o["key"])}" data-input="{o["field"]["input"]}"'
            f' data-float="{"1" if o["field"].get("float") else ""}" data-source="{o["source"]}"'
            f' data-blank-unsets="{"1" if o["default_raw"] is None and not o["required"] else ""}"')
    return (f'<div class="opt"{data}><div class="k"><code class="key">{e(o["key"])}</code>'
            f'<span class="src {o["source"]}">{src}</span></div>'
            f'<p class="meta">In <code>{e(o["table"])}</code></p>'
            f'<div class="field">{_field(o, locked)}{reset}</div><p class="meta bad" hidden></p>'
            f'<p class="meta">{e(o["about"])}{takes}{need}{default}</p>{lock}</div>')


def _section_html(s: dict, programs: bool) -> str:
    head = (f'<h2 id="{s["id"]}">{e(s["title"])}</h2><p class="meta">{e(s["about"])}</p>'
            f'<p class="where">In the config file: <code>{e(s["where"])}</code></p>')
    plugin = ""
    if s["plugin"]:
        p = s["plugin"]
        plugin = (f'<p class="meta">In use: <b>{e(p["name"])}</b> ({e(p["origin"])}). {e(p["about"])}'
                  + ("" if p["declared"] else " This plugin does not say what it takes, so only what the file "
                                              "gives it is listed.") + "</p>")
    body = "".join(_option(o, programs) for o in s["rows"])
    for item in s.get("listed", []):
        body += (f'<h3>{e(item["name"])}</h3><p class="meta">{e(item["origin"])}. {e(item["about"])}</p>'
                 + "".join(_option(o, programs) for o in item["rows"]) + _secrets(item["secrets"]))
    body += _secrets(s["secrets"])
    facts = "".join(f'<p class="meta"><b>{e(k)}:</b> {e(v)}</p>' for k, v in s["facts"])
    if s.get("voices"):
        facts += '<h3>Voices</h3><div id="voices"><p class="meta">Asking the voice engine...</p></div>'
    if s.get("prompt"):
        facts += '<p><a class="btn small" href="/prompts">Read and change the rewriter\'s instructions</a></p>'
    if s.get("id") == "notify":
        facts += '<p><a class="btn small" href="/notifications">Muting, questions only, quiet hours</a></p>'
    others = ""
    if s["others"]:
        others = ("<details><summary>Others you could use, and what each takes</summary>"
                  + "".join(f'<h3>{e(o["name"])}</h3><p class="meta">{e(o["origin"])}. {e(o["about"])}</p>'
                            + "".join(_option(r, programs) for r in o["rows"]) + _secrets(o["secrets"])
                            for o in s["others"]) + "</details>")
    return head + plugin + body + facts + others


def _secrets(secrets: list[dict]) -> str:
    out = []
    for x in secrets:
        if x["editable"]:
            ctl = (f'<div class="field"><input type="password" class="secret" autocomplete="new-password" '
                   f'placeholder="{"a new value" if x["set"] else "its value"}" aria-label="{e(x["name"])}">'
                   '<button type="button" class="btn small set">Set</button>'
                   + ('<button type="button" class="btn small unset">Remove</button>' if x["set"] else "")
                   + '</div><p class="meta bad" hidden></p>')
            how = " Setting it here writes it to that file and restarts the service."
        else:
            ctl, how = "", " To set it here, the config needs an env_file."
        out.append(f'<div class="opt" data-secret="{e(x["name"])}"><div class="k"><code class="key">{e(x["name"])}'
                   f'</code><span class="src {"config" if x["set"] else "missing"}">'
                   f'{"set" if x["set"] else "missing"}</span></div>{ctl}'
                   f'<p class="meta">A secret, read from {e(x["from"])}. Its value is never shown.{how}</p></div>')
    return "".join(out)


CONFIG_EDIT_SCRIPT = """
<script>
(() => {
  const page = document.querySelector(".cfg[data-started]");
  const bar = document.getElementById("cfg-save");
  const note = document.getElementById("cfg-note");
  const count = document.getElementById("cfg-count");
  const opts = [...page.querySelectorAll(".opt[data-key]")];
  const control = (opt) => opt.querySelector(".edit");
  for (const opt of opts) opt.dataset.orig = control(opt).value;
  // Changes the browser may not survive: the address it uses, or what the service listens on.
  const reach = new Set(["public_url", "host", "port", "trusted_proxies"]);
  const change = (opt) => {
    const at = { table: JSON.parse(opt.dataset.path), key: opt.dataset.key };
    if (opt.classList.contains("unset")) return { ...at, unset: true };
    const c = control(opt);
    if (c.value === opt.dataset.orig) return null;
    const kind = opt.dataset.input;
    const t = c.value;
    if (kind === "bool" || kind === "choice") return t === "" ? null : { ...at, value: JSON.parse(t) };
    if (t.trim() === "" && opt.dataset.blankUnsets) return opt.dataset.source === "config" ? { ...at, unset: true } : null;
    if (kind === "number") {
      const n = Number(t);
      if (t.trim() === "" || !Number.isFinite(n)) throw new Error("a number");
      return { ...at, value: n, ...(opt.dataset.float ? { float: true } : {}) };
    }
    if (kind === "json") {
      try {
        return { ...at, value: JSON.parse(t) };
      } catch (err) {
        throw new Error(`not JSON: ${err.message}`);
      }
    }
    return { ...at, value: t };
  };
  const collect = () => {
    const out = [];
    let bad = 0;
    for (const opt of opts) {
      const say = opt.querySelector(".bad");
      try {
        const c = change(opt);
        say.hidden = true;
        opt.classList.toggle("dirty", c !== null);
        if (c) out.push(c);
      } catch (err) {
        say.textContent = `Not saved: ${err.message}`;
        say.hidden = false;
        opt.classList.add("dirty");
        bad += 1;
      }
    }
    return { out, bad };
  };
  const refresh = () => {
    const { out, bad } = collect();
    bar.hidden = out.length + bad === 0;
    count.textContent = `${out.length + bad} ${out.length + bad === 1 ? "change" : "changes"}` +
                        (bad ? `, ${bad} to fix` : "");
    document.getElementById("cfg-apply").disabled = bad > 0;
  };
  page.addEventListener("input", (ev) => { if (ev.target.classList.contains("edit")) refresh(); });
  page.addEventListener("change", (ev) => { if (ev.target.classList.contains("edit")) refresh(); });
  page.addEventListener("click", (ev) => {
    const reset = ev.target.closest(".reset");
    if (!reset) return;
    const opt = reset.closest(".opt");
    const on = opt.classList.toggle("unset");
    control(opt).disabled = on;
    reset.textContent = on ? "Keep the file's value" : "Use the default";
    refresh();
  });
  document.getElementById("cfg-discard").addEventListener("click", () => location.reload());
  const busy = (on) => { for (const b of page.querySelectorAll("button")) b.disabled = on; };
  // The service answers, then restarts; this page reloads once a new process says it has started.
  const comeBack = async () => {
    const was = page.dataset.started;
    const until = Date.now() + 90000;
    let last = "";
    while (Date.now() < until) {
      await new Promise((ok) => setTimeout(ok, 1000));
      try {
        const r = await fetch("/api/config/started", { cache: "no-store" });
        if (r.ok && (await r.json()).started !== was) return location.reload();
        last = r.ok ? "" : `${r.status} ${r.statusText}`;
      } catch (err) {
        last = err.message;
      }
      note.textContent = "Saved. Restarting the service...";
    }
    note.textContent = "Saved, but the service has not come back after 90 seconds" + (last ? ` (${last})` : "") +
                       ". Its log says why.";
    note.className = "error";
  };
  const send = async (url, body) => {
    busy(true);
    note.className = "meta";
    note.textContent = "Checking it as the service would start with it...";
    bar.hidden = false;
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
    } catch (err) {
      note.className = "error";
      note.textContent = `Not saved: ${err.message}`;
      busy(false);
      refresh();
      return;
    }
    await comeBack();
  };
  document.getElementById("cfg-apply").addEventListener("click", () => {
    const { out, bad } = collect();
    if (bad || !out.length) return;
    const risky = out.filter((c) => c.table.length === 0 && reach.has(c.key)).map((c) => c.key);
    if (risky.length && !confirm(`${risky.join(", ")} decide how this browser reaches the service. If the new ` +
                                 "values are wrong, this page can't come back, and the fix is in the config file " +
                                 "on its machine. Save?")) return;
    send("/api/config", { changes: out });
  });
  page.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-secret] .set, [data-secret] .unset");
    if (!b) return;
    const opt = b.closest("[data-secret]");
    const name = opt.dataset.secret;
    if (b.classList.contains("unset")) {
      if (confirm(`Remove ${name} from the secrets file?`)) send("/api/config/secret", { name, unset: true });
      return;
    }
    const value = opt.querySelector(".secret").value;
    const say = opt.querySelector(".bad");
    say.hidden = !!value;
    say.textContent = value ? "" : "Type its value first.";
    if (value) send("/api/config/secret", { name, value });
  });
})();
</script>
"""


def config_page(view: dict) -> str:
    """view: configview.build(). Everything the service is running with, section by section, each key editable."""
    jump = "".join(f'<a class="btn small" href="#{s["id"]}">{e(s["title"])}</a>' for s in view["sections"])
    programs = ("Programs from this app is on: the settings that pick the programs the service runs, the code it "
                "loads, and where it keeps its data and secrets change here too." if view["programs"] else
                "The settings that pick the programs the service runs, the code it loads, and where it keeps its "
                "data and secrets are locked here. To change them here, run nanotea config programs on, on the "
                "machine the service runs on; or edit the file.")
    changed = f'<p class="error">{e(view["changed"])}</p>' if view["changed"] else ""
    body = (f'<div class="cfg" data-started="{e(view["started"])}">'
            f'<p class="meta">What the service runs with, from <code>{e(view["file"])}</code>. Each value says '
            'whether the file set it or the default applies. Change any of them here and save: the service checks '
            'the new config as it would at start, writes it into the file in place (comments stay), keeps the old '
            'file under data/config-history, and restarts. Settings has what changes without a restart. Secrets '
            f'are never shown, only whether they are set.</p><p class="meta">{e(programs)}</p>{changed}'
            '<p><a class="btn small" href="/prompts">Prompts</a> <a class="btn small" href="/settings">Settings</a>'
            f'</p><div class="jump">{jump}</div>'
            + "".join(_section_html(s, view["programs"]) for s in view["sections"])
            + '<div id="cfg-save" hidden><span id="cfg-count" class="meta"></span>'
            '<button type="button" class="btn small" id="cfg-discard">Discard</button>'
            '<button type="button" class="btn small primary" id="cfg-apply">Save and restart</button>'
            '<span id="cfg-note" class="meta"></span></div>'
            + "</div>" + CONFIG_SCRIPT + CONFIG_EDIT_SCRIPT)
    return _page("Configuration", body, "Configuration")


PROMPTS_SCRIPT = """
<script>
(() => {
  const form = document.getElementById("prompt");
  const note = document.getElementById("prompt-note");
  const send = async (body, done) => {
    note.textContent = "";
    for (const b of form.querySelectorAll("button")) b.disabled = true;
    try {
      const r = await fetch("/api/prompts", { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify(body) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      note.textContent = done;
      location.reload();
    } catch (err) {
      note.textContent = err.message;
      for (const b of form.querySelectorAll("button")) b.disabled = false;
    }
  };
  form.addEventListener("submit", () => send({ name: "rewrite", text: form.elements.text.value }, "Saved."));
  document.getElementById("prompt-reset").addEventListener("click", () => {
    if (confirm("Go back to the built-in instructions? Yours are deleted.")) {
      send({ name: "rewrite", reset: true }, "Back to the built-in.");
    }
  });
})();
</script>
"""


def prompts_page(view: dict) -> str:
    """view: configview.prompts(). The text the service sends to models."""
    r, a = view["rewrite"], view["agreement"]
    owner = e(view["owner"])
    mine = (f'Yours, kept in <code>{e(r["kept_at"])}</code>. The built-in one is below.' if r["custom"]
            else "The built-in one, from the package. Edit it and save to make it yours.")
    builtin = (f'<details><summary>The built-in instructions</summary><pre>{e(r["builtin"])}</pre></details>'
               if r["custom"] else "")
    rewrite = (
        '<h2 id="rewrite">Rewriter instructions</h2>'
        '<p class="meta">What the rewriter model is told when it turns an agent\'s message into what the voice '
        f'reads. {e(r["used"])}</p><p class="meta">{mine} <code>{{owner}}</code> becomes {owner}. A change reaches '
        'the next message; nothing already rewritten changes.</p>'
        '<form class="form" id="prompt" onsubmit="return false">'
        f'<textarea name="text" rows="22" spellcheck="false" aria-label="Rewriter instructions">'
        f'{e(r["template"])}</textarea>'
        f'<p class="meta">At most {r["max_chars"]} characters. Keep the paragraph about audio markers such as '
        '[[audio 1]]: without it every message with a clip fails.</p>'
        '<p><button type="submit" class="btn primary">Save</button> '
        f'<button type="button" class="btn" id="prompt-reset"{"" if r["custom"] else " disabled"}>'
        'Use the built-in</button> <span id="prompt-note" class="meta"></span></p></form>' + builtin
        + f'<details><summary>As the model gets it, with {owner} filled in</summary><pre>{e(r["text"])}</pre>'
        '</details>'
        '<details><summary>What goes with it on every message</summary>'
        f'<p class="meta">The message arrives as a request, like this:</p><pre>{e(r["request"])}</pre>'
        f'<p class="meta">The reply must fit this schema. The command rewriter is also told:</p>'
        f'<pre>{e(r["contract"])}</pre><pre>{e(r["schema"])}</pre></details>')
    lines = "".join(f'<tr class="{"" if x["on"] else "off"}"><td>{e(x["text"])}</td><td>{e(x["added_by"])}'
                    f'{"" if x["on"] else ", off now"}</td></tr>' for x in a["lines"])
    idle = "".join(f'<p class="meta"><b>{e(k)}</b>: {e(v)}</p>' for k, v in a["idle"].items())
    modes = "".join(f'<p class="meta"><b>{e(k)}</b>: {e(v)}</p>' for k, v in a["delivery"].items())
    agreement = (
        '<h2 id="agreement">Working agreement</h2>'
        f'<p class="meta">What every agent is told when it joins, with {owner} filled in, as your switches are '
        'now (Settings, Agents), for a session in pull mode that ends its turn with check. Your standing rules come '
        'after it. Not editable here: change the switches, or add a rule.</p>'
        f'<pre>{e(a["text"])}</pre>'
        f'<details><summary>Every line, and what adds it</summary><table class="lines">{lines}</table>'
        f'<p class="meta">The idle line depends on how the session waits:</p>{idle}'
        f'<p class="meta">The delivery line depends on the agent\'s mode (Configuration, Delivery):</p>{modes}'
        '</details>'
        f'<details><summary>The brief form ({len(a["brief"])} of {a["brief_max"]} characters)</summary>'
        '<p class="meta">Some harnesses keep only so much of what a server tells the agent, so their sessions get '
        'the same rules in fewer words: '
        + ", ".join(f'<code>nanotea mcp --harness {e(h)}</code>' for h in a["brief_for"]) + '.</p>'
        f'<pre>{e(a["brief"])}</pre></details>')
    tools = ('<h2 id="tools">Tools agents are shown</h2><p class="meta">Each tool\'s description is text the '
             'model reads. Optional tools show only while their switch is on.</p>'
             + "".join(f'<details><summary>{e(t["name"])}</summary><pre>{e(t["description"])}</pre></details>'
                       for t in view["tools"]))
    other = ('<h2 id="other">Other text agents get</h2>'
             + "".join(f'<p class="meta">{e(o["what"])}:</p><pre>{e(o["text"])}</pre>' for o in view["other"]))
    body = ('<div class="cfg">'
            '<p class="meta">The text the service sends to models, as it sends it. '
            '<a href="/config">Configuration</a> has the settings around it.</p>'
            + rewrite + agreement + tools + other + "</div>" + PROMPTS_SCRIPT)
    return _page("Prompts", body, "Prompts")


def config_summary(items: list[tuple[str, str, str]]) -> str:
    """The Settings page's block on what the service runs with: (label, what, link) for each part."""
    rows = "".join(f'<div class="opt"><div class="k"><a href="{e(link)}"><b>{e(label)}</b></a>'
                   f'<span class="val">{e(what)}</span></div></div>' for label, what, link in items)
    return ('<h2>Configuration</h2><p class="meta">What the service is running with, from its config file. '
            '<a href="/config">All of it</a>, and <a href="/prompts">the prompts</a> it sends to models.</p>'
            + rows)


def rules_page(rules: list[dict], agents: list[str], rules_on: bool, pin: dict | None = None) -> str:
    """Standing rules, newest first, and a form to add one. agents: names a rule can be for. pin: {text, for,
    from}, one of the owner's messages to start the form with."""
    pin = pin or {"text": "", "for": None, "from": None}
    names = sorted(set(agents) | ({pin["for"]} if pin["for"] else set()), key=str.casefold)
    opts = '<option value="">Every agent</option>' + "".join(
        f'<option value="{e(a)}"{" selected" if a == pin["for"] else ""}>{e(who(a))}</option>' for a in names)
    off = ('' if rules_on else '<p class="notice">Standing rules are off in <a href="/settings">Settings</a>, so '
           'agents don\'t get these until you turn them on.</p>')
    form = (f'<form class="form" id="add-rule">{off}<h2>New rule</h2>'
            f'<textarea name="text" required maxlength="500" placeholder="What agents should always do, or never do"'
            f' aria-label="Rule">{e(pin["text"])}</textarea>'
            f'<div class="row"><label class="about">For <select name="for">{opts}</select></label>'
            f'<input type="hidden" name="from" value="{e(pin["from"] or "")}">'
            f'<div class="ctl"><button class="btn primary small">Add rule</button></div></div>'
            f'<p id="rules-note" class="error"></p></form>')

    def item(r: dict) -> str:
        whom = who(r["for"]) if r["for"] else "Every agent"
        return (f'<div class="rule"><div class="txt"><div class="text">{markdown.to_html(r["text"])}</div><div class="meta">{e(whom)} · '
                f'{_age_span(r["at"], "ago")}</div></div><button type="button" class="btn small" '
                f'data-rule="{e(r["id"])}">Remove</button></div>')

    listed = ("".join(item(r) for r in reversed(rules)) if rules
              else '<div class="empty">No rules yet. Pin one of your messages, or write one above.</div>')
    n = len(rules)
    return _page("Rules", form + f'<div class="form">{listed}</div>' + RULES_SCRIPT, "Rules",
                 f"{n} standing rule{'' if n == 1 else 's'}")


def tokens_page(tokens: list[dict], tokenless: list[str], waiting: list[dict], approver: dict | None) -> str:
    """The tokens agents and event sources present, live ones; the requests for one waiting on a decision; and the
    approver, who decides them in the owner's place. tokenless: agents seen without a live token."""
    out = '<p id="tokens-note" class="error"></p>'
    if tokenless:
        names = ", ".join(e(who(n)) for n in tokenless)
        out += (f'<p class="notice">No token yet for {names}. Each asks for one when its nanotea mcp next starts, '
                f'and is refused until then.</p>')

    def request(r: dict) -> str:
        what = f'events to {r["line"]}' if r["kind"] == "events" else (
            f'agent on line {r["line"]}' if r["line"] else "agent")
        had = f' · had a token, revoked {_age_span(r["had"], "ago")}' if r["had"] else ""
        return (f'<div class="rule"><div class="txt"><div class="text"><b>{e(who(r["name"]))}</b> '
                f'<span class="meta">{e(what)}</span></div><div class="meta">in {e(r["dir"] or "no dir given")} · '
                f'from {e(r["from"])} · asked {_age_span(r["asked"], "ago")}{had}</div></div>'
                f'<button type="button" class="btn small primary" data-act="approve" data-id="{e(r["id"])}">'
                f'Approve</button><button type="button" class="btn small" data-act="deny" data-id="{e(r["id"])}">'
                f'Deny</button></div>')

    if waiting:
        out += (f'<div class="form"><h2>Asking for a token</h2><p class="meta">The name and directory are the '
                f'asker\'s own word. Approved, it collects its token itself.</p>'
                f'{"".join(request(r) for r in waiting)}</div>')
    if approver:
        out += (f'<div class="form"><h2>Approver</h2><p class="meta"><b>{e(who(approver["name"]))}</b> approves '
                f'the agents and programs that ask for a token. You can approve them here too.</p>'
                f'<div class="ctl"><button type="button" class="btn small" data-act="unappoint">No approver'
                f'</button></div></div>')
    else:
        out += ('<div class="form"><h2>Approver</h2><p class="meta">None: you approve each agent and program that '
                'asks for a token, here. Make one agent the approver to have it decide them.</p></div>')

    def item(t: dict) -> str:
        what = (f'events to {t["line"]}' if t["kind"] == "events" else
                f'agent on line {t["line"]}' if t["line"] else "agent")
        used = _age_span(t["last_used"], "ago") if t["last_used"] else "not since the service started"
        mine = approver is not None and approver["id"] == t["id"]
        role = ' · <b>approver</b>' if mine else ""
        appoint = (f'<button type="button" class="btn small" data-act="appoint" data-id="{e(t["id"])}" '
                   f'data-name="{e(who(t["name"]))}">Make approver</button>'
                   if t["kind"] == "agent" and not mine else "")
        return (f'<div class="rule"><div class="txt"><div class="text"><b>{e(who(t["name"]))}</b> '
                f'<span class="meta">{e(what)}</span></div><div class="meta">{e(t["id"])} · made '
                f'{_age_span(t["created"], "ago")} · used {used}{role}</div></div>{appoint}'
                f'<button type="button" class="btn small" data-act="revoke" data-id="{e(t["id"])}">Revoke</button>'
                f'</div>')

    listed = ("".join(item(t) for t in tokens) if tokens
              else '<div class="empty">No tokens yet. An agent asks for one when its nanotea mcp starts.</div>')
    n = len(tokens)
    sub = f"{n} live token{'' if n == 1 else 's'}" + (f", {len(waiting)} asking" if waiting else "")
    return _page("Tokens", out + f'<div class="form">{listed}</div>' + TOKENS_SCRIPT, "Tokens", sub)


def talk_page(msgs: list[dict], mode: str) -> str:
    """Agents' messages to each other, newest first. mode: the agent_messages setting."""
    if mode == "off":
        body = ('<div class="empty">Agents can\'t message each other. Turn it on in '
                '<a href="/settings">Settings</a>.</div>')
    elif mode == "hidden":
        body = ('<div class="empty">Agents can message each other, and it stays between them. To see it here, '
                'choose shown in <a href="/settings">Settings</a>.</div>')
    elif not msgs:
        body = '<div class="empty">Nothing yet.</div>'
    else:
        def item(m: dict) -> str:
            got = f"read {_age(m['delivered_at'], 'ago')}" if m["delivered_at"] else "not read yet"
            return (f'<div class="talk"><div class="meta"><b>{e(who(m["from"]))}</b> to <b>{e(who(m["to"]))}</b> · '
                    f'{_age_span(m["at"], "ago")} · {e(got)}</div><div class="txt text">{markdown.to_html(m["text"])}</div></div>')
        body = '<div class="form">' + "".join(item(m) for m in reversed(msgs)) + "</div>"
    return _page("Agent talk", body, "Agent talk")


def _view(store: Store, holders: dict[str, str | None], msgs: list[Message], title: str, sub: str, empty: str,
          actions: str = "") -> str:
    """Messages as a list, newest first, under their own heading."""
    body = (f'<div class="thread list">{list_rows(store, msgs, holders)}</div>' if msgs
            else f'<div class="empty">{e(empty)}</div>')
    return _page(title, body, title, sub, actions)


def waiting(store: Store, holders: dict[str, str | None], msgs: list[Message], n: int,
            cuts: list[tuple[float, int]]) -> str:
    """Questions agents are still waiting on, newest first. msgs: those shown, of n. cuts: (a day ago, how many
    were asked before it), (now, how many before it): what the set-aside buttons sweep."""
    sub = f"{n} question{'' if n == 1 else 's'} waiting" + (f", the newest {len(msgs)} shown" if n > len(msgs) else "")
    (day, older), (now, before) = cuts
    day, now = f"{day:.6f}", f"{now:.6f}"
    buttons = ""
    if older and older < before:
        buttons += (f'<button type="button" class="btn small" data-set-aside="{day}" data-n="{older}">'
                    f'Set aside {older} older than a day</button>')
    if before:
        buttons += (f'<button type="button" class="btn small" data-set-aside="{now}" data-n="{before}">'
                    f'Set all {before} aside</button>')
    return (_view(store, holders, msgs, "Waiting on you", sub, "No agent is waiting on an answer.", buttons)
            + SET_ASIDE_SCRIPT)


SET_ASIDE_SCRIPT = """
<script>
(() => {
  for (const btn of document.querySelectorAll("[data-set-aside]")) {
    btn.addEventListener("click", async () => {
      const n = Number(btn.dataset.n);
      if (!confirm(`Set aside ${n} question${n === 1 ? "" : "s"} without answering? Each agent is told. ` +
                   "You can still answer any of them from its page.")) return;
      btn.disabled = true;
      try {
        const r = await fetch("/api/set-aside", { method: "POST", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify({ before: Number(btn.dataset.setAside) }) });
        const out = await r.json();
        if (!r.ok) throw new Error(out.error || r.statusText);
        location.reload();
      } catch (err) {
        btn.disabled = false;
        alert(`Couldn't set them aside: ${err.message}`);
      }
    });
  }
})();
</script>
"""


READ_ALL_SCRIPT = """
<script>
(() => {
  const btn = document.querySelector("[data-read-all]");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const r = await fetch("/api/read-all", { method: "POST", headers: { "Content-Type": "application/json" },
                                                body: "{}" });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || r.statusText);
      location.reload();
    } catch (err) {
      btn.disabled = false;
      alert(`Couldn't mark them read: ${err.message}`);
    }
  });
})();
</script>
"""


def unread(store: Store, holders: dict[str, str | None], msgs: list[Message], n: int) -> str:
    """Messages you haven't seen, newest first. msgs: those shown, of n."""
    sub = f"{n} unread" + (f", the newest {len(msgs)} shown" if n > len(msgs) else "")
    button = '<button type="button" class="btn small" data-read-all>Mark all read</button>' if n else ""
    return _view(store, holders, msgs, "Unread", sub, "Nothing unread.", button) + READ_ALL_SCRIPT


def search(store: Store, holders: dict[str, str | None], q: str, msgs: list[Message], n: int) -> str:
    """Agents' messages matching every word of q, newest first. msgs: those shown, of n."""
    form = (f'<form class="form" method="get" action="/search" role="search"><div class="row">'
            f'<input type="search" name="q" value="{e(q)}" placeholder="Words in a message, a title, an agent or an answer"'
            f' aria-label="Search" required{"" if q else " autofocus"}>'
            f'<div class="ctl"><button class="btn primary small">Search</button></div></div></form>')
    if not q:
        keys = ('<p class="meta">With a keyboard: / opens search. g then w, u, m, n or s goes to Waiting, Unread, '
                'Messages, Notifications or Search.</p>')
        return _page("Search", form + keys, "Search",
                     actions='<a class="btn small" href="/export.md" download>Export all</a>')
    sub = f"{n} match{'' if n == 1 else 'es'}" + (f", the newest {len(msgs)} shown" if n > len(msgs) else "")
    results = (f'<div class="thread list">{list_rows(store, msgs, holders)}</div>' if msgs
               else '<div class="empty">No message matches.</div>')
    export = f'<a class="btn small" href="/export.md?q={quote_plus(q)}" download>Export these</a>' if msgs else ""
    return _page("Search", form + results, "Search", sub, export)


def not_found(message: str) -> str:
    return _page("Not found", f'<div class="empty"><div class="mark">?</div>{e(message)}</div>', "Not found")


def unpaired() -> str:
    return _page("Not paired", "<p>This device isn't paired yet. On the machine running nanotea, run "
                               "<code>nanotea pair-link</code> and open the link it prints on this device. For "
                               "the Home Screen app, add it to the Home Screen from a paired Safari page.</p>",
                 _app_name(), paired=False)
