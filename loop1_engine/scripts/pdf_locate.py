# -*- coding: utf-8 -*-
"""
pdf_locate.py — ZFK本文の一節が図幅PDFの何ページにあるかを突き止める

ZFKのAPIにはページ番号が入っていない。節番号（4.12）と小見出し（分布及び層厚）は
取れるが、「実際にPDFのどこを見ればいいか」が分からない。そこで手元のPDFから
テキストを引き出してページ索引を作り、ZFKの文と照合する。

★ 記号の正規化が肝。PDFとZFKで書き分けが違う:
    PDF「第3‒4地点」(U+2012)   ZFK「第3-4地点」(U+002D)
    PDF「第3. 4図」(空白入り)   ZFK「第3.4図」
    PDF は行の途中で改行が入る
  これを揃えないと1件も一致しない（実際に全滅した）。

★ 印刷ページ番号も拾う。PDFの通し番号（p.60）より、説明書に印刷された
  ページ番号（p.52）のほうが引用に使える。両方出す。

索引は references/m{id}_pdfpages.json にキャッシュする（PDF解析は約7秒／91ページ）。
"""

import json
import os
import re
import unicodedata

# ダッシュ・波ダッシュの類は全部ハイフンに寄せる
_DASH = dict.fromkeys(map(ord, "‒–—―−‐-ｰー~〜～"), "-")
# 空白・読点・句点は落とす（PDFは行途中で改行し、句読点の幅も違う）
_DROP = re.compile(r"[\s　,，、.．・]+")

CACHE_NAME = "{mid}_pdfpages.json"


def normalize(text):
    """PDFとZFKの表記ゆれを吸収する。"""
    s = unicodedata.normalize("NFKC", str(text or "")).translate(_DASH)
    return _DROP.sub("", s)


def _printed_page_no(raw_text):
    """本文下部の「― 52 ―」から印刷ページ番号を拾う。"""
    m = re.findall(r"[-]\s*(\d{1,3})\s*[-]\s*$",
                   unicodedata.normalize("NFKC", raw_text or "").translate(_DASH).strip())
    if m:
        return int(m[-1])
    m = re.findall(r"[-]\s*(\d{1,3})\s*[-]",
                   unicodedata.normalize("NFKC", raw_text or "").translate(_DASH))
    return int(m[-1]) if m else None


def build_index(pdf_path, cache_path=None, quiet=False):
    """PDFを1回だけ解析してページ索引を作る。失敗しても None を返すだけ。"""
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("pdf") == os.path.basename(pdf_path):
                return d
        except Exception:
            pass
    if not pdf_path or not os.path.exists(pdf_path):
        return None
    pages, printed = [], []
    try:
        try:
            import pdfplumber
        except ImportError:
            pdfplumber = None
        if pdfplumber is not None:
            with pdfplumber.open(pdf_path) as doc:
                raw_pages = [p.extract_text() or "" for p in doc.pages]
        else:
            # ``extract_abstract`` can delegate to Codex Desktop's bundled
            # Python runtime.  Reuse the same page-text source so the user's
            # lightweight venv does not need a second PDF dependency.
            from extract_abstract import _fallback_pages
            raw_pages = list(_fallback_pages(os.path.abspath(pdf_path)))
        for raw in raw_pages:
            pages.append(normalize(raw))
            printed.append(_printed_page_no(raw))
    except Exception as e:
        if not quiet:
            print(f"  [info] PDFを読めませんでした（照合は省略）: {type(e).__name__}")
        return None

    if not any(pages):
        if not quiet:
            print("  [info] PDFにテキスト層がありません（画像PDF）。ページ照合は省略します。")
        return None

    idx = {"pdf": os.path.basename(pdf_path), "pages": pages, "printed": printed}
    if cache_path:
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)
        except Exception:
            pass
    return idx


# 照合に使う先頭文字数を長い順に試す。
# 長いほど確実だが、PDF側の改ページやルビ挿入で切れることがあるので段階的に短くする。
_TRY_LENGTHS = (60, 40, 25, 18)
_MIN_KEY = 12


def locate(index, text):
    """
    text がPDFの何ページにあるかを返す。

    戻り値: {"pdf_page": 1始まり, "printed_page": int|None, "matched": 照合に使った文字数}
            見つからなければ None
    """
    if not index or not text:
        return None
    n = normalize(text)
    if len(n) < _MIN_KEY:
        return None
    pages = index["pages"]
    for L in (len(n),) + _TRY_LENGTHS:
        key = n[:L]
        if len(key) < _MIN_KEY:
            break
        for i, pg in enumerate(pages):
            if key in pg:
                return {"pdf_page": i + 1,
                        "printed_page": index["printed"][i],
                        "matched": len(key)}
    return None


def cite(index, text, sec_label=None, sec_title=None):
    """
    出典を1行の文字列にする。

    例: 「§4.12 分布及び層厚 / PDF p.60（印刷 p.52）」
       PDFで照合できなければ節番号だけを返す。
    """
    bits = []
    if sec_label:
        bits.append(f"§{sec_label}")
    if sec_title:
        bits.append(str(sec_title))
    hit = locate(index, text)
    if hit:
        p = f"PDF p.{hit['pdf_page']}"
        if hit["printed_page"] is not None:
            p += f"（印刷 p.{hit['printed_page']}）"
        bits.append(p)
    return " / ".join(bits)


def find_pdf(ref_dir):
    """references/ の中から図幅説明書PDFを探す。"""
    if not ref_dir or not os.path.isdir(ref_dir):
        return None
    cands = [os.path.join(ref_dir, f) for f in sorted(os.listdir(ref_dir))
             if f.lower().endswith(".pdf")]
    if not cands:
        return None
    # 説明書は末尾が _D.pdf（Description）。無ければ一番大きいものを使う。
    for c in cands:
        if c.lower().endswith("_d.pdf"):
            return c
    return max(cands, key=os.path.getsize)


def index_for(map_id, ref_dir, quiet=False):
    """図幅のPDFページ索引を用意する（キャッシュあり）。"""
    pdf = find_pdf(ref_dir)
    if not pdf:
        return None
    cache = os.path.join(ref_dir, CACHE_NAME.format(mid=f"m{map_id}"))
    return build_index(pdf, cache, quiet=quiet)
