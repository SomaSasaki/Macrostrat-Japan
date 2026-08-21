"""
GSJ 5万分の1地質図幅カタログ 16地域ページの集計スクリプト

各地域ページから刊行済み図幅を抽出し、
- 地域別の刊行面数
- 全体の合計面数
- 各図幅の区画番号・図名・発行年
- ベクトルデータの有無
を集計する。
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import re
import json
import time
import urllib.request
from html.parser import HTMLParser
from collections import defaultdict

BASE_URL = "https://www.gsj.jp/Map/JP/"

# 16 region pages (geology4-1 through geology4-16)
REGIONS = {
    1: "網走",
    2: "釧路",
    3: "旭川",
    4: "札幌",
    5: "青森",
    6: "秋田",
    7: "新潟",
    8: "東京",
    9: "八丈島・小笠原諸島",
    10: "金沢",
    11: "京都",
    12: "岡山",
    13: "高知",
    14: "福岡",
    15: "鹿児島",
    16: "種子島・奄美大島・那覇・宮古島",
}


class SheetParser(HTMLParser):
    """Parse GSJ catalog pages to extract published sheet entries.
    
    Each published sheet has an <a id="NNNNN"> tag where NNNNN is the 
    region-code + sheet number (e.g. "05001" = region 5, sheet 001).
    The sheet name and year are in the adjacent <td> cells.
    """
    
    def __init__(self):
        super().__init__()
        self.sheets = []
        self._current_id = None
        self._in_td = False
        self._td_text = ""
        self._td_count = 0  # track which td we're in after the id anchor
        self._has_vector = False
        
    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        
        # Detect sheet entry anchor: <a id="NNNNN" ...>
        if tag == "a" and "id" in attr_dict:
            id_val = attr_dict["id"]
            if re.match(r'^\d{5}$', id_val):
                self._current_id = id_val
                self._td_count = 0
                self._has_vector = False
                
        # Track td elements after finding an id
        if tag == "td" and self._current_id is not None:
            self._in_td = True
            self._td_text = ""
            self._td_count += 1
            
        # Check for vector data download links
        if tag == "a" and self._current_id is not None:
            href = attr_dict.get("href", "")
            if "/VCT/" in href or "ベクトル" in href:
                self._has_vector = True
                
    def handle_data(self, data):
        if self._in_td:
            self._td_text += data
            
        # Also check for vector data text outside td
        if self._current_id is not None and "ベクトル" in data:
            self._has_vector = True
            
    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self._in_td = False
            
            # The first td after the anchor contains: sheet name, code, year, price/status
            if self._td_count == 1 and self._current_id is not None:
                text = self._td_text.strip()
                
                # Extract sheet name (first line, often in <strong>)
                name = ""
                name_match = re.search(r'(.+?)[\n\r]', text)
                if name_match:
                    name = name_match.group(1).strip()
                else:
                    name = text.split('\n')[0].strip() if text else ""
                
                # Remove any "※現地形図名..." annotation from name
                name = re.sub(r'※.*', '', name).strip()
                    
                # Extract year
                year_match = re.search(r'発行年[：:]?\s*(\d{4})', text)
                year = int(year_match.group(1)) if year_match else None
                
                # Extract code (e.g. "05-001")
                code_match = re.search(r'(\d{2})-(\d{3})', text)
                code = code_match.group(0) if code_match else self._current_id[:2] + "-" + self._current_id[2:]
                
                self._current_sheet_data = {
                    "id": self._current_id,
                    "code": code,
                    "name": name,
                    "year": year,
                    "region_num": int(self._current_id[:2]),
                }
                
            # The second td contains download links (vector data check happens via starttag)
            if self._td_count == 2 and self._current_id is not None:
                if hasattr(self, '_current_sheet_data'):
                    self._current_sheet_data["has_vector"] = self._has_vector
                    self.sheets.append(self._current_sheet_data)
                self._current_id = None


def fetch_page(url, retries=3):
    """Fetch a URL with retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (research project - GSJ catalog aggregation)'
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  ERROR fetching {url}: {e}")
                return None


