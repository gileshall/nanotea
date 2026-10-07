"""Muting, only-questions and quiet hours: what decides whether the owner's phone is notified."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from harness import Case
from nanotea.hush import HushBook, HushError
from nanotea.notify import Note
from nanotea.store import Store
from nanotea.worker import Escalator


def around_now(before_h: float, after_h: float) -> dict:
    now = datetime.now()
    return {"start": (now - timedelta(hours=before_h)).strftime("%H:%M"),
            "end": (now + timedelta(hours=after_h)).strftime("%H:%M")}


class Hush(Case):
    PORT_OFFSET = 4

    def setUp(self):
        self.assertEqual(self.call("POST", "/api/hush", {"muted": [], "questions_only": False, "quiet": None})[0], 200)

    def noted(self, msg_id):
        return any(n["tag"] == msg_id for n in self.notes())

    def test_a_muted_agent_still_delivers_but_does_not_notify(self):
        status, out = self.call("POST", "/api/hush", {"muted": ["Quiet-Bot"]})
        self.assertEqual(status, 200, out)
        quiet = self.send("quiet-bot", "Hush.", title="From the muted")
        loud = self.send("loud-bot", "Hello.", title="From the other")
        self.wait_for(lambda: self.noted(loud), "the unmuted agent's note")
        self.assertFalse(self.noted(quiet))
        self.assertIn("From the muted", self.call("GET", "/unread")[1])
        self.call("POST", "/api/hush", {"muted": []})
        again = self.send("quiet-bot", "Back.", title="Unmuted again")
        self.wait_for(lambda: self.noted(again), "the note after unmuting")

    def test_a_muted_channel(self):
        self.call("POST", "/api/hush", {"muted": ["#general"]})
        post = self.send("poster", "Announcement.", channel="general", title="Muted channel post")
        direct = self.send("poster", "Direct.", title="Direct post")
        self.wait_for(lambda: self.noted(direct), "the direct note")
        self.assertFalse(self.noted(post))

    def test_only_questions_notifies_for_questions_alone(self):
        self.assertEqual(self.call("POST", "/api/hush", {"questions_only": True})[0], 200)
        plain = self.send("curious", "Just so you know.", title="Plain")
        ask = self.send("curious", "May I?", ask=True, title="A question")
        self.wait_for(lambda: self.noted(ask), "the question's note")
        self.assertFalse(self.noted(plain))

    def test_quiet_hours_hold_notifications_and_send_one_summary(self):
        self.assertEqual(self.call("POST", "/api/hush", {"quiet": around_now(1, 1)})[0], 200)
        held = Path(self.tmp.name) / "data" / "hush-held.json"
        plain = self.send("sleeper", "Overnight note.", title="Overnight")
        ask = self.send("sleeper", "Wake me?", ask=True, title="Overnight question")
        self.wait_for(lambda: held.exists() and {plain, ask} <= set(json.loads(held.read_text())), "both held")
        self.assertFalse(self.noted(plain) or self.noted(ask))
        self.assertEqual(self.call("POST", "/api/hush", {"quiet": None})[0], 200)
        note = self.wait_for(lambda: next((n for n in self.notes() if n["tag"] == "quiet-summary"), None), "the summary")
        self.assertIn("1 question waiting", note["title"])
        self.assertIn("1 unread", note["title"])
        self.assertEqual(note["path"], "/waiting")
        self.assertEqual(json.loads(held.read_text()), [])

    def test_notifications_page_and_validation(self):
        self.send("pageagent", "Hi.", title="Hi")
        page = self.call("GET", "/notifications")[1]
        self.assertIn("Only questions", page)
        self.assertIn("Quiet hours", page)
        self.assertIn("Mute an agent", page)
        self.assertIn('data-mute="#general"', page)
        self.call("POST", "/api/hush", {"muted": ["gone-agent"]})
        self.assertIn("Muted, not seen lately", self.call("GET", "/notifications")[1])
        for bad in ({}, {"nope": 1}, {"muted": "x"}, {"questions_only": "yes"}, {"quiet": {"start": "25:00", "end": "07:00"}},
                    {"quiet": {"start": "07:00", "end": "07:00"}}, {"quiet": {"start": "07:00"}}):
            self.assertEqual(self.call("POST", "/api/hush", bad)[0], 400, bad)
        self.assertEqual(self.call("POST", "/api/hush", {"muted": []}, paired=False)[0], 401)
        self.assertEqual(self.local("POST", "/api/hush", {"muted": []})[0], 403)
        self.assertEqual(self.call("GET", "/notifications", paired=False)[0], 403)


class Clock(unittest.TestCase):
    def book(self, quiet):
        book = HushBook(Path(self.enterContext(tempfile.TemporaryDirectory())))
        book.change({"quiet": quiet})
        return book

    def test_a_window_across_midnight(self):
        book = self.book({"start": "22:00", "end": "07:00"})
        day = datetime(2026, 10, 5)
        for hm, expect in (("23:30", True), ("03:00", True), ("22:00", True), ("07:00", False), ("12:00", False)):
            h, m = map(int, hm.split(":"))
            self.assertEqual(book.quiet_now(day.replace(hour=h, minute=m)), expect, hm)

    def test_a_window_within_a_day(self):
        book = self.book({"start": "09:00", "end": "17:00"})
        day = datetime(2026, 10, 5)
        self.assertTrue(book.quiet_now(day.replace(hour=12)))
        self.assertFalse(book.quiet_now(day.replace(hour=8)))
        self.assertFalse(book.quiet_now(day.replace(hour=17)))

    def test_settings_survive_a_restart_and_a_bad_file_stops_it(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        HushBook(root).change({"muted": ["B", "a"], "questions_only": True})
        again = HushBook(root).get()
        self.assertEqual(again["muted"], ["a", "B"])
        self.assertTrue(again["questions_only"])
        (root / "hush.json").write_text('{"quiet": {"start": "nope", "end": "07:00"}}')
        with self.assertRaises(HushError):
            HushBook(root)


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, note):
        self.sent.append(note)


class Escalation(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root / "data")
        self.hush = HushBook(self.root)
        self.out = Recorder()
        self.esc = Escalator(self.store, [self.out], 30, lambda m, w: Note(
            title="late", text="", path="/", link="", tag=m.id, unseen=0), self.hush)

    def overdue(self, sender):
        msg = self.store.create("Please look.", sender, None, False, "v1", [])
        msg.delivered = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
        self.store.save(msg)
        return msg

    def test_overdue_messages_escalate(self):
        msg = self.overdue("slow")
        self.esc._check()
        self.assertEqual([n.tag for n in self.out.sent], [msg.id])

    def test_a_muted_line_is_recorded_not_sent(self):
        self.hush.change({"muted": ["muted-one"]})
        msg = self.overdue("muted-one")
        self.esc._check()
        self.assertEqual(self.out.sent, [])
        self.assertIn("not sent: muted", self.store.escalated(msg.id))

    def test_quiet_hours_defer_until_they_end(self):
        msg = self.overdue("sleepy")
        self.hush.change({"quiet": around_now(1, 1)})
        self.esc._check()
        self.assertEqual(self.out.sent, [])
        self.assertIsNone(self.store.escalated(msg.id))
        self.hush.change({"quiet": None})
        self.esc._check()
        self.assertEqual([n.tag for n in self.out.sent], [msg.id])


if __name__ == "__main__":
    unittest.main()
