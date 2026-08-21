# -*- coding: utf-8 -*-
"""
scripts.v2_200k.domain_segmentation — 地質ドメイン分割 & Column Footprint 算出エンジン (WP3)

機能:
- テクトノ層序大分類 (Tectonostratigraphic Domain) と空間連結成分によるドメイン分割
- 各ドメインの実ポリゴン群から Union Footprint MultiPolygon WKT を算出 (図幅矩形付与の完全廃止)
- 代表点 (Representative PointOnSurface) の算出
- ColumnKind の自動判定
"""

import json
import os
import sys
from typing import List, Dict, Any, Tuple, Optional

try:
    from shapely.geometry import shape, Point, Polygon, MultiPolygon
    from shapely.ops import unary_union
    from shapely import wkt
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, BASE_DIR)

from scripts.v2_200k.models import (
    MapUnitEntity, PolygonOccurrence, GeologicDomain, ColumnKind
)
from scripts.v2_200k.geometry_engine import union_wkt_geometries
import scripts.make_review_200k as make_rev

def determine_column_kind(domain_name: str, grp_en: str, lith_en: str) -> ColumnKind:
    """
    ドメイン名および岩相・グループ名から ColumnKind を判定
    """
    text = (domain_name + " " + grp_en + " " + lith_en).lower()
    if 'accretionary' in text:
        return ColumnKind.ACCRETIONARY_COMPLEX
    if 'metamorphic' in text or 'schist' in text or 'gneiss' in text:
        return ColumnKind.METAMORPHIC_BELT
    if 'plutonic' in text or 'granit' in text or 'gabbro' in text:
        return ColumnKind.PLUTONIC_COMPLEX
    if 'volcanic' in text or 'pyroclastic' in text or 'lava' in text or 'basalt' in text or 'andesite' in text:
        return ColumnKind.VOLCANIC_ARC
    if 'terrace' in text or 'alluvium' in text or 'quaternary basin' in text or 'gravel' in text:
        return ColumnKind.QUATERNARY_COVER
    if 'marine' in text:
        return ColumnKind.MARINE_SUCCESSION
    return ColumnKind.SEDIMENTARY_SUCCESSION

def segment_sheet_domains(
    sheet_code: str,
    name_en: str,
    unit_entities: Dict[str, MapUnitEntity],
    occurrences: List[PolygonOccurrence]
) -> List[GeologicDomain]:
    """
    図幅内の単元・ポリゴンから地質ドメイン (GeologicDomain) を分割構築
    """
    # 1. 凡例属性による大分類グループ化
    unit_to_domain_map: Dict[str, str] = {}
    domain_units_map: Dict[str, List[str]] = {}
    domain_kinds_map: Dict[str, ColumnKind] = {}

    for sym, unit in unit_entities.items():
        leg_mock = {
            'group_en': unit.group_en,
            'formationAge_en': f"{unit.b_int} to {unit.t_int}",
            'lithology_en': unit.lithology
        }
        dom_name = make_rev.classify_legend_domain(leg_mock)
        col_kind = determine_column_kind(dom_name, unit.group_en, unit.lithology)

        unit_to_domain_map[unit.unit_id] = dom_name
        domain_units_map.setdefault(dom_name, []).append(unit.unit_id)
        domain_kinds_map[dom_name] = col_kind

    # 2. 各ドメインに属する PolygonOccurrence の集約と Footprint 算出
    domains: List[GeologicDomain] = []
    
    for d_idx, (dom_name, u_ids) in enumerate(domain_units_map.items(), start=1):
        dom_clean = dom_name.replace(' ', '_').replace('&', 'and')
        dom_id = f"dom_{sheet_code.replace('-', '_')}_{dom_clean}"
        col_kind = domain_kinds_map[dom_name]

        # 該当ポリゴンの抽出
        dom_occs = [occ for occ in occurrences if occ.unit_id in u_ids]
        occ_ids = [occ.occurrence_id for occ in dom_occs]

        # ドメインIDを occurrence へ設定
        for occ in dom_occs:
            occ.domain_id = dom_id

        # 実ポリゴン群の Union Footprint 算出
        wkt_list = [occ.geometry_wkt for occ in dom_occs if occ.geometry_wkt]
        footprint_wkt_str, rep_point = union_wkt_geometries(wkt_list)
        total_area = sum(occ.area_sq_km for occ in dom_occs)

        domain_obj = GeologicDomain(
            domain_id=dom_id,
            sheet_code=sheet_code,
            domain_name=f"{name_en} {dom_name}",
            column_kind=col_kind,
            footprint_wkt=footprint_wkt_str,
            representative_point=rep_point,
            total_area_sq_km=round(total_area, 4),
            unit_ids=u_ids,
            occurrence_ids=occ_ids,
            confidence=1.0
        )
        domains.append(domain_obj)

    return domains
