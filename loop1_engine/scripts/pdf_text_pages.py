# -*- coding: utf-8 -*-
"""Emit PDF page text as JSON for runtimes that bundle ``pdfplumber``.

This tiny helper is invoked by :mod:`extract_abstract` when the user's Python
environment has neither Poppler's ``pdftotext`` nor a PDF extraction package.
It deliberately writes only JSON to stdout so the caller can fail closed.
"""
from __future__ import annotations

import json
import sys

import pdfplumber


def main() -> int:
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--count":
        with pdfplumber.open(args[1]) as document:
            count = len(document.pages)
        print(count)
        return 0
    if len(args) == 4 and args[0] == "--pages":
        start, end, pdf = int(args[1]), int(args[2]), args[3]
        with pdfplumber.open(pdf) as document:
            pages = [
                document.pages[index].extract_text(layout=True) or ""
                for index in range(max(0, start - 1), min(len(document.pages), end))
            ]
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        json.dump(pages, sys.stdout, ensure_ascii=False)
        return 0
    if len(args) != 1:
        return 2
    with pdfplumber.open(args[0]) as document:
        pages = [page.extract_text(layout=True) or "" for page in document.pages]
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    json.dump(pages, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
