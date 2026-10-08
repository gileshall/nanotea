"""One deployment of nanotea, driven headless: the owner in Chromium, agents over MCP and nanotea-tell.

python -m e2e.journey WORK/scenario.json

The scenario (written by e2e/run.py) says where the service is and how it was set up. The checks run in order and
stop at the first failure, since each builds on the last. WORK/results.json has each check, its time and its
numbers; WORK/artifacts/ has what was made on the way: the voice's audio, the recorded reply, screenshots, logs.

Numbers about audio (word recall through whisper, loudness, length) say the pipeline carried the words. They
are not a judgment of how anything sounds."""

import asyncio
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from playwright.async_api import TimeoutError, async_playwright

from e2e.pushmock import PushService
from nanotea import configedit

BUILDER, REPORTER, SOURCE = "e2e-builder", "e2e-reporter", "e2e-ci"
SAID = "Please ship the release on Thursday after the documentation lands."
SENT = "The **nightly build** finished: 214 tests passed, and the release notes are ready for review."
WORD_RECALL_MIN = 0.6  # the words of a clean sentence that must survive speech and transcription


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))


def recall(reference: str, heard: str) -> float:
    """The share of the reference's words found in what was heard, each counted as often as it occurs."""
    left = words(heard)
    hits = 0
    for w in words(reference):
        if w in left:
            left.remove(w)
            hits += 1
    return hits / max(1, len(words(reference)))


class Failed(AssertionError):
    pass


def recorded(err: BaseException) -> bool:
    """Whether a check has already put err in the results, perhaps inside the task groups of MCP's client."""
    return getattr(err, "recorded", False) or (
        isinstance(err, BaseExceptionGroup) and any(recorded(e) for e in err.exceptions))


def check(cond, message: str) -> None:
    if not cond:
        raise Failed(message)


