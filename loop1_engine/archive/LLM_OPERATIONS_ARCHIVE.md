# Loop 1: LLM運用・プロバイダ・コスト管理 統合開発記録 (LLM_OPERATIONS_ARCHIVE.md)

本ファイルは、過去の開発において個別に作成された LLM ルーティング、プロバイダ障害（429エラー）、コスト配分監査レポートを1つに統合した恒久記録である。


---

## [429診断_20260810.md]

# 429エラー 原因切り分け診断

**診断日**: 2026-08-10
**目的**: `groq_integration_proposal.md` が前提としている 429 エラーの発生源を、コードと実測値から特定する
**結論**: **Google API 側の本物の HTTP 429 である。**ただし原因は「Gemini の無料枠が小さいこと」ではなく、**日本語プロンプトが約42,000トークンと巨大で、リトライ機構が一切ないこと**。Groq への移行では解決しない（むしろ悪化する）。**リトライ/バックオフの追加だけで大半が解決する。**

---

## 1. 切り分け結果：ローカル制限か、Google の 429 か

### 判定：Google 側の HTTP 429

| 根拠 | 内容 |
|---|---|
| 例外の種類が違う | ローカル上限は `BudgetExceeded(RuntimeError)` を投げ、**日本語のメッセージ**（「本日のトークン数が上限に達しました」）を出す（`llm_extract.py:110-141`）。提案書が引用する `HTTP Error 429: Too Many Requests` は `urllib.error.HTTPError` の英語文字列であり、別物 |
| 現時点でローカル上限に未達 | 本日 26 calls / 817,131 tokens。上限は 200 calls / 1,000,000 tokens。`check_budget` の日次判定（`day["tokens"] >= max_tokens_per_day`）は**まだ発火しない** |
| 例外ハンドリングが無い | `call_gemini`（`llm_extract.py:277-299`）は `urllib.request.urlopen` を裸で呼んでおり、`HTTPError` の捕捉が一切ない。`llm_extract.py:596` の `except urllib.error.HTTPError` は `list_models` 内で、本経路とは無関係 |

したがって提案書 §1 の「HTTP Error 429 が頻発」という観測自体は正しい。**ただし処方箋が誤っている。**

### ただし警告：ローカル上限が目前

```
本日の残り: 1,000,000 - 817,131 = 182,869 tokens
1コールあたり約 41,876 tokens（後述）
→ 残り 約4コールで BudgetExceeded に到達する
```

次に図幅1件を通しで回すと、**今度は Google の 429 ではなくローカルの `BudgetExceeded` で止まる**可能性が高い。症状が変わるだけで、根本原因（プロンプトが大きすぎる）は同じ。

---

## 2. 根本原因：プロンプトが約42,000トークン

`pdf_field_extract` に渡される本文コンテキストの実測（m1286_一戸 2018）：

| ファイル | chars | UTF-8 bytes |
|---|---|---|
| `system/pdf_enrichment/routed_contexts.json` | 51,419 | 125,628 |
| `routed_contexts.mapped.json` | — | 160,745 |

推定トークン数：**約 41,876**（`utf8_bytes / 3`）。
実測平均も一致する：本日 817,131 tokens ÷ 26 calls = **31,428 tokens/call**。

### Gemini 3 Flash 無料枠との突き合わせ

| 制限 | 値 | 42k トークンのプロンプトでの意味 |
|---|---|---|
| RPM | 10 | 律速ではない |
| **TPM** | **250,000** | **毎分 約6コールで上限**。5ステージを間隔なしで連射すると 5 × 42k = 210k で上限直前、リトライ1回で超過 |
| RPD | 1,500 | 律速ではない |

→ **観測されている 429 は TPM（毎分25万トークン）超過**とみて矛盾がない。パイプラインは `sleep` もリトライも持たないため、1回の瞬間的な TPM 超過で全体が停止する。

### Groq に移した場合（提案の効果の検証）

| | Gemini 3 Flash | Groq llama-3.3-70b-versatile |
|---|---|---|
| TPM | 250,000 | **12,000** |
| TPD | （実質制限なし） | **100,000** |

**Groq の TPM は必要量の約 1/3.5。**42,000 トークンのリクエストは1本たりとも通らず、待機しても永久に 429 になる。TPD 100,000 も1日2〜3コールで枯渇する。

**提案を実装すると 429 は解消せず、悪化する。**（ただし §2 の `model` 引数バグにより実際には Groq が呼ばれないため、表面上は「何も変わらない」形で現れる可能性が高い。）

---

## 3. 副次的な発見：トークン推定器が2種類あり、片方が日本語で機能していない

コードベースに2つの推定式が併存している。

| 場所 | 式 | 性質 |
|---|---|---|
| `llm_extract.py:283`（`call_gemini` 内の `check_budget`） | `len(prompt) // 4` | **文字数** ÷ 4 |
| `pilot_llm.py:384-386`, `pdf_field_extract.py:326` | `ceil(len(utf8_bytes) / 3)` | **UTF-8バイト数** ÷ 3 |

日本語は1文字 = UTF-8で3バイトなので、両者は約4倍ずれる。実測：

| 対象 | `len//4`（call_gemini） | `utf8/3`（pdf_field_extract） | 乖離 |
|---|---|---|---|
| `routed_contexts.json`（日本語本文） | 12,854 | 41,876 | **3.26倍** |
| `m1286_abstract.txt`（英文Abstract） | 5,680 | 7,602 | 1.34倍 |

英文では問題にならないが、**日本語本文では `call_gemini` 内のガードが約1/3に過小評価**している。

結果として `max_tokens_per_call: 200,000` のガードは事実上無効化されている。`len//4` で 200,000 に達するには 800,000 文字（日本語で約2.4MB）が必要で、現実には到達しない。

**修正すべき**: `call_gemini` の `est` を `pilot_llm.estimate_tokens` と同一の式に統一する。

---

## 4. 推奨する対策（優先順位順）

### 対策A：リトライ + バックオフの追加【最優先・最小コスト】

現在 `call_gemini` はリトライを一切持たない。TPM バケットは約60秒で回復するため、**429 を捕捉して `Retry-After` 秒だけ待って再試行するだけで、観測されている停止のほぼ全てが解消する。**

- 新規プロバイダ不要
- 抽出品質への影響ゼロ（同じモデル・同じプロンプト）
- 変更は `call_gemini` 1関数に閉じる

パッチ案: `claude_work/patches/llm_extract_retry.py.md`

### 対策B：トークン推定器の統一

`call_gemini` の `len(prompt)//4` を `ceil(utf8_bytes/3)` へ。日本語プロンプトに対する `max_tokens_per_call` ガードが正しく機能するようになる。

### 対策C：プロンプトの分割・縮小

42,000 トークン/コールが根本原因。`routed_contexts.json` は 21 contexts を1回のプロンプトに詰め込んでいる。

- contexts を数件ずつのバッチに分割 → 1コール 5〜10k トークンに低減
- TPM 制約から解放され、失敗時の再実行コストも下がる
- `vocab_hint()` が語彙表全体をプロンプトに載せている点も見直し余地あり（`llm_extract.py:244-256`）

### 対策D：Groq の限定導入【対策A〜Cの後、必要なら】

対策A〜Cを実施してもなお枠が足りない場合のみ検討する。条件は `groq提案レビュー_20260810.md` §「推奨する対応順序」ステップ3を参照。少なくとも **1コール12,000トークン未満のステージに限定**しなければ機能しない。

---

## 5. 検証に使った根拠

| 項目 | 出典 | 信頼度 |
|---|---|---|
| 本日の使用量 26 calls / 817,131 tokens | `config/llm_usage.json` | high |
| ローカル上限 200 calls / 1,000,000 tokens | `config/llm_limits.json` | high |
| `check_budget` の判定ロジック | `scripts/llm_extract.py:114-141` | high |
| `call_gemini` にリトライ・HTTPError処理が無い | `scripts/llm_extract.py:277-299` を通読、`grep HTTPError` で確認 | high |
| プロンプト実測 51,419 chars / 125,628 bytes | `data/02_review/05_青森/m1286_一戸 2018/system/pdf_enrichment/routed_contexts.json` を実測 | high |
| 推定器の3.26倍乖離 | 上記ファイルで両式を実計算 | high |
| Groq llama-3.3-70b: TPM 12K / TPD 100K | https://console.groq.com/docs/rate-limits | high |
| Gemini 3 Flash 無料枠: 10 RPM / 250K TPM / 1,500 RPD | Web検索（複数の二次情報源が一致）。Google公式ドキュメントでの直接確認は未実施 | **medium** |
| 429 の直接原因が TPM 超過であること | 上記から整合的に推論。**実際の429レスポンスヘッダ／エラー本文は未取得** | **medium** |

### 未確認事項（事実と解釈の区別）

- **事実**: リトライ機構が無い。プロンプトが約42,000トークン。Groq の TPM は 12,000。
- **解釈**: 429 の直接原因が TPM 超過であること。これを確定させるには、実際の 429 レスポンスの本文（Google は超過した quota 名を返す）を1回記録する必要がある。対策Aのリトライ実装時に、429 のレスポンス本文をログへ残せば次回以降確定できる。


---

## [groq提案レビュー_20260810.md]

# Groq 組み込み提案書 レビュー（Claude）

**レビュー日**: 2026-08-10
**対象**: `groq_integration_proposal.md`
**結論**: **現状のまま実装すべきでない。** 提案の中心的前提（無料枠の大きさ）が対象モデルの実際の制限と食い違っており、実装すると 429 は解消せず悪化する。加えて、提案コードには「常に Gemini へ無言でフォールバックする」バグがあり、提案の検証手順ではそれを検出できない。

---

## 判定サマリ

| # | 指摘 | 深刻度 | 種別 |
|---|---|---|---|
| 1 | 対象モデルの無料枠の数字が誤り（前提の崩壊） | **致命的** | 事実誤認 |
| 2 | `model` 引数の衝突で常に Gemini へサイレント・フォールバック | **致命的** | 実装バグ |
| 3 | `check_budget` / `record_usage` をバイパス（設計不変条件の破壊） | 高 | 設計違反 |
| 4 | 既存の `executor` 注入口を使わず 3 ファイルを書き換える | 高 | 設計 |
| 5 | 抽出品質の検証が受け入れ条件に無い | 高 | 検証計画 |
| 6 | `call_llm` が `api_key` を受け取らず署名互換性を壊す | 中 | 実装 |
| 7 | 返り値の互換性が不完全（`extract_text(raw)` が機能しない） | 中 | 実装 |
| 8 | 例外の握り潰し（`except Exception as e: pass`） | 中 | 実装 |
| 9 | JSON モード未使用 / `retry-after` 未処理 / timeout 不整合ほか | 低 | 実装 |

---

## 1. 【致命的】対象モデルの無料枠の数字が誤り

提案 §2 は `llama-3.3-70b-versatile` を「1日1.4万回無料」としているが、Groq 公式のレート制限表では 14.4K RPD は `llama-3.1-8b-instant` など小型モデルの値である。

### Groq Free Plan（公式値）

| MODEL ID | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| **llama-3.3-70b-versatile** | 30 | **1K** | **12K** | **100K** |
| llama-3.1-8b-instant | 30 | 14.4K | 6K | 500K |
| openai/gpt-oss-120b | 30 | 1K | 8K | 200K |
| qwen/qwen3.6-27b | 30 | 1K | 8K | 200K |

出典: https://console.groq.com/docs/rate-limits （2026-08-10 取得）

### 本パイプラインの実測プロンプト規模

`config/llm_usage.json`（2026-08-10 時点）:

```
"2026-08-10": { "calls": 26, "tokens": 817131 }
```

→ **平均 約31,400 tokens / call**（`record_usage` は input+output の total_tokens を記録）。
`config/llm_limits.json` の `max_tokens_per_call` が 200,000 に設定されていることからも、大きいプロンプトを前提とした設計であることが分かる。

### 帰結

| 制限 | Groq Free の値 | 本パイプラインでの意味 |
|---|---|---|
| TPD | 100,000 | **1日あたり約3コールで枯渇**（26コール必要な日には全く足りない） |
| TPM | 12,000 | **1リクエスト単体（約31k tokens）が TPM バケットを超過**。分間待機しても通らない恒久的 429 |
| RPD | 1,000 | ここは問題にならない（RPD は律速ではない） |

提案 §4-1 の「429エラーの完全解凍」「Gemini の無料枠が大幅に温存される」は成立しない。**テキスト解析ステージこそがプロンプト最大の工程**であり、そこを最も TPM/TPD の狭いモデルへ寄せる役割分担は逆効果である。

なお `x-ratelimit-limit-requests: 14400` は公式ドキュメントのヘッダ**例示**であり、特定モデルの値ではない。提案の 14,400 という数字はこの例示か、8b 系の値との混同に由来する可能性が高い。

---

## 2. 【致命的】`model` 引数の衝突 → 常に Gemini へサイレント・フォールバック

### 現状の呼び出し側

```python
# scripts/pdf_unit_bootstrap.py:290
raw, response_text = call_gemini(prompt, api_key, model=model, timeout=600, quiet=True)
# scripts/pdf_field_extract.py:344
raw, text = call_gemini(prompt, api_key, model=model, timeout=600, quiet=True)
# scripts/pdf_alias_mapping.py:202
raw, text = call_gemini(prompt, str(api_key), model=model, timeout=600, quiet=True)
```

`model` は既定で `MODEL = "gemini-3.6-flash"`（`scripts/llm_extract.py:36`）。

### 提案コードの挙動

```python
if task_type == "text" and groq_key:
    try:
        return call_groq(prompt, groq_key, model=model or GROQ_DEFAULT_MODEL, ...)
```

`model` は `"gemini-3.6-flash"` という**非 None の値**なので `or` は短絡し、Groq へ `"gemini-3.6-flash"` が送信される。

→ Groq が `404 model_not_found` を返す → `except Exception: pass` → Gemini へフォールバック。

**結果：Groq は一度も使われない。** すべてのリクエストが従来どおり Gemini を通り、429 は何も改善しない。

### 検証計画がこのバグを検出できない

提案 §5 の受け入れ条件は「429 なしで全5ステージが完走」「48 units 生成」「440 PASS」。上記バグがあっても、Gemini の枠が残っていれば**全部パスする**。「Groq が実際に使われたか」を確認する項目が無いため、バグを埋め込んだまま完了扱いになる。

**必須の追加検証項目**: 実行後に「どのプロバイダが何回呼ばれたか」をログまたはキャッシュ JSON の `model` フィールドで確認すること。

### 修正方針

`model` パラメータをプロバイダ別に分離する（例: `text_model` / `vision_model`）か、`call_llm` 側で「Gemini 用モデル名が渡された場合は Groq 既定モデルへ読み替える」明示的な正規化を入れる。`or` による暗黙処理は不可。

---

## 3. 【高】`check_budget` / `record_usage` のバイパス（設計不変条件の破壊）

`scripts/llm_extract.py:277` の docstring に明記されている：

```
呼び出す前に必ず上限を確認する（check_budget）。ここを通さない経路は作らない。
```

提案の `call_groq` は `check_budget` も `record_usage` も呼ばない。これは意図的に置かれた安全装置を回避する新経路であり、コードベースの明示的な不変条件に反する。

さらに実害がある。`pdf_field_extract.py` と `pdf_alias_mapping.py` は独自の `_preflight` で `today_usage()` を参照して事前判定している：

```python
# scripts/pdf_field_extract.py:336-339
if max_calls and int(usage.get("calls") or 0) + 1 > max_calls:
    raise PDFFieldError("Daily LLM call limit would be exceeded by body extraction.")
if max_tokens and int(usage.get("tokens") or 0) + estimated_tokens > max_tokens:
    raise PDFFieldError("Daily LLM token limit would be exceeded by body extraction.")
```

Groq 分が `record_usage` されないと、この会計が実態からずれる。かつ **Groq 側の TPD 100K をローカルで防ぐ手段が無くなる**（§1 の通り、Groq の TPD は Gemini より遥かに狭いので、むしろ Groq 用のカウンタこそ必要）。

**修正方針**: プロバイダ別の usage 記録（`llm_usage.json` に provider 次元を追加）と、Groq 用の `check_budget` 相当（TPM/TPD ベース）を実装する。少なくとも `record_usage` は必ず通す。

---

## 4. 【高】既存の `executor` 注入口を使っていない

対象 3 モジュールには**すでに依存注入の seam が存在する**：

```
scripts/pdf_unit_bootstrap.py:479   executor: Executor | None = None
scripts/pdf_field_extract.py:360    executor: Executor | None = None
scripts/pdf_alias_mapping.py:177    executor: Executor | None = None
```

```python
# scripts/pdf_field_extract.py:401
response = executor(prompt) if executor else _execute(prompt, str(api_key), model)
```

Groq executor をここに渡せば、提案 §3「変更3: テキスト抽出モジュールの書き換え」は**丸ごと不要**になる。3 ファイルの本文に手を入れないので差分が小さく、既存テストへの影響も局所化でき、プロバイダ選択のロジックが 1 箇所に集約される。

**提案の実装計画より、この seam を使う方が明確に優れている。**

---

## 5. 【高】抽出品質の検証が受け入れ条件に無い

提案 §5 の受け入れ条件は 3 つとも「速度・クォータ・既存テスト」であり、**抽出された地質情報の内容が同等かを一切見ていない**。

これはプロジェクト規則との衝突である：

