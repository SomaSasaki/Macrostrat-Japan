# -*- coding: utf-8 -*-
"""
gsj_derived.py — ZFKの `derived` ブロック（GSJが本文から抽出済みの構造化データ）を読む

これまでこのパイプラインは ZFK の `target.text`（本文べた書き）だけを使い、
自前の正規表現で層厚や岩相を拾い直していた。ところが ZFK の `derived` には
GSJ自身が本文から抽出した構造化データが最初から入っている。

  derived.thickness  {min_m, max_m, approx, raw, source_block_index, confidence, evidence}
  derived.lithology  [{term_jp, term_en, category, confidence, evidence{block_index, snippet}}]
  derived.contacts   [{type, type_jp, with_unit_name, text, source_block_index, confidence, evidence}]
  derived.structures / minerals / distribution

十和田図幅（23層）での取得状況:
  thickness  17層 / lithology 17層 / contacts 14層

★ これは本文（日本語）由来なので、英文Abstract（要約）より情報が細かく、
  LLMを通さないので幻覚が入らない。確信度と該当箇所も付いてくる。

★ ただし用語は日本語→英語の素朴な対応（凝灰岩→Tuff）で、Macrostratの語彙とは
  一致しないことがある。そのままの語を出典つきで見せ、判断は人に任せる。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    lithology_role_from_context,
    parse_lithology_relations,
    resolve_lithology_term,
)

# GSJ の term_en は先頭大文字（Tuff, Andesite）。Macrostrat は小文字。
# 明らかな綴りの差だけ直す。無理な言い換えはしない（推測で埋めない原則）。
_LITH_FIX = {
    "tuff breccia": "tuff breccia",
    "pumice tuff": "tuff",
    "lapilli tuff": "lapilli tuff",
    "welded tuff": "tuff",
    "gravel": "gravel",
    "sand": "sand",
    "silt": "silt",
    "mud": "mud",
    "mudstone": "mudstone",
    "sandstone": "sandstone",
    "conglomerate": "conglomerate",
    "andesite": "andesite",
    "basalt": "basalt",
    "dacite": "dacite",
    "rhyolite": "rhyolite",
    "tuff": "tuff",
    "lava": "lava",
    "scoria": "scoria",
    "pumice": "pumice",
    "peat": "peat",
    "diatomite": "diatomite",
    "shale": "shale",
    "siltstone": "siltstone",
}

# basal_surface に使える語（公式仕様の例に合わせる）
_CONTACT_MAP = {
    "unconformable": "unconformable",
    "conformable": "conformable",
    "disconformable": "disconformable",
    "fault": "fault",
    "gradational": "gradational",
    "intrusive": "intrusive",
}

# 「瞬間的な噴火イベント」とみなす地層名のことば。
# ★ 年代が1点であることと **両方** 揃ったときだけ噴火イベント扱いにする
#   （段丘堆積物などが年代1点で出ることがあるため）。
ERUPTION_WORDS = (
    "火砕流", "軽石流", "スコリア流", "火山灰", "テフラ", "降下", "溶岩", "噴出物",
    "pyroclastic", "tephra", "ash", "lava", "pumice flow", "scoria",
    "ignimbrite", "fall deposit",
)


def _as_text(v):
    """pandas の NaN / Series / None を安全に文字列にする。"""
    if v is None:
        return ""
    if hasattr(v, "tolist"):            # Series / ndarray
        try:
            return " ".join(_as_text(x) for x in v.tolist())
        except Exception:
            return ""
    if isinstance(v, float) and v != v:  # NaN
        return ""
    return str(v)


def is_eruption_unit(name, *names):
    """地層名が噴火イベント（瞬間的な堆積）を指しているか。"""
    hay = " ".join(_as_text(n) for n in (name,) + names).lower()
    return any(w.lower() in hay for w in ERUPTION_WORDS)


def _clean_lith(term_en, term_jp=""):
    if term_jp:
        japanese = parse_lithology_relations(term_jp)
        japanese_terms = [*japanese.get("major", []), *japanese.get("minor", [])]
        if len(japanese_terms) == 1:
            return japanese_terms[0]
    t = " ".join(str(term_en or "").split()).lower()
    if not t:
        return ""
    fixed = _LITH_FIX.get(t, t)
    resolved = resolve_lithology_term(fixed)
    return resolved.get("term") or fixed


_LITHOLOGY_ROLE_WORDS = re.compile(
    r"主体|主に|構成され|からなる|伴う|挟|互層|まれに|一部で"
)
_NEXT_SECTION = re.compile(
    r"(?m)^\s*(?:化石|時代|年代|岩石記載|地質構造|対比|堆積環境|地層名|模式地)\s*$"
)


def _lithology_section_items(unit, pdf_index=None):
    """Deterministically parse explicit role sentences in the unit's 岩相 section."""
    text = str((unit.get("target") or {}).get("text") or "")
    heading = re.search(r"(?m)^\s*岩相\s*$", text)
    if not heading:
        return []
    tail = text[heading.end():]
    end = _NEXT_SECTION.search(tail)
    section = tail[:end.start()] if end else tail
    all_sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。．！？])\s*|\n+", section)
        if sentence.strip()
    ]
    dominant_index = next((
        index for index, sentence in enumerate(all_sentences[:4])
        if re.search(r"主体|主に|構成され|からなる", sentence)
    ), None)
    sentences = []
    if dominant_index is not None:
        sentences.append(all_sentences[dominant_index])
        # Summary descriptions often continue with one short ``また`` or
        # ``まれに`` sentence.  Stop before locality/petrography detail.
        for sentence in all_sentences[dominant_index + 1:dominant_index + 3]:
            if re.match(r"^(?:また[，、,]?|まれに|一部で|しばしば)", sentence):
                sentences.append(sentence)
            else:
                break
    else:
        # A role phrase deep in a long petrographic description usually refers
        # to one bed, clast or sample, not to the unit as a whole.
        return []
    items = []
    for sentence in sentences:
        parsed = parse_lithology_relations(sentence)
        for role in ("major", "minor"):
            cue = (parsed.get("role_cues") or {}).get(role, "")
            if not cue or cue == "legend_list":
                continue
            for term in parsed.get(role) or []:
                items.append({
                    "term": term,
                    "term_jp": "",
                    "confidence": 1.0,
                    "snippet": sentence,
                    "src": _src(unit, None, sentence, pdf_index),
                    "role": role,
                    "role_cue": cue,
                    "method": "deterministic 岩相-section relation parser",
                })
    return items


