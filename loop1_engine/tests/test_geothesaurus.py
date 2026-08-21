# -*- coding: utf-8 -*-
"""
test_geothesaurus.py — GeoThesaurus の層序ランクおよび岩相分類判定の自動回帰テスト
"""

import os
import re
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from scripts.build_geothesaurus import STRAT_RANK_PATTERNS

def classify_strat_rank(name: str) -> str:
    for pattern, rank in STRAT_RANK_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return rank
    return "Formation"

class TestGeoThesaurus(unittest.TestCase):
    def test_strat_rank_classification(self):
        # 1. Member (部層) が Formation に誤判定されないこと
        self.assertEqual(classify_strat_rank("槇島部層"), "Member")
        self.assertEqual(classify_strat_rank("Makishima Member"), "Member")
        self.assertEqual(classify_strat_rank("上部部層"), "Member")
        self.assertEqual(classify_strat_rank("下部メンバー"), "Member")

        # 2. Bed (単層・層準)
        self.assertEqual(classify_strat_rank("鍵単層"), "Bed")
        self.assertEqual(classify_strat_rank("Key Bed"), "Bed")

        # 3. Formation (累層・層)
        self.assertEqual(classify_strat_rank("日吉累層"), "Formation")
        self.assertEqual(classify_strat_rank("Hiyoshi Formation"), "Formation")
        self.assertEqual(classify_strat_rank("設楽層"), "Formation")

        # 4. Group (層群・累層群)
        self.assertEqual(classify_strat_rank("丹波層群"), "Group")
        self.assertEqual(classify_strat_rank("和泉層群"), "Group")
        self.assertEqual(classify_strat_rank("Tamba Group"), "Group")

        # 5. Complex / Pluton
        self.assertEqual(classify_strat_rank("美濃コンプレックス"), "Complex")
        self.assertEqual(classify_strat_rank("苗木花崗岩体"), "Pluton")

if __name__ == '__main__':
    unittest.main()
