# -*- coding: utf-8 -*-
"""
test_v2_200k_pipeline.py — WP1〜WP3 (Vector Ingest, Registry, Domain Segmentation) 結合テスト
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from scripts.v2_200k.ingest_vector import ingest_sheet_polygons, normalize_geometry
from scripts.v2_200k.unit_registry import build_unit_entities, build_polygon_occurrences, compute_spatial_adjacency_matrix
from scripts.v2_200k.domain_segmentation import segment_sheet_domains
from scripts.v2_200k.models import ColumnKind

class TestV2Pipeline(unittest.TestCase):
    def setUp(self):
        self.sheet_code = "NI-53-14"
        self.name_en = "Kyoto-Osaka"
        self.bbox = [34.6666, 135.0, 35.3333, 136.0]
        self.mock_legends = [
            {
                "symbol": "Q3_al",
                "formationAge_en": "Cenozoic Quaternary Holocene",
                "lithology_en": "sand & gravel",
                "group_en": "Quaternary Sediments",
                "formationAge_ja": "完新世",
                "lithology_ja": "砂礫",
                "group_ja": "第四紀堆積物"
            },
            {
                "symbol": "M_ac",
                "formationAge_en": "Mesozoic Jurassic",
                "lithology_en": "chert & sandstone & shale",
                "group_en": "Accretionary Complex",
                "formationAge_ja": "ジュラ紀",
                "lithology_ja": "チャート・砂岩・頁岩",
                "group_ja": "付加体"
            },
            {
                "symbol": "K_gr",
                "formationAge_en": "Mesozoic Cretaceous",
                "lithology_en": "granite",
                "group_en": "Plutonic Rocks",
                "formationAge_ja": "白亜紀",
                "lithology_ja": "花崗岩",
                "group_ja": "深成岩"
            }
        ]

    def test_wp1_to_wp3_end_to_end(self):
        # 1. WP1: ポリゴンインジェスト
        features = ingest_sheet_polygons(self.sheet_code, self.bbox, self.mock_legends)
        self.assertEqual(len(features), 3)

        # 2. WP2: MapUnitEntity & PolygonOccurrence 構築
        unit_entities = build_unit_entities(self.sheet_code, self.mock_legends)
        self.assertEqual(len(unit_entities), 3)
        self.assertIn("Q3_al", unit_entities)

        occurrences = build_polygon_occurrences(self.sheet_code, features, unit_entities)
        self.assertEqual(len(occurrences), 3)
        self.assertTrue(all(occ.area_sq_km > 0 for occ in occurrences))
        self.assertTrue(all(occ.geometry_wkt.startswith("POLYGON") for occ in occurrences))

        # 空間隣接グラフのテスト
        adj = compute_spatial_adjacency_matrix(occurrences)
        self.assertIsInstance(adj, dict)
        self.assertEqual(len(adj), 3)

        # 3. WP3: 地質ドメイン分割 & Column Footprint 算出
        domains = segment_sheet_domains(self.sheet_code, self.name_en, unit_entities, occurrences)
        self.assertEqual(len(domains), 3)

        domain_names = [d.domain_name for d in domains]
        domain_kinds = [d.column_kind for d in domains]

        self.assertIn(ColumnKind.QUATERNARY_COVER, domain_kinds)
        self.assertIn(ColumnKind.ACCRETIONARY_COMPLEX, domain_kinds)
        self.assertIn(ColumnKind.PLUTONIC_COMPLEX, domain_kinds)

        for d in domains:
            self.assertTrue(len(d.footprint_wkt) > 0, "Footprint WKT should not be empty")
            self.assertTrue(d.total_area_sq_km > 0, "Domain total area should be positive")
            self.assertTrue(d.representative_point[0] != 0.0, "Representative point latitude should be non-zero")
            self.assertTrue(d.representative_point[1] != 0.0, "Representative point longitude should be non-zero")

if __name__ == '__main__':
    unittest.main()
