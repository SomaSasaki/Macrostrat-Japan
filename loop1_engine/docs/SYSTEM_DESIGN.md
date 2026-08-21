# MacroStrat GSJ データ処理パイプライン — システム設計図

> **目的:** レビュー担当（人・AI問わず）がシステム全体を把握するためのリファレンス。
> **最終更新:** 2026-08-07（第2版）
> **準拠フォーマット:** Macrostrat column-ingestion format **v0.1.1**
> **完成形リファレンス:** `data/03_submission/05_青森/m1286_一戸 2018/Ichinohe_Composite_column.xlsx`
> **回帰テスト:** `python claude_work/tests/test_roundtrip.py` — 現在 **440 PASS / 0 FAIL**

---

## 1. システム概要

産総研（GSJ）の **5万分の1地質図幅** から、**Macrostrat公式の取り込みフォーマット** へ変換するパイプライン。

```
GSJ (ZFK API / 説明書PDF)
        │  python run.py make <図幅>
        ▼
   レビュー用 Excel（候補が埋まった状態）
        │  ← 研究者が確認・補完（ここだけが人の作業）
        │  python run.py check <図幅>     入力前チェック
        ▼
   提出用 Excel（公式v0.1.1・5シート）
        │  python run.py export <図幅>
        ▼
      Macrostrat
```

### 設計原則

| 原則 | 具体的にどう守っているか |
|:---|:---|
| **推測で埋めない** | 自動取得できない値は空欄。LLMの出力も原文照合を通らなければ捨てる |
| **出典を残す** | `comments` / `gsj_meta` / `REF_*` 列に取得元とページを記録 |
| **既存の作業を壊さない** | `make` は既存レビューを上書きしない。`--force` 時も `.bak_日時.xlsx` を作る |
| **完成形との一致を機械で保証** | 一戸完成形42層・prop 59個を回帰テストで突合 |
| **課金しない** | モデル・回数・トークンの三重の上限。API呼び出し前に停止 |
| **自動入力は必ず記録を残す** | 上書きする前に `.bak_日時.xlsx` を作り、変わった値を旧→新で一覧表示する。LLMの出力は実行ごとに変わるため |
| **誤った値より空欄** | 本文が層厚として述べていない数値は入れない。判断材料（該当文＋PDFページ）だけ出す |

---

## 2. ファイル構成

```
MacroStrat/
├── run.py                        ★ 唯一のCLI入口。ユーザーはこれだけ叩く
│
├── scripts/                      ★ 実装の唯一の正（single source of truth）
│   ├── common.py                 列定義・変換ロジック。他は全部ここを参照する
│   ├── make_review_sheet.py      make の本体
│   ├── export_submission.py      export / check の本体
│   ├── extract_abstract.py       PDF巻末の英文Abstractを切り出す
│   ├── llm_extract.py            Gemini呼び出し・出力検証・課金ガード
│   ├── apply_llm_candidates.py   LLM候補をレビューExcelに書き込む
│   ├── build_zfk_index.py        index の本体
│   ├── search_zfk.py             search の本体
│   ├── build_vocab.py            vocab の本体（Macrostrat公式語彙の取得）
│   ├── gsj_derived.py            ★ ZFKの derived（GSJが本文から抽出済みの構造化データ）を読む
│   ├── pdf_locate.py             ★ 本文の一節が図幅PDFの何ページかを照合する
│   └── repair_layout.py          repair の本体
│
├── config/
│   ├── age_mapping.json          地質時代(和) → t_int/b_int
│   ├── lithology_mapping.json    岩相(和キーワード) → Macrostrat岩相名
│   ├── intervals.json            ★ Macrostrat公式intervalの b_age/t_age（1715件）prop計算用
│   ├── map_index.json            GSJ全図幅リストのキャッシュ（地名→ID解決）
│   ├── zfk_index.json            ZFKデータのある図幅の索引（run.py index で生成）
│   ├── vocab.json                Macrostrat公式の語彙表（環境83/岩相213/属性180・run.py vocab で更新）
│   ├── llm_limits.json           ★ 課金ガードの上限値
│   ├── llm_usage.json            使用量の記録（.gitignore済み・自動生成）
│   ├── secret.json               ★ APIキー（.gitignore済み・手動作成）
│   └── secrets.example.json      secret.json の雛形
│
├── data/
│   ├── raw/zfk/m{id}/            ZFK APIレスポンスのキャッシュ
│   ├── 02_review/{地域}/m{id}_{地名}/
│   │   ├── references/           PDF・Shapefile・GeoTIFF・凡例画像・m{id}_abstract.txt
│   │   └── m{id}_review.xlsx     ★ 研究者が編集するファイル
│   └── 03_submission/{地域}/m{id}_{地名}/
│       └── {ProjectName}_Composite_column.xlsx   ★ 提出物
│
├── claude_work/                  Claudeの作業領域（Codexとの競合防止）
│   ├── backup/                   改修前スクリプトの退避
│   ├── tests/test_roundtrip.py   ★ 回帰テスト
│   └── reports/                  完成形リファレンスのコピー・ドキュメントの控え
│
├── docs/SYSTEM_DESIGN.md         ← このファイル
├── docs/ROADMAP.md               これから作るもの
└── .gitignore                    secret.json / llm_usage.json を除外
```

> **実装は `scripts/` だけにある。** 以前は `claude_work/scripts/` に複製を置いていたが、
> 片方だけ直して古い `run.py` が残る事故が起きたため一本化した。`claude_work/` には
> 改修前のバックアップ・テスト・レポートだけを置く。

---

## 3. コマンド一覧

| コマンド | 用途 |
|:---|:---|
| `python run.py make <名前\|ID>` | レビュー用Excel生成。ZFK・PDF・凡例画像・**英文Abstract**を一括取得 |
| `python run.py make <名前> --columns 3` | west/central/east の3Columnを最初から用意 |
| `python run.py make <名前> --compiler "名前"` | compiler_name を埋める |
| `python run.py make <名前> --force` | 既存レビューを上書き（`.bak_日時.xlsx` を自動作成） |
| `python run.py check <名前>` | 出力せずに検証だけ実行 |
| `python run.py export <名前>` | 提出ファイルを生成 |
| `python run.py list` | 全図幅の入力進捗 |
| `python run.py repair` | Excelの表示崩れを修復（**セルの値は変更しない**） |
| `python run.py repair --migrate` | 古いレビューファイルを現行の列構成へ引き上げる（**値は保持**。`--dry` で確認だけ） |
| `python run.py abstract <名前>` | Abstract取り直し（`make` に含まれるので通常不要） |
| `python run.py llm --usage` | API使用量と上限（課金されていないことの確認） |
| `python run.py llm --test` | キー・接続・モデルの確認 |
| `python run.py llm --models` | このキーで使えるモデル一覧 |
| `python run.py llm <名前>` | Abstractから抽出し、REF_列と編集列に**自動入力**（上書き前に自動バックアップ＋変更ログ） |
| `python run.py llm <名前> --keep` | REF_列だけ更新し、編集列の既存値には触らない |
| `python run.py llm <名前> --dry` | 書き込まずに表示のみ |
| `python run.py llm <名前> --debug` | APIレスポンスの構造を表示（調査用） |
| `python run.py index` | ZFK図幅の索引を作成（最初に1回・数分） |
| `python run.py search [aomori]` | ZFKデータのある図幅を検索 |
| `python run.py vocab --show` | Macrostrat公式語彙の件数を表示 |
| `python run.py vocab` | 公式APIから語彙表を取り直す（年1回程度でよい） |

