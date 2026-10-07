"""Markdown in what the owner and agents write: HTML for the pages, plain text for the voice and notifications.

A chat-flavored subset of CommonMark and GitHub's extensions: paragraphs keep their line breaks and spaces, fenced
code, inline code, headings, block quotes, lists (nested, task items), tables, rules, bold, italic,
strikethrough, links and bare URLs. Quote and list lines continue only when marked or indented, as in a chat.
Raw HTML is never passed through, and links go only to http, https, mailto, or a path on this site."""

import html
import re
import unicodedata
from collections.abc import Callable

from nanotea.store import AUDIO_MARKER, MARKER

e = html.escape

URL = re.compile(r"https?://[^\s<>\"'`]+")
# Links in what someone wrote or sent open beside the app, so the conversation keeps its place.
NEW_TAB = ' target="_blank" rel="noopener noreferrer"'

FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*(.*?)[ \t]*$")
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
RULE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
QUOTE = re.compile(r"^ {0,3}> ?")
ITEM = re.compile(r"^( {0,3})([-+*]|\d{1,9}[.)])(?:([ \t]+)(.*))?$")
TASK = re.compile(r"^\[([ xX])\][ \t]+")
TABLE_SEP = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
AUTOLINK = re.compile(r"<((?:https?|mailto):[^\s<>]+)>", re.I)
SAFE_HREF = re.compile(r"(?i)^(?:https?://|mailto:)\S+$|^/(?![/\\])\S*$|^#\S*$")  # not //host or /\host
MAX_DEPTH = 16  # quotes and lists nested deeper are text
MAX_INLINE = 32  # and styles and links nested deeper
DEST_STOP = re.compile(r"[\s\\()]")
ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def trim(url: str) -> str:
    """Drop trailing punctuation and an unmatched closing bracket, as in "(see http://x/y)." """
    while url and (url[-1] in ".,;:!?*_~"
                   or (url[-1] == ")" and url.count(")") > url.count("("))
                   or (url[-1] == "]" and url.count("]") > url.count("["))):
        url = url[:-1]
    return url


def urls(text: str) -> list[str]:
    """Every link in the text, once each, in order."""
    return list(dict.fromkeys(trim(m[0]) for m in URL.finditer(text)))


# Inline nodes: str (text), ("code", s), ("br",), (tag, children) for em, strong and del, ("a", href, children),
# ("img", href, children), ("clip", n, kind). Delim only while parsing.
BR = ("br",)


class Delim:
    __slots__ = ("ch", "n", "orig", "can_open", "can_close", "pos", "prev", "next", "opens", "closes")

    def __init__(self, ch: str, n: int, can_open: bool, can_close: bool):
        self.ch, self.n, self.orig, self.can_open, self.can_close = ch, n, n, can_open, can_close


