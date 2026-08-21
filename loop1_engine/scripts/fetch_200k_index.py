# -*- coding: utf-8 -*-
"""
fetch_200k_index.py — GSJ 20万分の1地質図幅マスターインデックス生成スクリプト

日本の国土地理院・GSJ 20万分の1地勢図/地質図幅のグリッド定義（南から北、西から東）に基づき、
日本全国の20万分の1地質図幅のBBOXとメタデータを正確に計算して保存する。
"""

import io
import json
import os
import re
import sys
import urllib.request
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'map_index_200k.json')

def imw_code_to_bbox(code):
    """
    国土地理院 / GSJ 20万分の1地勢図のIMWコード（例: 'NJ-54-21', 'NI-54-32'）から
    緯度経度のBBOXと中心座標を計算する。
    
    1区画: 緯度40分 (4.0/6.0 度), 経度1度 (6.0/6.0 度)
    付番規則: 
      - row: 0 (南端) 〜 5 (北端)
      - col: 0 (西端) 〜 5 (東端)
      - sheet_num = row * 6 + col + 1
    """
    parts = code.strip().split('-')
    if len(parts) != 3:
        return None
    lat_zone = parts[0].upper()
    lng_zone = int(parts[1])
    sheet_num = int(parts[2])

    lat_letter = lat_zone[1]
    base_lat = (ord(lat_letter) - ord('A')) * 4.0
    base_lng = (lng_zone - 31) * 6.0

    row = (sheet_num - 1) // 6  # 0: 南端, 5: 北端
    col = (sheet_num - 1) % 6   # 0: 西端, 5: 東端

    min_lat = base_lat + row * (4.0 / 6.0)
    max_lat = min_lat + (4.0 / 6.0)
    min_lng = base_lng + col * 1.0
    max_lng = min_lng + 1.0

    return {
        'min_lat': round(min_lat, 6),
        'min_lng': round(min_lng, 6),
        'max_lat': round(max_lat, 6),
        'max_lng': round(max_lng, 6),
        'center_lat': round((min_lat + max_lat) / 2.0, 6),
        'center_lng': round((min_lng + max_lng) / 2.0, 6),
    }

def fetch_gsj_200k_catalog():
    base_url = 'https://www.gsj.jp/Map/JP/'
    pages = [
        ('geology2-1.html', '01_北海道'),
        ('geology2-2.html', '02_東北'),
        ('geology2-3.html', '03_関東甲信越'),
        ('geology2-4.html', '04_中部近畿'),
        ('geology2-5.html', '05_中国四国'),
        ('geology2-6.html', '06_九州'),
        ('geology2-7.html', '07_南西諸島'),
    ]

    sheet_map = {}

    for page_name, region_name in pages:
        url = base_url + page_name
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                html = res.read().decode('utf-8')
        except Exception as e:
            print(f"[WARN] Failed to fetch {url}: {e}")
            continue

        soup = BeautifulSoup(html, 'html.parser')
        tables = soup.find_all('table')

        for table in tables:
            rows = table.find_all('tr')
            for tr in rows:
                text = tr.get_text()
                m = re.search(r'([Nn][A-Za-z]-\d{2}-\d{1,2})', text)
                if not m:
                    continue
                code = m.group(1).upper()

                name_ja = ""
                name_en = ""
                a_tags = tr.find_all('a')
                for a in a_tags:
                    title_attr = a.get('title', '')
                    tm = re.search(r'「(.*?)」', title_attr)
                    if tm:
                        name_ja = tm.group(1).strip()
                    a_id = a.get('id', '')
                    if a_id and not name_en:
                        name_en = a_id.strip()
                    a_href = a.get('href', '')
                    hm = re.search(r'200k_([A-Za-z0-9_]+)\.jpg', a_href)
                    if hm and not name_en:
                        name_en = hm.group(1).strip()

                tds = tr.find_all('td')
                if not name_ja and tds:
                    td0_text = tds[0].get_text().strip()
                    cm = re.search(r'^(.*?)[Nn][A-Za-z]-\d{2}-\d{1,2}', td0_text)
                    if cm:
                        name_ja = cm.group(1).strip()

                pub_year = None
                ym = re.search(r'(\d{4})年|発行年[：:]\s*(\d{4})', text)
                if ym:
                    pub_year = int(ym.group(1) or ym.group(2))

                has_pdf = bool(re.search(r'解説|説明書|PDF', text, re.IGNORECASE))

                bbox = imw_code_to_bbox(code)
                if bbox and code not in sheet_map:
                    sheet_map[code] = {
                        'sheet_code': code,
                        'name_ja': name_ja or code,
                        'name_en': name_en or code,
                        'region': region_name,
                        'pub_year': pub_year,
                        'has_explanation_pdf': has_pdf,
                        'bbox': [bbox['min_lat'], bbox['min_lng'], bbox['max_lat'], bbox['max_lng']],
                        'center': [bbox['center_lat'], bbox['center_lng']],
                        'wkt_geom': f"POLYGON(({bbox['min_lng']} {bbox['min_lat']}, {bbox['max_lng']} {bbox['min_lat']}, {bbox['max_lng']} {bbox['max_lat']}, {bbox['min_lng']} {bbox['max_lat']}, {bbox['min_lng']} {bbox['min_lat']}))"
                    }

    sheets = list(sheet_map.values())
    sheets.sort(key=lambda x: (x['region'], x['sheet_code']))

    print(f"Total 200k sheets fetched: {len(sheets)}")
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(sheets, f, ensure_ascii=False, indent=2)
    print(f"Saved to {CONFIG_PATH}")
    return sheets

if __name__ == '__main__':
    fetch_gsj_200k_catalog()
