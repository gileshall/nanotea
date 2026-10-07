"""Files the owner attaches (pictures, PDFs, video, audio). Uploaded as drafts while composing, kept byte for byte;
moved into the message when it is sent, as files/<n>-<name>."""

import json
import os
import re
import secrets
from pathlib import Path

PICTURES = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/heic": "heic"}
AUDIO = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav",
         "audio/flac": "flac", "audio/x-flac": "flac", "audio/aiff": "aiff", "audio/x-aiff": "aiff",
         "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
         "audio/ogg": "ogg", "audio/opus": "opus", "audio/webm": "webm", "audio/x-caf": "caf"}
VIDEO = {"video/mp4": "mp4", "video/quicktime": "mov", "video/x-m4v": "m4v"}
FILE_TYPES = {**PICTURES, "application/pdf": "pdf", **VIDEO, **AUDIO}
# A browser that can't name a file's type sends none, or application/octet-stream: the extension names it.
BY_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp",
          "heic": "image/heic", "pdf": "application/pdf", "wav": "audio/wav", "flac": "audio/flac",
          "aif": "audio/aiff", "aiff": "audio/aiff", "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
          "ogg": "audio/ogg", "opus": "audio/opus", "webm": "audio/webm", "caf": "audio/x-caf",
          "mp4": "video/mp4", "mov": "video/quicktime", "m4v": "video/x-m4v"}
UNNAMED = ("", "application/octet-stream")
FILE_ID = r"[0-9a-f]{12}"


class Unsupported(ValueError):
    pass


def type_of(name: str, ctype: str) -> str:
    """The file's type: as the browser named it, or by its extension when the browser didn't."""
    if ctype in FILE_TYPES:
        return ctype
    ext = Path(name).suffix[1:].lower()
    if ctype in UNNAMED and ext in BY_EXT:
        return BY_EXT[ext]
    raise Unsupported(f"pictures, PDFs, video (mp4, mov) and audio ({', '.join(sorted(set(AUDIO.values())))}) "
                      f"only; not {ctype or 'a file of no type'} named {name!r}")


def save_draft(drafts: Path, name: str, ctype: str, upload: Path) -> tuple[str, str, int]:
    """Moves the uploaded file into the drafts. Returns its id, type and size. Raises Unsupported for a type that
    isn't a picture, PDF, video or audio."""
    ctype = type_of(name, ctype)
    folder = drafts / "files"
    folder.mkdir(parents=True, exist_ok=True)
    file_id = secrets.token_hex(6)
    size = upload.stat().st_size
    os.replace(upload, folder / f"{file_id}.{FILE_TYPES[ctype]}")
    (folder / f"{file_id}.json").write_text(json.dumps({"name": name, "type": ctype, "size": size}))
    return file_id, ctype, size


def drafts_for(drafts: Path, ids: list[str]) -> list[tuple[Path, dict]]:
    """The uploaded drafts for these ids, in order. Raises ValueError for one that was never uploaded."""
    found = []
    for file_id in ids:
        if not re.fullmatch(FILE_ID, file_id):
            raise ValueError(f"bad file id: {file_id!r}")
        meta_path = drafts / "files" / f"{file_id}.json"
        if not meta_path.exists():
            raise ValueError(f"file {file_id} was never uploaded")
        meta = json.loads(meta_path.read_text())
        found.append((drafts / "files" / f"{file_id}.{FILE_TYPES[meta['type']]}", meta))
    return found


def move_into(dest: Path, sources: list[tuple[Path, dict]]) -> list[dict]:
    """Move drafts into dest/files/. Returns their records: name, stored (relative to dest), type, size."""
    records = []
    if sources:
        (dest / "files").mkdir()
    for n, (src, meta) in enumerate(sources, 1):
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(meta["name"]).stem)[:60] or "file"
        stored = f"files/{n}-{stem}.{FILE_TYPES[meta['type']]}"
        os.replace(src, dest / stored)
        records.append({"name": meta["name"], "stored": stored, "type": meta["type"], "size": meta["size"]})
    return records
