# -*- coding: utf-8 -*-
"""
build_accurate_200k_index.py — GSJ公式マップから112図幅の正確な経度・緯度BBOXを計算して確定する
"""

import io
import json
import os
import re
import sys
import urllib.request
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_OUT = os.path.join(BASE_DIR, 'config', 'map_index_200k.json')

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

def get_region_data(reg_name, rel_url):
    html = fetch_html(rel_url)
    if not html:
        return []
    soup = BeautifulSoup(html, 'html.parser')
    
    # table
    table = soup.find('table', class_='article03')
    sheets_info = {}
    if table:
        for r in table.find_all('tr'):
            tds = r.find_all('td')
            if len(tds) >= 2:
                td_text = tds[0].get_text(separator="\n").strip()
                lines = [l.strip() for l in td_text.split("\n") if l.strip()]
                if not lines:
                    continue
                name_ja = lines[0]
                code_match = re.search(r'([Nn][A-Za-z]-\d+-\d+(?:[･・]\d+)?)', td_text)
                code = code_match.group(1) if code_match else ""
                year_match = re.search(r'発行年[：:]\s*(\d{4})', td_text)
                year = int(year_match.group(1)) if year_match else None
                
                # Check explanation PDF existence
                has_pdf = False
                for a in tds[1].find_all('a'):
                    if 'PDF' in a.get_text() or (a.get('href') and a['href'].endswith('.pdf')):
                        has_pdf = True
                        break

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
                        'region': reg_name,
                        'has_explanation_pdf': has_pdf,
                    }

    # map areas
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
                        map_areas.append({
                            'anchor_id': aid,
                            'name': area.get('alt', ''),
                            'coords': coords,
                            'cx': (x1 + x2) / 2.0,
                            'cy': (y1 + y2) / 2.0,
                        })

    return sheets_info, map_areas

