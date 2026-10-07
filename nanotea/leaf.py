"""Procedural leaves: one leaf per name, as an SVG or a PNG icon.

A name picks a variety (camellia, elm, oak, maple, ginkgo, fern) and every trait of the outline, then veins grow
into it by space colonization. `unfurl` runs from a curled bud (0) to the open leaf (1). Ported from the JavaScript
widget with its arithmetic kept the same: the generator is seeded by FNV-1a and mulberry32 as JS computes them,
and `math.hypot` is replaced by V8's formula where its last bit can reach the output."""

import math
import re
import struct
import threading
import zlib
from collections import OrderedDict
from itertools import accumulate

VARIETIES = ("camellia", "elm", "oak", "maple", "ginkgo", "fern")
_WEIGHTS = (.2, .17, .17, .17, .12, .17)
_M = 0xFFFFFFFF
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff" + "".join(map(chr, range(0x2000, 0x200B)))
_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_HEX = re.compile(r"#([0-9a-fA-F]{6})\Z")
_CACHE_MAX = 128


# JS arithmetic

def _rng(s: str):
    """FNV-1a over the string's characters, then mulberry32. JS iterates by code point and keeps the first UTF-16
    unit of each, so an astral character contributes its high surrogate."""
    h = 2166136261
    for c in s:
        o = ord(c)
        if o > 0xFFFF:
            o = 0xD800 + ((o - 0x10000) >> 10)
        h = ((h ^ o) * 16777619) & _M

    def nxt():
        nonlocal h
        h = (h + 0x6D2B79F5) & _M
        t = ((h ^ (h >> 15)) * (h | 1)) & _M
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & _M)) & _M
        return (t ^ (t >> 14)) / 4294967296
    return nxt


def _round(x: float) -> int:
    """Math.round: halves go up."""
    f = math.floor(x)
    return f + 1 if x - f >= .5 else f


def _hyp(a: float, b: float) -> float:
    """Math.hypot as V8 computes it (scaled by the larger, then sqrt), not math.hypot."""
    a = abs(a)
    b = abs(b)
    m = a if a > b else b
    if m == 0:
        return 0.0
    a /= m
    b /= m
    return math.sqrt(a * a + b * b) * m


def _n2(v: float) -> float:
    return _round(v * 100) / 100


def _fmt(v: float) -> str:
    return str(int(v)) if v == int(v) else repr(v)


def _cl(v: float) -> float:
    return min(1, max(0, v))


def _ease(v: float) -> float:
    v = _cl(v)
    return v * v * (3 - 2 * v)


def _saw(x: float) -> float:
    return x - math.floor(x)


def _key(name: str) -> str:
    return name.strip(_JS_SPACE).lower() or "nanotea"


# Outline

def _env(t, peak, eb, et, acu):
    if t <= 0 or t >= 1:
        return 0
    u = .5 * t / peak if t < peak else .5 + .5 * (t - peak) / (1 - peak)
    w = math.pow(math.sin(math.pi * u), eb if t < peak else et)
    if acu:
        w *= 1 - acu * _ease((t - .72) / .28)
    return w


def _axis_poly(fr, fl, length, n=200):
    p = [(fr(i / n), -length * (i / n)) for i in range(n + 1)]
    p += [(-fl(i / n), -length * (i / n)) for i in range(n, -1, -1)]
    return p


def _in_poly(x, y, p):
    c = False
    j = len(p) - 1
    for i in range(len(p)):
        a = p[i]
        b = p[j]
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            c = not c
        j = i
    return c


def _clip_x(p, sg):
    o = []
    for i in range(len(p)):
        a = p[i]
        b = p[(i + 1) % len(p)]
        ia = sg * a[0] >= 0
        ib = sg * b[0] >= 0
        if ia:
            o.append(a)
        if ia != ib:
            t = a[0] / (a[0] - b[0])
            o.append((0, a[1] + (b[1] - a[1]) * t))
    return o


def _resample(pts, step):
    o = [pts[0]]
    acc = 0
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        seg = _hyp(x1 - x0, y1 - y0)
        while acc + seg >= step:
            t = (step - acc) / seg
            x0 += (x1 - x0) * t
            y0 += (y1 - y0) * t
            o.append((x0, y0))
            seg = _hyp(x1 - x0, y1 - y0)
            acc = 0
        acc += seg
    return o