- 「推測で値を埋めない。不明値は空欄にする。」
- 「地層名、上下関係、年代、岩相、層厚を資料から抽出する。」

対象は日本語の 5万分の1地質図幅説明書である。`llama-3.3-70b-versatile` が日本語の地質学専門テキストで `gemini-3.6-flash` と同等の抽出精度を持つという根拠は提案書に示されていない。提案 §2 の「テキスト読解＆JSONルール遵守精度最高峰」は無根拠の主張である。

モデルが弱いと、空欄にすべき値を埋める（＝ハルシネーション）方向に振れる可能性があり、これはこのプロジェクトで最も避けたい失敗モードである。

**必須の追加検証**: 同一 PDF に対する Gemini / Groq の抽出結果を **フィールド単位で差分比較**し、以下を確認する。

1. `strat_name` の一致率
2. `b_age_ma` / `t_age_ma` / `min_thickness` / `max_thickness` の数値一致
3. **`*_quote` フィールドが原文に実在するか**（`llm_extract.verify` / `number_supported` を流用可能）
4. 空欄であるべき箇所が埋められていないか（偽陽性率）

これが通らない限り、速度改善は意味を持たない。

---

## 6. 【中】`call_llm` が `api_key` を受け取らず署名互換性を壊す

現行は呼び出し側が鍵を保持して渡す設計（`_execute(prompt, api_key, model)`、`run_body_enrichment(..., api_key=...)`）。提案の `call_llm` は引数に `api_key` を持たず内部で `load_secret` する。

影響：
- 呼び出し側が持つ `api_key` パラメータが宙に浮く（`pdf_alias_mapping.py:197` の `if executor is None and not api_key: raise` などの検査が無意味化）
- テストの monkeypatch / ダミーキー注入の経路が壊れる
- `run.py` から鍵を明示指定する経路があれば同様に破綻

**修正方針**: `call_llm(prompt, *, task_type, api_keys: Mapping[str, str] | None = None, ...)` のように、明示注入を許しつつ未指定時のみ `load_secret` にフォールバックする。

---

## 7. 【中】返り値の互換性が不完全

呼び出し側は 3 箇所とも次の二段構えになっている：

```python
parsed = parse_json_block(text or extract_text(raw))
```

`extract_text` は Gemini のレスポンス形状（`output_text` / `steps[].model_output.content[].text` / `candidates[].content.parts[].text`、最後の保険として `"text"` キーの全探索）を前提としている（`llm_extract.py:322-353`）。

Groq の OpenAI 互換レスポンスは `choices[0].message.content` であり、`"text"` というキーを持たない。したがって **`extract_text(raw)` は空文字を返す**。第一経路が空だったときの保険が完全に死ぬ。

**修正方針**: `call_groq` は raw をそのまま返さず、`extract_text` が拾える形へ正規化するか、`extract_text` に OpenAI 形式の経路（`choices[].message.content`）を追加する。後者が望ましい。

---

## 8. 【中】例外の握り潰し

```python
except Exception as e:
    # Groq エラー時は Gemini へ自動フォールバック
    pass
```

- 401（鍵不正）、429（Groq 側のレート制限）、404（モデル名誤り＝§2 のバグ）、JSON 破損がすべて等しく無言で握り潰される
- 変数 `e` が未使用
- `quiet` フラグを無視している

§2 のバグが表面化しなかった最大の理由がここになる。**最低限、フォールバック発生時は理由を stderr に出す**こと。恒久的失敗（401/404）と一時的失敗（429/タイムアウト）は区別し、恒久的失敗はフォールバックせず即座にエラーにするのが望ましい。

---

## 9. 【低】その他の実装上の指摘

| 項目 | 指摘 |
|---|---|
| JSON モード | Groq は Structured Outputs（`response_format={"type": "json_object"}`）に対応。本パイプラインは全て JSON 前提（`parse_json_block`）なので使うべき |
| `retry-after` | Groq も 429 時に `retry-after` ヘッダを返す。バックオフ処理が無い |
| `timeout=600` | 「0.02秒で即答」を謳う API に 600 秒のタイムアウトは不整合。60 秒程度で十分 |
| `temperature` | 現行 `call_gemini` は temperature 未指定。提案は 0.1。再現性を重視するなら 0 に統一し、方針を明記すべき |
| import 位置 | `import urllib.request, json` は `llm_extract.py` 冒頭で既に import 済み。関数内 import は不要 |
| `User-Agent` | `"Mozilla/5.0"` を詐称する必要は無い。素直にツール名を入れるべき |
| レート制限ヘッダ | `x-ratelimit-remaining-tokens` 等を読んで usage 記録に使えば、ローカル会計の精度が上がる |

---

## 推奨する対応順序

### ステップ 1: 429 の出所を切り分ける（実装前に必須）

`config/llm_usage.json` の 2026-08-10 は **817,131 / 1,000,000 tokens（82%）**、26 / 200 calls。
`config/llm_limits.json` の `max_tokens_per_day` は 1,000,000。

つまり観測されている失敗が、

- (a) Google API が返す本物の HTTP 429 なのか
- (b) ローカルの `check_budget` が投げている例外なのか

を先に確定させる必要がある。**(b) ならプロバイダを増やしても解決しない**（設定値の見直しが正解）。提案書はこの切り分けをしないまま (a) と断定している。

### ステップ 2: プロンプトを削る（本命の対策）

1 コールあたり約 31,000 tokens が根本原因である。これを縮小すれば、

- Gemini の日次トークン枠に余裕が生まれる
- どのプロバイダを使っても TPM 制限に当たらなくなる
- コスト・レイテンシも同時に改善する

具体策：ページ単位・セクション単位への分割、`vocab_hint()` の語彙表の縮約（プロンプトに全語彙を載せている）、`pdf_context_router` による対象箇所の事前絞り込みの強化。

**プロバイダ追加より優先度が高い。**

### ステップ 3: それでも Groq を入れる場合の条件

1. 適用対象を**短いプロンプトのステージに限定**する（alias mapping など）。31k tokens のステージには適用しない
2. `executor` 注入口を使う（§4）。3 ファイルの本文は変更しない
3. Groq 専用の budget カウンタ（TPM 12K / TPD 100K ベース）を実装し、`record_usage` を必ず通す（§3）
4. `model` パラメータをプロバイダ別に分離する（§2）
5. フォールバック時のログを必ず出す（§8）
6. **Gemini / Groq の抽出結果 A/B 差分比較を受け入れ条件に加える**（§5）
7. モデル選定を再検討する。`llama-3.1-8b-instant` は TPD 500K と広いが TPM 6K とさらに狭く、31k tokens のプロンプトは通らない。TPM 制約はプロンプト縮小なしには回避できない

---

## 出典

- Groq 公式レート制限表: https://console.groq.com/docs/rate-limits （2026-08-10 取得）
- 本リポジトリ実測値: `config/llm_usage.json`, `config/llm_limits.json`
- 該当コード: `scripts/llm_extract.py:36,277-299,322-353`, `scripts/pdf_unit_bootstrap.py:289-296,479-520`, `scripts/pdf_field_extract.py:333-348,360-401`, `scripts/pdf_alias_mapping.py:177-205`, `scripts/common.py:1860-1897`

## 信頼度

| 指摘 | 信頼度 | 根拠 |
|---|---|---|
| §1 レート制限の誤り | **high** | Groq 公式ドキュメントの表を直接確認 |
| §1 平均トークン数 | **high** | リポジトリ内の実測ログ |
| §2 model 引数バグ | **high** | 呼び出し側 3 箇所のコードを直接確認 |
| §3 budget バイパス | **high** | docstring の明文と `_preflight` 実装を確認 |
| §4 executor seam | **high** | 3 モジュールすべてに存在を確認 |
| §5 日本語精度の懸念 | **medium** | 未検証。A/B 比較を行うまでは事実ではなく懸念 |
| §7 extract_text 非互換 | **high** | `extract_text` の実装を読んで確認 |


---

## [モデル選定と効率化_20260810.md]

# 無料枠モデルの横断調査と効率化案

**作成日**: 2026-08-10
**きっかけ**: `python run.py ichinohe --force` の実行ログで、429 の実体が判明した
**結論**: **Google のまま、モデルを `gemini-3.6-flash`（20回/日）から Flash-Lite 系（1,500回/日）へ変えるのが最善。**他社の無料枠は、本パイプラインが必要とする「4万〜9万トークンの日本語入力」を通せない。

---

## 0. 先に訂正

`claude_work/reports/429診断_20260810.md` で「429 の直接原因は TPM（毎分25万トークン）超過」と推定したが、**これは誤りだった**。実際のエラー本文：

```
Quota exceeded for metric:
  generativelanguage.googleapis.com/generate_content_free_tier_requests,
  limit: 20, model: gemini-3.6-flash
```

超過していたのは**リクエスト数**の枠で、上限は **20**。225秒のバックオフが全て失敗したことから、分単位ではなく**日単位**の枠と判断できる（`llm_usage.json` の本日26回が既に20を超過）。

**`groq_integration_proposal.md` の「1分間/1日あたり20回」という記述が正しかった。**当該診断レポートで「TPM超過という解釈は未確認・信頼度 medium」と明記していた箇所が、この実測で否定された形になる。

あわせて、同レポートの**対策C（プロンプト分割）の推奨も撤回する**。律速がトークン数ではなくリクエスト数である以上、分割は**リクエスト数を増やして事態を悪化させる**。

---

## 1. 本パイプラインが必要とするもの

| 要件 | 実測値 | 出典 |
|---|---|---|
| 入力トークン数（最大） | **88,199** | 実行ログのエラーメッセージ |
| 入力トークン数（本文抽出） | 約 57,200 | `llm_cache/pfe_*.json` の `estimated_tokens` |
| 入力トークン数（環境解析） | 約 42,300 | `llm_cache/penv_*.json` |
| 入力トークン数（別名対応） | 約 5,900 | `llm_cache/pam_*.json` |
| 言語 | 日本語（5万分の1地質図幅説明書） | — |
| マルチモーダル | **必要**（5ステージ中2つが画像） | `cv_*`（柱状図）, `penv_*`（凡例図） |
| 1図幅あたりの呼び出し | **5回** | ステージ数（cv / pam / pboot / penv / pfe） |

**最大9万トークンの日本語入力を通せること**が、事実上の足切り条件になる。

---

## 2. 無料枠プロバイダの横断比較

| プロバイダ | 無料枠 | 無料枠でのコンテキスト上限 | マルチモーダル | 9万トークンを通せるか |
|---|---|---|---|---|
| **Google AI Studio** | モデルにより **20〜1,500 回/日** | **最大 1M** | ✅ | **✅ 通せる** |
| Groq (llama-3.3-70b) | 1,000 回/日 / **TPM 12K・TPD 100K** | 128K | 限定的 | ❌ TPM の **7.4倍**。1本も通らない |
| Cerebras | 約 1M トークン/日 | **無料枠は約 8,192 に制限** | ❌ | ❌ コンテキスト上限で不可 |
| Mistral | 約 1B トークン/月 | 32K〜256K | Pixtral | △ 量は潤沢。ただし**学習利用への同意が必須**、日本語地質記載の精度は未検証 |
| OpenRouter | **50 回/日**（$10課金で1,000） | 最大 1M | 経由先次第 | △ 20回/日よりは多い。Gemini へも1キーで回せる |
| GitHub Models | 150〜1,000 回/日 | 8K〜128K | 一部 | △ 検証の価値あり |
| Cohere | 約 100 回/日 | 128K | ❌ | ❌ **非商用限定** |
| Cloudflare Workers AI | 高頻度 | **2K〜8K** | ❌ | ❌ |
| NVIDIA NIM | 約 1,000 回/日 | 128K | 一部 | △ |

### 読み取れること

**「9万トークンの日本語入力＋画像」を無料で通せるのは、事実上 Google だけ。** 他社は次のどれかで落ちる。

- コンテキスト上限（Cerebras 8K、Cloudflare 8K）
- 毎分トークン数（Groq 12K）
- マルチモーダル非対応（Cerebras、Cohere）
- ライセンス（Cohere は非商用限定）
- データ学習利用への同意が前提（Mistral Experiment tier）

**Groq 提案が成立しない理由が、これで確定した。**枠の大きさ以前に、9万トークンのリクエストが物理的に通らない。

---

## 3. Google 内でのモデル選択

決定的な差は**同じ Google の中にある**。

| モデル | 無料枠 RPD | 備考 |
|---|---|---|
| `gemini-3.6-flash`（現行） | **20** | 2026-07-21 発表の最新上位モデル。最新モデルほど無料枠が絞られる |
| Gemini 3.5 Flash-Lite | **1,500** | 軽量版 |

**75倍の差**がある。しかも `scripts/llm_extract.py` の `FREE_TIER_MODELS = ("flash", "flash-lite", "gemma")` は既に flash-lite を許可しているので、**モデル名の文字列を変えるだけで動く**。新規のAPI統合もキー追加も不要。

### 「常に最良のモデル」との折り合い

現行 20回/日は **1図幅5回 → 1日4図幅**。Flash-Lite なら **1日300図幅**。
ただし Flash-Lite は 3.6 Flash より軽量で、日本語の地質記載からの抽出精度は落ちる可能性がある。プロジェクト規則「推測で値を埋めない」に照らすと、**精度の実測なしに切り替えるべきではない**。

**幸い、比較の基準になるファイルが既にある**: `claude_work/reports/Ichinohe_reference_GOLD.xlsx`

これを正解として、同一図幅（m1286）で 3.6-flash と Flash-Lite の抽出結果を突き合わせれば、精度差を数字で出せる。比較すべき項目は `groq提案レビュー_20260810.md` §5 に挙げたものと同じ。

1. `strat_name` の一致率
2. `b_age_ma` / `t_age_ma` / `min_thickness` / `max_thickness` の数値一致
3. `*_quote` が原文に実在するか（`llm_extract.verify` / `number_supported` を流用）
4. 空欄であるべき箇所が埋められていないか（偽陽性率）

### 段階的な使い分けという選択肢

全ステージを一律にする必要はない。たとえば：

- 精度が最重要で1回しか呼ばないステージ（`pdf_unit_bootstrap`）→ 3.6-flash
- 反復・再実行が多いステージ（`pdf_alias_mapping`、`pdf_field_extract`）→ Flash-Lite

ただし後述のとおり**モデル名はキャッシュキーに含まれる**ため、使い分けを増やすとキャッシュの再利用性が下がる。まずは一律で精度比較し、必要なら分けるのが順序として良い。

---

## 4. 効率化：呼び出しを減らす

### 実測：キャッシュに4倍の重複がある

`data/02_review/05_青森/m1286_一戸 2018/llm_cache/` の中身（全20件、すべて `status=complete`）：

| 接頭辞 | ステージ | 件数 | 重複の原因 |
|---|---|---|---|
| `cv_` | 柱状図 Vision | 4 | モデル違い（3.5/3.6）＋ `prompt_version` v2→age-v3 |
| `pam_` | 別名対応 | 5 | モデル違い＋ソース更新（est が 5215/5771/5918/6410 とばらつく） |
| `pboot_` | unit 抽出 | 4 | モデル違い＋ v1→v2 |
| `penv_` | 環境 Vision | 3 | モデル違い＋ v1→v2 |
| `pfe_` | 本文フィールド | 4 | モデル違い＋ v1→v2-lithology-roles |

**5ステージ分で足りるはずが20件ある。**差分15件が、そのまま無駄になった API 呼び出しに対応する。1日20回の枠に対して、これは極めて重い。

### 無駄の発生源と対策

| 発生源 | 影響 | 対策 |
|---|---|---|
| **モデル名がキャッシュキーに含まれる** | モデルを変えると**全ステージのキャッシュが失効**し、図幅あたり5回を再消費 | モデル変更は一度だけ、枠がリセットされた直後にまとめて実施する |
| **`prompt_version` の改訂** | 改訂のたびに全図幅のキャッシュが失効 | プロンプト改訂は溜めてから一度に行う。改訂前に影響図幅数×5回の枠があるか確認する |
| **ソーステキストの再生成** | `source_sha256` が変わると失効 | 上流（`routed_contexts.json` 等）を無用に作り直さない |
| **枠切れ後のリトライ** | 1コールあたり **4回の無駄なリクエスト＋225秒** | **対応済み**（後述） |
| **Vision ステージがリトライ非対応** | `llm_column_vision.py` は `call_gemini` を通らず独自に `urlopen` している（378行目）。今回の実行でも 429 を受けて即座に落ちた | 未対応。`call_gemini` と同じ扱いにするかは要検討 |

### 枠のリセット時刻（運用上重要）

Google 公式によれば **RPD は太平洋時間の深夜にリセット**される。日本時間だと**夕方ごろ**（16〜17時前後）にあたる。日本時間の深夜0時ではない。

大きな作業（モデル変更やプロンプト改訂に伴う一斉再実行）は、このリセット直後に始めるのが最も枠を使い切れる。

---

## 5. 実施済みの修正

### `scripts/llm_extract.py` — 日次枠切れの検知（本日追加）

429 の本文から `limit: N` を読み、本日の記録済み呼び出し回数と突き合わせる。既に超えていれば**待たずに即座に諦める**。

```
本日の無料枠（20 リクエスト/日・モデル gemini-3.6-flash）を使い切りました。
記録上の本日の呼び出しは 26 回です。
  待っても回復しません。枠が戻るのは太平洋時間の深夜（日本時間の夕方ごろ）です。
  枠の大きいモデルに変えるか、明日回してください。
```

