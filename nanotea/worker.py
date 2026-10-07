"""Background work. Worker: rewrite, synthesize, splice clips, notify. Transcriber: the owner's recordings.
Escalator: iMessage about messages that went too long without a response."""

import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime

from nanotea import audio, speech
from nanotea.hush import HushBook
from nanotea.notify import Note
from nanotea.settings import DEFAULTS
from nanotea.store import AUDIO_MARKER, SHOWN, Message, Store, media_kind, now_iso

log = logging.getLogger(__name__)

CLIP_GAP_S = 0.5
ESCALATION_CHECK_S = 60


class _Queue:
    """One daemon thread handling items in order."""

    def __init__(self, name: str):
        self.queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, name=name, daemon=True).start()

    def submit(self, item) -> None:
        self.queue.put(item)

    def _loop(self) -> None:
        while True:
            item = self.queue.get()
            try:
                self._process(item)
            except Exception:
                log.exception("%s error on %s", threading.current_thread().name, self._label(item))

    def _label(self, item) -> str:
        return str(item)

    def _process(self, item) -> None:
        raise NotImplementedError


class Worker(_Queue):
    def __init__(self, store: Store, rewriter, tts, notifiers: list, note_for: Callable[[Message], Note],
                 who: Callable[[str], str], mix: audio.Mix, hush: HushBook, settings: Callable[[], dict]):
        """who(sender): "Name (sender)", the speaker the rewrite writes as. mix: how speech and clips are joined.
        hush: whether the owner wants a notification for this message now. settings: the owner's settings now,
        for reading aloud."""
        self.store = store
        self.settings = settings
        self.hush = hush
        self.mix = mix
        self.who = who
        self._speak_lock = threading.Lock()
        self.rewriter = rewriter
        self.tts = tts
        self.notifiers = notifiers
        self.note_for = note_for
        super().__init__("worker")

    def _set(self, msg: Message, status: str) -> None:
        msg.status = status
        self.store.save(msg)

    def _process(self, msg_id: str) -> None:
        msg = self.store.load(msg_id)
        try:
            self._set(msg, "rewriting")
            text = self.store.read_text(msg.id, "original.md")
            # Clips the sender didn't place play at the end; pictures, video and PDFs show there.
            clips = [n for n, a in enumerate(msg.attachments, 1) if media_kind(a) == "audio"]
            for n in clips:
                if f"[[audio {n}]]" not in text:
                    text += f"\n\n[[audio {n}]]"
            spoken = self.rewriter.rewrite(text, self.who(msg.sender), msg.title_hint, msg.ask)
            placed = sorted(int(n) for n in AUDIO_MARKER.findall(spoken.script))
            if placed != clips:
                raise RuntimeError(f"rewrite placed audio markers {placed}, expected {clips}")
            shown, kept = sorted(SHOWN.findall(text)), sorted(SHOWN.findall(spoken.script))
            if kept != shown:
                raise RuntimeError(f"rewrite placed media markers {kept}, expected {shown}")
            self.store.path(msg.id, "script.txt").write_text(spoken.script)
            msg.title = spoken.title
            msg.status = "done"  # voiced only when the owner asks (speak)
            log.info("made %s: %s", msg.id, msg.title)
        except Exception as e:
            log.exception("message %s failed while %s", msg.id, msg.status)
            msg.error = f"{msg.status}: {type(e).__name__}: {e}"
            msg.status = "failed"
        # The owner hears about failures too; either way the escalation clock starts now.
        msg.delivered = now_iso()
        self.store.save(msg)
        verdict = self.hush.verdict(msg)
        if verdict != "send":
            log.info("no notification for %s: %s", msg.id, verdict)
            if verdict == "held":
                self.hush.hold(msg.id)
            return
        note = self.note_for(msg)
        for n in self.notifiers:
            try:
                n.send(note)
            except Exception as e:
                log.exception("notifying %s via %s failed", msg.id, type(n).__name__)
                msg.notify_error = f"{type(n).__name__}: {type(e).__name__}: {e}"
                self.store.save(msg)

    def speak(self, msg_id: str) -> str:
        """Voice a finished message on request, read as the owner's reading settings say. Each reading is voiced
        once and kept; a message is voiced again only under a reading it hasn't been. Returns its audio file
        name."""
        with self._speak_lock:
            msg = self.store.load(msg_id)
            read = speech.reading(self.settings())
            stem = speech.stem(read, DEFAULTS)
            if msg.audio and msg.audio.rsplit(".", 1)[0] == stem and self.store.path(msg.id, msg.audio).exists():
                return msg.audio
            if msg.status != "done":
                raise ValueError(f"message {msg_id} is {msg.status}, not ready to voice")
            kept = [n for n in (f"{stem}.{self.tts.ext}", f"{stem}.{self.mix.ext}")
                    if self.store.path(msg.id, n).exists()]
            script = speech.for_voice(self.store.read_text(msg.id, "script.txt"), read)
            msg.audio = kept[0] if kept else self._speak(msg, script, stem)
            self.store.save(msg)
            log.info("voiced %s on request%s", msg.id, " (kept from before)" if kept else "")
            return msg.audio

    def _speak(self, msg: Message, script: str, stem: str) -> str:
        """Synthesize the script, splicing in clips at their markers, as stem.<ext>. Returns the file's name."""
        if not AUDIO_MARKER.search(script):
            name = f"{stem}.{self.tts.ext}"
            self.tts.synthesize(script, self.store.path(msg.id, name), msg.voice)
            return name
        work = self.store.path(msg.id, "parts")
        work.mkdir(exist_ok=True)
        parts: list = []
        pieces = AUDIO_MARKER.split(script)  # text, clip number, text, clip number, ..., text
        for i, piece in enumerate(pieces):
            if i % 2 == 1:
                clip = self.store.path(msg.id, "attachments") / msg.attachments[int(piece) - 1]
                parts += [CLIP_GAP_S, clip, CLIP_GAP_S]
            elif piece.strip():
                said = work / f"speech{i // 2}.{self.tts.ext}"
                self.tts.synthesize(piece.strip(), said, msg.voice)
                parts.append(said)
        # The synthesized pieces stay in parts/, the clips' originals in attachments/.
        return self.mix.join(parts, self.store.path(msg.id, stem)).name


