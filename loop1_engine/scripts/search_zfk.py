# -*- coding: utf-8 -*-
"""
search_zfk.py — ZFKデータのある図幅を検索する

  python run.py search aomori     地域で検索（ローマ字・漢字・コードのいずれも可）
  python run.py search 十和田      図幅名で検索
  python run.py search            全件を地域別に表示

「地域」は GSJ の図幅区画であって都道府県境ではない。
例えば 05_青森 には岩手県北部の図幅（一戸など）も含まれる。
"""

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    REGION_ALIASES,
    REGION_MAP,
    canonical_map_title,
    disp_width,
    load_json,
    pad,
    region_label,
    resolve_region,
)


def display_name(row):
    """表示名をフォルダ名と同じ規則に揃える（十和田地域の地質 -> 十和田 2005）。

    索引にはGSJの生データを保存しておき、見せるときだけ正規化する。
    索引を作り直さなくても表記が揃う。
    """
    return (canonical_map_title(pub_title="", zfk_title=row.get("title_ja", ""),
                                pub_year=row.get("pub_year", ""))
            or row.get("title_ja") or row.get("title_en") or "")

INDEX_PATH = os.path.join("config", "zfk_index.json")


def load_index():
    if not os.path.exists(INDEX_PATH):
        print(f"索引がまだありません: {INDEX_PATH}")
        print("  先に `python run.py index` を実行してください（数分かかります）。")
        return None
    data = load_json(INDEX_PATH)
    if not data.get("maps"):
        print(f"[ERROR] 索引が空です: {INDEX_PATH}")
        print("  `python run.py index --force` で作り直してください。")
        return None
    return data


def review_status():
    """50k/02_review にレビューファイルがある map_id の集合と、提出済みの集合を返す。"""
    started, submitted = set(), set()
    for p in glob.glob(os.path.join("data", "50k", "02_review", "**", "*_review.xlsx"), recursive=True):
        base = os.path.basename(p)
        if base.startswith("~$") or ".bak_" in base:
            continue
        mid = base.split("_")[0].lstrip("m")
        if mid.isdigit():
            started.add(mid)
            sub_dir = os.path.dirname(p).replace(
                os.path.join("data", "50k", "02_review"), os.path.join("data", "50k", "03_submission"))
            if glob.glob(os.path.join(sub_dir, "*.xlsx")):
                submitted.add(mid)
    return started, submitted


def match(rows, query):
    """(絞り込んだ行, 検索の説明) を返す。"""
    if not query:
        return rows, "全件"

    code = resolve_region(query)
    if code:
        hit = [r for r in rows if r.get("region_code") == code]
        if hit:
            return hit, f"地域 {region_label(code)}"

    q = str(query).strip().lower()
    hit = [r for r in rows
           if q in display_name(r).lower()
           or q in str(r.get("title_ja", "")).lower()
           or q in str(r.get("title_en", "")).lower()
           or q == str(r.get("map_id", ""))
           or q in str(r.get("sheet_code", ""))
           or q in str(r.get("authors", "")).lower()]
    return hit, f"'{query}' に一致"