def _punct(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "PS"


def _deep(depth: dict, x) -> int:
    return depth.get(id(x), (0,))[0]


def _depth(depth: dict, nodes: list) -> int:
    return max((_deep(depth, x) for x in nodes), default=0)


def _flat(nodes: list) -> list:
    """The leaves of nodes, in order: their text without its styles and links."""
    out, todo = [], [iter(nodes)]
    while todo:
        x = next(todo[-1], None)
        if x is None:
            todo.pop()
        elif isinstance(x, str) or x[0] in ("br", "code", "clip"):
            out.append(x)
        else:
            todo.append(iter(x[-1]))
    return out


def _emphasis(nodes: list, depth: dict) -> list:
    """CommonMark's delimiter matching, with ~~ for strikethrough; unmatched delimiters stay as text. depth: how
    deep each node made so far nests, by id, as (depth, node) so the id stays its own; styles that would nest past MAX_INLINE stay as text."""
    stack = [x for x in nodes if isinstance(x, Delim)]
    if not stack:
        return nodes
    for pos, x in enumerate(nodes):
        if isinstance(x, Delim):
            x.pos, x.opens, x.closes = pos, [], []
    for a, b in zip(stack, stack[1:]):
        a.next, b.prev = b, a
    stack[0].prev = stack[-1].next = None

    def unlink(d: Delim):
        if d.prev:
            d.prev.next = d.next
        if d.next:
            d.next.prev = d.prev

    # The lowest place an opener for a kind of closer could be, so a failed search isn't done again.
    bottom: dict = {}
    c = stack[0]
    while c:
        if not c.can_close:
            c = c.next
            continue
        key = (c.ch, c.can_open, c.orig % 3)
        floor = bottom.get(key, -1)
        o = c.prev
        while o and o.pos > floor:
            if o.ch == c.ch and o.can_open and (c.ch == "~" or not (
                    (o.can_close or c.can_open) and (o.orig + c.orig) % 3 == 0 and (o.orig % 3 or c.orig % 3))):
                break
            o = o.prev
        else:
            o = None
        if o is None:
            bottom[key] = c.pos - 1
            after = c.next
            if not c.can_open:
                unlink(c)
            c = after
            continue
        use = 2 if c.ch == "~" or (o.n >= 2 and c.n >= 2) else 1
        tag = "del" if c.ch == "~" else "strong" if use == 2 else "em"
        o.n -= use
        c.n -= use
        o.opens.append((tag, c.ch * use))  # innermost first
        c.closes.append(tag)
        o.next, c.prev = c, o  # delimiters between them are text now
        if o.n == 0:
            unlink(o)
        if c.n == 0:
            after = c.next
            unlink(c)
            c = after

    # Build the tree: frames of [tag, mark, children, deepest child].
    frames: list = [[None, "", [], 0]]
    for x in nodes:
        top = frames[-1]
        if not isinstance(x, Delim):
            top[2].append(x)
            top[3] = max(top[3], _deep(depth, x))
            continue
        for _ in x.closes:
            tag, mark, children, deep = frames.pop()
            top = frames[-1]
            if tag is None:
                children.append(mark)
                top[3] = max(top[3], deep)
            elif deep + 1 > MAX_INLINE:
                top[2] += [mark, *children, mark]
                top[3] = max(top[3], deep)
            else:
                node = (tag, children)
                depth[id(node)] = (deep + 1, node)
                top[2].append(node)
                top[3] = max(top[3], deep + 1)
        if x.n:
            frames[-1][2].append(x.ch * x.n)
        for tag, mark in reversed(x.opens):
            if len(frames) > MAX_INLINE:  # text, in its parent's children
                frames[-1][2].append(mark)
                frames.append([None, mark, frames[-1][2], 0])
            else:
                frames.append([tag, mark, [], 0])
    return frames[0][2]


def _destination(src: str, p: int) -> tuple[str, int] | None:
    """The (href, end) of "(dest "title")" at p, or None."""
    n = len(src)
    if p >= n or src[p] != "(":
        return None
    q = p + 1
    while q < n and src[q] in " \t\n":
        q += 1
    if q < n and src[q] == "<":
        line = src.find("\n", q)
        end = src.find(">", q, n if line < 0 else line)
        if end < 0:
            return None
        dest, q = src[q + 1:end], end + 1
    else:
        start, depth = q, 0
        while (m := DEST_STOP.search(src, q)) and src[m.start()] in "\\()":
            q = m.start()
            if src[q] == "\\":
                q += 2
                continue
            if src[q] == "(":
                depth += 1
                if depth > 32:
                    return None
            elif depth == 0:
                break
            else:
                depth -= 1
            q += 1
        else:
            q = m.start() if m else n
        dest = src[start:min(q, n)]
    while q < n and src[q] in " \t\n":
        q += 1
    if q < n and src[q] in "\"'(":
        close = ")" if src[q] == "(" else src[q]
        q += 1
        while q < n and src[q] != close:
            if close == ")" and src[q] == "(":
                return None
            q += 2 if src[q] == "\\" else 1
        if q >= n:
            return None
        q += 1
        while q < n and src[q] in " \t\n":
            q += 1
    if q >= n or src[q] != ")":
        return None
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", dest), q + 1


def inline(src: str, clips: bool = False) -> list:
    """Inline nodes of src. clips: [[audio N]], [[image N]] and the like are attachments, not text."""
    nodes: list = []
    depth: dict = {}
    brackets: list = []  # [node index, image] of each open [ or ![
    linked = 0  # the [ below this many brackets can't make links: no links in links
    unclosed: dict = {}  # backtick run length: where a search for its closing run failed
    buf: list[str] = []

    def flush():
        if buf:
            nodes.append("".join(buf))
            buf.clear()

    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n and src[i + 1] in ESCAPABLE:
            buf.append(src[i + 1])
            i += 2
        elif c == "\n":
            flush()
            nodes.append(BR)
            i += 1
        elif c == "`":
            j = i
            while j < n and src[j] == "`":
                j += 1
            m = None if unclosed.get(j - i, n) <= j else re.compile(rf"(?<!`)`{{{j - i}}}(?!`)").search(src, j)
            if m is None:
                unclosed[j - i] = min(j, unclosed.get(j - i, n))
                buf.append(src[i:j])
                i = j
                continue
            code = src[j:m.start()].replace("\n", " ")
            if len(code) > 1 and code[0] == " " and code[-1] == " " and code.strip(" "):
                code = code[1:-1]
            flush()
            nodes.append(("code", code))
            i = m.end()
        elif c == "[" and clips and (m := MARKER.match(src, i)):
            flush()
            nodes.append(("clip", int(m[2]), m[1]))
            i = m.end()
        elif c == "<" and (m := AUTOLINK.match(src, i)):
            flush()
            nodes.append(("a", m[1], [m[1]]))
            i = m.end()
        elif c == "!" and src.startswith("![", i):
            flush()
            brackets.append([len(nodes), True])
            nodes.append("![")
            i += 2
        elif c == "[":
            flush()
            brackets.append([len(nodes), False])
            nodes.append("[")
            i += 1
        elif c == "]":
            flush()
            if brackets:
                at, image = brackets.pop()
                active = image or len(brackets) >= linked
                linked = min(linked, len(brackets))
                link = _destination(src, i + 1) if active else None
                if link and SAFE_HREF.match(link[0]):
                    inner = _emphasis(nodes[at + 1:], depth)
                    deep = _depth(depth, inner)
                    if deep + 1 > MAX_INLINE:
                        inner, deep = _flat(inner), 0
                    del nodes[at:]
                    node = ("img" if image else "a", link[0], inner)
                    depth[id(node)] = (deep + 1, node)
                    nodes.append(node)
                    if not image:
                        linked = len(brackets)
                    i = link[1]
                    continue
            nodes.append("]")
            i += 1
        elif c in "*_~":
            j = i
            while j < n and src[j] == c:
                j += 1
            if c == "~" and j - i != 2:
                buf.append(src[i:j])
                i = j
                continue
            before = src[i - 1] if i else " "
            after = src[j] if j < n else " "
            left = not after.isspace() and (not _punct(after) or before.isspace() or _punct(before))
            right = not before.isspace() and (not _punct(before) or after.isspace() or _punct(after))
            if c == "_":
                can_open, can_close = left and (not right or _punct(before)), right and (not left or _punct(after))
            else:
                can_open, can_close = left, right
            flush()
            nodes.append(Delim(c, j - i, can_open, can_close))
            i = j
        elif c in "hH" and (i == 0 or not src[i - 1].isalnum()) and (m := URL.match(src, i)):
            url = trim(m[0])
            if len(url) <= url.index("//") + 2:
                buf.append(c)
                i += 1
                continue
            flush()
            nodes.append(("a", url, [url]))
            i += len(url)
        else:
            buf.append(c)
            i += 1
    flush()
    return _emphasis(nodes, depth)


# Blocks: ("p", nodes), ("h", level, nodes), ("code", lang, text), ("quote", blocks), ("rule",),
# ("list", start or None, tight, [(checked or None, blocks)]), ("table", aligns, header cells, rows of cells).


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in CELL_SPLIT.split(s)]


