"""Markdown: HTML for chosen inputs, the plain and spoken text, notification titles, links that must not become
links, input built to be slow or deep, and a random comparison of emphasis matching against the straightforward
quadratic version."""

import random
import time
import unittest

from nanotea import markdown as md
from nanotea.rewrite import IdentityRewriter

EMPHASIS = [
    ("*foo bar*", "<em>foo bar</em>"), ("a * foo bar*", "a * foo bar*"), ("foo*bar*", "foo<em>bar</em>"),
    ("_foo_bar", "_foo_bar"), ("snake_case_name", "snake_case_name"), ("2*3*4", "2<em>3</em>4"),
    ("*foo**bar**baz*", "<em>foo<strong>bar</strong>baz</em>"), ("*foo**bar*", "<em>foo**bar</em>"),
    ("***foo***", "<em><strong>foo</strong></em>"), ("foo***bar***baz", "foo<em><strong>bar</strong></em>baz"),
    ("*foo**bar***", "<em>foo<strong>bar</strong></em>"), ("**foo*bar*baz**", "<strong>foo<em>bar</em>baz</strong>"),
    ("*foo *bar**", "<em>foo <em>bar</em></em>"), ("*(**foo**)*", "<em>(<strong>foo</strong>)</em>"),
    ("**foo**bar", "<strong>foo</strong>bar"), ("__foo__bar", "__foo__bar"), ("__foo__", "<strong>foo</strong>"),
    ("**foo*", "*<em>foo</em>"), ("*foo**", "<em>foo</em>*"), ("***foo**", "*<strong>foo</strong>"),
    ("****foo*", "***<em>foo</em>"), ("**foo***", "<strong>foo</strong>*"), ("*foo****", "<em>foo</em>***"),
    ("*a **b** c*", "<em>a <strong>b</strong> c</em>"), ("~~a~~", "<del>a</del>"), ("~a~", "~a~"),
    ("a ~~~b~~~", "a ~~~b~~~"), ("**a ~~b~~ c**", "<strong>a <del>b</del> c</strong>"),
    ("*a `*` b*", "<em>a <code>*</code> b</em>"), ("\\*a\\*", "*a*"), ("**", "**"), ("a ** b", "a ** b"),
]

INLINE = [
    ("*[foo*](/u)", '*<a href="/u">foo*</a>'), ("[*a*](/b)", '<a href="/b"><em>a</em></a>'),
    ("[a [b](/c)](/d)", '[a <a href="/c">b</a>](/d)'),
    ("![a [b](/c)](/d)", '<a href="/d" class="img">a b</a>'),
    ("[x](/a(b)c)", '<a href="/a(b)c">x</a>'), ("[x](/a \"t\")", '<a href="/a">x</a>'),
    ("[x](</a>)", '<a href="/a">x</a>'), ("[x](</a\nb>)", "[x](&lt;/a\nb&gt;)"),
    ("[x](/a\\)b)", '<a href="/a)b">x</a>'),
    ("see https://a.b/c_(d)).", 'see <a href="https://a.b/c_(d)" target="_blank" rel="noopener noreferrer">'
                                'https://a.b/c_(d)</a>).'),
    ("**https://a.b/c**", '<strong><a href="https://a.b/c" target="_blank" rel="noopener noreferrer">'
                          'https://a.b/c</a></strong>'),
    ("``a ` b``", "<code>a ` b</code>"), ("` a `", "<code>a</code>"), ("`` a", "`` a"),
    ("**[[audio 2]]**", "<strong><CLIP 2></strong>"), ("see [[image 3]] and [[pdf 1]]", "see <IMAGE 3> and <PDF 1>"),
    ("[[video 2]]", "<VIDEO 2>"), ("[[gif 2]]", "[[gif 2]]"),
]

NO_LINK = ["[x](javascript:alert(1))", "[x](JAVASCRIPT:alert(1))", "[x](//evil.com)", "[x](/\\evil.com)",
           "[x](data:text/html,hi)", "[x](vbscript:x)", "![x](javascript:alert(1))", "<javascript:alert(1)>"]