def _shape(variety, R):
    """The variety's outline polygons, vein seeds ({'from': (chain, frac) or None, 'pts': [...]}), petiole, traits."""
    L = 50
    polys = []
    seeds = []
    pet = 3 + R() * 5
    d = [variety]
    if variety == "camellia":
        W = .38 + R() * .18
        pk = .4 + R() * .15
        eb = .7 + R() * .6
        et = .8 + R() * .5
        acu = .25 + R() * .45
        n = _round(16 + R() * 18)
        dp = .03 + R() * .06
        as_ = (R() - .5) * .14

        def f(s):
            return lambda t: _env(t, pk, eb, et, acu) * W * L * .5 * (1 + s * as_) * (
                1 - dp * math.pow(_saw(t * n), 1.5) if .06 < t < .95 else 1)
        polys = [_axis_poly(f(1), f(-1), L)]
        seeds = [{"from": None, "pts": [(0, 0), (0, -.96 * L)]}]
        d += [f"{n} fine teeth", "long tip" if acu > .5 else "short tip"]
    elif variety == "elm":
        W = .5 + R() * .14
        pk = .36 + R() * .12
        eb = .35 + R() * .3
        et = .9 + R() * .4
        acu = .15 + R() * .3
        off = .05 + R() * .08
        n = _round(10 + R() * 7)
        d1 = .07 + R() * .07
        d2 = .025 + R() * .03
        side = 1 if R() < .5 else -1

        def f(s):
            def g(t):
                tt = t if s == side else _cl((t - off) / (1 - off))
                return _env(tt, pk, eb, et, acu) * W * L * .5 * (
                    1 - d1 * math.pow(_saw(t * n), 1.4) - d2 * math.pow(_saw(t * n * 3), 1.4) if .05 < t < .96 else 1)
            return g
        polys = [_axis_poly(f(1), f(-1), L)]
        seeds = [{"from": None, "pts": [(0, 0), (0, -.95 * L)]}]
        d += [f"{n} doubled teeth", "lopsided base"]
    elif variety == "oak":
        W = .6 + R() * .2
        pk = .55 + R() * .15
        eb = .9 + R() * .5
        et = .45 + R() * .3
        lobes = 3 + math.floor(R() * 3)
        dp = .42 + R() * .28
        ph = R() * 6.28

        def f(s):
            def g(t):
                win = _ease((t - .06) / .14) * _ease((.97 - t) / .12)
                c = .5 + .5 * math.cos(2 * math.pi * lobes * t + ph + (0 if s > 0 else 2.2))
                return _env(t, pk, eb, et, 0) * W * L * .5 * (1 - dp * win * math.pow(c, 3))
            return g
        polys = [_axis_poly(f(1), f(-1), L)]
        seeds = [{"from": None, "pts": [(0, 0), (0, -.95 * L)]}]
        d += [f"{lobes} lobes a side", "deep sinuses" if dp > .55 else "shallow sinuses"]
    elif variety == "maple":
        n = (3, 5, 5, 7)[math.floor(R() * 4)]
        spread = (.8 if n == 3 else 1.3 if n == 5 else 1.75) + (R() - .5) * .25
        sin = .3 + R() * .22
        p = .55 + R() * .6
        tn = _round(20 + R() * 30)
        td = .03 + R() * .07
        R0 = 26
        hw = spread * 2 / (n - 1) * .62
        lob = []
        for i in range(n):
            a = abs(i / (n - 1) * 2 - 1)
            phi = spread * (i / (n - 1) * 2 - 1)
            lob.append((phi, R0 * (1 - .4 * a * a) * (.88 + R() * .24)))
        pts = []
        for i in range(481):
            phi = -math.pi + 2 * math.pi * i / 480
            base = R0 * (sin + (.1 - sin) * _ease((abs(phi) - spread) / (math.pi - spread)))
            r = base
            for pi, li in lob:
                r = max(r, li * math.pow(max(0, 1 - abs(phi - pi) / hw), p))
            r *= 1 - td * math.pow(_saw(phi * tn / 6.283), 1.4)
            pts.append((r * math.sin(phi), -r * math.cos(phi)))
        polys = [pts]
        seeds = [{"from": None, "pts": [(0, 0)]}]
        for phi, li in lob:
            seeds.append({"from": (0, 0), "pts": [
                (0, 0),
                (math.sin(phi) * li * .45, -math.cos(phi) * li * .45 - 1),
                (math.sin(phi) * li * .9, -math.cos(phi) * li * .9)]})
        d += [f"{n} palm lobes", f"{tn} teeth"]
    elif variety == "ginkgo":
        spread = .7 + R() * .55
        notch = .15 + R() * .3 if R() < .65 else 0
        wave = .015 + R() * .04
        waves = _round(10 + R() * 14)
        R0 = 34
        pet = 8 + R() * 6
        pts = [(0, 0)]
        for i in range(201):
            phi = -spread + 2 * spread * i / 200
            r = R0 * (1 - notch * math.exp(-((phi / .09) ** 2))) * (1 + wave * math.sin(phi * waves * 3))
            pts.append((r * math.sin(phi), -r * math.cos(phi)))
        polys = [pts]
        seeds = [{"from": None, "pts": [(0, 0), (0, -1.5)]}]
        d += ["notched fan" if notch else "whole fan", f"spread {_round(spread * 114)} deg"]
    elif variety == "fern":
        n = _round(10 + R() * 7)
        Pm = 17 + R() * 7
        a0 = (58 + R() * 16) * math.pi / 180
        pw = .22 + R() * .12
        lob = _round(4 + R() * 6) if R() < .55 else 0
        ld = .3 + R() * .3
        off = R() * .5
        seeds = [{"from": None, "pts": [(0, 0), (0, -L)]}]
        polys = [[(.8, 0), (.15, -L), (-.15, -L), (-.8, 0)]]
        for side in (1, -1):
            for k in range(1, n + 1):
                u = (k + (off if side < 0 else 0)) / (n + 1)
                s = L * (.04 + .92 * u)
                pl = Pm * math.pow(math.sin(math.pi * math.pow(u, .75)), .8)
                if pl < 2.5:
                    continue
                a = a0 * (1 - .25 * u)
                dx = side * math.sin(a)
                dy = -math.cos(a)
                bx = side * .4
                by = -s
                P = []
                Q = []
                for i in range(41):
                    uu = i / 40
                    w = pl * pw * .5 * _env(uu, .3, .5, .9, 0) * (
                        1 - ld * math.pow(.5 + .5 * math.cos(2 * math.pi * lob * uu), 3) * _ease((uu - .08) / .1)
                        if lob else 1)
                    cx = bx + dx * uu * pl
                    cy = by + dy * uu * pl
                    P.append((cx - dy * w, cy + dx * w))
                    Q.append((cx + dy * w, cy - dx * w))
                polys.append(P + Q[::-1])
                seeds.append({"from": (0, s / L), "pts": [(bx, by), (bx + dx * pl * .9, by + dy * pl * .9)]})
        d += [f"{n} pinnae a side", f"pinnae lobed {lob}" if lob else "pinnae plain"]
    return polys, seeds, pet, d


