# -*- coding: utf-8 -*-
"""GeoThesaurus-JP: 日本語地質シソーラスの構築と引き当てエンジン。

日本の地質報告書・図幅に登場する「地層名」「岩相」「堆積環境」「地質時代」の
表記揺れ（シノニム）と日英対訳を管理し、MacroStrat公式スキーマへ正規化する。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
THESAURUS_PATH = CONFIG_DIR / "geothesaurus_jp.json"
VOCAB_PATH = CONFIG_DIR / "vocab.json"

API_BASE = "https://macrostrat.org/api/v2/defs"
UA = {"User-Agent": "MacroStrat-GeoThesaurus-JP/1.0"}

# ---------------------------------------------------------------------------
# 基礎対訳マスター辞書（岩相・環境・時代・ランク）
# ---------------------------------------------------------------------------

LITHOLOGY_JA_EN = {
    # 堆積岩 (Sedimentary)
    "泥岩": "mudstone",
    "頁岩": "shale",
    "粘板岩": "slate",
    "シルト岩": "siltstone",
    "砂岩": "sandstone",
    "粗粒砂岩": "coarse sandstone",
    "中粒砂岩": "medium sandstone",
    "細粒砂岩": "fine sandstone",
    "礫岩": "conglomerate",
    "角礫岩": "breccia",
    "石灰岩": "limestone",
    "苦灰岩": "dolostone",
    "ドロマイト": "dolostone",
    "チャート": "chert",
    "珪質泥岩": "siliceous mudstone",
    "珪藻土": "diatomite",
    "泥炭": "peat",
    "石炭": "coal",
    
    # 火山砕屑岩 (Pyroclastic)
    "凝灰岩": "tuff",
    "軽石凝灰岩": "pumice tuff",
    "火山灰": "volcanic ash",
    "凝灰角礫岩": "tuff breccia",
    "火山角礫岩": "volcanic breccia",
    "集塊岩": "agglomerate",
    "凝灰質砂岩": "tuffaceous sandstone",
    "凝灰質泥岩": "tuffaceous mudstone",
    "火砕流堆積物": "pyroclastic flow deposit",
    
    # 火成岩 (Igneous)
    "安山岩": "andesite",
    "玄武岩": "basalt",
    "デイサイト": "dacite",
    "石英安山岩": "dacite",
    "流紋岩": "rhyolite",
    "花崗岩": "granite",
    "閃緑岩": "diorite",
    "斑れい岩": "gabbro",
    "斑糲岩": "gabbro",
    "かんらん岩": "peridotite",
    "橄欖岩": "peridotite",
    "蛇紋岩": "serpentinite",
    "花崗閃緑岩": "granodiorite",
    
    # 変成岩 (Metamorphic)
    "片麻岩": "gneiss",
    "結晶片岩": "schist",
    "片岩": "schist",
    "千枚岩": "phyllite",
    "ホルンフェルス": "hornfels",
    "大理石": "marble",
    "結晶質石灰岩": "marble",
    "珪岩": "quartzite",
    
    # 表層・未固結堆積物 (Unconsolidated)
    "砂": "sand",
    "泥": "mud",
    "粘土": "clay",
    "シルト": "silt",
    "礫": "gravel",
    "ローム": "loam",
    "沖積層": "alluvium",
    "段丘堆積物": "terrace deposit",
    "扇状地堆積物": "alluvial fan deposit",
    "崖錐堆積物": "talus deposit"
}

ENVIRONMENT_JA_EN = {
    # 海成 (Marine)
    "浅海": "shallow marine",
    "内湾": "bay",
    "沿岸": "coastal",
    "干潟": "tidal flat",
    "潮間帯": "intertidal",
    "サンゴ礁": "reef",
    "礁": "reef",
    "大陸棚": "continental shelf",
    "外洋": "open marine",
    "半深海": "bathyal",
    "深海": "deep marine",
    "海溝": "trench",
    "タービダイト": "submarine fan",
    
    # 陸成・遷移 (Terrestrial & Transitional)
    "陸成": "terrestrial",
    "河川": "fluvial",
    "氾濫原": "floodplain",
    "扇状地": "alluvial fan",
    "三角州": "deltaic",
    "デルタ": "deltaic",
    "湖沼": "lacustrine",
    "湖": "lacustrine",
    "湿原": "marsh",
    "沼沢": "swamp",
    "汽水": "brackish",
    "河口": "estuarine",
    "風成": "eolian",
    "砂丘": "dune",
    "氷河": "glacial",
    
    # 火山 (Volcanic)
    "火山": "volcanic",
    "カルデラ": "caldera",
    "クレーター": "crater",
    "火砕流": "pyroclastic",
    "海底火山": "submarine volcanic"
}

INTERVAL_JA_EN = {
    # 新生代 (Cenozoic)
    "第四紀": {"en": "Quaternary", "b_age": 2.58, "t_age": 0.0},
    "完新世": {"en": "Holocene", "b_age": 0.0117, "t_age": 0.0},
    "更新世": {"en": "Pleistocene", "b_age": 2.58, "t_age": 0.0117},
    "後期更新世": {"en": "Late Pleistocene", "b_age": 0.129, "t_age": 0.0117},
    "中期更新世": {"en": "Middle Pleistocene", "b_age": 0.774, "t_age": 0.129},
    "前期更新世": {"en": "Early Pleistocene", "b_age": 2.58, "t_age": 0.774},
    "新第三紀": {"en": "Neogene", "b_age": 23.03, "t_age": 2.58},
    "鮮新世": {"en": "Pliocene", "b_age": 5.333, "t_age": 2.58},
    "中新世": {"en": "Miocene", "b_age": 23.03, "t_age": 5.333},
    "後期中新世": {"en": "Late Miocene", "b_age": 11.63, "t_age": 5.333},
    "中期中新世": {"en": "Middle Miocene", "b_age": 15.97, "t_age": 11.63},
    "前期中新世": {"en": "Early Miocene", "b_age": 23.03, "t_age": 15.97},
    "古第三紀": {"en": "Paleogene", "b_age": 66.0, "t_age": 23.03},
    "漸新世": {"en": "Oligocene", "b_age": 33.9, "t_age": 23.03},
    "始新世": {"en": "Eocene", "b_age": 56.0, "t_age": 33.9},
    "暁新世": {"en": "Paleocene", "b_age": 66.0, "t_age": 56.0},
    
    # 中生代 (Mesozoic)
    "中生代": {"en": "Mesozoic", "b_age": 251.9, "t_age": 66.0},
    "白亜紀": {"en": "Cretaceous", "b_age": 145.0, "t_age": 66.0},
    "後期白亜紀": {"en": "Late Cretaceous", "b_age": 100.5, "t_age": 66.0},
    "前期白亜紀": {"en": "Early Cretaceous", "b_age": 145.0, "t_age": 100.5},
    "ジュラ紀": {"en": "Jurassic", "b_age": 201.4, "t_age": 145.0},
    "三畳紀": {"en": "Triassic", "b_age": 251.9, "t_age": 201.4},
    
    # 古生代 (Paleozoic)
    "古生代": {"en": "Paleozoic", "b_age": 538.8, "t_age": 251.9},
    "ペルム紀": {"en": "Permian", "b_age": 298.9, "t_age": 251.9},
    "二畳紀": {"en": "Permian", "b_age": 298.9, "t_age": 251.9},
    "石炭紀": {"en": "Carboniferous", "b_age": 358.9, "t_age": 298.9},
    "デボン紀": {"en": "Devonian", "b_age": 419.2, "t_age": 358.9},
    "泥見紀": {"en": "Devonian", "b_age": 419.2, "t_age": 358.9},
    "シルル紀": {"en": "Silurian", "b_age": 443.8, "t_age": 419.2},
    "志留紀": {"en": "Silurian", "b_age": 443.8, "t_age": 419.2},
    "オルドビス紀": {"en": "Ordovician", "b_age": 485.4, "t_age": 443.8},
    "カンブリア紀": {"en": "Cambrian", "b_age": 538.8, "t_age": 485.4}
}

STRAT_RANK_PATTERNS = [
    (r"(?:累層群|層群|Group)$", "Group"),
    (r"(?:部層|メンバー|Member)$", "Member"),
    (r"(?:単層|層準|Bed)$", "Bed"),
    (r"(?:累層|層|フォーメーション|Formation)$", "Formation"),
    (r"(?:コンプレックス|複合体|Complex)$", "Complex"),
    (r"(?:岩体|深成岩体|貫入岩体|Pluton|Body)$", "Pluton"),
    (r"(?:溶岩|Lava)$", "Lava"),
    (r"(?:火砕流|火砕流堆積物|Pyroclastic Flow)$", "Pyroclastic Flow"),
    (r"(?:堆積物|段丘堆積物|Deposits|Terrace)$", "Deposits")
]

# ---------------------------------------------------------------------------
# シソーラス構築関数
# ---------------------------------------------------------------------------

def fetch_macrostrat_defs(endpoint: str) -> list[dict[str, Any]]:
    url = f"{API_BASE}/{endpoint}?all=1&format=json"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("success", {}).get("data", [])
    except Exception as e:
        print(f"[WARN] Failed to fetch {endpoint} from MacroStrat: {e}")
        return []

def build_thesaurus() -> dict[str, Any]:
    """完全な GeoThesaurus-JP マスターデータを構築する"""
    print("Building GeoThesaurus-JP...")
    
    # 1. MacroStrat 公式語彙の取得
    ms_lith = fetch_macrostrat_defs("lithologies")
    ms_env = fetch_macrostrat_defs("environments")
    
    thesaurus = {
        "schema_version": "geothesaurus-jp/1.0",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "description": "Japanese Geological Thesaurus for MacroStrat Normalization",
        "lithology": {
            "ja_to_en": LITHOLOGY_JA_EN,
            "macrostrat_official_count": len(ms_lith)
        },
        "environment": {
            "ja_to_en": ENVIRONMENT_JA_EN,
            "macrostrat_official_count": len(ms_env)
        },
        "geochronology": {
            "ja_to_interval": INTERVAL_JA_EN
        },
        "stratigraphic_ranks": [
            {"pattern": p, "rank": r} for p, r in STRAT_RANK_PATTERNS
        ]
    }
    
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with THESAURUS_PATH.open("w", encoding="utf-8") as f:
        json.dump(thesaurus, f, ensure_ascii=False, indent=2)
        
    print(f"GeoThesaurus-JP successfully saved to: {THESAURUS_PATH}")
    return thesaurus

# ---------------------------------------------------------------------------
# 高速引き当て API
# ---------------------------------------------------------------------------

class GeoThesaurus:
    def __init__(self, data: Mapping[str, Any] | None = None):
        if data is None:
            if not THESAURUS_PATH.exists():
                data = build_thesaurus()
            else:
                with THESAURUS_PATH.open(encoding="utf-8") as f:
                    data = json.load(f)
        self.data = data
        self.lith_map = data.get("lithology", {}).get("ja_to_en", {})
        self.env_map = data.get("environment", {}).get("ja_to_en", {})
        self.time_map = data.get("geochronology", {}).get("ja_to_interval", {})
        
    def lookup_lithology(self, text: str) -> str | None:
        if not text:
            return None
        text_clean = text.strip()
        
        # 1. 日本語完全・部分一致
        if text_clean in self.lith_map:
            return self.lith_map[text_clean]
        for k in sorted(self.lith_map.keys(), key=len, reverse=True):
            if k in text_clean:
                return self.lith_map[k]
                
        # 2. 英語キーワード一致
        en_patterns = [
            (r"\bSandstone\b", "sandstone"),
            (r"\bMudstone\b", "mudstone"),
            (r"\bSiltstone\b", "siltstone"),
            (r"\bConglomerate\b", "conglomerate"),
            (r"\bBreccia\b", "breccia"),
            (r"\bLimestone\b", "limestone"),
            (r"\bChert\b", "chert"),
            (r"\bTuff\b", "tuff"),
            (r"\bPyroclastic\b", "pyroclastic"),
            (r"\bAndesite\b", "andesite"),
            (r"\bBasalt\b", "basalt"),
            (r"\bDacite\b", "dacite"),
            (r"\bRhyolite\b", "rhyolite"),
            (r"\bGranite\b", "granite"),
            (r"\bPluton\b", "plutonic"),
            (r"\bVolcanic\b", "volcanic rock"),
            (r"\bTerrace\b", "terrace deposit"),
            (r"\bAlluvial\b", "alluvium"),
            (r"\bFan Deposits\b", "alluvial fan deposit"),
            (r"\bFloodplain\b", "floodplain deposit"),
            (r"\bLandslide\b", "landslide deposit"),
            (r"\bRiver bed\b", "alluvium")
        ]
        for pat, lith in en_patterns:
            if re.search(pat, text_clean, re.IGNORECASE):
                return lith
                
        return None

    def lookup_environment(self, text_ja: str) -> str | None:
        if not text_ja:
            return None
        text_clean = text_ja.strip()
        if text_clean in self.env_map:
            return self.env_map[text_clean]
        for k in sorted(self.env_map.keys(), key=len, reverse=True):
            if k in text_clean:
                return self.env_map[k]
        return None

    def lookup_interval(self, text_ja: str) -> dict[str, Any] | None:
        if not text_ja:
            return None
        text_clean = text_ja.strip()
        if text_clean in self.time_map:
            return self.time_map[text_clean]
        for k in sorted(self.time_map.keys(), key=len, reverse=True):
            if k in text_clean:
                return self.time_map[k]
        return None

    def detect_rank(self, strat_name: str) -> str:
        for p, r in STRAT_RANK_PATTERNS:
            if re.search(p, strat_name.strip(), re.IGNORECASE):
                return r
        return "Formation"

if __name__ == "__main__":
    t = build_thesaurus()
    engine = GeoThesaurus(t)
    
    # 動作確認テスト
    print("\n=== Thesaurus Lookup Test ===")
    print("泥岩 ->", engine.lookup_lithology("泥岩"))
    print("軽石凝灰岩層 ->", engine.lookup_lithology("軽石凝灰岩層"))
    print("浅海成 ->", engine.lookup_environment("浅海成"))
    print("前期中新世 ->", engine.lookup_interval("前期中新世"))
    print("門ノ沢層 rank ->", engine.detect_rank("門ノ沢層"))
    print("末の松山層群 rank ->", engine.detect_rank("末の松山層群"))