def _block_title(blocks, index):
    """該当ブロックの直前の小見出し（地層名／分布及び層厚／岩相／時代など）を返す。"""
    if index is None or not blocks:
        return ""
    try:
        i = int(index)
    except (TypeError, ValueError):
        return ""
    for b in reversed(blocks[:max(i, 0) + 1]):
        if b.get("type") in ("section-title", "heading"):
            return str(b.get("text") or "").strip()
    return ""


def _src(unit, block_index, text="", pdf_index=None):
    """出典の1行表記。§節番号 / 小見出し / PDFページ。"""
    from pdf_locate import cite
    target = unit.get("target") or {}
    title = _block_title(target.get("blocks") or [], block_index)
    return cite(pdf_index, text, target.get("label"), title)


# ---------------------------------------------------------------------------

def thickness(unit, pdf_index=None):
    """
    層厚。{min_m, max_m, text, bound} を返す。値が無ければ None。

    ★ GSJ の min_m / max_m は「本文に出てくる代表値ひとつ」で、
      「層厚10 m以上」でも min=max=10 と入れてくる。これは誤りに近い。
      該当文に「以上／以下／最大／最小」があるかを見て振り分け直す:

        「層厚10 m以上」  → min=10, max=空欄（上限は分からない）
        「層厚10 m以下」  → min=空欄, max=10
        「最大で8 m」     → min=空欄, max=8
        「層厚6〜7 m」    → min=6, max=7（GSJが両方入れていればそのまま）

    ★ さらに、GSJ が拾うのは本文中ひとつだけ。場所ごとに違う値
      （西部10m・東部25m）は取りこぼす。だから数値は候補にすぎず、
      判断材料として該当文と出典ページを必ず添える。
    """
    dv = unit.get("derived") or {}
    th = dv.get("thickness") or {}
    if th.get("min_m") is None and th.get("max_m") is None and not th.get("raw"):
        return None
    raw = str(th.get("raw") or "")
    ev = (th.get("evidence") or {}).get("text") or ""
    mn, mx = th.get("min_m"), th.get("max_m")

    # 「以上／以下」の判定は該当箇所のすぐ近くだけを見る（本文全体だと誤爆する）
    near = ev or raw[:80]
    bound = ""
    if mn is not None and mn == mx:
        if re.search(r"以上|＋|より厚", near):
            mx, bound = None, "以上（下限のみ）"
        elif re.search(r"以下|未満|より薄", near):
            mn, bound = None, "以下（上限のみ）"
        elif re.search(r"最大", near):
            mn, bound = None, "最大値"
        elif re.search(r"最小", near):
            mx, bound = None, "最小値"

    src = _src(unit, th.get("source_block_index"), raw, pdf_index)
    conf = th.get("confidence")
    parts = []
    if ev:
        parts.append(f"「{ev}」" + (f"＝{bound}" if bound else ""))
    if raw:
        parts.append(raw)
    tail = f"（{src}" + (f" / 確信度{conf}" if conf is not None else "") + "）"
    return {"min_m": mn, "max_m": mx, "bound": bound,
            "approx": bool(th.get("approx")),
            "text": (" ".join(parts) + tail).strip()}