複数指定可: `python run.py make 1050 ichinohe`

---

## 4. Excelファイル仕様

### 4-1. `m*_review.xlsx`（レビュー用・11シート）

| シート | 用途 |
|:---|:---|
| Instructions | 使い方 |
| **units_review** | 地層データ本体。グレー(`REF_*`)=参照専用 / 黄色=要入力 / 水色=自動計算 / 緑=提出物へ転記 |
| columns_review | Column定義 |
| refs_review | 文献（ZFK書誌情報から自動下書き） |
| images_review | 図版（references/ の凡例・断面画像から自動下書き） |
| project_meta | プロジェクトメタデータ（key-value） |
| gsj_meta | GSJ由来の出典情報（自動・参照専用） |
| **abstract** | 図幅PDF巻末の英文Abstract（段落ごと1行） |
| **intervals** | prop計算用の参照表（**193件**）。`common.intervals_for_excel()` が絞る |
| **descriptions** | 地層ごとの説明文の**全文**（セルは抜粋しか持てないため） |
| **thickness_notes** | 本文中の層厚の記述を全部並べたもの。areaで値が変わる判断に使う |

#### `units_review` の列（39列・左から順）

情報源は2系統ある。**混ぜずに並べて、人が見比べて選べるようにする。**

- **[本文]** ZFKの `derived` … GSJが図幅説明書の日本語本文から抽出済みの構造化データ。細かく、確信度と該当箇所つき。LLMを通さないので幻覚が無い。
- **[要約]** 英文Abstract … LLMが読んだもの。最初からMacrostrat向けの英語だが、要約なので粗い。


| 列 | 種別 | 説明 |
|:---|:---|:---|
| REF_unit_name_en | 参照 | 「地層名 (岩相)」形式の原文（ZFK: `parent_facies` + `focus`） |
| REF_unit_name_ja | 参照 | 同上の日本語 |
| **REF_source** | 参照 | **出典ページ。** `§4.12 十和田段丘堆積物（Tw）｜ PDF p.59（印刷 p.51）｜ 小見出し: 地層名 / 模式地 / 分布及び層厚 / 層序関係 / 岩相 / 時代` |
| REF_age_text | 参照 | ZFKの地質時代 原文 |
| REF_age_from_abstract | 参照 | [要約] 年代候補（原文引用つき） |
| **REF_desc** | 参照 | [本文] 地層の説明文 **全文**（切らない・折り返しなし） |
| **REF_thickness** | 参照 | [本文] 【採用値】【本文中の数値】【GSJ自動抽出】を1文ごとの出典ページつきで **全文** |
| REF_lith_text | 参照 | ZFKの岩相 原文 |
| REF_lith_candidates | 参照 | 辞書マッチングによる岩相候補 |
| **REF_lithology_gsj / REF_minor_lith_gsj** | 参照 | **[本文]** GSJ が日本語本文から抽出した岩相。`凝灰岩→tuff「…該当文…」§4.12 / 岩相` の形 |
| REF_lithology / REF_minor_lith | 参照 | **[要約]** 英文Abstract由来の岩相 |
| REF_strat_name | 参照 | [本文] ZFK凡例の階層から組み立てた層序名 |
| REF_environment | 参照 | [要約] 堆積環境 |
| REF_basal_surface | 参照 | [本文] `derived.contacts` の関係（unconformable など） |
| **REF_unit_description** | 参照 | [要約] 英文の地層記載。そのまま `unit_description` になる |
| unit_id | 参照 | ZFKの地層ID（提出物では `source_unit_id` に退避） |
| **column_id** | **要入力** | columns_review の col_id。「a, b」でカンマ区切り可 |
| **sort_order** | **要入力** | 最上位（新しい）=1、下に行くほど大きい数 |
| **unit_name** | **要入力** | `(岩相)` を除いた地層名。自動補完済み |
| t_int / b_int | 自動+修正可 | 上位（若い）/ 下位（古い）の地質時代 |
| **t_age_ma / b_age_ma** | **要入力** | 地層の年代 [Ma]（15.3ka なら 0.0153）。入れると prop が出る |
| t_prop / b_prop | **自動計算** | Excel数式。`intervals` を VLOOKUP。直接上書きも可 |
| strat_name | **自動**[本文] | ZFK凡例の階層から。`make` の時点で入る |
| environment | **自動**[要約] | `llm` で入る。公式語彙と照合して警告を出す |
| unit_description | **自動**[要約] | `llm` で入る。英文Abstractの記述から |
| lithology / minor_lith | **手動** | ★ここだけ自動入力しない。[本文]と[要約]を見比べて主／副を決める |
| min_thickness / max_thickness | **自動**[本文] | 「分布及び層厚」節から。GSJの `derived` は使わない（下記） |
| basal_surface | **自動**[本文] | `derived.contacts` から |
| lateral_relationship | 手動 | `interfingering` / `onlaps` など |
| section_id | **自動計算** | export時に年代ギャップから算出。手入力を優先 |
| t_pos | **自動計算** | 各Columnの最上位に必ず入れる（下記）。手入力を優先 |
| comments | 手動 | 備考・出典（ページ・図表番号） |

### 4-1b. 自動入力の設計判断

#### 層厚は GSJ の `derived.thickness` を使わない

ZFK には GSJ が本文から抽出済みの `derived.thickness` が入っている。しかし
十和田図幅23層で本文と突き合わせたところ、**ほぼ全件で地層全体の層厚ではなかった**。

| 地層 | `derived` | 本文（分布及び層厚 節） |
|:---|---:|:---|
| 道川層 | 1 m | 「本層の層厚は最大で300～400mである」 |
| 小増沢層 | 25 m | 「層厚は最大で約300mである」 |
| 月日山火山岩類 | 1 m | 「最大層厚は250m程度である」 |
| 野辺地層 | 10 m | 「層厚は少なくとも30m以上である」 |
| 八甲田第2期火砕流堆積物 | 5 m | 「検行平牧野付近で最大約150m」 |

`derived` は本文のどこかにある「層厚○m」を1つ拾うだけなので、「岩相」節に出てくる
**1枚の砂層の厚さ**を掴んでしまう。そこで `gsj_derived.thickness_from_section()` が
**「分布及び層厚」節に限定して**読む方式にした。読めなければ空欄にする（誤った値を
入れるより空欄のほうがよい）。`derived` の値は REF_thickness に「参考程度に」と
注記して残してある。

さらに「高峠付近で約70m，立惣辺山付近で50～140m」のように場所で変わる場合は、
その節の数値を全部集めて全体の下限・上限にし、`場所により変動` と印を付ける。

#### `t_pos` は各Columnの最上位に必ず入れる

公式仕様にこうある。

> Units that are unbounded at the top or bottom of a section are **dropped during
> ingestion**, but their `t_pos`,`b_pos` values are still used to infer the bounds
> of units above or below. In practice, this can allow a section to be defined with
> a single `position` column, **if an unbounded unit is included at the top**.

