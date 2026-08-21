# -*- coding: utf-8 -*-
"""GSJ 5万分の1図幅の正規グリッド幾何を、キャッシュ済み公式データだけから導出する。

背景
----
GSJ の 5万分の1地質図幅は、日本の地形図体系と同じ**経緯度グリッド**に載っている。
1 図幅 = 緯度 10 分 × 経度 15 分（20万分の1図幅 1° × 40′ をちょうど 4 × 4 に分割）。
グリッドの原点・格子線は **旧日本測地系（Tokyo Datum）** で定義されている。

一方 GSJ 出版物 API（``data/50k/raw/publication/g050/m*.json``）が返す
断面線 GeoJSON は **WGS84** である。両者の差（約 −0.0036° 経度 / +0.0026° 緯度）を
補正して初めて、図幅の四隅がグリッド線と一致する。

    一戸図幅 (m1286) の断面線西端 = 141.24641°E (WGS84)
    Tokyo Datum 141.25°E を WGS84 に変換 = 141.24645°E   ← 0.00004° で一致

本モジュールはこの性質を使い、各図幅について
「断面線が載っているグリッドセル」を一意に決定する。推測は一切しない。

導出根拠は図幅ごとに ``geometry_source`` として記録し、
交差検証（ZFK 重心・区画内番号の単調性）の結果も出力に残す。

出力: ``config/gsj_50k_grid.json``

    python scripts/sheet_geometry.py            # 生成して要約を表示
    python scripts/sheet_geometry.py --check    # 生成せず検証だけ
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
PUB_DIR = (ROOT / "loop2_governance" / "data" / "50k" / "raw" / "publication" / "g050") if (ROOT / "loop2_governance" / "data" / "50k" / "raw" / "publication" / "g050").is_dir() else (ROOT / "data" / "50k" / "raw" / "publication" / "g050")
CATALOG = (ROOT / "loop2_governance" / "data" / "50k" / "gsj_50k_catalog.json") if (ROOT / "loop2_governance" / "data" / "50k" / "gsj_50k_catalog.json").is_file() else (ROOT / "data" / "50k" / "gsj_50k_catalog.json")
ZFK_INDEX = (ROOT / "loop2_governance" / "config" / "zfk_index.json") if (ROOT / "loop2_governance" / "config" / "zfk_index.json").is_file() else (ROOT / "config" / "zfk_index.json")
OUT_JSON = (ROOT / "loop2_governance" / "config" / "gsj_50k_grid.json") if (ROOT / "loop2_governance" / "config").is_dir() else (ROOT / "config" / "gsj_50k_grid.json")

# 1 図幅の寸法（旧日本測地系のグリッド上）
SHEET_DLAT = 10.0 / 60.0          # 10 分
SHEET_DLON = 15.0 / 60.0          # 15 分
# 20万分の1図幅 = 50k を 4 × 4 束ねたもの
PARENT_ROWS = 4
PARENT_COLS = 4

# ZFK 重心とセル中心のずれの許容量（測地系差 + 丸めを吸収する）
CENTROID_TOLERANCE_DEG = 0.02

TMS_RE = re.compile(r"G50_(\d{2})_((?:\d{3})+)")


# ---------------------------------------------------------------------------
# 測地系変換（Molodensky 近似・国土地理院系の慣用式）
#
# 数十 m 級の近似だが、図幅の格子（10 分 = 約 18 km）の判定には 3 桁以上の余裕がある。
# 逆変換との往復誤差は tests/test_sheet_geometry.py で 1e-6 度以内を確認している。
# ---------------------------------------------------------------------------

def tokyo_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    """旧日本測地系 (Tokyo Datum) の緯度経度を WGS84 に変換する。"""
    return (
        lat - 0.00010695 * lat + 0.000017464 * lon + 0.0046017,
        lon - 0.000046038 * lat - 0.000083043 * lon + 0.010040,
    )


def wgs84_to_tokyo(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 の緯度経度を旧日本測地系 (Tokyo Datum) に変換する。"""
    return (
        lat + 0.000106961 * lat - 0.000017467 * lon - 0.004602017,
        lon + 0.000046047 * lat + 0.000083049 * lon - 0.010041046,
    )


