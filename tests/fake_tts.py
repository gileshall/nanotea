import sys
from pathlib import Path

Path(sys.argv[2]).write_bytes(b"fake audio for " + Path(sys.argv[1]).read_bytes())