_NUM_M = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[〜~～\-–—]\s*(\d+(?:\.\d+)?)\s*)?[mｍ](?![mｍ])")


def _fmt_range(lo, hi):
    if lo is None and hi is None:
        return "不明"
    if lo is None:
        return f"〜{hi:g} m"
    if hi is None:
        return f"{lo:g} m〜"
    return f"{lo:g} m" if lo == hi else f"{lo:g}–{hi:g} m"


def best_thickness(unit):
    """
    min_thickness / max_thickness に入れる値を決める。

    1. 「分布及び層厚」節から読めればそれ（本文が層厚として述べている値）
    2. 読めなければ何も入れない
       （GSJ の derived は下位の1枚を指していることが多く、入れると誤りになる）
    """
    sec = thickness_from_section(unit)
    if not sec:
        return None, None
    return sec["min_m"], sec["max_m"]
_QUAL = (("以上", "以上"), ("以下", "以下"), ("未満", "未満"), ("最大", "最大"),
         ("最小", "最小"), ("前後", "前後"), ("程度", "程度"), ("約", "約"))


# 「分布及び層厚」「層厚」など、層厚そのものを述べる節の見出し
_THICK_SECTION = re.compile(r"層厚|厚さ")
# 「層厚は…である」の形。地層そのものの厚さを述べている文を拾う。
# 「層厚10mの中粒砂層が見られる」のような下位の層の記述は、後ろに「の＋名詞」が
# 続くので除外する。
_UNIT_THICK = re.compile(
    r"(?:層厚|厚さ|全層厚)\s*(?:は|が)?\s*[^。．]{0,24}?"
    r"(\d+(?:\.\d+)?)\s*(?:[〜~～\-–—]\s*(\d+(?:\.\d+)?)\s*)?[mｍ](?![mｍ])")