# Veins and widths

class Built:
    """A leaf's geometry in its own units, about 56 across: outline polygons, vein segments (x0, y0, x1, y1, width),
    petiole length, trait lines `d` (the variety first), and the placement traits ang, bend and dir."""

    __slots__ = ("variety", "polys", "segs", "pet", "d", "smax", "cx", "cy", "ang", "bend", "dir")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _pick(k: str) -> str:
    """The variety a name chooses, by weighted draw."""
    x = _rng(k + ":leaf")()
    for i in range(6):
        x -= _WEIGHTS[i]
        if x < 0:
            return VARIETIES[i]
    return "fern"


def _build(k: str, forced: str | None) -> Built:
    variety = _pick(k) if forced is None else forced
    R = _rng(k + ":" + variety)
    polys0, seeds, pet, d = _shape(variety, R)
    mnx, mxx, mny, mxy = 1e9, -1e9, 1e9, pet
    for p in polys0:
        for a, b in p:
            mnx = min(mnx, a)
            mxx = max(mxx, a)
            mny = min(mny, b)
            mxy = max(mxy, b)
    sc = 56 / max(mxx - mnx, mxy - mny)
    polys = [[(a * sc, b * sc) for a, b in p] for p in polys0]
    D, Ri, Kd, sp = .8, 6, 1.25, 1.9
    # nodes: parallel coordinate lists and parent index; the grid hashes cells of Ri units, as in the original
    nx, ny, npar = [], [], []
    grid: dict[int, list[int]] = {}
    floor = math.floor

    def add(x, y, par):
        nx.append(x)
        ny.append(y)
        npar.append(par)
        n = len(nx) - 1
        key = floor(x / Ri) * 10007 + floor(y / Ri)
        lst = grid.get(key)
        if lst is None:
            grid[key] = [n]
        else:
            lst.append(n)
        return n

    chains: list[list[int]] = []
    for s in seeds:
        pts = _resample([(a * sc, b * sc) for a, b in s["pts"]], D)
        par = -1
        if s["from"]:
            c = chains[s["from"][0]]
            par = c[min(len(c) - 1, _round(s["from"][1] * (len(c) - 1)))]
        ids = []
        for a, b in pts:
            par = add(a, b, par)
            ids.append(par)
        chains.append(ids)

    # bounding boxes let most polygons be skipped; the margin keeps the test equal to the plain one
    boxes = []
    for p in polys:
        xs = [q[0] for q in p]
        ys = [q[1] for q in p]
        boxes.append((min(xs) - 1e-9, max(xs) + 1e-9, min(ys) - 1e-9, max(ys) + 1e-9, p))

    def inside(x, y):
        for x0, x1, y0, y1, p in boxes:
            if x0 <= x <= x1 and y0 <= y <= y1 and _in_poly(x, y, p):
                return True
        return False

    # attractors, kept at least sp apart and clear of the seed veins
    attractors = []
    ag: dict[int, list[tuple[float, float]]] = {}
    sp2 = sp * sp
    Kd2 = Kd * Kd
    ymax = min(0, mxy)
    for _ in range(7000):
        ax = mnx * sc + R() * (mxx - mnx) * sc
        ay = mny * sc + R() * (ymax - mny) * sc
        if not inside(ax, ay):
            continue
        cx = floor(ax / sp)
        cy = floor(ay / sp)
        ok = True
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                a = ag.get((cx + i) * 10007 + cy + j)
                if a:
                    for qx, qy in a:
                        dx = qx - ax
                        dy = qy - ay
                        if dx * dx + dy * dy < sp2:
                            ok = False
        if not ok:
            continue
        gx = floor(ax / Ri)
        gy = floor(ay / Ri)
        hit = False
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                a = grid.get((gx + i) * 10007 + gy + j)
                if a:
                    for n in a:
                        dx = nx[n] - ax
                        dy = ny[n] - ay
                        if dx * dx + dy * dy < Kd2:
                            hit = True
        if hit:
            continue
        key = cx * 10007 + cy
        lst = ag.get(key)
        if lst is None:
            ag[key] = [(ax, ay)]
        else:
            lst.append((ax, ay))
        # the nine grid keys around the attractor never change, so they are made once
        attractors.append((ax, ay, tuple((gx + i) * 10007 + gy + j for i in (-1, 0, 1) for j in (-1, 0, 1))))

    A = attractors
    Ri2 = Ri * Ri
    crowd2 = (D * .35) * (D * .35)
    gget = grid.get
    for _ in range(320):
        if not A:
            break
        acc: dict[int, list[float]] = {}
        keep = []
        for att in A:
            ax, ay, keys = att
            best = -1
            bd = Ri2
            for key in keys:
                lst = gget(key)
                if lst:
                    for n in lst:
                        dx = nx[n] - ax
                        dy = ny[n] - ay
                        d2 = dx * dx + dy * dy
                        if d2 < bd:
                            bd = d2
                            best = n
            if best < 0:
                keep.append(att)
                continue
            if bd < Kd2:
                continue
            keep.append(att)
            l = math.sqrt(bd)
            g = acc.get(best)
            if g is None:
                g = acc[best] = [0, 0]
            g[0] += (ax - nx[best]) / l
            g[1] += (ay - ny[best]) / l
        A = keep
        grew = 0
        for i, g in acc.items():
            l = _hyp(g[0], g[1]) or 1
            dx = g[0] / l + (R() - .5) * .35
            dy = g[1] / l + (R() - .5) * .35
            l = _hyp(dx, dy) or 1
            x = nx[i] + dx / l * D
            y = ny[i] + dy / l * D
            gx = floor(x / Ri)
            gy = floor(y / Ri)
            crowd = False
            for a in (-1, 0, 1):
                for b in (-1, 0, 1):
                    lst = gget((gx + a) * 10007 + gy + b)
                    if lst:
                        for n in lst:
                            ex = nx[n] - x
                            ey = ny[n] - y
                            if ex * ex + ey * ey < crowd2:
                                crowd = True
            if crowd:
                continue
            add(x, y, i)
            grew += 1
        if not grew:
            break

    desc = [1] * len(nx)
    for i in range(len(nx) - 1, -1, -1):
        if npar[i] >= 0:
            desc[npar[i]] += desc[i]
    segs = []
    for i in range(len(nx)):
        p = npar[i]
        if p >= 0:
            segs.append((nx[p], ny[p], nx[i], ny[i], min(1.5, .075 * math.pow(desc[i], .42))))
    smax = 0
    for p in polys:
        for _, b in p:
            smax = max(smax, -b)
    return Built(
        variety=variety, polys=polys, segs=segs, pet=pet * sc, d=d, smax=smax,
        cx=(mnx + mxx) / 2 * sc, cy=(mny + mxy) / 2 * sc,
        ang=(_rng(k + ":ang")() - .5) * 26, bend=(_rng(k + ":bend")() - .5) * .5,
        dir=1 if _rng(k + ":dir")() < .5 else -1)