def _table_at(lines: list[str], i: int) -> bool:
    return (i + 1 < len(lines) and "|" in lines[i] and bool(TABLE_SEP.match(lines[i + 1]))
            and "|" in lines[i] + lines[i + 1] and len(_cells(lines[i])) == len(_cells(lines[i + 1])))


def _fence(line: str):
    m = FENCE.match(line)
    if m and not (m[2][0] == "`" and "`" in m[3]):
        return m
    return None


def _starts(lines: list[str], i: int) -> bool:
    """Whether lines[i] begins a block other than a paragraph, so it ends the paragraph before it."""
    line = lines[i]
    if _fence(line) or HEADING.match(line) or RULE.match(line) or QUOTE.match(line) or _table_at(lines, i):
        return True
    m = ITEM.match(line)
    return bool(m and m[4] and m[4].strip() and (m[2] in "-+*" or m[2][:-1] == "1"))


def _indent(line: str) -> tuple[int, str]:
    line = line.expandtabs(4) if line.startswith((" ", "\t")) and "\t" in line[:len(line) - len(line.lstrip())] \
        else line
    return len(line) - len(line.lstrip(" ")), line


def _list(lines: list[str], i: int, clips: bool, depth: int) -> tuple[tuple, int]:
    first = ITEM.match(lines[i])
    ordered = first[2][-1] in ".)"
    kind = first[2][-1] if ordered else first[2]
    items, tight = [], True
    while i < len(lines):
        m = ITEM.match(lines[i])
        if not m or (m[2][-1] if ordered else m[2]) != kind or (m[2][-1] in ".)") != ordered:
            break
        gap = len((m[3] or " ").expandtabs(4))
        width = len(m[1]) + len(m[2]) + (gap if gap <= 4 else 1)
        body = [m[4] or ""]
        i += 1
        while i < len(lines):
            if not lines[i].strip():
                body.append("")
                i += 1
                continue
            lead, line = _indent(lines[i])
            if lead < width:
                break
            body.append(line[width:])
            i += 1
        blank = 0
        while body and body[-1] == "":
            body.pop()
            blank += 1
        if "" in body:
            tight = False
        checked = None
        if t := TASK.match(body[0]):
            checked = t[1] != " "
            body[0] = body[0][t.end():]
        items.append((checked, blocks(body, clips, depth + 1)))
        if blank:
            m = ITEM.match(lines[i]) if i < len(lines) else None
            if m and (m[2][-1] if ordered else m[2]) == kind:
                tight = False
                continue
            break
    return ("list", int(first[2][:-1]) if ordered else None, tight, items), i


