"""Audio: the [audio] table, joining in each format, and which files the owner can attach."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nanotea import audio, files
from nanotea.config import ConfigError


def tone(path: Path, seconds: float, rate: int = 48000) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate={rate}:duration={seconds}", str(path)], check=True)
    return path


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels",
                          "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return {**data["streams"][0], "duration": float(data["format"]["duration"])}


class Settings(unittest.TestCase):
    def test_defaults(self):
        mix, limit = audio.settings({})
        self.assertEqual((mix.format, mix.kbps, mix.rate, mix.ext), ("mp3", 192, 48000, "mp3"))
        self.assertEqual(limit, 1024 * 1024 * 1024)

    def test_errors(self):
        for table, message in [
            ({"format": "ogg"}, r"format must be one of mp3, aac, flac, wav; not 'ogg'"),
            ({"rate": 22050}, r"rate must be one of 44100, 48000, 88200, 96000; not 22050"),
            ({"kbps": 999}, r"kbps must be from 32 to 320; not 999"),
            ({"kbps": "192"}, r"kbps must be a positive whole number; not '192'"),
            ({"max_upload_mb": 0}, r"max_upload_mb must be a positive whole number; not 0"),
            ({"max_upload_mb": True}, r"max_upload_mb must be a positive whole number"),
            ({"bitrate": 192}, r"\[audio\] has no bitrate; it takes format, kbps, rate, max_upload_mb"),
        ]:
            with self.subTest(table=table), self.assertRaisesRegex(ConfigError, message):
                audio.settings({"audio": table})
        with self.assertRaisesRegex(ConfigError, r"\[audio\] must be a table"):
            audio.settings({"audio": "flac"})


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class Join(unittest.TestCase):
    def test_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            a, b = tone(tmp / "a.wav", 0.5, 44100), tone(tmp / "b.flac", 0.25, 96000)
            for fmt, codec in (("mp3", "mp3"), ("aac", "aac"), ("flac", "flac"), ("wav", "pcm_s24le")):
                with self.subTest(fmt=fmt):
                    mix = audio.Mix(fmt, 256, 96000 if fmt in ("flac", "wav") else 48000)
                    out = mix.join([a, 0.25, b], tmp / f"out-{fmt}")
                    self.assertEqual(out.name, f"out-{fmt}.{mix.ext}")
                    got = probe(out)
                    self.assertEqual((got["codec_name"], int(got["sample_rate"]), got["channels"]),
                                     (codec, mix.rate, 2))
                    self.assertAlmostEqual(got["duration"], 1.0, delta=0.08)

    def test_unreadable_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp, "bad.wav")
            bad.write_bytes(b"not audio")
            with self.assertRaisesRegex(RuntimeError, "ffmpeg exited"):
                audio.Mix().join([bad], Path(tmp, "out"))


class Files(unittest.TestCase):
    def test_types(self):
        for name, ctype, want in [
            ("take.wav", "audio/wav", "audio/wav"), ("take.wav", "audio/x-wav", "audio/x-wav"),
            ("mix.flac", "", "audio/flac"), ("mix.aif", "application/octet-stream", "audio/aiff"),
            ("voice.caf", "", "audio/x-caf"), ("photo.heic", "", "image/heic"), ("a.pdf", "application/pdf",
                                                                                 "application/pdf"),
        ]:
            with self.subTest(name=name, ctype=ctype):
                self.assertEqual(files.type_of(name, ctype), want)
        for name, ctype in [("notes.txt", "text/plain"), ("mix.flac", "text/plain"), ("mystery", ""),
                            ("song.mid", "application/octet-stream")]:
            with self.subTest(name=name, ctype=ctype), self.assertRaisesRegex(files.Unsupported, "audio"):
                files.type_of(name, ctype)

    def test_kept_as_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            raw = bytes(range(256)) * 4096
            upload = tmp / "upload"
            upload.write_bytes(raw)
            file_id, ctype, size = files.save_draft(tmp / "drafts", "Mix 3 (final).flac", "", upload)
            self.assertEqual((ctype, size), ("audio/flac", len(raw)))
            self.assertFalse(upload.exists())
            [(src, meta)] = files.drafts_for(tmp / "drafts", [file_id])
            (tmp / "msg").mkdir()
            [record] = files.move_into(tmp / "msg", [(src, meta)])
            self.assertEqual(record["stored"], "files/1-Mix_3__final_.flac")
            self.assertEqual((tmp / "msg" / record["stored"]).read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
