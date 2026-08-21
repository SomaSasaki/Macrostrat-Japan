# Macrostrat Japan: 永続知見保管庫 総合マスター (KNOWLEDGE_VAULT.md)

本ドキュメントは、Macrostrat Japan プロジェクトにおいて採択された設計根拠、棄却理由、手動正解データ策定基準、開発年代記を集約した総合知見マスターである。


---

# 設計判断理由と失敗知識 (Design Rationale & Negative Evidence)

﻿# Architecture Design Rationale & Negative Evidence Archive

本ドキュメントは、Macrostrat Japan パイプラインの開発において下された重要設計判断の根拠（Rationale）、および試行錯誤の中で得られた「失敗したアプローチ（Negative Evidence）」を体系的に記録した永続知見アーカイブである。
未来の開発者およびAIエージェントは、本ドキュメントを参照することで同一の過ちを防ぎ、設計意図を正確に継承すること。

---

## 1. 採択された重要アーキテクチャ判断（Adopted Decisions）

### 1.1 なぜ「5段階年代解決ソルバー（5-Stage Chronology）」が必要だったのか
- **課題**: LLM単体に年代推定を行わせると、存在しない絶対年代（Ma）を幻覚（ハルシネーション）して数値を埋めてしまう問題が発生した。
- **決定**: 
  - LLMには「原文の引用（verbatim quote）」と「地質時代名（例: Early Miocene）」のみを抽出させ、数値年代の計算は決定論的な Python ソルバー（`age_resolution.py`）へ完全に分離。
  - 上位層・下位層の年代から存在可能区間 $[t\_age_{upper}, b\_age_{lower}]$ を数学的に補間するルールを導入。
- **成果**: 捏造数値の根絶と単調性（$b\_age \ge t\_age \ge 0.0$）の100%保証。

### 1.2 なぜ「章節ルーティング（Context Router）」を導入したのか
- **課題**: 50〜100ページのPDF全文をLLMに一度に渡すと、コンテキスト長の上限圧迫、APIコストの高騰、および別ユニットの記述との混同（False Association）が多発した。
- **決定**: 
  - `pdf_context_router.py` により、目次・凡例から各ユニットの記述が含まれるページ・章節（約2,000文字）をピンポイントにスライスしてプロンプトへ供給。
- **成果**: トークン消費量を90%以上削減し、ユニット間の属性混同をゼロ化。

---

## 2. 棄却されたアプローチ（Negative Evidence）

### 2.1 自由形式JSONプロンプティングによる一括抽出の失敗
- **試み**: 「このPDFからすべての地層の年代、層厚、環境をJSONで出力せよ」という巨大プロンプトを実行。
- **結果**: ユニットの欠落、部層と主地層の階層混同、およびMacrostrat公式語彙外の単語の出力が多発し破綻。
- **教訓**: 抽出は「幹（ユニット識別・幾何構造）」を確定させた後、各「枝（属性）」を個別パイプラインで多段階に抽出する木構造（Harness Engineering）でなければならない。

### 2.2 OCRテキストのみに頼る柱状図解析の失敗
- **試み**: 地質柱状図の地域区分（東西中央の3カラム）をOCRテキストから正規表現で判別しようとした。
- **結果**: 柱状図は複雑な2次元レイアウトであり、テキスト順序が崩れて地域区分が正しく認識できなかった。
- **教訓**: 柱状図は Vision モデル（Gemini Flash Vision / Claude Vision）または画像切り出し（`column_vision.py`）による幾何学的検出が不可欠。
---

## 3. 採択された設計判断・拡張アーカイブ（2026-08-19 統合）

本節は、`knowledge/legacy_claude_work/reports/`（38件）、`docs/`、`knowledge/architecture/` に分散していた 2026-08-10 〜 08-14 の作業記録を、2026-08-19 に圧縮・統合したものである。各項目に一次出典を付す。原典ファイルは削除していないため、詳細は出典先を参照すること。

### 3.1 データモデルと不変条件

#### 3.1.1 sort_order を position へ反転する
- 課題: Macrostrat 公式仕様は `position` を最古 = 1 と定めるが、人間が編集する `sort_order` は最新 = 1 で書き下す方が自然である。
- 決定: `position = (Column 内 sort_order 最大値) − sort_order + 1` で変換する。
- 根拠: 一戸完成形 18 層で検証済み。
- 出典: `docs/SYSTEM_DESIGN.md`

#### 3.1.2 各 Column 最上位行に t_pos を必須付与する
- 課題: 公式仕様上、区間の上端が未確定のユニットは取り込み時に drop される。
- 決定: 各 Column の最上位行に `t_pos = max(position) + 1` を明示的に与える（一戸 central では 7 → 8）。
- 出典: `docs/SYSTEM_DESIGN.md`

#### 3.1.3 unit_id 対応表の単射化
- 課題: `pdf_unit_bootstrap.py` の `_stable_ids_from_cache`（`setdefault`）が異なる地層名に同一 `unit_id` を割り当て、`m1286_p019`〜`p022` の 4 件が重複。`m1286_p020`（flood-plain and valley-floor deposits / Toya Formation）では記載文・年代が別地層のものへ転写されていた。
- 決定: `_prior_stable_ids` を単射化（値の重複を許さない）し、`_evidence_rows` 側でも対応表の値の重複を除去してから採番する。
- 成果: `unit_id` 重複 4 件 → 0 件。新規テスト `test_unit_id_uniqueness.py` 17 PASS、既存 `test_roundtrip` 440 PASS 維持。
- 出典: `knowledge/legacy_claude_work/reports/unit_id重複と年代誤伝播_修正提案_20260811.md` (2026-08-11)

#### 3.1.4 unit_id の検査仕様は「一意性」ではなく「単射性」である
- 課題: 当初「unit_id は一律一意」と仕様化したところ、`test_roundtrip` が 440 PASS → 415 PASS / 4 FAIL へ退行した。
- 決定: 1 地層が複数 Column にまたがるのは正常であり、識別子は `(unit_id, column_id)` の組である。検査を「同一 unit_id に別地層名 = エラー」「同一 (unit_id, column_id) の重複行 = エラー」の 2 種に分割する。
- 成果: 訂正後 `test_roundtrip` 440 PASS / 0 FAIL に復帰。
- 出典: `knowledge/legacy_claude_work/reports/unit_id重複と年代誤伝播_修正提案_20260811.md` (2026-08-11)

#### 3.1.5 年代の自己ブラケット補完を停止する
- 課題: `age_resolution.py` の `infer_interval_pairs` は上下ユニットの区間一致を条件に年代を補完するが、上下が同一 `unit_id`（重複行）だと自明に一致し保護が無効化される。sort2〜18 の間で 15 ユニットに Messinian / Zanclean（約 7.25–3.6 Ma）が誤伝播した。手動正解データは Holocene 〜 Late Pleistocene であり、500 万年以上のずれであった。
- 決定: 上下候補が同一 index または同一 `unit_id` の場合は補完対象から除外する。
- 成果: 再計算で誤補完 21 件 → 補完 6 件（すべて Burdigalian、部層間の妥当な補完）。
- 出典: `knowledge/legacy_claude_work/reports/unit_id重複と年代誤伝播_修正提案_20260811.md` (2026-08-11)

#### 3.1.6 噴火イベントの t_prop / b_prop に最小丸め幅を与える
- 課題: 年代が 1 点の噴火性堆積物は本来 `b_prop = t_prop` だが、公式仕様は `b_prop < t_prop` を要求する。
- 決定: 表示桁（小数第 3 位）で同値になる最小幅を上下端に付与する。
- 出典: `docs/SYSTEM_DESIGN.md`

### 3.2 情報源の優先順位と信頼度

#### 3.2.1 ZFK > Shapefile > PDF 英文 Abstract > 日本語本文
- 決定: フィールドごとにこの優先順位でデータを採用する。LLM は ZFK / Shapefile で埋まらない場合の最終手段であり、PDF 本文全体を無条件で LLM に渡さない。
- 根拠: データ確実性。
- 出典: `docs/AI_HANDOFF.md`, `docs/SYSTEM_DESIGN.md`
- 補足: Review v2 における候補優先順位は ZFK > GSJ Shapefile > PDF（英語 Abstract 優先、不足分のみ本文・LLM）。出典: `docs/REVIEW_V2.md`

#### 3.2.2 ZFK の derived.thickness を層厚に使わない
- 課題: `derived.thickness` は本文中の任意の「層厚 ○ m」記載を 1 つ拾うだけで、地層全体の層厚と一致しないことが多い（十和田 23 層でほぼ全件不一致）。
- 決定: `gsj_derived.thickness_from_section()` で「分布及び層厚」節に限定して読み、読めなければ空欄とする。
- 出典: `docs/SYSTEM_DESIGN.md`

#### 3.2.3 Review v2 は非破壊運用とする
- 決定: 既存の `_review_v2.xlsx` は再実行で上書きせず、`--force` 明示時のみ再生成する。lithology・minor_lith・environment は Macrostrat 語彙で検証できた語のみ黄色の AUTO CANDIDATE として仮入力する。
- 出典: `docs/REVIEW_V2.md`

### 3.3 ハルシネーション対策

#### 3.3.1 quote の二段階照合
- 決定: LLM 出力に原文逐語引用 `quote` を必須化し、(1) quote が原文に存在するか、(2) 報告数値が quote 内に存在するか、をコードで検証する。不一致ならそのフィールドのみ破棄し、ユニット全体は残す。数値比較は単位（Ma / ka / 年 BP）を正規化して行う。
- 出典: `docs/SYSTEM_DESIGN.md`, `docs/ROADMAP.md`