_cache: OrderedDict[tuple[str, str | None], Built] = OrderedDict()
_cache_lock = threading.Lock()
_key_locks: dict[tuple[str, str | None], threading.Lock] = {}


def _check_variety(variety):
    if variety is not None and variety not in VARIETIES:
        raise ValueError(f"unknown variety {variety!r}; known: {', '.join(VARIETIES)}")


def build(name: str, variety: str | None = None) -> Built:
    """The leaf for `name`, built once per (name, variety) and shared; `variety=None` lets the name pick."""
    if not isinstance(name, str):
        raise TypeError("leaf name must be a string")
    _check_variety(variety)
    k = _key(name)
    ck = (k, variety)
    with _cache_lock:
        got = _cache.get(ck)
        if got is not None:
            _cache.move_to_end(ck)
            return got
        lock = _key_locks.setdefault(ck, threading.Lock())
    with lock:
        with _cache_lock:
            got = _cache.get(ck)
        if got is None:
            got = _build(k, variety)
            with _cache_lock:
                _cache[ck] = got
                while len(_cache) > _CACHE_MAX:
                    _cache.popitem(last=False)
        with _cache_lock:
            _key_locks.pop(ck, None)
        return got


def traits(name: str, variety: str | None = None) -> tuple[str, list[str]]:
    """The variety and its trait lines; the first line is the variety again, as the original lists them."""
    g = build(name, variety)
    return g.variety, list(g.d)


# Unfurling

