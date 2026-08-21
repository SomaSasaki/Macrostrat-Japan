# -*- coding: utf-8 -*-
"""Synchronize and compile GitHub release documentation inside loop3_community/publications/."""

from __future__ import annotations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUB_DIR = ROOT / "loop3_community" / "publications"
PAPER_EN = PUB_DIR / "PUBLICATION_PAPER.en.md"
PAPER_JA = PUB_DIR / "PUBLICATION_PAPER.ja.md"
PAPER_MASTER = PUB_DIR / "PUBLICATION_PAPER.md"

def sync_github_release() -> None:
    """Compile English-top and Japanese-bottom unified paper inside publications/."""
    if not PAPER_EN.is_file() or not PAPER_JA.is_file():
        raise FileNotFoundError("Publication papers (en/ja) not found in loop3_community/publications/")
    
    en_content = PAPER_EN.read_text(encoding="utf-8")
    ja_content = PAPER_JA.read_text(encoding="utf-8")
    
    master_content = f"""# Macrostrat Japan: Automated Construction and Quality Verification Architecture
<!-- AUTOMATICALLY SYNCHRONIZED FROM loop3_community/publications/ -->

# [Part 1: English Edition]

{en_content}

---
---

# [Part 2: 日本語版論文]

{ja_content}
"""
    PAPER_MASTER.write_text(master_content, encoding="utf-8")
    print(f"Successfully compiled GitHub publication master: {PAPER_MASTER} ({len(master_content)} bytes)")
    print("Structure: English on top, Japanese on bottom.")

if __name__ == "__main__":
    sync_github_release()