#### 3.3.2 公式語彙の「格下げ」防止（vocab_quality）
- 課題: LLM 出力が実行ごとに揺れ、公式語彙をより一般的な語や自由記述で上書きする事例が発生した（`deep-water indet.` → `marine` 等）。
- 決定: 語に品質スコア（2.0 / 1.0 / 0.0）を付け、平均が下がる上書きを拒否する。
- 出典: `docs/SYSTEM_DESIGN.md`

#### 3.3.3 語彙ゲートは呼び出しごとの閉世界として扱う
- 課題: `pdf_environment.py` の `_verified_environment` が Macrostrat 公式 83 語表にない語を常に棄却しており、手動正解データの正解語 `sublittoral`・`bathyal` が公式表に無いため構造的に到達不能（max recall 0.600）であった。
- 決定: `vocab` 引数を呼び出しごとの閉世界として扱う。本番ステージは公式 83 語のまま不変とし、制約版評価にはレビュー由来の限定候補リストを渡す。`CONSTRAINED_VALIDATOR_VERSION` を v1 → v2 に更新。
- 成果: Environment 到達可能性 3/5（max recall 0.600）→ 5/5（1.000）。
- 出典: `knowledge/legacy_claude_work/reports/画像LLM三本化_Claude作業記録_20260812.md` (2026-08-12)

#### 3.3.4 Evidence の scope 契約（unit_global / column_specific / map_global）
- 決定: Column 分割後も unit-global Evidence が新規行に引き継がれるよう scope 契約を導入する。`unsplit` を scope として使うことを禁止し、分割後は必ず orphan Evidence 0 件を監査する。
- 状態: Status: Proposed（未実装）。
- 出典: `docs/PDF_FIELD_ENRICHMENT_DESIGN.md`

### 3.4 抽出パイプラインの構造

#### 3.4.1 bootstrap 抽出の 2 段バッチ化
- 課題: 一戸 48 ユニットの inventory が 1 応答で約 11,400 output token を要し、全モデルで応答が途中で切れていた。
- 決定: 段 A（名前列挙、予約 4,096）＋段 B（8 件ずつ詳細、予約 `min(4096, 320 × 件数)`）に分割し、決定的にマージする。
- 成果: 48 ユニット名 / 6 バッチ / 外部コール 7 件、全応答正常終了。
- 出典: `knowledge/legacy_claude_work/reports/Codex向け引き継ぎ_20260813.md` (2026-08-13)

#### 3.4.2 PDF ユニット抽出ルール v2（最表層堆積物の包含）
- 課題: `BOOTSTRAP_RULES` が最表層の小堆積物（河床・氾濫原・崖錐等）を除外し、一戸で 27 ユニットしか抽出されていなかった。
- 決定: `PROMPT_VERSION` を v2 へ上げ、River bed deposits 等を明示的に抽出対象へ追加する。
- 成果: 一戸 inventory 27 → 48 ユニット（既存 27 ID 維持、orphan evidence 0 件、137 pytest PASS、440/440 ラウンドトリップ PASS）。Vision 実行で western / central / eastern の 3 列へ 46 件割当（landslide / colluvial の 2 件は unassigned）。
- 出典: `knowledge/architecture/PDF_Unit_Extraction_Proposal.md` (2026-08-10 / 08-11)

#### 3.4.3 json_parse は括弧バランス方式で抽出する
- 課題: `llm_router.py` の `_parse_json_block` は最初の `{` から最後の `}` までを切り出しており、正しい JSON の後にモデルが注釈（例: `{western, central, eastern}` を含む文）を足すと全体をパース失敗として捨てていた。
- 決定: 文字列・エスケープを考慮して括弧の対応を数え、最初に閉じた完全なオブジェクトのみを取り出す。closed-world validator の緩和は行わない。診断用に本文を含まない構造カウンタ（chars / open_braces / close_braces / balanced_objects / fenced / starts_with_brace）をエラーに付与する。
- 成果: `test_json_block_recovery.py` 10 件で固定。
- 出典: `knowledge/legacy_claude_work/reports/json_parse耐性の修正_20260812.md` (2026-08-12)

#### 3.4.4 GOLD fixture のページ束縛 preflight チェック
- 課題: Column GOLD が PDF 15 ページ（本文のみ、図なし）の画像を送信していた。fixture は `pdf_page: 15` と `printed_page: 6` で自己矛盾しており、正しい図は印刷 6 ページ = PDF 16 ページの第 2.1 図（西部 / 中央部 / 東部の 3 列）であった。
- 決定: `config/llm_gold_column_vision.json` の `pdf_page` を 15 → 16 に修正し、preflight に「fixture の pdf_page と印刷ページ対応表の照合」を追加、不一致なら exit 1 で外部送信を止める。
- 成果: column_detection が 3 つとも false → 3 つとも true、membership TP 0/42 → 10/42、recall 0.000 → 0.238（Bedrock Claude Haiku 4.5）。
- 出典: `knowledge/legacy_claude_work/reports/Column検出の真因_ページ束縛ミス_20260812.md` (2026-08-12)

#### 3.4.5 Environment 証拠図の差し替え
- 課題: Environment GOLD の証拠図 PDF55（印刷 45）は本文のみで図が無く、PDF27（印刷 17）は評価対象と無関係な高屋敷層の柱状図であった。
- 決定: PDF16（第 2.1 図、「堆積場」列あり）を最優先候補に追加し `figures: [55, 27]` → `[16, 27]` に変更する。PDF27 は無関係図混入時の誤引用検査のため意図的に残置する。
- 成果: 完全一致 1/5 → 2/5、accept 2/5 → 3/5（OpenRouter）。
- 出典: `knowledge/legacy_claude_work/reports/ABC実施結果_図と解像度の修正_20260812.md` (2026-08-12)

#### 3.4.6 日本語別名は検証済みデータがある場合にのみ併記する
- 課題: membership 応答の無回答 14 件の多くが、図が日本語表記のみであるのに英語翻字名しか供給していなかったことに起因していた（例: Rendaino terrace deposits → 図では「蓮台野段丘堆積物」）。
- 決定: `system/pdf_enrichment/unit_aliases.mapped.json`（48 unit 中 26 unit に日本語名あり、無回答 14 件中 8 件が該当）を prompt 構築時にのみ付加する。別名表に無い unit には何も追加しない。prompt version を `column-membership-batched-v1` → `v2` へ。
- 成果: `test_membership_prompt_carries_verified_japanese_labels_only` により捏造混入を固定的に防止。効果測定自体は保留（4.2.3 参照）。
- 出典: `knowledge/legacy_claude_work/reports/誤りの型分析と日本語名の付与_20260812.md` (2026-08-12)

#### 3.4.7 制約版 GOLD ハーネスにおける正当回答の扱い
- 課題: Column 検出で `present:false`（prompt が許容する正当回答）を reject として扱い、membership 6 バッチが一度も送られず run 全体が破棄されていた。Environment の `unresolved`（申告済み正当回答）も provider 障害として扱われ、サーキットブレーカーが開いて残り unit が無送信のまま停止していた。
- 決定: `present:false` と申告済み `unresolved` は accept として記録し、採点対象とする。
- 成果: フェイク router 通し検証で、修正前コードは 8 件中 5 件失敗、修正後 8 件全合格。
- 出典: `knowledge/legacy_claude_work/reports/画像LLM三本化_Claude作業記録_20260812.md` (2026-08-12)

### 3.5 LLM 運用・コスト管理

#### 3.5.1 429 リトライと指数バックオフ
- 課題: `call_gemini` にリトライ機構がなく、瞬間的な 429 で処理全体が停止していた。
- 決定: 429 / 5xx を捕捉して指数バックオフ（15 → 30 → 60 → 120 秒、最大 4 回）、`Retry-After` を尊重、恒久的失敗（400 / 401 / 403 / 404）は即エラー、例外型は `GeminiAPIError(OSError)` に統一する。さらに 429 本文の `limit: N` を読み、記録済み呼出し回数が超過済みなら待たず即座に諦める。
- 成果: `test_llm_retry.py` 44 PASS / 0 FAIL、`test_roundtrip` 440 PASS。
- 出典: `knowledge/legacy_claude_work/patches/llm_extract_retry.py.md`, `knowledge/legacy_claude_work/reports/モデル選定と効率化_20260810.md` (2026-08-10)

#### 3.5.2 トークン推定器の統一（日本語の過小評価是正）
- 課題: `call_gemini` 内の `len(prompt) // 4` は日本語で実測比 3.26 倍の過小評価となり、`max_tokens_per_call` ガードが実質無効化されていた。
- 決定: `ceil(utf8_bytes / 3)` に統一する（`pilot_llm.estimate_tokens` と同一式）。
- 根拠: Bedrock 実測で送信 10,046 文字 → 実測 9,449 トークンに対し推定 10,042（誤差 +6.3%、安全側）。
- 出典: `knowledge/legacy_claude_work/reports/429診断_20260810.md`, `プロバイダ検証結果_20260811.md` (2026-08-10 / 11)