def thickness_from_section(unit):
    """
    「分布及び層厚」節から地層そのものの層厚を読む。

    ★ なぜ GSJ の derived.thickness をそのまま使わないか。
      十和田図幅23層で突き合わせたところ、derived の値は **ほぼ全件で
      地層全体の層厚ではなかった**。例:

        道川層     derived 1 m   ← 本文「本層の層厚は最大で300～400mである」
        小増沢層   derived 25 m  ← 本文「層厚は最大で約300mである」
        野辺地層   derived 10 m  ← 本文「層厚は少なくとも30m以上である」

      derived は本文のどこかにある「層厚○m」を1つ拾うだけなので、
      「岩相」節に出てくる1枚の砂層の厚さを掴んでしまう。
      層厚を述べている節（分布及び層厚／層厚）に絞って読むほうが正しい。

    ★ 場所によって値が変わる場合（「高峠付近で約70m，立惣辺山付近で50～140m」）は、
      節の中の数値を全部集めて全体の下限・上限にする。variable=True を立てるので
      Column ごとに分けたいときはユーザーが判断できる。

    戻り値: {"min_m", "max_m", "bound", "variable", "sentences", "section"} / None
    """
    blocks = (unit.get("target") or {}).get("blocks") or []
    cur, hits, section = "", [], ""
    for b in blocks:
        if b.get("type") in ("section-title", "heading"):
            cur = str(b.get("text") or "")
            continue
        if not _THICK_SECTION.search(cur):
            continue
        for sent in re.split(r"[。．]", str(b.get("text") or "")):
            if not _UNIT_THICK.search(sent):
                continue
            # ★ 層厚を述べている文だと分かったら、その文の数値を全部拾う。
            #   「層厚は高峠付近で約70m，立惣辺山付近で50～140mである」のように、
            #   2つ目以降の地点には「層厚」の語が付かないため、
            #   語に紐づく形だけを拾うと 50–140 を取りこぼす（実際に取りこぼした）。
            for m in _NUM_M.finditer(sent):
                # 「層厚10mの砂層」のように直後が「の＋名詞」なら下位の層の話
                if sent[m.end():m.end() + 1] == "の":
                    continue
                lo = float(m.group(1))
                hi = float(m.group(2)) if m.group(2) else None
                near = sent[max(0, m.start() - 16):m.end() + 8]
                hits.append({"lo": lo, "hi": hi, "sent": sent.strip(), "near": near})
                section = section or cur
    if not hits:
        return None

    nums = [v for h in hits for v in (h["lo"], h["hi"]) if v is not None]
    only_upper = all(re.search(r"最大|以下|未満", h["near"]) for h in hits)
    only_lower = all(re.search(r"以上|より厚", h["near"]) for h in hits)

    min_m = None if only_upper else min(nums)
    max_m = None if only_lower else max(nums)
    bound = ("最大値のみ" if only_upper else
             "以上（下限のみ）" if only_lower else "")
    variable = len(hits) > 1 and len(set(nums)) > 1
    if variable:
        bound = (bound + "／場所により変動").lstrip("／")
    return {"min_m": min_m, "max_m": max_m, "bound": bound, "variable": variable,
            "sentences": list(dict.fromkeys(h["sent"] for h in hits)),
            "section": section}


def thickness_block(unit, pdf_index=None):
    """
    REF_thickness に入れる文字列を丸ごと作る。make と migrate の両方から使う。

    構成:
      【候補】      文中の数値を拾って並べたもの（一目で見るため）
      【GSJ抽出】   GSJ が構造化した値＋該当箇所＋出典ページ
      【本文】      層厚に触れた文を **全部**。1文ごとに出典ページを付ける

    ★ 数値は自動では min/max に入れない。「泥層はまれに挟在され，層厚は1m以下」
      のように、地層そのものではなく挟み層の厚さを述べている文があるため。
      どの数値がどの地点・どの部分の話かはPDFを見ないと決められない。
      だからページ番号を必ず添えて、判断を早くすることに徹する。
    """
    from common import extract_thickness_notes
    from pdf_locate import cite

    desc = (unit.get("target") or {}).get("text") or ""
    notes = extract_thickness_notes(desc, max_items=30)
    th = thickness(unit, pdf_index)

    parts = []

    # 【採用】層厚の節から読んだ値。min_thickness / max_thickness に入るのはこれ。
    sec = thickness_from_section(unit)
    if sec:
        rng = _fmt_range(sec["min_m"], sec["max_m"])
        parts.append(f"【採用 {rng}{'／' + sec['bound'] if sec['bound'] else ''}】"
                     f"「{sec['section']}」節より: "
                     + " ／ ".join(s for s in sec["sentences"]))

    # 【候補】文中の数値を一望できるように並べる
    cands = []
    for n in notes:
        for m in _NUM_M.finditer(n):
            lo, hi = m.group(1), m.group(2)
            around = n[max(0, m.start() - 12):m.end() + 6]
            q = next((label for word, label in _QUAL if word in around), "")
            v = f"{lo}–{hi} m" if hi else f"{lo} m"
            item = f"{v}{q}"
            if item not in cands:
                cands.append(item)
    if cands:
        parts.append("【本文中の数値】" + " / ".join(cands))

    if th:
        rng = _fmt_range(th["min_m"], th["max_m"])
        parts.append(f"【GSJ自動抽出 {rng}"
                     f"{'／' + th['bound'] if th['bound'] else ''}｜"
                     f"★地層全体ではなく下位の1枚を指していることが多い。参考程度に】"
                     + th["text"])

    # 【本文】1文ごとに出典ページを付ける
    if notes:
        with_src = []
        target = unit.get("target") or {}
        for n in notes:
            src = cite(pdf_index, n, target.get("label"), None)
            with_src.append(f"{n}（{src}）" if src else n)
        parts.append("【本文の層厚記述】" + " ／ ".join(with_src))

    return "  ".join(parts)


