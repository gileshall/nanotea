"""python -m e2e.configure SRC DEST EDITS: SRC's config with EDITS made in place, written to DEST and checked.

EDITS is JSON, [[["table", ...], "key", value], ...], applied as the Configuration page applies an owner's changes.
DEST's directory needs the env file the config names."""

import json
import subprocess
import sys
from pathlib import Path

from nanotea import configedit


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    src, dest, edits = Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.argv[3])
    text, _, _ = configedit.edit(src.read_text(), [(tuple(t), k, v, False) for t, k, v in edits])
    dest.write_text(text)
    # Only the file's form: its plugins are built where the service runs.
    r = subprocess.run([sys.executable, "-c", "import sys, tomllib; from nanotea.config import check; "
                        "check(tomllib.load(open(sys.argv[1], 'rb')))", str(dest)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"{dest}: {r.stderr.strip()}")


if __name__ == "__main__":
    main()
