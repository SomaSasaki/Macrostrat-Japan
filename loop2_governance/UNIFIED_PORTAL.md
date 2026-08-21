# Macrostrat Japan: 統合研究ガバナンスポータル (UNIFIED_PORTAL.md)
**Unified Research Governance Portal (Japanese & English Unified Command Center)**

本ポータルは、研究責任者（soma / PI）が AI エージェント（Loop 1）の自律計算を統括・指示し、手動正解データ（Ground Truth）との照合検証を行い、承認済みカラムを国際公開（Loop 3）へ昇格させるための**統合管理司令室**である。

---

## ★ 全国進捗監視システム (Web UI Dashboard)
- **ブラウザアクセス先**: [http://127.0.0.1:8787/](http://127.0.0.1:8787/)
  - 日本全国 891 図幅の達成状況、各図幅の Column 構成、5大不変条件の監査状況を GSJ 地質図ナビ風のインタラクティブマップでリアルタイム監視できます。

---

## 1. 研究者司令室・現役アクティブノート (Loop 2)
- **先行研究トラッキングハブ**: [[research_hub/INDEX.md|research_hub/ (先行研究包括DB・ギャップ分析)]]
- **最新タスク指示書**: [[specs/TASK.md|TASK.md (AIへの最新指示)]]
- **最新成果報告書**: [[specs/FEEDBACK.md|FEEDBACK.md (AIからの成果報告)]]
- **全AI共通永続記憶**: [[specs/MEMORY.md|MEMORY.md (共通課題・役割分担)]]
- **全体アーキテクチャ図**: [[MacroStrat_Architecture.canvas|MacroStrat_Architecture.canvas]]
- **マルチAIオーケストレーター仕様**: [[specs/ORCHESTRATOR.md|ORCHESTRATOR.md]]
- **Claude Code ワークフロー詳細**: [[specs/CLAUDE_CODE_WORKFLOW.md|CLAUDE_CODE_WORKFLOW.md]]
- **タスク定義テンプレート**: [[specs/TEMPLATE_TASK.md|TEMPLATE_TASK.md]]
- **手動正解データ検証レポート**: [[data/50k/02_review/05_青森/m1286_一戸 2018/system/qa/science_gold_comparison.md|science_gold_comparison.md]]

---

## 2. 第1ループ: AI自律計算・知見アーカイブ (Loop 1)
- **AI行動規範**: [[../loop1_engine/CLAUDE.md|CLAUDE.md]] 
- **パイプライン構造解剖書**: [[../loop1_engine/docs/LOOP1_PIPELINE_STRUCTURE.md|LOOP1_PIPELINE_STRUCTURE.md]]
- **一戸図幅 (m1286) 統合開発記録**: [[../loop1_engine/archive/M1286_ICHINOHE_ARCHIVE.md|M1286_ICHINOHE_ARCHIVE.md]]
- **LLM運用・コスト管理 統合記録**: [[../loop1_engine/archive/LLM_OPERATIONS_ARCHIVE.md|LLM_OPERATIONS_ARCHIVE.md]]
- **50k/200k広域データ 統合記録**: [[../loop1_engine/archive/REGIONAL_DATA_ARCHIVE.md|REGIONAL_DATA_ARCHIVE.md]]
- **永続知見保管庫 総合マスター**: [[../loop1_engine/archive/KNOWLEDGE_VAULT.md|KNOWLEDGE_VAULT.md]]

---

## 3. 第3ループ: 学術論文・GitHub公開同期 (Loop 3)
- **公式学術論文 (英語上・日本語下 対訳マスター)**: [[../loop3_community/publications/PUBLICATION_PAPER.md|PUBLICATION_PAPER.md]]
- **学術執筆規程ガイドライン**: [[../loop3_community/docs/ACADEMIC_WRITING_GUIDELINES.md|ACADEMIC_WRITING_GUIDELINES.md]]
- **GitHub 同期コマンド**: `python run.py publish`