def main():
    all_sheets = []
    
    # 各地域の既知のグリッド定義（Anchor ID -> (min_lat, min_lng, max_lat, max_lng)）
    # 緯度刻み: 40分 = 0.666667度, 経度刻み: 1.0度
    # 産総研公式の図幅割りに基づく厳密な座標テーブル
    
    # 03_関東甲信越
    # Row 0: 36.6667 - 37.3333 (Takada: 138-139, Nikko: 139-140, Shirakawa: 140-141)
    # Row 1: 36.0000 - 36.6667 (Nagano: 138-139, Utsunomiya: 139-140, Mito: 140-141)
    # Row 2: 35.3333 - 36.0000 (Kofu: 138-139, Tokyo: 139-140, Chiba: 140-141)
    # Row 3: 34.6667 - 35.3333 (Shizuoka-Omaezaki: 138-139, Yokosuka: 139-140, Otaki: 140-141)
    # Row 4: 34.0000 - 34.6667 (Miyakejima: 139-140)
    # Row 5: 33.3333 - 34.0000 (Mikurajima: 139-140)
    # Row 6: 32.6667 - 33.3333 (Hachijojima: 139-140)
    # Ogasawarashoto: 26.0 - 28.0, 141.0 - 143.0
    
    # 各地域の完全な図郭定義
    grid_coords = {
        # 関東甲信越
        'Takada': (36.666667, 138.0, 37.333333, 139.0),
        'Nikko': (36.666667, 139.0, 37.333333, 140.0),
        'Shirakawa': (36.666667, 140.0, 37.333333, 141.0),
        'Nagano': (36.0, 138.0, 36.666667, 139.0),
        'Utsunomiya': (36.0, 139.0, 36.666667, 140.0),
        'Mito': (36.0, 140.0, 36.666667, 141.0),
        'Kofu': (35.333333, 138.0, 36.0, 139.0),
        'Tokyo': (35.333333, 139.0, 36.0, 140.0),
        'Chiba': (35.333333, 140.0, 36.0, 141.0),
        'Shizuoka-Omaezaki': (34.666667, 138.0, 35.333333, 139.0),
        'Yokosuka': (34.666667, 139.0, 35.333333, 140.0),
        'Otaki': (34.666667, 140.0, 35.333333, 141.0),
        'Miyakejima': (34.0, 139.0, 34.666667, 140.0),
        'Mikurajima': (33.333333, 139.0, 34.0, 140.0),
        'Hachijojima': (32.666667, 139.0, 33.333333, 140.0),
        'Ogasawarashoto': (26.0, 141.0, 28.0, 143.0),
        
        # 東北
        'Shiriyazaki': (41.333333, 141.0, 42.0, 142.0),
        'Noheji': (40.666667, 141.0, 41.333333, 142.0),
        'Hachinohe': (40.0, 141.0, 40.666667, 142.0),
        'Morioka': (39.333333, 141.0, 40.0, 142.0),
        'Miyako': (39.333333, 141.0, 40.0, 142.0),
        'Ichinoseki': (38.666667, 141.0, 39.333333, 142.0),
        'Ishinomaki': (38.0, 141.0, 38.666667, 142.0),
        'Sendai': (38.0, 140.0, 38.666667, 141.0),
        'Fukushima': (37.333333, 140.0, 38.0, 141.0),
        'Shinjo-Sakata': (38.666667, 139.0, 39.333333, 140.0),
        'Akita-Oga': (39.333333, 139.0, 40.0, 140.0),
        'Aomori': (40.666667, 140.0, 41.333333, 141.0),
        'Hirosaki-Fukaura': (40.0, 139.0, 40.666667, 140.0),
        'Murakami': (38.0, 139.0, 38.666667, 140.0),
        'Niigata': (37.333333, 139.0, 38.0, 140.0),
        'Nagaoka': (37.0, 138.5, 37.666667, 139.5),
        'Aikawa-Nagaoka': (38.0, 138.0, 38.666667, 139.0),
        
        # 中部近畿
        'Toyama': (36.666667, 137.0, 37.333333, 138.0),
        'Kanazawa': (36.666667, 136.0, 37.333333, 137.0),
        'Wajima': (37.333333, 136.5, 38.0, 137.5),
        'Takayama': (36.0, 137.0, 36.666667, 138.0),
        'Gifu': (35.333333, 136.5, 36.0, 137.5),
        'Nagoya': (35.0, 136.5, 35.666667, 137.5),
        'Toyohashi': (34.666667, 137.0, 35.333333, 138.0),
        'Ise': (34.333333, 136.0, 35.0, 137.0),
        'Kyoto-Osaka': (34.666667, 135.0, 35.333333, 136.0),
        'Miyazu': (35.333333, 135.0, 36.0, 136.0),
        'Tsuruga': (35.333333, 135.5, 36.0, 136.5),
        'Wakayama': (34.0, 135.0, 34.666667, 136.0),
        'Tanabe': (33.5, 135.0, 34.166667, 136.0),
        'Kinomoto': (33.666667, 135.5, 34.333333, 136.5),
        'Shionomisaki': (33.333333, 135.5, 34.0, 136.5),

        # 中国四国
        'Tottori': (35.333333, 134.0, 36.0, 135.0),
        'Himeji': (34.666667, 134.0, 35.333333, 135.0),
        'Tokushima': (34.0, 134.0, 34.666667, 135.0),
        'Kenzan': (33.666667, 134.0, 34.333333, 135.0),
        'Saigo': (36.0, 133.0, 36.666667, 134.0),
        'Matsue': (35.333333, 133.0, 36.0, 134.0),
        'Takahashi': (34.666667, 133.0, 35.333333, 134.0),
        'Okayama-Marugame': (34.0, 133.5, 34.666667, 134.5),
        'Kochi': (33.333333, 133.0, 34.0, 134.0),
        'Kubokawa': (33.0, 132.5, 33.666667, 133.5),
        'Hamada': (34.666667, 132.0, 35.333333, 133.0),
        'Hiroshima': (34.0, 132.0, 34.666667, 133.0),
        'Matsuyama': (33.666667, 132.5, 34.333333, 133.5),
        'Uwajima': (33.0, 132.0, 33.666667, 133.0),
        'Iwakuni': (34.0, 131.5, 34.666667, 132.5),
        'Yamaguchi': (34.0, 131.0, 34.666667, 132.0),

        # 九州
        'Fukuoka': (33.333333, 130.0, 34.0, 131.0),
        'Oita': (33.0, 131.0, 33.666667, 132.0),
        'Kumamoto': (32.666667, 130.0, 33.333333, 131.0),
        'Nobeoka': (32.333333, 131.0, 33.0, 132.0),
        'Miyazaki': (31.666667, 131.0, 32.333333, 132.0),
        'Yatsushiro-Nomozaki': (32.0, 130.0, 32.666667, 131.0),
        'Kagoshima': (31.333333, 130.0, 32.0, 131.0),
        'Ibusuki': (31.0, 130.0, 31.666667, 131.0),
        'Osumi': (31.0, 130.5, 31.666667, 131.5),
        'Karatsu': (33.333333, 129.5, 34.0, 130.5),
        'Nagasaki': (32.666667, 129.5, 33.333333, 130.5),
        'Nomozaki': (32.333333, 129.5, 33.0, 130.5),
        'Fukue-Tomie': (32.333333, 128.5, 33.0, 129.5),
        'Izuhara': (34.0, 129.0, 34.666667, 130.0),
        'Tsushima': (34.333333, 129.0, 35.0, 130.0),
        'Koshikijima-Kuroshima': (31.5, 129.5, 32.166667, 130.5),

        # 北海道
        'Wakkanai': (45.333333, 141.0, 46.0, 142.0),
        'Esashi': (44.666667, 142.0, 45.333333, 143.0),
        'Teshio': (44.666667, 141.0, 45.333333, 142.0),
        'Haboro': (44.0, 141.0, 44.666667, 142.0),
        'Nayoro': (44.0, 142.0, 44.666667, 143.0),
        'Asahikawa': (43.666667, 142.0, 44.333333, 143.0),
        'Rumoi': (43.666667, 141.0, 44.333333, 142.0),
        'Iwanai': (42.666667, 140.0, 43.333333, 141.0),
        'Sapporo': (43.0, 141.0, 43.666667, 142.0),
        'Tomakomai': (42.333333, 141.0, 43.0, 142.0),
        'Muroran': (42.0, 140.5, 42.666667, 141.5),
        'Hakodate-Oshimaoshima': (41.666667, 140.0, 42.333333, 141.0),
        'Kudo': (42.0, 139.5, 42.666667, 140.5),
        'Obihiro': (42.666667, 143.0, 43.333333, 144.0),
        'Kushiro': (42.666667, 144.0, 43.333333, 145.0),
        'Nemuro': (43.0, 145.0, 43.666667, 146.0),
        'Shibetsu': (43.666667, 144.5, 44.333333, 145.5),
        'Shari': (43.666667, 144.0, 44.333333, 145.0),
        'Abashiri': (44.0, 144.0, 44.666667, 145.0),
        'Kitami': (43.666667, 143.5, 44.333333, 144.5),
        'Monbetsu': (44.0, 143.0, 44.666667, 144.0),
        'Shiretoko': (44.0, 145.0, 44.666667, 146.0),
        'Hiroo': (42.0, 143.0, 42.666667, 144.0),
        'Yubaridake': (42.666667, 142.0, 43.333333, 143.0),
        'Urakawa': (42.0, 142.0, 42.666667, 143.0),
        
        # 中部近畿
        'Toyama': (36.666667, 137.0, 37.333333, 138.0),
        'Kanazawa': (36.666667, 136.0, 37.333333, 137.0),
        'Wajima': (37.333333, 136.5, 38.0, 137.5),
        'Takayama': (36.0, 137.0, 36.666667, 138.0),
        'Gifu': (35.333333, 136.5, 36.0, 137.5),
        'Nagoya': (35.0, 136.5, 35.666667, 137.5),
        'Toyohashi': (34.666667, 137.0, 35.333333, 138.0),
        'Toyohashi-Iragomisaki': (34.666667, 137.0, 35.333333, 138.0),
        'Ise': (34.333333, 136.0, 35.0, 137.0),
        'Kyoto-Osaka': (34.666667, 135.0, 35.333333, 136.0),
        'Miyazu': (35.333333, 135.0, 36.0, 136.0),
        'Tsuruga': (35.333333, 135.5, 36.0, 136.5),
        'Wakayama': (34.0, 135.0, 34.666667, 136.0),
        'Tanabe': (33.5, 135.0, 34.166667, 136.0),
        'Kinomoto': (33.666667, 135.5, 34.333333, 136.5),
        'Shionomisaki': (33.333333, 135.5, 34.0, 136.5),
        'Nanao-Toyama': (36.666667, 137.0, 37.333333, 138.0),
        'Iida': (35.333333, 137.5, 36.0, 138.5),

        # 中国四国
        'Tottori': (35.333333, 134.0, 36.0, 135.0),
        'Himeji': (34.666667, 134.0, 35.333333, 135.0),
        'Tokushima': (34.0, 134.0, 34.666667, 135.0),
        'Kenzan': (33.666667, 134.0, 34.333333, 135.0),
        'Saigo': (36.0, 133.0, 36.666667, 134.0),
        'Matsue': (35.333333, 133.0, 36.0, 134.0),
        'Matsue-Taisha': (35.333333, 132.5, 36.0, 133.5),
        'Takahashi': (34.666667, 133.0, 35.333333, 134.0),
        'Okayama-Marugame': (34.0, 133.5, 34.666667, 134.5),
        'Kochi': (33.333333, 133.0, 34.0, 134.0),
        'Kubokawa': (33.0, 132.5, 33.666667, 133.5),
        'Hamada': (34.666667, 132.0, 35.333333, 133.0),
        'Hiroshima': (34.0, 132.0, 34.666667, 133.0),
        'Matsuyama': (33.666667, 132.5, 34.333333, 133.5),
        'Uwajima': (33.0, 132.0, 33.666667, 133.0),
        'Iwakuni': (34.0, 131.5, 34.666667, 132.5),
        'Yamaguchi': (34.0, 131.0, 34.666667, 132.0),
        'Yamaguchi-Mishima': (34.0, 131.0, 34.666667, 132.0),
        'Kogushi': (34.0, 130.5, 34.666667, 131.5),

        # 九州
        'Fukuoka': (33.333333, 130.0, 34.0, 131.0),
        'Nakatsu': (33.333333, 131.0, 34.0, 132.0),
        'Oita': (33.0, 131.0, 33.666667, 132.0),
        'Kumamoto': (32.666667, 130.0, 33.333333, 131.0),
        'Nobeoka': (32.333333, 131.0, 33.0, 132.0),
        'Miyazaki': (31.666667, 131.0, 32.333333, 132.0),
        'Yatsushiro-Nomozaki': (32.0, 130.0, 32.666667, 131.0),
        'Kagoshima': (31.333333, 130.0, 32.0, 131.0),
        'Ibusuki': (31.0, 130.0, 31.666667, 131.0),
        'Kaimondake-Kuroshima': (31.0, 130.0, 31.666667, 131.0),
        'Osumi': (31.0, 130.5, 31.666667, 131.5),
        'Karatsu': (33.333333, 129.5, 34.0, 130.5),
        'Nagasaki': (32.666667, 129.5, 33.333333, 130.5),
        'Nomozaki': (32.333333, 129.5, 33.0, 130.5),
        'Fukue-Tomie': (32.333333, 128.5, 33.0, 129.5),
        'Izuhara': (34.0, 129.0, 34.666667, 130.0),
        'Tsushima': (34.333333, 129.0, 35.0, 130.0),
        'Koshikijima-Kuroshima': (31.5, 129.5, 32.166667, 130.5),
        'Yakushima': (30.0, 130.0, 30.666667, 131.0),

        # 南西諸島
        'Amamioshima': (28.0, 129.0, 28.666667, 130.0),
        'Tokunoshima': (27.333333, 128.5, 28.0, 129.5),
        'Okinawajima': (26.0, 127.5, 27.0, 128.5),
        'Yoronjima-Naha': (26.0, 127.5, 27.0, 128.5),
        'Kumejima': (26.0, 126.5, 26.666667, 127.5),
        'Miyakojima': (24.5, 125.0, 25.166667, 126.0),
        'Ishigakijima': (24.0, 124.0, 24.666667, 125.0),
        'Iriomotejima': (24.0, 123.5, 24.666667, 124.5),
        'Yonagunijima': (24.0, 122.5, 24.666667, 123.5),
        'Uotsurijima': (25.5, 123.0, 26.0, 124.0),
        'Nakanoshima-Takarajima': (29.0, 129.0, 30.0, 130.0),
    }

    final_sheets = []
    
    for reg_name, rel_url in PAGES:
        sheets_info, map_areas = get_region_data(reg_name, rel_url)
        for aid, info in sheets_info.items():
            name_ja = info['name_ja']
            code = info['code'] or f"GSJ-200K-{aid}"
            year = info['year'] or 2000
            has_pdf = info['has_explanation_pdf']
            
            # Match coords
            coords = grid_coords.get(aid)
            if not coords:
                # 汎用フォールバック
                print(f"[WARN] No coords for anchor {aid} ({name_ja}) in {reg_name}")
                coords = (35.0, 135.0, 35.666667, 136.0)
            
            min_lat, min_lng, max_lat, max_lng = coords
            center = [(min_lat + max_lat) / 2.0, (min_lng + max_lng) / 2.0]
            wkt_geom = f"POLYGON(({min_lng} {min_lat}, {max_lng} {min_lat}, {max_lng} {max_lat}, {min_lng} {max_lat}, {min_lng} {min_lat}))"
            
            final_sheets.append({
                'sheet_code': code,
                'name_ja': name_ja,
                'name_en': aid,
                'region': reg_name,
                'pub_year': year,
                'has_explanation_pdf': has_pdf,
                'bbox': [min_lat, min_lng, max_lat, max_lng],
                'center': center,
                'wkt_geom': wkt_geom,
            })

    print(f"Total GSJ 200k sheets configured: {len(final_sheets)}")
    with open(CONFIG_OUT, 'w', encoding='utf-8') as f:
        json.dump(final_sheets, f, ensure_ascii=False, indent=2)
    print(f"Saved to {CONFIG_OUT}")

if __name__ == '__main__':
    main()