#### 3.5.3 LLM 会計を SQLite に一本化
- 課題: 旧 `config/llm_usage.json` 経路と router 経由の SQLite 経路が互いに独立に動作し、枠の二重消費とガード不全が起きうる状態であった。`config/llm_limits.json` の `max_calls_per_day: 200` は実枠 20 と 10 倍ずれていた。
- 決定: `today_usage()` / `record_usage()` / `load_limits()` のシグネチャを維持しつつ内部を SQLite（`LLMRuntimeStore`）の薄ラッパーとする。`llm_usage.json` は読み取り専用の移行元として 1 回だけ取り込み、以後は不変とする。Gemini の数値上限の正本を `llm_routing.json` に一本化する（20 calls/day、500,000 tokens/day、120,000 tokens/call）。
- 成果: 2026-08-06 〜 11 分の 55 回・1,686,931 トークンが一致し再実行 0 件。pytest 220 件 ＋ standalone 回帰 583 件、計 803 件すべて合格。ルート監査 0 error / 2 warning / 5 info、SQLite `integrity_check = ok`、未解放 reservation 0 件。
- 出典: `knowledge/legacy_claude_work/reports/緊急提案_会計二重化_20260811.md` (2026-08-11), `docs/LLM_ROUTING.md`

#### 3.5.4 プロバイダ切替は図幅単位で行う
- 課題: モデル名がキャッシュキーに含まれるため、1 図幅内でモデルが混在すると失効・衝突が起きる。実際に `gemini-3.5-flash` と `3.6-flash` の混在で `unit_id` 重複（`m1286_p019`〜`p022`）が発生した。当初のステージ別分散案は未検証モデル（`claude-haiku-4-5`）に費用の 71% を配分しており、全国処理で 262 ドルと 200 ドルクレジットを超過する試算であった。
- 決定: 図幅処理開始前に残枠を確認し、その図幅の全ステージで単一プロバイダに統一する。ステージ別分散は `compare_units.py` でどの工程が弱いか判明してから行う。
- 根拠: 単一案は 1 図幅 0.134 ドル・全国 174 ドル（未検証モデル 0）、分散案は 1 図幅 0.2019 ドル・全国 262 ドル。
- 出典: `knowledge/legacy_claude_work/reports/AWS_Azure組み込み仕様_Codex向け_20260811.md` (2026-08-11。`Bedrock組み込み仕様_Codex向け_20260811.md` を明示的に置換)

#### 3.5.5 プロバイダ追加は既存 executor 注入口を使い、本体を書き換えない
- 課題: プロバイダ追加の提案が既存 3 ファイルの本文を書き換える設計であった。
- 決定: `pdf_unit_bootstrap.py:479`、`pdf_field_extract.py:360`、`pdf_alias_mapping.py:177` に既存の `executor` 注入口があるため、それを使う。本体は変更しない。
- 出典: `knowledge/legacy_claude_work/reports/groq提案レビュー_20260810.md`, `Bedrock組み込み仕様_Codex向け_20260811.md` (2026-08-10 / 11)

#### 3.5.6 フェイルオーバーは順序付きチェーンとし、同時送信・結果合成は行わない
- 決定: 各ステージに主系 1・第 1 予備 1・第 2 予備 1（最大 3 候補）の順序付きフェイルオーバーを設定する。同一入力の複数社同時送信および結果合成は行わない。
- 2026-08-11 時点のチェーン: unit alias = Groq → Azure gpt-5-mini → Cohere / body field・unit bootstrap = Mistral API → Bedrock Mistral Large 3 → Cohere / nationwide Abstract = Mistral API → Cohere → Gemini / Column Vision・PDF environment = Mistral API → Gemini。
- 出典: `knowledge/legacy_claude_work/reports/LLM運用配分監査_20260811.md`, `docs/LLM_ROUTING.md`

### 3.6 データ保全・運用

#### 3.6.1 ワークブック保護機構
- 課題: 既存 Review Excel があると実行が停止するため `--force` が常用化し、それが一戸ワークブック消失事故（4.4.4）の要因となった。
- 決定: `--force` なしでも実行が完走して `candidate-<日時>.xlsx` へ出力し、`--force` 時は本体を `.before-<日時>.xlsx` へ退避、GOLD 束縛対象 JSON を毎回 `system/backup-<日時>/` へ退避する。
- 成果: `scripts/pilot.py` に実装、固定テスト `test_pilot_workbook_guard.py` を配備。
- 出典: `knowledge/legacy_claude_work/reports/Codex向け引き継ぎ_20260813.md` (2026-08-13)

#### 3.6.2 実装は scripts/ に一本化する
- 決定: `claude_work/scripts/` との二重管理は過去に事故を起こしたため廃止し、実装は `scripts/` のみとする。`run.py` はコマンド振り分けのみを担う。
- 出典: `docs/SYSTEM_DESIGN.md`

#### 3.6.3 Cold-start は fail-closed 監査とし、GOLD を本番から隔離する
- 決定: Cold-start 生成は GSJ 一次資料のみを入力とし、Review ワークブック・GOLD・派生 JSON 等の混入を事前監査で fail-closed に検出する。GOLD は `claude_work/gold_snapshots/` に `evaluation_only` として隔離し、本番モジュールは GOLD リゾルバを import しない。
- 出典: `docs/COLD_START.md`

#### 3.6.4 全国一括 LLM 実行の禁止
- 決定: 原則 1 図幅 1 回、キャッシュ再利用。既存の人間編集 Excel は自動上書きしない（`--force` 時も `.bak_日時.xlsx` へ退避）。全国バッチ実行はユーザーの明示許可を要する。
- 出典: `docs/AI_HANDOFF.md`

---

## 4. 棄却されたアプローチ・拡張アーカイブ（Negative Evidence, 2026-08-19 統合）

### 4.1 LLM プロバイダ・インフラ

#### 4.1.1 Groq（llama-3.3-70b-versatile）への一括移行
- 試み: テキスト解析 3 ステージを Groq へ振り分け、「1 日 1.4 万回無料」を根拠に 429 解消を狙う提案。
- 結果: Groq 公式レート制限表では `llama-3.3-70b-versatile` は RPD 1K・TPM 12K・TPD 100K であり、14.4K は `llama-3.1-8b-instant` の値との混同であった。本パイプラインの 1 コール平均約 31,400 〜 42,000 トークンは TPM 12K の約 3.5 倍で 1 本も通らない。加えて提案コードは `model` 引数の `or` 短絡により常に `"gemini-3.6-flash"` という非 None の値が Groq へ送られ、`404 model_not_found` → 無言で Gemini へフォールバックし Groq が一度も使われない設計バグがあった。
- 教訓: 無料枠の数字は一次情報（公式表）で確認する。検証計画に「どのプロバイダが実際に使われたか」を含めないとこの種のバグは検出できない。
- 出典: `knowledge/architecture/Groq_Integration_Proposal.md`, `knowledge/legacy_claude_work/reports/groq提案レビュー_20260810.md` (2026-08-10)

#### 4.1.2 429 の原因を TPM 超過と推定したこと
- 試み: Google 429 の原因を Gemini 3.6 Flash 無料枠 TPM 250,000 の超過と推定（信頼度 medium と明記）。
- 結果: 実エラー本文で否定された。超過していたのはリクエスト数の日次枠（`generate_content_free_tier_requests, limit: 20`）であった。同時に、プロンプト分割の推奨も撤回した（分割はリクエスト数を増やし悪化させるため）。
- 教訓: 未確認の解釈には信頼度を明記し、実測が得られ次第訂正する。
- 出典: `knowledge/legacy_claude_work/reports/429診断_20260810.md` → `モデル選定と効率化_20260810.md` (2026-08-10)

#### 4.1.3 429 リトライパッチ初版の 2 つの誤り
- 試み: (1) `urllib.error.URLError` を一律リトライ対象にした。(2) 最終失敗を `RuntimeError` で投げた。
- 結果: (1) ネットワーク遮断環境でプロキシ 403 を 225 秒待つ無意味な遅延が発生した。(2) `pilot.py:912` の `except (BudgetExceeded, ColumnVisionError, OSError, ValueError)` から漏れ、`test_pdf_unit_bootstrap.py` が 7 PASS → 2 ERROR へ退行した。
- 教訓: 例外の型を変える際は、その型を捕捉している呼び出し側を先に `grep` で確認する。
- 出典: `knowledge/legacy_claude_work/patches/llm_extract_retry.py.md` (2026-08-10)

#### 4.1.4 無料枠プロバイダによる Gemini 代替（横断調査）
- 試み: Groq / Cerebras / Mistral / OpenRouter / GitHub Models / Cohere / Cloudflare / NVIDIA を「9 万トークンの日本語＋画像を無料で通せるか」で比較。
- 結果: Google 以外は全滅。Cerebras / Cloudflare はコンテキスト上限（8K 前後）、Groq は 12K TPM、Cohere は非商用限定。
- 教訓: 「4 万 〜 9 万トークンの日本語入力＋マルチモーダル」を要件条件とする限り、無料枠での代替は事実上不可能である。
- 出典: `knowledge/legacy_claude_work/reports/モデル選定と効率化_20260810.md` (2026-08-10)

#### 4.1.5 Cohere / NVIDIA / Bedrock Claude Haiku 4.5 の GOLD 不合格
- 試み: 各社モデルで Column Vision / PDF Environment / alias / main Abstract の GOLD を実施。
- 結果:
  - Cohere `command-a-vision-07-2025` Column Vision: 42 期待中 0 採用（`json_parse` エラー、出力 2,048 トークンで打ち切り）。
  - Cohere PDF Environment: 5 対象中 TP 1・FP 1、recall 0.2、precision 0.5 で基準未達。
  - NVIDIA `nvidia/nemotron-3-nano-30b-a3b` alias: 19 マッピング中 0/19、出力ちょうど 2,048 トークンで打ち切り。同 main Abstract: 87 フィールド中 0 件、出力上限 16,384 トークンで約 162 秒後に打ち切り。実測応答 64.8 秒（1 図幅 5 コールで 5 分）。
  - Bedrock Claude Haiku 4.5 Column Vision: 42 対象中 TP 31・FP 24、precision 0.563636、recall 0.738095（基準 precision 1.0 / recall 0.85 未達）。同 PDF Environment: 5 対象中 TP 1・FP 1、precision 0.5、recall 0.2 で未達。通信は HTTP 200 で成功しており問題は精度のみ。
