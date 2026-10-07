"""Leaves: the same name gives the same leaf, every variety builds and draws, unfurling stays on the page, PNGs are
well formed, and the generator matches values taken from the JavaScript original."""

import math
import re
import struct
import unittest
import xml.etree.ElementTree as ET
import zlib

from nanotea import leaf

SVG = "{http://www.w3.org/2000/svg}"


def points(text):
    return [(float(a), float(b)) for a, b in re.findall(r"(-?[\d.]+) (-?[\d.]+)", text)]


class Leaf(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(leaf.svg("nanotea"), leaf.svg("nanotea"))
        self.assertIs(leaf.build("nanotea"), leaf.build("  NanoTea "))
        self.assertEqual(leaf.png("nanotea", 32, "#ffffff", "#336699"), leaf.png("nanotea", 32, "#ffffff", "#336699"))
        self.assertNotEqual(leaf.svg("nanotea"), leaf.svg("docs site"))

    def test_matches_the_original(self):
        # varieties and traits as the JavaScript computes them
        for name, variety in (("nanotea", "elm"), ("", "elm"), ("docs site", "ginkgo"), ("ios build", "oak"),
                              ("ingest", "camellia"), ("amber harbour", "fern"), ("paper cut", "maple"),
                              ("\u65e5\u672c\u8a9e", "ginkgo"), ("\U0001f600 emoji", "maple"),
                              ("\u0130stanbul", "elm")):
            self.assertEqual(leaf._pick(leaf._key(name)), variety, name)
        self.assertEqual(leaf.traits("nanotea"), ("elm", ["elm", "13 doubled teeth", "lopsided base"]))
        self.assertEqual(leaf.traits("ingest"), ("camellia", ["camellia", "23 fine teeth", "short tip"]))
        self.assertEqual(len(leaf.build("nanotea").segs), 461)
        self.assertEqual(len(leaf.build("ingest").segs), 275)

    def test_varieties(self):
        self.assertEqual(len(leaf.VARIETIES), 6)
        for v in leaf.VARIETIES:
            g = leaf.build("test", v)
            self.assertEqual((g.variety, g.d[0]), (v, v))
            self.assertGreater(len(g.segs), 100)
            root = ET.fromstring(leaf.svg("test", 1, 64, v))
            self.assertEqual(root.tag, SVG + "svg")
            self.assertEqual(root.get("viewBox"), "-4 -4 72 72")
            self.assertEqual(len(root.findall(f".//{SVG}mask")), 2)

    def test_name_picks_every_variety(self):
        picked = {leaf._pick(f"name {i}") for i in range(60)}
        self.assertEqual(picked, set(leaf.VARIETIES))

    def test_mask_ids_are_prefixed(self):
        a = ET.fromstring(leaf.svg("test", ids="one"))
        b = ET.fromstring(leaf.svg("test", ids="two"))
        ida = {m.get("id") for m in a.iter(SVG + "mask")}
        idb = {m.get("id") for m in b.iter(SVG + "mask")}
        self.assertEqual(len(ida), 2)
        self.assertFalse(ida & idb)
        for p in a.iter(SVG + "path"):
            if p.get("mask"):
                self.assertIn(p.get("mask")[5:-1], ida)

    def test_unfurl_stays_in_the_viewbox(self):
        for v in ("oak", "fern", "ginkgo"):
            for o in (0, .5, 1):
                root = ET.fromstring(leaf.svg("test", o, 64, v))
                g = root.find(f"{SVG}g")
                m = re.match(r"translate\((\S+) (\S+)\) rotate\((\S+)\)", g.get("transform"))
                tx, ty, ang = (float(t) for t in m.groups())
                ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))
                outline = [p.get("d") for p in g.findall(f"{SVG}path") if p.get("mask")]
                pts = [q for d in outline for q in points(d)]
                self.assertTrue(pts)
                for x, y in pts:
                    for u in (x * ca - y * sa + tx, x * sa + y * ca + ty):
                        self.assertTrue(-4 <= u <= 68, (v, o, u))

    def test_bad_arguments(self):
        with self.assertRaisesRegex(ValueError, "known: camellia, elm, oak, maple, ginkgo, fern"):
            leaf.build("x", "birch")
        for bad in (-.01, 1.01, float("nan")):
            with self.assertRaises(ValueError):
                leaf.svg("x", bad)
            with self.assertRaises(ValueError):
                leaf.png("x", 16, "#ffffff", "#000000", unfurl=bad)
        with self.assertRaises(ValueError):
            leaf.svg("x", ids="1a")
        for kw in ({"fg": "white"}, {"bg": "#fff"}, {"radius": .6}, {"scale": 0}, {"px": 0}):
            args = {"px": 16, "fg": "#ffffff", "bg": "#000000", **kw}
            with self.assertRaises(ValueError):
                leaf.png("x", **args)