`position`（= `b_pos`）だけだと最上位の層は上端が決まらず「unbounded」になり、
取り込み時に落ちる。一戸完成形も各Columnの最上位に `t_pos = max(position)+1` を
入れている（central: 7 → 8 / west: 18 → 19 / east: 15 → 16）。

★ `column_id` は「1, 2」と複数Columnを指せる。文字列のままグループ分けすると
「1, 2」が1つのColumnとして扱われ誤った値が入るので、`common.auto_t_pos()` で
必ず展開してから Column ごとに計算する。

#### 噴火イベントの `t_prop` / `b_prop`

火砕流・テフラ・溶岩のように**年代が1点で決まる**堆積は、本来 `b_prop` と `t_prop` が
同じ値になる。しかし公式仕様は `b_prop must be less than t_prop` を求める。そこで
**表示桁（小数第3位）に四捨五入すると同じ値になる最小の幅**を上下端にする。

```
prop = 0.13212  →  表示は 0.132
   b_prop = 0.132 - 0.0005        = 0.1315
   t_prop = 0.132 + 0.0005 - ε
          = 0.1324999…  → 下5桁で切り捨て → 0.13249
```

判定は **「年代が1点」かつ「地層名が火砕流・テフラ・溶岩などを含む」の両方**が
揃ったときだけ。段丘堆積物も年代が1点で出ることがあるが、実際には期間をもって
堆積しているため（`gsj_derived.ERUPTION_WORDS`）。

`t_prop` / `b_prop` の Excel 表示形式は `0.000`（小数第3位）。

#### 出典ページ（`REF_source` と `REF_thickness`）

ZFK のAPIには**ページ番号が入っていない**。節番号（4.12）と小見出し（分布及び層厚）は
取れる。そこで `pdf_locate.py` が手元のPDFからテキストを引き出してページ索引を作り、
本文の一節と文字列照合してページを特定する。**PDFの通し番号と印刷ページ番号の両方**を出す。

★ 記号の正規化が肝。PDFは `第3‒4地点`(U+2012)・`第3. 4図`（空白入り）、ZFKは
`第3-4地点`・`第3.4図` と書き分けが違い、揃えないと1件も一致しない（実際に全滅した）。
十和田図幅の層厚17件で **16件が照合できた**。索引は `references/m{id}_pdfpages.json` に
キャッシュする（91ページで約7秒、2回目以降は0.03秒）。

#### pandas の dtype に依存しない書き方

新しい pandas は「文字列だけの列」を `object` ではなく **文字列dtype** と推論する。
そこへリストや数値を入れようとすると落ちる。

```
ValueError: setting an array element with a sequence.
```

export の Column 展開でこれが起きた。対策は2つ。

1. **セルにリストを入れない。** `df.at[idx, col] = [...]` ではなく、
   列ぶんのリストを組み立ててから `df[col] = pd.Series(..., dtype=object)` で
   列まるごと差し替える。
2. **explode の直後に `reset_index(drop=True).astype(object)`。**
   explode すると index が重複し、`df.at[i, col]` が1セルではなく複数行を
   指してしまう。あわせて object へ揃えておけば、`section_id` / `t_pos` の
   数値代入も通る。

回帰テスト `[12]` が `object` と `string` の両方で export を通している。

#### 時代名は年代に合わせて自動で直す

ZFK の時代区分は粗い（「更新世」）。`age_mapping.json` を通すと
`Early Pleistocene`(2.58–1.8 Ma) のような広い区分に落ちる。そこへ引用照合済みの
数値年代 0.4 Ma を入れると

```
b_prop = (2.58 − 0.4) / (2.58 − 1.8) = 2.79     ← 公式仕様は 0〜1
```

となり無効なデータが出る。**数値年代のほうが確かな証拠**なので、
`common.best_interval_for_age()` が時代名をそちらへ合わせる。十和田では11件が直った。

候補は **Macrostrat の国際年代層序（`timescales` に `international *` を含むもの）だけ**。
生層序帯（NN20）・古地磁気（Jaramillo）・地域階（Nukumaruan）は除く。
そのうえで **元の区分と幅が近いもの**を選ぶ。これをしないと 0 Ma に対して
Meghalayan（4.2 千年前以降）のような、資料が主張していない細かさを当ててしまう。

| 年代 | 元（ZFK由来） | 直した先 |
|---:|:---|:---|
| 0.4 Ma | Early Pleistocene | Chibanian |
| 0.99 Ma | Early Pleistocene | Calabrian |
| 0 Ma | Late Pleistocene | Holocene |
| 1.8 Ma | Pliocene | Pleistocene |
| 5.1 Ma | Late Miocene | Pliocene |

`llm` はレビューExcelの `t_int`/`b_int` を直接直す。`export` は出力時に直して報告する。

#### 「数式のキャッシュ値」を手入力と間違えない

`t_prop`/`b_prop` はレビューExcelでは数式。**Excel で開いて保存すると計算結果が
キャッシュされ、pandas はその値を返す**。数式かどうかは pandas からは見えない。

区別しないと、古い `t_int` で計算された値を「手入力だから尊重」してそのまま出す。
`export_submission._formula_rows()` が openpyxl で生の値を読み、数式の行を印付ける。

あわせて **0〜1 の範囲外の値は手入力でも尊重しない**。過去の実行が書いた
無効な残骸（`b_prop = 2.79`）がそのまま通ってしまうため。
`props_from_ages()` 側も、範囲外になる組み合わせでは値を返さない
（無効な値を自分で作らない）。

#### 年代が1点しかない層

噴火かどうかに関わらず `b_prop == t_prop` になり、仕様の `b_prop < t_prop` に反する。
そこで**どちらの場合も丸め幅を使う**。噴火かどうかは「なぜ1点なのか」の説明であって、
扱いは同じ。export の報告では

```
年代が1点 6 件（表示桁で同値になる幅を使用）
  噴火など瞬間的な堆積 5 件: Towada-Hachinohe Pyroclastic Flow Deposits, ...
  年代が片側しか分からない 1 件: Noheji Formation
    → 上下の年代が別々に分かるなら t_age_ma / b_age_ma に入れてください。
```

と分けて出す。後者は「本当は期間があるが片方しか読めていない」ので、人に見てほしい。

#### カンマで area別に分けてよい列は **ホワイトリスト**

以前は「`REF_` と `_` 以外は全部分解」にしていた。すると英文の `unit_description` が
「, 」の数がたまたま Column 数と一致したときに **真っ二つに割れた**（十和田で6行）。

```
"The Noheji Formation is a shallow marine sequence, mainly fine sand."
  → Column1: "The Noheji Formation is a shallow marine sequence"
     Column2: "mainly fine sand."
```

分解してよいのは **短い値が1つ入る欄だけ**（`common.PER_COLUMN_SPLIT_FIELDS`）。

| 分解する | 分解しない（`NEVER_SPLIT_FIELDS`） |
|:---|:---|
| min/max_thickness, t/b_age_ma, t/b_prop, t/b_int, section_id, t_pos, basal_surface, lateral_relationship, environment | unit_name, unit_description, comments, strat_name, lithology, minor_lith |

`strat_name` と `lithology` のカンマは**公式仕様上の区切り**なので触ってはいけない。

