# -*- coding: utf-8 -*-
"""
fetch_seamless_robust.py — 全112図幅のシームレス地質図V2凡例データを完全取得（リトライ・キャッシュ更新）
"""

import io
import json
import os
import sys
import time
import urllib.request
import urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'map_index_200k.json')
OUT_DIR = os.path.join(BASE_DIR, 'data', 'raw', 'seamless_200k')
os.makedirs(OUT_DIR, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0"}
BASE_API = "https://gbank.gsj.jp/seamless/v2/api/1.3.1/legend.json"

def fetch_legends_for_box(bbox, max_retries=3):
    min_lat, min_lng, max_lat, max_lng = bbox
    url = f"{BASE_API}?box={min_lat},{min_lng},{max_lat},{max_lng}"
    req = urllib.request.Request(url, headers=UA)
    
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data
        except Exception as e:
            if attempt == max_retries:
                print(f"[ERROR] Failed after {max_retries} attempts: {url} ({e})")
                return None
            time.sleep(2 * attempt)
    return None

def main():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        sheets = json.load(f)

    total = len(sheets)
    success = 0
    total_legends = 0

    print(f"Starting robust fetch for {total} 200k sheets...")

    for idx, sheet in enumerate(sheets, start=1):
        code = sheet['sheet_code']
        aid = sheet.get('name_en', code)
        name_ja = sheet.get('name_ja', code)
        bbox = sheet['bbox']

        # ファイル名として安全なキー（code と aid の両方に対応）
        cache_file = os.path.join(OUT_DIR, f"{code}.json")
        aid_cache_file = os.path.join(OUT_DIR, f"{aid}.json")

        # 既存キャッシュが有効（>0 legends）ならスキップ
        legends = None
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as cf:
                    cached = json.load(cf)
                    if isinstance(cached, list) and len(cached) > 0:
                        legends = cached
            except Exception:
                pass

        if legends is None:
            time.sleep(0.3)
            legends = fetch_legends_for_box(bbox)
            if legends is not None:
                with open(cache_file, 'w', encoding='utf-8') as cf:
                    json.dump(legends, cf, ensure_ascii=False, indent=2)
                # aid 側にもコピー保存
                if aid and aid != code:
                    with open(aid_cache_file, 'w', encoding='utf-8') as af:
                        json.dump(legends, af, ensure_ascii=False, indent=2)
                print(f"[{idx}/{total}] {code} ({name_ja}): FETCHED ({len(legends)} legends)")
            else:
                print(f"[{idx}/{total}] {code} ({name_ja}): FAILED")
        else:
            print(f"[{idx}/{total}] {code} ({name_ja}): CACHED ({len(legends)} legends)")

        if legends:
            success += 1
            total_legends += len(legends)

    print(f"\n=======================================================")
    print(f"Complete: {success}/{total} sheets, {total_legends} total geological legends")
    print(f"=======================================================")

if __name__ == '__main__':
    main()