- 教訓: 出力上限到達による JSON 破損が疑われるが、生レスポンスは保存方針上残しておらず、これは推定であって確定事実ではない。設定上 `enabled: true` でも実質使われていない「死んだルート」が生じうるため、定期的に実使用回数を監査する。
- 出典: `docs/LLM_ROUTING.md`, `knowledge/legacy_claude_work/reports/緊急提案_会計二重化_20260811.md`, `LLM運用配分監査_20260811.md`, `死んでいる部分_20260811.md` (2026-08-11)

#### 4.1.6 予算会計の二重化（JSON と SQLite の並存）
- 試み: `call_gemini` 経由は JSON（`config/llm_usage.json`）、`llm_router` 経由は SQLite に記録し、分岐は「`api_key` が渡されたら旧経路」としていた。
- 結果: どちらも相手の消費を知らないため、一方が 20 回使い切ってももう一方は 0 回と認識してさらに呼べる状態であった。
- 教訓: 会計は必ず単一の正本に一本化する（3.5.3 で解消）。
- 出典: `knowledge/legacy_claude_work/reports/死んでいる部分_20260811.md` (2026-08-11)

### 4.2 抽出・データ処理

#### 4.2.1 Column の all-or-nothing 却下設計
- 試み: Vision 提案が GOLD の west 19 / central 5 件と件数一致していたにもかかわらず、48 件中 30 件しか返せなかったため提案全体を却下し `unsplit` に落としていた。
- 結果: 却下理由の内訳は「no valid Column membership」18 件、「response did not return every canonical unit」1 件、「interval_not_in_controlled_list: Early Pliocene」1 件。正しい 30 件分の割当も一緒に破棄された。
- 教訓: 部分採用を許容する設計が必要である（コードは後に実装済み、ただしキャッシュに `assignment_ready: False` が焼き付いており出力未反映）。
- 出典: `knowledge/legacy_claude_work/reports/課題一覧_20260811.md`, `システム分析_20260811.md` (2026-08-11)

#### 4.2.2 Column 検出 prompt の内部矛盾
- 試み: 独立した 2 モデル（Bedrock Claude Haiku 4.5、OpenRouter gemma-4-26b）に Column 検出をさせた。
- 結果: 両モデルとも 3 Column すべて `present: false`。`build_column_detection_prompt` は「diagram panel は Column でない」と明記する一方、正解は図中の地理パネル（西部・中部・東部）を Column と認めることを要求しており矛盾していた。
- 教訓: prompt 内の禁止規則と正解定義の整合を必ず確認する。なお、この事例の真因は別途ページ束縛ミス（3.4.4）であり、prompt の矛盾は独立に存在した第二の問題であった。
- 出典: `knowledge/legacy_claude_work/reports/制約版GOLD_v2実行結果_20260812.md` (2026-08-12)

#### 4.2.3 不安定な provider での効果測定
- 試み: json_parse 修正後、OpenRouter `gemma-4-26b:free` で日本語別名併記の効果を測定しようとした。
- 結果: 毎回異なる地点で `json_parse` に落ち、6 バッチのうち完走数が実行ごとに変わった。
- 教訓: この条件下の数値（prompt v1: TP 10/42, precision 0.417 / v2 日本語名あり: TP 6/42, precision 0.375）を比較材料に使ってはならない。完走できる provider で測定するまで、日本語別名の効果は不明である。
- 出典: `knowledge/legacy_claude_work/reports/誤りの型分析と日本語名の付与_20260812.md`, `json_parse耐性の修正_20260812.md` (2026-08-12)

#### 4.2.4 地層名の部分文字列一致によるマッチング
- 試み: 地層名の照合に部分文字列一致を使用した。
- 結果: `towada` が `towada caldera forming stage tephra` にも一致し、十和田テフラ群 3 件が段丘堆積物の行へ誤って流入し年代を 3 回上書きする事故が発生した。
- 教訓: 単語集合一致とし、短い方が 2 語以上であることを条件とする。
- 出典: `docs/SYSTEM_DESIGN.md`

#### 4.2.5 カンマ区切りの無条件分解
- 試み: 「`REF_` と `_` 以外は全部分解」とした。
- 結果: 英文 `unit_description` のカンマ数が Column 数と偶然一致し、文が真っ二つに割れる事故が発生した（十和田で 6 行）。
- 教訓: 分解可能列をホワイトリスト化する。
- 出典: `docs/SYSTEM_DESIGN.md`

#### 4.2.6 ZFK parent_age 境界からの prop 自動計算
- 試み: `parent_age.lower_age_ma` / `upper_age_ma` から prop を計算しようとした。
- 結果: これらは年代区分自体の境界であり地層固有の年代ではないため、計算しても prop = 0 / 1 にしかならない（十和田 7 区分中 5 区分で境界値が完全一致）。
- 教訓: 自動化を断念し、別経路で求める。
- 出典: `docs/SYSTEM_DESIGN.md`

### 4.3 データソース選定

#### 4.3.1 200k シームレス地質図を丸ごと column として流し込む計画
- 試み: 「200k 補完による全国 100% カバー」を、50k（上層）＋ 200k（下層）の 2 層レイヤー構造として設計した。
- 結果: map 系統（面的属性）と column 系統（層序累重）の混同、全国 100% カバーは column の目標ではないこと、「2 層レイヤー構造」は Macrostrat に存在しない概念であること、が指摘され致命的欠陥ありと判定された。
- 教訓: シームレス地質図は map source として提供すべきであり、LLM は不要でコストはほぼ 0 である。書き換え案として、(1) 200k を map source、(2) 50k を column、(3) 解説面がある 1999 年以降刊行分のみ低解像度 column とする案が提示された（未実施）。
- 出典: `knowledge/legacy_claude_work/reports/200k補完計画_レビュー_20260811.md` (2026-08-11)

#### 4.3.2 5 万分の 1 土地分類基本調査を穴埋め資料に使う案
- 試み: 全国カバー資料として候補視した。
- 結果: 調査対象が「北海道のほぼ全域と本州の山間地の一部を除く」約 30 万 km² であり、まさに 50k 未刊行域と重なることが判明した。一戸（0308）簿冊 PDF はテキスト層なし（空）と実測確認された。
- 教訓: 穴埋め資料としては不採用。副次的に手動正解データの相互検証にのみ使用可能。
- 出典: `knowledge/legacy_claude_work/reports/穴埋め資料_実地検証_20260811.md` (2026-08-11)

#### 4.3.3 単一機関のカタログのみに依拠した判定（北海道の二重誤判定）
- 試み: GSJ カタログのみを根拠に「北海道は 50k・200k・土地分類調査とも空白」と判定した。また「200k 解説面には層厚が体系的に載っていない」「新しい面のみ統合層序図を持つ」と判定した。
- 結果: いずれも後続調査で訂正された。北海道は道総研資料を含めれば 6 区画を除き全域網羅（索引 153 図幅、説明書 PDF 107 本）。200k 解説面には層厚記載があり（京都及大阪・野辺地で実文確認）。層序対比図はどの面にも存在し、差は有無ではなく粒度であった。
- 教訓: 単一機関のカタログのみに依拠した判定は誤りやすい。北海道の見落としは 2 回連続で発生した（GSJ に北海道が無いと誤認 → 道総研の方が網羅的と誤認）。
- 出典: `knowledge/legacy_claude_work/reports/北海道_調査結果_20260811.md`, `層序図検証と北海道_20260811.md`, `50k刊行面数_集計途中_20260811.md` (2026-08-11)

#### 4.3.4 200k 全国統合マスター旧版の不整合
- 試み: 提出アーカイブの全国統合マスターを利用しようとした。
- 結果: 455 Columns / 3,091 Units であり、現行の正規集合 803 Columns / 5,838 Units と不一致であった。
- 教訓: 旧版扱いとしリリース件数に使用しない。正規 112 レビュー集合から再生成する。
- 出典: `docs/Codex_200k_V2_Handoff_Report.md` (2026-08-14)

### 4.4 ツール・実装上の落とし穴

#### 4.4.1 openpyxl の insert_cols による数式破壊
- 試み: 列を途中に挿入した。
- 結果: `openpyxl` の `insert_cols` は数式参照を書き換えないため、`t_prop` / `b_prop` の VLOOKUP が別列を指し黙って壊れた。
- 教訓: 列構成変更時は「値読み出し → 並べ直し → 数式再生成」の手順を徹底する。
- 出典: `docs/SYSTEM_DESIGN.md`

#### 4.4.2 フリーズペインで列を固定
- 試み: `G2` 固定にした。
- 結果: 幅広い `REF_*` 列が約 1100px の固定領域となり、G 列以降へスクロールできなくなった。
- 教訓: 見出し行のみ固定（`A2`）とする。
- 出典: `docs/SYSTEM_DESIGN.md`

#### 4.4.3 安全条件を満たさない画像 Vision 実装
- 試み: 層序図かどうか未確認のまま先頭の大きい画像を Vision へ送信した。
- 結果: 実際には県内位置図であった。加えて使用量・画像 hash が未記録、地名検証なしで全 Column に同一座標を付与、Web geocoding により再現性が失われる等の問題が発覚した。
- 教訓: `pdf_image_extract.py` のページ rank 方式および `llm_column_vision.py` の cache / 検証付き実装へ置換して回復した。画像送信前には「何の図か」の判定と hash 記録を必須とする。
- 出典: `docs/AI_HANDOFF.md`

