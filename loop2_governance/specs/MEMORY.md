# Macrostrat Japan: AI Agent Shared Memory & Persistent Context

本ドキュメントは、Antigravity (Gemini), Claude Code (Opus/Sonnet), Codex が共同作業を行う際に参照・更新する統合永続記憶（Shared Memory）である。

---

## 1. Project Overview & Primary Focus
- **対象**: GSJ（産総研 地質調査総合センター）5万分の1地質図幅（1,286図幅）および20万分の1シームレス地質図。
- **作業ディレクトリ**: `C:\Users\somas\projects\MacroStrat`
- **現在の最優先課題**:
  1. `m1286 一戸 (2018年)` の未解決23ユニット（堆積環境 9ユニット、基底関係 `b_prop` 21ユニット）の本文各論からの高精度抽出。
  2. `python run.py audit` による不変条件・語彙検査の継続的パス。
  3. `python run.py ui`（GSJ風ズーム式ダッシュボード）の実機稼働・検証。

---

## 2. System Invariants & Strict Rules
1. **年代の単調性（Monotonicity）**: すべてのユニット $u$ について、$b\_age(u) \ge t\_age(u) \ge 0.0$ Ma。
2. **証拠の保持（Verbatim Evidence）**: 推測による数値年代の自動生成は厳禁。抽出されたすべての年代はGSJ説明書の日本語原文引用（`verbatim_quote`）と1対1でリンクすること。
3. **公式統制語彙（Controlled Vocabulary）**: `lithology` および `environment` は `config/vocab.json` に厳密準拠すること。
4. **識別子の不変性（Immutable Unit ID）**: 生成された `unit_id`（例: `m1286_p001`）は恒久的に変更しないこと。
5. **実体ファイルの参照**: レビューシートの実体は `m1286_review.candidate-20260814T074455Z.xlsx` である（空の `m1286_review.xlsx` を参照しないこと）。

---

## 3. Current Benchmark Status: m1286 Ichinohe (2018)

`python run.py audit m1286` の実測結果：
- **層数**: 30 ユニット / カラム: Western Area, Central Area, Eastern Area
- **年代 (t_int / b_int)**: **30/30 解決済 (100%)**
- **主岩相 (lithology)**: **30/30 解決済 (100%)**
- **堆積環境 (environment)**: 21/30 (9ユニット未解決)
- **基底関係 (b_prop)**: 9/30 (21ユニット未解決)
- **未解決合計**: **23 ユニット**（一部両方欠落）

---

## 4. Operational Commands & Audit Workflow
1. **全国ダッシュボード起動**: `python run.py ui` (既定 `http://127.0.0.1:8787/`)
2. **不変条件・語彙・未解決の機械検査**: `python run.py audit m1286` (または `--all`)
3. **テストスイート実行**: `.venv\Scripts\python -m pytest tests/` (全73件 PASS)
---

## 7. 記憶統合ログ（2026-08-19）

注: 本ファイルは本節の追記後に別セッションにより第 1〜4 章が再構成された。章番号 5・6 は現存しないが、本節の内容は独立して参照できるため番号はそのまま維持する。

### 7.1 完了したこと

1. ワークスペースの移行先 `C:\Users\somas\projects\MacroStrat` を正本として確定した。旧 OneDrive 側（`…\summer research 2026\MacroStrat`）は空であることを確認済み。
2. `knowledge/legacy_claude_work/reports/` の 38 件、`knowledge/architecture/` の 6 件、`docs/` の設計文書 21 件を読み、出典付きで `knowledge/memory_vault/` へ圧縮統合した。
   - `DESIGN_RATIONALE.md`: 第 3 章（採択された設計判断 24 項目）、第 4 章（棄却されたアプローチ 16 項目）を追記。
   - `AGENT_ARCHIVES.md`: 第 4 章（Claude 実装 8 件、Codex 実装 4 件、未実施 Backlog 13 件、運用教訓 4 件）を追記。
   - `CHRONICLES.md`: 2026-08-10 〜 08-14 の詳細年表と Epoch 4 を追記。
   - `DATA_SOURCE_LEDGER.md`: 新規作成。データソース被覆、ライセンス、抽出精度ベンチマーク、LLM コスト実測値の台帳。
   - `INDEX.md`: 統合元と統合先の対応表、統合規則、参照順序を追記。
3. Git による版管理を導入した（`.gitignore` の被覆をデータ分離後の構造に合わせて拡張）。

### 7.2 未解決課題（本セッションで新たに判明したもの）

3. **記録の重複による正本の不明確さ**:
   - `knowledge/legacy_claude_work/` と `knowledge/archives/claude_work/` が `__pycache__` を除き同一内容。
   - `docs/` と `knowledge/legacy_docs/` が同名ファイルで重複（差分は BOM のみ）。
   - どちらを正本とするか、また統合するか併存させるかは研究者の判断を要する。削除は行っていない。

4. **過去の Backlog 13 件が未着手のまま**:
   - `knowledge/memory_vault/AGENT_ARCHIVES.md` 4.3 の表に集約済み。着手前に前提条件が今も有効か確認すること。特に優先度が高いと記録されているもの:
     - Column 検出 prompt の内部矛盾の解消（地理区分パネルを Column の証拠と明示）
     - membership prompt への列の左右位置の明示
     - `common.py` の許可リストへの `Early Pliocene` 追加（1〜2 行）
     - Vision 系 2 ステージ（`llm_column_vision.py:378`、`pdf_environment.py:367`）へのリトライ追加