def lithologies(unit, pdf_index=None):
    """
    岩相。本文の卓越・従属表現から (主, 副, 不明) へ振り分ける。

    明示的な主従表現を最優先する。表現が無い GSJ derived 候補は、提案仕様に
    従って confidence 順（同点は原文順）の先頭を主岩相、2番手以降を副次岩相
    とする。フォールバックを使った候補は role_cue に残るため監査できる。
    """
    dv = unit.get("derived") or {}
    items = _lithology_section_items(unit, pdf_index)
    for it in (dv.get("lithology") or []):
        term = _clean_lith(it.get("term_en"), it.get("term_jp"))
        if not term:
            continue
        ev = it.get("evidence") or {}
        role, role_cue = lithology_role_from_context(
            term, it.get("term_jp") or "", ev.get("snippet") or ""
        )
        items.append({
            "term": term,
            "term_jp": it.get("term_jp") or "",
            "confidence": it.get("confidence") or 0,
            "snippet": ev.get("snippet") or "",
            "src": _src(unit, ev.get("block_index"), ev.get("snippet") or "", pdf_index),
            "role": role,
            "role_cue": role_cue,
        })
    if not items:
        return None
    for source_order, item in enumerate(items):
        item["source_order"] = source_order
    items.sort(key=lambda x: (-x["confidence"], x["source_order"]))
    major = [i for i in items if i["role"] == "major"]
    minor = [i for i in items if i["role"] == "minor"]
    unknown = [i for i in items if i["role"] == "unknown"]

    assigned_terms = {i["term"] for i in major + minor}
    ranked_unknown = []
    for item in unknown:
        if item["term"] not in assigned_terms:
            ranked_unknown.append(item)
            assigned_terms.add(item["term"])
    if not major and ranked_unknown:
        promoted = ranked_unknown.pop(0)
        promoted["role"] = "major"
        promoted["role_cue"] = "highest-confidence fallback"
        major.append(promoted)
    for promoted in ranked_unknown:
        promoted["role"] = "minor"
        promoted["role_cue"] = "secondary-confidence fallback"
        minor.append(promoted)

    if not minor and len(major) > 1:
        # Explicit wording can establish that several terms belong to the
        # unit without giving a relative abundance between them (for example
        # ``basalt to andesite lava``).  The 100%-fill proposal defines the
        # ranked first term as primary and later terms as secondary.
        leading, *secondary = major
        major = [leading]
        for promoted in secondary:
            promoted["role"] = "minor"
            promoted["role_cue"] = "secondary-ranked fallback"
            minor.append(promoted)

    def without_shadowed_bases(group):
        terms = {item["term"] for item in group}
        return [
            item for item in group
            if not any(other != item["term"] and other.endswith(" " + item["term"])
                       for other in terms)
        ]

    major = without_shadowed_bases(major)
    minor = without_shadowed_bases(minor)

    major_terms = {i["term"] for i in major}
    minor_terms = {i["term"] for i in minor}
    overlaps = major_terms.intersection(minor_terms)
    if overlaps:
        # A term explicitly classified as dominant remains major.  Duplicate
        # subordinate hits are retained only as provenance, not as a value.
        unknown.extend(i for i in minor if i["term"] in overlaps)
        minor = [i for i in minor if i["term"] not in overlaps]

    def fmt(group):
        if not group:
            return ""
        terms = []
        for i in group:
            if i["term"] not in terms:
                terms.append(i["term"])
        srcs = []
        for i in group:
            s = f"{i['term_jp']}→{i['term']}"
            if i["snippet"]:
                s += f"「…{i['snippet'][:40]}…」"
            if i.get("role_cue"):
                s += f" [判定: {i['role_cue']}]"
            if i["src"]:
                s += f" {i['src']}"
            if s not in srcs:
                srcs.append(s)
        return "; ".join(terms) + "（出典: " + " / ".join(srcs) + "）"

    return {"major": fmt(major), "minor": fmt(minor), "unknown": fmt(unknown),
            "major_terms": "; ".join(dict.fromkeys(i["term"] for i in major)),
            "minor_terms": "; ".join(dict.fromkeys(i["term"] for i in minor)),
            "unknown_terms": "; ".join(dict.fromkeys(i["term"] for i in unknown)),
            "role_conflicts": sorted(overlaps),
            "items": items}