def main():
    all_sheets = []
    region_counts = {}
    
    print("=" * 70)
    print("GSJ 5万分の1地質図幅カタログ 全16地域ページ集計")
    print("=" * 70)
    print()
    
    for region_num in sorted(REGIONS.keys()):
        region_name = REGIONS[region_num]
        url = f"{BASE_URL}geology4-{region_num}.html"
        
        print(f"[{region_num:2d}] {region_name} ... ", end="", flush=True)
        
        html = fetch_page(url)
        if html is None:
            print("FAILED")
            region_counts[region_num] = {"name": region_name, "count": 0, "error": True}
            continue
            
        parser = SheetParser()
        parser.feed(html)
        
        count = len(parser.sheets)
        region_counts[region_num] = {
            "name": region_name, 
            "count": count,
            "error": False,
        }
        all_sheets.extend(parser.sheets)
        
        # Count those with vector data
        vec_count = sum(1 for s in parser.sheets if s.get("has_vector"))
        
        print(f"{count} 面 (ベクトル有: {vec_count})")
        
        # Be polite
        time.sleep(0.5)
    
    # ========== Summary ==========
    print()
    print("=" * 70)
    print("集計結果")
    print("=" * 70)
    print()
    
    # By region
    total = 0
    total_vec = 0
    hokkaido_total = 0
    honshu_etc_total = 0
    
    print(f"{'地域':>4}  {'地域名':<20}  {'刊行面数':>8}  {'ベクトル有':>10}")
    print("-" * 55)
    
    for region_num in sorted(region_counts.keys()):
        rc = region_counts[region_num]
        count = rc["count"]
        
        # Count vector data for this region
        region_sheets = [s for s in all_sheets if s["region_num"] == region_num]
        vec_count = sum(1 for s in region_sheets if s.get("has_vector"))
        
        print(f"  {region_num:2d}    {rc['name']:<20}  {count:>6}    {vec_count:>8}")
        total += count
        total_vec += vec_count
        
        if region_num <= 4:
            hokkaido_total += count
        else:
            honshu_etc_total += count
    
    print("-" * 55)
    print(f"  {'合計':>4}  {'':<20}  {total:>6}    {total_vec:>8}")
    print(f"  {'うち北海道(1-4)':>16}  {'':<8}  {hokkaido_total:>6}")
    print(f"  {'本州以南(5-16)':>16}  {'':<8}  {honshu_etc_total:>6}")
    print()
    
    # Coverage calculation
    TOTAL_POSSIBLE = 1260  # approximate total sheets for full Japan coverage
    coverage_pct = (total / TOTAL_POSSIBLE) * 100
    print(f"理論全面数: 約{TOTAL_POSSIBLE}面 (378,000 km^2 / 300 km^2/面)")
    print(f"刊行率: {total}/{TOTAL_POSSIBLE} = {coverage_pct:.1f}%")
    print(f"未刊行: 約{TOTAL_POSSIBLE - total}面")
    print()
    
    # Year distribution
    years = [s["year"] for s in all_sheets if s.get("year")]
    if years:
        decade_counts = defaultdict(int)
        for y in years:
            decade = (y // 10) * 10
            decade_counts[decade] += 1
        
        print("発行年代別分布:")
        for decade in sorted(decade_counts.keys()):
            bar = "█" * (decade_counts[decade] // 2)
            print(f"  {decade}s: {decade_counts[decade]:>4} 面  {bar}")
        print(f"  最古: {min(years)}, 最新: {max(years)}")
    print()
    
    # Unique sheet IDs (check for duplicates)
    id_set = set(s["id"] for s in all_sheets)
    if len(id_set) < len(all_sheets):
        print(f"⚠ 重複エントリあり: {len(all_sheets)} エントリ, {len(id_set)} ユニークID")
        # Find duplicates
        from collections import Counter
        id_counter = Counter(s["id"] for s in all_sheets)
        dups = {k: v for k, v in id_counter.items() if v > 1}
        print(f"  重複ID: {dups}")
    else:
        print(f"✓ 重複なし: {len(all_sheets)} 面全てユニーク")
    
    # Save detailed data
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = os.path.join(base_dir, "data", "50k", "gsj_50k_catalog.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "metadata": {
                "source": "GSJ 5万分の1地質図幅カタログ",
                "url": "https://www.gsj.jp/Map/JP/geology4.html",
                "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "total_sheets": total,
                "total_with_vector": total_vec,
                "hokkaido_sheets": hokkaido_total,
                "honshu_etc_sheets": honshu_etc_total,
            },
            "region_summary": region_counts,
            "sheets": all_sheets,
        }, f, ensure_ascii=False, indent=2)
    
    print(f"詳細データ保存先: {output_path}")
    print()
    
    # List all sheet names for regions 5-16 (non-Hokkaido)
    print("=" * 70)
    print("本州以南 (地域5-16) の図幅一覧")
    print("=" * 70)
    for region_num in range(5, 17):
        region_sheets = sorted(
            [s for s in all_sheets if s["region_num"] == region_num],
            key=lambda s: s["id"]
        )
        if region_sheets:
            print(f"\n--- {region_num}. {REGIONS[region_num]} ({len(region_sheets)}面) ---")
            for s in region_sheets:
                vec_mark = "V" if s.get("has_vector") else " "
                year_str = str(s["year"]) if s.get("year") else "????"
                print(f"  {s['code']}  {s['name']:<16}  {year_str}  [{vec_mark}]")


if __name__ == "__main__":
    main()