# ---------------------------------------------------------------------------
# グリッド演算
# ---------------------------------------------------------------------------

def cell_of_wgs84(lat: float, lon: float) -> tuple[int, int]:
    """WGS84 の 1 点が載る 50k グリッドセル (row, col) を返す。

    row は南から北へ増える（緯度 10 分単位）。col は西から東へ増える（経度 15 分単位）。
    """
    t_lat, t_lon = wgs84_to_tokyo(lat, lon)
    return (math.floor(t_lat / SHEET_DLAT), math.floor(t_lon / SHEET_DLON))


def cell_bounds_tokyo(row: int, col: int) -> tuple[float, float, float, float]:
    """セルの (南, 西, 北, 東) を旧日本測地系で返す。"""
    return (row * SHEET_DLAT, col * SHEET_DLON,
            (row + 1) * SHEET_DLAT, (col + 1) * SHEET_DLON)


def cell_bounds_wgs84(row: int, col: int) -> tuple[float, float, float, float]:
    """セルの (南, 西, 北, 東) を WGS84 で返す。四隅を個別に変換して外接をとる。"""
    s, w, n, e = cell_bounds_tokyo(row, col)
    corners = [tokyo_to_wgs84(lat, lon) for lat in (s, n) for lon in (w, e)]
    lats = [c[0] for c in corners]
    lons = [c[1] for c in corners]
    return (min(lats), min(lons), max(lats), max(lons))


def cell_center_wgs84(row: int, col: int) -> tuple[float, float]:
    s, w, n, e = cell_bounds_wgs84(row, col)
    return ((s + n) / 2.0, (w + e) / 2.0)


def parent_200k_cell(row: int, col: int) -> tuple[int, int]:
    """この 50k セルを含む 20万分の1図幅セル (row, col) を返す。"""
    return (math.floor(row / PARENT_ROWS), math.floor(col / PARENT_COLS))


def parent_200k_bounds_wgs84(prow: int, pcol: int) -> tuple[float, float, float, float]:
    s, w, _, _ = cell_bounds_wgs84(prow * PARENT_ROWS, pcol * PARENT_COLS)
    _, _, n, e = cell_bounds_wgs84(prow * PARENT_ROWS + PARENT_ROWS - 1,
                                   pcol * PARENT_COLS + PARENT_COLS - 1)
    return (s, w, n, e)


# ---------------------------------------------------------------------------
# 公式データの読み出し
# ---------------------------------------------------------------------------

def _iter_coords(node: Any) -> Iterable[tuple[float, float]]:
    """GeoJSON 断片から (lon, lat) の座標を再帰的に取り出す。"""
    if isinstance(node, dict):
        if node.get("type") == "Feature":
            yield from _iter_coords(node.get("geometry"))
        elif "coordinates" in node:
            yield from _iter_coords(node["coordinates"])
        else:
            for value in node.values():
                yield from _iter_coords(value)
    elif isinstance(node, list):
        if len(node) >= 2 and all(isinstance(x, (int, float)) for x in node[:2]):
            yield (float(node[0]), float(node[1]))
        else:
            for value in node:
                yield from _iter_coords(value)