def blocks(lines: list[str], clips: bool = False, depth: int = 0) -> list:
    if depth > MAX_DEPTH:
        return [("p", inline("\n".join(lines).strip("\n"), clips))]
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif m := _fence(line):
            ind, mark = len(m[1]), m[2]
            close = re.compile(rf"^ {{0,3}}{re.escape(mark[0])}{{{len(mark)},}}[ \t]*$")
            body = []
            i += 1
            while i < len(lines) and not close.match(lines[i]):
                body.append(re.sub(rf"^ {{0,{ind}}}", "", lines[i]) if ind else lines[i])
                i += 1
            i += 1
            out.append(("code", m[3].split()[0] if m[3] else "", "\n".join(body)))
        elif m := HEADING.match(line):
            out.append(("h", len(m[1]), inline(m[2] or "", clips)))
            i += 1
        elif RULE.match(line):
            out.append(("rule",))
            i += 1
        elif QUOTE.match(line):
            body = []
            while i < len(lines) and QUOTE.match(lines[i]):
                body.append(QUOTE.sub("", lines[i], count=1))
                i += 1
            out.append(("quote", blocks(body, clips, depth + 1)))
        elif _table_at(lines, i):
            seps = _cells(lines[i + 1])
            aligns = [("center" if s.startswith(":") and s.endswith(":") else "right" if s.endswith(":")
                       else "left" if s.startswith(":") else "") for s in seps]
            head = [inline(c, clips) for c in _cells(line)]
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip() and not (_starts(lines, i) and not _table_at(lines, i)):
                cells = _cells(lines[i])[:len(aligns)]
                rows.append([inline(c, clips) for c in cells + [""] * (len(aligns) - len(cells))])
                i += 1
            out.append(("table", aligns, head, rows))
        elif ITEM.match(line):
            block, i = _list(lines, i, clips, depth)
            out.append(block)
        else:
            body = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not _starts(lines, i):
                body.append(lines[i])
                i += 1
            out.append(("p", inline("\n".join(body).strip("\n").rstrip(), clips)))
    return out