BLOCKS = [
    ("a\nb\n  c", "<p>a\nb\n  c</p>"), ("> a\n> b", "<blockquote><p>a\nb</p></blockquote>"),
    ("> a\n>\n> b", "<blockquote><p>a</p><p>b</p></blockquote>"), ("> > a", "<blockquote><blockquote><p>a</p>"
                                                                   "</blockquote></blockquote>"),
    ("- a\n- b", "<ul><li>a</li><li>b</li></ul>"), ("- a\n\n- b", "<ul><li><p>a</p></li><li><p>b</p></li></ul>"),
    ("3. a\n4. b", '<ol start="3"><li>a</li><li>b</li></ol>'), ("x\n2. y", "<p>x\n2. y</p>"),
    ("- [x] a\n- [ ] b", '<ul><li class="task"><input type="checkbox" disabled checked>a</li>'
                         '<li class="task"><input type="checkbox" disabled>b</li></ul>'),
    ("# A\n#b", "<h1>A</h1><p>#b</p>"), ("---", "<hr>"), ("a\n---", "<p>a</p><hr>"),
    ("| a | b |\n|:-|-:|\n| 1 | 2 \\| 3 |", '<div class="table"><table><thead><tr><th style="text-align: left">a'
     '</th><th style="text-align: right">b</th></tr></thead><tbody><tr><td style="text-align: left">1</td>'
     '<td style="text-align: right">2 | 3</td></tr></tbody></table></div>'),
    ("```\nopen", '<div class="code"><div class="code-head"><span></span><button type="button" class="tool" '
     'data-copy>Copy</button></div><pre><code>open</code></pre></div>'),
]

PLAIN = [  # (source, notification text, spoken text)
    ("**Build** _done_, see `x.py`", "Build done, see x.py", "Build done, see x.py"),
    ("```\na\nb\n```", "a\nb", "There's a 2-line code block here, in the app."),
    ("- a\n- [x] b\n- [ ] c", "- a\n- [x] b\n- [ ] c", "a\ndone: b\nto do: c"),
    ("1. a\n2. b", "1. a\n2. b", "1. a\n2. b"), ("> said\n\nreply", "said\n\nreply", "said\n\nreply"),
    ("| a | b |\n|-|-|\n| 1 | 2 |", "a | b\n1 | 2", "a, b\n1, 2"),
    ("[docs](https://a.b) **[[audio 1]]**", "docs [[audio 1]]", "docs [[audio 1]]"),
    ("```\n[[audio 3]]\n```", "[[audio 3]]", "There's a 1-line code block here, in the app. [[audio 3]]"),
]

TITLES = [("## Build report\nmore", "Build report"), ("**Ship** it", "Ship it"),
                  ("[[audio 1]]\n> quoted", "quoted"), ("```\n```", "Attachment")]

SLOW = {
    "unclosed links": "[a](" * 5000, "unclosed titles": "[a](x '" * 3000 + "[a](x (" * 3000,
    "long destinations": "[a](b" * 5000, "unclosed <": "[a](<" * 5000, "unclosed code": "`` `" * 5000 + "x",
    "quotes in quotes": "> " * 2000 + "x", "lists in lists": "\n".join(" " * (2 * k) + "- x" for k in range(400)),
    "em in em": "*a " * 3000 + "b" + " a*" * 3000, "strong in strong": "**a " * 3000 + "b" + " a**" * 3000,
    "links in links": "[" * 3000 + "x" + "](/a)" * 3000, "brackets then links": "[" * 5000 + "[x](/a)" * 5000,
    "mixed": "_*~~`[" * 3000, "images in images": "![" * 5000 + "](/x)" * 5000,
    "em in images": "![*" * 3000 + "x" + "*](/x)" * 3000, "delimiter run": "*a" * 10000 + "*",
    "underscores": "_a" * 10000, "stars": "* " * 10000, "tildes": "~~a " * 5000,
}


def html(text: str) -> str:
    return md.to_html(text, clip=lambda kind, n: f"<CLIP {n}>" if kind == "audio" else f"<{kind.upper()} {n}>")


def inner(text: str) -> str:
    out = html(text)
    return out[3:-4] if out.startswith("<p>") and out.endswith("</p>") else out


