# -*- coding: utf-8 -*-
"""都道府県地質図（県地質誌）のメタデータ管理インベントリ。

GSJ 50k でカバーされない33%の空白地域を埋めるための、各都道府県発行の
地質図・説明書のメタデータ（縮尺、発行年、PDF等の入手元）を管理する。
将来的に Web クローリングや API と連携し、自動抽出パイプラインへ接続する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "00_management"
PREF_INVENTORY_JSON = OUT_DIR / "prefectural_geology_inventory.json"

# JIS X 0401 都道府県コード
PREFECTURES = {
    "01": "北海道", "02": "青森県", "03": "岩手県", "04": "宮城県", "05": "秋田県",
    "06": "山形県", "07": "福島県", "08": "茨城県", "09": "栃木県", "10": "群馬県",
    "11": "埼玉県", "12": "千葉県", "13": "東京都", "14": "神奈川県", "15": "新潟県",
    "16": "富山県", "17": "石川県", "18": "福井県", "19": "山梨県", "20": "長野県",
    "21": "岐阜県", "22": "静岡県", "23": "愛知県", "24": "三重県", "25": "滋賀県",
    "26": "京都府", "27": "大阪府", "28": "兵庫県", "29": "奈良県", "30": "和歌山県",
    "31": "鳥取県", "32": "島根県", "33": "岡山県", "34": "広島県", "35": "山口県",
    "36": "徳島県", "37": "香川県", "38": "愛媛県", "39": "高知県", "40": "福岡県",
    "41": "佐賀県", "42": "長崎県", "43": "熊本県", "44": "大分県", "45": "宮崎県",
    "46": "鹿児島県", "47": "沖縄県"
}

def generate_skeleton() -> dict[str, Any]:
    """インベントリの初期スケルトンを生成する"""
    inventory = {}
    for code, name in PREFECTURES.items():
        inventory[code] = {
            "pref_code": code,
            "pref_name": name,
            "map_title": f"{name}地質図",
            "scale": None, # e.g., 100000, 200000
            "pub_year": None,
            "publisher": None,
            "description_book_available": False,
            "pdf_url": None,
            "local_pdf": None,
            "status": "pending_survey", # pending_survey, collected, structured
        }
    return inventory

def load_inventory() -> dict[str, Any]:
    if PREF_INVENTORY_JSON.exists():
        with PREF_INVENTORY_JSON.open(encoding="utf-8") as f:
            return json.load(f)
    return generate_skeleton()

def save_inventory(inventory: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with PREF_INVENTORY_JSON.open("w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2)
    print(f"Saved prefectural inventory to {PREF_INVENTORY_JSON}")

if __name__ == "__main__":
    inv = load_inventory()
    save_inventory(inv)
    print(f"Total prefectures configured: {len(inv)}")
