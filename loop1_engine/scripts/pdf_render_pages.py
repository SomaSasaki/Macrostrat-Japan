# -*- coding: utf-8 -*-
"""Bundled-runtime helper that renders selected PDF pages with pypdfium2."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pypdfium2 as pdfium


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4:
        print("usage: pdf_render_pages.py PDF OUTPUT_DIR JSON_PAGES SCALE", file=sys.stderr)
        return 2
    pdf_path = Path(args[0]).expanduser().resolve()
    output_dir = Path(args[1]).expanduser().resolve()
    pages = json.loads(args[2])
    scale = float(args[3])
    if not isinstance(pages, list) or not all(isinstance(page, int) and page >= 1 for page in pages):
        raise ValueError("JSON_PAGES must be a list of one-based positive integers")
    output_dir.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(str(pdf_path))
    output: list[str] = []
    try:
        for page_number in pages:
            if page_number > len(document):
                raise ValueError(f"PDF page is out of range: {page_number}")
            page = document[page_number - 1]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            path = output_dir / f"page_{page_number:04d}_candidate.png"
            image.save(path, format="PNG", optimize=True)
            output.append(str(path.resolve()))
            bitmap.close()
            page.close()
    finally:
        document.close()
    # ASCII-only JSON avoids Windows console-codepage corruption in paths
    # whose multibyte representation happens to contain a backslash byte.
    print(json.dumps(output, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