> `strat_name`: Use commas `,` to separate child and parent within a single chain.
> `lithology`: `<attribute> <lith>, <attribute> <lith>; <attribute> <lith>`

#### 公式語彙を自由記述で上書きしない

LLMの出力は実行ごとに揺れる。実際にこうなった。

```
'deep-water indet.' → 'deep marine'      公式語 → 自由記述
'shallow subtidal'  → 'shallow marine'   公式語 → 自由記述
'fluvial indet.'    → 'fluvial'          公式語 → 自由記述
```

対策は2段構え。

1. **機械的な正規化**（`common.normalize_vocab`）。綴りを公式表に合わせ、
   「X」が表に無くて「X indet.」があればそちらにする。
   `fluvial → fluvial indet.` / `Mudstone → mudstone`
   **言い換えはしない。** `deep marine → deep-water indet.` は解釈であって
   機械が決めることではない。表に無ければそのまま残す（自由記述は仕様上許容）。
2. **格下げを拒む**（`common.vocab_quality`）。語ごとに点を付け、
   平均が下がる上書きは行わず、元の値を残して報告する。

   | 点 | 意味 | 例 |
   |---:|:---|:---|
   | 2.0 | 公式表にあり、かつ具体的 | `deep-water indet.` / `shallow subtidal` |
   | 1.0 | 公式表にあるが**最上位の大分類** | `marine` / `non-marine` / `marginal marine` / `inferred marine` |
   | 0.0 | 公式表に無い（自由記述） | `deep marine` / `shallow marine` |

   「具体的か」まで見るのは、`deep-water indet.` を `marine` で上書きされたため。
   どちらも公式語なので割合だけでは差がつかないが、情報は落ちている。
   Macrostrat の environments で `type` が空なのはこの4語だけなので、
   それを大分類の目印に使っている。逆向き（大分類 → 具体、自由記述 → 公式語）は通す。

#### 地層名の対応づけは「単語の集合」で見る

`apply_llm_candidates.norm_name()` は種別語（Formation / Deposits / Terrace ...）を
落として固有名だけにする。すると **十和田段丘堆積物 → `towada`** と1語になる。

以前は部分文字列一致だったので、`towada` が
`towada caldera forming stage tephra` にも当たり、**十和田テフラ群3件が
十和田段丘堆積物の行に流れ込んで、同じ行の年代を3回上書きした**。

```
行6 Towada Terrace Deposits  t_age_ma '0' → '0.012'
行6 Towada Terrace Deposits  t_age_ma '0.012' → '0.015'
行6 Towada Terrace Deposits  b_age_ma '0.015' → '0.055'   ← 別の地層の年代
```

いまは **単語の集合が片方に含まれること＋短いほうが2語以上** を条件にしている。
1語しか残らない名前は完全一致でしか対応づけない。

#### 記載文を、より内容の薄いもので上書きしない

LLM は実行ごとに揺れ、`unit_description` にこういう文を返すことがある。

```
'The age is ca. 15 ka.'                          ← 年代は別の欄がある
'They are mainly composed of gravel and sand.'   ← どの地層の話か分からない
'K-Ar age of the lava indicates 1.8 Ma.'
```

`apply_llm_candidates._desc_score()` が

- 地層名に触れているか（＋1.0）
- 「The age is」「They are」など主語が地層でない書き出し（−0.6）
- 「composed of」「distributed」など記載らしい語（＋0.3）
- 長さ（0〜1）

で点を付け、**点が下がる上書きは行わない**。プロンプト側でも
「記載文は地層名で始めること」「年代だけは記載ではない」を明示している。

#### `unit_description` が空になる2つの理由

空欄には**別々の原因**があるので、`llm` の実行後に切り分けて報告する。

**(1) Abstract にその地層が出てこない**

十和田の「崖錐堆積物（Talus deposits）」は英文Abstractに一言も無い
（本文にも「露頭が存在しないため，崖錐堆積物の詳細は不明である」とある）。
候補が作れないので空欄のままにする。**これが正しい挙動**（捏造しない）。
`llm` は該当する地層名を一覧で出すので、`REF_desc`（本文全文）と
`REF_source`（PDFページ）を見て手で書く。

```
--- 英文Abstractに記載が無い地層 1 件 ---
    Abstractに一言も出てこないので、候補を作れません（捏造しません）。
    REF_desc（本文全文）と REF_source（PDFページ）を見て、手で書いてください。
      ・Talus deposits
```

**(2) 同名の行が複数ある（バグだった）**

十和田には「Tsukihiyama Volcanics」の行が2つある（ZFK の u018 / u019）。
`match_row()` が最初の1件しか返していなかったので、2つ目が永遠に空だった。
`match_rows()` に変えて**全部の行に入れる**ようにした。

### 4-2. `{ProjectName}_Composite_column.xlsx`（提出用・5シート）

Ichinohe完成形と**完全に同じシート構成・列順**。

| シート | 列 |
|:---|:---|
| **metadata** | key-valueレイアウト。`Documentation` 行より下は取り込み時に無視される（出典記録に使用） |
| **units** | `unit_id, col_id, section_id, position, b_int, b_prop, t_int, t_prop, unit_name, strat_name, environment, unit_description, lithology, minor_lith, min_thickness, max_thickness, basal_surface, lateral_relationship, comments, t_pos` + 仕様外の `source_unit_id, source_unit_name_ja` |
| **columns** | `col_id, col_name, col_group, ref_ids, date_collected, col_type, axis_type, b_int, t_int, b_prop, t_prop, geom, rgeom, comments` |
| **refs** | `ref_id, title, authors, publication, compilation, organization, date, doi, url, comments` |
| **images** | `col_ids, image_name, ref_id, page_no, fig_no, description, comments` |

> 公式仕様に「Extra columns will be skipped」とあるため `source_*` 列は取り込み時に無視される。出典追跡用。

---

## 5. 核心的な変換ロジック

### 5-1. ★ `sort_order` → `position` の反転（最重要）

公式仕様: *"in the case of axis-type==age this should be a sequence from oldest to youngest"*

Macrostrat の `position` は **最下位（最古）が 1**。一方 `sort_order` は人が上から書き下せるよう **最上位（最新）が 1**。**両者は逆向き**なので Column ごとに反転する。

```
position = (そのColumn内の sort_order 最大値) − sort_order + 1
```

一戸完成形 `ichinohe-west`（18層）での検証:

| 地層 | sort_order | position |
|:---|---:|---:|
| River bed deposits（最上位・完新世） | 1 | 18 |
| Shitazaki Formation | 9 | 10 |
| Kuzumaki Formation（最下位・中期ジュラ紀） | 18 | 1 |

同じ `sort_order` を複数行に付けると `position` も同値になり、公式仕様の「重なり合うユニット」を表現できる。
実装は `common.derive_positions()`。

### 5-2. ★ t_prop / b_prop の自動計算

```
prop = (interval の b_age − 地層の年代) / (interval の b_age − interval の t_age)
```

- `0` = interval の下端（古い側） / `1` = 上端（若い側）
- `b_prop` は `b_int` 内、`t_prop` は `t_int` 内の位置（**別々の interval を参照する**）
- 制約: `0 ≤ prop ≤ 1`、同一interval内なら `b_prop < t_prop`

