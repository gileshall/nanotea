"""Makes the pictures in the docs: the leaves, and screenshots of the app with a few agents at work.

    uv run --with playwright --with pillow python docs/assets/make.py

The screenshots come from a real service on a temporary data directory, seeded through the agents' and the
owner's APIs, and taken in headless WebKit. Needs Playwright's WebKit (`playwright install webkit`)."""

import io
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from nanotea import leaf  # noqa: E402
from nanotea.themes import BUILT_IN  # noqa: E402
from demo import Service  # noqa: E402

PORT = 17499
AGENTS = ["builder", "tester", "docs", "ios", "release", "search"]
APP = """channels = { general = "Announcements" }
groups = [{ name = "App", members = ["builder", "tester", "ios"] }]"""


# Gogs drops width and height from <img>, so each picture is drawn at the size it is shown: the leaves at 1x,
# and screenshots as wide as a README page or wider, to be scaled down to it.

def leaves():
    green = BUILT_IN["sencha"]["light"]["leaf"]
    (OUT / "nanotea.png").write_bytes(leaf.png("nanotea", 88, green, scale=1))
    (OUT / "agents.png").write_bytes(leaf.png_row(AGENTS, 64, green, scale=1))


def seed(s: Service) -> dict:
    for n, name in enumerate(("builder", "tester", "ios")):
        s.open_line(name, name, n + 1)
    red = s.send("tester", "`test_threads` failed twice on macOS in the last hour, both on the reply order. "
                 "Looking now.", "Flaky thread order")
    s.send("tester", "Found it: replies sent in the same second sort by id, not by time. A one second wait in "
           "the test fixes the flake; the order itself is right.", "Flake explained", re=red)
    s.owner("POST", "/api/dm-tester/messages", {"text": "Good catch. Leave a comment on why the wait is there.",
                                                "re": red})
    time.sleep(1.1)
    s.send("tester", "Done, and the suite is green on both runners.", "Green again", re=red)
    s.send("ios", "The share sheet now attaches recordings at full quality. Ready for you to try on a phone.",
           "Share sheet attachments")
    s.send("release", "**0.9** is tagged. Notes are in `CHANGELOG.md`; nothing needs you.", "Tagged 0.9",
           channel="general")
    s.send("builder", "Every write under `data/` now goes through a temp file and `os.replace`. A crash mid-write "
           "leaves the old file whole.\n\n- `store.py`: messages and replies\n- `inbox.py`: lines and channels\n"
           "- 14 new tests, all passing", "Atomic writes everywhere")
    s.owner("POST", "/api/dm-builder/messages", {"text": "Nice. What about the index?"})
    time.sleep(1.1)
    ask = s.send("builder", "The index can rebuild at start, as now, or lazily on the first search. At start costs "
                 "about a second per ten thousand messages; lazily makes the first search slow instead. Which?",
                 "Index: rebuild when?", ask=True,
                 control={"type": "choice", "options": ["At start", "On first search"]})
    for name, text in (("builder", "waiting on the index question"), ("tester", "watching CI"),
                       ("ios", "testing on an iPhone 16"), ("docs", "rewriting the README")):
        s.agent(name).post("/api/status", {"name": name, "text": text})
    s.agent("ios").post("/api/agents/ios/held", {"what": "Bash: xcodebuild test -scheme Nanotea"})
    return {"thread": f"m-{red}", "ask": ask}


def leaves_drawn(page):
    """WebKit paints CSS-masked SVGs a moment after the page settles: wait until the first leaf has its pixels,
    not one flat color."""
    box = page.locator(".msg .avatar.leaf i").first.bounding_box()
    for _ in range(100):
        colors = Image.open(io.BytesIO(page.screenshot(clip=box))).getcolors(4096)
        if colors is None or len(colors) > 2:  # None: more than 4096
            return
        page.wait_for_timeout(100)
    raise RuntimeError(f"the leaves were never drawn on {page.url}")