5. **測定条件の異なるベンチマーク数値の混在**:
   - `DATA_SOURCE_LEDGER.md` 第 3 章に測定条件を併記した。条件が異なる数値どうしを比較しないこと。特に、不安定な provider（OpenRouter `gemma-4-26b:free`）で得た prompt v1 / v2 比較は無効である。

6. **200k environment 語彙警告 2,586 件**:
   - 解消方針は「根拠がある場合のみ正規化、それ以外は unknown」の方向性のみで未確定。

7. **一戸（m1286）の残課題**:
   - Column 未割当 2 件（landslide deposits、colluvial and alluvial cone deposits）は図・本文からは確定できず人の判断待ち。
   - GOLD fixture（sha256）の再束縛が未実施。
   - 手動正解 42 / 出力 48 のうち名前対応がついたのは 30 件であり、その差の原因（表記ゆれか別地層立てか）は未調査。

### 7.3 版管理の運用ルール（新規）

- 本リポジトリは Git 管理下にある。作業前に `git status` で未コミットの変更を確認すること。
- 秘匿情報（`config/secret.json`、`config/secrets.json`、`config/llm_usage.json`、`llm_runtime.sqlite`）は `.gitignore` により追跡対象外である。誤ってコミットしないこと。
- 破壊的操作（`--force` を伴う実行、ファイルの上書き）の前にコミットして復元点を作ること。過去に一戸ワークブックの復元不能な消失事故が発生している（`knowledge/memory_vault/DESIGN_RATIONALE.md` 4.4.4）。

### 7.4 訂正と補足（2026-08-19、ディレクトリ再編の反映）

本節 7.1 の記載時点と、その直後に別セッションが実施したディレクトリ再編（`loop1_engine` / `loop2_governance` / `loop3_community`）が競合したため、以下を訂正・補足する。

1. **記憶保管庫の現在位置**: `knowledge/memory_vault/` は `loop3_community/memory_vault/` へ移動した。統合した 6 文書（`INDEX.md`、`DESIGN_RATIONALE.md`、`AGENT_ARCHIVES.md`、`CHRONICLES.md`、`DATA_SOURCE_LEDGER.md`、`GROUND_TRUTH_METHODOLOGY.md`）はすべて再編後のディレクトリに揃っており、内容は保全されている。統合元の `knowledge/legacy_claude_work/` および `docs/` も `loop3_community/` 配下へ移動している。
2. **本ファイルの現在位置**: `specs/MEMORY.md` は `loop2_governance/specs/MEMORY.md` へ移動した。
3. **7.1 の 3 番（Git 導入）は「未完了」に訂正する**: リポジトリはまだ Git 管理下にない。クラウドサンドボックスからデバイスブリッジ経由で `git add` を実行したところ、書き込み速度が実用に耐えず（約 2.5 分で 924 KB）、さらにブリッジ側がファイル削除を許可しないため git のロックファイル処理が失敗した。代替として、リポジトリ直下に `git_bootstrap.bat` を配置した。**研究者が Windows 上で 1 回実行する必要がある**。
   - このバッチは、クラウドセッションが作成した不完全な `.git` を削除してから `git init` → `git add -A` → 秘匿ファイル混入チェック → 初回コミットを行う。
   - `.gitignore` は再編に追従できるよう、パス非依存パターン（`**/config/secret.json` 等）を追記済み。
4. **再編後の未検証事項**: 統合した文書内の相対パス表記（`knowledge/legacy_claude_work/...`、`docs/...` 等）は再編前のものである。出典としての識別性は保たれるが、リンクとしては解決しない。パスの張り替えは行っていない（履歴としての正確さを優先したため）。必要になった時点で `loop3_community/` を接頭辞として補うこと。


## 最新ステータス (2026-08-19 Claude Code 引き継ぎ)
- 全国監視ダッシュボード（ポート 8787）の起動・常駐方式について、チャット画面を汚さずに確実に開けるアーキテクチャの確立を Claude Code へ依頼中。詳細は specs/TASK.md を参照。

---

## 8. ダッシュボード起動の確定運用（2026-08-19 恒久化）

- **開き方は 3 通り。ショートカットは `start_dashboard_hidden.vbs` に向けること**（非表示常駐 → 健全性確認 → ブラウザ自動起動）。
  - 調査したいとき: `start_dashboard.bat`（可視・エラーで `pause`・ログ末尾を表示）
  - サーバ不要: `make_static_dashboard.bat` / `python run.py ui-static` → `dashboard_static.html`
  - 停止: `stop_dashboard.bat`
- **禁止事項: `socketserver` の `allow_reuse_address` を Windows で `True` にしないこと。** Windows の `SO_REUSEADDR` は listen 中の他プロセスからポートを奪い、接続先が不定になる（`ERR_CONNECTION_REFUSED` の断続再現の原因）。ポート確認は必ず実 `connect()` で行う。
- **禁止事項: `.bat` ファイルを BOM 付きで保存しないこと。** `cmd.exe` が 1 行目で失敗する。バッチは純 ASCII・CRLF とし、日本語メッセージは Python 側に置く。
- **禁止事項: 背景常駐しうるコードで素の `print()` を使わないこと。** `pythonw.exe` では `sys.stdout is None` となり `AttributeError` でプロセスが死ぬ。`dashboard_server._say()` を通す。
- ログ: `loop2_governance/logs/dashboard_server.log`（1MB ローテート）、稼働情報: `loop2_governance/logs/dashboard_server.state.json`（PID / URL）。
- 起動確認だけなら `python run.py ui-status`。