**効果**: 1コールあたり **225秒の待機と4回の無駄なリクエスト**がなくなる。枠がまだ残っている場合の 429（瞬間的な超過）には、従来どおりバックオフして再試行する。

### テスト

- `claude_work/tests/test_llm_retry.py`: **53 PASS / 0 FAIL**（実際に観測された429本文をそのまま使用）
- 既存32ファイル: **失敗0件**、`test_roundtrip` 440 PASS / 0 FAIL
- `config/llm_usage.json` は全実行を通して変更なし

---

## 6. 推奨する順序

1. **AI Studio で実際の枠を確認する** — https://aistudio.google.com/rate-limit
   公式ドキュメントはモデル別の表を公開しなくなり、「自分の枠を見よ」という案内になっている。`gemini-3.6-flash` と Flash-Lite 系の実数を確認したい。
2. **枠がリセットされた直後（日本時間の夕方以降）に、m1286 で Flash-Lite の精度を検証する**
   `Ichinohe_reference_GOLD.xlsx` を正解として4項目を突き合わせる。5回の呼び出しで済む。
3. **精度が許容範囲なら Flash-Lite へ切り替える** — `scripts/llm_extract.py:36` の `MODEL` を変更。枠が75倍になる。
4. 許容範囲でなければ、**ステージ別の使い分け**か、**1日4図幅ペースでの運用**を選ぶ。

---

## 出典と信頼度

| 項目 | 出典 | 信頼度 |
|---|---|---|
| 超過メトリックが `generate_content_free_tier_requests`、limit 20 | 実行ログのエラー本文 | **high**（一次情報） |
| 日単位の枠であること | 225秒のバックオフが全滅した事実＋本日26回の記録から推論 | **medium**（Google は分/日の別を明示していない） |
| 入力 88,199 トークン | 実行ログ | **high** |
| キャッシュ20件の内訳 | `llm_cache/*.json` を実測 | **high** |
| RPD は太平洋時間深夜にリセット | https://ai.google.dev/gemini-api/docs/rate-limits | **high** |
| Gemini 3.5 Flash-Lite = 1,500 RPD | Web検索（二次情報）。**AI Studio での確認が必要** | **medium** |
| Groq llama-3.3-70b = TPM 12K / TPD 100K | https://console.groq.com/docs/rate-limits | **high** |
| Cerebras 無料枠のコンテキストが約8,192 | Web検索（二次情報、報告に食い違いあり） | **low〜medium** |
| 他社の無料枠の数値 | OpenRouter のまとめ記事（2026-06-15、cheahjs/free-llm-api-resources 準拠） | **medium** |
| Flash-Lite の日本語地質記載での精度 | **未検証**。切り替え前に必ず実測すること | — |


---

## [config点検レポート_20260810.md]

# config/ 4ファイル 点検レポート

- 実施日: 2026-08-10
- 対象: `config/vocab.json` / `config/intervals.json` / `config/age_mapping.json` / `config/lithology_mapping.json`
- 照合先: Macrostrat 公式API v2（`defs/lithologies`, `defs/environments`, `defs/lithology_attributes`, `defs/intervals?timescale=international epochs`）を本日再取得
- 方針: 既存ファイルは一切変更していない。指摘のみ。

---

## 0. 総括

| ファイル | 判定 | 重大(A) | 中(B) | 軽(C) |
|---|---|---|---|---|
| vocab.json | 概ね良好・1語欠落 | 1 | 2 | 0 |
| intervals.json | 数値は公式と完全一致・**説明が不正確** | 1 | 1 | 0 |
| age_mapping.json | 構造は健全・**個別に明確な誤りあり** | 4 | 4 | 4 |
| lithology_mapping.json | 語彙違反ゼロ・**変換ロジックに系統誤差** | 4 | 2 | 3 |

自動検査で通った項目（＝問題なし）:

- `age_mapping` 233件すべての `b_int` / `t_int` が `intervals.json` に実在。**存在しない年代名 0件**。
- `b_int` が `t_int` より新しい**逆転 0件**。
- `lithology_mapping` 2042件・延べ4,000超のトークンすべてが公式 `lithology` 語彙内。**不正語 0件、空値 0件**。
- `intervals.json` の international epochs 34件を公式APIと数値照合 → **不一致 0件**（Ordovician基底 486.85 Ma など最新ICS値を反映）。
- `vocab.json` の `environment`(83) / `lith_att`(180) は公式件数と一致。`*_detail` のキー集合も本体リストと完全整合。

---

## 1. vocab.json

### 【A-1】`tufa` が欠落（公式214語 → 収録213語）

公式 `defs/lithologies?all=1` は **214件**（lith_id 191 = `tufa`）。`vocab.json.lithology` は213件で、`trondhjemite` と `tuff` の間にあるべき `tufa` のみが抜けている。

→ 石灰華（トゥファ）を扱う際に照合失敗する。`python run.py vocab` の再実行、または1語追加で解消。

### 【B-1】`environment` の名称重複を畳んだ際に type/class の一方を喪失

公式は87レコードだが、以下4語が2つの `environ_id` に重複定義されている:

| 名称 | 公式定義A | 公式定義B | vocab.json が保持している値 |
|---|---|---|---|
| `basinal` | id20 carbonate/marine | id35 siliciclastic/marine | siliciclastic |
| `slope` | id19 carbonate/marine | id91 siliciclastic/marine | siliciclastic |
| `delta plain` | id30 siliciclastic/marine | id59 fluvial/non-marine | fluvial |
| `deltaic indet.` | id29 siliciclastic/marine | id58 fluvial/non-marine | fluvial |

**名称そのものの検証には影響しない**（83語で正しい）。ただし `environment_detail` の `class`（marine / non-marine）で判定する処理があると、`delta plain` / `deltaic indet.` が常に non-marine 扱いになる。

### 【B-2】`lith_att` の `"massive "`（末尾空白）が `"massive"` に正規化されている

公式 lith_att_id 9 の name は末尾に半角空白を含む `"massive "`。`vocab.json` は trim 済み。Macrostrat 側が取り込み時に trim する可能性が高く実害は小さいが、厳密一致で照合する場合は要注意。

---

## 2. intervals.json

### 【A-2】プロジェクト定義文の「ICS の全 Interval 定義」は事実と異なる

実体は **Macrostrat の全Interval 1,715件**であり、ICS 国際年代層序表に限定されていない。内訳（`int_type`）:

```
zone 665 / age 389 / subchron 209 / sub-age 169 / chron 101
epoch 65 / period 41 / subzone 29 / era 18 / supereon 9 / eon 8 ...
```

含まれている非ICS区間の例:

- **火星の年代**: `Amazonian`, `Late Amazonian`（`int_type: epoch`, b_age 400 Ma）
- 生層序帯: `NN21`, `Buccinosphaera invaginata`, `Neodenticula seminae` ほか665件
- 古地磁気: `Brunhes` など
- 地域階: `Haweran`（NZ）, `Russian Stages/Epochs`, `COSUNA`, `Scotese Reconstruction`
- 合成区間: `Ordovician-Silurian`, `Jurassic-Cretaceous`, `Cretaceous-Paleogene`, `Late Paleozoic`, `Tertiary`

**ファイル自体は公式に忠実で問題ない**が、「ICS準拠」と思って使うと火星の `Amazonian` などに誤ヒットしうる。ドキュメント側の記述修正を推奨。

### 【B-3】`Ionian` の年代値がICSと不一致（Macrostrat側の既知の癖）

`Ionian` = 0.774–0.0117 Ma。ICS では Ionian（中期更新世）は 0.774–0.129 Ma。**このIntervalは使用しないこと**を運用ルールに追加すべき。現状 `age_mapping` では未使用（問題は顕在化していない）。

---

## 3. age_mapping.json（233件）

### 【A-3】`後期完新世` → `Late Pleistocene`（完新世が更新世になっている）

```json
"後期完新世": {"b_int": "Late Pleistocene", "t_int": "Late Pleistocene"}
```

0.129–0.0117 Ma を返す。後期完新世は **0.0042–0 Ma**。

修正案: `{"b_int": "Meghalayan", "t_int": "Meghalayan"}`（`Meghalayan` は intervals.json に存在、0.0042–0）。原典が `後期更新世` の誤記である可能性もあるため、出典側の確認を推奨。

### 【A-4】「先〜」4件の `t_int` が論理的に逆

```json
"先白亜紀":   {"b_int": null, "t_int": "Cretaceous"}     → 上限 66 Ma
"先第三紀":   {"b_int": null, "t_int": "Paleogene"}      → 上限 23.04 Ma
"先第三系":   {"b_int": null, "t_int": "Paleogene"}      → 上限 23.04 Ma
"先新第三紀": {"b_int": null, "t_int": "Neogene"}        → 上限 2.58 Ma
```

`b_int: null`（推測しない）は規則どおりで良い。しかし `t_int` は「地層の上限（最も新しい側）」であり、`t_int = Cretaceous` は **t_age = 66 Ma、つまり白亜紀末まで続く**を意味する。「先白亜紀（＝白亜紀より古い）」と正反対。

修正案（いずれか）:

| キー | 現状 t_int | 推奨 t_int | 得られる上限 |
|---|---|---|---|
| 先白亜紀 | Cretaceous | `Jurassic` | 143.1 Ma |
| 先第三紀 / 先第三系 | Paleogene | `Cretaceous` | 66 Ma |
| 先新第三紀 | Neogene | `Paleogene` | 23.04 Ma |

あるいは `t_int` も `null` にして空欄で出力する（規則「不明値は空欄」に最も忠実）。

### 【A-5】同義キーが別の値を持つ（`白亜紀-古第三紀`）

```json
"白亜紀-古第三紀？": {"b_int": "Cretaceous-Paleogene", "t_int": "Cretaceous-Paleogene"}  → 100.5–56 Ma
"白亜紀－古第三紀":   {"b_int": "Cretaceous",           "t_int": "Paleogene"}            → 143.1–23.04 Ma
```

ハイフンの全半角違いだけで**87 Myrも範囲が変わる**。Macrostrat の合成区間 `Cretaceous-Paleogene` は 100.5–56 Ma（後期白亜紀〜暁新世）であり、「白亜紀〜古第三紀」の意味ではない。後者に統一すべき。

なお、正規化して重複検出した結果、値が食い違うのはこの1組のみだった。

### 【A-6】文脈なしのキー `前期` / `中期` / `後期` が中新世に固定されている

```json
"前期": Early Miocene / "中期": Middle Miocene / "後期": Late Miocene
```

図幅の説明文中の「前期」は直前の紀・世に依存する。中新世以外の文脈でヒットすると**無言で誤年代が入る**。もっとも危険な項目。削除して呼び出し側で文脈解決するか、キーを `中新世_前期` のように限定することを推奨。

### 【B-4】`前期更新世` 系が Calabrian を取りこぼす（5件）

Macrostrat の `Early Pleistocene` は **2.58–1.8 Ma（実質 Gelasian のみ）** で、`Middle Pleistocene`（0.774–0.129）との間の **Calabrian（1.8–0.774 Ma）が空白**になっている。

日本語の「前期更新世」は通常 2.58–0.774 Ma。該当5件:
`前期更新世` / `更新世前期` / `鮮新世-前期更新世` / `後期鮮新世-前期更新世` / `後期鮮新-前期更新世`

修正案: `t_int` を `Calabrian` に変更（`intervals.json` に存在、1.8–0.774、`international ages` 所属）。

### 【B-5】`後期ペルム紀` 系が約15 Myr広すぎる（3件）

`Late Permian` は timescales = null の非ICS区間で **274.4–251.902 Ma**（Guadalupian + Lopingian）。ICS の後期ペルム紀は **Lopingian 259.51–251.902 Ma**。

一方で `中期ペルム紀` は ICS の `Guadalupian` を使っており、**粒度が不統一**。

該当: `後期ペルム紀` / `上部二畳紀` / `後期二畳紀` → `Lopingian` への変更を推奨。

### 【B-6】`第三紀後期－第四紀初期` が過大

```json
{"b_int": "Neogene", "t_int": "Quaternary"}  → 23.04–0 Ma
```

意図はおよそ 11.6–1.8 Ma。現状は新第三紀全体〜現在を含む。`{"b_int": "Late Miocene", "t_int": "Early Pleistocene"}` などへの見直しを推奨（ただし原典の記述確認が必要）。

### 【B-7】`中期始新世` の粒度が不統一

```json
"中期始新世-前期漸新世": Middle Eocene(48.07) – Early Oligocene
"中期始新世－漸新世":     Eocene(56)          – Oligocene   ← b_int が始新世全体
```

後者の `b_int` を `Middle Eocene` に。

### 【C】軽微

- **C-1 精度の切り捨て**: `前期中新世後半` → Early Miocene 全体（23.04–15.98）、`前期中新世末－中期中新世初期` → 23.04–11.63、`中期更新世-後期更新世前半` → 0.774–0.0117。いずれも「後半」「初期」「前半」が無視され範囲が広がる。Macrostrat の粒度上やむを得ない面もあるが、備考欄に原文を残すべき。
- **C-2 解釈の混入**（「推測で埋めない」規則との関係）:
  - `先第三系貫入岩類` → `Paleozoic`–`Mesozoic`：原典に根拠がなければ推測。
  - `ヘトナイ世` → `Campanian`–`Maastrichtian`：北海道の地方階の対比であり解釈。文献根拠の明記を推奨。
  - `後期中生代` → `Late Cretaceous`：後期中生代はジュラ紀を含みうる。
- **C-3 表記ゆれ**: `Chibanian` と `Middle Pleistocene` が混在（年代値は同一 0.774–0.129）。どちらかに統一を推奨。ICS準拠なら `Chibanian`。
- **C-4 未収録の旧称**: `洪積世` `洪積統` はあるが `沖積世` `沖積統` がない（旧5万分の1図幅で頻出）。

---

## 4. lithology_mapping.json（2,042件）

**前提として、語彙違反はゼロ**（全トークンが公式 `lithology` 213語内）。以下は「語としては正しいが、対応が事実と合わない」問題。

### 【A-7】`石英安山岩` → `andesite; dacite`

石英安山岩は **dacite の旧称**で、andesite ではない。「安山岩」の部分文字列が拾われた誤りと推定。同様のパターンが `"鮮新世"火山岩類に関連した石英安山岩岩脈` 等にも波及。

### 【A-8】`花崗閃緑岩` → `diorite; granodiorite`

花崗閃緑岩は granodiorite のみ。「閃緑岩」の部分文字列由来と推定。

### 【A-9】`火山礫`（lapilli）→ `gravel`（71件）

`火山礫凝灰岩` = lapilli tuff は火砕岩であり、未固結の礫（gravel）ではない。

例: `デイサイト凝灰岩・火山礫凝灰岩，凝灰質砂岩及び泥岩` → `dacite; gravel; mudstone; sandstone; tuff`

→ `gravel` を削除し `tuff`（必要なら lith_att `lithic`）で表現すべき。71件すべてに同じ誤りが入っている。

### 【A-10】固結岩の修飾語が未固結堆積物になっている（127件）

| キー | 現状 | 問題 |
|---|---|---|
| 砂質泥岩 | `mudstone; sand` | 「砂質」は lith_att `sandy`。`sand`（未固結砂）は誤り |
| 泥質砂岩 | `mud; sandstone` | 「泥質」は lith_att `muddy` / `argillaceous` |
| 珪質粘土岩 | `clay; claystone` | `clay` は不要 |
| 含礫泥岩 | `gravel; mudstone` | lith_att `pebbly` が適切 |
| 礫岩（中礫‐巨礫） | `conglomerate; gravel` | 固結した礫岩に `gravel` は不要 |

**Macrostrat には `sandy` `muddy` `silty` `clayey` `gravelly` `pebbly` `conglomeratic` `tuffaceous` `argillaceous` がすべて lith_att として存在**する（`vocab.json` にも収録済み）。修飾語は `lith_att` 列へ移すのが正しい設計。

### 【B-8】複数岩相が100%アルファベット順で、主岩相が判別不能（1,399件）

複数岩相エントリ1,399件すべてが `a;b;c` のアルファベット昇順。Macrostrat の units シートは `lithology`（主）と `minor_lith`（従）を分けるため、現状では**どれが主岩相か復元できない**。

例: `泥質砂岩` → `mud; sandstone`（主は sandstone だが mud が先頭）

→ 「〜勝ち」「主に〜」「〜を伴う」「少量の〜」といった原文の語を使い、主/従を分離する処理の追加を推奨。

### 【B-9】「変成」接頭辞の扱いが不統一（27件）

| キー | 現状 | 備考 |
|---|---|---|
| 変成玄武岩溶岩・変成ドレライト… | `basalt; mafic; metabasalt; metagabbro; schist` | metabasalt を使用 |
| 変成玄武岩凝灰岩及び溶岩 | `basalt; tuff` | metabasalt を使わず |
| 変成泥岩及び変成砂岩 | `mudstone; sandstone` | 変成が消えている |
| 変成かんらん岩 | `metagabbro; peridotite` | serpentinite / metaigneous の検討余地 |
| 変成チャート | `chert` | 変成が消えている |

公式語彙には `metabasalt` `metagabbro` `metasedimentary` `metaigneous` `metasiltstone` `metagraywacke` `metaconglomerate` `metapelite` `metavolcanic` `metarhyolite` `metabasite` がある。方針を1つに決めて統一すべき。

### 【C】軽微