class Png(unittest.TestCase):
    def chunks(self, data):
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        i, out = 8, []
        while i < len(data):
            n, tag = struct.unpack(">I4s", data[i:i + 8])
            body = data[i + 8:i + 8 + n]
            self.assertEqual(struct.unpack(">I", data[i + 8 + n:i + 12 + n])[0], zlib.crc32(tag + body))
            out.append((tag, body))
            i += 12 + n
        return out

    def pixels(self, data, px, width=None):
        width = width or px
        ch = dict(self.chunks(data))
        self.assertEqual(struct.unpack(">IIBBBBB", ch[b"IHDR"]), (width, px, 8, 6, 0, 0, 0))
        raw = zlib.decompress(ch[b"IDAT"])
        self.assertEqual(len(raw), px * (width * 4 + 1))
        return [raw[y * (width * 4 + 1) + 1:(y + 1) * (width * 4 + 1)] for y in range(px)]

    def test_header_and_size(self):
        for px in (1, 24, 100):
            self.pixels(leaf.png("test", px, "#ffffff", "#4d7a26"), px)

    def test_colours_and_corners(self):
        px = 64
        rows = self.pixels(leaf.png("test", px, "#ffffff", "#4d7a26", radius=.5), px)
        self.assertEqual(rows[0][:4], bytes((0x4d, 0x7a, 0x26, 0)))
        self.assertEqual(rows[px // 2][4:8], bytes((0x4d, 0x7a, 0x26, 255)))
        self.assertEqual(rows[px // 2][-8:-4], bytes((0x4d, 0x7a, 0x26, 255)))
        square = self.pixels(leaf.png("test", px, "#ffffff", "#4d7a26"), px)
        self.assertEqual(square[0][:4], bytes((0x4d, 0x7a, 0x26, 255)))
        self.assertTrue(all(r[3::4] == b"\xff" * px for r in square))
        whites = sum(r[i:i + 3] == b"\xff\xff\xff" for r in square for i in range(0, px * 4, 4))
        self.assertGreater(whites, px * px // 20)

    def test_no_bg_is_nothing_but_the_leaf(self):
        px = 64
        rows = self.pixels(leaf.png("test", px, "#367f57"), px)
        self.assertEqual(rows[0][:4], bytes(4))
        seen = {bytes(r[i:i + 3]) for r in rows for i in range(0, px * 4, 4) if r[i + 3]}
        self.assertEqual(seen, {bytes((0x36, 0x7f, 0x57))})
        alphas = [a for r in rows for a in r[3::4]]
        self.assertGreater(sum(a == 255 for a in alphas), px * px // 20)
        self.assertGreater(sum(a == 0 for a in alphas), px * px // 4)
        with self.assertRaisesRegex(ValueError, "radius rounds the bg square"):
            leaf.png("test", px, "#367f57", radius=.2)

    def test_a_row_is_tiles_a_quarter_apart(self):
        px = 32
        self.assertEqual(leaf.png_row(["api"], px, "#367f57", "#ffffff"), leaf.png("api", px, "#367f57", "#ffffff"))
        rows = self.pixels(leaf.png_row(["api", "ios", "search"], px, "#367f57", "#ffffff"), px, 3 * px + 2 * 8)
        one = self.pixels(leaf.png("ios", px, "#367f57", "#ffffff"), px)
        for y in range(px):
            self.assertEqual(rows[y][(px + 8) * 4:(2 * px + 8) * 4], one[y])
            self.assertEqual(rows[y][px * 4:(px + 8) * 4], bytes(32))
        with self.assertRaisesRegex(ValueError, "at least one name"):
            leaf.png_row([], px, "#367f57")

    def test_unfurl_changes_the_picture(self):
        a = leaf.png("test", 32, "#ffffff", "#000000", unfurl=0)
        b = leaf.png("test", 32, "#ffffff", "#000000", unfurl=1)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
