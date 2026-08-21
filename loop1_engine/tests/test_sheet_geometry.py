# -*- coding: utf-8 -*-
"""図幅グリッド導出の回帰テスト。

ここで守っているのは「グリッドの定義そのもの」と「導出結果の健全性」の 2 つ。
前者は測地系変換と 10 分 × 15 分の格子演算、後者は実データに対する
セル重複ゼロ・区画内番号の単調性・既知図幅の座標一致。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import sheet_geometry as sg                                        # noqa: E402

PUB_DIR = ROOT / "loop2_governance" / "data" / "50k" / "raw" / "publication" / "g050"


class TestDatumAndGrid(unittest.TestCase):
    def test_datum_roundtrip(self):
        """Tokyo -> WGS84 -> Tokyo の往復誤差が 1e-6 度以内であること。"""
        for lat, lon in ((43.0, 141.0), (35.5, 139.75), (26.2, 127.7), (40.25, 141.375)):
            wgs = sg.tokyo_to_wgs84(lat, lon)
            back = sg.wgs84_to_tokyo(*wgs)
            self.assertAlmostEqual(back[0], lat, places=6)
            self.assertAlmostEqual(back[1], lon, places=6)

    def test_datum_shift_direction(self):
        """日本付近では WGS84 の方が北・西にずれる（既知の符号）。"""
        lat, lon = sg.tokyo_to_wgs84(40.25, 141.25)
        self.assertGreater(lat, 40.25)
        self.assertLess(lon, 141.25)

    def test_cell_size_is_ten_by_fifteen_minutes(self):
        south, west, north, east = sg.cell_bounds_tokyo(241, 565)
        self.assertAlmostEqual(north - south, 10 / 60, places=9)
        self.assertAlmostEqual(east - west, 15 / 60, places=9)

    def test_cell_of_point_is_stable_inside_the_cell(self):
        """セル内部の任意点は同じセルに落ちる。境界の丸めに引きずられない。"""
        row, col = 241, 565
        south, west, north, east = sg.cell_bounds_tokyo(row, col)
        for flat in (0.2, 0.5, 0.8):
            for flon in (0.2, 0.5, 0.8):
                lat = south + (north - south) * flat
                lon = west + (east - west) * flon
                self.assertEqual(sg.cell_of_wgs84(*sg.tokyo_to_wgs84(lat, lon)), (row, col))

    def test_parent_200k_is_four_by_four(self):
        base_row, base_col = 240, 564
        parents = {sg.parent_200k_cell(base_row + r, base_col + c)
                   for r in range(4) for c in range(4)}
        self.assertEqual(len(parents), 1)
        self.assertNotEqual(sg.parent_200k_cell(base_row + 4, base_col),
                            sg.parent_200k_cell(base_row, base_col))

    def test_parent_bounds_cover_children(self):
        prow, pcol = sg.parent_200k_cell(241, 565)
        south, west, north, east = sg.parent_200k_bounds_wgs84(prow, pcol)
        child = sg.cell_bounds_wgs84(241, 565)
        self.assertLessEqual(south, child[0] + 1e-9)
        self.assertLessEqual(west, child[1] + 1e-9)
        self.assertGreaterEqual(north, child[2] - 1e-9)
        self.assertGreaterEqual(east, child[3] - 1e-9)


@unittest.skipUnless(PUB_DIR.is_dir(), "GSJ 出版物キャッシュが無い環境ではスキップ")
class TestDerivedGrid(unittest.TestCase):
    """実データに対する導出結果の健全性。ここが崩れたら幾何を信用できない。"""

    @classmethod
    def setUpClass(cls):
        cls.grid = sg.build_grid()
        cls.by_code = {s["sheet_code"]: s for s in cls.grid["sheets"]}

    def test_enough_sheets_resolved(self):
        self.assertGreaterEqual(len(self.grid["sheets"]), 850)

    def test_no_two_sheets_share_a_cell(self):
        self.assertEqual(self.grid["validation"]["duplicate_cells"], [])

    def test_region_numbering_is_almost_monotonic(self):
        """区画内番号は北西→東→南に増える。例外は 1% 未満に抑える。"""
        violations = self.grid["validation"]["region_order_violations"]
        self.assertLess(len(violations) / len(self.grid["sheets"]), 0.01,
                        f"順序違反が多すぎる: {violations}")

    def test_zfk_centroids_mostly_agree(self):
        report = self.grid["validation"]
        checked = report["zfk_centroid_checked"]
        self.assertGreater(checked, 100)
        self.assertGreaterEqual((checked - len(report["zfk_centroid_failed"])) / checked, 0.94)

    def test_ichinohe_lands_on_the_published_quadrangle(self):
        """ゴールデン図幅 m1286 一戸は 40°10′-40°20′N / 141°15′-141°30′E。"""
        sheet = self.by_code["05048"]
        self.assertEqual(sheet["latest_map_id"], "1286")
        south, west, north, east = sheet["bbox_wgs84"]
        self.assertAlmostEqual(south, 40.169, places=2)
        self.assertAlmostEqual(west, 141.246, places=2)
        self.assertAlmostEqual(north, 40.336, places=2)
        self.assertAlmostEqual(east, 141.496, places=2)

    def test_towada_is_north_west_of_ichinohe(self):
        towada, ichinohe = self.by_code["05031"], self.by_code["05048"]
        self.assertGreater(towada["grid_row"], ichinohe["grid_row"])
        self.assertLess(towada["grid_col"], ichinohe["grid_col"])

    def test_every_sheet_has_a_positive_extent(self):
        for sheet in self.grid["sheets"]:
            south, west, north, east = sheet["bbox_wgs84"]
            self.assertGreater(north, south, sheet["sheet_code"])
            self.assertGreater(east, west, sheet["sheet_code"])

    def test_all_sheets_are_inside_japan(self):
        for sheet in self.grid["sheets"]:
            south, west, north, east = sheet["bbox_wgs84"]
            self.assertTrue(20.0 <= south <= 46.5, sheet["sheet_code"])
            self.assertTrue(122.0 <= west <= 154.5, sheet["sheet_code"])

    def test_geometry_source_is_always_recorded(self):
        allowed = {"publication_section_geojson", "publication_section_geojson_merged",
                   "region_sequence_interpolation", "region_sequence_repair", "zfk_centroid"}
        for sheet in self.grid["sheets"]:
            self.assertIn(sheet["geometry_source"], allowed)

    def test_written_file_matches_build(self):
        path = ROOT / "loop2_governance" / "config" / "gsj_50k_grid.json"
        if not path.is_file():
            self.skipTest("config/gsj_50k_grid.json が未生成")
        with path.open(encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(len(stored["sheets"]), len(self.grid["sheets"]))


if __name__ == "__main__":
    unittest.main()