def parse(text: str, clips: bool = False) -> list:
    return blocks(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), clips)


def _inline_html(nodes: list, clip: Callable[[str, int], str] | None, in_link: bool = False) -> str:
    out = []
    for x in nodes:
        if isinstance(x, str):
            out.append(e(x))
        elif x is BR:
            out.append("\n")  # .text keeps line breaks (white-space: pre-wrap)
        elif x[0] == "code":
            out.append(f"<code>{e(x[1])}</code>")
        elif x[0] in ("em", "strong", "del"):
            out.append(f"<{x[0]}>{_inline_html(x[1], clip, in_link)}</{x[0]}>")
        elif x[0] in ("a", "img"):
            inner = _inline_html(x[2], clip, True) if x[0] == "a" else e(_plain_inline(x[2]) or "image")
            if in_link:
                out.append(inner)
            else:
                tab = NEW_TAB if x[1][:4].lower() == "http" else ""
                cls = ' class="img"' if x[0] == "img" else ""
                out.append(f'<a href="{e(x[1])}"{cls}{tab}>{inner}</a>')
        elif x[0] == "clip":
            if clip is None:
                raise ValueError(f"[[{x[2]} {x[1]}]] with no attachments")
            out.append(clip(x[2], x[1]))
    return "".join(out)


def _blocks_html(bs: list, clip: Callable[[str, int], str] | None, tight: bool = False) -> str:
    out = []
    for b in bs:
        kind = b[0]
        if kind == "p":
            body = _inline_html(b[1], clip)
            out.append(body if tight else f"<p>{body}</p>")
        elif kind == "h":
            out.append(f"<h{b[1]}>{_inline_html(b[2], clip)}</h{b[1]}>")
        elif kind == "code":
            lang = f'<span class="lang">{e(b[1])}</span>' if b[1] else "<span></span>"
            out.append(f'<div class="code"><div class="code-head">{lang}<button type="button" class="tool" '
                       f'data-copy>Copy</button></div><pre><code>{e(b[2])}</code></pre></div>')
        elif kind == "quote":
            out.append(f"<blockquote>{_blocks_html(b[1], clip)}</blockquote>")
        elif kind == "rule":
            out.append("<hr>")
        elif kind == "list":
            start, loose = b[1], not b[2]
            tag = "ol" if start is not None else "ul"
            attr = f' start="{start}"' if start not in (None, 1) else ""
            items = []
            for checked, body in b[3]:
                box = ""
                if checked is not None:
                    box = f'<input type="checkbox" disabled{" checked" if checked else ""}>'
                cls = ' class="task"' if checked is not None else ""
                items.append(f"<li{cls}>{box}{_blocks_html(body, clip, tight=not loose)}</li>")
            out.append(f"<{tag}{attr}>{''.join(items)}</{tag}>")
        elif kind == "table":
            def row(cells, cell):
                return "<tr>" + "".join(
                    f'<{cell}{f" style=\"text-align: {a}\"" if a else ""}>{_inline_html(c, clip)}</{cell}>'
                    for a, c in zip(b[1], cells)) + "</tr>"
            body = "".join(row(r, "td") for r in b[3])
            out.append(f'<div class="table"><table><thead>{row(b[2], "th")}</thead>'
                       f'{f"<tbody>{body}</tbody>" if body else ""}</table></div>')
    return "".join(out)


