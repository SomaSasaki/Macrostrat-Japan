# -*- coding: utf-8 -*-
"""
extract_gsj_200k_grid.py — GSJ 20万分の1地質図カタログから正確な図郭（経度・緯度）を抽出する
"""

import io
import json
import os
import re
import sys
import urllib.request
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE_URL = "https://www.gsj.jp/Map/JP/"
PAGES = [
    ("01_北海道", "geology2-1.html"),
    ("02_東北", "geology2-2.html"),
    ("03_関東甲信越", "geology2-3.html"),
    ("04_中部近畿", "geology2-4.html"),
    ("05_中国四国", "geology2-5.html"),
    ("06_九州", "geology2-6.html"),
    ("07_南西諸島", "geology2-7.html"),
]

UA = {"User-Agent": "Mozilla/5.0"}

def fetch_html(rel_url):
    url = urllib.request.urljoin(BASE_URL, rel_url)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return ""

def parse_region(region_name, rel_url):
    html = fetch_html(rel_url)
    if not html:
        return []
    soup = BeautifulSoup(html, 'html.parser')
    
    # 1. table から図幅名・コード・発行年を抽出
    table = soup.find('table', class_='article03')
    sheets_info = {}
    if table:
        rows = table.find_all('tr')
        for r in rows:
            tds = r.find_all('td')
            if len(tds) >= 2:
                # td 0: 図名・コード・発行年
                td_text = tds[0].get_text(separator="\n").strip()
                lines = [l.strip() for l in td_text.split("\n") if l.strip()]
                if not lines:
                    continue
                name_ja = lines[0]
                # code
                code_match = re.search(r'([Nn][A-Za-z]-\d+-\d+(?:[･・]\d+)?)', td_text)
                code = code_match.group(1) if code_match else ""
                
                # year
                year_match = re.search(r'発行年[：:]\s*(\d{4})', td_text)
                year = int(year_match.group(1)) if year_match else None
                
                # anchor id
                th = r.find('th')
                anchor_id = ""
                if th and th.find('a'):
                    anchor_id = th.find('a').get('id', '')
                
                if anchor_id:
                    sheets_info[anchor_id] = {
                        'name_ja': name_ja,
                        'code': code,
                        'year': year,
                        'anchor_id': anchor_id,
                        'region': region_name,
                    }

    # 2. map タグからエリア座標を取得して相対位置（行・列）を判定
    map_tag = soup.find('map', id='Map') or soup.find('map', attrs={'name': 'Map'})
    map_areas = []
    if map_tag:
        for area in map_tag.find_all('area'):
            href = area.get('href', '')
            if href.startswith('#'):
                aid = href[1:]
                coords_str = area.get('coords', '')
                if coords_str:
                    coords = [int(c.strip()) for c in coords_str.split(',') if c.strip().isdigit()]
                    if len(coords) == 4:
                        x1, y1, x2, y2 = coords
                        cx = (x1 + x2) / 2.0
                        cy = (y1 + y2) / 2.0
                        map_areas.append({
                            'anchor_id': aid,
                            'name': area.get('alt', ''),
                            'coords': coords,
                            'cx': cx,
                            'cy': cy,
                        })

    print(f"[{region_name}] Table: {len(sheets_info)} sheets, Map areas: {len(map_areas)}")
    return sheets_info, map_areas

if __name__ == '__main__':
    all_res = {}
    for reg, p in PAGES:
        all_res[reg] = parse_region(reg, p)
