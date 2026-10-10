"""Speechify: a model no voice supports fails when voices are listed, naming the models there are."""

import io
import json
import unittest
from unittest import mock

from nanotea.tts import Speechify


class Ctx:
    def secret(self, key):
        return "k"

    def need(self, cfg, key, kind):
        return cfg[key]


def page(*models):
    return io.BytesIO(json.dumps({"voices": [{"id": f"v{i}", "models": [{"name": m}]} for i, m in enumerate(models)],
                                  "has_more": False}).encode())


class Model(unittest.TestCase):
    def test_a_model_no_voice_supports_names_the_ones_there_are(self):
        tts = Speechify({"model": "geffen_32", "timeout_s": 5}, Ctx())
        with mock.patch("urllib.request.urlopen", return_value=page("simba-3.2", "simba-3.0")):
            with self.assertRaisesRegex(RuntimeError, r"'geffen_32': no Speechify voice supports it\. .*: "
                                                      r"simba-3\.0, simba-3\.2$"):
                tts.voices()

    def test_a_supported_model_lists_its_voices(self):
        tts = Speechify({"model": "simba-3.2", "timeout_s": 5}, Ctx())
        with mock.patch("urllib.request.urlopen", return_value=page("simba-3.2", "simba-3.0")):
            self.assertEqual([v["id"] for v in tts.voices()], ["v0"])


if __name__ == "__main__":
    unittest.main()