一戸完成形の prop **59個すべて**で `prop → Ma → prop` の往復一致を確認済み。

| 地層 | interval | prop | 逆算年代 |
|:---|:---|---:|---:|
| Shitazaki 下限 | Tortonian (11.63–7.246) | 0.258 | 10.499 Ma |
| Yanagisawa 上限 | Tortonian | 0.258 | 10.499 Ma |
| Shitazaki 上限 | Tortonian | 0.714 | 8.500 Ma |

Shitazaki の下限と Yanagisawa の上限が同値なのは、両者が整合関係で境界を共有しているため。

実装は `common.compute_prop()` / `common.age_from_prop()` / `common.interval_bounds()`。

**Excelに載せる参照表は193件に絞ってある**（`common.intervals_for_excel()`）。
全1715件を載せると sharedStrings が肥大化し（50KB → 10KB に削減）、利用者にとってもノイズになる。
絞り込みの条件は「`age_mapping.json` が生成しうるもの」＋「国際年代表」。
一戸完成形15種・十和田7種のいずれも欠落ゼロを確認済み。
**Python側の `interval_bounds()` は常に全1715件を見る**ので、表に無い interval を
手入力しても export では正しく計算される。

#### ★ export 側でも同じ計算をする（重要）

**pandas は Excel の数式を評価しない。** Excelで一度も開いていないファイルはキャッシュ値も無いため、
`export_submission.resolve_props()` が Python 側で計算し直す。優先順位:

1. `t_prop`/`b_prop` に数値が直接入っている → 尊重（手動上書き）
2. `t_age_ma`/`b_age_ma` がある → interval から計算
3. どちらも無い → 空欄のまま

Excel数式と `compute_prop()` が同値であることをテストで検証している。

#### 副次効果: t_int / b_int の検算になる

prop が 0〜1 に収まらなければ、年代か interval のどちらかが誤り。export が具体的に指摘する:

```
[warn] 行12: b_prop が 0〜1 の範囲外です (2.7821) — 年代 0.41Ma がその時代の範囲外です
```

（この例では `b_int` が Early Pleistocene だが、0.41Ma は Chibanian に入る）

### 5-3. ★ area（Column）ごとの値をカンマで書く

`column_id` が `1, 2` の行では、値もカンマで分けると Column ごとに割り当てられる。

```
column_id      1, 2
min_thickness  10, 20     -> Column1 は 10m、Column2 は 20m
max_thickness  15         -> 両方 15m（値が1つなら全Columnに同じ値）
```

**安全側に倒してある。** 次の両方を満たすときだけ分解する:

- その行が複数Columnにまたがっている
- カンマ区切りの個数が Column 数とちょうど一致する

`lithology` の `gravel, sand` のようにカンマが本来の区切りである列を壊さないため。
1つのColumn内で複数の岩相を書くときは公式仕様どおり `;` を使う（`gravel; sand`）。
`lithology` `minor_lith` `environment` `strat_name` 等を分解したときは警告を出す
（`common.COMMA_AMBIGUOUS_FIELDS`）。

実装は `common.split_per_column()`。

### 5-4. ★ section_id / t_pos の自動計算

**section_id** — 公式仕様 *"Sections can also be inferred from gaps in
chronostratigraphic position fields"*。ただし**すき間を少しでも見つけたら切る、では駄目**。
地層はほぼ必ず微小なすき間を持つので全層が別sectionになる。実際 Ichinohe完成形は
42層すべて `section_id` が空。次の両方を満たすときだけ切る:

- すき間が Column 全体の年代幅の 15% を超える
- すき間が 0.5 Ma 以上ある

さらに section 数が層数の半分を超えたら「切りすぎ＝判断できていない」として何も出さない。

**t_pos** — `position` が重なっている層にだけ、上に隣接する層の position を入れる。
重なっていない層は空欄（仕様上は隣接層から推定される）。

どちらも**手入力があればそちらを優先**する。実装は `common.derive_sections()` /
`common.derive_t_pos()`。

### 5-5. 図幅名の正規化

タイトルの取得元が出版API（`十和田 (2005)`）とZFK（`十和田地域の地質`）で異なる。
`common.canonical_map_title()` が両方を `十和田 2005` に揃える。**冪等**（何度かけても同じ）。

**出版APIの `title_j` は年が括弧付き**。裸の年しか想定しないと年が二重に付き `十和田 2005 2005` になる（実際に発生した）。

---

## 6. データ取得元

### 6-1. GSJ API

| API | エンドポイント | 用途 |
|:---|:---|:---|
| 全図幅リスト | `.../publication/map/g050.json` | 地名→ID解決（`config/map_index.json` にキャッシュ） |
| 図幅メタデータ | `.../publication/map/g050/map{id}.json` | タイトル・図幅コード・DL URL |
| **ZFK図幅リスト** | `.../zfk/maps.json` | **ZFKデータのある図幅だけ**を返す（索引の起点） |
| ZFK マップ | `.../zfk/maps/m{id}.json` | 地層概要・著者・出版年（refsの元データ） |
| ZFK 地層リスト | `.../zfk/query/unitsInMap?map_id={id}` | 図幅内の全地層ID |
| ZFK 地層詳細 | `.../zfk/units/{unit_id}.json` | 個々の地層（`/unit/` にもフォールバック） |

ベースURL: `https://gbank.gsj.jp/ld/resource/`
取得済みデータは `data/raw/zfk/` にキャッシュされ、再実行時はAPIを叩かない。

#### ZFK JSON の構造（重要）

| JSONパス | 中身 |
|:---|:---|
| `legend.parent_facies.label_en` | **地層名** → `unit_name` |
| `legend.focus.label_en` | **岩相** → `REF_lith_text` |
| `legend.parent_age.label_ja` | 地質時代 |
| `legend.parent_age.lower_age_ma` / `upper_age_ma` | **年代区分の境界**（地層の年代ではない・下記参照） |
| `target.text` | 説明書本文（節見出し付きのプレーンテキスト） |
| `derived` | ZFK自身が抽出した lithology / minerals / thickness / contacts（confidence + evidence付き） |
| `map.authors[].name_en` | `"Takashi KUDO"` 形式 → `"Kudo, T."` に整形 |

`REF_unit_name_en` は `"{parent_facies} ({focus})"` で組み立てられるため、`unit_name` には `parent_facies` を直接入れる（文字列パース不要）。旧フォーマットには末尾括弧を除去するフォールバックが働く。

### 6-2. ★ ZFKから t_prop/b_prop は自動で埋められない（調査済み）

- `parent_age.lower_age_ma` / `upper_age_ma` は**年代区分そのものの境界**であって地層の年代ではない。
  同じ年代区分を持つ全ユニットが同じ値になる。さらに Macrostrat の interval 境界と小数点まで一致するため、
  計算しても prop は 0/1 にしかならない（十和田図幅の7区分中5区分で完全一致を確認）。
- 地層自身の年代は `target.text` の「時代」節に**自由文**で書かれている（`14.9～15.3ka`, `0.40Ma`, `1.79±0.21Ma`）。
  m1050 では 23層中12層（52%）にしか数値がなく、1層に矛盾する値が複数並ぶことも多い
  （八甲田第1期火砕流は 0.53〜1.28Ma の7個）。**どれを採用するかは人間の判断が必要。**