#### 4.4.4 一戸ワークブック消失事故
- 試み: `python run.py ichinohe --force` を実行した。
- 結果: 当時 LLM 4 プロバイダが全滅しており、NO_DATA プレースホルダ（1 unit）が 48 ユニット版 `raw_bundle.json` / `compiled.json` を上書きした。原本は復元不能であった（ヘルプには「自動バックアップあり」と記載されていたが未実装であった）。GOLD fixture の sha256 束縛が切れ 16 件のテストが失敗した。
- 教訓: 破壊的操作のガードは「ヘルプの記載」ではなく「実装とテスト」で担保する。本事故が 3.6.1 のワークブック保護機構導入の契機となった。人手レビュー済み `Ichinohe_reference_GOLD.xlsx`（42 units）は無傷であり判断材料として残った。
- 出典: `knowledge/legacy_claude_work/reports/Codex向け引き継ぎ_20260813.md` (2026-08-13)


---

# 手動正解データ策定基準 (Ground Truth Methodology)

﻿# Ground Truth Methodology & Validation Protocol

本ドキュメントは、研究者が説明書本文および地質図を目視精読して作成した手動正解データ（Ground Truth）の定義、作成基準、および自動パイプライン出力との比較照合プロトコルを記録したものである。

---

## 1. Ground Truth の定義と位置付け
- **定義**: 地質学的専門知識を持つ研究者が、GSJ地質図幅説明書（PDF）および図幅本体を目視精読し、手動で整理・入力した層序カラムデータ（`data/50k/03_submission/` 等に配置）。
- **役割**: 自動抽出パイプラインの精度評価、不一致箇所の特定、およびアルゴリズム改善のための客観的基準（教師データ）。

---

## 2. 目視精読による地質属性の同定基準

1. **層序単元（Stratigraphic Units）**:
   - 説明書の地質層序表および凡例に記載された正式名称（例: 一戸層、折壁部層）を基本単位とする。
2. **層序境界関係（Basal Surface Contacts）**:
   - 本文各論の地層境界記載に基づき、`conformable`（整合）、`unconformable`（不整合）、`intrusive`（貫入）、`fault`（断層）を判定。
3. **年代範囲（Age Constraints）**:
   - 化石生層序、放射年代値、対比表の記述から地質時代区間および絶対年代数値（Ma）を同定。
4. **岩相（Lithology）**:
   - 説明書記載の主岩相および副次岩相を、Macrostrat公式統制語彙（`official_vocab.json`）へ適合。

---

## 3. 自動パイプライン出力との照合・評価プロトコル

1. **一致度評価マトリクス**:
   - ユニット同定一致率: パイプラインが抽出したユニットと手動正解データの対応関係。
   - 年代区間包含度: パイプラインが算出した年代区間 $[t\_age, b\_age]$ が手動設定年代と整合しているか。
   - 岩相・環境一致度: 統制語彙へのマッピング精度。
2. **不一致分析（Discrepancy Analysis）**:
   - 不一致が「パイプラインの誤読（False Positive / False Negative）」によるものか、「原典テキストの曖昧性」によるものかを分類し、ルール改修へ反映する。

---

# 開発年代記 (Project Chronicles)

﻿# Macrostrat Japan Development Chronicles & Agent Heritage

本ドキュメントは、プロジェクトの立ち上げから現在に至る開発の変遷、主要マイルストーン、および各AIエージェントの貢献履歴を記録する年代記である。

---

## Epoch 1: 50k パイプラインの黎明と十和田図幅の提出 (2026年8月上旬)
- **主要エージェント**: Claude, Codex
- **達成事項**:
  - GSJ 50k カタログのスクレイピングおよび十和田図幅（`m1050_十和田 2005`）の提出ワークブック生成。
  - Macrostrat 公式統制語彙辞書（`official_vocab.json`）の構築。

## Epoch 2: PDFオンリー図幅（一戸 m1286）の開拓とゴールデン化 (2026年8月中旬)
- **主要エージェント**: Claude Code, Codex, Antigravity
- **達成事項**:
  - GISデータのない一戸図幅に対する8段階抽出パイプラインの確立。
  - 英文要約（Abstract）解析と日本語本文深層抽出のハイブリッド化。
  - 3カラム幾何アンカリングと450件のエビデンス完備（Review-v2仕様）。

## Epoch 3: ワークスペース統合・2層ループ・記憶伝承フレームワーク (2026年8月18日)
- **主要エージェント**: Antigravity (Gemini 3.7 Flash)
- **達成事項**:
  - OneDrive からローカル（`C:\Users\somas\projects\MacroStrat`）への全データ完全移行と旧環境消去。
  - 50k と 200k のフォルダ物理分離。
  - 第1ループ（Claude/Codex）と第2ループ（研究者）の2層ループ運用の確立。
  - `specs/MEMORY.md`, `specs/TASK.md`, `specs/FEEDBACK.md` によるポチッと連携サイクルの配備。
  - `knowledge/memory_vault/` による知見・失敗知識の永続保管庫の設置。
---

## 詳細年表（2026-08-10 〜 2026-08-14）

Epoch 1 および Epoch 2 に対応する日次の作業記録を、`knowledge/legacy_claude_work/reports/` および `docs/` から復元したものである（2026-08-19 統合）。

### 2026-08-10
- 429 エラーの診断を実施し、原因を Gemini 無料枠 TPM 超過と推定（信頼度 medium）。同日中に実エラー本文から日次リクエスト枠（limit: 20）の超過と判明し撤回。プロンプト分割案も同時に撤回。出典: `429診断_20260810.md`, `モデル選定と効率化_20260810.md`
- Groq 統合提案書のレビューで致命的欠陥 2 件（無料枠数値の誤認、モデル引数の `or` 短絡バグ）を指摘し不採用。出典: `groq提案レビュー_20260810.md`
- `llm_extract.py` に 429 リトライとトークン推定修正を適用（`test_llm_retry.py` 44 PASS）。出典: `llm_extract_retry.py.md`
- config 4 ファイルの点検と修正差分作成（`age_mapping` 19 件修正、`lithology_mapping` 366 トークン修正ほか）。出典: `config点検レポート_20260810.md`, `config修正差分_20260810.md`
- PDF Unit Extraction v2 の提案・実装により、一戸の inventory が 27 → 48 ユニットへ拡大。出典: `knowledge/architecture/PDF_Unit_Extraction_Proposal.md`
- 京都及大阪（2026 年刊）解説面 PDF で層序総括図・層厚記載を実地確認。出典: `穴埋め資料_実地検証_20260811.md`

### 2026-08-11
- Macrostrat API を実測（一戸 lat 40.2 / lng 141.3）。column 該当 0 件、map source は Chorlton 世界図（1:3,500 万）のみと確認。出典: `200k補完計画_レビュー_20260811.md`
- 200k 補完計画をレビューし、map 系統と column 系統の混同という致命的欠陥を指摘。出典: 同上
- 北海道を「空白ではない」と訂正。道総研資料で 6 区画を除き全域網羅（索引 153 図幅、説明書 PDF 107 本）を確認。出典: `北海道_調査結果_20260811.md`, `層序図検証と北海道_20260811.md`
- 一戸 48 ユニットに対し公式 L1 柱状図の Vision 実行。western / central / eastern の 3 列へ 46 件割当（2 件 unassigned）。出典: `PDF_Unit_Extraction_Proposal.md`
- 野辺地（200k）で column 成立性を測定。29 ユニット中 lng/lat 充足 0%、層厚両側充足 20.7%。出典: `200k_column成立性_野辺地_20260811.md`
- `compare_units.py` により m1286 出力の `unit_id` 重複 4 件・年代誤伝播 17 件を発見。同日中に `pdf_unit_bootstrap.py` / `age_resolution.py` / `export_submission.py` へ修正を適用し、適用時に仕様を「一意性」から「単射性」へ訂正。出典: `m1286_データ不整合_20260811.md`, `unit_id重複と年代誤伝播_修正提案_20260811.md`
- 手動正解データとの比較: 修正前は一致 47 / 不一致 48 / 捏造 1 / 取りこぼし 108、修正後は一致 48 / 不一致 18 / 捏造 0 / 取りこぼし 137。出典: `比較_GOLD_vs_現行_20260811.md`, `比較_修正後_20260811.md`
- AWS Bedrock・Azure OpenAI の取得手順を作成し、両方の実機疎通を確認。全プロバイダの実測検証を実施（Groq 失敗の原因は UA、NVIDIA 応答 64.8 秒）。出典: `AWS_Bedrock_取得手順_20260811.md`, `Azure_Foundry_取得手順_20260811.md`, `プロバイダ検証結果_20260811.md`
- 予算会計の二重化（JSON / SQLite）を報告し、同日中に SQLite 一本化で解消。出典: `緊急提案_会計二重化_20260811.md`
- システムの「死んでいる部分」を調査（NVIDIA 全 6 ルートが実質未使用、OpenRouter は routes 出現 0 回）。出典: `死んでいる部分_20260811.md`
- Bedrock 単独の組み込み仕様を作成後、AWS + Azure 統合仕様で置換。ステージ別分散案を撤回し図幅単位切替へ。出典: `Bedrock組み込み仕様_Codex向け_20260811.md`, `AWS_Azure組み込み仕様_Codex向け_20260811.md`
- LLM 運用配分を監査し、順序付きフェイルオーバーのチェーンを確定。出典: `LLM運用配分監査_20260811.md`