def basal_surface(unit, pdf_index=None):
    """基底面の関係。derived.contacts の type をそのまま使う。"""
    dv = unit.get("derived") or {}
    out, seen = [], []
    for c in (dv.get("contacts") or []):
        t = _CONTACT_MAP.get(str(c.get("type") or "").strip().lower())
        if not t or t in seen:
            continue
        seen.append(t)
        ev = (c.get("evidence") or {}).get("text") or ""
        src = _src(unit, c.get("source_block_index"), c.get("text") or ev, pdf_index)
        with_u = c.get("with_unit_name") or ""
        bit = t
        detail = " ".join(x for x in (f"{c.get('type_jp') or ''}", with_u,
                                      f"「{ev}」" if ev else "") if x).strip()
        out.append((bit, f"{detail} {src}".strip()))
    if not out:
        return None
    return {"value": "; ".join(b for b, _ in out),
            "text": "; ".join(b for b, _ in out) + "（出典: "
                    + " / ".join(d for _, d in out) + "）"}


def strat_name(unit):
    """
    層序名。ZFK凡例の階層をそのまま連ねる。

    凡例の構造:  legend_group（図幅凡例）> parent_facies（地層名）> focus（岩相）
    Macrostrat の strat_name は「子, 親」の順にカンマで連ねる決まりなので
    parent_facies から上へ辿る。

    ★ 「図幅凡例」のような容れ物の名前は層序名ではないので落とす。
    """
    lg = unit.get("legend") or {}
    pf = lg.get("parent_facies") or {}
    chain = []
    en = pf.get("label_en") or pf.get("text_en") or ""
    if en:
        chain.append(" ".join(str(en).split()))
    grp = lg.get("legend_group") or {}
    gen = grp.get("label_en") or ""
    if gen and gen.lower() not in ("map legend", "図幅凡例", ""):
        chain.append(" ".join(str(gen).split()))
    if not chain:
        return None
    return ", ".join(dict.fromkeys(chain))


def section_info(unit):
    """節番号と小見出し一覧。REF_desc の頭に付けて、どこを見ればいいか示す。"""
    t = unit.get("target") or {}
    titles = [str(b.get("text") or "").strip()
              for b in (t.get("blocks") or [])
              if b.get("type") == "section-title"]
    return {"label": t.get("label") or "", "title": t.get("title") or "",
            "sections": [x for x in titles if x],
            "html_url": ((unit.get("provenance") or {}).get("html_url") or ""),
            "sec_id": t.get("sec_id") or ""}


def describe_source(unit, pdf_index=None):
    """REF_desc の先頭に置く出典行。"""
    si = section_info(unit)
    bits = []
    if si["label"]:
        bits.append(f"§{si['label']} {si['title']}".strip())
    text = (unit.get("target") or {}).get("text") or ""
    from pdf_locate import locate
    hit = locate(pdf_index, re.sub(r"^\S+\s+\S+\s+", "", text)[:80]) or locate(pdf_index, text)
    if hit:
        p = f"PDF p.{hit['pdf_page']}"
        if hit["printed_page"] is not None:
            p += f"（印刷 p.{hit['printed_page']}）"
        bits.append(p)
    if si["sections"]:
        bits.append("小見出し: " + " / ".join(si["sections"]))
    return " ｜ ".join(bits)