### 6-3. ★ 英文Abstract（PDF巻末）

GSJ説明書の巻末には英文Abstractがあり、**完成形の prop の出典はここだった**（図ではない）。

> the Zyūmonzi: 15–12 Ma, **the Yanagisawa: 12–10.5 Ma**, **the Shitazaki: 10.5–8.5 Ma**

一戸図幅での検証:

| 項目 | 結果 |
|:---|:---|
| 地層リスト | **30/30** が Abstract に出現 |
| 年代 | 完成形の prop と **6/6 一致** |
| 分量 | 22,720文字（5ページ・約1万トークン） |

`extract_abstract.py` が巻末側を走査し、**英字率95%以上かつ英字500字以上のページが連続する塊**を検出する。図版ページで1〜2ページ途切れても同じ塊として扱う。

| 図幅 | 検出ページ | 文字数 |
|:---|:---|---:|
| m1050 十和田 2005 | p.85–87 | 8,744 |
| m1286 一戸 2018 | p.168–171 | 22,720 |

`make` に統合済み。`references/m{id}_abstract.txt` と `abstract` シートの両方に保存される。

### 6-4. 出典の優先順位

**ZFKがあれば ZFK を優先する**（GSJが整備した一次データで信憑性が高い）。Abstract はその補完、およびZFKが無い642図幅（763中）の主戦場。

```
ZFK あり  →  ZFK を自動入力 + Abstract を REF_ 列に併記
ZFK なし  →  Abstract のみ（LLMが地層リストごと作る）
```

ただし ZFK の `derived.thickness` は実測で誤りが多いので候補扱いにする
（十和田八戸火砕流: derived `4m` に対し本文は「最大20m」）。

---

## 7. LLM連携（Gemini）

### 7-1. 構成

```
references/m{id}_abstract.txt
        │  llm_extract.run()  — Gemini Interactions API を1回
        ▼
   {unit_name, b_age_ma, t_age_ma, quote}
        │  llm_extract.verify()  — 原文照合（下記）
        ▼
   apply_llm_candidates.apply()
        ▼
   units_review の REF_age_from_abstract 列
```

- API: `https://generativelanguage.googleapis.com/v1beta/interactions`
- モデル既定: `gemini-3.6-flash`
- 認証: `x-goog-api-key` ヘッダ。キーは `config/secret.json`（または環境変数 `GEMINI_API_KEY`）
- **書き込むのは `REF_age_from_abstract` 列だけ。`t_age_ma`/`b_age_ma` には触れない。**

### 7-1b. 抽出するフィールド

| フィールド | 書き込み先 | 引用キー |
|:---|:---|:---|
| b_age_ma / t_age_ma | `REF_age_from_abstract` | `age_quote` |
| strat_name | `REF_strat_name` | `strat_quote` |
| lithology / minor_lith | `REF_lithology` / `REF_minor_lith` | `lith_quote` |
| environment | `REF_environment` | `env_quote` |
| min_thickness / max_thickness | `REF_thickness_llm` | `thickness_quote` |
| basal_surface | `REF_basal_surface` | `basal_quote` |

### 7-1c. 語彙表 `config/vocab.json`

**Macrostrat 公式APIから取得した本物の語彙表**（CC-BY 4.0）。
`scripts/build_vocab.py` が3つの定義表を取りに行って作る。`python run.py vocab` で更新。

| キー | 件数 | 取得元 |
|:---|---:|:---|
| `environment` | 83 | `/api/v2/defs/environments?all=1` |
| `lithology` | 213 | `/api/v2/defs/lithologies?all=1` |
| `lith_att` | 180 | `/api/v2/defs/lithology_attributes?all=1` |

`*_detail` に各語の class / type / group も保存してある。
語彙ブロック全体で約1,600トークン。Abstract本文より小さいので、プロンプトに丸ごと載せている。

岩相は **`<属性> <岩相>`** の形が正式（例: `siliceous mudstone` = `siliceous`(属性) + `mudstone`(岩相)）。
照合ロジック `common.check_vocab()` は末尾から最長一致で岩相を探し、残りを属性表と突き合わせる。

**★ 公式表に無い語を使ってはいけないわけではない。**
公式仕様の `environment` の説明は
「*Depositional environment interpretation; **free text** (e.g., "fluvial", "shallow marine") or Macrostrat environment*」
で、自由記述が明示的に許されている。実際、**仕様の例文にある `shallow marine` 自体が公式表に載っていない**。

一戸完成形の environment 10語を公式表と突き合わせた結果:

| 照合できた（4語） | 公式表に無い（6語） |
|:---|:---|
| `alluvial fan`, `fluvial indet.`, `lacustrine indet.`, `non-marine` | `bathyal`, `sublittoral`, `shallow marine`, `deep marine`, `shallow marine to bathyal`, `pyroclastic flow` |

つまり **既に取り込まれている完成形の6割が「公式表に無い語」** で通っている。
したがって `export` の検証は**エラーにせず、語ごとに集約した警告1件を出すだけ**にしてある。

```
[warn] environment: Macrostrat公式表に無い語が 3 種 — 'sublittoral'×12, 'bathyal'×5, 'shallow marine'×2
       （自由記述は仕様上許容。意図的ならそのままで構いません）
```

LLMには「公式表から選べ。当てはまる語が無ければ報告書の言い回しをそのまま使え。
**間違った語を無理に当てはめるな**」と指示している。

### 7-2. ★ ハルシネーション対策（`llm_extract.verify`）

LLMに**原文の逐語引用（`quote`）を必須**にし、コード側で2段階照合する。

1. `quote` が Abstract 原文に逐語で存在するか
2. 報告された数値が `quote` の中に存在するか

どちらか外れたら**そのフィールドだけを捨てる**（ユニット全体は残す）。

> **副作用:** フィールド単位で落とすので `b_age_ma` だけ通って `t_age_ma` が落ちる、
> という**片側だけの候補**が出る。表示・書き込みの両方でこれを扱えるようにしてある
> （`下限 0.4 Ma（上限不明）` のように書く）。実際にここで `TypeError` を出したことがある。
>
> **順序:** `llm` は**保存を先、表示を後**にしている。表示側の不具合で落ちたときに
> API呼び出し1回ぶんの結果を丸ごと失う事故が起きたため。表示は `try/except` で囲ってある。
年代が怪しくても lithology が確かならそれは使える。数値の捏造は構造的に起きない。

照合は**文字列ではなく数値**で行う（`number_supported`）。`0.40 Ma` と `0.4` を別物として取りこぼしたバグの対策。引用中の数値を全部拾い、Ma / ka / 年BP のいずれの単位で書かれていても数値として比較する。`0 Ma` は `present` / `recent` 等があれば認める。

検証済みの挙動:

```
[採用] Shitazaki Formation  → 10.5–8.5 Ma
[採用] Toya Formation       → 6–5 Ma（上下が逆だったので入れ替え）
[却下] Fake Formation       :: 引用が原文に無い
[却下] Shitazaki Formation  :: b_age_ma=7.7 が引用に見当たらない
```

### 7-3. ★ 課金ガード（`llm_extract.check_budget`）

