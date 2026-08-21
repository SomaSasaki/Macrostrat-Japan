# -*- coding: utf-8 -*-
"""
test_v2_200k_models.py — WP0 データモデル・DAG非循環検証の単体テスト
"""

import unittest
from scripts.v2_200k.models import (
    ColumnStatus, ColumnKind, RelationType, BasalSurfaceType,
    EvidenceRecord, MapUnitEntity, PolygonOccurrence,
    TopologyEdge, GeologicDomain, ReviewDecision, ColumnGraph
)

class TestV2Models(unittest.TestCase):
    def test_map_unit_entity_serialization(self):
        unit = MapUnitEntity(
            unit_id="m200k_NI_53_14_u001",
            symbol="Q3_al",
            name_ja="完新世 沖積層",
            name_en="Holocene Alluvium",
            b_int="Holocene",
            t_int="Holocene",
            b_age_ma=0.0117,
            t_age_ma=0.0,
            lithology="alluvium",
            minor_lith="gravel",
            environment="fluvial",
            group_ja="第四紀堆積物",
            group_en="Quaternary Sediments"
        )
        d = unit.to_dict()
        self.assertEqual(d["unit_id"], "m200k_NI_53_14_u001")
        self.assertEqual(d["symbol"], "Q3_al")
        self.assertEqual(d["lithology"], "alluvium")

    def test_dag_cycle_detection(self):
        u1 = MapUnitEntity("u1", "sym1", "古生代 砂岩", "Paleozoic Sandstone", "Permian", "Permian", 298.9, 251.9, "sandstone")
        u2 = MapUnitEntity("u2", "sym2", "中生代 泥岩", "Mesozoic Mudstone", "Jurassic", "Jurassic", 201.4, 145.0, "mudstone")
        u3 = MapUnitEntity("u3", "sym3", "新生代 礫岩", "Cenozoic Conglomerate", "Neogene", "Neogene", 23.03, 2.58, "conglomerate")

        # 1. 正常な非循環 DAG: u1 -> u2 -> u3
        e1 = TopologyEdge("e1", "u1", "u2", RelationType.STRATIGRAPHIC_OVER, BasalSurfaceType.UNCONFORMITY)
        e2 = TopologyEdge("e2", "u2", "u3", RelationType.STRATIGRAPHIC_OVER, BasalSurfaceType.CONFORMABLE)

        col = ColumnGraph(
            col_id="col_1",
            col_group="TEST_GRP",
            col_name="Test Column",
            sheet_code="NI-53-14",
            domain_id="dom_1",
            column_kind=ColumnKind.SEDIMENTARY_SUCCESSION,
            footprint_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
            representative_point=(35.0, 135.0),
            b_int="Permian",
            t_int="Neogene",
            b_age_ma=298.9,
            t_age_ma=2.58,
            units=[u1, u2, u3],
            edges=[e1, e2]
        )
        is_valid, cycles = col.validate_dag()
        self.assertTrue(is_valid)
        self.assertEqual(len(cycles), 0)

        # 2. 循環（Cycle）が発生した場合: u3 -> u1 を追加
        e_cycle = TopologyEdge("e_bad", "u3", "u1", RelationType.STRATIGRAPHIC_OVER)
        col.edges.append(e_cycle)
        is_valid, cycles = col.validate_dag()
        self.assertFalse(is_valid)
        self.assertGreater(len(cycles), 0)
        self.assertIn("Cycle detected", cycles[0])

    def test_review_decision_immutability(self):
        dec = ReviewDecision(
            decision_id="dec_001",
            sheet_code="NI-53-14",
            target_type="edge",
            target_id="e1",
            action="change_contact",
            original_value="unknown",
            reviewed_value="faulted",
            reviewer="expert_soma",
            timestamp="2026-08-14T00:00:00Z",
            rationale="地質調査総合センター図幅解説書 p.42 の断層記載に基づく"
        )
        d = dec.to_dict()
        self.assertEqual(d["reviewed_value"], "faulted")
        self.assertEqual(d["reviewer"], "expert_soma")

if __name__ == '__main__':
    unittest.main()
