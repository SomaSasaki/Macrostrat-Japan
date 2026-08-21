# -*- coding: utf-8 -*-
"""
extract_abstract.py — 図幅説明書PDFから英文Abstractだけを取り出す

GSJの5万分の1説明書は巻末に英文Abstractがあり、そこに
  ・全地層の英語名
  ・各地層の年代（例: the Yanagisawa: 12–10.5 Ma）
  ・上下関係（unconformably overlie ...）
  ・岩相（mainly composed of gravel bed）
がまとまっている。一戸図幅で検証したところ、完成形の30地層すべてと
b_prop/t_prop の元になった年代がここに揃っていた。

本文200ページを読む必要はなく、この5ページだけでよい。

使い方:
  python scripts/extract_abstract.py <pdf>            # 標準出力に表示
  python scripts/extract_abstract.py <pdf> -o out.txt # ファイルに保存
"""

import argparse
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

# 英文ページとみなす条件
MIN_LATIN = 500          # 英字がこれ未満のページは本文とみなさない
MIN_RATIO = 0.95         # 英字 / (英字+和字)
GAP_TOLERANCE = 2        # 図版ページ等で途切れても、これ以下なら同じ塊とみなす
TAIL_FRACTION = 0.45     # 巻末側のこの割合だけを探索する


def _native_tool(name):
    """Return a directly executable binary, excluding Windows ``.cmd`` shims."""
    path = shutil.which(name)
    if not path or (os.name == "nt" and Path(path).suffix.casefold() in {".cmd", ".bat"}):
        return None
    return path


