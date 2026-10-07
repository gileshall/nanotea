"""Reading aloud: an agent's message, written for reading, turned into what the voice says. Mechanical, and set by
the owner's read_* settings (settings.py), so the same message always reads the same way under the same
settings. [[audio N]] markers pass through untouched: clips are spliced there."""

import hashlib
import json
import re
from urllib.parse import urlsplit

from nanotea import markdown
from nanotea.store import AUDIO_MARKER, SHOWN

KEYS = ("read_code", "read_tables", "read_links", "read_paths", "read_hashes", "read_symbols", "read_max_words")

URL = re.compile(r"\bhttps?://[^\s<>()\[\]\"'`]+")
# A path at the start of a word: two or more parts, the last a file with an extension, and an optional :line.
PATH = re.compile(r"(?<![^\s(\[\"'`])(?:~|\.{1,2})?/?(?:[\w.@+-]+/)+([\w@+-][\w.@+-]*\.[A-Za-z0-9]+)(?::(\d+))?(?::\d+)?")
HASH = re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b")
SYMBOLS = [
    (re.compile(r"\s*(?:->|→|⟶)\s*"), " to "),
    (re.compile(r"\s*(?:<-|←)\s*"), " from "),
    (re.compile(r"\s*(?:=>|⇒)\s*"), ", so "),
    (re.compile(r"\s*(?:>=|≥)\s*"), " at least "),
    (re.compile(r"\s*(?:<=|≤)\s*"), " at most "),
    (re.compile(r"\s*(?:!=|≠)\s*"), " is not "),
    (re.compile(r"\s*≈\s*"), " about "),
    (re.compile(r"(?<![\w~])~(?=\d)"), "about "),
    (re.compile(r"±\s*"), "plus or minus "),
    (re.compile(r"(?<=\d)\s*[x×]\s*(?=\d)"), " by "),
    (re.compile(r"\s+&\s+"), " and "),
    (re.compile(r"\be\.g\.,?", re.I), "for example"),
    (re.compile(r"\bi\.e\.,?", re.I), "that is"),
    (re.compile(r"\betc\.", re.I), "and so on"),
    (re.compile(r"\bvs\.?(?=\s)", re.I), "versus"),
    (re.compile(r"\bw/o\b", re.I), "without"),
    (re.compile(r"\bw/(?=\s)", re.I), "with"),
]
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def reading(settings: dict) -> dict:
    """The settings reading aloud follows."""
    return {k: settings[k] for k in KEYS}


def stem(read: dict, defaults: dict) -> str:
    """The voiced file's name, less its extension: the same for the same reading. Under the defaults it is
    "audio", the name messages were voiced under before reading had settings."""
    if read == {k: defaults[k] for k in KEYS}:
        return "audio"
    return "audio-" + hashlib.sha256(json.dumps(read, sort_keys=True).encode()).hexdigest()[:10]


def _url(found: str, how: str) -> str:
    """A web address found in the text, said as how says (site, skip). Punctuation after it is the sentence's."""
    url = found.rstrip(".,;:!?")
    if how == "skip":
        return found[len(url):]
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return (f"a link to {host}" if host else "a link") + found[len(url):]


def _tidy(text: str) -> str:
    """Spaces and brackets a removal left behind."""
    text = re.sub(r"[ \t]+([,.;:!?])(?=\s|$)", r"\1", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def _cut(text: str, words: int) -> str:
    """At most about words words, ended at a sentence's end, then a pointer to the rest."""
    found = list(re.finditer(r"\S+", text))
    if len(found) <= words:
        return text
    limit = found[words - 1].end()
    ends = [m.end() for m in SENTENCE_END.finditer(text, 0, limit + 1)]
    cut = ends[-1] if ends else limit
    clips = " ".join(f"[[audio {n}]]" for n in AUDIO_MARKER.findall(text[cut:]))  # clips still play
    return text[:cut].rstrip() + "\n\nThe rest is in the app." + (f"\n\n{clips}" if clips else "")


def for_voice(script: str, read: dict) -> str:
    """What the voice says for script (a message's markdown), read as read says."""
    text = markdown.plain(SHOWN.sub("", script), speech=True, code=read["read_code"], tables=read["read_tables"])
    keep = AUDIO_MARKER.findall(text)
    if read["read_links"] != "read":
        text = URL.sub(lambda m: _url(m[0], read["read_links"]), text)
    if read["read_paths"] == "file":
        text = PATH.sub(lambda m: m[1] + (f", line {m[2]}" if m[2] else ""), text)
    if read["read_hashes"] == "skip":
        text = HASH.sub("", text)
    if read["read_symbols"]:
        for pattern, words in SYMBOLS:
            text = pattern.sub(words, text)
    if read["read_links"] == "skip" or read["read_hashes"] == "skip" or read["read_symbols"]:
        text = _tidy(text)
    if read["read_max_words"]:
        text = _cut(text, read["read_max_words"])
    if AUDIO_MARKER.findall(text) != keep:
        raise RuntimeError(f"reading aloud moved the message's clips: {keep} became {AUDIO_MARKER.findall(text)}")
    return text