### 2026-08-12
- OpenRouter の `json_parse` 失敗の原因（括弧切り出し方式の欠陥）を特定し修正。出典: `json_parse耐性の修正_20260812.md`
- 画像 LLM 三本化ハーネスの 3 欠陥（`present:false` 誤判定、`unresolved` 誤判定、語彙ゲート誤棄却）を特定・修正。`CONSTRAINED_VALIDATOR_VERSION` を v1 → v2 へ。Environment 到達可能性 3/5 → 5/5。出典: `画像LLM三本化_Claude作業記録_20260812.md`
- 制約版 GOLD v2 を実行。OpenRouter・Bedrock とも BLOCKED。Column 検出 prompt の内部矛盾を新たに発見。出典: `制約版GOLD_v2実行結果_20260812.md`
- Column 検出失敗の真因がページ束縛ミス（PDF 15 → 16）と判明。修正により membership TP 0/42 → 10/42、recall 0.000 → 0.238。出典: `Column検出の真因_ページ束縛ミス_20260812.md`
- 施策 A（Environment 証拠図の差し替え）・B（Column 解像度 x2 → x3）・C（OpenRouter 再測定）を実施。出典: `ABC実施結果_図と解像度の修正_20260812.md`
- Column membership の誤り型を分析（not_answered 14 / shifted 9 / invented 7 / over 4 / exact 2）し、日本語別名併記を実装（prompt v2）。出典: `誤りの型分析と日本語名の付与_20260812.md`
- 一戸ワークブック消失事故が発生（`run.py ichinohe --force` により NO_DATA プレースホルダが 48 ユニット版を上書き）。出典: `Codex向け引き継ぎ_20260813.md`

### 2026-08-13
- bootstrap 抽出の 2 段バッチ化とワークブック保護機構を実装。出典: `Codex向け引き継ぎ_20260813.md`
- 50k columnization plan（ZFK / Shapefile 統合版、Phase 0〜5）を策定。出典: `50k_columnization_plan_zfk_shapefile_20260813.md`

### 2026-08-14
- 200k v1 の P0 改修が完了し、全 112 図幅で厳格バリデータ 0 errors を確認。出典: `docs/Codex_Handoff_Summary.md`, `docs/Codex_Submission_Report.md`
- 200k v2_polygon_column の WP0〜WP3 を実装し、京都及大阪で 10 ドメイン提案を実証。出典: `docs/Codex_200k_V2_Handoff_Report.md`

---

## Epoch 4: 記憶の統合と版管理の導入 (2026年8月19日)
- **主要エージェント**: Claude (Cowork)
- **達成事項**:
  - ワークスペースを `C:\Users\somas\projects\MacroStrat` へ完全移行（OneDrive 側は空）。
  - `knowledge/legacy_claude_work/reports/` の 38 件および `docs/`・`knowledge/architecture/` の設計文書を、出典付きで `memory_vault` の 4 文書へ圧縮統合。
  - データソースの実測値を `DATA_SOURCE_LEDGER.md` として新設。
  - Git による版管理を導入し、初回コミットを作成。
- **既知の課題**:
  - `knowledge/legacy_claude_work/` と `knowledge/archives/claude_work/` が内容重複（`__pycache__` を除き同一）。
  - `docs/` と `knowledge/legacy_docs/` が重複（差分は BOM のみ）。


---

# データソース精度台帳 (Data Source Accuracy Ledger)

# Data Source Ledger & Measured Benchmarks

本ドキュメントは、Macrostrat Japan プロジェクトで実測・確認されたデータソースの被覆状況、ライセンス条件、および各種ベンチマーク数値を、出典付きで台帳化したものである（2026-08-19 初版、`knowledge/legacy_claude_work/reports/` および `docs/` から統合）。

数値はいずれも記録された時点の実測値である。再利用の前に、出典先と現在の状態を照合すること。推測値は記載しない。

---

## 1. データソース被覆状況

### 1.1 5 万分の 1 地質図幅（50k）

| 項目 | 実測値 | 出典 |
| :-- | :-- | :-- |
| 総図幅数（計画上の母数） | 763 図幅 | `50k_columnization_plan_zfk_shapefile_20260813.md` |
| ZFK 保有 | 121 件（15.9%） | 同上 |
| Shapefile 保有（計） | 403 件（52.8%） | 同上 |
| PDF のみ | 254 件（33.3%） | 同上 |
| Viewer 画像のみ | 106 件（13.9%） | 同上 |
| PDF 取得成功 / 未取得 | 655 / 108 | `docs/Codex_Submission_Report.md` |
| GSJ カタログ 16 区画中 6 区画の刊行面数 | 333 面（網走 61 / 釧路 67 / 旭川 48 / 札幌 66 / 青森 35 / 秋田 56） | `50k刊行面数_集計途中_20260811.md` |
| 1 面の被覆面積 | 約 15 km × 20 km（約 300 km²、公式値） | 同上 |
| 全国陸域からの必要面数概算 | 約 378,000 km² ÷ 300 km² ≒ 1,260 面 | 同上 |

注: 残り 10 区画は未集計であり、全国 50k の実質カバー率および「全 763 面」の出典は未確定である。出典: `50k体系の再整理_20260811.md`, `50k刊行面数_集計途中_20260811.md`

### 1.2 20 万分の 1 シームレス地質図（200k）

| 項目 | 実測値 | 出典 |
| :-- | :-- | :-- |
| カタログ掲載面数 | 112 面 | `200k解説面_全国集計_20260811.md` |
| 解説面あり / なし | 42 面 / 70 面（カバー率 37.5%） | 同上 |
| うち北海道 | 26 面中 2 面（7.7%） | 同上 |
| v1 現況（2026-08-14） | 803 Columns / 5,838 Units、全 112 図幅で厳格バリデータ 0 errors | `docs/GSJ_200k_System_Report.md`, `docs/Codex_Submission_Report.md` |
| v1 environment 語彙警告 | 2,586 件（未解消） | 同上 |
| 旧版全国統合マスター（使用不可） | 455 Columns / 3,091 Units、現行正規集合と不一致 | `docs/Codex_200k_V2_Handoff_Report.md` |

注: 200k 解説面 42 面の分布が 50k 未刊行域とどれだけ重なるかは未検証である。出典: `200k解説面_全国集計_20260811.md`

### 1.3 北海道

| 項目 | 実測値 | 出典 |
| :-- | :-- | :-- |
| 道総研 50k 被覆 | 「6 区画を除いて全域網羅」（公式ページ記載） | `北海道_調査結果_20260811.md` |
| 索引図幅数 | 153 図幅 | 同上 |
| 説明書 PDF | 107 本（道総研ホスト） | 同上 |
| 参照可能な機関 | 地質調査所 / 北海道開発庁 / 道総研の 3 機関分を GSJ カタログ経由で統合参照可能 | `50k刊行面数_集計途中_20260811.md` |

注: 代替資料「日本地方地質誌 1 北海道地方」は品切れ疑い（「在庫問い合わせ」表示）であり、目次が大項目 15 章のみのため層序対比表の有無は現物未確認である。出典: `層序図検証と北海道_20260811.md`

### 1.4 Macrostrat 側の状況（2026-08-11 実測）

| 項目 | 実測値 | 出典 |
| :-- | :-- | :-- |
| 一戸（lat 40.2 / lng 141.3）の column | 該当 0 件 | `200k補完計画_レビュー_20260811.md` |
| 同地点の map source | Chorlton 世界図（1:3,500 万）のみ | 同上 |

Format documentation v0.1.1 の要点:
- `min_thickness` / `max_thickness` は Composite column で必須。
- `b_pos` / `t_pos` なしで行順に依拠する運用は非推奨。

出典: 同上

### 1.5 除外されたデータソース

| ソース | 除外理由 | 出典 |
| :-- | :-- | :-- |
| 5 万分の 1 土地分類基本調査 | 調査対象が「北海道のほぼ全域と本州の山間地の一部を除く」約 30 万 km² であり、50k 未刊行域と重なる。一戸（0308）簿冊 PDF はテキスト層なし（空）と実測確認。 | `穴埋め資料_実地検証_20260811.md` |

---

## 2. ライセンス条件

- GSJ 5 万分の 1 地質図幅、20 万分の 1 シームレス地質図、北海道立総合研究機構の資料: いずれも政府標準利用規約 2.0 に準拠し、出典明記により改変を含む自由利用が可能、申請不要。
- Macrostrat への提出データ: CC-BY 4.0。

出典: 複数レポート横断（`200k補完計画_レビュー_20260811.md`, `北海道_調査結果_20260811.md` ほか）

---

## 3. 抽出精度ベンチマーク（m1286 一戸 2018）

すべて実測値であり、測定条件（プロバイダ・prompt version・fixture）を併記する。条件が異なる数値どうしを比較してはならない。

