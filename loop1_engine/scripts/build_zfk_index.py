# -*- coding: utf-8 -*-
"""
build_zfk_index.py — ZFKデータが存在する図幅の索引を作る

GSJ の 763 枚の5万分の1図幅のうち、ZFK（地質図幅凡例データセット）が
整備されているのは一部だけ。どの図幅にデータがあるかを一覧化して
config/zfk_index.json に保存する。

  https://gbank.gsj.jp/ld/resource/zfk/maps.json          <- ZFKがある図幅のリスト
  https://gbank.gsj.jp/ld/resource/zfk/maps/m{id}.json    <- 図幅コード・出版年・座標
  https://gbank.gsj.jp/ld/resource/zfk/query/unitsInMap   <- 層数

使い方:
  python run.py index          # 索引を作成／更新
  python run.py index --force  # キャッシュを無視して全件取り直す
"""

import argparse
import concurrent.futures
import json
import os
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    get_region_folder,
    load_json,
    normalize_sheet_code,
)

UA = {"User-Agent": "Mozilla/5.0"}
INDEX_PATH = os.path.join("config", "zfk_index.json")
MAPS_URL = "https://gbank.gsj.jp/ld/resource/zfk/maps.json"


def fetch_json(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def fetch_map_detail(map_id, use_cache=True):
    """1図幅ぶんの情報を集める。ローカルキャッシュがあればAPIを叩かない。"""
    cache = os.path.join("data", "raw", "zfk", f"m{map_id}", "map.json")
    data = load_json(cache) if use_cache and os.path.exists(cache) else None
    if not data:
        data = fetch_json(f"https://gbank.gsj.jp/ld/resource/zfk/maps/m{map_id}.json") or {}

    mm = data.get("map", {})
    centroid = (data.get("geom") or {}).get("centroid") or {}
    sheet_code = normalize_sheet_code(mm.get("sheet_code", ""))

    # 層数。unitsInMap のキャッシュがあればそれを使う。
    n_units = None
    idx_cache = os.path.join("data", "raw", "zfk", f"m{map_id}", "units-index.json")
    if use_cache and os.path.exists(idx_cache):
        ui = load_json(idx_cache)
        units = (ui.get("result", {}) or {}).get("units") or ui.get("units") or []
        if isinstance(units, list) and units:
            n_units = len(units)
    if n_units is None:
        ui = fetch_json(
            f"https://gbank.gsj.jp/ld/resource/zfk/query/unitsInMap?map_id={map_id}", timeout=20
        )
        if ui:
            n_units = len((ui.get("result", {}) or {}).get("units") or [])

    authors = [a.get("name_en") or a.get("name_ja") or "" for a in mm.get("authors", [])]
    return {
        "map_id": str(map_id),
        "title_ja": mm.get("title_ja") or "",
        "title_en": mm.get("title_en") or "",
        "sheet_code": sheet_code,
        "region_code": sheet_code[:2] if len(sheet_code) >= 2 else "",
        "region_folder": get_region_folder(sheet_code),
        "pub_year": mm.get("pub_year") or "",
        "n_units": n_units if n_units is not None else "",
        "lat": centroid.get("lat", ""),
        "lng": centroid.get("lon", ""),
        "authors": "; ".join(a for a in authors if a),
    }


def build(force=False, workers=8):
    print("ZFKデータのある図幅リストを取得中 ...")
    payload = fetch_json(MAPS_URL, timeout=60)
    if not payload:
        print(f"[ERROR] {MAPS_URL} を取得できませんでした。ネットワークを確認してください。")
        return None

    maps = payload.get("maps") or payload.get("result", {}).get("maps") or []
    if not maps:
        print("[ERROR] 図幅リストが空です。APIの仕様が変わった可能性があります。")
        print(f"        レスポンスのキー: {list(payload)[:10]}")
        return None

    ids = []
    for m in maps:
        mid = m.get("id") or m.get("map_id")
        if mid is None:
            continue
        mid = str(mid).lstrip("m")
        if mid.isdigit():
            ids.append(mid)
    print(f"  {len(ids)} 枚に ZFK データがあります（全763図幅中）。詳細を取得します ...")

    rows = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_map_detail, i, not force): i for i in ids}
        for fut in concurrent.futures.as_completed(futures):
            try:
                rows.append(fut.result())
            except Exception as e:
                print(f"  [warn] map {futures[fut]}: {e}")
            done += 1
            if done % 20 == 0 or done == len(ids):
                print(f"    {done}/{len(ids)} 完了")

    rows.sort(key=lambda r: (r["region_code"] or "zz", r["sheet_code"] or "", r["map_id"]))

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": MAPS_URL,
        "total_zfk_maps": len(rows),
        "maps": rows,
    }
    os.makedirs("config", exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"\n索引を保存しました: {INDEX_PATH}")

    by_region = {}
    for r in rows:
        by_region.setdefault(r["region_folder"], []).append(r)
    print(f"\n地域別の内訳（全 {len(rows)} 枚）:")
    for folder in sorted(by_region):
        grp = by_region[folder]
        units = sum(int(g["n_units"]) for g in grp if str(g["n_units"]).isdigit())
        print(f"  {folder:<18} {len(grp):>3} 枚   計 {units:>5} 層")
    missing = [r["map_id"] for r in rows if not r["sheet_code"]]
    if missing:
        print(f"\n[warn] 図幅コードを取得できなかった図幅: {missing[:10]}"
              f"{' ...' if len(missing) > 10 else ''}")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="ZFKデータのある図幅の索引を作成")
    p.add_argument("--force", action="store_true", help="ローカルキャッシュを無視して取り直す")
    p.add_argument("--workers", type=int, default=8, help="並列取得数（既定8）")
    a = p.parse_args()
    sys.exit(0 if build(force=a.force, workers=a.workers) else 1)
