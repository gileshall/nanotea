"""The command rewriter against stand-in LLM command lines."""

import sys
import unittest

import tempfile
from pathlib import Path

from nanotea import plugins
from nanotea.rewrite import CommandRewriter

# Echoes what it was given: the request from stdin, and whether the system and schema files were readable.
ECHO = r"""
import json, pathlib, sys
system, schema = pathlib.Path(sys.argv[1]).read_text(), json.loads(pathlib.Path(sys.argv[2]).read_text())
print(json.dumps({"title": "T " + schema["required"][0], "script": sys.stdin.read() + "|" + sys.argv[3][:9]
                  + "|" + str("Reply with one JSON object" in system)}))
"""


def rewriter(code: str, *args: str) -> CommandRewriter:
    return plugins.build("rewrite", "command", {"argv": [sys.executable, "-c", code, *args], "timeout_s": 30},
                         {"env": {}, "owner": "Robin", "app_name": "Teapot", "data": Path(tempfile.gettempdir())})


class CommandRewriterTest(unittest.TestCase):
    def test_placeholders_and_request(self):
        out = rewriter(ECHO, "{system_file}", "{schema_file}", "{system}").rewrite("Build done.", "Builder (b)", None,
                                                                                  True)
        self.assertEqual(out.title, "T title")
        request, system_head, contract = out.script.split("|")
        self.assertIn("Speaker: Builder (b)", request)
        self.assertIn("Wants a reply: yes", request)
        self.assertIn("<message>\nBuild done.\n</message>", request)
        self.assertEqual(system_head, "You rewri")
        self.assertEqual(contract, "True")

    def test_failures_are_errors(self):
        cases = {
            "import sys; sys.exit('boom')": "exited 1: boom",
            "print('```json {}```')": "non-JSON",
            "print('{\"title\": \"x\"}')": "no title and script",
            "print('{\"title\": \" \", \"script\": \"s\"}')": "empty title or script",
        }
        for code, message in cases.items():
            with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, message):
                rewriter(code).rewrite("hi", "a", None, False)


if __name__ == "__main__":
    unittest.main()