def _frame(g: Built, o: float, size: float) -> dict:
    """The leaf at unfurl `o`, drawn `size` px across the viewBox: the petiole, the two halves' outline paths and
    vein levels, all in viewBox units rounded to hundredths, plus the group's translate and rotation."""
    v = g.variety
    e = _ease(o)
    fern = v == "fern"
    fan = v in ("maple", "ginkgo")
    proj = 1 if fern or fan else .32 + .68 * e
    spread = .5 + .5 * e if fan else 1
    sc = .84 + .16 * e
    curl = (0 if fern else .3 if fan else .8) * (1 - e) * g.dir
    c0 = .3 + .7 * e if fern else 1
    coil = (1 - e) * 5 * g.dir if fern else 0

    def q(u):
        return _cl((u - c0) / max(1e-6, 1 - c0))
    N = 240
    length = g.smax * sc
    ds = length / N
    M = [(0, 0, 0)]
    x = 0
    y = 0
    for i in range(1, N + 1):
        u = i / N
        h = g.bend * u + curl * u * u + coil * q(u) ** 2
        x += math.sin(h) * ds
        y -= math.cos(h) * ds
        M.append((x, y, h))

    def deform(px, py):
        if spread != 1:
            r = _hyp(px, py)
            ph = math.atan2(px, -py) * spread
            px = r * math.sin(ph)
            py = -r * math.cos(ph)
        s = -py * sc
        lat = px * sc * (proj if px > 0 else 1)
        if fern:
            lat *= 1 - .65 * q(s / length)
        if s <= 0:
            return lat, -s
        f = min(N - .001, s / ds)
        i = math.floor(f)
        u = f - i
        a = M[i]
        b = M[i + 1]
        h = a[2] + (b[2] - a[2]) * u
        return a[0] + (b[0] - a[0]) * u + math.cos(h) * lat, a[1] + (b[1] - a[1]) * u + math.sin(h) * lat

    def pt(px, py):
        r = deform(px, py)
        return _n2(r[0]), _n2(r[1])

    unit = size / 62
    halves = []
    for sg in (-1, 1):
        paths = []
        for p in g.polys:
            c = _clip_x(p, sg)
            if len(c) > 2:
                paths.append([pt(a, b) for a, b in c])
        levels: dict[int, list] = {}
        for s in g.segs:
            mx = (s[0] + s[2]) / 2
            if mx > .05 if sg < 0 else mx < -.05:
                continue
            if s[4] * unit < .22:
                continue
            lv = _round(math.log2(s[4] / .075) * 2)
            a = pt(s[0], s[1])
            b = pt(s[2], s[3])
            levels.setdefault(lv, [s[4], []])[1].append((a[0], a[1], b[0], b[1]))
        out = []
        for w, segs in levels.values():
            al = .2 + .42 * _cl(w / 1.2)
            out.append((_n2(w), _round(255 * (1 - al)), segs))
        halves.append((paths, out))
    p0 = pt(0, 0)
    p1 = pt(0, g.pet)
    return {"tx": _n2(32 - g.cx), "ty": _n2(32 - g.cy), "ang": _n2(g.ang), "pet": (p0, p1, _n2(max(1.3, 1 / unit))),
            "halves": halves}


def _check_unfurl(unfurl):
    if not isinstance(unfurl, (int, float)) or not 0 <= unfurl <= 1:
        raise ValueError(f"unfurl must be from 0 to 1, got {unfurl!r}")


def svg(name: str, unfurl: float = 1.0, size: float = 64, variety: str | None = None, ids: str = "l") -> str:
    """A standalone SVG of the leaf in currentColor. `size` is the width and height in px and sets how much vein
    detail is kept; `ids` prefixes the mask ids so several can share a page."""
    _check_unfurl(unfurl)
    if not isinstance(size, (int, float)) or not size > 0:
        raise ValueError(f"size must be positive, got {size!r}")
    if not isinstance(ids, str) or not _ID.match(ids):
        raise ValueError(f"ids must start with a letter or underscore and use letters, digits, _ . -: got {ids!r}")
    f = _frame(build(name, variety), unfurl, size)

    def mask(mid, levels):
        paths = "".join(
            f'<path d="{"".join(f"M{_fmt(a)} {_fmt(b)}L{_fmt(c)} {_fmt(d)}" for a, b, c, d in segs)}" '
            f'stroke="rgb({gr},{gr},{gr})" stroke-width="{_fmt(w)}"/>' for w, gr, segs in levels)
        return (f'<mask id="{mid}" maskUnits="userSpaceOnUse" x="-100" y="-100" width="200" height="200">'
                f'<rect x="-100" y="-100" width="200" height="200" fill="#fff"/>'
                f'<g fill="none" stroke-linecap="round">{paths}</g></mask>')

    def outline(paths):
        return "".join("M" + "L".join(f"{_fmt(a)} {_fmt(b)}" for a, b in p) + "Z" for p in paths)
    (lp, ll), (rp, rl) = f["halves"]
    (x0, y0), (x1, y1), pw = f["pet"]
    a, b = f"{ids}-l", f"{ids}-r"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(size)}" height="{_fmt(size)}" '
            f'viewBox="-4 -4 72 72" fill="currentColor" aria-hidden="true">'
            f'<defs>{mask(a, ll)}{mask(b, rl)}</defs>'
            f'<g transform="translate({_fmt(f["tx"])} {_fmt(f["ty"])}) rotate({_fmt(f["ang"])})">'
            f'<path d="M{_fmt(x0)} {_fmt(y0)}L{_fmt(x1)} {_fmt(y1)}" stroke="currentColor" '
            f'stroke-width="{_fmt(pw)}" stroke-linecap="round"/>'
            f'<path mask="url(#{a})" d="{outline(lp)}"/><path mask="url(#{b})" d="{outline(rp)}"/></g></svg>')


