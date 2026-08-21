# -*- coding: utf-8 -*-
"""ダッシュボード索引の組み立てと、配信サーバのパス防御のテスト。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard_data as dd                                        # noqa: E402
import dashboard_server as ds                                      # noqa: E402

GRID = ROOT / "loop2_governance" / "config" / "gsj_50k_grid.json"


class TestClassification(unittest.TestCase):
    def test_source_badges_follow_the_four_documented_cases(self):
        self.assertEqual(dd._source_badge(True, False, True), "zfk_pdf")
        self.assertEqual(dd._source_badge(False, True, True), "shape_pdf")
        self.assertEqual(dd._source_badge(False, False, True), "pdf_only")
        self.assertEqual(dd._source_badge(False, False, False), "none")

    def test_vector_without_pdf_is_not_reported_as_pdf(self):
        self.assertEqual(dd._source_badge(True, False, False), "vector_only")

    def test_stage_precedence(self):
        self.assertEqual(dd._stage(True, True, True, True, True), "submitted")
        self.assertEqual(dd._stage(False, True, True, True, True), "review")
        self.assertEqual(dd._stage(False, False, False, False, False), "unpublished")
        self.assertEqual(dd._stage(False, False, True, True, False), "vector_ready")
        self.assertEqual(dd._stage(False, False, True, False, True), "pdf_only")
        self.assertEqual(dd._stage(False, False, True, False, False), "no_source")

    def test_completion_is_a_ratio_over_required_fields(self):
        stats = {"units": 10, "filled": {"unit_name": 10, "lithology": 10,
                                         "t_int": 5, "b_int": 5}}
        self.assertAlmostEqual(dd._completion(stats), 0.75)

    def test_completion_of_an_empty_workbook_is_zero(self):
        self.assertEqual(dd._completion(None), 0.0)
        self.assertEqual(dd._completion({"units": 0, "filled": {}}), 0.0)

    def test_completion_never_exceeds_one(self):
        stats = {"units": 2, "filled": {"unit_name": 9, "lithology": 9,
                                        "t_int": 9, "b_int": 9}}
        self.assertLessEqual(dd._completion(stats), 1.0)

    def test_titles_are_reduced_to_the_sheet_name(self):
        self.assertEqual(dd._clean_title("5万分の1地質図幅「一戸」 (2018)"), "一戸")
        self.assertEqual(dd._clean_title_en("1:50K GeoMap 'Ichinohe' (2018)"), "Ichinohe")
        self.assertEqual(dd._clean_title(""), "")

    def test_union_bbox_covers_every_member(self):
        members = [{"bbox": [35.0, 139.0, 35.2, 139.3]},
                   {"bbox": [34.5, 139.5, 34.8, 139.9]}]
        self.assertEqual(dd._union_bbox(members), [34.5, 139.0, 35.2, 139.9])


@unittest.skipUnless(GRID.is_file(), "config/gsj_50k_grid.json が未生成の環境ではスキップ")
class TestBuildIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = dd.build_index()

    def test_every_sheet_carries_what_the_map_needs(self):
        for sheet in self.payload["sheets"]:
            for key in ("sheet_code", "bbox", "stage", "badge", "region_code"):
                self.assertIn(key, sheet)
            self.assertEqual(len(sheet["bbox"]), 4)

    def test_totals_match_the_sheet_list(self):
        totals = self.payload["totals"]
        self.assertEqual(totals["sheets"], len(self.payload["sheets"]))
        self.assertEqual(sum(totals["by_stage"].values()), len(self.payload["sheets"]))
        self.assertEqual(sum(totals["by_badge"].values()), len(self.payload["sheets"]))

    def test_region_totals_sum_to_the_national_total(self):
        self.assertEqual(sum(r["sheets"] for r in self.payload["regions"]),
                         self.payload["totals"]["sheets"])

    def test_parents_are_the_200k_aggregation(self):
        self.assertLess(len(self.payload["parents"]), len(self.payload["sheets"]))
        self.assertGreater(len(self.payload["parents"]), 40)

    def test_stage_keys_are_the_declared_ones(self):
        declared = {s["key"] for s in self.payload["stages"]}
        self.assertEqual({s["stage"] for s in self.payload["sheets"]} - declared, set())


class TestServerPathGuard(unittest.TestCase):
    """/files/ はリポジトリ配下の成果物だけを返す。ここが緩むと情報が漏れる。"""

    def test_traversal_is_rejected(self):
        for path in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd",
                     "data/50k/../../../etc/passwd"):
            self.assertIsNone(ds._safe_file(path), path)

    def test_disallowed_root_is_rejected(self):
        self.assertIsNone(ds._safe_file("run.py"))
        self.assertIsNone(ds._safe_file("specs/TASK.md"))

    def test_disallowed_suffix_is_rejected(self):
        self.assertIsNone(ds._safe_file("config/secret.json.py"))

    def test_empty_and_null_bytes_are_rejected(self):
        self.assertIsNone(ds._safe_file(""))
        self.assertIsNone(ds._safe_file("data/50k/a\x00.png"))

    def test_an_allowed_existing_artifact_resolves(self):
        target = ROOT / "loop2_governance" / "config" / "vocab.json"
        if not target.is_file():
            self.skipTest("config/vocab.json が無い")
        self.assertEqual(ds._safe_file("config/vocab.json"), target.resolve())


if __name__ == "__main__":
    unittest.main()