class Run:
    def __init__(self, scenario_path: Path):
        self.sc = json.loads(scenario_path.read_text())
        self.work = scenario_path.parent
        self.art = self.work / "artifacts"
        self.art.mkdir(exist_ok=True)
        self.results: list[dict] = []
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.env = {**os.environ, "NANOTEA_CONFIG": self.sc["config"]}
        self.env.pop("NANOTEA_TOKEN_FILE", None)
        if key := self.sc.get("key_file"):
            self.env["NANOTEA_KEY_FILE"] = key
        self.bin = Path(self.sc["bin"])
        self.agent_dir = self.work / "agent"
        self.agent_dir.mkdir(exist_ok=True)
        self.timeouts = {"voice": 600, "transcribe": 900, "rewrite": 300, **self.sc.get("timeouts", {})}

    # The record

    def save(self) -> None:
        tmp = self.work / "results.json.tmp"
        tmp.write_text(json.dumps({"scenario": self.sc["name"], "expect": self.sc.get("expect", {}),
                                   "checks": self.results}, indent=2))
        os.replace(tmp, self.work / "results.json")

    async def step(self, name: str, fn, *args):
        print(f"[{self.sc['name']}] {name} ...", flush=True)
        entry = {"check": name, "ok": False, "seconds": None, "metrics": {}, "error": None}
        self.results.append(entry)
        self.metrics = entry["metrics"]
        start = time.monotonic()
        try:
            out = await fn(*args)
            entry["ok"] = True
            return out
        except Exception as err:
            entry["error"] = f"{type(err).__name__}: {err}"
            entry["traceback"] = traceback.format_exc()
            err.recorded = True
            if getattr(self, "page", None) is not None:
                try:
                    await self.page.screenshot(path=str(self.art / f"failed-{name}.png"), full_page=True)
                except Exception as shot:
                    entry["screenshot_error"] = f"{type(shot).__name__}: {shot}"
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - start, 2)
            print(f"[{self.sc['name']}] {name}: {'ok' if entry['ok'] else 'FAILED ' + str(entry['error'])} "
                  f"({entry['seconds']} s) {json.dumps(entry['metrics'])}", flush=True)
            self.save()

    # Programs

    def run(self, argv, timeout=120, env=None, cwd=None, check_rc=True) -> subprocess.CompletedProcess:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env or self.env,
                           cwd=cwd or self.agent_dir)
        if check_rc and p.returncode != 0:
            raise Failed(f"{' '.join(map(str, argv))[:200]} exited {p.returncode}: {(p.stderr or p.stdout)[-1500:]}")
        return p

    def spawn(self, label: str, argv, env=None, cwd=None) -> subprocess.Popen:
        log = open(self.art / f"{label}.log", "w")
        p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=env or self.env,
                             cwd=cwd or self.agent_dir, start_new_session=True)
        self.procs.append((label, p))
        return p

    def stop_all(self) -> None:
        for label, p in reversed(self.procs):
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=5)

    def nanotea(self, *args, **kw):
        return self.run([str(self.bin / "nanotea"), *args], **kw)

    # Setting up a native install, as docs/running.md says

    async def install(self):
        nat = self.sc["native"]
        for argv in nat["install"]:
            self.run(argv, env={**self.env, **nat.get("install_env", {})}, timeout=1800)

    async def native_setup(self):
        nat = self.sc["native"]
        cfg = Path(self.sc["config"])
        check(not cfg.exists(), f"{cfg} exists before init")
        out = self.nanotea("init", "--owner", "Robin", "--url", nat["init_url"])
        (self.art / "init.txt").write_text(out.stdout + out.stderr)
        text, _, after = configedit.edit(cfg.read_text(), [(tuple(t), k, v, False) for t, k, v in nat["edits"]])
        cfg.write_text(text)
        shutil.copy(cfg, self.art / "config.toml")
        self.nanotea("config", "check", str(cfg), env={**self.env, **nat.get("env", {})}, timeout=900)
        self.metrics.update(tts=after["tts"]["command"]["argv"][0], stt=after["stt"]["command"]["argv"],
                            rewrite=after["rewrite"].get("command", {}).get("argv"))

    async def plugins_check(self):
        out = self.nanotea("plugins", "--check", env={**self.env, **self.sc["native"].get("env", {})}, timeout=900)
        (self.art / "plugins-check.txt").write_text(out.stdout + out.stderr)
        self.metrics["plugins"] = [line for line in out.stdout.splitlines() if line.startswith("ok")]

    async def serve(self):
        env = {**self.env, **self.sc["native"].get("env", {})}
        p = self.spawn("service", [str(self.bin / "nanotea"), "serve"], env=env, cwd=Path(self.sc["config"]).parent)
        url = f"http://127.0.0.1:{self.sc['agent_port']}/favicon.svg"
        deadline = time.monotonic() + 120
        while True:
            check(p.poll() is None, f"the service exited {p.returncode}; see artifacts/service.log")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            check(time.monotonic() < deadline, "the service did not answer within 120 s")
            await asyncio.sleep(0.5)

    async def caddy(self):
        c = self.sc["caddy"]
        home = self.work / "caddy"
        home.mkdir(exist_ok=True)
        (home / "Caddyfile").write_text(
            "{\n\tskip_install_trust\n\tadmin off\n\tauto_https disable_redirects\n"
            f"\thttps_port {c['site'].rsplit(':', 1)[1]}\n\thttp_port {c['http_port']}\n}}\n"
            f"{c['site']} {{\n\tbind {c['bind']}\n\ttls internal\n\treverse_proxy {c['upstream']}\n}}\n")
        env = {**os.environ, "XDG_DATA_HOME": str(home / "data"), "XDG_CONFIG_HOME": str(home / "config")}
        p = self.spawn("caddy", ["caddy", "run", "--config", str(home / "Caddyfile"), "--adapter", "caddyfile"],
                       env=env, cwd=home)
        root = home / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt"
        deadline = time.monotonic() + 60
        while not root.exists():
            check(p.poll() is None, f"caddy exited {p.returncode}; see artifacts/caddy.log")
            check(time.monotonic() < deadline, "caddy made no root certificate within 60 s")
            await asyncio.sleep(0.5)
        self.sc.setdefault("trust", []).append(str(root))

    # The owner's browser

    async def browser(self, pw):
        mic = self.art / "said.wav"
        engine = self.sc["fake_mic"]
        if engine == "espeak-ng":
            self.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(mic), SAID])
        elif engine == "say":
            self.run(["say", "-v", "Samantha", "-o", str(mic), "--file-format=WAVE", "--data-format=LEI16@48000", SAID])
        else:
            raise Failed(f"no fake mic engine {engine!r}")
        self.mic_s = self.duration(mic)
        if self.sc.get("trust"):
            nss = Path.home() / ".pki" / "nssdb"
            nss.mkdir(parents=True, exist_ok=True)
            if not (nss / "cert9.db").exists():
                self.run(["certutil", "-d", f"sql:{nss}", "-N", "--empty-password"])
            for i, ca in enumerate(self.sc["trust"]):
                self.run(["certutil", "-d", f"sql:{nss}", "-A", "-t", "C,,", "-n", f"nanotea-e2e-{i}", "-i", ca])
        args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={mic}", "--autoplay-policy=no-user-gesture-required"]
        if rules := self.sc.get("resolve"):
            args.append("--host-resolver-rules=" + ", ".join(f"MAP {h} {ip}" for h, ip in rules.items()))
        # New headless: the old headless shell denies notifications to every page.
        self.chromium = await pw.chromium.launch(channel="chromium", args=args)
        self.context = await self.chromium.new_context()
        # No grants: granting any denies the rest, and the page should find notifications not yet asked for.
        # The fake-UI flag answers the microphone's prompt.
        # The run is the push service: the page's subscribe gets its endpoint, as a browser would get its vendor's.
        await self.context.add_init_script("""
            if (window.PushManager) {
              PushManager.prototype.getSubscription = async function () { return null; };
              PushManager.prototype.subscribe = async function (opts) {
                const key = new Uint8Array(opts.applicationServerKey);
                window.__e2eSubscribe = {userVisibleOnly: opts.userVisibleOnly,
                                         key: btoa(String.fromCharCode(...key))};
                const sub = window.__e2eEndpoint;
                if (!sub) throw new Error("no e2e endpoint set");
                return {endpoint: sub.endpoint, toJSON: () => sub};
              };
            }""")
        self.console: list[str] = []
        self.page = await self.context.new_page()
        self.page.on("console", lambda m: self.console.append(f"{m.type}: {m.text}"))
        self.page.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))
        self.page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

    async def goto(self, path: str):
        r = await self.page.goto(self.sc["public_url"] + path)
        return r

    async def shot(self, name: str):
        await self.page.screenshot(path=str(self.art / f"{name}.png"), full_page=True)

    async def until(self, path: str, selector: str, timeout_s: float, what: str):
        """Load path until selector is on it: pages refresh themselves only every few seconds."""
        deadline = time.monotonic() + timeout_s
        while True:
            await self.goto(path)
            if await self.page.locator(selector).count():
                return self.page.locator(selector).first
            check(time.monotonic() < deadline, f"no {what} at {path} within {timeout_s} s")
            await asyncio.sleep(2)

    async def not_paired(self):
        r = await self.goto("/")
        check(r.status == 403, f"an unpaired page answered {r.status}, not 403")
        check("isn't paired yet" in await self.page.content(), "the unpaired page doesn't say so")

    async def pair(self):
        if path := self.sc.get("pair_link_file"):
            link = Path(path).read_text().strip()
        else:
            link = self.nanotea("pair-link").stdout.strip()
        check(link.startswith(self.sc["public_url"] + "/?k="), "pair-link doesn't start with public_url/?k=")
        r = await self.page.goto(link)
        check(r.status == 200, f"the pairing link answered {r.status}")
        check(await self.page.locator("body[data-owner]").count(), "the paired page is not the app")
        (cookie,) = [c for c in await self.context.cookies() if c["name"] == "nanotea_key"]
        https = self.sc["public_url"].startswith("https://")
        check(cookie["httpOnly"] and cookie["secure"] == https, f"the cookie is {cookie}")
        self.metrics["secure_cookie"] = cookie["secure"]
        await self.page.goto(self.sc["public_url"] + "/")  # the key out of the address bar
        await self.shot("paired")

    # Agents

    def token_file(self, name: str, events: bool = False) -> Path:
        return Path(self.sc["config"]).parent / "tokens" / f"{name}{'.events' if events else ''}.token"

    async def tool(self, tool: str, /, **args) -> dict:
        r = await self.builder.call_tool(tool, args)
        check(not r.is_error, f"{tool} failed: {r.content[0].text if r.content else r}")
        return r.structured_content

    async def wait_item(self, test, timeout_s: float, what: str) -> dict:
        """The first item from wait that test accepts. Every item seen is kept in artifacts/items.jsonl."""
        deadline = time.monotonic() + timeout_s
        while True:
            for item in self.pending:
                if test(item):
                    self.pending.remove(item)
                    return item
            left = deadline - time.monotonic()
            check(left > 0, f"no {what} within {timeout_s} s; had {[i['kind'] for i in self.pending]}")
            out = await self.tool("wait", timeout_s=max(1, min(60, int(left))))
            with open(self.art / "items.jsonl", "a") as f:
                for item in out["items"]:
                    f.write(json.dumps(item) + "\n")
            self.pending.extend(out["items"])

    async def agent_asks(self):
        check(not self.token_file(BUILDER).exists(), "the builder has a token file before it asked")
        r = await self.builder.call_tool("check", {})
        check(r.is_error and "waiting for approval" in r.content[0].text,
              f"a tokenless agent's check didn't say it waits: {r.content[0].text if r.content else r}")

    async def owner_approves(self):
        row = await self.until("/tokens", f'div.rule:has-text("{BUILDER}") button[data-act="approve"]', 60,
                               "the builder's request")
        await self.shot("tokens-asking")
        waiter = asyncio.create_task(self.builder.call_tool("wait", {"timeout_s": 60}))
        async with self.page.expect_navigation():  # the page reloads once it is done
            await row.click()
        r = await waiter
        check(not r.is_error and r.structured_content.get("approved"), f"wait after approval: {r.content}")
        mode = self.token_file(BUILDER).stat().st_mode & 0o777
        check(mode == 0o600, f"the token file is mode {oct(mode)}")

    async def join(self):
        voices = (await self.tool("voices"))["voices"]
        free = [v["id"] for v in voices if not v.get("taken_by")]
        check(len(free) >= 2, f"fewer than two free voices: {voices}")
        self.voices = free
        self.metrics["voices"] = len(voices)
        out = await self.tool("join", name=BUILDER, voice=free[0], about="an end-to-end run")
        check(out["line"] == BUILDER, f"joined line {out['line']}")

    async def make_approver(self):
        button = await self.until("/tokens", f'div.rule:has-text("{BUILDER}") button[data-act="appoint"]', 30,
                                  "Make approver")
        async with self.page.expect_navigation():  # the page reloads once it is done
            await button.click()
        await self.until("/tokens", 'button[data-act="unappoint"]', 30, "the approver")
        await self.shot("tokens-approver")

    async def approver_decides(self):
        tell = self.spawn("tell-reporter", [str(self.bin / "nanotea-tell"), "--from", REPORTER, "--voice",
                                            self.voices[1], "--title", "Reporter", "Hello from a script."])
        item = await self.wait_item(lambda i: i["kind"] == "request" and REPORTER in i["text"], 120,
                                    "the reporter's request")
        out = await self.tool("decide", id=item["id"], approve=True)
        check(out["state"] == "approved", f"decide answered {out}")
        rc = await asyncio.to_thread(tell.wait, 120)
        check(rc == 0, f"nanotea-tell exited {rc}; see artifacts/tell-reporter.log")

    async def send_and_rewrite(self):
        start = time.monotonic()
        sent = await self.tool("send", text=SENT)
        self.sent_id = sent["id"]
        art = await self.until(f"/chat/{BUILDER}", f"article#m-{self.sent_id}:not(:has(.tag.status))",
                               self.timeouts["rewrite"], "the delivered message")
        self.metrics["delivered_s"] = round(time.monotonic() - start, 2)
        if await art.locator("p.error").count():
            raise Failed(f"the message shows an error: {await art.inner_text()}")
        self.title = (await art.locator(".subject a").inner_text()).strip()
        self.script = (await art.locator(".text").inner_text()).strip()
        self.metrics.update(title=self.title, script_words=len(words(self.script)),
                            script_recall_of_sent=round(recall(SENT.replace("*", ""), self.script), 2))
        check(self.title and self.script, "an empty title or script")
        await self.shot("delivered")

    def duration(self, path: Path) -> float:
        out = self.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
        return float(out.stdout.strip())

    def loudness(self, path: Path) -> float:
        out = self.run(["ffmpeg", "-nostdin", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
        return float(re.search(r"mean_volume: (-?[\d.]+) dB", out.stderr)[1])

    def transcribe(self, path: Path) -> str:
        script = self.sc["transcribe_sh"]
        out = self.run(["sh", script, self.sc["stt_python"], self.sc["stt_model"], str(path)], timeout=900)
        return out.stdout.strip()

    async def voice(self):
        await self.goto(f"/chat/{BUILDER}")
        button = self.page.locator(f"article#m-{self.sent_id} button[data-speak]")
        await button.wait_for()
        start = time.monotonic()
        async with self.page.expect_response(lambda r: f"/audio/{self.sent_id}" in r.url,
                                             timeout=self.timeouts["voice"] * 1000) as got:
            await button.click()
        first = await got.value
        self.metrics["voice_s"] = round(time.monotonic() - start, 2)
        if first.status not in (200, 206):
            raise Failed(f"GET /audio answered {first.status}: {await first.text()}")
        # Fetched by the page, whose resolver and trust are the browser's.
        whole = await self.page.evaluate("""async (url) => {
            const r = await fetch(url);
            const bytes = new Uint8Array(await r.arrayBuffer());
            let text = "";
            for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
            return {status: r.status, type: r.headers.get("content-type") || "", body: btoa(text)};
        }""", f"/audio/{self.sent_id}")
        check(whole["status"] == 200, f"GET /audio answered {whole['status']}")
        kind = whole["type"]
        ext = {"audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mp4": "m4a",
               "audio/x-m4a": "m4a", "audio/flac": "flac", "audio/ogg": "ogg", "audio/aac": "aac"}.get(
            kind.split(";")[0].strip())
        check(ext, f"/audio came back as {kind!r}")
        path = self.art / f"voice.{ext}"
        path.write_bytes(base64.b64decode(whole["body"]))
        played = await self.page.evaluate(f"""() => new Promise((ok) => {{
            const a = document.querySelector('article#m-{self.sent_id} audio');
            if (!a) return ok({{error: "no audio element"}});
            const done = () => ok({{duration: a.duration, error: a.error && a.error.message}});
            if (a.readyState >= 1) return done();
            a.addEventListener("loadedmetadata", done); a.addEventListener("error", done);
            setTimeout(done, 15000);
        }})""")
        check(not played.get("error") and played.get("duration", 0) > 0, f"the page's player: {played}")
        heard = self.transcribe(path)
        self.metrics.update(content_type=kind, bytes=path.stat().st_size, seconds=round(self.duration(path), 2),
                            mean_db=self.loudness(path), page_duration=round(played["duration"], 2),
                            heard=heard, word_recall=round(recall(self.script, heard), 2))
        check(self.metrics["seconds"] > 1, "the voice is under a second long")
        check(self.metrics["word_recall"] >= WORD_RECALL_MIN,
              f"whisper heard {self.metrics['word_recall']:.0%} of the script's words in the voice")

    async def choice(self):
        out = await self.tool("ask", question="Ship tonight, or hold for the docs?",
                              control={"type": "choice", "options": ["Ship", "Hold"]})
        qid = out["id"]
        pick = await self.until(f"/m/{qid}", f'div.control[data-control="{qid}"] button[data-act="pick"]:has-text("Hold")',
                                self.timeouts["rewrite"], "the choice")
        async with self.page.expect_navigation():
            await pick.click()
        item = await self.wait_item(lambda i: i["kind"] == "answer" and (i.get("re") or {}).get("id") == qid, 60,
                                    "the tapped answer")
        check(item.get("tap") and item["tap"]["data"] == {"picked": "Hold"}, f"the answer: {item}")
        await self.shot("choice")

    async def voice_reply(self):
        out = await self.tool("ask", question="Say when we should ship.")
        qid = out["id"]
        record = await self.until(f"/m/{qid}", "section.recorder button.record:not([disabled])",
                                  self.timeouts["rewrite"], "the recorder")
        await record.click()
        await self.page.locator("section.recorder button.record.live").wait_for(timeout=15000)
        await asyncio.sleep(self.mic_s + 1.5)
        async with self.page.expect_response(lambda r: "/drafts" in r.url and r.request.method == "POST") as d:
            await record.click()
        draft = await d.value
        if not draft.ok:
            raise Failed(f"the draft upload answered {draft.status}: {await draft.text()}")
        self.metrics["recorded_as"] = draft.request.headers.get("content-type")
        start = time.monotonic()
        send_url = await self.page.locator("section.recorder").get_attribute("data-send")
        async with self.page.expect_response(lambda r: r.url.endswith(send_url)) as s:
            await self.page.locator("section.recorder button.send").click()
        sent = await s.value
        if not sent.ok:
            raise Failed(f"sending answered {sent.status}: {await sent.text()}")
        item = await self.wait_item(lambda i: i["kind"] == "answer" and (i.get("re") or {}).get("id") == qid,
                                    self.timeouts["transcribe"], "the recorded answer")
        self.metrics["transcribed_s"] = round(time.monotonic() - start, 2)
        voice = item.get("voice") or {}
        heard = voice.get("transcript") or ""
        self.metrics.update(heard=heard, word_recall=round(recall(SAID, heard), 2),
                            audio_path=voice.get("audio_path"))
        check(voice.get("audio_path"), f"the answer has no recording: {item}")
        if Path(voice["audio_path"]).exists():  # on this machine: a native install
            shutil.copy(voice["audio_path"], self.art / f"reply{Path(voice['audio_path']).suffix}")
        check(self.metrics["word_recall"] >= WORD_RECALL_MIN,
              f"the transcript has {self.metrics['word_recall']:.0%} of the words said")

    async def typed_reply(self):
        await self.goto(f"/chat/{BUILDER}")
        note = self.page.locator("section.recorder textarea.note")
        await note.fill("Typed from the e2e browser.")
        send_url = await self.page.locator("section.recorder").get_attribute("data-send")
        async with self.page.expect_response(lambda r: r.url.endswith(send_url)) as s:
            await self.page.locator("section.recorder button.send").click()
        sent = await s.value
        if not sent.ok:
            raise Failed(f"sending answered {sent.status}: {await sent.text()}")
        await self.wait_item(lambda i: i["kind"] == "message" and i["text"] == "Typed from the e2e browser.", 60,
                             "the typed message")

    async def push(self):
        p = self.sc["push"]
        self.pushes = PushService(p["listen"], p["port"], p["cert"], p["key"], p["endpoint_host"])
        sub = self.pushes.endpoint("owner")
        await self.goto("/")
        vapid = await self.page.evaluate("document.body.dataset.vapid")
        check(vapid, "the page has no VAPID key: push is off in the config")
        await self.page.evaluate("(sub) => { window.__e2eEndpoint = sub; }", sub)
        button = self.page.locator("#notify-on")
        try:
            await button.wait_for(timeout=10000)
        except TimeoutError as err:
            hint = await self.page.locator("#notify-hint").inner_text()
            permission = await self.page.evaluate("Notification.permission")
            raise Failed(f"no Turn on notifications; permission {permission!r}, the page says {hint!r}") from err
        await self.context.grant_permissions(["microphone", "notifications"], origin=self.sc["public_url"])
        await button.click()
        await self.page.locator("#notify-hint", has_text="Notifications are on.").wait_for(timeout=30000)
        asked = await self.page.evaluate("window.__e2eSubscribe")
        check(asked["userVisibleOnly"], "subscribe without userVisibleOnly")
        welcome = await asyncio.to_thread(self.pushes.wait, lambda r: True, 30, "welcome")
        check(welcome["vapid"]["k"] == vapid, "the push was signed by another key than the page's")
        sent = await self.tool("send", text="A message to push.", title="Pushed")
        got = await asyncio.to_thread(self.pushes.wait, lambda r: sent["id"] in json.dumps(r["payload"]),
                                      self.timeouts["rewrite"], "for the message")
        self.metrics.update(welcome=welcome["payload"], pushed=got["payload"], vapid_sub=got["vapid"]["sub"])
        (self.art / "pushes.json").write_text(json.dumps(self.pushes.received, indent=2))
        # The browser's half: the service worker shows what the service sent.
        cdp = await self.context.new_cdp_session(self.page)
        regs: list[dict] = []
        versions: list[dict] = []
        sw_errors: list[dict] = []
        cdp.on("ServiceWorker.workerRegistrationUpdated", lambda e: regs.extend(e["registrations"]))
        cdp.on("ServiceWorker.workerVersionUpdated", lambda e: versions.extend(
            {k: v.get(k) for k in ("versionId", "runningStatus", "status")} for v in e["versions"]))
        cdp.on("ServiceWorker.workerErrorReported", lambda e: sw_errors.append(e["errorMessage"]))
        await cdp.send("ServiceWorker.enable")
        await asyncio.sleep(1)
        origin = self.sc["public_url"]
        reg = next((r for r in regs if r["scopeURL"].startswith(origin) and not r["isDeleted"]), None)
        check(reg, f"no service worker registration for {origin}: {regs}")
        # What the worker did with the push: a showNotification that rejects inside waitUntil reports nowhere.
        worker = next((w for w in self.context.service_workers if w.url.startswith(origin)), None)
        check(worker, f"no service worker in the context for {origin}: {[w.url for w in self.context.service_workers]}")
        await worker.evaluate("""() => {
            self.__e2e = [`permission ${Notification.permission}`];
            self.addEventListener("push", () => self.__e2e.push("push"));
            const show = self.registration.showNotification.bind(self.registration);
            self.registration.showNotification = (...a) => show(...a).then(
                (v) => { self.__e2e.push("shown"); return v; },
                (e) => { self.__e2e.push(`showNotification: ${e}`); throw e; });
        }""")
        await cdp.send("ServiceWorker.deliverPushMessage", {"origin": origin, "registrationId": reg["registrationId"],
                                                            "data": json.dumps(got["payload"])})
        shown = []
        for _ in range(40):
            shown = await self.page.evaluate("""navigator.serviceWorker.ready.then((r) => r.getNotifications())
                .then((ns) => ns.map((n) => ({title: n.title, body: n.body, tag: n.tag})))""")
            if shown:
                break
            await asyncio.sleep(0.5)
        self.metrics["shown"] = shown
        if not any(n["title"] == got["payload"]["title"] for n in shown):
            seen = await worker.evaluate("self.__e2e")
            raise Failed(f"the worker showed {shown}; in the worker {seen}; worker versions {versions}, "
                         f"errors {sw_errors}")

    async def event_source(self):
        tell = self.spawn("tell-event", [str(self.bin / "nanotea-tell"), "--event", "build", "--from", SOURCE,
                                         "--inbox", BUILDER, "Nightly build passed."])
        item = await self.wait_item(lambda i: i["kind"] == "request" and SOURCE in i["text"], 120,
                                    "the event source's request")
        check(f"events to line {BUILDER}" in item["text"], f"the request: {item['text']}")
        await self.tool("decide", id=item["id"], approve=True)
        rc = await asyncio.to_thread(tell.wait, 120)
        check(rc == 0, f"nanotea-tell --event exited {rc}; see artifacts/tell-event.log")
        event = await self.wait_item(lambda i: i["kind"] == "event", 60, "the event")
        check(event["from"] == SOURCE and "Nightly build passed" in event["text"], f"the event: {event}")
        check(self.token_file(SOURCE, events=True).exists(), "no events token file")

    async def revoke(self):
        button = await self.until("/tokens", f'div.rule:has-text("{REPORTER}") button[data-act="revoke"]', 30,
                                  "the reporter's Revoke")
        async with self.page.expect_navigation():  # the page reloads once it is done
            await button.click()
        p = await asyncio.to_thread(self.run, [str(self.bin / "nanotea-tell"), "--from", REPORTER, "After revoke."],
                                    60, None, None, False)
        self.metrics["said"] = p.stderr.strip()[-300:]
        check(p.returncode != 0 and "revoked" in p.stderr, f"a revoked token: exit {p.returncode}, {p.stderr}")

    async def main(self):
        sc = self.sc
        try:
            if sc.get("native"):
                await self.step("install", self.install)
                await self.step("native-setup", self.native_setup)
                await self.step("plugins-check", self.plugins_check)
                await self.step("serve", self.serve)
            if sc.get("caddy"):
                await self.step("caddy", self.caddy)
            async with async_playwright() as pw:
                await self.step("browser", self.browser, pw)
                await self.step("not-paired", self.not_paired)
                await self.step("pair", self.pair)
                server = StdioServerParameters(command=str(self.bin / "nanotea"), args=["mcp", "--name", BUILDER],
                                               env=self.env, cwd=str(self.agent_dir))
                async with Client(server, mode="legacy") as self.builder:
                    self.pending: list[dict] = []
                    for name, fn in (("agent-asks", self.agent_asks), ("owner-approves", self.owner_approves),
                                     ("join", self.join), ("make-approver", self.make_approver),
                                     ("approver-decides", self.approver_decides),
                                     ("send-rewrite", self.send_and_rewrite), ("voice", self.voice),
                                     ("choice", self.choice), ("voice-reply", self.voice_reply),
                                     ("typed-reply", self.typed_reply), ("push", self.push),
                                     ("event-source", self.event_source), ("revoke", self.revoke)):
                        await self.step(name, fn)
                await self.chromium.close()
        except Exception as err:
            if not recorded(err):  # failed outside any check: starting MCP or the browser
                self.results.append({"check": "run", "ok": False, "error": f"{type(err).__name__}: {err}",
                                     "traceback": traceback.format_exc()})
                traceback.print_exc()
        finally:
            if getattr(self, "pushes", None):
                self.pushes.close()
            if getattr(self, "console", None):
                (self.art / "console.log").write_text("\n".join(self.console))
            self.stop_all()
            self.save()
        return all(c["ok"] for c in self.results)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m e2e.journey WORK/scenario.json")
    ok = asyncio.run(Run(Path(sys.argv[1])).main())
    sys.exit(0 if ok else 1)
