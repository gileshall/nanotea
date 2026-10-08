"""The code's version: a hash of the package's files, so the service and the programs that talk to it can tell
when they run different code.

The service sends its version on every response (HEADER). A long-lived program (nanotea mcp, nanotea-tell
--listen, nanotea hook rewake) that sees a version other than its own, with that version's code on disk, is
stale: starting it again would load the service's code, so it hands over (docs/service.md#upgrading).
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).parent
HEADER = "X-Nanotea-Version"


def on_disk() -> str:
    h = hashlib.sha256()
    for f in sorted(ROOT.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            h.update(f.relative_to(ROOT).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:12]


RUNNING = on_disk()


def stale(service: str | None) -> bool:
    """Whether this process runs older code than the service, and starting it again would load the service's.
    A service older than the code on disk (mid-deploy, before its restart) makes nothing stale."""
    return service is not None and service != RUNNING and on_disk() == service
