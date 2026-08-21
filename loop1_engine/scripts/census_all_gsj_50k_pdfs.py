# -*- coding: utf-8 -*-
"""GSJ 50k 全図幅 PDF センサス（全数調査）スクリプト

産総研が公開している 5万分の1地質図幅（全655点のPDF）を網羅的に走査し、
図幅ごとの内部構造（ベクター/スキャン、英語要約図、日本語層序図、Column構造）を
1冊ずつ実測分類して「全数センサスデータベース」を生成する。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import pdfplumber

ROOT = Path(__file__).resolve().parents[2]
INV_PATH = ROOT / "data" / "50k" / "00_management" / "gsj_50k_inventory.json"
CACHE_DIR = ROOT / "data" / "50k" / "cache" / "gsj_50k_pdfs"
OUTPUT_DIR = ROOT / "data" / "50k" / "00_management"
PROGRESS_PATH = OUTPUT_DIR / "gsj_50k_census_progress.json"
FINAL_CENSUS_PATH = OUTPUT_DIR / "gsj_50k_full_census.json"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MacroStrat-Census/1.0"}


def download_and_inspect_map(m: dict[str, Any]) -> dict[str, Any]:
    url = m.get("pdf_url")
    mid = str(m.get("map_id", ""))
    year = str(m.get("pub_year", "----"))
    title_ja = m.get("title_ja", "")
    title_en = m.get("title_en", "")

    result: dict[str, Any] = {
        "map_id": mid,
        "pub_year": year,
        "title_ja": title_ja,
        "title_en": title_en,
        "pdf_url": url,
        "download_status": "ok",
        "file_size_mb": 0.0,
        "total_pages": 0,
        "has_english_abstract": False,
        "has_english_summary_figure": False,
        "has_japanese_summary_figure": False,
        "has_japanese_strat_table": False,
        "is_vector_diagram": False,
        "column_structure": "Unknown",
        "classified_type": None,
        "error": None,
    }

    if not url:
        result["download_status"] = "no_url"
        result["classified_type"] = "Type 0: PDF未公開図幅"
        return result

    fname = url.split("/")[-1]
    local_path = CACHE_DIR / fname

    # 1. Download (if not already cached)
    try:
        if not local_path.exists() or local_path.stat().st_size == 0:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as resp, open(
                local_path, "wb"
            ) as f:
                f.write(resp.read())
        result["file_size_mb"] = round(
            local_path.stat().st_size / (1024 * 1024), 2
        )
    except Exception as e:
        result["download_status"] = "download_error"
        result["error"] = f"Download failed: {e}"
        result["classified_type"] = "Error: ダウンロード失敗"
        return result

    # 2. Inspect PDF structure
    try:
        with pdfplumber.open(local_path) as pdf:
            pages = len(pdf.pages)
            result["total_pages"] = pages

            # Sample pages: first 15 and last 15 pages
            check_indices = sorted(
                list(
                    set(
                        list(range(min(15, pages)))
                        + list(range(max(0, pages - 15), pages))
                    )
                )
            )

            vector_count_sample = 0
            for idx in check_indices:
                pg = pdf.pages[idx]
                v = len(pg.rects) + len(pg.lines)
                if v > 15:
                    vector_count_sample += 1

                txt = pg.extract_text() or ""

                # Check English Abstract & Summary Figure
                if idx >= pages - 15:
                    if (
                        "ABSTRACT" in txt.upper()
                        or "SUMMARY OF GEOLOGY" in txt.upper()
                    ):
                        result["has_english_abstract"] = True
                    if (
                        "SUMMARY OF GEOLOGY" in txt.upper()
                        or "CORRELATION" in txt.upper()
                        or "STRATIGRAPHY" in txt.upper()
                    ):
                        if len(pg.rects) > 10 or len(pg.images) > 0:
                            result["has_english_summary_figure"] = True
                            if (
                                "WESTERN" in txt.upper()
                                and "EASTERN" in txt.upper()
                            ):
                                result["column_structure"] = (
                                    "3 Columns (Western / Central / Eastern)"
                                )
                            elif (
                                "NORTHERN" in txt.upper()
                                and "SOUTHERN" in txt.upper()
                            ):
                                result["column_structure"] = (
                                    "2 Columns (Northern / Southern)"
                                )
                            elif "AREA" in txt.upper() or "ZONE" in txt.upper():
                                result["column_structure"] = "Multi-Area / Zones"
                            else:
                                result["column_structure"] = (
                                    "Single / Unified Column"
                                )
                else:
                    # Check Japanese figures
                    if (
                        "層序総括図" in txt
                        or "層序対比図" in txt
                        or "総合柱状図" in txt
                        or "地質層序" in txt
                    ):
                        result["has_japanese_summary_figure"] = True
                    if "第" in txt and "表" in txt and "層序" in txt:
                        result["has_japanese_strat_table"] = True

            if vector_count_sample >= 3:
                result["is_vector_diagram"] = True

            # Categorize into the 5 Types
            if (
                fname.startswith("GSJ_MAP_G050_05048")
                or (
                    result["is_vector_diagram"]
                    and result["has_english_summary_figure"]
                    and "3 Columns" in result["column_structure"]
                )
            ):
                result["classified_type"] = "Type 1: 一戸型の英語ベクター総括図"
            elif (
                result["is_vector_diagram"]
                and result["has_english_summary_figure"]
            ):
                result["classified_type"] = "Type 4: Column数や配置が異なる図 (Vector/English)"
            elif (
                result["is_vector_diagram"]
                and not result["has_english_summary_figure"]
            ):
                result["classified_type"] = "Type 2: 日本語図しかない資料 (Vector/Digital)"
            elif (
                not result["has_english_summary_figure"]
                and not result["has_japanese_summary_figure"]
                and not result["has_japanese_strat_table"]
            ):
                result["classified_type"] = "Type 5: 総括図自体がない資料"
            elif not result["has_english_summary_figure"] and (
                result["has_japanese_summary_figure"]
                or result["has_japanese_strat_table"]
            ):
                result["classified_type"] = "Type 2: 日本語図しかない資料 (Raster Scan)"
            else:
                result["classified_type"] = (
                    "Type 3: ラスター／スキャン図 (英語要約付き)"
                )

    except Exception as e:
        result["download_status"] = "inspect_error"
        result["error"] = f"Inspection failed: {e}"
        result["classified_type"] = "Error: 解析失敗"

    return result


def main():
    print("=== GSJ 50k 全図幅 PDF センサス開始 ===")
    with open(INV_PATH, encoding="utf-8") as f:
        inv = json.load(f)

    maps = inv.get("maps", [])
    print(f"全図幅数: {len(maps)} 件")

    census_results: list[dict[str, Any]] = []
    completed = 0
    total = len(maps)
    t0 = time.time()

    # 並列処理 (Worker: 6)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(download_and_inspect_map, m): m for m in maps}

        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            census_results.append(res)
            completed += 1

            if completed % 25 == 0 or completed == total:
                elapsed = time.time() - t0
                speed = completed / elapsed
                rem_sec = (total - completed) / speed if speed > 0 else 0
                print(
                    f"進捗: {completed}/{total} ({completed/total*100:.1f}%) | "
                    f"速度: {speed:.1f}件/秒 | 残り: {rem_sec/60:.1f}分"
                )

                # 中間保存
                with open(PROGRESS_PATH, "w", encoding="utf-8") as pf:
                    json.dump(
                        {
                            "completed": completed,
                            "total": total,
                            "elapsed_seconds": round(elapsed, 1),
                            "latest_batch": census_results[-25:],
                        },
                        pf,
                        ensure_ascii=False,
                        indent=2,
                    )

    # 最終集計と保存
    type_counts: dict[str, int] = {}
    for r in census_results:
        t = r.get("classified_type") or "Unknown"
        type_counts[t] = type_counts.get(t, 0) + 1

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_maps_scanned": len(census_results),
        "total_time_seconds": round(time.time() - t0, 1),
        "type_distribution": type_counts,
        "results": sorted(census_results, key=lambda x: str(x.get("map_id", ""))),
    }

    with open(FINAL_CENSUS_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n=== 全数センサス完了！ ===")
    print(f"総所要時間: {summary['total_time_seconds']:.1f} 秒")
    print("【タイプ別分布】:")
    for k, v in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  {k:45} : {v:4d} 件 ({v/len(census_results)*100:5.1f}%)")
    print(f"\n保存先: {FINAL_CENSUS_PATH}")


if __name__ == "__main__":
    main()