- **C-5 冗長**: `溶結凝灰岩` → `tuff; welded tuff`、`火山灰` → `ash; volcanic`。上位語が併記されている（誤りではないが不要）。
- **C-6 「凝灰質」の扱いが不統一**: `凝灰質シルト` → `silt`（tuff が落ちる）／`凝灰質砂岩`を含む他エントリでは `tuff` が付く。lith_att `tuffaceous` に統一を推奨。
- **C-7 同義キーで値が違う（2組）**:
  - `砂・礫および粘土` → `clay; gravel; sand` ／ `砂，礫および粘土` → `clay; gravel; mud; sand`（`mud` の有無）
  - `礫・砂及び火山灰` → `ash; gravel; sand; volcanic` ／ `礫，砂及び火山灰` → `ash; gravel; sand`（`volcanic` の有無）
- **C-8**: `泥流`（lahar）→ `mud`。堆積過程であり岩相ではない。

---

## 5. 推奨対応の優先順位

1. **A-3 / A-4 / A-5**（`age_mapping` の明確な誤り、計6件）— 年代が直接間違う。最優先。
2. **A-6**（裸の `前期`/`中期`/`後期`）— 無言で誤りを生むため、削除または限定。
3. **A-9 / A-10**（`lithology_mapping` の系統誤差、計198件）— スクリプトによる一括修正が可能。
4. **A-1**（`tufa` 追加）— `python run.py vocab` の再実行で解消。
5. **A-2**（ドキュメント記述の修正）— intervals.json は「Macrostrat 全Interval」であり ICS ではない旨を明記。
6. B/C 群 — 出力前の目視レビュー時に個別判断。

---

## 6. 本レポートで検証**していない**こと

- `intervals.json` 1,715件**全件**の網羅性・数値。照合したのは `international epochs` 34件（完全一致）と `age_mapping` が参照する33区間の素性のみ。
- `lithology_mapping` 2,042件の**日本語→英語対応の全数**の妥当性。代表語40語の突合と、系統パターン（部分文字列由来の誤り）の抽出にとどまる。
- `config/` 内の他ファイル（`map_index.json`, `zfk_index.json`, `pilots/` など）。
- 出典・ページ・図表番号の記録状況（4ファイルとも `vocab.json` の `_出典` 以外に出典欄を持たない）。

---

## 付録: 照合に使ったAPIエンドポイント

- `https://macrostrat.org/api/v2/defs/lithologies?all=1` → 214件
- `https://macrostrat.org/api/v2/defs/environments?all=1` → 87レコード / ユニーク名83
- `https://macrostrat.org/api/v2/defs/lithology_attributes?all=1` → 180件
- `https://macrostrat.org/api/v2/defs/intervals?timescale=international epochs` → 34件

いずれも 2026-08-10 取得、License: CC-BY 4.0。


---

## [config修正差分_20260810.md]

# config/ 修正差分レポート

- 実施日: 2026-08-10
- 実行スクリプト: `claude_work/scripts/fix_config_20260810.py`
- 原本退避: `claude_work/backup/config_20260810/`（4ファイルそのまま）
- 出力先: `claude_work/config_fixed/`
- **`config/` の原本は一切変更していない。** 差し替えるかどうかは確認後に判断してください。
- 詳細ログ（全変更の1件ずつ）: `claude_work/reports/修正ログ_20260810.json`

## 実行結果

```
age_mapping       : 230件 (削除 3件)
  明示修正         : 19件
  ICS正規化        : 102箇所 / 失敗 0箇所
lithology_mapping : 2041件
  順序を主従に再構成 : 608件
  トークン修正       : 366件
  再導出できず据置   : 223件
minor_lith_mapping: 22件   （新規）
lith_att_mapping  : 973件  （新規）
vocab             : lithology に tufa を追加（213語 → 214語）
検証エラー         : 0件
```

自動検証（スクリプト内蔵）で確認済み:

- `age_mapping` の全 `b_int` / `t_int` が `intervals.json` に実在し、**全件が international（ICS）timescale 所属**。非ICS 残存 0件。
- 年代逆転 0件。
- `lithology` / `minor_lith` / `lith_att` の全トークンが `vocab.json` の公式語彙内。語彙違反 0件。

---

## 1. intervals.json — 変更なし

ご指示のとおり Macrostrat 公式の定義（全1,715 Interval、火星年代・生層序帯・地域階を含む）をそのまま維持しました。`config_fixed/` にも原本をコピーしてあります。

誤ヒット対策は、**参照する側**で担保しました。`age_mapping` が指す interval をすべて international timescale 所属のものに限定したので、`Amazonian`（火星）や `NN21`（ナンノ化石帯）に当たることはありません。

---

## 2. age_mapping.json — 233件 → 230件

### 2-1. 年代範囲が実際に変わったもの（19件）

| キー | 旧 | 新 | 根拠 |
|---|---|---|---|
| 後期完新世 | Late Pleistocene (0.129–0.0117) | **Meghalayan (0.0042–0)** | A-3 完新世が更新世になっていた |
| 先白亜紀 | –Cretaceous (–66) | **–Jurassic (–143.1)** | A-4 上限が白亜紀末になっていた |
| 先第三紀 / 先第三系 | –Paleogene (–23.04) | **–Cretaceous (–66)** | A-4 |
| 先新第三紀 | –Neogene (–2.58) | **–Paleogene (–23.04)** | A-4 |
| 先第三系貫入岩類 | Paleozoic–Mesozoic (538.8–66) | **–Cretaceous (–66)** | A-4／推測で埋めていた b_int を空に |
| 白亜紀-古第三紀？ | Cretaceous-Paleogene (100.5–56) | **Cretaceous–Paleogene (143.1–23.04)** | A-5 全半角違いの同義キーと統一 |
| 前期更新世 / 更新世前期 | Early Pleistocene (2.58–1.8) | **Gelasian–Calabrian (2.58–0.774)** | B-4 Calabrian の欠落 |
| 鮮新世-前期更新世 | Pliocene–Early Pleistocene (5.333–1.8) | **Zanclean–Calabrian (5.333–0.774)** | B-4 |
| 後期鮮新世-前期更新世 ほか2件 | Late Pliocene–Early Pleistocene (3.6–1.8) | **Piacenzian–Calabrian (3.6–0.774)** | B-4 |
| 後期ペルム紀 / 上部二畳紀 / 後期二畳紀 | Late Permian (274.4–251.902) | **Lopingian (259.51–251.902)** | B-5 非ICS区間から ICS 統へ |
| 第三紀後期－第四紀初期 | Neogene–Quaternary (23.04–0) | **Tortonian–Gelasian (11.63–1.8)** | B-6 範囲が新第三紀全体〜現在になっていた |
| 中期始新世－漸新世 | Eocene–Oligocene (56–23.04) | **Lutetian–Oligocene (48.07–23.04)** | B-7 中期始新世が始新世全体になっていた |
| 後期カンブリア紀 | Late Cambrian (501–486.85) | **Furongian (497–486.85)** | 非ICS(Russian Epochs) → ICS。基底が 501→497 Ma に変わる |

### 2-2. 削除（3件）

`前期` / `中期` / `後期` の3キーを削除しました。それぞれ Early / Middle / Late Miocene に固定されており、中新世以外の文脈でヒットすると無言で誤年代が入るためです。

**呼び出し側の対応が必要です。** これらのキーに依存する処理があれば、直前の紀・世を文脈から解決してから引く形に変えてください。

### 2-3. ICS 正規化（102箇所・年代値は不変）

`timescales` が `null` や `Scotese Reconstruction` / `Russian Epochs` の非ICS区間を、**同じ b_age / t_age を持つ ICS 区間**に置き換えました。数値は1つも変わっていません。

| 旧（非ICS） | 新（ICS） | 件数 |
|---|---|---|
| Early / Middle / Late Miocene | Aquitanian–Burdigalian / Langhian–Serravallian / Tortonian–Messinian | 35 |
| Middle Pleistocene | Chibanian | 12 |
| Early Permian | Cisuralian | 7 |
| Tertiary | Paleogene（b側）／Neogene（t側） | 9 |
| Late Paleozoic | Carboniferous（b側）／Permian（t側） | 7 |
| Early Pleistocene | Gelasian | 4 |
| Middle / Late / Early Eocene | Lutetian–Bartonian / Priabonian / Ypresian | 7 |
| Early / Late Oligocene | Rupelian / Chattian | 4 |
| Early / Late Pliocene | Zanclean / Piacenzian | 5 |
| Early Carboniferous | Mississippian | 4 |
| Ordovician-Silurian, Jurassic-Cretaceous | 紀2つに分解 | 4 |
| Early Paleocene, Late Silurian, Late Permian | Danian, Ludlow, Lopingian | 4 |

これで `age_mapping` は **ICS 国際年代層序表の区間だけ**を参照します。

---

## 3. lithology_mapping.json — 2,042件 → 2,041件 + 新規2ファイル

### 3-1. トークン修正（366件）

| 修正内容 | 件数 |
|---|---|
| `gravel` を削除（火山礫・礫岩・含礫・中礫等の複合語だけが根拠だった） | 106 |
| `diorite` を削除（花崗閃緑岩の部分文字列「閃緑岩」由来） | 71 |
| `sand` を削除（砂質・砂岩など複合語だけが根拠） | 52 |
| `andesite` を削除（石英安山岩の部分文字列「安山岩」由来） | 47 |
| `mud` を削除（泥質・泥岩など複合語だけが根拠） | 41 |
| `tuff` を削除（溶結凝灰岩と重複する上位語） | 29 |
| `volcanic` を削除（火山灰と重複する上位語） | 22 |
| `clay` / `silt` を削除（粘土岩・シルト質など複合語だけが根拠） | 19 |
| `dacite` を追加（石英安山岩なのに欠けていた） | 9 |
| `granodiorite` を追加（花崗閃緑岩なのに欠けていた） | 5 |
| `gravel` → `breccia`（角礫は角ばった岩片であり礫ではない） | 1 |
| 再導出結果で置換（旧値が全部誤りだった） | 3 |

代表例:

```
石英安山岩          andesite; dacite            →  dacite
花崗閃緑岩          diorite; granodiorite       →  granodiorite
花崗閃緑岩質斑岩     diorite                     →  granodiorite
砂質泥岩            mudstone; sand              →  mudstone        [att] sandy
泥質砂岩            mud; sandstone              →  sandstone       [att] muddy
含礫泥岩            gravel; mudstone            →  mudstone        [att] pebbly
溶結凝灰岩          tuff; welded tuff           →  welded tuff
火山灰              ash; volcanic               →  ash
“砥石型”珪質粘土岩   clay; claystone             →  claystone       [att] siliceous
砂質片岩・泥質片岩互層 mud; sand                  →  schist          [att] sandy; muddy
デイサイト凝灰岩・火山礫凝灰岩，凝灰質砂岩及び泥岩
   dacite; gravel; mudstone; sandstone; tuff  →  dacite; mudstone; sandstone; tuff  [att] tuffaceous
```

**未固結（sand/mud/silt/clay/gravel）と固結岩が同居する件数は 127件 → 17件**になりました。残り17件は「礫岩中の礫」「砂泥互層」など、実際に両方が記載されている正当なケースです。

### 3-2. 主岩相／従岩相の分離（608件を再構成）

日本語キーを最長一致で語彙分解し、**再導出した岩相の集合が既存値と一致した場合のみ**、出現順に並べ替えました。一致しない場合は既存の順序を保持しています（＝勝手に順序を作らない）。

```
かんらん石斜方輝石玄武岩－斜方輝石単斜輝石安山岩溶岩
   旧: andesite; basalt      （アルファベット順）
   新: basalt; andesite      （記載順＝玄武岩が主）
```

「を伴う」「を挟む」「少量の」「一部」より後ろに出る岩相は `minor_lith_mapping.json`（22件）に分離しました。

```
変成かんらん岩（少量の変成斑れい岩を伴う）
   主: peridotite  /  従: metagabbro
流紋岩凝灰岩（一部非溶結），凝灰質泥岩・礫岩を伴う
   主: rhyolite; tuff  /  従: mudstone; conglomerate
```

### 3-3. 新規 `lith_att_mapping.json`（973件）

修飾語を Macrostrat の `lith_att` 語彙に切り出しました。units シートの `lith_att` 列にそのまま使えます。

上位: `hornblende`(258) `biotite`(245) `olivine`(122) `tuffaceous`(97) `fine`(78) `siliceous`(65) `medium`(64) `coarse`(62) `quartz`(54) `black`(51) `sandy`(49) `felsic`(41) `porphyritic`(39) `muddy`(38)

### 3-4. キーが1つ減った理由

`含礫部` → 旧値 `gravel`。これは岩相ではなく「礫を含む部分」という記載なので、`lithology_mapping` からは外し、`lith_att_mapping` に `pebbly` として残しました。

---

## 4. vocab.json

- `lithology` に **`tufa`（lith_id 191, 石灰華）** を追加。213語 → **214語**で公式と一致。
- `lithology_detail` にも `{"type": "carbonate", "group": "", "class": "sedimentary"}` を追加。
- `_取得日` を 2026-08-10 に更新、`_注意` に「公式 lith_att_id 9 の正式名は末尾空白付きの `'massive '` だが本ファイルは `'massive'` に正規化してある。照合時は strip すること」を追記。

---

## 5. 直していない項目（人手の判断が要るもの）

| 項目 | 現状 | コメント |
|---|---|---|
| 後期中生代 → Late Cretaceous | 未変更 | 後期中生代はジュラ紀を含みうる。原典の確認が必要 |
| ヘトナイ世 → Campanian–Maastrichtian | 未変更 | 北海道の地方階の対比。文献根拠の明記を推奨 |
| 沖積世 / 沖積統 | 未収録 | 洪積世はあるが対になる語がない。旧図幅で頻出 |
| 崩積土 → `soil` | 未変更 | Macrostrat には `colluvium` がある。そちらが適切 |
| ローム → 岩相に反映されず | 未変更 | Macrostrat には `loess` がある |
| 松脂岩 → 岩相に反映されず | 未変更 | Macrostrat には `volcanic glass` がある |
| 泥流 → `mud` | 未変更 | ラハールに対応する Macrostrat 語彙がない |
| 緑色片岩 → `schist` | 未変更 | Macrostrat には `greenschist` がある |
| 石英質砂岩 → `sandstone` | 未変更 | Macrostrat には `quartz arenite` がある |
| 「変成」接頭辞（B-9, 27件） | 一部のみ | 変成玄武岩→`metabasalt` 等、正確な meta* 語がある場合だけ辞書に入れた。変成砂岩・変成チャートは Macrostrat に対応語がなく据置 |
| 再導出できなかった223件 | 順序据置 | 値そのものは検証済み（語彙違反なし）。順序だけアルファベット順のまま。一覧は `修正ログ_20260810.json` の `lith_unmatched` |
| 精度の切り捨て（C-1） | 未変更 | 「前期中新世後半」等。Macrostrat の粒度上やむを得ない。備考欄に原文を残す運用を推奨 |

---

## 6. 次にやること

1. `claude_work/config_fixed/` の中身を確認する。特に上の 2-1 表（年代が変わった19件）と 3-1 の代表例。
2. 問題なければ `config/` に差し替える。原本は `claude_work/backup/config_20260810/` にある。
3. **`前期` / `中期` / `後期` の3キーを削除したので、呼び出し側の対応が必要。**
4. 新規の `minor_lith_mapping.json` / `lith_att_mapping.json` を units シート出力（`minor_lith` 列 / `lith_att` 列）に接続する。


---

## [LLM運用配分監査_20260811.md]

# LLM運用配分監査（2026-08-11）

## 結論

LLM呼び出しは負荷分散型の並列実行ではなく、順序付きフェイルオーバーとして運用する。
通常の運用チェーンは主系1・予備2の最大3候補とし、同じ入力を複数社へ同時送信しない。
検証済みの一部結果を別プロバイダの結果と合成することもしない。

`config/llm_routing.json` の `max_failovers` をこの方針に合わせて設定した。
候補定義自体は削除せず、4番目以降は設定変更時に利用できるstandbyとして残す。

## 運用チェーン

| ステージ | 主系 | 第1予備 | 第2予備 | standby / 保留 |
| :--- | :--- | :--- | :--- | :--- |
| unit alias | Groq | Azure GPT-5 mini | Cohere | Mistral、Gemini。NVIDIAはGOLD不合格 |
| body field | Mistral API | Bedrock Mistral Large 3 | Cohere | Gemini。NVIDIAはGOLD待ち |
| unit bootstrap | Mistral API | Bedrock Mistral Large 3 | Cohere | Gemini。NVIDIAはGOLD待ち |
| nationwide Abstract | Mistral API | Cohere | Gemini | NVIDIAは出力上限で不合格 |
| Column Vision | Mistral API | Gemini | 未確保 | Bedrock/Cohere/NVIDIAは資格不足 |
| PDF environment | Mistral API | Gemini | 未確保 | Bedrock/Cohere/NVIDIAは資格不足 |

画像2ステージだけは現在も2本構成である。候補数を増やすために未検証モデルを
自動で有効化するより、validator付きの2本構成を維持する方が安全である。

## プロバイダの役割

### Groq

短いJSONを高速に返すalias主系。日次呼出し枠は大きいが、ローカル設定の
日次トークン枠と1回上限が小さいため、長文・画像工程には配分しない。

### Azure OpenAI

alias GOLDを19/19で通過した第1予備。実モデルsnapshotを固定している。
promotional credit依存なので、恒久的な無料枠とは扱わない。