**本当の保証はGoogle側にある。** 公式仕様上、Free Tier の条件は「請求先アカウントを紐付けていないこと」。
紐付けていなければ、上限超過は課金ではなく **429エラー**で止まる。

その上でこちら側にも門を置く。**`call_gemini()` は必ず `check_budget()` を通る**（バイパス経路なし）。

| 停止条件 | 既定値 | 設定 |
|:---|:---|:---|
| 有料専用モデル | flash / flash-lite / gemma **以外を拒否** | `allow_paid_tier` |
| 1回のトークン | 200,000 | `max_tokens_per_call` |
| 1日の呼び出し | 200回（無料枠250より低い） | `max_calls_per_day` |
| 1日のトークン | 1,000,000 | `max_tokens_per_day` |

設定は `config/llm_limits.json`、使用量の記録は `config/llm_usage.json`（直近30日・gitignore済み）。
**停止はAPI呼び出しの前**に起きるので、その分の消費もゼロ。

```
python run.py llm --usage
```

### 7-4. レスポンスの解析

`extract_text()` は既知の経路（`output_text` → `steps/model_output` → 旧`candidates`）を順に試し、
最後の保険として**JSONを再帰的に歩いて `text` を全部集める**。`thought`/`thinking` は本文ではないので除外。
API形式が変わっても取りこぼさないための設計（実際に空応答バグが起きた）。

本文が取れなかった場合は `describe()` がレスポンスの構造を人が読める形で自動表示する。

---

## 8. ZFK図幅の索引と検索

`config/zfk_index.json`（`run.py index` で生成）。GSJの5万分の1図幅は全763枚あるが、ZFKが整備されているのは一部だけ。`zfk/maps.json` がZFKを持つ図幅だけを返すので、これを起点に索引を作る。

各行: `map_id, title_ja, title_en, sheet_code, region_code, region_folder, pub_year, n_units, lat, lng, authors`

`common.resolve_region()` が `aomori` / `Aomori` / `青森` / `青森県` / `05` / `5` / `05_青森` のいずれからも地域コードを引く（別名は `REGION_ALIASES`）。地域として解決できなければ、図幅名（和英）・図幅ID・図幅コード・著者名の部分一致で探す。

> **注意:** ここでいう「地域」は **GSJの図幅区画であって都道府県境ではない**。
> 例えば `05_青森` には岩手県北部の図幅（一戸など）も含まれる。

着手状況（未着手／作業中／提出済み）は索引ではなく `data/02_review` / `data/03_submission` の実ファイルから毎回判定するので、索引を作り直さなくても最新。

### 地域コードマッピング

図幅コード（例 `05048`）の先頭2桁。定義は `common.REGION_MAP`。

| コード | フォルダ | コード | フォルダ |
|:---|:---|:---|:---|
| 01 | 01_宗谷 | 11 | 11_中部 |
| 02 | 02_網走 | 12 | 12_関西 |
| 03 | 03_根室 | 13 | 13_中国東部 |
| 04 | 04_札幌 | 14 | 14_中国西部 |
| 05 | 05_青森 | 15 | 15_四国 |
| 06 | 06_秋田 | 16 | 16_九州北部 |
| 07 | 07_岩手 | 17 | 17_九州中部 |
| 08 | 08_宮城・山形 | 18 | 18_九州南部 |
| 09 | 09_福島・新潟 | 19 | 19_南西諸島 |
| 10 | 10_関東 | | |

図幅コードは `G50_05_031`（ZFK）と `05031`（出版API）の2形式があり、`common.normalize_sheet_code()` が5桁に正規化する。

---

## 9. 検証（`export_submission.validate`）

`export` / `check` 時に自動実行。

**エラー（出力を中止）**

- `units.column_id` が `columns_review.col_id` と一致しない
- `columns_review` に有効な `col_id` がない
- Columnに `geom` も `lat`/`lng` もない

**警告（表示して続行）**

- `unit_name` が未入力
- `t_int` と `b_int` が**両方とも**未入力（片方あればよい）
- `sort_order` の未入力・欠番・重複
- `b_prop`/`t_prop` が 0〜1 の範囲外（年代も併記して指摘）
- 同一interval内で `b_prop >= t_prop`
- `b_age_ma < t_age_ma`（上下が逆）
- `refs.title` が空 / `project_name`・`compiler_name` が空

**お知らせ（警告ですらない）**

`lithology` `t_age_ma` `b_age_ma` `min_thickness` `max_thickness` `environment` の未入力件数。
**公式仕様上これらは空欄でも提出できる。** 埋まらないことを問題として扱わない。

---

## 10. 開発上の注意事項（ハマりどころ）

すべて**実際に発生した**問題。

- **実装の複製を作らない。** かつて `claude_work/scripts/` と `scripts/` が二重管理になり、
  古い `scripts/run.py` が残って混乱した。現在は `scripts/` のみ。CLIはルートの `run.py` のみ。

- **列定義は `common.py` に集約。** 他ファイルにハードコードしない。

- **`run.py` はコマンド振り分けだけを担う。** 各コマンドは `cmd_*()` 関数にまとめ、
  `COMMANDS` 辞書で振り分ける。処理本体は `scripts/` 側に置く。
  引数の解釈は `split_args()` に集約（フラグと値付きオプションを分離する）。

- **`sort_order` と `position` は逆向き。** 5-1参照。

- **pandas は Excel の数式を評価しない。** prop は Python 側でも計算し直す。5-2参照。

- **フリーズペインは見出し行だけ（`A2`）。列は固定しない。**
  `G2`（A〜F固定）にすると幅の広い `REF_*` 列が計157文字 ≒ 1100px の固定領域となり、
  **G以降の列へスクロールできなくなる**。`B2`（A列だけ固定）でも「なぜかA列だけ動かない」と混乱の元になる。

- **`repair` が直すのは「列を固定している場合」だけ。** `A2`/`A24` のような行だけの固定は
  利用者が意図して設定していることがあるので書き換えない（`repair_layout.freezes_columns()`）。

- **ターミナル表示で `f"{s:<20}"` を使わない。** 文字数で数えるため日本語（全角）で桁がずれる。
  `common.pad()` / `common.disp_width()` を使う。

- **図幅フォルダ名は通信状況で変わりうる。** 5-3参照。**フォルダ名で同一性を判断してはいけない。**
  `make` は `data/02_review/**/m{id}_*` にマッチするフォルダがあれば名前に関わらず再利用する。
  既存フォルダの探索は `os.makedirs()` より**前**に行う（後にすると使われない空フォルダが残る）。

- **`config/map_index.json` のスキーマは1種類ではない。**
  `{"map_id","name_en"}` と `{"@id","label"}` が混在しうる。`run._normalize_entries()` が吸収する。
  決め打ちすると**地名での図幅指定が全て無言で失敗する**（実際に発生した）。

- **★ `openpyxl` の `insert_cols` は数式の参照を書き換えない。**
  途中に列を挿すと `t_prop`/`b_prop` の VLOOKUP が別の列を指すようになり、**黙って壊れる**。
  列構成を変えるときは「値を読み出す → 並べ直す → `make_review_sheet.write_prop_formulas()` で
  数式を作り直す」こと（`repair_layout.migrate()` がこの方式）。
  `apply_llm_candidates` は列が足りなければ**末尾に追加**するだけにしてある。

