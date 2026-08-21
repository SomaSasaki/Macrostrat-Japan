from pathlib import Path
# -*- coding: utf-8 -*-
"""
scripts.v2_200k.unit_registry — MapUnit Registry & PolygonOccurrence 構築エンジン (WP2)

機能:
- 図幅内の地質フィーチャから MapUnitEntity (単元台帳) を構築
- 各実ポリゴンから PolygonOccurrence (幾何形状・面積・重心) を抽出
- ポリゴン同士の空間隣接関係 (SpatialContactGraph) を算出
"""

import json
import os
import sys
from typing import List, Dict, Any, Tuple, Optional

try:
    from shapely.geometry import shape, Point
    from shapely import wkt
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, BASE_DIR)

from scripts.v2_200k.models import MapUnitEntity, PolygonOccurrence
from scripts.v2_200k.geometry_engine import compute_geometry_properties
import scripts.make_review_200k as make_rev
import scripts.common as common

VOCAB_PATH = os.path.join(Path(__file__).resolve().parents[3], 'loop2_governance', 'config', 'vocab.json')
with open(VOCAB_PATH, 'r', encoding='utf-8') as f:
    vocab_data = json.load(f)
lith_vocab = {v.lower(): v for v in vocab_data.get('lithology', [])}

def build_unit_entities(sheet_code: str, legends: List[Dict[str, Any]]) -> Dict[str, MapUnitEntity]:
    """
    凡例リストから一意な MapUnitEntity 辞書 (symbol -> MapUnitEntity) を構築
    """
    entities: Dict[str, MapUnitEntity] = {}
    
    for idx, leg in enumerate(legends, start=1):
        sym = leg.get('symbol', f"U_{idx}")
        age_en = leg.get('formationAge_en', '')
        lith_en = leg.get('lithology_en', '')
        grp_en = leg.get('group_en', '')
        age_ja = leg.get('formationAge_ja', '')
        lith_ja = leg.get('lithology_ja', '')
        grp_ja = leg.get('group_ja', '')

        b_int, t_int, bp, tp, b_age, t_age = make_rev.parse_seamless_age(age_en)
        main_lith, minor_lith = make_rev.extract_macrostrat_lithologies(lith_en)
        env = make_rev.infer_environment(grp_en, lith_en)

        unit_id = f"m200k_{sheet_code.replace('-', '_')}_u{str(idx).zfill(3)}"
        name_ja = f"{age_ja} {lith_ja}".strip()
        name_en = f"{t_int} {main_lith.capitalize()}".strip()

        entity = MapUnitEntity(
            unit_id=unit_id,
            symbol=sym,
            name_ja=name_ja,
            name_en=name_en,
            b_int=b_int,
            t_int=t_int,
            b_age_ma=b_age,
            t_age_ma=t_age,
            lithology=main_lith,
            minor_lith=minor_lith,
            environment=env,
            group_ja=grp_ja,
            group_en=grp_en,
            description_ja=f"{age_ja}（{age_en}）に形成された{lith_ja}（{lith_en}）。",
            description_en=f"{age_en}; {lith_en}",
            source_symbol_level="detailed" if len(sym) > 3 else "basic"
        )
        entities[sym] = entity

    return entities

def build_polygon_occurrences(
    sheet_code: str,
    features: List[Dict[str, Any]],
    unit_entities: Dict[str, MapUnitEntity]
) -> List[PolygonOccurrence]:
    """
    ポリゴンフィーチャ群から PolygonOccurrence リストを抽出
    """
    occurrences: List[PolygonOccurrence] = []

    for idx, feat in enumerate(features, start=1):
        props = feat.get('properties', {})
        sym = props.get('symbol', '')
        poly_id = props.get('poly_id', f"poly_{idx}")
        occ_id = f"{sheet_code.replace('-', '_')}_{poly_id}"

        # 該当する MapUnitEntity の取得
        unit = unit_entities.get(sym)
        unit_id = unit.unit_id if unit else f"m200k_{sheet_code.replace('-', '_')}_unmapped_{idx}"

        geom_obj = feat.get('geometry')
        wkt_str, area_km2, centroid = compute_geometry_properties(geom_obj)

        occ = PolygonOccurrence(
            occurrence_id=occ_id,
            unit_id=unit_id,
            sheet_code=sheet_code,
            geometry_wkt=wkt_str,
            area_sq_km=area_km2,
            centroid=centroid,
            domain_id=None,
            is_major_occurrence=area_km2 > 0.01
        )
        occurrences.append(occ)

    return occurrences

def compute_spatial_adjacency_matrix(occurrences: List[PolygonOccurrence]) -> Dict[str, List[str]]:
    """
    ポリゴン同士の空間隣接 (touches / intersects) グラフを算出
    """
    from scripts.v2_200k.geometry_engine import compute_spatial_adjacency
    return compute_spatial_adjacency(occurrences)
