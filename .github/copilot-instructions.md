# Codex / GitHub Copilot 開発ガイドライン

## プロジェクト概要
このリポジトリは、産業技術総合研究所（GSJ）の地質図幅データを Macrostrat 公式形式（v0.1.1）へ変換するオープンサイエンス開発基盤です。

## コーディング原則
1. **決定論的アルゴリズムの優先**:
   - 年代の比較、上下拘束（`auto_t_pos`）、層厚の計算は `scripts/common.py` の純粋関数として実装する。
   - LLMに計算を行わせず、コード側で区間演算を行うこと。
2. **型安全と不変条件**:
   - `b_age >= t_age`（単調性）を厳格に保持する。
   - すべてのデータ構造は Pydantic または明示的な TypedDict / データクラスで定義する。
3. **語彙チェック**:
   - Lithology および Environment の変換は `config/official_vocab.json` に適合させること。