def read_publication(path: Path) -> dict[str, Any] | None:
    """出版物 JSON 1 件から、図幅コードと断面線の座標範囲を取り出す。"""
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    map_id = str(data.get("map_id") or "").strip()
    if not map_id:
        match = re.match(r"m(\d+)\.json$", path.name)
        map_id = match.group(1) if match else ""
    if not map_id:
        return None

    tms_dir = ""
    coords: list[tuple[float, float]] = []
    for page in data.get("page") or []:
        if not isinstance(page, dict):
            continue
        tms_dir = tms_dir or str(page.get("tms_dir") or "")
        for section in page.get("section") or []:
            raw = section.get("geojson") if isinstance(section, dict) else None
            if not raw:
                continue
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError):
                continue
            coords.extend(_iter_coords(parsed))

    match = TMS_RE.match(tms_dir)
    region_code = match.group(1) if match else ""
    digits = match.group(2) if match else ""
    numbers = [digits[i:i + 3] for i in range(0, len(digits), 3)]
    sheet_codes = [region_code + n for n in numbers] if region_code else []

    bbox = None
    if coords:
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        bbox = (min(lats), min(lons), max(lats), max(lons))

    # 断面線は図幅を横切るので、点の大半は図幅本体のセルに落ちる。
    # 端点が隣接セルにはみ出しても、多数決なら影響を受けない。
    cell_votes: dict[tuple[int, int], int] = {}
    for lon, lat in coords:
        key = cell_of_wgs84(lat, lon)
        cell_votes[key] = cell_votes.get(key, 0) + 1

    median = None
    if coords:
        lons_sorted = sorted(c[0] for c in coords)
        lats_sorted = sorted(c[1] for c in coords)
        middle = len(coords) // 2
        median = (lats_sorted[middle], lons_sorted[middle])

    return {
        "cell_votes": cell_votes,
        "section_median": median,
        "map_id": map_id,
        "region_code": region_code,
        "sheet_numbers": [int(n) for n in numbers] if numbers else [],
        "sheet_codes": sheet_codes,
        "tms_dir": tms_dir,
        "title_ja": str(data.get("title_j") or ""),
        "title_en": str(data.get("title_e") or ""),
        "pub_year": data.get("pub_year"),
        "authors_ja": str(data.get("authors_j") or ""),
        "viewer_url": str(data.get("viewer_url") or ""),
        "section_bbox": bbox,
        "section_points": len(coords),
    }


def load_publications(pub_dir: Path = PUB_DIR) -> list[dict[str, Any]]:
    if not pub_dir.is_dir():
        return []
    records = []
    for path in sorted(pub_dir.glob("m*.json")):
        record = read_publication(path)
        if record:
            records.append(record)
    return records


def load_region_names(catalog: Path = CATALOG) -> dict[str, str]:
    try:
        with catalog.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    summary = data.get("region_summary") or {}
    return {str(key).zfill(2): str(value.get("name") or "")
            for key, value in summary.items() if isinstance(value, dict)}


def load_zfk_centroids(index: Path = ZFK_INDEX) -> dict[str, tuple[float, float]]:
    try:
        with index.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    rows = data.get("maps", []) if isinstance(data, dict) else data
    out: dict[str, tuple[float, float]] = {}
    for row in rows or []:
        code = str(row.get("sheet_code") or "").strip()
        lat, lon = row.get("lat"), row.get("lng")
        if len(code) == 5 and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            out[code] = (float(lat), float(lon))
    return out


# ---------------------------------------------------------------------------
# 図幅コード -> セルの決定
# ---------------------------------------------------------------------------

def _order_key(cell: tuple[int, int]) -> tuple[int, int]:
    """区画内の図幅番号は北西から東へ、次いで南へ増える。その並び順のキー。"""
    return (-cell[0], cell[1])