@lru_cache(maxsize=4)
def _fallback_pages(pdf):
    """Extract page text without requiring Poppler command-line tools.

    Codex Desktop bundles ``pdfplumber`` even on Windows installations where
    ``pdftotext`` is not on PATH.  Keeping this fallback here makes the
    one-command pilot able to derive the English Abstract immediately after a
    PDF download on either runtime.
    """
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pdfplumber is None:
        configured = os.environ.get("CODEX_PRIMARY_RUNTIME")
        runtime_root = (
            Path(configured).expanduser()
            if configured
            else Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
        )
        python = runtime_root / "dependencies" / "python" / (
            "python.exe" if os.name == "nt" else "bin/python"
        )
        helper = Path(__file__).with_name("pdf_text_pages.py")
        if not python.is_file() or not helper.is_file():
            return ()
        completed = subprocess.run(
            [str(python), str(helper), str(pdf)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            return ()
        try:
            pages = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return ()
        return tuple(str(page or "") for page in pages) if isinstance(pages, list) else ()
    try:
        with pdfplumber.open(pdf) as document:
            return tuple(page.extract_text(layout=True) or "" for page in document.pages)
    except Exception:
        return ()


def _bundled_pdf_helper(*arguments):
    configured = os.environ.get("CODEX_PRIMARY_RUNTIME")
    runtime_root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
    )
    python = runtime_root / "dependencies" / "python" / (
        "python.exe" if os.name == "nt" else "bin/python"
    )
    helper = Path(__file__).with_name("pdf_text_pages.py")
    if not python.is_file() or not helper.is_file():
        return None
    return subprocess.run(
        [str(python), str(helper), *map(str, arguments)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@lru_cache(maxsize=8)
def _fallback_page_count(pdf):
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pdfplumber is not None:
        try:
            with pdfplumber.open(pdf) as document:
                return len(document.pages)
        except Exception:
            return 0
    completed = _bundled_pdf_helper("--count", pdf)
    if completed is None or completed.returncode != 0:
        return 0
    try:
        return int(completed.stdout.strip())
    except ValueError:
        return 0


@lru_cache(maxsize=8)
def _fallback_page_range(pdf, start, end):
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pdfplumber is not None:
        try:
            with pdfplumber.open(pdf) as document:
                return tuple(
                    document.pages[index].extract_text(layout=True) or ""
                    for index in range(max(0, start - 1), min(len(document.pages), end))
                )
        except Exception:
            return ()
    completed = _bundled_pdf_helper("--pages", start, end, pdf)
    if completed is None or completed.returncode != 0:
        return ()
    try:
        pages = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ()
    return tuple(str(page or "") for page in pages) if isinstance(pages, list) else ()


def page_count(pdf):
    pdfinfo = _native_tool("pdfinfo")
    if pdfinfo:
        out = subprocess.run([pdfinfo, pdf], capture_output=True, text=True).stdout
        m = re.search(r"Pages:\s+(\d+)", out)
        if m:
            return int(m.group(1))
    return _fallback_page_count(os.path.abspath(pdf))


def page_text(pdf, pg, layout=True):
    pdftotext = _native_tool("pdftotext")
    if pdftotext:
        cmd = [pdftotext]
        if layout:
            cmd.append("-layout")
        cmd += ["-f", str(pg), "-l", str(pg), pdf, "-"]
        return subprocess.run(cmd, capture_output=True, text=True).stdout
    pages = _fallback_pages(os.path.abspath(pdf))
    return pages[pg - 1] if 1 <= pg <= len(pages) else ""


def score_page(text):
    latin = len(re.findall(r"[A-Za-z]", text))
    jp = len(re.findall(r"[ぁ-んァ-ヶ一-龠]", text))
    total = latin + jp
    return latin, jp, (latin / total if total else 0.0)


def find_abstract_pages(pdf, verbose=False):
    """英文Abstractのページ範囲 [start, end] を返す。見つからなければ None。"""
    n = page_count(pdf)
    if not n:
        return None
    start_scan = max(1, int(n * (1 - TAIL_FRACTION)))

    native_text = _native_tool("pdftotext")
    fallback = () if native_text else _fallback_page_range(os.path.abspath(pdf), start_scan, n)

    def text_for(pg):
        if native_text:
            return page_text(pdf, pg)
        index = pg - start_scan
        return fallback[index] if 0 <= index < len(fallback) else ""

    english = []
    latin_by_page = {}
    for pg in range(start_scan, n + 1):
        latin, jp, ratio = score_page(text_for(pg))
        latin_by_page[pg] = latin
        if latin >= MIN_LATIN and ratio >= MIN_RATIO:
            english.append(pg)
        if verbose and latin + jp > 100:
            mark = "  <= 英文" if (latin >= MIN_LATIN and ratio >= MIN_RATIO) else ""
            print(f"    p.{pg:>3} 英字{latin:>5} 和字{jp:>5} {ratio*100:>5.1f}%{mark}")

    if not english:
        return None

    # 連続した塊にまとめる（小さな隙間は許容）
    runs, cur = [], [english[0]]
    for pg in english[1:]:
        if pg - cur[-1] <= GAP_TOLERANCE + 1:
            cur.append(pg)
        else:
            runs.append(cur)
            cur = [pg]
    runs.append(cur)

    # 一番英字量の多い塊を採用
    def bulk(run):
        return sum(latin_by_page.get(p, 0) for p in run)

    best = max(runs, key=bulk)
    return (best[0], best[-1])


def extract(pdf, verbose=False):
    """(abstract本文, (開始ページ, 終了ページ)) を返す。"""
    rng = find_abstract_pages(pdf, verbose=verbose)
    if not rng:
        return "", None
    s, e = rng
    pdftotext = _native_tool("pdftotext")
    if pdftotext:
        txt = subprocess.run(
            [pdftotext, "-layout", "-f", str(s), "-l", str(e), pdf, "-"],
            capture_output=True, text=True).stdout
    else:
        txt = "\n\n".join(_fallback_page_range(os.path.abspath(pdf), s, e))
    return txt, rng


def summarize(txt):
    """取れた内容の手応えを数字で返す（検証用）。"""
    has_marker = bool(re.search(r"\bABSTRACT\b", txt, re.I))
    ages = re.findall(r"(\d+(?:\.\d+)?)\s*[–—-]\s*(\d+(?:\.\d+)?)\s*Ma", txt)
    single = re.findall(r"(?:ca\.\s*)?(\d+(?:\.\d+)?)\s*Ma", txt)
    forms = re.findall(r"\b([A-Z][a-zA-Zū]+)\s+(?:Formation|Group|Pluton|Member)", txt)
    return {
        "chars": len(txt),
        "ABSTRACT の見出し": has_marker,
        "年代レンジ (A–B Ma)": len(ages),
        "年代 (単独 Ma)": len(set(single)),
        "地層名 (Formation/Group等)": len(set(forms)),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="図幅PDFから英文Abstractを抽出")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", help="保存先")
    ap.add_argument("-v", "--verbose", action="store_true", help="ページ判定を表示")
    a = ap.parse_args()

    if not os.path.exists(a.pdf):
        print(f"[ERROR] 見つかりません: {a.pdf}")
        sys.exit(1)

    text, rng = extract(a.pdf, verbose=a.verbose)
    if not text.strip():
        print("[ERROR] 英文Abstractを検出できませんでした。")
        print("        -v を付けるとページごとの判定を確認できます。")
        sys.exit(1)

    print(f"検出: p.{rng[0]}–{rng[1]}")
    for k, v in summarize(text).items():
        print(f"  {k}: {v}")

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"保存: {a.out}")
    else:
        print("-" * 70)
        print(text)