### Mistral API

長文と画像の主系。Geminiへの通常負荷を最も大きく減らす役割を持つ。
同じ会社・同じモデルだけに依存しないよう、後続は別基盤を選ぶ。

### AWS Bedrock

body fieldとbootstrapのGOLDを通過した独立基盤の第1予備。
Mistral APIとはネットワーク・認証・会計が別なので障害分離に有効だが、
promotional creditの残高境界を持つ。

### Cohere

テキスト工程の第2予備。Vision payloadの疎通実績はあるが、Column GOLDと
PDF Environment日本語GOLDはいずれも不合格なので画像ルートでは無効のまま。

追補: 2026-08-11にPDF Environment日本語GOLDを実施した。送信対象はレビュー済み
5ユニットの短い本文コンテキストとPDF 27・55ページの図2枚で、Cohere単独・
再試行なし・フェイルオーバーなしとした。実応答は10,088入力・150出力トークン。
JSON通信は成功したがproduction validatorの採用は0/5、recall 0.0、critical failure
5件でBLOCKEDとなった。追加試行はせず、画像ルートでは無効のまま維持する。

### Gemini

最大のcontext windowを持つ非常用候補。全国Abstractでは第2予備、画像では
第1予備として残す。alias、body field、bootstrapでは通常チェーン外のstandbyにし、
Gemini枠を広い文脈と画像障害時のために温存する。

### NVIDIA / OpenRouter

キーとprovider定義は保持するが、現在の運用チェーンには含めない。
NVIDIAは対象GOLDの不合格または画像能力未検証、OpenRouterはモデル単位の
route/GOLDが未定義である。削除ではなく `prepared_dormant` として監査表示する。

## 自動監査

次のコマンドは秘密情報を読まず、外部通信もしない。

```powershell
python scripts/llm_route_audit.py
python scripts/llm_route_audit.py --strict --json
```

監査は運用候補数、standby、無効理由、GOLDでBLOCKEDの候補が有効になっていないか、
promotional credit候補、routeを持たない準備済みproviderを確認する。

現時点の結果は `0 error / 2 warning / 5 info`。2 warningはいずれも画像工程が
2本構成であること、infoはcredit-backed候補3件とNVIDIA/OpenRouterの休眠状態である。

## 障害訓練

ネットワークを使わないfake adapterで、次を回帰検証する。

- 429はRetry-Afterを尊重して同じproviderを再試行する。
- 503が再試行上限まで続けば次のproviderへ進む。
- 不正JSONとvalidator rejectは次のproviderへ進む。
- 出力容量不足はキー読込・予算予約・HTTPより前にskipする。
- circuit openの候補はHTTPせず次のproviderへ進む。
- 3候補が全て失敗した場合は `AllProvidersFailed` で安全に停止する。

## 次の外部評価候補

ライブ送信を伴う次段階は、Column VisionのBedrock GOLDまたはPDF environmentの
Cohere日本語GOLDである。どちらかが合格するまでは画像工程のwarningを解消しない。
NVIDIAの再調整は引き続き後回しとする。


---

## [プロバイダ検証結果_20260811.md]

# APIプロバイダ 実測検証結果

**検証日**: 2026-08-11
**方法**: `claude_work/scripts/test_all_providers.py` / `test_bedrock.py` で同一プロンプトを送信
**目的**: 「どれが実際に使えるか」を推測ではなく実測で確定する

---

## 結論

| プロバイダ | 結果 | 応答時間 | 備考 |
| :--- | :--- | ---: | :--- |
| **Amazon Bedrock** | ✅ | — | **73モデル**（Claude/Llama/Mistral/Qwen/GLM/Nova/DeepSeek…） |
| **Azure OpenAI** | ✅ | 3.0秒 | **GPT-5系**。Bedrock に無い |
| **Mistral（直）** | ✅ | 0.8秒 | 最速 |
| **NVIDIA** | ✅ | **64.8秒** | 遅すぎ。5コール/図幅で5分 |
| Gemini | ✅ | — | 既存。20回/日 |
| Groq | ❌ | — | UA修正後に要再試験 |
| OpenRouter | ❌ | — | モデル名要確認 |
| Cohere | ❌ | — | モデル名要確認 |

**選択肢は十分すぎるほど揃った。**ここから先はプロバイダ探しではなく、抽出品質の勝負。

---

## 1. 学生サブスクリプションで Azure OpenAI は使える

事前調査では Microsoft Q&A に「Azure for Students では Azure OpenAI にアクセスできない」という
報告が複数あり、私もそう伝えた。**実測でこれは否定された。**

エラーの推移が証拠になっている。

```
1. DeploymentNotFound (404)   デプロイが1つも無い状態
       ↓ gpt-5-mini をデプロイ
2. パラメータ名の誤り (400)     モデルまで到達している
       ↓ max_completion_tokens へ修正
3. 正常応答                    {"units":["一戸層"]}  3.0秒
```

401でも403でもなく400が返った時点で、認証・権限・モデルアクセスはすべて通っていた。

**教訓**: 二次情報の「使えない」報告は、実測で確かめるまで確定しない。

---

## 2. 実装上の落とし穴（実際に踏んだもの）

### GPT-5 系は `max_tokens` を受け付けない

```
Unsupported parameter: 'max_tokens' is not supported with this model.
Use 'max_completion_tokens' instead.
```

OpenAI互換とはいえ、モデル世代でパラメータ名が変わる。
`test_all_providers.py` はエラー本文を読んで自動で読み替える実装にした。
`temperature` を受け付けないモデルにも同様に対応。

### Groq は urllib の既定 User-Agent を弾く

```
HTTP 403 error code: 1010   （Cloudflare のブロック）
```

`Python-urllib/3.x` という既定UAが原因。

**私の以前の指摘が誤りだった。** `groq_integration_proposal.md` に
`"User-Agent": "Mozilla/5.0"` という行があり、私は「詐称する必要はない」と指摘したが、
**UAを名乗る必要自体はあった。**ブラウザ詐称ではなく素性の分かるUAで対応。

```python
USER_AGENT = "MacroStrat-ColumnBuilder/1.0 (+research; python-urllib)"
```

### モデル名は推測せずAPIから取る

- Cohere `command-r-plus` … **2025-09-15に廃止**
- OpenRouter `...:free` … 有料版へ移行済み
- Azure … カタログ406件のうち呼べるのはデプロイ済みのものだけ
- Bedrock … カタログの表示名（"Claude Sonnet 5"）とAPI ID（`anthropic.claude-sonnet-5`）が別物

既定値を推測で持つと、モデル改廃のたびに壊れる。
`--list-models` / `--list` / `--list-azure` で毎回APIから取得する方式にした。

### Bedrock はモデルによってアカウント開放が要る

```
anthropic.claude-sonnet-5 is not available for this account.
```

`--list` に出ても使えるとは限らない。403でもキーの問題とは別。

---

## 3. 出力形式の安定性（重要）

このパイプラインは**逐語引用の一致**で検証しているので、形式の安定性が効く。

### Mistral Large 3 は指示より饒舌になることがある

「説明は不要」と指示しても、同じ条件で出力が揺れた。

| 実行 | 出力トークン | 内容 |
| ---: | ---: | :--- |
| 1回目 | 35 | `以下のJSON配列に…` + フェンス付きJSON |
| 2回目 | 18 | フェンス付きJSONのみ |
| 3回目 | 35 | 前置きあり |

**temperature 0 でも決定的にならない。**分散推論では珍しくない。

### GPT-5-mini は前置きなしで返した

```
{"units":["一戸層"]}
```

1回の観測なので断定はできないが、形式遵守という観点では良好。

### 実害の有無

`parse_json_block` の挙動を確認した。

| 入力 | 結果 |
| :--- | :--- |
| 前置きあり + フェンス + **配列** | ❌ 取り出せない |
| 前置きなし + フェンス + **配列** | ✅ |
| 前置きあり + フェンス + **オブジェクト** | ✅ |

**本番プロンプトはすべてオブジェクト形式**（`{"units":...}` `{"aliases":...}`）なので、
この揺れは吸収される。ただしプロンプトでオブジェクトを要求し続けることが前提。

---

## 4. トークン推定器の精度が実測で裏づけられた

日本語で3.26倍過小評価していた問題を修正した効果を、実データで確認。

| | |
| :--- | ---: |
| 送信 | 10,046 文字（日本語） |
| 実測トークン（Mistral） | 9,449 |
| `estimate_prompt_tokens` の推定 | 10,042 |
| 誤差 | **+6.3%（安全側）** |

本番プロンプト 90,858文字 → **約85,000トークン**と予測。
Mistral Large 3 の256K、Azure GPT-5 の272K、Bedrock Claude の200Kいずれにも収まる。

---

## 5. 費用の実測

Bedrock の単価表で計算（1図幅 = 入力25万・出力1万トークン）。

| モデル | 画像 | 1図幅 | 全国1300面 |
| :--- | :---: | ---: | ---: |
| `zai.glm-4.7-flash` | ✗ | $0.022 | $28 |
| `openai.gpt-oss-120b-1:0` | ✗ | $0.044 | $57 |
| **`mistral.mistral-large-3-675b-instruct`** | **✓** | **$0.14** | **$182** |
| `anthropic.claude-haiku-4-5-...` | ✓ | $0.30 | $390 |
| `anthropic.claude-sonnet-5` | ✓ | $0.60 | $780 |

**画像対応で最安は Mistral Large 3。**$200のクレジット内で全国走破できる唯一の選択肢。
最安の GLM/gpt-oss はテキスト専用なので、画像2ステージには使えない。

---

## 6. 次にやること

**プロバイダ探しはここで打ち切ってよい。**残る作業は抽出品質。

現在の基準値（`compare_units.py` / GOLD比較）:

```
対応 30 地層 / GOLD のみ 12 / 出力のみ 18
一致 48 / 不一致 18 / 捏造 0 / 取りこぼし 137
Column: unsplit 1列（GOLD は west/central/east の3列）
```

1. **`run.py ichinohe --force`** — Column 分割の修正を反映（Vision の再実行が要る）
2. `compare_units.py` で再測定
3. **ユニット同定のズレ**（GOLD 42 / 出力48 / 対応30）の原因調査 ← 未着手の最大の問題

---

## 信頼度

| 項目 | 信頼度 |
| :--- | :--- |
| 各プロバイダの成否・応答時間 | **high**（実測） |
| 学生サブスクで Azure OpenAI が使える | **high**（正常応答を得ている） |
| Groq の403がUA起因 | **medium**（UA修正後の再試験は未実施） |
| Mistral の出力揺れ | **medium**（3回の観測） |
| GPT-5-mini の形式遵守 | **low**（1回の観測のみ） |
| 費用試算 | **medium**（カタログ単価。請求で要確認） |
| 各モデルの日本語地質記載での精度 | **未検証** |


---

## [緊急提案_会計二重化_20260811.md]

# 【緊急】LLM予算会計が二重化している

**作成日**: 2026-08-11
**宛先**: Codex
**深刻度**: 高。枠の二重消費とガード不全が同時に起きうる

---

## 実装完了追補（2026-08-11）

会計二重化の解消に加え、直接Gemini transportの共通ルーター移行も完了した。

- 対象は `towada_pdf_llm`、alias、body field、unit bootstrap、column vision、
  PDF environment の全6ステージと `llm_extract` CLI。
- 呼び出し元から明示されたAPIキーは、公開ルートを複製したメモリ内の
  1プロバイダrouterへ渡す。キーを設定、キャッシュ、結果、SQLiteへ保存しない。
- 本番推論は共通の予算予約、利用量記録、リトライ、エラー分類、
  サーキットブレーカー、validator判定を必ず通る。
- `call_gemini()` / `request_json()` は旧リトライ互換テスト専用として隔離し、
  本番呼び出し元は0件。AST回帰テストで再混入を防止する。
- 本文中の「直接Gemini移行を次の整理項目として残す」という記述は、
  現在の状態には適用しない。

---

## 解決状況（2026-08-11）

**会計二重化は解消済み。SQLiteを唯一の稼働台帳とした。**

- `today_usage()` / `record_usage()` / `load_limits()` のシグネチャは維持し、
  内部を `LLMRuntimeStore` の互換ラッパーへ変更した。
- routerと直接Gemini経路は同じ `google-ai-project` quota groupを参照する。
- `pilot.py:_usage_day()` は既存呼び出しのまま両経路の合算値を見る。
- `config/llm_usage.json` は削除・更新せず、一度だけSQLiteへ取り込んだ。
  2026-08-06〜11の55回・1,686,931トークンが一致し、再実行は0件だった。
- Geminiの数値上限の正本を `llm_routing.json` に一本化し、保守的な
  `20 calls/day`, `500,000 tokens/day`, `120,000 tokens/call` とした。
- 双方向可視性、移行の冪等性、旧JSON不変を自動テストに追加した。

補足: GroqのUser-Agentは共通adapterですでに設定済み。Bedrock
`pdf_unit_bootstrap` の非対称もGOLD合格・有効化によって解消済み。
直接Gemini transportのrouter移行とリトライ実装の一本化は、会計とは分離した
次の整理項目として残す。

---

## 症状

**互いを知らない2つの会計が並行して動いている。**

| | 保存先 | 記録する経路 |
| :--- | :--- | :--- |
| 旧 | `config/llm_usage.json` | `call_gemini()` → `record_usage()` |
| 新 | SQLite（`LLMRuntimeStore`） | `LLMRouter.execute()` |

`llm_router.py` 内での `record_usage` / `today_usage` / `load_limits` の出現回数は **0**。
router は旧カウンタを一切読まず、書かない。

## 何が壊れるか

**1. 枠の二重消費**

旧経路で Gemini を20回使い切っても、SQLite 側は「0回」と認識してさらに呼ぶ。
逆も同じ。実枠は共有されているので、429 に突っ込む。

**2. ガードが効かない**

`config/llm_limits.json` の `max_calls_per_day` が **200**。
`gemini-3.6-flash` の実枠は **20**（429本文の `limit: 20` で確認済み）。
**10倍甘い**ので、旧経路の `check_budget` は事実上素通り。

**3. `pilot.py` の判断が旧カウンタ依存**

```python
# scripts/pilot.py:521
def _usage_day() -> dict[str, int]:
    _path, _all, _date, day = today_usage()   # ← 旧カウンタのみ
```

router 経由の消費が見えないまま「まだ枠がある」と判断する。

## 分岐が呼び出し元依存になっている

`call_gemini` は今も5箇所から現役で呼ばれる。

```
pdf_alias_mapping.py:333   pdf_field_extract.py:523
pdf_unit_bootstrap.py:358  pilot_llm.py:728
llm_extract.py:761,808
```

分岐条件は `api_key` の有無（`pdf_alias_mapping.py:329` 付近）。

```python
elif api_key is not None and router is None:
    # 旧経路
```

**同じステージでも、呼び出し元が api_key を渡すかどうかで会計先が変わる。**

---

## 提案

### 案A: SQLite に一本化（推奨）

`LLMRuntimeStore` は予約制・タイムゾーン別日境界・サーキットブレーカーを持ち、
機能として明確に上位。旧 JSON をこれに寄せる。

1. `record_usage()` / `today_usage()` / `load_limits()` を `LLMRuntimeStore` の
   薄いラッパーに置き換える（関数シグネチャは維持し、呼び出し側は変更しない）
2. `config/llm_usage.json` は読み取り専用の移行元として1回だけ取り込む
3. `pilot.py:_usage_day()` も同じ store を見るようにする

**利点**: 呼び出し元がどちらの経路でも会計が1つ。
**注意**: `llm_usage.json` は `.gitignore` 済み。移行時に消さないこと。

### 案B: `call_gemini` を router 配下に入れる

旧経路そのものを無くし、Gemini も router の1プロバイダとして扱う。
（`llm_routing.json` には既に `gemini` プロバイダの定義がある）

**利点**: 経路が1本になり、リトライ規則の重複も解消する。
**注意**: `call_gemini` を直接呼ぶ5箇所の書き換えが要る。テストへの影響も大きい。

**どちらでも構わないが、両立させたままにはしないこと。**

---

## あわせて直したいもの

| # | 項目 | 対処 |
| :--- | :--- | :--- |
| 1 | `llm_limits.json` の `max_calls_per_day: 200` | 実枠に合わせる。`gemini-3.5-flash-lite` なら 1,500。SQLite に寄せるなら削除でもよい |
| 2 | リトライ規則が2箇所（`llm_extract.request_json` と `llm_router`） | 一本化。片方だけ直すと挙動が食い違う |
| 3 | **Groq の User-Agent** | `llm_routing.json` で有効だが、urllib 既定UAだと Cloudflare の `403 error code 1010` で落ちる。`User-Agent` ヘッダーが要る（実測済み） |

---

## 設定の整理（緊急ではないが誤解の元）

| 項目 | 状態 | 提案 |
| :--- | :--- | :--- |
| **NVIDIA** | 6ルート中6つ無効（生存0）。`providers.enabled: true` のまま | `providers` 側も `false` にして意図を明示 |
| **OpenRouter** | `providers` に定義のみ。routes での出現 **0回** | ルートに入れるか、定義を外す |
| **Bedrock の非対称** | 同じ `mistral-large-3` が `pdf_body_field_enrichment` で有効、`pdf_unit_bootstrap` で無効。理由の記録なし | 意図的なら `disabled_reason` を、そうでなければ設定ミス |
| **Gemini の順序** | 全6ルートで最後尾。`gemini-3.5-flash-lite` は無料かつ1,500回/日 | 利用者の意向は「枠が余っているなら先に使う」。先頭へ移すか、順序の根拠を記録 |

