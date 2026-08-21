# -*- coding: utf-8 -*-
"""不変条件監査ロジックの単体テスト。

実データに依存せず、違反を意図的に仕込んだ行で各ルールを 1 つずつ検証する。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import invariant_audit as audit                                    # noqa: E402

INTERVALS = {
    "Holocene": {"b_age": 0.0117, "t_age": 0.0},
    "Pleistocene": {"b_age": 2.58, "t_age": 0.0117},
    "Jurassic": {"b_age": 201.4, "t_age": 143.1},
}
VOCAB = {"lithology": ["sandstone", "mudstone", "tuff"],
         "environment": ["fluvial indet.", "floodplain"]}


def rules(findings, rule):
    return [f for f in findings if f["rule"] == rule]


class TestMonotonicity(unittest.TestCase):
    def test_reversed_ages_are_an_error(self):
        units = [{"unit_id": "m1_p001", "b_age_ma": 1.0, "t_age_ma": 5.0}]
        found = rules(audit.check_monotonicity(units, INTERVALS), "monotonicity")
        self.assertTrue(any(f["severity"] == "error" for f in found))

    def test_equal_ages_are_allowed(self):
        units = [{"unit_id": "m1_p001", "b_age_ma": 2.0, "t_age_ma": 2.0}]
        self.assertEqual([f for f in audit.check_monotonicity(units, INTERVALS)
                          if f["severity"] == "error"], [])

    def test_negative_age_is_an_error(self):
        units = [{"unit_id": "m1_p001", "b_age_ma": 3.0, "t_age_ma": -0.5}]
        found = audit.check_monotonicity(units, INTERVALS)
        self.assertTrue(any(f["severity"] == "error" and f["field"] == "t_age_ma" for f in found))

    def test_unknown_interval_name_is_a_warning(self):
        units = [{"unit_id": "m1_p001", "b_int": "Sarumatian", "t_int": "Holocene"}]
        found = audit.check_monotonicity(units, INTERVALS)
        self.assertTrue(any(f["severity"] == "warning" and f["field"] == "b_int" for f in found))

    def test_interval_order_is_checked(self):
        units = [{"unit_id": "m1_p001", "b_int": "Holocene", "t_int": "Jurassic"}]
        found = audit.check_monotonicity(units, INTERVALS)
        self.assertTrue(any(f["severity"] == "error" for f in found))

    def test_numeric_age_outside_its_interval_is_flagged(self):
        units = [{"unit_id": "m1_p001", "b_int": "Holocene", "b_age_ma": 120.0}]
        found = audit.check_monotonicity(units, INTERVALS)
        self.assertTrue(any(f["field"] == "b_age_ma" and f["severity"] == "warning" for f in found))

    def test_empty_row_produces_nothing(self):
        self.assertEqual(audit.check_monotonicity([{"unit_id": "m1_p001"}], INTERVALS), [])


class TestEvidence(unittest.TestCase):
    def test_value_without_any_evidence_is_an_error(self):
        units = [{"unit_id": "m1_p001", "b_int": "Jurassic"}]
        found = rules(audit.check_evidence(units, {}), "evidence")
        self.assertTrue(any(f["severity"] == "error" for f in found))

    def test_quote_in_the_evidence_sheet_satisfies_the_rule(self):
        units = [{"unit_id": "m1_p001", "b_int": "Jurassic"}]
        evidence = {("m1_p001", "b_int"): [
            {"source_and_full_context": "本層からはジュラ紀の放散虫化石が産出する。"}]}
        self.assertEqual(rules(audit.check_evidence(units, evidence), "evidence"), [])

    def test_inline_evidence_only_is_downgraded_to_warning(self):
        units = [{"unit_id": "m1_p001", "b_int": "Jurassic",
                  "age_evidence": "[C | PDF | English ABSTRACT]"}]
        found = rules(audit.check_evidence(units, {}), "evidence")
        self.assertTrue(found and all(f["severity"] == "warning" for f in found))


class TestVocabulary(unittest.TestCase):
    def test_unknown_term_is_a_warning(self):
        units = [{"unit_id": "m1_p001", "lithology": "sandstone; kandanstone"}]
        found = rules(audit.check_vocabulary(units, VOCAB), "vocabulary")
        self.assertEqual(len(found), 1)
        self.assertIn("kandanstone", found[0]["message"])

    def test_known_terms_pass_and_splitting_uses_semicolons(self):
        units = [{"unit_id": "m1_p001", "lithology": "sandstone; mudstone",
                  "environment": "floodplain"}]
        self.assertEqual(audit.check_vocabulary(units, VOCAB), [])

    def test_case_and_spacing_are_tolerated(self):
        units = [{"unit_id": "m1_p001", "lithology": " Sandstone ;TUFF"}]
        self.assertEqual(audit.check_vocabulary(units, VOCAB), [])


class TestUnitIds(unittest.TestCase):
    def test_duplicate_unit_id_is_an_error(self):
        units = [{"unit_id": "m1_p001"}, {"unit_id": "m1_p001"}]
        with tempfile.TemporaryDirectory() as tmp:
            found = audit.check_unit_ids(units, Path(tmp) / "reg.json", write=False)
        self.assertTrue(any(f["severity"] == "error" for f in found))

    def test_disappearing_unit_id_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "reg.json"
            audit.check_unit_ids([{"unit_id": "m1_p001"}, {"unit_id": "m1_p002"}],
                                 registry, write=True)
            found = audit.check_unit_ids([{"unit_id": "m1_p001"}], registry, write=False)
        self.assertTrue(any(f["severity"] == "error" and f["unit_id"] == "m1_p002"
                            for f in found))

    def test_registry_accumulates_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "reg.json"
            audit.check_unit_ids([{"unit_id": "m1_p001"}], registry, write=True)
            audit.check_unit_ids([{"unit_id": "m1_p002"}], registry, write=True)
            stored = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(stored["unit_ids"], ["m1_p001", "m1_p002"])


class TestOneRowPerFormation(unittest.TestCase):
    def test_split_formation_is_flagged(self):
        units = [{"unit_id": "a", "strat_name": "Seki Formation"},
                 {"unit_id": "b", "strat_name": "seki formation"}]
        found = rules(audit.check_one_row_per_formation(units), "one_row_per_formation")
        self.assertEqual(len(found), 1)

    def test_distinct_formations_pass(self):
        units = [{"unit_id": "a", "strat_name": "Seki Formation"},
                 {"unit_id": "b", "strat_name": "Kuzumaki Formation"}]
        self.assertEqual(audit.check_one_row_per_formation(units), [])


class TestUnresolved(unittest.TestCase):
    def test_missing_fields_are_listed_with_their_evidence(self):
        units = [{"unit_id": "m1_p001", "unit_name": "Seki Formation",
                  "lithology": "sandstone", "t_int": "Jurassic", "b_int": "Jurassic"}]
        evidence = {("m1_p001", "environment"): [
            {"candidate": "deep marine", "flag": "INFERRED",
             "source_and_full_context": "半遠洋性の泥岩を主体とする。"}]}
        out = audit.collect_unresolved(units, evidence)
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0]["missing"]), ["b_prop", "environment"])
        self.assertEqual(out[0]["evidence_hints"]["environment"][0]["candidate"], "deep marine")

    def test_complete_unit_is_not_listed(self):
        units = [{f: "x" for f in audit.REQUIRED_FOR_CLOSE} | {"unit_id": "m1_p001"}]
        self.assertEqual(audit.collect_unresolved(units, {}), [])


class TestHelpers(unittest.TestCase):
    def test_split_terms_handles_full_width_semicolon(self):
        self.assertEqual(audit.split_terms("sandstone；mudstone"), ["sandstone", "mudstone"])

    def test_number_pulls_a_value_out_of_annotated_text(self):
        self.assertEqual(audit._number("約 12.5 Ma"), 12.5)
        self.assertIsNone(audit._number(""))


if __name__ == "__main__":
    unittest.main()


class TestIntrusiveBodies(unittest.TestCase):
    """貫入岩体には堆積環境が存在しないので、未解決として数えない。"""

    PLUTON = {"unit_id": "m1_p005", "unit_name": "Ichinohe Pluton",
              "strat_name": "Ichinohe Pluton", "lithology": "gabbro; quartz monzonite",
              "t_int": "Early Cretaceous", "b_int": "Early Cretaceous",
              "b_prop": 0.63615, "environment": ""}
    SEDIMENT = {"unit_id": "m1_p004", "unit_name": "Kuzumaki Formation",
                "strat_name": "Kuzumaki Formation",
                "lithology": "phyllite; mudstone; chert",
                "unit_description": "白亜紀中頃の一戸深成岩体に貫入されている",
                "t_int": "Jurassic", "b_int": "Jurassic",
                "b_prop": 0.0, "environment": ""}

    def test_pluton_is_detected_by_name(self):
        self.assertTrue(audit.is_intrusive(self.PLUTON))

    def test_pluton_is_detected_by_lithology_alone(self):
        self.assertTrue(audit.is_intrusive(
            {"unit_name": "Unnamed body", "lithology": "granodiorite"}))

    def test_intruded_sediment_is_not_intrusive(self):
        """本文に「貫入されている」とあっても、被貫入側は貫入岩体ではない。"""
        self.assertFalse(audit.is_intrusive(self.SEDIMENT))

    def test_environment_is_not_required_for_a_pluton(self):
        self.assertNotIn("environment", audit.required_for_close(self.PLUTON))
        self.assertIn("environment", audit.required_for_close(self.SEDIMENT))

    def test_pluton_without_environment_is_not_unresolved(self):
        unresolved = audit.collect_unresolved([self.PLUTON], {})
        self.assertEqual(unresolved, [])

    def test_sediment_without_environment_is_still_unresolved(self):
        unresolved = audit.collect_unresolved([self.SEDIMENT], {})
        self.assertEqual([u["missing"] for u in unresolved], [["environment"]])
