import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "loop1_engine" / "scripts"
SRC = ROOT / "loop1_engine" / "src"

for p in (SCRIPTS, SRC, ROOT):
    p_str = str(p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)