def show(rows, header, started, submitted, show_coords=False):
    if not rows:
        print(f"\n{header}: 該当なし\n")
        return

    print(f"\n{header}  ({len(rows)} 枚)\n")
    cols = (pad("ID", 6, "right") + "  " + pad("図幅名", 18) + pad("コード", 9)
            + pad("年", 6) + pad("層数", 5, "right") + "  " + pad("状況", 10))
    if show_coords:
        cols += pad("緯度", 11, "right") + pad("経度", 12, "right")
    print(cols)
    width = disp_width(cols)
    print("-" * width)

    by_region = {}
    for r in rows:
        by_region.setdefault(r.get("region_folder", "不明"), []).append(r)

    total_units = 0
    for folder in sorted(by_region):
        grp = by_region[folder]
        if len(by_region) > 1:
            print(f"\n[{folder}]")
        for r in sorted(grp, key=lambda x: str(x.get("sheet_code", ""))):
            mid = str(r.get("map_id", ""))
            n = r.get("n_units", "")
            if str(n).isdigit():
                total_units += int(n)
            if mid in submitted:
                mark = "提出済み"
            elif mid in started:
                mark = "作業中"
            else:
                mark = "-"
            title = display_name(r)
            line = (pad(mid, 6, "right") + "  " + pad(title, 18)
                    + pad(r.get("sheet_code", ""), 9) + pad(r.get("pub_year", ""), 6)
                    + pad(n, 5, "right") + "  " + pad(mark, 10))
            if show_coords:
                line += (pad(str(r.get("lat", ""))[:9], 11, "right")
                         + pad(str(r.get("lng", ""))[:10], 12, "right"))
            print(line.rstrip())

    print("-" * width)
    # 3つの状態は排他にする（提出済みを作業中に二重計上しない）
    n_sub = sum(1 for r in rows if str(r.get("map_id")) in submitted)
    n_wip = sum(1 for r in rows
                if str(r.get("map_id")) in started and str(r.get("map_id")) not in submitted)
    n_todo = len(rows) - n_sub - n_wip
    print(f"{len(rows)} 枚 / 計 {total_units} 層 / "
          f"未着手 {n_todo} 枚 ・ 作業中 {n_wip} 枚 ・ 提出済み {n_sub} 枚")
    print("\n着手するには: python run.py make <図幅ID または 図幅名>\n")


def summary(rows, started, submitted):
    """地域別の概況。検索語なしのとき先に出す。"""
    by_code = {}
    for r in rows:
        by_code.setdefault(r.get("region_code", ""), []).append(r)
    print(f"\nZFKデータのある図幅: {len(rows)} 枚（全763図幅中）\n")
    head = (pad("地域", 20) + pad("枚数", 6, "right") + pad("層数", 8, "right")
            + pad("作業中", 8, "right") + pad("提出済", 8, "right") + "   " + "検索キーワード")
    print(head)
    print("-" * disp_width(head))
    for code in sorted(by_code):
        grp = by_code[code]
        units = sum(int(g["n_units"]) for g in grp if str(g["n_units"]).isdigit())
        nb = sum(1 for g in grp if str(g["map_id"]) in submitted)
        ns = sum(1 for g in grp
                 if str(g["map_id"]) in started and str(g["map_id"]) not in submitted)
        folder = REGION_MAP.get(code, f"Region_{code}" if code else "不明")
        romaji = [a for a in REGION_ALIASES.get(code, []) if a.isascii()]
        kw = " / ".join(romaji[:2]) if romaji else ""
        print(pad(folder, 20) + pad(len(grp), 6, "right") + pad(units, 8, "right")
              + pad(ns, 8, "right") + pad(nb, 8, "right") + "   " + kw)
    print("-" * disp_width(head))
    print("\n地域を絞るには: python run.py search aomori\n")


def run(query=None, show_coords=False, show_all=False):
    data = load_index()
    if not data:
        return False
    rows = data["maps"]
    started, submitted = review_status()

    if not query:
        summary(rows, started, submitted)
        if show_all:
            show(rows, "全図幅", started, submitted, show_coords)
        return True

    hit, header = match(rows, query)
    if not hit:
        print(f"\n'{query}' に一致する図幅（ZFKデータあり）はありません。")
        code = resolve_region(query)
        if code:
            print(f"  地域 {region_label(code)} は認識できましたが、"
                  f"この地域にZFKデータのある図幅がありません。")
        else:
            print("  地域名で探す場合は aomori / 青森 / 05 のように指定してください。")
        print("  `python run.py search` で全体の一覧を見られます。\n")
        return True
    show(hit, header, started, submitted, show_coords)
    return True


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="ZFKデータのある図幅を検索")
    p.add_argument("query", nargs="?", help="地域名・図幅名・図幅ID・図幅コード")
    p.add_argument("--coords", action="store_true", help="中心座標も表示")
    p.add_argument("--all", action="store_true", help="概況のあと全件も表示")
    a = p.parse_args()
    sys.exit(0 if run(a.query, a.coords, a.all) else 1)