---

## 画像2ステージが薄い（別件だが要注意）

| ルート | 生/全 | 生存者 |
| :--- | :--- | :--- |
| `column_geography_vision` | **2/5** | mistral-small / gemini-flash-lite |
| `pdf_environment_multimodal` | **2/5** | 同上 |

小型モデル2本で地質図の柱状図・凡例を読ませている。
`pdf_environment_multimodal` の Bedrock Claude Haiku は
**Anthropic のアカウント要件で弾かれた**と記録されている
（`use-case-details account requirement`）。**AWS 側の申請で解ける可能性がある。**

2026-08-11の一戸 Column Vision GOLDでも同じ制約を確認した。事前監査では
PDF 15ページの画像1枚、48 unit、3 Column、推定入力14,021 tokens、
予約出力7,680 tokens（モデル上限8,192内）だったが、Bedrockは
HTTP 404 `model_unavailable` を返した。Anthropic use-case detailsが未提出、
または反映待ちであり、provider実使用tokensは報告されていない。
申請反映後の再probeはHTTP 200で通過し、Column Vision GOLDも通信・JSON・
production validatorを通過した。しかしGOLD照合は31/42 true positive、
24 false positive、precision 0.563636、recall 0.738095で、資格基準の
precision 1.0 / recall 0.85を満たさなかった。したがってBedrock Claude
HaikuはColumn Visionで無効のままとする。AWS申請問題は解消済みであり、
残る課題はモデル出力のColumn所属精度である。GOLD本体のprovider報告使用量は
入力10,191 tokens、出力6,858 tokens、合計17,049 tokensだった。

同じ申請反映後に固定画像2枚のprobeもHTTP 200で通過し、日本語PDF
Environment GOLDを実行した。結果は1/5 true positive、1 false positive、
validator pass rate 0.4、precision 0.5、recall 0.2で資格基準未達だった。
GOLD本体のprovider報告使用量は入力16,400 tokens、出力1,602 tokens、
合計18,002 tokens。Bedrock Claude Haikuは画像2ルートとも通信可能だが、
GOLD品質不足のため無効を維持する。

---

## 検証

会計を一本化したあと、次が成り立つこと。

1. router 経由で1回呼んだあと、`call_gemini` 経路の残枠が1減っている
2. 逆も成り立つ
3. `pilot.py:_usage_day()` が両方の消費を合算して返す
4. 既存43テストが通る（`test_roundtrip.py` は 440 PASS / 0 FAIL）

実装後の2026-08-11再検証では、通常pytest 220件とstandalone回帰583件、
合計803件がすべて合格した。ルート監査は `0 error / 2 warning / 5 info`、
SQLiteは `integrity_check=ok`、未解放reservationは0件。2 warningは引き続き
画像2ルートの稼働候補がMistral/Geminiの2本だけであることを示す。

---

## 根拠

すべて本日の実測。

- 二重化: 両系統の呼び出し箇所を grep で確認。`llm_router.py` の
  `record_usage|today_usage|load_limits` 出現回数 = 0
- 実枠20: 429本文 `generate_content_free_tier_requests, limit: 20`
- Groq の UA: `HTTP 403 error code: 1010`（Cloudflare）
- ルート生存数: `config/llm_routing.json` を直接集計


---

## [AWS_Bedrock_取得手順_20260811.md]

# AWS Bedrock APIキーの取得手順

**作成日**: 2026-08-11
**目的**: $200 のクレジットで Bedrock を使い、10万トークンの日本語＋画像を1つのプロバイダで賄えるか試す
**所要**: 30分程度（アカウント作成15分＋モデル有効化＋キー発行）

---

## 0. なぜ Bedrock か

このコードベースは `urllib` で素の HTTP を叩いています。Bedrock は通常 SigV4 署名が要り相性が悪いのですが、
**Bedrock APIキー（ベアラートークン）**を使えば `Authorization: Bearer <キー>` だけで済みます。
boto3 も署名処理も不要で、既存の `llm_extract.py` とほぼ同じ書き方で組み込めます。

---

## 1. AWSアカウントを作る

https://aws.amazon.com/free/ から登録。

- サインアップ時に **Free プラン**と Paid プランを選ぶ画面が出る → **Free を選ぶ**
- 本人確認のため支払い方法の登録を求められる
- Free プランなら、クレジットを使い切った時点で**課金ではなく停止**になる
- **$100** が登録時に付与される

**有効期限は6か月**（またはクレジットを使い切るまで）。

---

## 2. 追加の $100 を獲得する

以下5つのタスクを完了すると、さらに **$100**（合計 $200）。

1. EC2 インスタンスの起動と終了
2. RDS データベースの構成
3. Lambda 関数のデプロイ
4. **Amazon Bedrock でプロンプトを試す** ← 今回やることそのもの
5. AWS Budgets で予算を設定

4番はこの手順で自然に終わります。5番も次でやります。

---

## 3. 予算アラートを設定する（先にやる）

意図しない課金を防ぐため、キーを作る前に設定します。これで前章のタスク5も完了します。

### 手順

1. **Billing and Cost Management** コンソールを開く
   https://console.aws.amazon.com/cost-management/
2. 左のナビゲーションペインで **Budgets**
3. ページ上部の **Create budget**
4. **Budget setup** で **Use a template (simplified)** を選ぶ
   （もう一方の Customize は5画面のウィザードで、今回は不要）
5. **Templates** から選ぶ
6. 通知先メールアドレスなどを入力
7. **Create budget**

### どのテンプレートを選ぶか

公式の説明はこうです。

| テンプレート | 公式の説明 |
| :--- | :--- |
| **Zero spend budget** | 支出が **AWS 無料枠を超えた後**に通知する |
| **Monthly cost budget** | 月額予算を超えた、または超える見込みになったら通知する |

**両方作るのを勧めます。**役割が違います。

- **Zero spend budget** … 「1セントでも課金が出たら知りたい」ための番人。最も安全側
- **Monthly cost budget（$5 程度）** … クレジットの消費ペースを把握するため

### 知っておくべき点

**クレジットで賄われた分をどう数えるか**は設定によります。
予算の詳細設定に Credits / Refunds / Taxes を含めるかのチェックがあり、
既定のままだと「クレジットで相殺された利用」は課金として数えられません。

- **実際の請求だけ見たい** → 既定のままでよい
- **$200 をどれだけ使ったか追いたい** → 詳細設定でクレジットを含める、
  または **Billing → Credits** ページで残高を直接確認する

**初回の予算作成時に Cost Explorer が自動で有効化されます。**
グラフが出るまで最大24時間かかりますが、予算自体はすぐ作れます。

**IAM ユーザーで作業している場合、請求情報が見えないことがあります。**
ルートユーザーで操作するか、IAM ユーザーに請求情報へのアクセスを許可してください。

### ついでに

**Billing preferences** で **Free tier usage alerts** も有効にしておくと、
無料枠の消費が閾値に達した時点で通知が来ます（予算とは別の仕組み）。

---

## 4. リージョンを決める

**モデルの提供はリージョンごとに違います。**

- **us-east-1（バージニア北部）** または **us-west-2（オレゴン）** が品揃え最良
- 東京（ap-northeast-1）は使えるモデルが少ない場合がある
- この用途はバッチ処理なので、**レイテンシは問題になりません**。品揃え優先で構いません

コンソール右上でリージョンを切り替えます。以降の操作はすべて同じリージョンで行うこと。

---

## 5. モデルアクセスを有効化する

Bedrock コンソール（https://console.aws.amazon.com/bedrock）→ 左メニュー **Model access**

- 使いたいモデルを選んで有効化
- モデルによっては利用申請フォームの記入が要る（多くは即時〜数分で通る）
- **Model catalog** で正確なモデルIDを確認できる（例: `us.anthropic.claude-sonnet-4-6`）

長い日本語＋画像を扱うので、コンテキストが大きく画像入力に対応したモデルを選びます。

---

## 6. APIキーを発行する

Bedrock コンソール → 左メニュー **API keys**

短期と長期の2種類があります。

| | 有効期間 | 用途 |
| :--- | :--- | :--- |
| Short-term | 最大12時間 | 公式は本番向けに推奨。都度生成が要る |
| **Long-term** | 指定した期限まで | **公式は「探索用」と明記**。今回はこちら |

**Long-term API keys** タブ → **Generate long-term API keys** → 有効期限を設定（例 30日）→ Generate

- IAM ユーザーが自動で作られ、`AmazonBedrockLimitedAccess` ポリシーが付く
- **表示されたキーをその場でコピー**すること
- 1 IAM ユーザーにつき長期キーは2つまで

---

## 7. キーを保存する

`config/secret.json` に追記します。

```json
{
  "gemini_api_key": "...",
  "groq_api_key": "...",
  "bedrock_api_key": "ここに発行されたキー"
}
```

**`config/secret.json` は `.gitignore` に入っている**ので、コミットされません。

環境変数でも構いません。

```powershell
setx AWS_BEARER_TOKEN_BEDROCK "発行されたキー"
```

---

## 8. 疎通を確認する

```powershell
python claude_work/scripts/test_bedrock.py
```

期待される出力:

```
キー: config/secret.json の bedrock_api_key（先頭6文字 ABSKQm…）
リージョン: us-east-1
モデル: us.anthropic.claude-sonnet-4-6
送信文字数: 78

--- 応答 ---
{"unit_name": "一戸層"}

--- 使用量 ---
入力 45 トークン / 出力 18 トークン
概算費用 約 $0.0004

疎通OK。boto3 も SigV4 署名も使わずに叩けています。
```

リージョンやモデルを変えるとき:

```powershell
python claude_work/scripts/test_bedrock.py --region us-west-2
python claude_work/scripts/test_bedrock.py --model <Model catalog で確認したID>
```

**長文が通るかの確認**（このパイプラインの本番規模に近い日本語1万字）:

```powershell
python claude_work/scripts/test_bedrock.py --big
```

---

## 9. よくある失敗

| 症状 | 原因と対処 |
| :--- | :--- |
| HTTP 403 | キーが無効・期限切れ・権限不足。有効期限と `AmazonBedrockLimitedAccess` を確認 |
| HTTP 400（model 関連） | そのリージョンでモデルが有効化されていない。Model access で有効化 |
| HTTP 404 | モデルIDかリージョンの誤り。Model catalog で正確なIDを確認 |
| 何も起きない | コンソールのリージョンと `--region` が食い違っている |

`test_bedrock.py` はこれらを判定してヒントを出します。

---

## 10. 注意点

- **長期キーは公式に「探索用」**と位置づけられています。検証が済んで常用するなら短期キー（`aws-bedrock-token-generator`）への移行を検討
- キーは IAM ユーザーに紐づく**資格情報**です。漏れた場合はコンソールから Deactivate / Reset / Delete
- **クレジットは6か月で失効**します
- Bedrock 自体に無料トークン枠はありません。$200 のクレジットから引かれます

---

## 11. 費用の見通し

これまでの実測から、1図幅あたり5コール・約45万トークン。

| | 概算 |
| :--- | ---: |
| 1図幅 | $0.05〜1.5（モデルによる） |
| 日本の5万分の1図幅 全1,300面 | $65〜2,000 |

Claude Sonnet 系（入力$3/百万・出力$15/百万相当）だと1図幅あたり $1.4 前後、
Haiku 系や Llama/Mistral ならもっと安くなります。**$200 で何ができるかはモデル選択次第**です。

まず `--big` で長文が通ることを確認し、次に `compare_units.py` で GOLD と突き合わせて
精度と費用の釣り合うモデルを決めるのが順当です。

---

## 出典

