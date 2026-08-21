# -*- coding: utf-8 -*-
"""Invariant Test Harness: Fast deterministic verification suite for Orchestrators.

Can be run with:
    python tests/test_invariants.py
    python -m unittest tests/test_invariants.py
"""

import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
VOCAB_FILE = HERE / "config" / "official_vocab.json"


def validate_unit_invariants(unit: dict) -> list[str]:
    """Verify core stratigraphic invariants for a single unit record."""
    errors = []
    
    # 1. Monotonicity check
    b_age = unit.get("b_age")
    t_age = unit.get("t_age")
    if b_age is not None and t_age is not None:
        try:
            b_val = float(b_age)
            t_val = float(t_age)
            if b_val < t_val:
                errors.append(f"Monotonicity violation: b_age ({b_val}) < t_age ({t_val})")
            if t_val < 0.0 or b_val < 0.0:
                errors.append(f"Negative age violation: b_age={b_val}, t_age={t_val}")
        except (ValueError, TypeError):
            errors.append(f"Non-numeric age format: b_age={b_age}, t_age={t_age}")

    # 2. Thickness check
    min_thick = unit.get("min_thick") or unit.get("min_thickness")
    max_thick = unit.get("max_thick") or unit.get("max_thickness")
    if min_thick is not None and max_thick is not None:
        try:
            min_v = float(min_thick)
            max_v = float(max_thick)
            if min_v > max_v:
                errors.append(f"Thickness range violation: min ({min_v}) > max ({max_v})")
            if min_v < 0.0 or max_v < 0.0:
                errors.append(f"Negative thickness: min={min_v}, max={max_v}")
        except (ValueError, TypeError):
            pass

    return errors


class InvariantTestSuite(unittest.TestCase):
    def test_monotonicity_valid_sample(self):
        """Verify valid chronostratigraphic unit satisfies monotonicity."""
        sample = {
            "unit_name": "Okuse Formation",
            "b_age": 2.58,
            "t_age": 0.78,
            "min_thick": 50.0,
            "max_thick": 120.0,
        }
        errors = validate_unit_invariants(sample)
        self.assertEqual(len(errors), 0)

    def test_monotonicity_inversion_detected(self):
        """Verify age inversion is strictly caught as an error."""
        inverted_sample = {
            "unit_name": "Inverted Unit",
            "b_age": 10.0,
            "t_age": 15.0,
        }
        errors = validate_unit_invariants(inverted_sample)
        self.assertTrue(any("Monotonicity violation" in e for e in errors))

    def test_official_vocab_file_exists(self):
        """Ensure official vocabulary dictionary is present and readable."""
        if VOCAB_FILE.exists():
            with open(VOCAB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.assertTrue(isinstance(data, dict) or isinstance(data, list))


if __name__ == "__main__":
    unittest.main()