| 測定 | 条件 | 結果 | 出典 |
| :-- | :-- | :-- | :-- |
| 手動正解データとの突合（修正前） | 2026-08-11 | 一致 47 / 不一致 48 / 捏造 1 / 取りこぼし 108 | `比較_GOLD_vs_現行_20260811.md` |
| 同（unit_id 単射化・年代補完停止の適用後） | 2026-08-11 | 一致 48 / 不一致 18 / 捏造 0 / 取りこぼし 137 | `比較_修正後_20260811.md` |
| ユニット同定の対応関係 | 2026-08-11 | 手動正解 42 / 出力 48 / 名前対応がついたのは 30 件 | `課題一覧_20260811.md` |
| Column membership（ページ束縛ミス時） | Bedrock Claude Haiku 4.5, PDF15 | TP 0/42、recall 0.000、column_detection 3 件とも false | `Column検出の真因_ページ束縛ミス_20260812.md` |
| Column membership（PDF16 修正後） | 同上, PDF16 | TP 10/42、recall 0.238、column_detection 3 件とも true | 同上 |
| Column Vision GOLD | Bedrock Claude Haiku 4.5 | TP 31 / FP 24、precision 0.563636、recall 0.738095（基準 precision 1.0 / recall 0.85 未達） | `緊急提案_会計二重化_20260811.md` |
| PDF Environment GOLD | Bedrock Claude Haiku 4.5 | 5 対象中 TP 1 / FP 1、precision 0.5、recall 0.2（未達） | 同上 |
| PDF Environment GOLD | Cohere `command-a-vision-07-2025` | 5 対象中 TP 1 / FP 1、recall 0.2、precision 0.5（未達） | `docs/LLM_ROUTING.md` |
| Column Vision GOLD | Cohere `command-a-vision-07-2025` | 42 期待中 0 採用（`json_parse` エラー、出力 2,048 トークンで打ち切り） | 同上 |
| alias GOLD | NVIDIA `nemotron-3-nano-30b-a3b` | 0/19、出力ちょうど 2,048 トークンで打ち切り | 同上 |
| main Abstract GOLD | 同上 | 87 フィールド中 0 件、出力上限 16,384 トークンで約 162 秒後に打ち切り | 同上 |
| alias GOLD | Azure gpt-5-mini（validator 修正後） | 19/19 合格、precision / recall 1.0、critical failure 0 件 | 同上 |
| Environment 証拠図差し替え | OpenRouter, figures [55,27] → [16,27] | 完全一致 1/5 → 2/5、accept 2/5 → 3/5 | `ABC実施結果_図と解像度の修正_20260812.md` |
| 誤りの型分布 | Column membership | not_answered 14 / shifted 9 / invented 7 / over 4 / exact 2。混同表で東部 → 西部の誤答が 8 件 | `誤りの型分析と日本語名の付与_20260812.md` |
| 50k Vision 抽出精度 | 一戸、制約付き Column 所属評価 | F1 = 0.272727 | `docs/Codex_Submission_Report.md` |

### 到達可能性の上限（構造的制約）

- Environment: 語彙ゲートが Macrostrat 公式 83 語に固定されていた時点で、正解語 `sublittoral` / `bathyal` が表に無いため max recall 0.600。閉世界を呼び出しごとに切り替える修正後は 1.000。出典: `画像LLM三本化_Claude作業記録_20260812.md`
- Column GOLD: unit 名の綴り不一致（レビュー済み `Floodplain and valley-floor deposits` と canonical inventory `flood-plain and valley-floor deposits`）により上限 recall が 40/42 = 0.952 に制限される。2026-08-12 にユーザー判断で許容。出典: 同上, `画像LLM三本化_引き継ぎ_20260812.md`

---

## 4. 200k Column 成立性（野辺地、2026-08-11 実測）

| 項目 | 実測値 |
| :-- | :-- |
| 対象ユニット数 | 29 |
| lng / lat の充足率 | 0% |
| 層厚（上下両側）の充足率 | 20.7% |

出典: `200k_column成立性_野辺地_20260811.md`

---

## 5. LLM コスト・枠の実測値

| 項目 | 実測値 | 出典 |
| :-- | :-- | :-- |
| Gemini 無料枠（当時） | 20 calls/day、500,000 tokens/day、120,000 tokens/call | `緊急提案_会計二重化_20260811.md`, `docs/LLM_ROUTING.md` |
| 1 コール平均トークン | 約 31,400 〜 42,000 | `groq提案レビュー_20260810.md` |
| Groq `llama-3.3-70b-versatile` の実枠 | RPD 1K / TPM 12K / TPD 100K | 同上 |
| 単一プロバイダ方式のコスト | 1 図幅 0.134 ドル、全国 174 ドル | `AWS_Azure組み込み仕様_Codex向け_20260811.md` |
| ステージ別分散方式のコスト（棄却） | 1 図幅 0.2019 ドル、全国 262 ドル | 同上 |
| 会計移行検証 | 2026-08-06 〜 11 の 55 回・1,686,931 トークンが一致、再実行 0 件 | `緊急提案_会計二重化_20260811.md` |
| NVIDIA 応答時間 | 64.8 秒（1 図幅 5 コールで約 5 分） | `プロバイダ検証結果_20260811.md` |
| トークン推定式の誤差 | `ceil(utf8_bytes/3)` で +6.3%（安全側）。`len//4` は実測比 3.26 倍の過小評価 | `429診断_20260810.md`, `プロバイダ検証結果_20260811.md` |


---

# AIエージェント提案アーカイブ (Agent Proposals Archive)

﻿# AI Agent Proposal Archives & Compressed Knowledge

本ドキュメントは、過去のセッションにおいて各AIエージェント（Claude, Codex, Antigravity）が提案・試行した技術的アプローチ、実験知見、および将来の発展案を圧縮記録したものである。

---

## 1. Claude Code による主要提案と知見

### 1.1 英文要約（Abstract）高速パーサーの着想
- **背景**: 説明書PDFの日本語本文は長文かつ複雑な構造を持つが、末尾の英文要約には年代・岩相・環境が高密度に凝縮されている。
- **知見**: 英文要約から初期値を決定論的に抽出（`local_abstract_science.py`）した上で、日本語本文から詳細（層厚・境界）を補完する2段階アプローチが最も効率的であると実証された。

### 1.2 柱状図の視覚幾何検出（Column Vision）
- **背景**: 1枚の図幅内に複数の地質区（例: 西部・東部）が存在する場合、OCRテキストのみではカラム所属関係を正しく認識できない。
- **知見**: 柱状図画像を切り出し、画像認識モデルまたは幾何学的分割（`column_vision.py`）によりカラムを検出し、地図郭内の緯度経度へアンカリングする手法が有効。

---

## 2. Codex による主要提案と知見

### 2.1 決定論的年代ソルバー（5-Stage Chronology）の分離
- **背景**: LLMに年代計算を行わせると、上下関係の破綻や数値捏造（ハルシネーション）が発生しやすい。
- **知見**: LLMの役割を「原文引用の抽出」に限定し、不等式評価（$b\_age \ge t\_age$）や層序補間計算はすべて Python（`age_resolution.py`）の純粋関数として実装すべきであると実証。

### 2.2 不変条件テスト（Invariants Test Suite）の自動化
- **背景**: パイプラインを改修するたびに、過去に正常動作していた図幅でリグレッション（改悪）が発生するリスク。
- **知見**: `pytest` による自動テストスイートを整備し、コード修正ごとに全件検証を行うガードレール体制を確立。

---

## 3. Antigravity による主要提案と知見

### 3.1 2層ループエンジニアリング（Double-Loop Workflow）の定式化
- **背景**: AIの自律改善ループ（第1ループ）と研究者の学術的ガバナンス（第2ループ）が混在すると、ブラックボックス化や手戻りが発生する。
- **知見**: `specs/TASK.md`（指示）と `specs/FEEDBACK.md`（報告）を境界とし、明確に責務を分離した運用プロトコルを確立。
---

## 4. 拡張アーカイブ（2026-08-19 統合）

本節は、`knowledge/legacy_claude_work/reports/` および `knowledge/architecture/`、`docs/` に残されていた 2026-08-10 〜 08-14 の提案・実験記録を圧縮したものである。実施状況を各項目に明記する。棄却された提案および採択された設計根拠の詳細は `DESIGN_RATIONALE.md` 第 3・4 章を参照すること。

### 4.1 Claude Code による提案・アルゴリズム（実施済）

#### 4.1.1 bootstrap 抽出の 2 段バッチアルゴリズム
段 A で地層名のみを列挙（出力予約 4,096 トークン）し、段 B で 8 件ずつ詳細を取得（予約 `min(4096, 320 × 件数)`）、決定的にマージする。1 応答約 11,400 output token による打ち切りを回避する。実測で 48 ユニット名 / 6 バッチ / 外部コール 7 件、全応答が正常終了した。
出典: `knowledge/legacy_claude_work/reports/Codex向け引き継ぎ_20260813.md`

#### 4.1.2 括弧バランスによる JSON 抽出
文字列とエスケープを考慮して波括弧の対応を数え、最初に閉じた完全なオブジェクトのみを取り出す。従来の「最初の `{` から最後の `}` まで」方式では、正しい JSON の後にモデルが注釈を付けた場合に全体を失敗として捨てていた。診断用に本文を含まない構造カウンタ（chars / open_braces / close_braces / balanced_objects / fenced / starts_with_brace）をエラーへ付与する。
出典: `knowledge/legacy_claude_work/reports/json_parse耐性の修正_20260812.md`

#### 4.1.3 GOLD fixture のページ束縛 preflight
fixture の `pdf_page` と印刷ページ対応表を照合し、不一致なら exit 1 で外部送信を止める。この機構がなかったため、図の無い本文ページを Vision へ送り続けて「モデルの能力不足」と誤診断していた。適用後 membership TP 0/42 → 10/42。
出典: `knowledge/legacy_claude_work/reports/Column検出の真因_ページ束縛ミス_20260812.md`

#### 4.1.4 誤りの型分析（error typology）
Column membership の誤りを not_answered 14 / shifted 9 / invented 7 / over 4 / exact 2 に分類し、混同表で「東部を西部と誤答」8 件という偏りを検出した。精度の総和ではなく誤りの型で見ることで、対策（日本語別名の併記、列の左右位置の明示）を具体化できる。
出典: `knowledge/legacy_claude_work/reports/誤りの型分析と日本語名の付与_20260812.md`