- **古いレビューファイルは `repair --migrate` で引き上げる。**
  列が増えるたびに作り直していると入力済みの値を失う。migrate は値を保ったまま
  列順を現行に揃え、足りないシート（intervals / abstract）を足し、数式を再生成する。
  実行前に必ず `.bak_日時.xlsx` を作る。

- **`openpyxl` の PermissionError**: 対象Excelを開いたまま実行するとエラー。閉じてから再実行。
  一時ファイル `~$*.xlsx` はファイルが開かれたままのサイン。

- **旧フォーマットへの後方互換**: `unit name`（半角スペース）、`env`、`min_thick`、`notes` は
  自動で新しい列名にマッピングされる。

- **秘密情報**: `config/secret.json` と `config/llm_usage.json` は `.gitignore` 済み。
  ファイル名は `secret.json` / `secrets.json` のどちらでもよい（`common.load_secret()`）。

- **`八戸/` フォルダは手動作業の成果物** → 変更・削除しないこと。

---

## 11. テスト

```
python claude_work/tests/test_roundtrip.py     # 440 PASS / 0 FAIL
python claude_work/tests/demo_prop.py          # prop計算のデモ
```

| 節 | 内容 |
|:---|:---|
| [1] | 純粋関数（地層名の括弧除去・position反転・著者整形・図幅名正規化） |
| [2] | **ラウンドトリップ**: 一戸完成形 → レビュー形式へ逆変換 → export → 完成形と突合 |
| [3] | 旧フォーマットの後方互換（合成データ・ユーザーファイルに依存しない） |
| [4] | 出力先が地域フォルダを維持するか |
| [5] | レビューExcelの表示レイアウト（横スクロール不能の再発防止） |
| [6] | ZFK図幅の検索（地域解決・表示幅） |
| [7] | **年代 → prop の計算**（完成形の prop 59個を往復検証） |
| [8] | **LLM候補の検証**（捏造の却下・数値照合） |

フォーマットを変更したら必ずこのテストを通すこと。

---

## 12. 未対応・今後の課題

`docs/ROADMAP.md` に詳細。要点のみ:

- `min_thickness` / `max_thickness` は Abstract に地層ごとには載っていない（本文側）。空欄スタート。
- `section_id` / `t_pos` の自動判定（年代ギャップから）は未実装。公式仕様に
  *"Sections can also be inferred from gaps in chronostratigraphic position fields"* とある。
- `lithology` / `environment` 等への LLM 適用は未着手。
- `age_mapping.json` は時代名→interval のみ。年代の新旧関係の自動検証は未実装。
- `facies` シートと column-linked data sheets は未対応（公式仕様では任意）。
- Abstract の年代は丸めた値（`12–10.5 Ma`）。本文にはより細かい値（`NPD5D 10.0–9.2 Ma`）もある。
  完成形が丸めた値を採用しているので実用上は問題ない。

---

## 13. 全国50k管理・Shapefile経路

### 13-1. 全国インベントリ

```
python run.py inventory                         # ローカルだけ。API/LLM使用なし
python run.py inventory --refresh --workers 8  # 未取得のGSJ出版物APIだけ取得
```

出力:

- `data/00_management/gsj_50k_inventory.json` — 自動処理の正本
- `data/00_management/gsj_50k_inventory.csv` — Excel・解析用の平坦表
- `data/00_management/GSJ_50k_全国管理表.xlsx` — 人が見るダッシュボード・管理表

出版物APIのレスポンスは `data/raw/publication/g050/m{id}.json` に図幅単位で保存する。
そのため763図幅の途中で止まっても、次回は未取得分だけ再開できる。全国管理表の右端
`優先度(手動) / 担当 / 手動状態 / メモ` は再生成時に `map_id` で引き継ぐ。

2026-08-08に全763図幅を照会した時点の内訳:

| 経路 | 図幅数 |
|:---|---:|
| ZFK + Shape + PDF | 121 |
| Shape + PDF | 280 |
| Shapeのみ | 2 |
| PDFのみ | 254 |
| GSJ Viewer/凡例画像のみ | 106 |

### 13-2. フィールド別の情報源優先順位

図幅全体を単純に `ZFK > Shape > PDF` とするのではなく、フィールド適性で選ぶ。

| フィールド | 第一候補 | 第二候補 | 第三候補 |
|:---|:---|:---|:---|
| 地層名・凡例年代・本文 | ZFK | `geo_A.dbf` | PDF/Viewer画像 |
| 正確な地理形状・代表位置 | Shapefile | ZFK代表GeoJSON | 図版 |
| 層厚・環境・基底関係・詳細記載 | 構造化本文の規則抽出 | PDF | LLM候補+引用照合 |
| 層序順 | 凡例順/`major_code` | PDF凡例 | 人の確認 |

確信度は A（GSJネイティブ構造化値）、B（決定的規則抽出）、C（LLM/OCR候補+原文照合）、
D（推定）に分ける。A/Bは検証通過時に自動採用、Cは引用・ページ・数値の照合後に採用、
Dは自動採用しない。競合だけを例外キューへ送る。

### 13-3. `geo_A.dbf` の処理

`scripts/shape_source.py` は外部GISライブラリなしでDBFを読む。GSJ配布物では次を使用する。

| DBF列 | 用途 |
|:---|:---|
| `MAJOR_CODE` | ZFK `self.major_code` との決定的な結合キー |
| `LEGEND01/01E` | era/system（和英） |
| `LEGEND02/02E` | age/epoch（和英） |
| `LEGEND03/03E` | 地層・ユニット名（和英） |
| `LEGEND04/04E` | 岩相（和英） |
| `SYMBOL` | 凡例記号 |

ZFKがある図幅はZFK値を採用し、shapeを自動検算に使う。ZFKがない図幅はshape から
`units_review` を起こす。Shapefileヘッダのbbox中心はZFK座標がないときのColumn初期座標に使う。
水域など、年代・地層名を持たない作業コードは地層ユニットから除外する。

十和田では `MAJOR_CODE=1..23` の23件がZFKと全件一致し、コード88の水域1件を除外した。

```
python run.py shape 1050
python claude_work/tests/test_shape_source.py
```

新規レビューExcelには `REF_shape_*`, `REF_confidence_class`, `REF_conflict` と、セル単位の候補を
長形式で持つ `source_evidence` シートが追加される。`REF_conflict` が空でない行だけ人が確認する。

### 13-4. 50kと200kの境界

刊行済み50k図幅のセル欠損を200kで埋めない。PDFがない106図幅にもGSJ Viewerの図幅・凡例画像が
あるため、先に50k画像のOCR/Vision経路を使う。200kは、50kが刊行されていない地理的空白だけに
使い、`source_scale=1:200,000` と `coverage_tier` を保持して50k由来データと区別する。

### 13-5. Codex/LLM usage方針

全国バッチ、shape読取り、索引更新、競合検出はローカルPythonで行う。Codexはコード変更・QA・
例外調査に限定し、図幅1枚ごとの通常実行には使わない。LLM/VisionはPDFのみ254図幅と
Viewer画像のみ106図幅の不足項目に限定し、ZFK/shapeが埋められるセルには呼ばない。