def assign_cells(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """図幅コード -> セル割り当てを作る。

    根拠の強さの順に確定させ、どの段で確定したかを必ず記録する。
      1. 単一図幅の刊行物 …… 断面線群の外接矩形の中心が載るセル
      2. 区画内番号の補間 …… 前後の既知図幅が同一行で連続する場合のみ
      3. 合併図幅の刊行物 …… 票が十分に集まったセルの数が未確定図幅数と一致する場合のみ
      4. ZFK 重心 …………… 断面線を持たない図幅の最終手段
    いずれにも当たらない図幅は割り当てない（推測で埋めない）。
    第2段以降は、既に他図幅が載っているセルへの割り当てを禁止する。
    """
    assigned: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    # 合併図幅で採用するセルの最低得票率。断面線の端点が隣接セルへ数点はみ出す
    # 程度では採用されず、図幅本体を実際に横切っているセルだけが残る。
    merged_vote_share = 0.15

    def put(code: str, cell: tuple[int, int], source: str, record: dict[str, Any] | None,
            votes: int = 0) -> None:
        assigned[code] = {
            "cell": cell,
            "geometry_source": source,
            "evidence_map_id": (record or {}).get("map_id", ""),
            "evidence_votes": votes,
        }

    def occupied_cells() -> set[tuple[int, int]]:
        return {tuple(info["cell"]) for info in assigned.values()}

    def interpolate_region_sequences() -> int:
        """区画内番号の単調性を使って、確実に決まる隙間だけを埋める。

        番号は北西から東へ、次いで南へ増える。前後の既知図幅が「同じ行」かつ
        「番号差 = 列差」なら、間の図幅は同じ行の連続セルに一意に定まる。
        """
        by_region: dict[str, list[int]] = {}
        for code in assigned:
            by_region.setdefault(code[:2], []).append(int(code[2:]))
        filled = 0
        for region, numbers in by_region.items():
            for left, right in zip(sorted(numbers), sorted(numbers)[1:]):
                gap = right - left
                if gap < 2:
                    continue
                cell_l = tuple(assigned[f"{region}{left:03d}"]["cell"])
                cell_r = tuple(assigned[f"{region}{right:03d}"]["cell"])
                if cell_l[0] != cell_r[0] or cell_r[1] - cell_l[1] != gap:
                    continue
                occupied = occupied_cells()
                candidates = [(f"{region}{left + step:03d}", (cell_l[0], cell_l[1] + step))
                              for step in range(1, gap)]
                if any(code in assigned or cell in occupied for code, cell in candidates):
                    continue
                for code, cell in candidates:
                    put(code, cell, "region_sequence_interpolation", None)
                    filled += 1
        return filled

    # 第1段: 1 刊行物 = 1 図幅。
    # 断面線は図幅を端から端まで横切るため、断面線群の外接矩形の中心は図幅の内側に落ちる。
    # 端点はグリッド線上にあり丸めで隣接セルへこぼれるので、端点そのものは使わない。
    for record in records:
        votes = record["cell_votes"]
        if len(record["sheet_codes"]) != 1 or not record["section_bbox"]:
            continue
        code = record["sheet_codes"][0]
        south, west, north, east = record["section_bbox"]
        cell = cell_of_wgs84((south + north) / 2.0, (west + east) / 2.0)
        extent = max(north - south, 0.0) * max(east - west, 0.0)
        count = votes.get(cell, 0)
        current = assigned.get(code)
        if current is None or extent < current.get("evidence_extent", float("inf")):
            if current is not None and tuple(current["cell"]) != cell:
                # 「A 及び B」のような合併版は断面線が隣の図幅まで伸びる。
                # 断面線の広がりが小さい版ほど図幅単体を表しているので、そちらを採る。
                notes.append(
                    f"{code}: 版間でセル判定が不一致 "
                    f"(m{current['evidence_map_id']} -> m{record['map_id']}) "
                    f"-> 断面線の広がりが小さい版を採用")
            assigned[code] = {
                "cell": cell,
                "geometry_source": "publication_section_geojson",
                "evidence_map_id": record["map_id"],
                "evidence_votes": count,
                "evidence_extent": extent,
            }

    # 第1段b: 区画内番号の単調性に反する図幅を、前後の図幅から一意に決まる場合だけ直す。
    for region in sorted({code[:2] for code in assigned}):
        numbers = sorted(int(code[2:]) for code in assigned if code[:2] == region)
        for index in range(1, len(numbers) - 1):
            code = f"{region}{numbers[index]:03d}"
            before = tuple(assigned[f"{region}{numbers[index - 1]:03d}"]["cell"])
            after = tuple(assigned[f"{region}{numbers[index + 1]:03d}"]["cell"])
            current = tuple(assigned[code]["cell"])
            if _order_key(before) < _order_key(current) < _order_key(after):
                continue
            span = numbers[index + 1] - numbers[index - 1]
            if before[0] != after[0] or after[1] - before[1] != span:
                continue
            repaired = (before[0], before[1] + numbers[index] - numbers[index - 1])
            if repaired in occupied_cells():
                continue
            notes.append(
                f"{code}: 区画内の番号順に反する位置 {current} -> 前後の図幅から "
                f"{repaired} に修正")
            put(code, repaired, "region_sequence_repair", None)

    # 第2段: 区画内番号の補間（合併図幅より先に、確実な隙間から埋める）。
    while interpolate_region_sequences():
        pass

    # 第3段: 合併図幅。票の集まったセルの数が未確定図幅数と一致するときだけ採る。
    for record in records:
        codes = record["sheet_codes"]
        votes = record["cell_votes"]
        unknown = [c for c in codes if c not in assigned]
        if len(codes) <= 1 or not votes or not unknown:
            continue
        occupied = occupied_cells()
        threshold = max(votes.values()) * merged_vote_share
        ranked = [cell for cell, count in sorted(votes.items(), key=lambda kv: -kv[1])
                  if count >= threshold and cell not in occupied]
        if len(ranked) != len(unknown):
            notes.append(
                f"m{record['map_id']} 合併図幅 {codes}: 断面線が届いた未使用セル "
                f"{len(ranked)} と未確定 {len(unknown)} 図幅が一致せず、割り当てを見送り")
            continue
        for code, cell in zip(sorted(unknown), sorted(ranked, key=_order_key)):
            put(code, cell, "publication_section_geojson_merged", record, votes.get(cell, 0))

    # 第4段: 断面線を持たない図幅を ZFK 重心で補う。
    for code, (lat, lon) in sorted(load_zfk_centroids().items()):
        cell = cell_of_wgs84(lat, lon)
        if code not in assigned and cell not in occupied_cells():
            put(code, cell, "zfk_centroid", None)

    # 新たに確定した図幅を種に、もう一度だけ補間する。
    while interpolate_region_sequences():
        pass
    return assigned, notes


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------

def validate_assignment(assigned: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """割り当てを 3 方向から検証する。返り値は機械可読な検証レポート。"""
    zfk = load_zfk_centroids()
    centroid_checked = 0
    centroid_failed: list[str] = []
    for code, info in assigned.items():
        point = zfk.get(code)
        if not point:
            continue
        centroid_checked += 1
        lat, lon = cell_center_wgs84(*info["cell"])
        if (abs(lat - point[0]) > SHEET_DLAT / 2 + CENTROID_TOLERANCE_DEG
                or abs(lon - point[1]) > SHEET_DLON / 2 + CENTROID_TOLERANCE_DEG):
            centroid_failed.append(code)

    # 同一セルに 2 図幅が載ることはない。
    seen: dict[tuple[int, int], str] = {}
    duplicates: list[tuple[str, str]] = []
    for code in sorted(assigned):
        cell = tuple(assigned[code]["cell"])
        if cell in seen:
            duplicates.append((seen[cell], code))
        else:
            seen[cell] = code

    # 区画内の図幅番号は北西から東へ、次いで南へ単調に増える。
    by_region: dict[str, list[tuple[int, tuple[int, int]]]] = {}
    for code, info in assigned.items():
        by_region.setdefault(code[:2], []).append((int(code[2:]), tuple(info["cell"])))
    monotonic_failed: list[str] = []
    for region, rows in by_region.items():
        rows.sort()
        previous = None
        for number, (row, col) in rows:
            key = (-row, col)
            if previous is not None and key <= previous:
                monotonic_failed.append(f"{region}{number:03d}")
            previous = key

    return {
        "assigned": len(assigned),
        "zfk_centroid_checked": centroid_checked,
        "zfk_centroid_failed": sorted(centroid_failed),
        "duplicate_cells": duplicates,
        "region_order_violations": sorted(monotonic_failed),
    }


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

def build_grid(pub_dir: Path = PUB_DIR) -> dict[str, Any]:
    records = load_publications(pub_dir)
    assigned, notes = assign_cells(records)
    region_names = load_region_names()

    publications: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for code in record["sheet_codes"]:
            publications.setdefault(code, []).append(record)

    sheets = []
    for code in sorted(assigned):
        info = assigned[code]
        row, col = info["cell"]
        south, west, north, east = cell_bounds_wgs84(row, col)
        prow, pcol = parent_200k_cell(row, col)
        pubs = sorted(publications.get(code, []),
                      key=lambda r: (r.get("pub_year") or 0), reverse=True)
        latest = pubs[0] if pubs else {}
        sheets.append({
            "sheet_code": code,
            "region_code": code[:2],
            "region_name": region_names.get(code[:2], ""),
            "sheet_number": int(code[2:]),
            "map_ids": sorted({p["map_id"] for p in pubs}, key=lambda x: int(x) if x.isdigit() else 0),
            "latest_map_id": latest.get("map_id", ""),
            "title_ja": latest.get("title_ja", ""),
            "title_en": latest.get("title_en", ""),
            "pub_year": latest.get("pub_year"),
            "grid_row": row,
            "grid_col": col,
            "parent_200k_row": prow,
            "parent_200k_col": pcol,
            "bbox_wgs84": [round(v, 6) for v in (south, west, north, east)],
            "center_wgs84": [round((south + north) / 2, 6), round((west + east) / 2, 6)],
            "bbox_tokyo": [round(v, 6) for v in cell_bounds_tokyo(row, col)],
            "geometry_source": info["geometry_source"],
            "evidence_map_id": info.get("evidence_map_id", ""),
        })

    report = validate_assignment(assigned)
    missing = [r["map_id"] for r in records
               if not any(c in assigned for c in r["sheet_codes"])]

    return {
        "schema": "gsj_50k_grid/1",
        "description": "GSJ 5万分の1図幅の正規グリッド（緯度10分×経度15分・旧日本測地系定義）",
        "graticule": {
            "d_lat_deg": SHEET_DLAT,
            "d_lon_deg": SHEET_DLON,
            "datum": "Tokyo Datum (grid definition); bbox_wgs84 is the WGS84 projection of the same cell",
            "parent_200k": {"rows": PARENT_ROWS, "cols": PARENT_COLS},
        },
        "source": {
            "publications": str(PUB_DIR.relative_to(ROOT)).replace(os.sep, "/"),
            "publication_records": len(records),
            "zfk_index": str(ZFK_INDEX.relative_to(ROOT)).replace(os.sep, "/"),
        },
        "validation": report,
        "notes": notes,
        "unresolved_map_ids": sorted(missing, key=lambda x: int(x) if x.isdigit() else 0),
        "sheets": sheets,
    }


def write_grid(grid: dict[str, Any], out: Path = OUT_JSON) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(grid, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    return out


def print_summary(grid: dict[str, Any]) -> None:
    report = grid["validation"]
    sources: dict[str, int] = {}
    for sheet in grid["sheets"]:
        sources[sheet["geometry_source"]] = sources.get(sheet["geometry_source"], 0) + 1
    print(f"50k グリッド: {len(grid['sheets'])} 図幅 "
          f"/ 出版物レコード {grid['source']['publication_records']} 件")
    for name, count in sorted(sources.items(), key=lambda kv: -kv[1]):
        print(f"  幾何の根拠 {name}: {count}")
    print(f"  ZFK 重心との整合: {report['zfk_centroid_checked'] - len(report['zfk_centroid_failed'])}"
          f"/{report['zfk_centroid_checked']}")
    print(f"  セル重複: {len(report['duplicate_cells'])}")
    print(f"  区画内番号の順序違反: {len(report['region_order_violations'])}"
          f" {report['region_order_violations'][:8]}")
    if grid["unresolved_map_ids"]:
        print(f"  幾何未確定の刊行物: {len(grid['unresolved_map_ids'])} 件 "
              f"{grid['unresolved_map_ids'][:8]}")
    for note in grid["notes"][:10]:
        print(f"  注記: {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GSJ 50k 図幅グリッドを導出する")
    parser.add_argument("--check", action="store_true", help="ファイルを書かず検証だけ行う")
    args = parser.parse_args(argv)

    if not PUB_DIR.is_dir():
        print(f"[ERROR] 出版物キャッシュがありません: {PUB_DIR}")
        return 1
    grid = build_grid()
    if not grid["sheets"]:
        print("[ERROR] 図幅を 1 件も解決できませんでした。")
        return 1
    if not args.check:
        path = write_grid(grid)
        print(f"書き出し: {path.relative_to(ROOT)}")
    print_summary(grid)
    report = grid["validation"]
    return 1 if (report["zfk_centroid_failed"] or report["duplicate_cells"]) else 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