#### 4.1.5 検証済みデータのみを prompt に注入する原則
日本語別名は `unit_aliases.mapped.json` に存在する 26 unit にのみ付加し、無い unit には何も追加しない。捏造混入を `test_membership_prompt_carries_verified_japanese_labels_only` で固定する。
出典: 同上

#### 4.1.6 429 リトライと日次枠即時判定
指数バックオフ（15 / 30 / 60 / 120 秒、最大 4 回）に加え、429 本文の `limit: N` を読んで記録済み呼出し回数と比較し、超過済みなら待たずに諦める。これにより 1 コールあたり 225 秒の無駄な待機を除去した。
出典: `knowledge/legacy_claude_work/patches/llm_extract_retry.py.md`, `モデル選定と効率化_20260810.md`

#### 4.1.7 UTF-8 バイト長ベースのトークン推定
`ceil(utf8_bytes / 3)` を全経路で統一する。`len(prompt) // 4` は日本語で実測比 3.26 倍の過小評価となる。Bedrock 実測では誤差 +6.3%（安全側）であった。
出典: `knowledge/legacy_claude_work/reports/429診断_20260810.md`, `プロバイダ検証結果_20260811.md`

#### 4.1.8 config 統制語彙の系統的点検
`age_mapping.json` の年代誤り 19 件（完新世が更新世表記になる等）、`lithology_mapping.json` の複合語由来の系統誤変換（火山礫 → gravel 等 198 件）を検出。102 箇所の ICS 正規化、`前期` / `中期` / `後期` 3 キー削除、366 トークン修正・608 件の主従再構成を行い、新規 `minor_lith_mapping.json`（22 件）・`lith_att_mapping.json`（973 件）を作成した。`config/` 原本は変更せず `claude_work/config_fixed/` へ出力し、差し替えは確認後判断とした。
出典: `knowledge/legacy_claude_work/reports/config点検レポート_20260810.md`, `config修正差分_20260810.md`

### 4.2 Codex による提案・実装（実施済）

#### 4.2.1 200k v1 の P0 改修
prop 値を Excel 数式依存から Python 直接計算（`compute_unit_prop`）へ、中心年代 `(b_age + t_age) / 2` の降順ソート、深成岩以外の接触関係を `unknown` 化、`STRAT_RANK_PATTERNS` で Member を Formation より優先評価。全 112 図幅で厳格バリデータ 0 errors、完全年代逆転 0 件（5,035 隣接ペア中、b_age のみ増加する重複ペアは 59 件）。自動回帰テスト 7 件を配備。environment 語彙警告 2,586 件は未解消。
出典: `docs/Codex_Handoff_Summary.md`, `docs/Codex_Submission_Report.md` (2026-08-14)

#### 4.2.2 200k v2_polygon_column 設計と WP0〜WP3 実装
現行 803 Columns は図幅 BBOX を凡例大分類で束ねたクラスタであり実地質ポリゴンを反映していない、という問題認識に基づく刷新。GSJ シームレス地質図 V2 の実 polygon から domain 分割し、Column footprint を Union Footprint として算出する。純 Python の空間幾何エンジンを構築し、京都及大阪（NI-53-14）で 127 ポリゴン → 10 ドメイン提案を実証。単体回帰テスト 11/11 PASS。WP4 以降は未着手。
出典: `docs/GSJ_200K_AUTOMATED_COLUMN_SYSTEM_DESIGN.md`, `docs/Codex_200k_V2_Handoff_Report.md` (2026-08-14)

#### 4.2.3 ワークブック保護機構
`--force` なしでも完走して `candidate-<日時>.xlsx` へ出力し、`--force` 時は本体を `.before-<日時>.xlsx` へ、GOLD 束縛対象 JSON を `system/backup-<日時>/` へ退避する。固定テスト `test_pilot_workbook_guard.py`。
出典: `knowledge/legacy_claude_work/reports/Codex向け引き継ぎ_20260813.md`

#### 4.2.4 LLM 会計の SQLite 一本化
`today_usage()` / `record_usage()` / `load_limits()` のシグネチャを維持したまま内部を `LLMRuntimeStore` の薄ラッパー化。`llm_usage.json` は読み取り専用の移行元として 1 回だけ取り込む。移行検証で 2026-08-06 〜 11 分の 55 回・1,686,931 トークンが一致し再実行 0 件。pytest 220 件 ＋ standalone 回帰 583 件の計 803 件が合格。
出典: `knowledge/legacy_claude_work/reports/緊急提案_会計二重化_20260811.md`, `docs/LLM_ROUTING.md`

### 4.3 未実施の提案（Backlog）

以下はいずれも記録上「提案のみ」または「承認待ち」で終わっており、実施記録が確認できなかった。着手前に前提条件が今も有効か確認すること。

| # | 提案 | 内容の要点 | 出典 |
| :-- | :-- | :-- | :-- |
| 1 | Column 検出 prompt の矛盾解消 | 「diagram panel is not a Column」を柱状図・凡例パネルに限定し、地理区分パネルを Column の証拠と明示。prompt version を `column-detection-closed-v2` へ。変更は 1 回に限定。 | `制約版GOLD_v2実行結果_20260812.md` |
| 2 | membership prompt への左右位置明示 | 西部 = 左、中央部 = 中、東部 = 右を prompt に明示。誤りが「東 → 西」8 件に集中していることに基づく。 | `Column検出の真因_ページ束縛ミス_20260812.md`, `ABC実施結果_図と解像度の修正_20260812.md` |
| 3 | Early Pliocene の許可リスト追加 | `common.py:650-654` の許可リストに Pliocene（`Early Pliocene` 5.333–3.6 Ma）が漏れている。1〜2 行の追加で修正可能。 | `課題一覧_20260811.md` |
| 4 | Vision 系 2 ステージへのリトライ追加 | `llm_column_vision.py:378`、`pdf_environment.py:367` は `call_gemini` を通らず独自 `urlopen` であり、503 / 429 で即死した実例がある。 | `課題一覧_20260811.md` |
| 5 | 推論値を b_int / t_int 列に書くか | (a) 現状維持、(b) 推論値は `REF_` 系参考列のみに出し b_int / t_int は空欄維持、(c) 補完の適用条件を狭める、の 3 択が未決。 | `unit_id重複と年代誤伝播_修正提案_20260811.md` |
| 6 | 既存キャッシュ 4 件の整理 | 採番の食い違いの出所である古い `pboot_*.json` 3 件を退避し、最新かつ網羅的な `pboot_ec362e…`（48 候補・v2・3.6-flash）のみ残す。ファイル削除は未実施。 | 同上 |
| 7 | Gemini 候補順序の見直し | 6 ルート全てで Gemini が候補リスト末尾に置かれているが、無料枠が大きい（flash-lite 1,500 回/日）にもかかわらず最後である根拠の記録が無い。`_order_reason` を残すか先頭へ移すかの判断が必要。 | `システム分析_20260811.md` |
| 8 | Google 内モデル切替（Flash-Lite 系） | 無料枠 20 回/日 → 1,500 回/日（75 倍）。ただし日本語地質記載での抽出精度は未検証であり、`Ichinohe_reference_GOLD.xlsx` との突合を切替の前提とする。 | `モデル選定と効率化_20260810.md` |
| 9 | ZFK / Shapefile 統合ルーティング計画 | 763 図幅をソース階層（ZFK+Shape+PDF / Shape+PDF / PDF のみ / Viewer のみ）と PDF 表現型の二軸でルーティングする Phase 0〜5 計画。Phase 0（manifest 修復・再計測）から未着手。 | `50k_columnization_plan_zfk_shapefile_20260813.md` |
| 10 | PDF Field Enrichment（Evidence scope 契約） | `unsplit` を scope として使うことを禁止し、Column 分割後に orphan Evidence 0 件を監査する。Status: Proposed、Phase1〜5 の実装順序のみ提示。 | `docs/PDF_FIELD_ENRICHMENT_DESIGN.md` |
| 11 | 200k を map source として提供する書き換え案 | (1) 200k を map source、(2) 50k を column、(3) 解説面のある 1999 年以降刊行分のみ低解像度 column。 | `200k補完計画_レビュー_20260811.md` |
| 12 | AI Usage Limit Tracker（デスクトップ HUD） | Claude / ChatGPT 等のデスクトップ利用上限を表示する Windows HUD ツール。MacroStrat パイプライン本体とは独立した別ツールの要件定義。 | `knowledge/architecture/Copilot_AI_Tracker_Spec.md` |
| 13 | Column 部分採用（assignment_ready） | コードは実装済みだが、キャッシュに `assignment_ready: False` が焼き付いているため出力に未反映。Vision の再実行が必要。 | `システム分析_20260811.md` |

### 4.4 エージェント運用上の教訓

- 自分が書いた仕様書は実装の進行によって陳腐化する。Codex 実装の分析時、Claude 自身の仕様書が既に現状と乖離していたと結論づけられている。仕様書には作成日と対象コミットを明記し、参照前に鮮度を確認すること。出典: `システム分析_20260811.md`
- 提案書のレビューでは「無料枠の数字」「モデル引数の受け渡し」「どのプロバイダが実際に使われたか」を必ず一次情報とコードで確認する。Groq 提案では、この 3 点すべてに欠陥があった。出典: `groq提案レビュー_20260810.md`
- 未確認の因果推定には信頼度（high / medium / low）を明記し、実測が得られ次第、撤回記録を残して訂正する。429 の原因推定はこの手順で TPM 超過説から RPD 超過へ訂正された。出典: `モデル選定と効率化_20260810.md`
- 不安定な provider で得た数値を比較材料にしてはならない。完走率が実行ごとに変わる状態での A/B 比較は意味を持たない。出典: `誤りの型分析と日本語名の付与_20260812.md`