def to_html(text: str, clip: Callable[[str, int], str] | None = None) -> str:
    """text as HTML, for an element that keeps line breaks (white-space: pre-wrap). clip(kind, n): the HTML of
    [[<kind> N]]; without it, the marker is text."""
    return _blocks_html(parse(text, clip is not None), clip)


def _plain_inline(nodes: list) -> str:
    out = []
    for x in nodes:
        if isinstance(x, str):
            out.append(x)
        elif x is BR:
            out.append("\n")
        elif x[0] == "code":
            out.append(x[1])
        elif x[0] in ("em", "strong", "del"):
            out.append(_plain_inline(x[1]))
        elif x[0] in ("a", "img"):
            out.append(_plain_inline(x[2]))
        elif x[0] == "clip":
            out.append(f"[[{x[2]} {x[1]}]]")
    return "".join(out)


def _plain_blocks(bs: list, speech: bool, code: str, tables: str) -> list[str]:
    out = []
    for b in bs:
        kind = b[0]
        if kind == "p":
            out.append(_plain_inline(b[1]))
        elif kind == "h":
            out.append(_plain_inline(b[2]))
        elif kind == "code":
            clips = "".join(f" [[audio {m}]]" for m in AUDIO_MARKER.findall(b[2]))
            if not speech or code == "read":
                out.append(b[2])
            elif code == "skip":
                out.append(clips.strip())
            else:
                n = len(b[2].split("\n"))
                out.append(f"There's a {n}-line code block here, in the app.{clips}")
        elif kind == "quote":
            out.append("\n\n".join(_plain_blocks(b[1], speech, code, tables)))
        elif kind == "list":
            lines = []
            for n, (checked, body) in enumerate(b[3], b[1] or 1):
                mark = f"{n}. " if b[1] is not None else "" if speech else "- "
                if checked is not None:
                    mark += ("done: " if checked else "to do: ") if speech else ("[x] " if checked else "[ ] ")
                lines.append(mark + "\n".join(_plain_blocks(body, speech, code, tables)))
            out.append("\n".join(lines))
        elif kind == "table":
            head, rows = [_plain_inline(c) for c in b[2]], [[_plain_inline(c) for c in r] for r in b[3]]
            if not speech:
                out.append("\n".join(" | ".join(r) for r in [head, *rows]))
            elif tables == "name":
                out.append(f"There's a table here, {len(rows)} row{'' if len(rows) == 1 else 's'} by {len(head)} "
                           f"column{'' if len(head) == 1 else 's'}, in the app.")
            elif tables == "labelled":
                out.append("\n".join(", ".join(f"{h}: {c}" if h.strip() else c for h, c in zip(head, r) if c.strip())
                                     + "." for r in rows))
            else:
                out.append("\n".join(", ".join(r) for r in [head, *rows]))
    return [x for x in out if x.strip()]


def plain(text: str, speech: bool = False, code: str = "name", tables: str = "rows") -> str:
    """text without its markup, [[<kind> N]] kept. speech: for the voice, which reads no bullets, and reads a
    code block as code says (name: says one is there; skip; read) and a table as tables says (rows; labelled:
    each cell after its column's heading; name: says one is there)."""
    return "\n\n".join(_plain_blocks(parse(text, True), speech, code, tables))
