"""Reading aloud: what the voice says for a message under each reading setting."""

import unittest

from nanotea import markdown, speech
from nanotea.settings import DEFAULTS

BASE = speech.reading(DEFAULTS)


def say(text, **read):
    return speech.for_voice(text, {**BASE, **read})


class Reading(unittest.TestCase):
    def test_defaults_read_as_before(self):
        text = "# Done\n\nSee `x` at https://example.com/a.\n\n```\ncode\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        self.assertEqual(say(text), markdown.plain(text, speech=True))
        self.assertEqual(speech.stem(BASE, DEFAULTS), "audio")

    def test_each_reading_has_its_own_file(self):
        site, skip = {**BASE, "read_links": "site"}, {**BASE, "read_links": "skip"}
        self.assertRegex(speech.stem(site, DEFAULTS), r"^audio-[0-9a-f]{10}$")
        self.assertNotEqual(speech.stem(site, DEFAULTS), speech.stem(skip, DEFAULTS))
        self.assertEqual(speech.stem(site, DEFAULTS), speech.stem(dict(site), DEFAULTS))

    def test_links(self):
        text = "Docs at https://www.example.com/a/b?c=1. Then go."
        self.assertEqual(say(text, read_links="site"), "Docs at a link to example.com. Then go.")
        self.assertEqual(say(text, read_links="skip"), "Docs at. Then go.")
        self.assertEqual(say("A [link](https://example.com) here.", read_links="skip"), "A link here.")

    def test_paths(self):
        self.assertEqual(say("Fixed in nanotea/server.py:1051 and ./docs/a.md.", read_paths="file"),
                         "Fixed in server.py, line 1051 and a.md.")
        self.assertEqual(say("A ratio of 3/4 stays.", read_paths="file"), "A ratio of 3/4 stays.")

    def test_hashes(self):
        self.assertEqual(say("Commit 8ee8609 is in.", read_hashes="skip"), "Commit is in.")
        self.assertEqual(say("The word facade stays.", read_hashes="skip"), "The word facade stays.")

    def test_symbols(self):
        self.assertEqual(say("A -> B, x >= 3, e.g. this, 4x5.", read_symbols=True),
                         "A to B, x at least 3, for example this, 4 by 5.")

    def test_tables_and_code(self):
        table = "| name | n |\n|---|---|\n| a | 1 |\n| b | 2 |\n"
        self.assertEqual(say(table, read_tables="labelled"), "name: a, n: 1.\nname: b, n: 2.")
        self.assertIn("table here, 2 rows by 2 columns", say(table, read_tables="name"))
        code = "Run:\n\n```\nmake\n```\n"
        self.assertNotIn("make", say(code, read_code="skip"))
        self.assertIn("make", say(code, read_code="read"))

    def test_longest_reading_keeps_clips(self):
        text = "One two three. Four five six. [[audio 1]] Seven eight."
        out = say(text, read_max_words=4)
        self.assertEqual(out, "One two three.\n\nThe rest is in the app.\n\n[[audio 1]]")
        self.assertEqual(say(text, read_max_words=100), text)
