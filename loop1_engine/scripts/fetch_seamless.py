# -*- coding: utf-8 -*-
"""
fetch_seamless.py — GSJ シームレス地質図V2 API クライアント

日本全国の20万分の1地質図幅（全124区画）のBBOXに対して
シームレス地質図V2 Web API (legend.json?box=...) を呼び出し、
各図幅に含まれる地質単元データを data/raw/seamless_200k/ にキャッシュする。
"""

import io
import json
import os
import sys
import time
import urllib.request

# stdout を utf-8 に設定
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'data', '200k', 'raw', 'seamless_200k')
MAP_INDEX_PATH = os.path.join(BASE_DIR, 'config', 'map_index_200k.json')

API_URL_TEMPLATE = "https://gbank.gsj.jp/seamless/v2/api/1.3.1/legend.json?box={min_lat},{min_lng},{max_lat},{max_lng}"

def fetch_seamless_box(min_lat, min_lng, max_lat, max_lng, timeout=15):
    """
    指定BBOXのシームレス地質図凡例を取得する
    """
    url = API_URL_TEMPLATE.format(
        min_lat=min_lat,
        min_lng=min_lng,
        max_lat=max_lat,
        max_lng=max_lng
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'MacroStrat-GSJ-Pipeline/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read().decode('utf-8'))
            return data
    except Exception as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        return None

def cache_all_200k_seamless(delay_sec=0.2):
    """
    config/map_index_200k.json に定義された全図幅の凡例データを取得・キャッシュ
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    if not os.path.exists(MAP_INDEX_PATH):
        print(f"[ERROR] Map index not found: {MAP_INDEX_PATH}")
        return

    with open(MAP_INDEX_PATH, 'r', encoding='utf-8') as f:
        sheets = json.load(f)

    print(f"Starting seamless fetch for {len(sheets)} 200k sheets...")
    success_count = 0
    total_units = 0

    for idx, s in enumerate(sheets, start=1):
        sheet_code = s['sheet_code']
        cache_file = os.path.join(CACHE_DIR, f"{sheet_code}.json")

        if os.path.exists(cache_file):
            with open(cache_file, 'r', encoding='utf-8') as cf:
                data = json.load(cf)
            print(f"[{idx}/{len(sheets)}] {sheet_code} ({s.get('name_ja', '')}): CACHED ({len(data)} legends)")
            success_count += 1
            total_units += len(data)
            continue

        min_lat, min_lng, max_lat, max_lng = s['bbox']
        data = fetch_seamless_box(min_lat, min_lng, max_lat, max_lng)
        if data is not None:
            with open(cache_file, 'w', encoding='utf-8') as cf:
                json.dump(data, cf, ensure_ascii=False, indent=2)
            print(f"[{idx}/{len(sheets)}] {sheet_code} ({s.get('name_ja', '')}): FETCHED ({len(data)} legends)")
            success_count += 1
            total_units += len(data)
        else:
            print(f"[{idx}/{len(sheets)}] {sheet_code} ({s.get('name_ja', '')}): FAILED")

        time.sleep(delay_sec)

    print(f"\nCompleted! Success: {success_count}/{len(sheets)}, Total legend units: {total_units}")

if __name__ == '__main__':
    cache_all_200k_seamless()