- [API keys — Amazon Bedrock（公式）](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)
- [AWS Free Tier now offers $200 in credits（AWS公式）](https://aws.amazon.com/about-aws/whats-new/2025/07/aws-free-tier-credits-month-free-plan/)
- [AWS Free Tier FAQs（公式）](https://aws.amazon.com/free/free-tier-faqs/)
- [Creating a budget — AWS Cost Management（公式）](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html)
- [Using a budget template (simplified)（公式）](https://docs.aws.amazon.com/cost-management/latest/userguide/budget-templates.html)

### 信頼度

| 項目 | 信頼度 |
| :--- | :--- |
| APIキーの発行手順・ベアラー利用 | **high**（公式ドキュメントを直接確認） |
| $200 クレジットと5タスク | **high**（AWS公式発表） |
| モデルIDとリージョンごとの提供状況 | **medium**（変動する。Model catalog で都度確認） |
| 費用の概算 | **medium**（単価は要確認。実測は `test_bedrock.py` の出力で） |


---

## [Azure_Foundry_取得手順_20260811.md]

# Azure（Microsoft Foundry）APIキーの取得手順

**作成日**: 2026-08-11
**前提**: Azure for Students に申込済み（$100 クレジット、カード不要）
**未確認事項**: 学生サブスクリプションで **Azure OpenAI（gpt-*）が使えるかは不明**。非OpenAIモデルなら通る可能性がある。この手順はそれを実地で切り分けることも兼ねる。

---

## 0. 前提の整理

以前調べた内容の再掲です。

- **クレジットの利用条件**は AI サービスを除外していない（公式のオファー条件を確認済み）
- ただし **Azure OpenAI へのアクセス**には別途の承認ゲートがあり、学生サブスクでは通らないとの報告が複数ある
- **Microsoft Foundry Models**（Llama / Mistral / DeepSeek / Phi / Grok など OpenAI 以外）は別枠なので、通る可能性がある

**どちらなのかは実際にデプロイしてみないと分かりません。**それを確かめるのが目的です。

---

## 1. Foundry リソースとプロジェクトを作る

Foundry ポータル https://ai.azure.com にサインイン（Azure for Students と同じアカウント）。

- **Create new** → プロジェクトを作成
- リージョンを選ぶ

### リージョンの選び方

- **West US 3** を選ぶと **instant models（プレビュー）** が使え、**デプロイ手順を飛ばせます**
- それ以外のリージョンでは、次章のデプロイが必要

学生サブスクは**割り当て（quota）が小さい**ことがあります。デプロイ時に quota エラーが出たら、別リージョンを試してください。

---

## 2. モデルをデプロイする

Foundry ポータル → **Model catalog** → 使いたいモデル → **Deploy**

**最初に非OpenAIモデルを試すことを勧めます。**理由は前提のとおりで、gpt-* が弾かれても他が通れば原因が切り分けられるからです。

候補（長い日本語を扱うのでコンテキスト長を確認すること）:

- Llama 系
- Mistral 系
- DeepSeek 系
- Phi 系

デプロイ後、**デプロイ名**を控えます。**モデル名とデプロイ名は違うことがあり**、API で指定するのはデプロイ名です。

---

## 3. キーとエンドポイントを取得する

2通りあります。どちらでも同じ値です。

- **Azure ポータル** → 作成した Foundry リソース → **Keys and Endpoint**
- **Foundry ポータル** → デプロイ詳細ページ

エンドポイントは次の形です。

```
https://<リソース名>.services.ai.azure.com
```

---

## 4. 保存する

`config/secret.json` に追記します（`.gitignore` 済みなのでコミットされません）。

```json
{
  "gemini_api_key": "...",
  "groq_api_key": "...",
  "bedrock_api_key": "...",
  "azure_ai_key": "ここにキー",
  "azure_ai_endpoint": "https://<リソース名>.services.ai.azure.com"
}
```

---

## 5. 疎通を確認する

```powershell
python claude_work/scripts/test_azure_foundry.py --model <デプロイ名>
```

### なぜ「試す」スクリプトなのか

Azure は今 API の移行期にあります。

- **Azure AI Inference beta SDK は 2026-08-26 に廃止予定**
- 後継は **OpenAI /v1 互換** の API
- 認証も `api-key` ヘッダーと `Authorization: Bearer` が混在

環境によってどれが通るか変わるため、スクリプトは**4つの経路を順に試して、通った組み合わせを報告**します。推測で決め打ちしません。

```
--- 経路を順に試します ---
  NG  OpenAI /v1 互換 + Bearer
        HTTP 401 ...
  OK  OpenAI /v1 互換 + api-key

=== 通った経路 ===
OpenAI /v1 互換 + api-key
  https://xxx.services.ai.azure.com/openai/v1/chat/completions
```

ここで通った経路をルーティング層に登録すれば組み込めます。

**長文の確認**（本番規模に近い日本語1万字）:

```powershell
python claude_work/scripts/test_azure_foundry.py --model <デプロイ名> --big
```

---

## 6. 切り分けの読み方

| 結果 | 意味 |
| :--- | :--- |
| 非OpenAIモデルが通り、gpt-* が 401/403 | **学生サブスクで Azure OpenAI が制限されている**（事前の想定どおり） |
| 両方通る | Azure OpenAI も使える。想定より良い |
| 両方 404 | デプロイ名かエンドポイントの誤り。パス自体は届いている |
| 両方 401/403 | キーの誤り、またはリソース側の権限 |
| デプロイ時に quota エラー | 学生サブスクの割り当て上限。別リージョンを試す |

**この結果は共有してください。**どちらに転んでもルーティング層の設計に反映します。

---

## 7. 注意点

- **$100 は12か月有効**。使い切るとサブスクリプションが停止する（課金ではない）
- キーはリソース全体へのフルアクセスを持ちます。公式も本番では Entra ID による keyless 認証を推奨しています。検証用途なのでキーで進めて構いません
- 使わないリソースは削除しておくとクレジットの無駄を防げます

---

## 出典

- [Endpoints for Microsoft Foundry Models（公式）](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/endpoints)
- [Quickstart: Get started with Microsoft Foundry SDK（公式）](https://learn.microsoft.com/en-us/azure/foundry/quickstarts/get-started-code)
- [Azure for Students — Offer Details（公式）](https://azure.microsoft.com/en-us/pricing/offers/ms-azr-0170p)
- [Azure OpenAI with Azure for Students — Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/2183197/azure-openai-with-azure-for-students)

### 信頼度

| 項目 | 信頼度 |
| :--- | :--- |
| キーとエンドポイントの取得場所 | **high**（公式ドキュメント） |
| Inference SDK の廃止予定日と OpenAI /v1 への移行 | **high**（公式ドキュメント） |
| West US 3 の instant models | **medium**（プレビュー機能。変動しうる） |
| 学生サブスクでの Azure OpenAI 制限 | **medium**（Q&A の報告のみ。**未検証**） |
| どの認証経路が通るか | **未検証**（スクリプトで判定する） |


---

## [AWS_Azure組み込み仕様_Codex向け_20260811.md]

# AWS Bedrock / Azure OpenAI 組み込み仕様（Codex実装用）

**作成日**: 2026-08-11
**状態**: 両方とも**実機で疎通確認済み**。日本語プロンプトで正常応答を得ている
**この文書は `Bedrock組み込み仕様_Codex向け_20260811.md` を置き換える**（Azure の実測結果を追加したため）

---

## 0. 何を作るか

ステージごとにプロバイダとモデルを差し替えられるようにする。
**既存モジュールの本体は書き換えない。**注入口（`executor`）と共通関数を使う。

```
                    ┌─ Gemini      （既存。20回/日）
llm_routing.json ──┼─ Bedrock     （新規。73モデル）
                    └─ Azure OpenAI（新規。GPT-5系）
                              ↓
                    request_json（リトライ・枠切れ判定を集約済み）
```

Groq / OpenRouter / Mistral / Cohere / NVIDIA に個別実装は**不要**。
Bedrock 1本で Llama・Mistral・Qwen・GLM・DeepSeek・Nova・Claude が使える。

---

## 1. 実測済みの事実（推測ではない）

### 1-1. AWS Bedrock

| 項目 | 内容 |
| :--- | :--- |
| 認証 | `Authorization: Bearer <キー>` のみ。**boto3 も SigV4 署名も不要** |
| キー | `config/secret.json` の `bedrock_api_key`（発行済み・136文字） |
| リージョン | `us-east-1` |
| 使えるモデル | **73件**（うち画像入力対応 33件） |
| 疎通実績 | `mistral.mistral-large-3-675b-instruct` で日本語10,046文字→9,449トークン、正常応答 |

```
POST https://bedrock-runtime.us-east-1.amazonaws.com/model/{modelId}/converse
```

リクエスト:

```json
{
  "messages": [{"role": "user", "content": [{"text": "プロンプト"}]}],
  "inferenceConfig": {"maxTokens": 4096, "temperature": 0}
}
```

画像つき（公式 ContentBlock / ImageBlock に準拠）:

```json
{"messages": [{"role": "user", "content": [
  {"text": "プロンプト"},
  {"image": {"format": "png", "source": {"bytes": "<base64>"}}}
]}]}
```

`format` の有効値: `png` / `jpeg` / `gif` / `webp`

レスポンス:

```json
{"output": {"message": {"content": [{"text": "..."}]}},
 "usage": {"inputTokens": 9449, "outputTokens": 18}}
```

### 1-2. Azure OpenAI

| 項目 | 内容 |
| :--- | :--- |
| 認証 | `api-key: <キー>` ヘッダー |
| キー | `config/secret.json` の `foundry_api_key`（84文字） |
| エンドポイント | `azure_ai_endpoint` = `https://<リソース>.openai.azure.com/openai/v1` |
| デプロイ名 | `azure_ai_model` = `gpt-5-mini` |
| 疎通実績 | 3.0秒で `{"units":["一戸層"]}` を取得 |

```
POST {azure_ai_endpoint}/chat/completions
```

**エンドポイントには既に `/openai/v1` が含まれている。**二重に付けないこと。

リクエストは OpenAI 互換だが、**GPT-5 系は `max_tokens` を受け付けない**（後述）。

### 1-3. 学生サブスクリプションで Azure OpenAI は使える

事前調査では「Azure for Students では使えない」という報告があったが、**実測で否定された**。
デプロイさえ作れば動く。

---

## 2. 実装仕様

### 2-1. リトライは既存の `request_json` に集約済み

`scripts/llm_extract.py` の `request_json()` に、429/5xx のリトライ・日次枠切れ判定・
バックオフ・`GeminiAPIError(OSError)` が入っている。テキスト用の `call_gemini` と
Vision 系2つは既にここを通る。

**Bedrock も Azure も必ず `request_json` を通すこと。**独自に `urlopen` を書かない。
Vision 系が独自に叩いていたせいで 503 で即死し、枠を無駄にした事故が実際にあった。

### 2-2. Bedrock

```python
def call_bedrock(prompt, api_key, model, *, images=None, region="us-east-1",
                 timeout=600, quiet=False):
    est = estimate_prompt_tokens(prompt)
    check_budget(model, est)        # ← 迂回する経路は作らない

    content = [{"text": prompt}]
    for image_path, fmt in images or []:
        content.append({"image": {
            "format": fmt,
            "source": {"bytes": base64.b64encode(
                Path(image_path).read_bytes()).decode("ascii")},
        }})

    url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{model}/converse"
    body = json.dumps({
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": 4096, "temperature": 0},
    }).encode("utf-8")

    def build():
        return urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}",
                     "User-Agent": USER_AGENT})

    data = request_json(build, timeout=timeout, quiet=quiet,
                        est_tokens=est, model=model, label="Bedrock")

    usage = data.get("usage") or {}
    record_usage(int(usage.get("inputTokens") or 0)
                 + int(usage.get("outputTokens") or 0))
    text = "".join(
        p.get("text", "")
        for p in ((data.get("output") or {}).get("message") or {}).get("content") or []
        if isinstance(p, dict))
    return data, text
```

### 2-3. Azure OpenAI

**パラメータ名がモデル世代で変わる。**エラー本文を読んでその場で直す実装にすること。
決め打ちすると GPT-5 系で必ず落ちる。

```python
def call_azure(prompt, api_key, endpoint, deployment, *,
               timeout=600, quiet=False):
    est = estimate_prompt_tokens(prompt)
    check_budget(deployment, est)

    # エンドポイントに既に /openai/v1 が入っていることがある
    base = endpoint.rstrip("/")
    url = (base + "/chat/completions"
           if base.endswith("/openai/v1")
           else base + "/openai/v1/chat/completions")

    payload = {
        "model": deployment,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0,
    }

    # GPT-5 系は max_tokens を拒否し max_completion_tokens を要求する。
    # temperature を受け付けないモデルもある。
    # 一度で決めず、エラー本文が嫌ったものを外して投げ直す。
    for _ in range(3):
        def build():
            return urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json",
                         "api-key": api_key, "User-Agent": USER_AGENT})
        try:
            data = request_json(build, timeout=timeout, quiet=quiet,
                                est_tokens=est, model=deployment, label="Azure")
            break
        except GeminiAPIError as exc:
            detail = str(exc)
            adjusted = False
            if "max_completion_tokens" in detail and "max_tokens" in payload:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                adjusted = True
            if ("temperature" in detail and "upported" in detail
                    and "temperature" in payload):
                payload.pop("temperature")
                adjusted = True
            if not adjusted:
                raise
    else:
        raise GeminiAPIError("Azure: パラメータを調整しても通りませんでした")

    usage = data.get("usage") or {}
    record_usage(int(usage.get("prompt_tokens") or 0)
                 + int(usage.get("completion_tokens") or 0))
    choices = data.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    return data, text
```

### 2-4. User-Agent を必ず名乗る

```python
USER_AGENT = "MacroStrat-ColumnBuilder/1.0 (+research; python-urllib)"
```

urllib の既定UA（`Python-urllib/3.x`）は CDN に弾かれる。
Groq は Cloudflare の `error code: 1010` を返した。ブラウザ詐称は不要だが、
素性の分かるUAを名乗る必要はある。

### 2-5. ステージ別ルーティング

3モジュールに `executor` の注入口が既にある。

```
scripts/pdf_unit_bootstrap.py:479   executor: Executor | None = None
scripts/pdf_field_extract.py:360    executor: Executor | None = None
scripts/pdf_alias_mapping.py:177    executor: Executor | None = None
```

#### 設計方針: 無料枠を先に使い切り、尽きたら課金プロバイダへ落とす

Gemini は無料枠（20回/日）がある。**枠が余っているのに課金するのは無駄。**
1図幅5コールなので、**1日4図幅までは費用ゼロで回せる。**

```json
// config/llm_routing.json
{
  "_方針": "無料枠を優先し、尽きたら課金へ。切替は図幅単位（下記の理由）",

  "default": {
    "primary":  {"provider": "gemini",  "model": "gemini-3.6-flash"},
    "fallback": [
      {"provider": "bedrock", "model": "mistral.mistral-large-3-675b-instruct"}
    ]
  }
}
```

ステージ別の指定も同じ形で書けるようにするが、**初期はステージを分けない**（後述）。

#### 切替は「図幅単位」で行うこと。コール単位にしてはいけない

これは実装上の要点で、外すと2つの問題が起きる。

**問題1: キャッシュにモデル名が入るため、混在すると衝突の温床になる**

`unit_id` 重複（`m1286_p019`〜`p022`）の根本原因は、
`gemini-3.5-flash` と `3.6-flash` のキャッシュが混在したことだった。
1図幅の中で3ステージ Gemini・2ステージ Bedrock という状態を作ると、
**同じ図幅のキャッシュが2モデルにまたがる。**再実行時の失効も読めなくなる。

**問題2: 品質を測れなくなる**

`compare_units.py` で GOLD と突き合わせても、
「この数字は Gemini のものか Bedrock のものか」が言えなくなる。
第2段階の測定が意味を失う。

**実装**: 図幅の処理を始める前に残枠を確認し、その図幅で必要なコール数
（現状5）を賄えるなら Gemini、賄えないなら fallback を**その図幅の全ステージに**使う。

```python
def pick_provider(routing, stage, *, calls_needed=5):
    """図幅の処理開始時に1回だけ呼ぶ。途中で切り替えない。"""
    entry = routing.get(stage) or routing.get("default") or {}
    primary = entry.get("primary") or entry
    if str(primary.get("provider")) != "gemini":
        return primary

    limits = load_limits()
    _p, _a, _k, day = today_usage()
    remaining = int(limits.get("max_calls_per_day") or 0) - int(day.get("calls") or 0)
    if remaining >= calls_needed:
        return primary
    for candidate in entry.get("fallback") or []:
        return candidate
    return primary   # fallback が無ければ primary のまま（枠切れは既存処理が検出する）
```

#### 前提: `config/llm_limits.json` の `max_calls_per_day` を実枠に合わせること

現在 **200** になっているが、**実際の Gemini 無料枠は 20回/日**
（429の本文 `generate_content_free_tier_requests, limit: 20` で確認済み）。

このままだと残枠の判定が10倍甘くなり、フォールバックが働かないまま
Gemini の429に突っ込む。**20 に直すこと。**

```json
"max_calls_per_day": 20
```

Flash-Lite など枠の大きいモデルへ変える場合は、その値に合わせて更新する。

#### なぜ最初から分散させないのか

当初この文書には、ステージごとに別モデルを割り当てる案を書いていた。**それは撤回する。**
理由は4つあり、いずれも実測に基づく。

**1. モデルを増やすと、直したばかりのバグを呼び戻す**

`unit_id` 重複（`m1286_p019`〜`p022`）の根本原因は、`gemini-3.5-flash` と `3.6-flash` の
キャッシュが混在したことだった。キャッシュキーにモデル名が入るため、
**モデル数を増やすほど失効と衝突のリスクが上がる。**

**2. 未検証のモデルに費用の大半を置くことになっていた**

分散案では `anthropic.claude-haiku-4-5` に費用の71%を配分していたが、
このモデルは**一度も叩いていない**。同系列の `claude-sonnet-5` は
`not available for this account` を返している。Haiku も同じ可能性がある。

**3. 逐語性が最も要る工程に、最も検証していないモデルを当てていた**

`pdf_unit_alias_mapping` のプロンプトは日本語目次からの逐語コピーを要求する。

```
japanese_alias must be copied exactly from the supplied contents text.
toc_quote must be an exact continuous substring of that same page.
```

ここに割り当てた `gpt-5-mini` の観測は **48トークンの些細なプロンプト1回のみ**。
しかもこの工程は 6,518トークン＝全体の2.8%で、別プロバイダを足す費用対効果がない。

**4. 費用がクレジットを超えていた**

| | 分散案 | 単一案 |
| :--- | ---: | ---: |
| 1図幅 | $0.2019 | **$0.134** |
| 全国1300面 | **$262（$200超過）** | **$174** |
| モデル数 | 4 | **1** |
| 未検証モデル | 2ステージ | 0 |

**`mistral.mistral-large-3-675b-instruct` を選ぶ理由**（すべて実測）:

- 画像入力に対応（`--list` の vision 一覧に存在）
- コンテキスト 256K（本番の約85,000トークンに余裕）
- **唯一、実機で日本語10,046文字を通してレスポンスを得たモデル**
- 画像対応モデルの中で最安（$0.14/図幅）

**分散させるのは、GOLD比較で「どの工程が弱いか」が判明してから。**
それが根拠のある分散であり、いまの段階でやるのは推測にすぎない。

---

## 3. 落とし穴（実際に踏んだもの）

| # | 症状 | 対処 |
| :--- | :--- | :--- |
| 1 | Azure: `'max_tokens' is not supported` | GPT-5系は `max_completion_tokens`。エラーを読んで適応 |
| 2 | Azure: `DeploymentNotFound` | モデル一覧406件はカタログ。呼ぶにはデプロイが要る |
| 3 | Azure: エンドポイントに既に `/openai/v1` | 二重に付けない |
| 4 | Azure: デプロイ名 ≠ モデル名 | API で指定するのはデプロイ名 |
| 5 | Bedrock: 表示名 ≠ モデルID | 「Claude Sonnet 5」→ `anthropic.claude-sonnet-5` |
| 6 | Bedrock: `not available for this account` | 403だがキーの問題ではない。モデル未開放 |
| 7 | Bedrock: `:24k` などの変種 | コンテキスト長。**本番は約85,000トークン**なので128K未満は不可 |
| 8 | Groq: `403 error code 1010` | UA未設定。CDNのブロック |
| 9 | モデル名の改廃 | Cohere `command-r-plus` は2025-09-15廃止。名前は毎回APIから取る |
| 10 | 単価がモデル間で30倍違う | 1つの単価で概算すると桁を誤る |

### 出力形式の揺れ

Mistral Large 3 は「説明は不要」と指示しても、同一条件3回で出力が揺れた
（前置きあり35トークン → なし18 → あり35）。**temperature 0 でも決定的にならない。**

`parse_json_block` の挙動:

| 入力 | 結果 |
| :--- | :--- |
| 前置き + フェンス + **配列** | ❌ |
| 前置き + フェンス + **オブジェクト** | ✅ |

**本番プロンプトはすべてオブジェクト形式**なので実害はない。
ただしプロンプトでオブジェクトを要求し続けること。

---

## 4. やってはいけないこと

| | 理由 |
| :--- | :--- |
| `prompt_version` を上げる | 全図幅のキャッシュが失効し、Gemini の枠（20回/日）を再消費する |
| `check_budget` / `record_usage` を迂回 | `call_gemini` の docstring に明記された不変条件 |
| 独自に `urlopen` を書く | リトライと枠切れ判定が効かない。Vision 系で実際に事故った |
| SigV4 署名を実装する | 不要。ベアラートークンで動くことを実測済み |
| 例外を `RuntimeError` にする | `pilot.py:912` の `except OSError` から漏れてパイプライン全体が落ちる。`OSError` 系にすること |
| モデル名を推測で決め打ち | 改廃で壊れる。APIから取得する |

---

## 5. 費用（実測単価）

1図幅あたりのステージ別入力トークン（すべて実測）:

| ステージ | 入力トークン | 画像 |
| :--- | ---: | :---: |
| `pdf_body_field_enrichment` | 100,374 | − |
| `pdf_environment_multimodal` | 97,388 | ○ |
| `column_geography_vision` | 25,657 | ○ |
| `pdf_unit_bootstrap` | 約7,600 | − |
| `pdf_unit_alias_mapping` | 6,518 | − |
| **合計** | **約237,500** | |

モデル別（出力1万トークンを加算）:

| モデル | 画像 | 1図幅 | 全国1300面 |
| :--- | :---: | ---: | ---: |
| `zai.glm-4.7-flash` | ✗ | $0.022 | $28 |
| `openai.gpt-oss-120b-1:0` | ✗ | $0.044 | $57 |
| **`mistral.mistral-large-3-675b-instruct`** | **✓** | **$0.134** | **$174** |
| `anthropic.claude-haiku-4-5-...` | ✓ | $0.30 | $390（未検証） |
| `anthropic.claude-sonnet-5` | ✓ | $0.60 | $780（現状403） |

**画像対応で最安は Mistral Large 3。**$200のAWSクレジット内で全国走破できる。
最安の GLM/gpt-oss はテキスト専用なので画像2ステージには使えない。

Azure は $100 クレジット、12か月有効。GPT-5系という Bedrock に無い選択肢を持つが、
**現時点では予備**。単一モデル構成で弱点が判明してから投入する。

---

## 6. 検証

### 疎通（既存スクリプトが使える）

```powershell
python claude_work/scripts/test_bedrock.py --list
python claude_work/scripts/test_bedrock.py --model <ID> --big
python claude_work/scripts/test_all_providers.py --only azure
```

### 単体テスト

**実ネットワークを使わず** `urlopen` を差し替えること。
`config/llm_usage.json` を汚さないよう `record_usage` も差し替える。

参考になる既存テスト:
- `claude_work/tests/test_llm_retry.py`（53 PASS）
- `claude_work/tests/test_vision_retry.py`（20 PASS）

**追加すべきテスト**:
1. Bedrock: Converse の body/レスポンス変換
2. Bedrock: 画像を含む ContentBlock の組み立て
3. Azure: `max_tokens` 拒否 → `max_completion_tokens` へ自動切替
4. Azure: エンドポイントに `/openai/v1` があってもURLが二重にならない
5. 両方: 例外が `OSError` 互換であること
6. 両方: 独自の `urlopen` を書いていないこと（`inspect.getsource` で検査）

### 実データでの評価

```powershell
python run.py ichinohe

python claude_work/scripts/compare_units.py `
  "claude_work/reports/Ichinohe_reference_GOLD.xlsx" `
  "data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx" `
  --out claude_work/reports/比較_<モデル名>.md
```

**現在の基準値（Gemini 3.6 Flash）**

```
対応 30 地層 / GOLD のみ 12 / 出力のみ 18
一致 48 / 不一致 18 / 捏造 0 / 取りこぼし 137
```

**捏造 0 の維持が最重要。**プロジェクト規則「推測で値を埋めない」に直結する。
一致率が上がっても捏造が増えるモデルは採用しないこと。

### 既存テストを壊さないこと

```powershell
python claude_work/tests/test_roundtrip.py     # 440 PASS / 0 FAIL
```

`claude_work/tests/test_*.py` は現在36ファイルすべて成功する。

---

## 6-2. 進め方（段階を守ること）

**第0段階 — Gemini の残枠を使い切る**

無料枠が残っている間は Gemini で回す。費用ゼロ。
この段階で `run.py ichinohe --force` を1本通し、Column 分割の修正が反映されるか確認する。

**第1段階 — 枠が尽きたら Bedrock 単一モデルで通す**

`llm_routing.json` の fallback（`mistral.mistral-large-3-675b-instruct`）で
m1286 を1本流す。ここで見るのは精度ではなく「**壊れずに通るか**」。

- 5ステージすべてが応答を返すか
- 画像2ステージが Bedrock の ImageBlock で通るか（**未検証の箇所**）
- `record_usage` に記録が入るか
- 既存36テストが通るか

**第2段階 — GOLD と突き合わせて基準を作る**

```powershell
python claude_work/scripts/compare_units.py `
  "claude_work/reports/Ichinohe_reference_GOLD.xlsx" `
  "data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx" `
  --out claude_work/reports/比較_mistral単一.md
```

現行 Gemini の値と並べる。

```
Gemini 3.6 Flash:  一致 48 / 不一致 18 / 捏造 0 / 取りこぼし 137
Mistral 単一:      ?
```

**第3段階 — 弱い工程だけ差し替える**

第2段階で「どの項目が落ちたか」が分かる。たとえば

- `strat_name` の取りこぼしが増えた → `pdf_unit_bootstrap` を別モデルへ
- `environment` の不一致が増えた → `pdf_environment_multimodal` を別モデルへ

**このときはじめて Azure GPT-5系や Claude 系を投入する。**
1ステージずつ変え、そのつど `compare_units.py` で測る。

**やってはいけないのは、第1段階を飛ばして最初から分散させること。**
どのモデルが原因で数字が動いたか分からなくなる。

### 費用の見通し

無料枠を優先する設計なので、実際の支出は処理ペースで決まる。

| 1日の処理量 | Gemini（無料20回） | 課金分 | 1日の費用 |
| :--- | ---: | ---: | ---: |
| 4図幅まで | 20コール | 0 | **$0** |
| 10図幅 | 20コール | 30コール | 約 $0.80 |
| 全国1300面を一気に | 20コール | 6,480コール | 約 $174 |

**急がなければ費用はほぼゼロ。**$200のクレジットは「急ぎたいとき」と
「Gemini が使えないとき」の保険として温存できる。

---

## 7. 出典

- [API keys — Amazon Bedrock（公式）](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)
- [ContentBlock — Bedrock API Reference（公式）](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ContentBlock.html)
- [ImageBlock — Bedrock API Reference（公式）](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ImageBlock.html)
- [Endpoints for Microsoft Foundry Models（公式）](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/endpoints)

## 8. 信頼度

| 項目 | 信頼度 |
| :--- | :--- |
| Bedrock がベアラートークンで動く | **high**（応答取得済み） |
| Azure が学生サブスクで動く | **high**（応答取得済み） |
| Converse のテキスト形式・レスポンス形式 | **high**（実測） |
| Azure の `max_completion_tokens` 要求 | **high**（実際のエラーで確認） |
| Bedrock の画像 ContentBlock 形式 | **high**（公式APIリファレンス）。**実際の画像送信は未実施** |
| Azure GPT-5 系の画像対応 | **未確認** |
| 単価 | **medium**（カタログ値。請求で要確認） |
| 各モデルの日本語地質記載での精度 | **未検証**（`compare_units.py` で測ること） |


---

## [Bedrock組み込み仕様_Codex向け_20260811.md]

# Amazon Bedrock 組み込み仕様（Codex実装用）

**作成日**: 2026-08-11
**状態**: 疎通確認済み。実際に日本語1万字を通してレスポンスを得ている
**目的**: 1つのキーで73モデルを使えるようにし、ステージごとにモデルを選べるようにする

---

## 0. なぜ Bedrock だけ入れれば足りるのか

`--list` で実測した結果、**1つのキー・1つのエンドポイント・1つのAPI形式**で以下が使える。

```
テキスト出力モデル 73 件（うち画像入力に対応 33 件）

Amazon (Nova) / Anthropic (Claude) / Google (Gemma) / Meta (Llama)
Mistral / MiniMax / Moonshot (Kimi) / NVIDIA (Nemotron) / OpenAI (gpt-oss)
Qwen / Writer (Palmyra) / Z.AI (GLM) / DeepSeek / TwelveLabs / Cohere
```

Groq・OpenRouter・Mistral・Cohere・NVIDIA に個別のクライアントを書く代わりに、
**Bedrock 1本 + 既存の Gemini** で足りる。実装量が大きく減る。

---

## 1. 確認済みの事実（推測ではない）

### 認証は Bearer トークンだけ

**boto3 も SigV4 署名も不要。**既存の `urllib` そのままで動く。実測済み。

```
Authorization: Bearer <キー>
Content-Type: application/json
```

キーは `config/secret.json` の `bedrock_api_key`（`.gitignore` 済み）。
長期キーは Bedrock コンソール → API keys → Long-term で発行。

### エンドポイントとリクエスト形式（Converse API）

```
POST https://bedrock-runtime.us-east-1.amazonaws.com/model/{modelId}/converse
```

テキストのみ:

```json
{
  "messages": [
    {"role": "user", "content": [{"text": "プロンプト"}]}
  ],
  "inferenceConfig": {"maxTokens": 4096, "temperature": 0}
}
```

画像つき（公式 ContentBlock / ImageBlock に準拠）:

```json
{
  "messages": [
    {"role": "user", "content": [
      {"text": "プロンプト"},
      {"image": {
        "format": "png",
        "source": {"bytes": "<base64エンコードした画像>"}
      }}
    ]}
  ],
  "inferenceConfig": {"maxTokens": 4096, "temperature": 0}
}
```

- `format` の有効値: `png` / `jpeg` / `gif` / `webp`
- `content` は配列で、テキストと画像を並べられる
- Gemini の `inline_data` とは形が違うので変換が要る

### レスポンス形式

```json
{
  "output": {"message": {"content": [{"text": "..."}]}},
  "usage": {"inputTokens": 9449, "outputTokens": 35}
}
```

`usage.inputTokens` / `usage.outputTokens` を `record_usage()` に渡すこと。

### 実測値

| 項目 | 値 |
| :--- | :--- |
| 送信 | 10,046 文字（日本語） |
| 実測トークン | 9,449 |
| `estimate_prompt_tokens` の推定 | 10,042（誤差 +6.3%、安全側） |
| 本番プロンプト 90,858文字 の予測 | 約 85,000 トークン |

**トークン推定器は日本語で正しく機能している**ことが実データで確認できた。

---

## 2. 実装箇所

### 2-1. リトライは既に共通化済み

`scripts/llm_extract.py` の `request_json()` に、429/5xx のリトライ・日次枠切れ判定・
バックオフが集約してある。テキスト用の `call_gemini` と Vision 系2つが既にここを通る。

**Bedrock も `request_json` を通すこと。**独自に `urlopen` を書かない。
過去に Vision 系が独自に叩いていたせいで、503 で即死して枠を無駄にした事故がある。

```python
def call_bedrock(prompt, api_key, model, *, images=None, region="us-east-1",
                 timeout=600, quiet=False):
    est = estimate_prompt_tokens(prompt)
    check_budget(model, est)        # ← 必ず通す。ここを迂回する経路は作らない

    content = [{"text": prompt}]
    for image_path, fmt in images or []:
        content.append({"image": {
            "format": fmt,
            "source": {"bytes": base64.b64encode(Path(image_path).read_bytes()).decode("ascii")},
        }})

    url = (f"https://bedrock-runtime.{region}.amazonaws.com"
           f"/model/{model}/converse")
    body = json.dumps({
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": 4096, "temperature": 0},
    }).encode("utf-8")

    def build():
        return urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"})

    data = request_json(build, timeout=timeout, quiet=quiet,
                        est_tokens=est, model=model, label="Bedrock")

    usage = data.get("usage") or {}
    used = int(usage.get("inputTokens") or 0) + int(usage.get("outputTokens") or 0)
    record_usage(used)              # ← 必ず記録する
    text = "".join(
        part.get("text", "")
        for part in ((data.get("output") or {}).get("message") or {}).get("content") or []
        if isinstance(part, dict)
    )
    return data, text
```

### 2-2. ステージ別のモデル指定

3モジュールには既に `executor` の注入口がある。**本体を書き換えずに差し替えられる。**

```
scripts/pdf_unit_bootstrap.py:479   executor: Executor | None = None
scripts/pdf_field_extract.py:360    executor: Executor | None = None
scripts/pdf_alias_mapping.py:177    executor: Executor | None = None
```

設定ファイルの案:

```json
// config/llm_routing.json
{
  "pdf_body_field_enrichment": {"provider": "bedrock", "model": "mistral.mistral-large-3-675b-instruct"},
  "pdf_unit_alias_mapping":    {"provider": "bedrock", "model": "zai.glm-4.7-flash"},
  "pdf_unit_bootstrap":        {"provider": "gemini",  "model": "gemini-3.6-flash"},
  "column_geography_vision":   {"provider": "bedrock", "model": "anthropic.claude-haiku-4-5-20251001-v1:0"},
  "pdf_environment_multimodal":{"provider": "bedrock", "model": "anthropic.claude-haiku-4-5-20251001-v1:0"}
}
```

未指定のステージは現行どおり Gemini にフォールバックすること。

---

## 3. 落とし穴（実際に踏んだもの）

### 3-1. モデルの表示名とIDは別物

カタログの「Claude Sonnet 5」に対して、APIで使うIDは `anthropic.claude-sonnet-5`。
IDは `python claude_work/scripts/test_bedrock.py --list` で取得できる。

### 3-2. コンテキスト変種に注意

```
amazon.nova-lite-v1:0:24k     ← 10万トークンは通らない
amazon.nova-lite-v1:0:300k    ← こちらを使う
```

`:24k` のような接尾辞はコンテキスト長。**本番プロンプトは約85,000トークン**なので、
128K未満の変種は使えない。

### 3-3. 「有効化済み」とは限らない

`--list` に出ても使えないことがある。実測で `anthropic.claude-sonnet-5` は次を返した。

```
HTTP 403 {"message":"anthropic.claude-sonnet-5 is not available for this account."}
```

**これはキーの問題ではない。**モデルがアカウントに開放されていないだけ。
`not available for this account` を含む403は、キー無効とは別に扱うこと。

### 3-4. 最安モデルは画像が使えない

| モデル | 1図幅 | 画像 |
| :--- | ---: | :---: |
| `zai.glm-4.7-flash` | $0.022 | ✗ |
| `openai.gpt-oss-120b-1:0` | $0.044 | ✗ |
| `minimax.minimax-m2.5` | $0.09 | ✗ |
| `deepseek.v3.2` | $0.17 | ✗ |
| `mistral.mistral-large-3-675b-instruct` | **$0.14** | **✓** |
| `anthropic.claude-haiku-4-5-...` | $0.30 | ✓ |
| `anthropic.claude-sonnet-5` | $0.60 | ✓（現状403） |

**画像対応で最安は Mistral Large 3。**全国1,300面で約$182（$200クレジット内）。
テキスト専用モデルは本文抽出・別名対応にだけ使える。

### 3-5. モデルによって単価が30倍違う

1つの単価で概算すると桁を誤る。`test_bedrock.py` の `PRICES` に単価表がある。
（最初 Claude Sonnet の単価を決め打ちしていて、Mistral の費用を6倍に表示する不具合を出した。）

### 3-6. 応答に前置きが付くことがある

Mistral Large 3 は「説明は不要」と指示しても前置きを付けた。

```
以下のJSON配列に地層名を抜き出しました。
```json
[...]
```
```

`parse_json_block` は **オブジェクト形式なら**前置き＋コードフェンス付きでも取り出せる。
本番プロンプトはすべて `{"units":...}` `{"aliases":...}` のオブジェクト形式なので実害はない。
ただし**トップレベルが配列だと取り出せない**ので、プロンプトでオブジェクトを要求し続けること。

---

## 4. やってはいけないこと

| | 理由 |
| :--- | :--- |
| `prompt_version` を上げる | 全図幅のキャッシュが失効し、Gemini の枠（20回/日）を再消費する |
| `check_budget` / `record_usage` を迂回する | 使用量の記録が壊れる。`call_gemini` の docstring にも明記されている不変条件 |
| 独自に `urlopen` を書く | リトライと枠切れ判定が効かなくなる。Vision 系で実際に事故った |
| SigV4 署名を実装する | 不要。ベアラートークンで動くことを実測済み |
| 例外を `RuntimeError` にする | `pilot.py:912` の `except OSError` から漏れてパイプライン全体が落ちる。`GeminiAPIError(OSError)` と同様に `OSError` 系にすること |

---

## 5. 検証手順

### 疎通

```powershell
python claude_work/scripts/test_bedrock.py --list
python claude_work/scripts/test_bedrock.py --model <ID> --big
```

### 単体テスト

既存の枠組みに合わせて追加すること。参考になる既存テスト:

- `claude_work/tests/test_llm_retry.py` — リトライの検証（53 PASS）
- `claude_work/tests/test_vision_retry.py` — Vision 系がリトライを通ること（20 PASS）

**実ネットワークを使わず** `urlopen` を差し替えて検証する形にすること。
`config/llm_usage.json` を汚さないよう `record_usage` も差し替える。

### 実データでの評価

```powershell
python run.py ichinohe

python claude_work/scripts/compare_units.py `
  "claude_work/reports/Ichinohe_reference_GOLD.xlsx" `
  "data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx" `
  --out claude_work/reports/比較_bedrock.md
```

**現在の基準値（Gemini 3.6 Flash）**

```
対応 30 地層 / GOLD のみ 12 / 出力のみ 18
一致 48 / 不一致 18 / 捏造 0 / 取りこぼし 137
```

**捏造 0 を維持できるかが最重要。**これはプロジェクト規則「推測で値を埋めない」に
直結する。一致率が上がっても捏造が増えるモデルは採用しないこと。

---

## 6. 全テストが通ることの確認

```powershell
python claude_work/tests/test_roundtrip.py     # 440 PASS / 0 FAIL であること
```

`claude_work/tests/test_*.py` は現在36ファイルすべて成功する状態。これを崩さないこと。

---

## 7. 出典

- [API keys — Amazon Bedrock（公式）](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)
- [ContentBlock — Bedrock API Reference（公式）](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ContentBlock.html)
- [ImageBlock — Bedrock API Reference（公式）](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ImageBlock.html)

## 8. 信頼度

| 項目 | 信頼度 |
| :--- | :--- |
| ベアラートークンで動く／boto3不要 | **high**（実際に応答を得ている） |
| Converse のテキスト形式・レスポンス形式 | **high**（実測） |
| 画像の ContentBlock / ImageBlock 形式 | **high**（公式APIリファレンス）。**ただし実際の画像送信は未実施** |
| モデルIDと画像対応の一覧 | **high**（APIから取得） |
| 単価 | **medium**（カタログの表示値。請求で要確認） |
| 各モデルの日本語地質記載での精度 | **未検証**（compare_units.py で測ること） |