# PNG

_SS = 8  # sub-scanlines per pixel row; coverage along a row is exact


class _Plane:
    """Fill coverage with exact horizontal and sampled vertical extent: spans go in per sub-scanline."""

    def __init__(self, px):
        self.px = px
        self.rows: dict[int, list] = {}

    def span(self, sub, x0, x1):
        px = self.px
        if x0 < 0:
            x0 = 0.0
        if x1 > px:
            x1 = float(px)
        if x1 <= x0:
            return
        y = sub // _SS
        row = self.rows.get(y)
        if row is None:
            row = self.rows[y] = [[0.0] * (px + 2), [0.0] * (px + 2), px, 0]
        direct, diff = row[0], row[1]
        w = 1 / _SS
        i0 = int(x0)
        i1 = int(x1)
        if i0 == i1:
            direct[i0] += (x1 - x0) * w
        else:
            direct[i0] += (i0 + 1 - x0) * w
            direct[i1] += (x1 - i1) * w
            diff[i0 + 1] += w
            diff[i1] -= w
        if i0 < row[2]:
            row[2] = i0
        if i1 > row[3]:
            row[3] = i1

    def finish(self):
        """Row index to (first pixel, last pixel, coverage list)."""
        return {y: (lo, hi, [a + b for a, b in zip(direct, accumulate(diff))])
                for y, (direct, diff, lo, hi) in self.rows.items()}


def _fill(plane, polys, px):
    """Nonzero fill of the polygons (pixel coordinates) into the plane."""
    cross: dict[int, list] = {}
    top = px * _SS
    for p in polys:
        for i in range(len(p)):
            x0, y0 = p[i - 1]
            x1, y1 = p[i]
            if y0 == y1:
                continue
            d = 1 if y1 > y0 else -1
            if d < 0:
                x0, y0, x1, y1 = x1, y1, x0, y0
            j0 = max(0, math.ceil(y0 * _SS - .5))
            j1 = min(top, math.ceil(y1 * _SS - .5))
            slope = (x1 - x0) / (y1 - y0)
            for j in range(j0, j1):
                cross.setdefault(j, []).append((x0 + ((j + .5) / _SS - y0) * slope, d))
    for j, xs in cross.items():
        xs.sort()
        w = 0
        start = 0.0
        for x, d in xs:
            was = w
            w += d
            if was == 0:
                start = x
            elif w == 0:
                plane.span(j, start, x)