class Transcriber(_Queue):
    """Jobs are (label, audio path, save), where save(**fields) records the outcome."""

    def __init__(self, stt):
        self.stt = stt
        super().__init__("transcriber")

    def _label(self, job) -> str:
        return job[0]

    def _process(self, job) -> None:
        label, audio, save = job
        try:
            transcript = self.stt.transcribe(audio)
        except Exception as e:
            log.exception("transcription of %s failed", label)
            save(transcript_status="failed", transcript_error=f"{type(e).__name__}: {e}")
            return
        save(transcript=transcript, transcript_status="done")
        log.info("transcribed %s", label)


class Escalator:
    """Once per message: iMessage the owner when a message goes after_min without a response. A question
    needs an answer; anything else needs opening. Channel posts are
    broadcasts: only their questions escalate."""

    def __init__(self, store: Store, notifiers: list, after_min: int,
                 note_for: Callable[[Message, int], Note], hush: HushBook):
        self.store = store
        self.hush = hush
        self.notifiers = notifiers
        self.after_min = after_min
        self.note_for = note_for
        threading.Thread(target=self._loop, name="escalator", daemon=True).start()

    def _loop(self) -> None:
        while True:
            time.sleep(ESCALATION_CHECK_S)
            try:
                self._check()
            except Exception:
                log.exception("escalation check failed")

    def _responded(self, msg: Message) -> bool:
        if msg.ask:
            return self.store.reply(msg.id) is not None
        return self.store.seen(msg.id) is not None

    def _check(self) -> None:
        if self.hush.quiet_now():
            return  # due messages escalate on the first check after quiet hours
        now = datetime.now().astimezone()
        # The index narrows it to those that might be due; the files decide.
        for msg in self.store.query("delivered IS NOT NULL AND escalated IS NULL"
                                    " AND (channel IS NULL OR ask = 1)"
                                    " AND ((ask = 1 AND reply IS NULL) OR (ask = 0 AND seen IS NULL))"):
            if msg.delivered is None or self.store.escalated(msg.id) is not None or self._responded(msg):
                continue
            if msg.channel is not None and not msg.ask:
                continue
            waited = int((now - datetime.fromisoformat(msg.delivered)).total_seconds() // 60)
            if waited < self.after_min:
                continue
            if (why := self.hush.verdict(msg)) in ("muted", "skipped"):
                self.store.mark_escalated(msg.id, f"not sent: {why}")
                log.info("not escalating %s: %s", msg.id, why)
                continue
            note = self.note_for(msg, waited)
            outcomes = []
            for n in self.notifiers:
                try:
                    n.send(note)
                    outcomes.append(f"{type(n).__name__} sent")
                except Exception as e:
                    log.exception("escalating %s via %s failed", msg.id, type(n).__name__)
                    outcomes.append(f"{type(n).__name__} failed: {type(e).__name__}: {e}")
            self.store.mark_escalated(msg.id, "; ".join(outcomes))
            log.info("escalated %s after %d min: %s", msg.id, waited, "; ".join(outcomes))