def shoot(s: Service, ids: dict):
    phone = {"viewport": {"width": 393, "height": 1020}, "device_scale_factor": 2, "is_mobile": True,
             "has_touch": True}
    # name, page, device, appearance, theme
    shots = [
        ("chat", "/chat/builder", phone, "light", "sencha"),
        ("chat-dark", "/chat/builder", phone, "dark", "earl grey"),
        ("thread", f"/t/{ids['thread']}", phone, "light", "oolong"),
        ("desktop", "/chat/builder", {"viewport": {"width": 1280, "height": 800}, "device_scale_factor": 2}, "light",
         "sencha"),
    ]
    got = {}
    with sync_playwright() as p:
        browser = p.webkit.launch()
        try:
            for name, path, device, scheme, theme in shots:
                s.owner("POST", "/api/theme", {"theme": theme, "appearance": "auto"})
                ctx = browser.new_context(**device, color_scheme=scheme, extra_http_headers=s.headers())
                page = ctx.new_page()
                page.goto(f"http://127.0.0.1:{s.port}{path}", wait_until="networkidle")
                leaves_drawn(page)
                got[name] = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
                ctx.close()
        finally:
            browser.close()
    got["desktop"].save(OUT / "desktop.png", optimize=True)
    phones([(got["chat"], "Sencha, light", "builder's line, with a question to tap"),
            (got["chat-dark"], "Earl grey, dark", "the same line"),
            (got["thread"], "Oolong, light", "a thread, with three replies")]).save(OUT / "phones.png", optimize=True)


PAPER, INK, MUTED = "#f6f7f3", "#1d1c1d", "#686769"  # docs/figures/kit.py's ground and text
AVENIR = "/System/Library/Fonts/Avenir Next.ttc"


def face(style: str, size: int) -> ImageFont.FreeTypeFont:
    """A face of Avenir Next by its style name; a machine without it stops rather than drawing in another."""
    for i in range(12):
        f = ImageFont.truetype(AVENIR, size, index=i)
        if f.getname()[1] == style:
            return f
    raise RuntimeError(f"{AVENIR} has no {style!r}")


def phones(shots):
    """Each screenshot in its own phone, apart on the docs' ground, with a caption under it."""
    bezel, radius, gap, margin, caption = 22, 96, 120, 80, 190
    title, sub = face("Demi Bold", 46), face("Regular", 38)
    w, h = max(i.width for i, _, _ in shots) + 2 * bezel, max(i.height for i, _, _ in shots) + 2 * bezel
    out = Image.new("RGB", (2 * margin + len(shots) * w + (len(shots) - 1) * gap, 2 * margin + h + caption), PAPER)
    shadow = Image.new("L", out.size, 0)
    for n in range(len(shots)):
        x = margin + n * (w + gap)
        ImageDraw.Draw(shadow).rounded_rectangle((x + 6, margin + 18, x + w + 6, margin + h + 18), radius, fill=70)
    out.paste(Image.new("RGB", out.size, INK), mask=shadow.filter(ImageFilter.GaussianBlur(28)))
    draw = ImageDraw.Draw(out)
    for n, (shot, head, line) in enumerate(shots):
        x = margin + n * (w + gap)
        draw.rounded_rectangle((x, margin, x + w - 1, margin + h - 1), radius, fill=INK)
        screen = Image.new("L", shot.size, 0)
        ImageDraw.Draw(screen).rounded_rectangle((0, 0, shot.width - 1, shot.height - 1), radius - bezel, fill=255)
        out.paste(shot, (x + bezel, margin + bezel), screen)
        cx = x + w // 2
        draw.text((cx, margin + h + 70), head, font=title, fill=INK, anchor="mm")
        draw.text((cx, margin + h + 130), line, font=sub, fill=MUTED, anchor="mm")
    return out


def main():
    leaves()
    with tempfile.TemporaryDirectory() as tmp:
        s = Service(Path(tmp), PORT, APP)
        try:
            shoot(s, seed(s))
        finally:
            s.stop()


if __name__ == "__main__":
    main()