def _capsules(segs, r, px):
    """Coverage of the union of round-capped segments of radius r, as {pixel index: coverage}."""
    spans: dict[int, list] = {}
    top = px * _SS
    r2 = r * r
    sqrt = math.sqrt
    for ax, ay, bx, by in segs:
        dx = bx - ax
        dy = by - ay
        ln = math.hypot(dx, dy)
        if ln > 0:
            nxr = -dy / ln * r
            nyr = dx / ln * r
            edges = (((ax + nxr, ay + nyr), (bx + nxr, by + nyr)), ((ax - nxr, ay - nyr), (bx - nxr, by - nyr)))
        else:
            edges = ()
        j0 = max(0, math.ceil((min(ay, by) - r) * _SS - .5))
        j1 = min(top, math.ceil((max(ay, by) + r) * _SS - .5))
        for j in range(j0, j1):
            y = (j + .5) / _SS
            lo = 1e18
            hi = -1e18
            for cx, cy in ((ax, ay), (bx, by)):
                t = y - cy
                t2 = r2 - t * t
                if t2 >= 0:
                    h = sqrt(t2)
                    lo = min(lo, cx - h)
                    hi = max(hi, cx + h)
            for (x0, y0), (x1, y1) in edges:
                if (y0 <= y < y1) or (y1 <= y < y0):
                    x = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
                    lo = min(lo, x)
                    hi = max(hi, x)
            if hi >= lo:
                spans.setdefault(j, []).append((lo, hi))
    cov: dict[int, float] = {}
    w = 1 / _SS
    for j, lst in spans.items():
        lst.sort()
        base = (j // _SS) * px
        cur0, cur1 = lst[0]
        merged = []
        for a, b in lst[1:]:
            if a <= cur1:
                if b > cur1:
                    cur1 = b
            else:
                merged.append((cur0, cur1))
                cur0, cur1 = a, b
        merged.append((cur0, cur1))
        for x0, x1 in merged:
            x0 = max(0.0, x0)
            x1 = min(float(px), x1)
            if x1 <= x0:
                continue
            i0 = int(x0)
            i1 = int(x1)
            if i0 == i1:
                cov[base + i0] = cov.get(base + i0, 0.0) + (x1 - x0) * w
            else:
                cov[base + i0] = cov.get(base + i0, 0.0) + (i0 + 1 - x0) * w
                if x1 > i1:
                    cov[base + i1] = cov.get(base + i1, 0.0) + (x1 - i1) * w
                for i in range(i0 + 1, i1):
                    cov[base + i] = cov.get(base + i, 0.0) + w
    return cov


def _rgb(c: str, what: str):
    m = _HEX.match(c) if isinstance(c, str) else None
    if not m:
        raise ValueError(f"{what} must be a colour like #rrggbb, got {c!r}")
    v = int(m.group(1), 16)
    return v >> 16, (v >> 8) & 255, v & 255


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def png(name: str, px: int, fg: str, bg: str | None = None, radius: float = 0.0, scale: float = .9,
        unfurl: float = 1.0, variety: str | None = None) -> bytes:
    """An RGBA PNG, px by px: the leaf in fg, `scale` of the canvas wide and centred, veins blended as the SVG's
    masks do, on a bg square (rounded by `radius`, a fraction of px; 0 is a full square) or, with no bg, on
    nothing."""
    return _encode(px, px, _pixels(name, px, fg, bg, radius, scale, unfurl, variety))


def png_row(names: list[str], px: int, fg: str, bg: str | None = None, radius: float = 0.0, scale: float = .9,
            unfurl: float = 1.0, variety: str | None = None) -> bytes:
    """Several leaves in one PNG, each a px square as `png` draws it, a quarter of one apart on nothing."""
    if not names:
        raise ValueError("a row needs at least one name")
    tiles = [_pixels(name, px, fg, bg, radius, scale, unfurl, variety) for name in names]
    gap = bytes(4 * (px // 4))
    return _encode(len(tiles) * px + (len(tiles) - 1) * (px // 4), px,
                   [gap.join(t[y] for t in tiles) for y in range(px)])


def _encode(width: int, height: int, rows: list) -> bytes:
    raw = b"".join(b"\0" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))


def _pixels(name, px, fg, bg, radius, scale, unfurl, variety) -> list[bytearray]:
    """The picture's rows, RGBA."""
    if not isinstance(px, int) or isinstance(px, bool) or px < 1:
        raise ValueError(f"px must be a positive integer, got {px!r}")
    fr, fgc, fb = _rgb(fg, "fg")
    br, bgc, bb = (0, 0, 0) if bg is None else _rgb(bg, "bg")
    if not isinstance(radius, (int, float)) or not 0 <= radius <= .5:
        raise ValueError(f"radius must be from 0 to 0.5 of px, got {radius!r}")
    if bg is None and radius:
        raise ValueError("radius rounds the bg square; give a bg, or no radius")
    if not isinstance(scale, (int, float)) or not 0 < scale <= 1:
        raise ValueError(f"scale must be above 0 and at most 1, got {scale!r}")
    _check_unfurl(unfurl)
    size = px * scale
    f = _frame(build(name, variety), unfurl, size)

    # leaf units to canvas pixels: rotate, translate, then the viewBox's scale and the centring
    k = size / 72
    a = math.radians(f["ang"])
    ca, sa = math.cos(a), math.sin(a)
    org = (px - size) / 2 + 4 * k

    def place(x, y):
        return org + (x * ca - y * sa + f["tx"]) * k, org + (x * sa + y * ca + f["ty"]) * k

    # background: full rows are one constant; only rows touched by the corners need coverage
    rad = radius * px
    bgp = _Plane(px)
    if rad > 0:
        for j in (list(range(math.ceil(rad) * _SS)) + list(range(math.floor(px - rad) * _SS, px * _SS))):
            y = (j + .5) / _SS
            t = rad - y if y < rad else y - (px - rad) if y > px - rad else None
            if t is None:
                bgp.span(j, 0, px)
            else:
                xl = rad - math.sqrt(max(0.0, rad * rad - t * t))
                bgp.span(j, xl, px - xl)
    bgrows = bgp.finish()
    solid = bytes((br, bgc, bb, 255)) * px
    clear = [0.0] * px

    def base_row(y):
        if bg is None:
            return bytearray(4 * px), clear
        r = bgrows.get(y)
        if r is None:
            return bytearray(solid), None
        out = bytearray()
        for c in r[2][:px]:
            out += bytes((br, bgc, bb, min(255, _round(c * 255))))
        return out, r[2]

    # the leaf: the petiole, then each half's outline under its own vein mask
    (p0, p1, pw), halves = f["pet"], f["halves"]
    pet = _capsules([(*place(*p0), *place(*p1))], pw * k / 2, px)
    layers = []
    for paths, levels in halves:
        plane = _Plane(px)
        _fill(plane, [[place(x, y) for x, y in p] for p in paths], px)
        mask: dict[int, float] = {}
        for w, gr, segs in levels:
            for idx, c in _capsules([(*place(s[0], s[1]), *place(s[2], s[3])) for s in segs], w * k / 2, px).items():
                m = mask.get(idx, 1.0)
                mask[idx] = m * (1 - c) + c * gr / 255
        layers.append((plane.finish(), mask))
    pet_rows: dict[int, list[int]] = {}
    for idx in pet:
        pet_rows.setdefault(idx // px, []).append(idx % px)
    leaf_rows = set(pet_rows)
    for cov, _ in layers:
        leaf_rows |= cov.keys()

    rows = []
    for y in range(px):
        row, bga = base_row(y)
        if y in leaf_rows:
            base = y * px
            lo, hi = px, -1
            for cov, _ in layers:
                if y in cov:
                    lo = min(lo, cov[y][0])
                    hi = max(hi, cov[y][1])
            for x in pet_rows.get(y, ()):
                lo = min(lo, x)
                hi = max(hi, x)
            hi = min(hi, px - 1)
            alphas = [(cov.get(y), mask) for cov, mask in layers]
            for x in range(lo, hi + 1):
                keep = 1 - pet.get(base + x, 0.0)
                for r, mask in alphas:
                    if r is not None:
                        keep *= 1 - r[2][x] * mask.get(base + x, 1.0)
                al = 1 - keep
                if al <= 0:
                    continue
                ba = 1.0 if bga is None else bga[x]
                oa = al + ba * (1 - al)
                if oa <= 0:
                    continue
                o = x * 4
                row[o] = min(255, int((fr * al + br * ba * (1 - al)) / oa + .5))
                row[o + 1] = min(255, int((fgc * al + bgc * ba * (1 - al)) / oa + .5))
                row[o + 2] = min(255, int((fb * al + bb * ba * (1 - al)) / oa + .5))
                row[o + 3] = min(255, int(oa * 255 + .5))
        rows.append(row)
    return rows


# The command line

def main(argv: list[str]) -> None:
    """nanotea leaf: a name's leaf as an SVG or a PNG, or its traits; several names make a row."""
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="nanotea leaf", description="A name's leaf, as the app draws it: an SVG on "
                                "stdout (in currentColor, or --color), a PNG with --png, or its traits.")
    p.add_argument("names", nargs="+", metavar="NAME",
                   help="what the leaf grows from: an agent's name, or the app's; several make a row of leaves")
    p.add_argument("--variety", choices=VARIETIES, help="this variety instead of the name's own")
    p.add_argument("--unfurl", type=float, default=1.0, help="0, a curled bud, to 1, the open leaf (the default)")
    p.add_argument("--size", type=float, default=64, help="SVG: the pixels it is drawn for; sets how fine the veins "
                   "go (default 64)")
    p.add_argument("--color", help="the leaf's color, #rrggbb; SVG default currentColor")
    p.add_argument("--png", type=int, metavar="PX", help="a PNG, each leaf PX square; needs --color and --out")
    p.add_argument("--bg", help="PNG: the square's color, #rrggbb; without it, the leaf is on nothing")
    p.add_argument("--radius", type=float, default=0.0, help="PNG: corner radius, a fraction of PX, 0 to 0.5")
    p.add_argument("--scale", type=float, default=.9, help="PNG: the leaf's width, a fraction of PX (default 0.9)")
    p.add_argument("--out", help="write here instead of stdout")
    p.add_argument("--traits", action="store_true", help="print the variety and its traits, and nothing else")
    a = p.parse_args(argv)
    try:
        if a.traits:
            for name in a.names:
                print(f"{name}: {', '.join(traits(name, a.variety)[1])}")
            return
        if a.png is not None:
            if not (a.color and a.out):
                p.error("--png needs --color and --out")
            body = png_row(a.names, a.png, fg=a.color, bg=a.bg, radius=a.radius, scale=a.scale, unfurl=a.unfurl,
                           variety=a.variety)
        else:
            text = _row([svg(name, unfurl=a.unfurl, size=a.size, variety=a.variety, ids=f"l{i}")
                         for i, name in enumerate(a.names)], a.size)
            if a.color:
                if not _HEX.match(a.color):
                    raise ValueError(f"--color must be #rrggbb, not {a.color!r}")
                text = text.replace('fill="currentColor"', f'fill="{a.color}"', 1)
            body = text.encode()
    except ValueError as err:
        sys.exit(f"nanotea leaf: {err}")
    if a.out:
        with open(a.out, "wb") as f:
            f.write(body)
    else:
        sys.stdout.buffer.write(body)


def _row(svgs: list[str], size: float) -> str:
    """Leaves side by side, a quarter of one apart, in one SVG; one leaf stays as it is."""
    if len(svgs) == 1:
        return svgs[0]
    gap = size / 4
    width = len(svgs) * size + (len(svgs) - 1) * gap
    inner = "".join(re.sub(r'^<svg xmlns="http://www.w3.org/2000/svg" ', f'<svg x="{_fmt(i * (size + gap))}" ', one)
                    for i, one in enumerate(svgs))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width)}" height="{_fmt(size)}" '
            f'viewBox="0 0 {_fmt(width)} {_fmt(size)}" fill="currentColor" aria-hidden="true">{inner}</svg>')