# The quadratic matcher this replaced, as the reference.
def reference(nodes: list, depth: dict) -> list:
    lit = lambda x: x.ch * x.n if isinstance(x, md.Delim) else x  # noqa: E731
    j = 0
    while j < len(nodes):
        c = nodes[j]
        if not isinstance(c, md.Delim) or not c.can_close:
            j += 1
            continue
        k = j - 1
        while k >= 0:
            o = nodes[k]
            if isinstance(o, md.Delim) and o.ch == c.ch and o.can_open:
                if c.ch == "~" or not ((o.can_close or c.can_open) and (o.orig + c.orig) % 3 == 0
                                       and (o.orig % 3 or c.orig % 3)):
                    break
            k -= 1
        if k < 0:
            j += 1
            continue
        o = nodes[k]
        use = 2 if c.ch == "~" or (o.n >= 2 and c.n >= 2) else 1
        tag = "del" if c.ch == "~" else "strong" if use == 2 else "em"
        o.n -= use
        c.n -= use
        nodes[k + 1:j] = [(tag, [lit(x) for x in nodes[k + 1:j]])]
        j = k + 2
        if o.n == 0:
            del nodes[k]
            j -= 1
        if c.n == 0:
            del nodes[j]
    return [lit(x) for x in nodes]


def merged(nodes: list) -> list:
    out = []
    for x in nodes:
        if isinstance(x, tuple) and x[0] in ("em", "strong", "del"):
            x = (x[0], merged(x[1]))
        elif isinstance(x, tuple) and x[0] in ("a", "img"):
            x = (x[0], x[1], merged(x[2]))
        if isinstance(x, str) and out and isinstance(out[-1], str):
            out[-1] += x
        elif x != "":
            out.append(x)
    return out


class Markdown(unittest.TestCase):
    def test_emphasis(self):
        for src, want in EMPHASIS:
            with self.subTest(src=src):
                self.assertEqual(inner(src), want)

    def test_inline(self):
        for src, want in INLINE:
            with self.subTest(src=src):
                self.assertEqual(inner(src), want)
        self.assertEqual(md.to_html("[[audio 1]]"), "<p>[[audio 1]]</p>", "with no clips to play, a marker is text")

    def test_what_stays_text(self):
        for src in NO_LINK:
            with self.subTest(src=src):
                self.assertNotIn("href", html(src))
        self.assertNotIn("<script>", html("<script>alert(1)</script>"))
        self.assertIn("&lt;script&gt;", html("<script>"))
        for src in ['[x](https://a.b/"onmouseover="y)', 'https://a.b/x"onmouseover="y', '[x](/a" "b)']:
            with self.subTest(src=src):
                self.assertNotIn('="y', html(src))
                self.assertNotIn('"b', html(src))
        lang = html("```a\"y\nx\n```")
        self.assertNotIn('"y', lang)
        self.assertIn("a&quot;y", lang)

    def test_blocks(self):
        for src, want in BLOCKS:
            with self.subTest(src=src):
                self.assertEqual(html(src), want)
        code = html("```py\n  x = '<b>'\n\n  y\n```")
        self.assertIn('<span class="lang">py</span>', code)
        self.assertIn("<code>  x = &#x27;&lt;b&gt;&#x27;\n\n  y</code>", code)
        self.assertEqual(html("````\n```\nx\n```\n````").count("<pre>"), 1, "a longer fence holds a shorter one")

    def test_plain(self):
        for src, note, speech in PLAIN:
            with self.subTest(src=src):
                self.assertEqual(md.plain(src), note)
                self.assertEqual(md.plain(src, speech=True), speech)
        for src, want in TITLES:
            with self.subTest(title=src):
                self.assertEqual(IdentityRewriter._title(src), want)

    def test_slow_or_deep(self):
        for name, src in SLOW.items():
            with self.subTest(name=name):
                t0 = time.perf_counter()
                md.to_html(src), md.plain(src), md.plain(src, speech=True)
                self.assertLess(time.perf_counter() - t0, 1)
        deep = html("> " * 40 + "x")
        self.assertEqual(deep.count("<blockquote>"), md.MAX_DEPTH + 1, "quotes past the limit are text")
        self.assertEqual(deep.count("&gt;"), 40 - md.MAX_DEPTH - 1)
        self.assertEqual(html("*a " * 40 + "b" + " a*" * 40).count("<em>"), md.MAX_INLINE)

    def test_emphasis_matches_reference(self):
        rng = random.Random(4)
        fast = md._emphasis
        self.addCleanup(setattr, md, "_emphasis", fast)
        for alphabet in ["*_~a .", "**__a b", "*a[](/ `"]:
            diffs = []
            for _ in range(20000):
                src = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 16)))
                got = merged(md.inline(src))
                md._emphasis = reference
                want = merged(md.inline(src))
                md._emphasis = fast
                if got != want:
                    diffs.append((src, got, want))
            self.assertFalse(diffs[:3], alphabet)


if __name__ == "__main__":
    unittest.main()
