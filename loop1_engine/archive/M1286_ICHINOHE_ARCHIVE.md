# Loop 1: m1286 一戸図幅 抽出品質・Vision系 統合開発記録 (M1286_ICHINOHE_ARCHIVE.md)

本ファイルは、一戸図幅（m1286）の黄金正解データ（GOLD）照合、年代誤伝播修正、柱状図画像認識（Vision）検証レポートを1つに統合した恒久記録である。


---

## [unit_id重複と年代誤伝播_修正提案_20260811.md]

# unit_id 重複と年代の誤伝播 修正提案書

**作成日**: 2026-08-11
**対象コンポーネント**: `scripts/pdf_unit_bootstrap.py`, `scripts/age_resolution.py`, 提出前チェック
**目的**: 1つのID採番バグが15ユニットの年代を500万年以上ずらしている連鎖を断ち切り、Macrostrat へ投入可能な状態に戻す
**発見経緯**: `claude_work/scripts/compare_units.py` で `Ichinohe_reference_GOLD.xlsx` と `m1286_review.xlsx` を突き合わせて発覚
**重要度**: **最高。** 投入を止めるべき欠陥であり、モデル変更や無料枠の検討より優先する
**状態**: **適用済み（2026-08-11）。** 詳細は末尾「9. 適用記録」

---

## 1. 現象

### 1-1. unit_id が4件重複している

m1286 の出力は48行だが `unit_id` はユニーク44件。

| unit_id | sort 2〜4 の行 | sort 17〜20 の行 |
| :--- | :--- | :--- |
| `m1286_p019` | landslide deposits | **Kamimetoki Sandstone Member** |
| `m1286_p020` | flood-plain and valley-floor deposits | **Toya Formation** |
| `m1286_p021` | colluvial and alluvial cone deposits | **Esashika Formation** |
| `m1286_p022` | river-bed deposits | **Nanashigure Volcanic Fan Deposits** |

Macrostrat の形式では `unit_id` は一意でなければならない。このままでは投入できない。

### 1-2. 第四紀の地層に中新世末〜鮮新世の年代が入っている

`b_int = Messinian` / `t_int = Zanclean`（約7.25〜3.6 Ma）が17ユニットに付与され、**うち15件は `b_age_ma` も `t_age_ma` も空**。対象には以下が含まれる。

- Horino / Ibonai / Maisawa / Rendaino / Hayawatari / Kusagi / Asanai / Mukaikawara 各段丘堆積物
- 十和田八戸火砕流堆積物、十和田大不動火砕流堆積物
- river-bed deposits、colluvial and alluvial cone deposits

GOLD ではいずれも **Holocene 〜 Late Pleistocene**（0〜0.13 Ma 程度）。**500万年以上ずれている。**

---

## 2. 原因

**2つは独立した不具合ではない。1-1 が 1-2 を引き起こしている。**

### 2-1. 直接原因: `_stable_ids_from_cache` が同じIDを別の地層に割り当てる

`scripts/pdf_unit_bootstrap.py:236-243`

```python
for _priority, _path, document in sorted(documents, key=...):
    for index, candidate in enumerate(document.get("candidates") or [], start=1):
        key = _inventory_name_key(candidate.get("unit_name"))
        if key:
            result.setdefault(key, f"m{map_id}_p{index:03d}")
```

この関数は「過去に発行済みのIDを保つ」ために、キャッシュ済みの `pboot_*.json` を全部読んで
「地層名 → `p{候補の並び順}`」の対応表を作る。

**`setdefault` は同じ *キー* の上書きは防ぐが、異なるキーが同じ *値* を取ることは防がない。**

m1286 には `pboot_*.json` が4件あり、候補の並びが食い違っていた。

| キャッシュ | model | prompt_version | 候補数 | index019 | index020 |
| :--- | :--- | :--- | ---: | :--- | :--- |
| `pboot_10f926…` | gemini-3.6-flash | v1 | 27 | Kamimetoki Sandstone Member | **Toya Formation** |
| `pboot_805a22…` | gemini-3.6-flash | v1 | 27 | Kamimetoki Sandstone Member | **Toya Formation** |
| `pboot_573478…` | gemini-3.5-flash | v2 | 22 | Landslide deposits | **Floodplain and valley-floor deposits** |
| `pboot_ec362e…` | gemini-3.6-flash | v2 | 48 | Shitazaki Formation | Kamimetoki Sandstone Member |

結果として、

- `"toya formation"` → `m1286_p020`
- `"floodplain and valley floor deposits"` → `m1286_p020`

の両方が対応表に残る。**キーは違うが値が同じ。**

呼び出し側の `_evidence_rows`（`pdf_unit_bootstrap.py:341-359`）も止められない。

```python
used_ids = set(stable_ids.values())   # ← 集合にした時点で重複が消えて見えなくなる
...
unit_id = stable_ids.get(key)
if not unit_id:                        # ← 既にIDがある場合は無検査で採用
    while f"m{map_id}_p{next_ordinal:03d}" in used_ids:
        ...
```

新規採番のときだけ衝突を避けており、**対応表が既に壊れている場合は素通しする**。

### 2-2. 連鎖: 重複した行が「自分自身で挟む」ブラケットになる

`scripts/age_resolution.py` の `infer_interval_pairs` は、年代未定のユニットについて
**同じ Column 内で直上・直下の年代付きユニットを探し、両者の区間が一致していれば**その値を補完する。
docstring はこれを "conservative" と説明している。

`scripts/age_resolution.py:82-92`

```python
lower = [item for item in placements.get(column, []) if item[0] < target_sort and _pair(units[item[1]])]
upper = [item for item in placements.get(column, []) if item[0] > target_sort and _pair(units[item[1]])]
if not lower or not upper:
    eligible = False
    break
lower_pair = _pair(units[max(lower, key=lambda item: item[0])[1]])
upper_pair = _pair(units[min(upper, key=lambda item: item[0])[1]])
if lower_pair is None or upper_pair is None or not _same_pair(lower_pair, upper_pair):
    eligible = False
    break
```

**`lower` と `upper` が同一ユニットでないことを確かめていない。**

実際に起きたことを `compiled.json` の sort_order 順に並べると明白になる。

```
sort  unit_id       地層名                              b_int        t_int      b_age  推論
   1  m1286_p019    landslide deposits                  None         None       None
   2  m1286_p020    flood-plain and valley-floor depo…  Messinian    Zanclean   6.0    ←種
   3  m1286_p021    colluvial and alluvial cone depos…  Messinian    Zanclean   None   INFERRED
   4  m1286_p022    river-bed deposits                  Messinian    Zanclean   None   INFERRED
   5  m1286_p028    Horino terrace deposits             Messinian    Zanclean   None   INFERRED
   …                （段丘・火砕流が並ぶ）                                               INFERRED
  15  m1286_p027    Oritsumedake fan deposits           Messinian    Zanclean   None   INFERRED
  16  m1286_p022    Nanashigure Volcanic Fan Deposits   Messinian    Zanclean   None   INFERRED
  17  m1286_p021    Esashika Formation                  Messinian    Zanclean   None   INFERRED
  18  m1286_p020    Toya Formation                      Messinian    Zanclean   6.0    ←種
  19  m1286_p018    Shitazaki Formation                 Tortonian    Tortonian  10.5
```

`m1286_p020` が **sort 2 と sort 18 の両方に存在する**。この2行は同じIDなので当然まったく同じ区間を持つ。

したがって `_same_pair(lower_pair, upper_pair)` は**自明に成立する**。
「上下の年代が一致しているから安全」という保護が、**同一ユニットを上下と誤認したために完全に無効化された。**

その間（sort 3〜17）に挟まれた15ユニットすべてに Messinian/Zanclean が流し込まれた。

### 2-3. 種になった 5〜6 Ma はどこから来たか

sort 2 の行は名前が "flood-plain and valley-floor deposits" だが `b_age_ma = 6.0` を持つ。
証拠は `[C | PDF | English Abstract] b_age_ma: 6`。

これは本来 **Toya Formation** に付くべき年代である。2-1 のID衝突により両者が同一行として扱われ、
Toya層の年代が氾濫原堆積物の行へ乗った。**ここが連鎖の起点。**

### 2-4. 一意性チェックが存在しない

`export_submission.py` / `compiled_layer.py` / `review_v2.py` を調べたが、
**`unit_id` の重複を検出する処理はどこにも無い。**

`submission_check` も気づいていない。出力された警告は次の1件だけだった。

```
[warn] t_int と b_int が両方とも未入力の行が 17 件あります
```

**逆に、誤った値が入っている17件は警告されない。**空欄は警告されるが、誤りは通る。

---

## 3. 修正仕様（Codex実装用）

### 変更 1: `scripts/pdf_unit_bootstrap.py` — ID対応表を単射にする

`_stable_ids_from_cache` を差し替える。異なる地層名に同じIDを割り当てない。
衝突した側はIDを持たせず、`_evidence_rows` に新規採番させる。

```python
    result: dict[str, str] = {}
    assigned: set[str] = set()
    for _priority, _path, document in sorted(documents, key=lambda item: (item[0], str(item[1]))):
        for index, candidate in enumerate(document.get("candidates") or [], start=1):
            if not isinstance(candidate, Mapping):
                continue
            key = _inventory_name_key(candidate.get("unit_name"))
            if not key or key in result:
                continue
            unit_id = f"m{map_id}_p{index:03d}"
            if unit_id in assigned:
                # 同じIDを別の地層が既に取っている。キャッシュ間で候補の並びが
                # 食い違うと起きる。ここで割り当てると unit_id が重複するので、
                # この地層にはIDを与えず、呼び出し側に新しい番号を採らせる。
                continue
            result[key] = unit_id
            assigned.add(unit_id)
    return result
```

### 変更 2: `scripts/pdf_unit_bootstrap.py` — `_evidence_rows` で二重に防ぐ

`_evidence_rows` の冒頭で、渡された対応表の値が一意であることを確認する。
古いキャッシュや外部から壊れた表が来ても、重複IDを出力しない。

```python
    stable_ids = dict(stable_ids or {})
    # 値の重複を落とす。先に現れたものを残す。
    seen_ids: set[str] = set()
    for key in list(stable_ids):
        if stable_ids[key] in seen_ids:
            del stable_ids[key]
        else:
            seen_ids.add(stable_ids[key])
    used_ids = set(stable_ids.values())
```

### 変更 3: `scripts/age_resolution.py` — ブラケットが同一ユニットでないことを確かめる

`infer_interval_pairs` の該当箇所に、上下が別のユニットであることの確認を入れる。

```python
            lower_index = max(lower, key=lambda item: item[0])[1]
            upper_index = min(upper, key=lambda item: item[0])[1]

            # 上下が同じユニットなら「両者が一致する」は自明に成立してしまい、
            # 一致確認が保護として機能しない。unit_id が重複している場合に起きる。
            if lower_index == upper_index:
                eligible = False
                break
            if str(units[lower_index].get("unit_id") or "") == str(units[upper_index].get("unit_id") or ""):
                eligible = False
                break

            lower_pair = _pair(units[lower_index])
            upper_pair = _pair(units[upper_index])
            if lower_pair is None or upper_pair is None or not _same_pair(lower_pair, upper_pair):
                eligible = False
                break
            column_candidates.append(lower_pair)
```

### 変更 4: 提出前チェックに `unit_id` の一意性検査を足す

重複を **warn ではなく error** として報告する。Macrostrat の形式要件だから。

```
[error] unit_id が重複しています（4件）:
  m1286_p019: landslide deposits / Kamimetoki Sandstone Member
  m1286_p020: flood-plain and valley-floor deposits / Toya Formation
  ...
```

### 変更 5（要判断）: 推論値を値の列に書くかどうか

`age_resolution` が補完した値は、証拠層では正しく `[C|Derived|INFERRED]`、
状態は CHECK として記録されている。**出所の記録は壊れていない。**

しかし値そのものは `b_int` / `t_int` の列に書き込まれる。
プロジェクト規則「推測で値を埋めない。不明値は空欄にする」「事実と解釈を明確に分ける」に照らすと、
**推論値を値の列に入れてよいかは設計判断**であり、本提案書では変更しない。

選択肢:

- (a) 現状維持。CHECK 状態と証拠で区別されているので許容する
- (b) 推論値は `REF_` 系の参考列にだけ出し、`b_int`/`t_int` は空欄のままにする
- (c) 推論の適用範囲を狭める（例: 上下のユニットが同じ紀・世に属する場合のみ）

なお変更3を入れれば、今回の15件は自動的に補完されなくなる（上下が同一ユニットでなくなるため）。
(b)(c) は、それでもなお残る誤補完への対策として検討する位置づけ。

---

## 4. 期待される効果

| | 修正前 | 修正後 |
| :--- | :--- | :--- |
| `unit_id` の重複 | 4件 | 0件 |
| Toya / Esashika / Nanashigure / Kamimetoki の記載文 | 別の地層のものが入っている | 自分のものになる |
| Messinian/Zanclean の誤補完 | 15件 | 0件（変更3により上下が同一ユニットの場合は補完しない） |
| 提出前チェック | 重複を検出しない | error として止める |

`compare_units.py` の不一致48件のうち、b_int/t_int 由来の30件が解消する見込み。

---

## 5. 検証手順

### 5-1. 単体テスト（API不要）

```powershell
python claude_work/tests/test_pdf_unit_bootstrap.py
python claude_work/tests/test_age_resolution.py
python claude_work/tests/test_roundtrip.py
```

**追加すべきテスト**:

1. `_stable_ids_from_cache`: 候補の並びが食い違う複数キャッシュを与え、**返り値の値がすべて一意**であること
2. `_evidence_rows`: 重複値を含む `stable_ids` を渡しても、出力行の `unit_id` が一意であること
3. `infer_interval_pairs`: 同じ `unit_id` が上下に現れる compiled を与え、**補完されない**こと
4. 提出前チェック: 重複 `unit_id` を含む入力で error になること

### 5-2. 実データでの再生成（API枠が必要）

**注意**: 変更後は `prompt_version` を上げないこと。上げるとキャッシュが全失効し、
無料枠（現在20回/日）を再消費する。今回の修正はプロンプトを変えないため、
**キャッシュを再利用したまま再構築できるはず**。

```powershell
python run.py ichinohe
```

確認項目:

- `unit_id` の重複が 0 件
- Toya Formation の `unit_description` が氾濫原堆積物のものでないこと
- 段丘堆積物・火砕流堆積物の `b_int`/`t_int` が空欄に戻っていること
- `submission_check` に unit_id 関連の error が出ないこと

### 5-3. GOLD との再比較

```powershell
python claude_work/scripts/compare_units.py `
  "claude_work/reports/Ichinohe_reference_GOLD.xlsx" `
  "data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx" `
  --out claude_work/reports/比較_修正後.md
```

修正前の値（基準）:

```
対応 30 地層 / GOLD のみ 12 / 出力のみ 18
一致 47 / 不一致 48 / 捏造 1 / 取りこぼし 108
```

**b_int / t_int の不一致 30 件が減ることを確認する。**

---

## 6. 既存キャッシュの扱い

壊れたIDの出所は `llm_cache/pboot_*.json` 4件の並びの食い違いである。
変更1を入れれば新たな重複は起きないが、**どのキャッシュが優先されるかで採番が変わる**点に注意。

- キャッシュを消せば採番はきれいになるが、**5回分の無料枠を再消費**する（1日20回のうち）
- 消さない場合、変更1は「先に現れたものを優先し、衝突した側は新規採番」という挙動になる

`pboot_ec362e…`（48候補・v2・3.6-flash）が最新かつ最も網羅的なので、
**それ以外の3件を退避してから再実行する**のが、枠の消費を抑えつつ採番を整える現実的な方法だと考える。
ただしこれは提案であり、既存ファイルの削除は行っていない。

---

## 7. 信頼度

| 項目 | 信頼度 | 根拠 |
| :--- | :--- | :--- |
| `unit_id` が4件重複 | **high** | `compiled.json` と xlsx を直接集計 |
| キャッシュ4件の index019-022 の食い違い | **high** | `pboot_*.json` を直接読み出し |
| `setdefault` が値の重複を防がないこと | **high** | コードの構造から自明 |
| `m1286_p020` が sort 2 と 18 に存在すること | **high** | `compiled.json` を sort_order 順に出力して確認 |
| sort 3〜17 の15件が INFERRED であること | **high** | `age_evidence.summary` に `[C|Derived|INFERRED]` を確認 |
| 自己ブラケットが誤補完の原因であること | **high** | 上記2点と `infer_interval_pairs` の実装から論理的に一意に定まる |
| 5〜6 Ma が Toya 層由来であること | **medium** | 証拠は `English Abstract` 由来と記録されているが、Abstract 原文との突き合わせは未実施 |
| 修正後に30件の不一致が減るという見積もり | **medium** | 未実行。再生成して確認が必要 |

---

## 9. 適用記録（2026-08-11）

**バックアップ**: `claude_work/backup/20260811/` に3ファイルの原本を退避（md5一致を確認済み）
**テスト**: `claude_work/tests/test_unit_id_uniqueness.py`（新規・17 PASS / 0 FAIL）、既存33ファイル全通過

### 適用した変更

| # | ファイル | 内容 |
| :--- | :--- | :--- |
| 1 | `scripts/pdf_unit_bootstrap.py` | `_prior_stable_ids` を単射化。既に使われた番号は別の地層に渡さない |
| 2 | `scripts/pdf_unit_bootstrap.py` | `_evidence_rows` で対応表の値の重複を落としてから `used_ids` を作る |
| 3 | `scripts/age_resolution.py` | 上下のブラケットが同一ユニット／同一 `unit_id` なら補完しない |
| 4 | `scripts/export_submission.py` | `validate()` に整合性チェックを追加（下記の訂正あり） |

関数名は提案書執筆時の `_stable_ids_from_cache` ではなく **`_prior_stable_ids`** が正しかった。

### 変更4の仕様を実装中に訂正した

提案書では「`unit_id` は一意でなければならない」として重複を一律エラーにする仕様を書いた。
**これは誤りだった。**実装して `test_roundtrip` を回したところ 440 PASS → 415 PASS / 4 FAIL に退行した。

原因は、**1つの地層が複数の Column にまたがるのは正常**だから。
`m1050_u002`（十和田八戸火砕流堆積物）は `column_id = "1, 2"` を持ち、
出力段で Column ごとの2行に展開される。同じ `unit_id` が2行に現れるが、これは正しい。
識別子は `unit_id` 単独ではなく **`(unit_id, column_id)`** の組である
（`compiled.json` の `row_key` が `m1286_p028::unsplit` 形式であることとも整合する）。

そこで検査を2つに分けた。

1. **同じ `unit_id` に別の地層名が付いている** → エラー。別々の地層が1つのIDを共有しており、記載文や年代が入れ替わる。m1286 の不具合はこちら
2. **同じ `(unit_id, column_id)` の組が2行以上ある** → エラー。同一 Column 内の重複行

この訂正後、`test_roundtrip` は 440 PASS / 0 FAIL に戻った。

### 実データでの効果（API消費なし）

現行の `compiled.json` から、INFERRED と記録されている21ユニットの区間を空欄に戻して
補完前の状態を復元し、修正前後の挙動を比較した。

```
補完前の状態を復元: 21 ユニットの区間を空欄に戻した

修正前: 21 件を補完   ← すべて Zanclean / Messinian
    m1286_p021  colluvial and alluvial cone deposits
    m1286_p022  river-bed deposits
    m1286_p028  Horino terrace deposits
    m1286_p026  Ibonai terrace deposits
    m1286_p029  Maisawa terrace deposits
    ... 計 21 件

修正後: 6 件を補完   ← すべて Burdigalian / Burdigalian
    m1286_p039  Tate Sandstone Member
    m1286_p040  Sikonai Siltstone Member
    m1286_p041  Matsukura Siliciclastic Rock Member
    m1286_p042  Koiwai Mudstone Member
    m1286_p043  Keiseitoge Volcanic Rock Member
```

**誤補完15件が消え、正当な補完6件は残った。**
残った6件はいずれも部層（Member）で、同一層群内の Burdigalian の層に上下を挟まれており、
補完として妥当な形になっている。

### まだ残っていること

- **`m1286_review.xlsx` 自体はまだ直っていない。**パイプラインを再実行するまで既存の出力は古いまま。
  再実行には無料枠が要る（リセットは太平洋時間の深夜＝日本時間の夕方ごろ）
- 再実行時は `prompt_version` を上げないこと。上げるとキャッシュが全失効し枠を再消費する
- 変更5（推論値を値の列に書くかどうか）は**未着手**。設計判断のため保留

### 再実行後に確認すること

```powershell
python run.py ichinohe

python claude_work/scripts/compare_units.py `
  "claude_work/reports/Ichinohe_reference_GOLD.xlsx" `
  "data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx" `
  --out claude_work/reports/比較_修正後.md
```

修正前の基準値: 一致 47 / 不一致 48 / 捏造 1 / 取りこぼし 108。
**b_int / t_int の不一致30件が減ることを確認する。**


---

## [m1286_データ不整合_20260811.md]

# m1286 出力に見つかったデータ不整合

**発見日**: 2026-08-11
**発見の経緯**: モデル変更の可否を判断するため `claude_work/scripts/compare_units.py` を作り、`Ichinohe_reference_GOLD.xlsx` と `m1286_review.xlsx` を突き合わせたところ判明した
**対象**: `data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx`（Review シート・48行）
**重要度**: **高。** Macrostrat への投入を止めるべき不整合を含む

---

## 要点

モデルや無料枠の問題より先に直すべき欠陥が2つある。どちらも**プロジェクト規則「推測で値を埋めない」「事実と解釈を明確に分ける」に正面から反する**。

1. **unit_id が重複し、4つの地層に別の地層の情報が付いている**
2. **年代区間が、年代値の根拠なしに15ユニットへ一律付与されている**

---

## 不整合 1: unit_id の重複と、情報の取り違え

`unit_id` は48行に対してユニーク44件。**4件が重複**している。

| unit_id | 行A（本来の地層） | 行B（誤って同じIDになった地層） |
| :--- | :--- | :--- |
| `m1286_p019` | landslide deposits | **Kamimetoki Sandstone Member** |
| `m1286_p020` | flood-plain and valley-floor deposits | **Toya Formation** |
| `m1286_p021` | colluvial and alluvial cone deposits | **Esashika Formation** |
| `m1286_p022` | river-bed deposits | **Nanashigure Volcanic Fan Deposits** |

行Bの4つは実在する地層だが、**行Aの記載文・年代区間をそのまま引き継いでいる**。

実例（`Toya Formation`）:

```
unit_id          = m1286_p020          ← flood-plain deposits と同じID
unit_name        = Toya Formation
b_int / t_int    = Messinian / Zanclean
unit_description = "Flood-plain and valley-floor deposits occur in the
                    district as young and minor deposits."   ← 別の地層の記載
```

Toya層は本来「凝灰岩・泥岩・砂岩・礫岩・亜炭からなり、ジュラ系を不整合に覆う」地層で（GOLD の記載）、氾濫原堆積物ではない。

同様に `Nanashigure Volcanic Fan Deposits` には "River-bed deposits occur in..."、`Esashika Formation` には "Colluvial and alluvial cone deposits occur in..." が入っている。**4件とも、直前の表層堆積物の記載が丸ごと転写されている。**

### なぜ問題か

- **Macrostrat の形式要件に反する。**`unit_id` は一意でなければならず、重複したまま投入できない
- 出典・ページと結びつくべき記載文が、別の地層のものになっている。**出典の追跡可能性が壊れている**
- 見た目は埋まっているので、レビューで気づきにくい

### 未確認

**コード上の原因は未特定。**症状（IDの衝突と、記載・年代の転写）は確定しているが、どの処理で結合が位置ベースになっているかは追えていない。`unit_id` を採番する箇所と、記載文を地層へ結合する箇所を追う必要がある。

---

## 不整合 2: 根拠のない年代区間

`b_int = Messinian` / `t_int = Zanclean` が **17ユニット**に付いている。

```
17件が Messinian / Zanclean
  うち b_age_ma を持つ        2件
  うち b_age_ma が空          15件   ← 年代値なしで区間だけ付いている
```

Messinian（約7.25〜5.33 Ma）と Zanclean（約5.33〜3.6 Ma）は中新世末〜鮮新世前期にあたる。しかしこの17件には以下が含まれる。

- Horino / Ibonai / Maisawa / Rendaino / Hayawatari / Kusagi / Asanai 各段丘堆積物
- Towada-Hachinohe / Towada-Ofudo 火砕流堆積物
- river-bed deposits、flood-plain deposits

GOLD ではこれらはいずれも **Holocene 〜 Late Pleistocene**（0〜0.13 Ma 程度）。**500万年以上ずれている。**

### 発生源の推定

年代値を持つ2件のうち `m1286_p020` は `b_age_ma=6, t_age_ma=5`、証拠は `[C | PDF | English Abstract] b_age_ma: 6`。つまり **英文Abstractから拾った 5〜6 Ma という値が、その1ユニットを超えて広がっている**可能性が高い。不整合1（情報の転写）と同じ根であることを示唆する。

### 証拠IDの共有

```
ev_a  … 30ユニットで共有
```

`ev_a` という短い識別子が30ユニットで使い回されている。個別の出典に紐づいていないため、**「各データに出典、ページ、図表番号を記録する」という規則を満たしていない**。他の証拠IDも2件ずつ共有されている。

---

## 参考: GOLD との一致状況（全体像）

`claude_work/reports/比較_GOLD_vs_現行_20260811.md` に詳細。

| | 件数 |
| :--- | ---: |
| 対応がついた地層 | 30 |
| GOLD にあって出力に無い | 12 |
| 出力にあって GOLD に無い | 18 |

| 項目 | 一致 | 不一致 | 捏造 | 取りこぼし |
| :--- | ---: | ---: | ---: | ---: |
| strat_name | 6 | 0 | 0 | 10 |
| lithology | 10 | 6 | 0 | 14 |
| environment | 2 | 5 | 0 | 21 |
| b_int | 9 | 15 | 0 | 6 |
| t_int | 9 | 15 | 0 | 4 |
| min_thickness | 2 | 0 | 0 | 13 |
| max_thickness | 2 | 0 | 0 | 17 |
| basal_surface | 7 | 2 | 1 | 15 |
| **合計** | **47** | **48** | **1** | **108** |

### この数字の読み方（重要）

**一致率23%という数字をそのまま「精度が低い」と読むべきではない。**内訳の性質が違う。

- **捏造は1件のみ。**空欄にすべき箇所を埋めない設計は機能している。これは良い結果
- **取りこぼし108件が支配的。**これは「値を入れられなかった」であり、規則上は正しい振る舞い。自動化率が低いという話であって、誤りではない
- **不一致48件のうち、b_int/t_int の30件は不整合2に由来する。**モデルの読解力の問題ではなく、上記のバグ
- 42 対 48 というユニット数の差と、対応がついたのが30件だけという事実のほうが、項目一致率より重い

つまり **モデルを変える前に、この2つの不整合を直すべき**。今の状態で別モデルと比較しても、バグの分だけノイズが乗る。

---

## 推奨する順序

1. **不整合1の原因を特定して直す** — `unit_id` の重複と記載文の転写。投入を止める欠陥であり最優先
2. **不整合2を直す** — 年代値の根拠がないユニットの `b_int`/`t_int` は空欄にする。`ev_a` の使い回しも出典単位に分ける
3. **修正後に GOLD と再比較** — `compare_units.py` を同じ条件で回し、不一致48件がどこまで減るかを見る
4. **その後にモデル比較** — きれいになった状態で 3.6-flash と Flash-Lite を比較すれば、モデル差だけを見られる

---

## 使ったもの

- 比較スクリプト: `claude_work/scripts/compare_units.py`（新規）
  ```
  python claude_work/scripts/compare_units.py 正解.xlsx 候補.xlsx --out 比較.md
  ```
  「捏造（正解が空欄なのに値が入っている）」を独立して数える。自由記述の `unit_description` は完全一致で測っても意味がないため合計から外してある。

## 信頼度

| 項目 | 信頼度 | 根拠 |
| :--- | :--- | :--- |
| unit_id が4件重複 | **high** | xlsx を直接集計 |
| 4地層に別地層の記載・年代が入っている | **high** | 該当行を直接確認 |
| Messinian/Zanclean が17件、うち15件は年代値なし | **high** | xlsx を直接集計 |
| GOLD の年代が Holocene〜Late Pleistocene | **high** | GOLD を直接確認 |
| 5〜6 Ma の値が他ユニットへ波及したという推定 | **medium** | 状況証拠。コード上の確認は未実施 |
| コード上の原因箇所 | **未特定** | 未調査 |


---

## [比較_GOLD_vs_現行_20260811.md]

# 抽出結果の比較

- 正解: `claude_work/reports/Ichinohe_reference_GOLD.xlsx` （シート units） — GOLD(人手)
- 候補: `data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx` （シート Review） — gemini-3.6-flash 現行

## 地層の対応

- 対応がついた地層: **30**（うち名称が完全一致 29、類似で対応 1）
- 正解にあって候補に無い: **12**
- 候補にあって正解に無い: **18**

正解にあって候補に無い地層:

- River bed deposits
- Floodplain and valley-floor deposits
- Zyūmonzi Formation
- Suenomatuyama Formation
- Kadonosawa Formation
- Yotuyaku Formation
- Nisatai Formation
- Kuzumaki Formation
- River bed deposits
- Towada-Hachinohe Pyroclastic Flow Deposits
- Zyūmonzi Formation
- Kadonosawa Formation

候補にあって正解に無い地層（余分な地層）:

- landslide deposits
- colluvial and alluvial cone deposits
- Kamimetoki Sandstone Member
- Kawaguchi Porcelanite Member
- Hinosawa Conglomerate Member
- Metoki Coquina Conglomerate Member
- Shimotomai Volcaniclastic Rock Member
- Anausi Conglomerate Member
- Itukamati Sandstone Member
- Aikawa Volcanic Rock Member
- Nakuidake Volcanic Rock Member
- Mainosawa Sandstone Member
- Tate Sandstone Member
- Sikonai Siltstone Member
- Matsukura Siliciclastic Rock Member
- Koiwai Mudstone Member
- Keiseitoge Volcanic Rock Member
- Sukohata Siliciclastic Rock Member

## 項目ごとの照合

「捏造」は正解が空欄なのに候補が値を入れた件数。プロジェクト規則「推測で値を埋めない」に直接反するため、ここが増えるモデルは採用すべきでない。

「（参考）」の付いた項目は自由記述で、文字列の完全一致で測っても意味がないため合計には入れていない。

| 項目 | 一致 | 不一致 | 捏造 | 取りこぼし | 両方空 | 一致率 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| strat_name | 6 | 0 | 0 | 10 | 14 | 38% |
| lithology | 10 | 6 | 0 | 14 | 0 | 33% |
| minor_lith | 0 | 5 | 0 | 8 | 17 | 0% |
| environment | 2 | 5 | 0 | 21 | 2 | 7% |
| b_int | 9 | 15 | 0 | 6 | 0 | 30% |
| t_int | 9 | 15 | 0 | 4 | 2 | 32% |
| min_thickness | 2 | 0 | 0 | 13 | 15 | 13% |
| max_thickness | 2 | 0 | 0 | 17 | 11 | 11% |
| basal_surface | 7 | 2 | 1 | 15 | 5 | 28% |
| unit_description（参考） | 2 | 26 | 0 | 2 | 0 | 7% |
| **合計** | **47** | **48** | **1** | **108** | 66 | **23%** |

## 判定の目安

- **捏造 1件。**空欄であるべき箇所に値が入っている。採用前に中身を確認すること。
- 取りこぼし 108件。埋められるはずの値が空欄になっている。

## 差分の一覧

| 地層 | 項目 | 種別 | 正解 | 候補 |
| :--- | :--- | :--- | :--- | :--- |
| River bed deposits | lithology | 不一致 | gravel; sand | gravel |
| River bed deposits | environment | 取りこぼし | fluvial indet. | (空) |
| River bed deposits | b_int | 不一致 | Holocene | Messinian |
| River bed deposits | t_int | 不一致 | Holocene | Zanclean |
| River bed deposits | min_thickness | 取りこぼし | 2.1 | (空) |
| River bed deposits | max_thickness | 取りこぼし | 2.1 | (空) |
| River bed deposits | basal_surface | 取りこぼし | unconformable | (空) |
| River bed deposits | unit_description | 不一致 | other young and minor deposits, such as landslide deposits, … | River-bed deposits occur in the district as young and minor … |
| Horino terrace deposits | lithology | 取りこぼし | gravel; sand; silt | (空) |
| Horino terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Horino terrace deposits | b_int | 不一致 | Holocene | Messinian |
| Horino terrace deposits | t_int | 不一致 | Holocene | Zanclean |
| Horino terrace deposits | min_thickness | 取りこぼし | 1.8 | (空) |
| Horino terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Horino terrace deposits | unit_description | 不一致 | The deposits of the lower lower terrace are the Horino and t… | The Horino terrace deposits are lower lower terrace deposits… |
| Maisawa terrace deposits | lithology | 取りこぼし | gravel; sand; silt | (空) |
| Maisawa terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Maisawa terrace deposits | b_int | 不一致 | Late Pleistocene | Messinian |
| Maisawa terrace deposits | t_int | 不一致 | Holocene | Zanclean |
| Maisawa terrace deposits | min_thickness | 取りこぼし | 0.6 | (空) |
| Maisawa terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Maisawa terrace deposits | unit_description | 不一致 | The deposits of the higher lower terrace are the Maisawa and… | The Maisawa terrace deposits are higher lower terrace deposi… |
| Towada-Hachinohe Pyroclastic Flow Deposits | lithology | 取りこぼし | pumice lapilli; ash | (空) |
| Towada-Hachinohe Pyroclastic Flow Deposits | environment | 取りこぼし | pyroclastic flow | (空) |
| Towada-Hachinohe Pyroclastic Flow Deposits | b_int | 不一致 | Late Pleistocene | Messinian |
| Towada-Hachinohe Pyroclastic Flow Deposits | t_int | 不一致 | Late Pleistocene | Zanclean |
| Towada-Hachinohe Pyroclastic Flow Deposits | max_thickness | 取りこぼし | 20 | (空) |
| Towada-Hachinohe Pyroclastic Flow Deposits | unit_description | 取りこぼし | The pyroclastic flow deposits, derived from Towada volcano, … | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | lithology | 取りこぼし | pumice lapilli; ash | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | environment | 取りこぼし | pyroclastic flow | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | b_int | 不一致 | Late Pleistocene | Messinian |
| Towada-Ofudo Pyroclastic Flow Deposits | t_int | 不一致 | Late Pleistocene | Zanclean |
| Towada-Ofudo Pyroclastic Flow Deposits | max_thickness | 取りこぼし | 7 | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | unit_description | 取りこぼし | The pyroclastic flow deposits, derived from Towada volcano, … | (空) |
| Kusagi terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Kusagi terrace deposits | b_int | 不一致 | Late Pleistocene | Messinian |
| Kusagi terrace deposits | t_int | 不一致 | Late Pleistocene | Zanclean |
| Kusagi terrace deposits | min_thickness | 取りこぼし | 2 | (空) |
| Kusagi terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Kusagi terrace deposits | unit_description | 不一致 | The middle terrace deposits are subdivided into the Kusagi a… | The Kusagi terrace deposits are middle terrace deposits deve… |
| Asanai terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Asanai terrace deposits | b_int | 不一致 | Chibanian | Messinian |
| Asanai terrace deposits | t_int | 不一致 | Late Pleistocene | Zanclean |
| Asanai terrace deposits | min_thickness | 取りこぼし | 2 | (空) |
| Asanai terrace deposits | max_thickness | 取りこぼし | 5 | (空) |
| Asanai terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Asanai terrace deposits | unit_description | 不一致 | The higher terrace deposits are subdivided into the Asanai a… | The Asanai terrace deposits are higher terrace deposits dist… |
| Nanashigure Volcanic Fan Deposits | lithology | 不一致 | gravel; sand; silt | gravel |
| Nanashigure Volcanic Fan Deposits | environment | 取りこぼし | alluvial fan | (空) |
| Nanashigure Volcanic Fan Deposits | b_int | 不一致 | Calabrian | Messinian |
| Nanashigure Volcanic Fan Deposits | t_int | 不一致 | Chibanian | Zanclean |
| Nanashigure Volcanic Fan Deposits | max_thickness | 取りこぼし | 30 | (空) |
| Nanashigure Volcanic Fan Deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Nanashigure Volcanic Fan Deposits | unit_description | 不一致 | The Nanashigure Volcanic Fan Deposits, distributed only in t… | River-bed deposits occur in the district as young and minor … |
| Shitazaki Formation | minor_lith | 不一致 | sandstone; tuff | sandstone |
| Shitazaki Formation | environment | 取りこぼし | sublittoral | (空) |
| Shitazaki Formation | min_thickness | 取りこぼし | 200 | (空) |
| Shitazaki Formation | max_thickness | 取りこぼし | 200 | (空) |
| Shitazaki Formation | basal_surface | 不一致 | disconformable; locally conformable | conformable |
| Shitazaki Formation | unit_description | 不一致 | The Shitazaki Formation conformably overlies the Yanagisawa … | The Shitazaki Formation conformably overlies the Yanagisawa … |
| Yanagisawa Formation | lithology | 取りこぼし | diatomite; diatomaceous mudstone; hard shale; porcellanite | (空) |
| Yanagisawa Formation | environment | 取りこぼし | bathyal | (空) |
| Yanagisawa Formation | min_thickness | 取りこぼし | 24 | (空) |
| Yanagisawa Formation | max_thickness | 取りこぼし | 60 | (空) |
| Yanagisawa Formation | unit_description | 不一致 | The Yanagisawa Formation conformably overlies the Zyūmonzi F… | The Yanagisawa Formation conformably overlies the Zyūmonzi F… |
| Zyūmonzi Formation | minor_lith | 不一致 | conglomerate; coquina conglomerate; volcaniclastic | conglomerate; volcaniclastic |
| Zyūmonzi Formation | environment | 不一致 | shallow marine | shallow subtidal |
| Zyūmonzi Formation | min_thickness | 取りこぼし | 100 | (空) |
| Zyūmonzi Formation | max_thickness | 取りこぼし | 150 | (空) |
| Zyūmonzi Formation | unit_description | 不一致 | The Zyūmonzi Formation overlies the Suenomatuyama Formation … | The Zyūmonzi Formation overlies the Suenomatuyama Formation … |
| Suenomatuyama Formation | lithology | 取りこぼし | sandstone | (空) |
| Suenomatuyama Formation | minor_lith | 取りこぼし | conglomerate; volcaniclastic; lava; intrusive rocks; mudston… | (空) |
| Suenomatuyama Formation | environment | 不一致 | shallow marine | marine |
| Suenomatuyama Formation | min_thickness | 取りこぼし | 200 | (空) |
| Suenomatuyama Formation | max_thickness | 取りこぼし | 400 | (空) |
| Suenomatuyama Formation | basal_surface | 不一致 | conformable; locally disconformable | conformable |
| Suenomatuyama Formation | unit_description | 不一致 | The Suenomatuyama Formation conformably / slightly-unconform… | The Suenomatuyama Formation conformably / slightly-unconform… |
| Kadonosawa Formation | lithology | 取りこぼし | siltstone | (空) |
| Kadonosawa Formation | minor_lith | 取りこぼし | mudstone; sandstone; sandy mudstone; conglomerate | (空) |
| Kadonosawa Formation | environment | 不一致 | shallow marine to bathyal | shallow subtidal |
| Kadonosawa Formation | max_thickness | 取りこぼし | 80 | (空) |
| Kadonosawa Formation | basal_surface | 取りこぼし | conformable | (空) |
| Kadonosawa Formation | unit_description | 不一致 | The Kadonosawa Formation conformably overlies the Yotuyaku F… | The Kadonosawa Formation conformably overlies the Yotuyaku F… |
| Yotuyaku Formation | lithology | 取りこぼし | conglomerate; sandstone; mudstone | (空) |
| Yotuyaku Formation | minor_lith | 取りこぼし | volcaniclastic; intrusive rocks; muddy sandstone | (空) |
| Yotuyaku Formation | environment | 不一致 | fluvial indet.; lacustrine indet.; shallow marine | non-marine |
| Yotuyaku Formation | max_thickness | 取りこぼし | 600 | (空) |
| Yotuyaku Formation | unit_description | 不一致 | The Yotuyaku Formation unconformably overlies the previous r… | The Yotuyaku Formation unconformably overlies the previous r… |
| Ainoyama Formation | strat_name | 取りこぼし | Ainoyama Formation | (空) |
| Ainoyama Formation | lithology | 不一致 | dacite lava | dacite; conglomerate |
| Ainoyama Formation | minor_lith | 取りこぼし | conglomerate | (空) |
| Ainoyama Formation | environment | 取りこぼし | non-marine | (空) |
| Ainoyama Formation | basal_surface | 取りこぼし | fault | (空) |
| Ainoyama Formation | unit_description | 不一致 | The Ainoyama Formation is composed of dacitic lava and congl… | The Ainoyama Formation is composed of dacitic lava and congl… |
| Nisatai Formation | strat_name | 取りこぼし | Nisatai Formation | (空) |
| Nisatai Formation | lithology | 不一致 | rhyolite lapilli tuff | pumice; tuff; conglomerate; sandstone; mudstone |
| Nisatai Formation | minor_lith | 取りこぼし | tuff breccia; conglomerate; sandstone; mudstone; lignite | (空) |
| Nisatai Formation | min_thickness | 取りこぼし | 150 | (空) |
| Nisatai Formation | unit_description | 不一致 | The Nisatai Formation is composed of upper welded rhyolitic … | The Nisatai Formation is composed of upper welded rhyolitic … |
| Ichinohe Pluton | strat_name | 取りこぼし | Ichinohe Pluton | (空) |
| Ichinohe Pluton | lithology | 取りこぼし | monzodiorite; quartz monzonite | (空) |
| Ichinohe Pluton | b_int | 取りこぼし | Early Cretaceous | (空) |
| Ichinohe Pluton | t_int | 取りこぼし | Early Cretaceous | (空) |
| Ichinohe Pluton | basal_surface | 取りこぼし | intrusive | (空) |
| Ichinohe Pluton | unit_description | 不一致 | The Ichinohe Pluton is lithologically characterised by two f… | The Ichinohe Pluton is lithologically characterised by two f… |
| Kuzumaki Formation | strat_name | 取りこぼし | Kuzumaki Formation | (空) |
| Kuzumaki Formation | lithology | 取りこぼし | phyllitic mudstone; pelitic mixed rock | (空) |
| Kuzumaki Formation | minor_lith | 不一致 | mafic; limestone; chert; siliceous mudstone; sandstone | mafic; limestone; chert; mudstone; sandstone |
| Kuzumaki Formation | environment | 取りこぼし | deep marine | (空) |
| Kuzumaki Formation | b_int | 取りこぼし | Middle Jurassic | (空) |
| Kuzumaki Formation | t_int | 取りこぼし | Middle Jurassic | (空) |
| Kuzumaki Formation | unit_description | 不一致 | The Kuzumaki Formation consists mainly of phyllitic mudstone… | The Kuzumaki Formation consists mainly of phyllitic mudstone… |
| Ibonai terrace deposits | lithology | 取りこぼし | gravel; sand | (空) |
| Ibonai terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Ibonai terrace deposits | b_int | 不一致 | Holocene | Messinian |
| Ibonai terrace deposits | t_int | 不一致 | Holocene | Zanclean |
| Ibonai terrace deposits | unit_description | 不一致 | The deposits of the lower lower terrace are the Horino and t… | The Ibonai terrace deposits are lower lower terrace deposits… |
| Rendaino terrace deposits | lithology | 取りこぼし | gravel; sand; silt | (空) |
| Rendaino terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Rendaino terrace deposits | b_int | 不一致 | Late Pleistocene | Messinian |
| Rendaino terrace deposits | t_int | 不一致 | Holocene | Zanclean |
| Rendaino terrace deposits | min_thickness | 取りこぼし | 3.5 | (空) |
| Rendaino terrace deposits | max_thickness | 取りこぼし | 3.5 | (空) |
| Rendaino terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Rendaino terrace deposits | unit_description | 不一致 | The deposits of the higher lower terrace are the Maisawa and… | The Rendaino terrace deposits are higher lower terrace depos… |
| Hayawatari terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Hayawatari terrace deposits | b_int | 不一致 | Late Pleistocene | Messinian |
| Hayawatari terrace deposits | t_int | 不一致 | Late Pleistocene | Zanclean |
| Hayawatari terrace deposits | min_thickness | 取りこぼし | 1.6 | (空) |
| Hayawatari terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Hayawatari terrace deposits | unit_description | 不一致 | The middle terrace deposits are subdivided into the Kusagi a… | The Hayawatari terrace deposits are middle terrace deposits … |
| Mukaikawara terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Mukaikawara terrace deposits | b_int | 不一致 | Chibanian | Messinian |
| Mukaikawara terrace deposits | t_int | 不一致 | Late Pleistocene | Zanclean |
| Mukaikawara terrace deposits | min_thickness | 取りこぼし | 1 | (空) |
| Mukaikawara terrace deposits | max_thickness | 取りこぼし | 5 | (空) |
| Mukaikawara terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Mukaikawara terrace deposits | unit_description | 不一致 | The higher terrace deposits are subdivided into the Asanai a… | The Mukaikawara terrace deposits are higher terrace deposits… |
| Oritsumedake Fan Deposits | environment | 取りこぼし | alluvial fan | (空) |
| Oritsumedake Fan Deposits | b_int | 不一致 | Chibanian | Messinian |
| Oritsumedake Fan Deposits | t_int | 不一致 | Holocene | Zanclean |
| Oritsumedake Fan Deposits | max_thickness | 取りこぼし | 20 | (空) |
| Esashika Formation | strat_name | 取りこぼし | Esashika Formation | (空) |
| Esashika Formation | minor_lith | 取りこぼし | sand; mud | (空) |
| Esashika Formation | environment | 取りこぼし | alluvial fan | (空) |
| Esashika Formation | b_int | 不一致 | Calabrian | Messinian |
| Esashika Formation | t_int | 不一致 | Chibanian | Zanclean |
| Esashika Formation | max_thickness | 取りこぼし | 80 | (空) |
| Esashika Formation | unit_description | 不一致 | The Esashika Formation, distributed only along the eastern f… | Colluvial and alluvial cone deposits occur in the district a… |
| Toya Formation | strat_name | 取りこぼし | Toya Formation | (空) |
| Toya Formation | lithology | 不一致 | pumice lapilli tuff | volcaniclastic; conglomerate; sandstone; mudstone |
| Toya Formation | minor_lith | 取りこぼし | tuff; mudstone; sandstone; conglomerate; lignite | (空) |
| Toya Formation | max_thickness | 取りこぼし | 170 | (空) |
| Toya Formation | unit_description | 不一致 | The Toya Formation unconformably overlies the Jurassic strat… | Flood-plain and valley-floor deposits occur in the district … |
| Tsukanaigawa Pluton | strat_name | 取りこぼし | Tsukanaigawa Pluton | (空) |
| Tsukanaigawa Pluton | b_int | 取りこぼし | Early Cretaceous | (空) |
| Tsukanaigawa Pluton | t_int | 取りこぼし | Early Cretaceous | (空) |
| Tsukanaigawa Pluton | basal_surface | 取りこぼし | intrusive | (空) |
| Kassenba Formation | strat_name | 取りこぼし | Kassenba Formation | (空) |
| Kassenba Formation | minor_lith | 取りこぼし | siliceous mudstone; slaty mudstone; laminated mudstone; cher… | (空) |
| Kassenba Formation | environment | 取りこぼし | deep marine | (空) |
| Kassenba Formation | b_int | 取りこぼし | Oxfordian | (空) |
| Kassenba Formation | t_int | 取りこぼし | Kimmeridgian | (空) |
| Kassenba Formation | basal_surface | 取りこぼし | fault | (空) |
| Kassenba Formation | unit_description | 不一致 | The Kassenba Formation is characterised by at least two repe… | The Kassenba Formation is characterised by at least two repe… |
| Seki Formation | strat_name | 取りこぼし | Seki Formation | (空) |
| Seki Formation | lithology | 取りこぼし | slaty mudstone; laminated mudstone | (空) |
| Seki Formation | minor_lith | 不一致 | chert; siliceous mudstone; sandstone | chert; mudstone; sandstone |
| Seki Formation | environment | 取りこぼし | deep marine | (空) |
| Seki Formation | b_int | 取りこぼし | Kimmeridgian | (空) |
| Seki Formation | basal_surface | 取りこぼし | fault | (空) |
| Seki Formation | unit_description | 不一致 | The Seki Formation is characterised by at least three repeti… | The Seki Formation is characterised by at least three repeti… |
| Takayashiki Formation | strat_name | 取りこぼし | Takayashiki Formation | (空) |
| Takayashiki Formation | lithology | 取りこぼし | dismembered sandstone; dismembered mudstone; slaty mudstone | (空) |
| Takayashiki Formation | minor_lith | 不一致 | chert; siliceous mudstone; mafic | chert; mudstone; mafic |
| Takayashiki Formation | environment | 取りこぼし | deep marine | (空) |
| Takayashiki Formation | b_int | 取りこぼし | Oxfordian | (空) |
| Takayashiki Formation | max_thickness | 取りこぼし | 3500 | (空) |
| Takayashiki Formation | unit_description | 不一致 | The Takayashiki Formation consists mainly of alternating bed… | The Takayashiki Formation consists mainly of alternating bed… |
| Floodplain and valley-floor deposits | lithology | 不一致 | gravel; sand; mud | volcaniclastic; conglomerate; sandstone; mudstone |
| Floodplain and valley-floor deposits | environment | 不一致 | fluvial indet. | non-marine |
| Floodplain and valley-floor deposits | b_int | 不一致 | Holocene | Messinian |
| Floodplain and valley-floor deposits | t_int | 不一致 | Holocene | Zanclean |
| Floodplain and valley-floor deposits | basal_surface | 捏造 | (空) | unconformable |
| Floodplain and valley-floor deposits | unit_description | 不一致 | other young and minor deposits, such as landslide deposits, … | Flood-plain and valley-floor deposits occur in the district … |



---

## [比較_修正後_20260811.md]

# 抽出結果の比較

- 正解: `claude_work/reports/Ichinohe_reference_GOLD.xlsx` （シート units） — GOLD(人手)
- 候補: `data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx` （シート Review） — 修正後(全キャッシュ)

## 地層の対応

- 対応がついた地層: **30**（うち名称が完全一致 29、類似で対応 1）
- 正解にあって候補に無い: **12**
- 候補にあって正解に無い: **18**

正解にあって候補に無い地層:

- River bed deposits
- Floodplain and valley-floor deposits
- Zyūmonzi Formation
- Suenomatuyama Formation
- Kadonosawa Formation
- Yotuyaku Formation
- Nisatai Formation
- Kuzumaki Formation
- River bed deposits
- Towada-Hachinohe Pyroclastic Flow Deposits
- Zyūmonzi Formation
- Kadonosawa Formation

候補にあって正解に無い地層（余分な地層）:

- landslide deposits
- colluvial and alluvial cone deposits
- Kamimetoki Sandstone Member
- Kawaguchi Porcelanite Member
- Hinosawa Conglomerate Member
- Metoki Coquina Conglomerate Member
- Shimotomai Volcaniclastic Rock Member
- Anausi Conglomerate Member
- Itukamati Sandstone Member
- Aikawa Volcanic Rock Member
- Nakuidake Volcanic Rock Member
- Mainosawa Sandstone Member
- Tate Sandstone Member
- Sikonai Siltstone Member
- Matsukura Siliciclastic Rock Member
- Koiwai Mudstone Member
- Keiseitoge Volcanic Rock Member
- Sukohata Siliciclastic Rock Member

## 項目ごとの照合

「捏造」は正解が空欄なのに候補が値を入れた件数。プロジェクト規則「推測で値を埋めない」に直接反するため、ここが増えるモデルは採用すべきでない。

「（参考）」の付いた項目は自由記述で、文字列の完全一致で測っても意味がないため合計には入れていない。

| 項目 | 一致 | 不一致 | 捏造 | 取りこぼし | 両方空 | 一致率 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| strat_name | 6 | 0 | 0 | 10 | 14 | 38% |
| lithology | 10 | 4 | 0 | 16 | 0 | 33% |
| minor_lith | 0 | 5 | 0 | 8 | 17 | 0% |
| environment | 3 | 7 | 0 | 18 | 2 | 11% |
| b_int | 9 | 0 | 0 | 21 | 0 | 30% |
| t_int | 9 | 0 | 0 | 19 | 2 | 32% |
| min_thickness | 2 | 0 | 0 | 13 | 15 | 13% |
| max_thickness | 2 | 0 | 0 | 17 | 11 | 11% |
| basal_surface | 7 | 2 | 0 | 15 | 6 | 29% |
| unit_description（参考） | 2 | 26 | 0 | 2 | 0 | 7% |
| **合計** | **48** | **18** | **0** | **137** | 67 | **24%** |

## 判定の目安

- 捏造 **0件**。空欄にすべき箇所を埋めていない。
- 取りこぼし 137件。埋められるはずの値が空欄になっている。

## 差分の一覧

| 地層 | 項目 | 種別 | 正解 | 候補 |
| :--- | :--- | :--- | :--- | :--- |
| River bed deposits | lithology | 取りこぼし | gravel; sand | (空) |
| River bed deposits | environment | 取りこぼし | fluvial indet. | (空) |
| River bed deposits | b_int | 取りこぼし | Holocene | (空) |
| River bed deposits | t_int | 取りこぼし | Holocene | (空) |
| River bed deposits | min_thickness | 取りこぼし | 2.1 | (空) |
| River bed deposits | max_thickness | 取りこぼし | 2.1 | (空) |
| River bed deposits | basal_surface | 取りこぼし | unconformable | (空) |
| River bed deposits | unit_description | 不一致 | other young and minor deposits, such as landslide deposits, … | River-bed deposits occur in the district as young and minor … |
| Horino terrace deposits | lithology | 取りこぼし | gravel; sand; silt | (空) |
| Horino terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Horino terrace deposits | b_int | 取りこぼし | Holocene | (空) |
| Horino terrace deposits | t_int | 取りこぼし | Holocene | (空) |
| Horino terrace deposits | min_thickness | 取りこぼし | 1.8 | (空) |
| Horino terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Horino terrace deposits | unit_description | 不一致 | The deposits of the lower lower terrace are the Horino and t… | The Horino terrace deposits are lower lower terrace deposits… |
| Maisawa terrace deposits | lithology | 取りこぼし | gravel; sand; silt | (空) |
| Maisawa terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Maisawa terrace deposits | b_int | 取りこぼし | Late Pleistocene | (空) |
| Maisawa terrace deposits | t_int | 取りこぼし | Holocene | (空) |
| Maisawa terrace deposits | min_thickness | 取りこぼし | 0.6 | (空) |
| Maisawa terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Maisawa terrace deposits | unit_description | 不一致 | The deposits of the higher lower terrace are the Maisawa and… | The Maisawa terrace deposits are higher lower terrace deposi… |
| Towada-Hachinohe Pyroclastic Flow Deposits | lithology | 取りこぼし | pumice lapilli; ash | (空) |
| Towada-Hachinohe Pyroclastic Flow Deposits | environment | 取りこぼし | pyroclastic flow | (空) |
| Towada-Hachinohe Pyroclastic Flow Deposits | b_int | 取りこぼし | Late Pleistocene | (空) |
| Towada-Hachinohe Pyroclastic Flow Deposits | t_int | 取りこぼし | Late Pleistocene | (空) |
| Towada-Hachinohe Pyroclastic Flow Deposits | max_thickness | 取りこぼし | 20 | (空) |
| Towada-Hachinohe Pyroclastic Flow Deposits | unit_description | 取りこぼし | The pyroclastic flow deposits, derived from Towada volcano, … | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | lithology | 取りこぼし | pumice lapilli; ash | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | environment | 取りこぼし | pyroclastic flow | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | b_int | 取りこぼし | Late Pleistocene | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | t_int | 取りこぼし | Late Pleistocene | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | max_thickness | 取りこぼし | 7 | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | unit_description | 取りこぼし | The pyroclastic flow deposits, derived from Towada volcano, … | (空) |
| Kusagi terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Kusagi terrace deposits | b_int | 取りこぼし | Late Pleistocene | (空) |
| Kusagi terrace deposits | t_int | 取りこぼし | Late Pleistocene | (空) |
| Kusagi terrace deposits | min_thickness | 取りこぼし | 2 | (空) |
| Kusagi terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Kusagi terrace deposits | unit_description | 不一致 | The middle terrace deposits are subdivided into the Kusagi a… | The Kusagi terrace deposits are middle terrace deposits deve… |
| Asanai terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Asanai terrace deposits | b_int | 取りこぼし | Chibanian | (空) |
| Asanai terrace deposits | t_int | 取りこぼし | Late Pleistocene | (空) |
| Asanai terrace deposits | min_thickness | 取りこぼし | 2 | (空) |
| Asanai terrace deposits | max_thickness | 取りこぼし | 5 | (空) |
| Asanai terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Asanai terrace deposits | unit_description | 不一致 | The higher terrace deposits are subdivided into the Asanai a… | The Asanai terrace deposits are higher terrace deposits dist… |
| Nanashigure Volcanic Fan Deposits | lithology | 不一致 | gravel; sand; silt | gravel |
| Nanashigure Volcanic Fan Deposits | environment | 取りこぼし | alluvial fan | (空) |
| Nanashigure Volcanic Fan Deposits | b_int | 取りこぼし | Calabrian | (空) |
| Nanashigure Volcanic Fan Deposits | t_int | 取りこぼし | Chibanian | (空) |
| Nanashigure Volcanic Fan Deposits | max_thickness | 取りこぼし | 30 | (空) |
| Nanashigure Volcanic Fan Deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Nanashigure Volcanic Fan Deposits | unit_description | 不一致 | The Nanashigure Volcanic Fan Deposits, distributed only in t… | The Nanashigure Volcanic Fan Deposits are distributed only i… |
| Shitazaki Formation | minor_lith | 不一致 | sandstone; tuff | sandstone |
| Shitazaki Formation | environment | 取りこぼし | sublittoral | (空) |
| Shitazaki Formation | min_thickness | 取りこぼし | 200 | (空) |
| Shitazaki Formation | max_thickness | 取りこぼし | 200 | (空) |
| Shitazaki Formation | basal_surface | 不一致 | disconformable; locally conformable | conformable |
| Shitazaki Formation | unit_description | 不一致 | The Shitazaki Formation conformably overlies the Yanagisawa … | The Shitazaki Formation conformably overlies the Yanagisawa … |
| Yanagisawa Formation | lithology | 取りこぼし | diatomite; diatomaceous mudstone; hard shale; porcellanite | (空) |
| Yanagisawa Formation | environment | 取りこぼし | bathyal | (空) |
| Yanagisawa Formation | min_thickness | 取りこぼし | 24 | (空) |
| Yanagisawa Formation | max_thickness | 取りこぼし | 60 | (空) |
| Yanagisawa Formation | unit_description | 不一致 | The Yanagisawa Formation conformably overlies the Zyūmonzi F… | The Yanagisawa Formation conformably overlies the Zyūmonzi F… |
| Zyūmonzi Formation | minor_lith | 不一致 | conglomerate; coquina conglomerate; volcaniclastic | conglomerate; volcaniclastic |
| Zyūmonzi Formation | environment | 不一致 | shallow marine | shallow subtidal |
| Zyūmonzi Formation | min_thickness | 取りこぼし | 100 | (空) |
| Zyūmonzi Formation | max_thickness | 取りこぼし | 150 | (空) |
| Zyūmonzi Formation | unit_description | 不一致 | The Zyūmonzi Formation overlies the Suenomatuyama Formation … | The Zyūmonzi Formation overlies the Suenomatuyama Formation … |
| Suenomatuyama Formation | lithology | 取りこぼし | sandstone | (空) |
| Suenomatuyama Formation | minor_lith | 取りこぼし | conglomerate; volcaniclastic; lava; intrusive rocks; mudston… | (空) |
| Suenomatuyama Formation | environment | 不一致 | shallow marine | marine |
| Suenomatuyama Formation | min_thickness | 取りこぼし | 200 | (空) |
| Suenomatuyama Formation | max_thickness | 取りこぼし | 400 | (空) |
| Suenomatuyama Formation | basal_surface | 不一致 | conformable; locally disconformable | conformable |
| Suenomatuyama Formation | unit_description | 不一致 | The Suenomatuyama Formation conformably / slightly-unconform… | The Suenomatuyama Formation conformably / slightly-unconform… |
| Kadonosawa Formation | lithology | 取りこぼし | siltstone | (空) |
| Kadonosawa Formation | minor_lith | 取りこぼし | mudstone; sandstone; sandy mudstone; conglomerate | (空) |
| Kadonosawa Formation | environment | 不一致 | shallow marine to bathyal | shallow subtidal |
| Kadonosawa Formation | max_thickness | 取りこぼし | 80 | (空) |
| Kadonosawa Formation | basal_surface | 取りこぼし | conformable | (空) |
| Kadonosawa Formation | unit_description | 不一致 | The Kadonosawa Formation conformably overlies the Yotuyaku F… | The Kadonosawa Formation conformably overlies the Yotuyaku F… |
| Yotuyaku Formation | lithology | 取りこぼし | conglomerate; sandstone; mudstone | (空) |
| Yotuyaku Formation | minor_lith | 取りこぼし | volcaniclastic; intrusive rocks; muddy sandstone | (空) |
| Yotuyaku Formation | environment | 不一致 | fluvial indet.; lacustrine indet.; shallow marine | non-marine |
| Yotuyaku Formation | max_thickness | 取りこぼし | 600 | (空) |
| Yotuyaku Formation | unit_description | 不一致 | The Yotuyaku Formation unconformably overlies the previous r… | The Yotuyaku Formation unconformably overlies the previous r… |
| Ainoyama Formation | strat_name | 取りこぼし | Ainoyama Formation | (空) |
| Ainoyama Formation | lithology | 不一致 | dacite lava | dacite; conglomerate |
| Ainoyama Formation | minor_lith | 取りこぼし | conglomerate | (空) |
| Ainoyama Formation | environment | 取りこぼし | non-marine | (空) |
| Ainoyama Formation | basal_surface | 取りこぼし | fault | (空) |
| Ainoyama Formation | unit_description | 不一致 | The Ainoyama Formation is composed of dacitic lava and congl… | The Ainoyama Formation is composed of dacitic lava and congl… |
| Nisatai Formation | strat_name | 取りこぼし | Nisatai Formation | (空) |
| Nisatai Formation | lithology | 不一致 | rhyolite lapilli tuff | pumice; tuff; conglomerate; sandstone; mudstone |
| Nisatai Formation | minor_lith | 取りこぼし | tuff breccia; conglomerate; sandstone; mudstone; lignite | (空) |
| Nisatai Formation | min_thickness | 取りこぼし | 150 | (空) |
| Nisatai Formation | unit_description | 不一致 | The Nisatai Formation is composed of upper welded rhyolitic … | The Nisatai Formation is composed of upper welded rhyolitic … |
| Ichinohe Pluton | strat_name | 取りこぼし | Ichinohe Pluton | (空) |
| Ichinohe Pluton | lithology | 取りこぼし | monzodiorite; quartz monzonite | (空) |
| Ichinohe Pluton | b_int | 取りこぼし | Early Cretaceous | (空) |
| Ichinohe Pluton | t_int | 取りこぼし | Early Cretaceous | (空) |
| Ichinohe Pluton | basal_surface | 取りこぼし | intrusive | (空) |
| Ichinohe Pluton | unit_description | 不一致 | The Ichinohe Pluton is lithologically characterised by two f… | The Ichinohe Pluton is lithologically characterised by two f… |
| Kuzumaki Formation | strat_name | 取りこぼし | Kuzumaki Formation | (空) |
| Kuzumaki Formation | lithology | 取りこぼし | phyllitic mudstone; pelitic mixed rock | (空) |
| Kuzumaki Formation | minor_lith | 不一致 | mafic; limestone; chert; siliceous mudstone; sandstone | mafic; limestone; chert; mudstone; sandstone |
| Kuzumaki Formation | environment | 取りこぼし | deep marine | (空) |
| Kuzumaki Formation | b_int | 取りこぼし | Middle Jurassic | (空) |
| Kuzumaki Formation | t_int | 取りこぼし | Middle Jurassic | (空) |
| Kuzumaki Formation | unit_description | 不一致 | The Kuzumaki Formation consists mainly of phyllitic mudstone… | The Kuzumaki Formation consists mainly of phyllitic mudstone… |
| Ibonai terrace deposits | lithology | 取りこぼし | gravel; sand | (空) |
| Ibonai terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Ibonai terrace deposits | b_int | 取りこぼし | Holocene | (空) |
| Ibonai terrace deposits | t_int | 取りこぼし | Holocene | (空) |
| Ibonai terrace deposits | unit_description | 不一致 | The deposits of the lower lower terrace are the Horino and t… | The Ibonai terrace deposits are lower lower terrace deposits… |
| Rendaino terrace deposits | lithology | 取りこぼし | gravel; sand; silt | (空) |
| Rendaino terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Rendaino terrace deposits | b_int | 取りこぼし | Late Pleistocene | (空) |
| Rendaino terrace deposits | t_int | 取りこぼし | Holocene | (空) |
| Rendaino terrace deposits | min_thickness | 取りこぼし | 3.5 | (空) |
| Rendaino terrace deposits | max_thickness | 取りこぼし | 3.5 | (空) |
| Rendaino terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Rendaino terrace deposits | unit_description | 不一致 | The deposits of the higher lower terrace are the Maisawa and… | The Rendaino terrace deposits are higher lower terrace depos… |
| Hayawatari terrace deposits | b_int | 取りこぼし | Late Pleistocene | (空) |
| Hayawatari terrace deposits | t_int | 取りこぼし | Late Pleistocene | (空) |
| Hayawatari terrace deposits | min_thickness | 取りこぼし | 1.6 | (空) |
| Hayawatari terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Hayawatari terrace deposits | unit_description | 不一致 | The middle terrace deposits are subdivided into the Kusagi a… | The Hayawatari terrace deposits are middle terrace deposits … |
| Mukaikawara terrace deposits | environment | 取りこぼし | fluvial indet. | (空) |
| Mukaikawara terrace deposits | b_int | 取りこぼし | Chibanian | (空) |
| Mukaikawara terrace deposits | t_int | 取りこぼし | Late Pleistocene | (空) |
| Mukaikawara terrace deposits | min_thickness | 取りこぼし | 1 | (空) |
| Mukaikawara terrace deposits | max_thickness | 取りこぼし | 5 | (空) |
| Mukaikawara terrace deposits | basal_surface | 取りこぼし | unconformable | (空) |
| Mukaikawara terrace deposits | unit_description | 不一致 | The higher terrace deposits are subdivided into the Asanai a… | The Mukaikawara terrace deposits are higher terrace deposits… |
| Oritsumedake Fan Deposits | environment | 取りこぼし | alluvial fan | (空) |
| Oritsumedake Fan Deposits | b_int | 取りこぼし | Chibanian | (空) |
| Oritsumedake Fan Deposits | t_int | 取りこぼし | Holocene | (空) |
| Oritsumedake Fan Deposits | max_thickness | 取りこぼし | 20 | (空) |
| Esashika Formation | strat_name | 取りこぼし | Esashika Formation | (空) |
| Esashika Formation | minor_lith | 取りこぼし | sand; mud | (空) |
| Esashika Formation | environment | 取りこぼし | alluvial fan | (空) |
| Esashika Formation | b_int | 取りこぼし | Calabrian | (空) |
| Esashika Formation | t_int | 取りこぼし | Chibanian | (空) |
| Esashika Formation | max_thickness | 取りこぼし | 80 | (空) |
| Esashika Formation | unit_description | 不一致 | The Esashika Formation, distributed only along the eastern f… | The Esashika Formation is distributed only along the eastern… |
| Toya Formation | strat_name | 取りこぼし | Toya Formation | (空) |
| Toya Formation | lithology | 不一致 | pumice lapilli tuff | volcaniclastic; conglomerate; sandstone; mudstone |
| Toya Formation | minor_lith | 取りこぼし | tuff; mudstone; sandstone; conglomerate; lignite | (空) |
| Toya Formation | max_thickness | 取りこぼし | 170 | (空) |
| Toya Formation | unit_description | 不一致 | The Toya Formation unconformably overlies the Jurassic strat… | The Toya Formation unconformably overlies the Jurassic strat… |
| Tsukanaigawa Pluton | strat_name | 取りこぼし | Tsukanaigawa Pluton | (空) |
| Tsukanaigawa Pluton | b_int | 取りこぼし | Early Cretaceous | (空) |
| Tsukanaigawa Pluton | t_int | 取りこぼし | Early Cretaceous | (空) |
| Tsukanaigawa Pluton | basal_surface | 取りこぼし | intrusive | (空) |
| Kassenba Formation | strat_name | 取りこぼし | Kassenba Formation | (空) |
| Kassenba Formation | minor_lith | 取りこぼし | siliceous mudstone; slaty mudstone; laminated mudstone; cher… | (空) |
| Kassenba Formation | environment | 不一致 | deep marine | deep-water indet. |
| Kassenba Formation | b_int | 取りこぼし | Oxfordian | (空) |
| Kassenba Formation | t_int | 取りこぼし | Kimmeridgian | (空) |
| Kassenba Formation | basal_surface | 取りこぼし | fault | (空) |
| Kassenba Formation | unit_description | 不一致 | The Kassenba Formation is characterised by at least two repe… | The Kassenba Formation is characterised by at least two repe… |
| Seki Formation | strat_name | 取りこぼし | Seki Formation | (空) |
| Seki Formation | lithology | 取りこぼし | slaty mudstone; laminated mudstone | (空) |
| Seki Formation | minor_lith | 不一致 | chert; siliceous mudstone; sandstone | chert; mudstone; sandstone |
| Seki Formation | environment | 取りこぼし | deep marine | (空) |
| Seki Formation | b_int | 取りこぼし | Kimmeridgian | (空) |
| Seki Formation | basal_surface | 取りこぼし | fault | (空) |
| Seki Formation | unit_description | 不一致 | The Seki Formation is characterised by at least three repeti… | The Seki Formation is characterised by at least three repeti… |
| Takayashiki Formation | strat_name | 取りこぼし | Takayashiki Formation | (空) |
| Takayashiki Formation | lithology | 取りこぼし | dismembered sandstone; dismembered mudstone; slaty mudstone | (空) |
| Takayashiki Formation | minor_lith | 不一致 | chert; siliceous mudstone; mafic | chert; mudstone; mafic |
| Takayashiki Formation | environment | 不一致 | deep marine | deep-water indet. |
| Takayashiki Formation | b_int | 取りこぼし | Oxfordian | (空) |
| Takayashiki Formation | max_thickness | 取りこぼし | 3500 | (空) |
| Takayashiki Formation | unit_description | 不一致 | The Takayashiki Formation consists mainly of alternating bed… | The Takayashiki Formation consists mainly of alternating bed… |
| Floodplain and valley-floor deposits | lithology | 取りこぼし | gravel; sand; mud | (空) |
| Floodplain and valley-floor deposits | environment | 不一致 | fluvial indet. | floodplain |
| Floodplain and valley-floor deposits | b_int | 取りこぼし | Holocene | (空) |
| Floodplain and valley-floor deposits | t_int | 取りこぼし | Holocene | (空) |
| Floodplain and valley-floor deposits | unit_description | 不一致 | other young and minor deposits, such as landslide deposits, … | Flood-plain and valley-floor deposits occur in the district … |



---

## [誤りの型分析と日本語名の付与_20260812.md]

# Column membershipの誤りの型と、日本語名の付与（2026-08-12 夜）

Bedrockが日次上限（20 call/日）に達したため、外部送信が要らない分析と
実装を進めた。測定は明日。

## 1. 誤りの型（Bedrock・PDF 16・完走した唯一の実測から）

`claude_work/tools/analyze_column_membership.py`（新規）で、GOLDのハッシュを
unit名へ復元して混同表に戻した。

内訳（回答があったunit 36件）:

| 型 | 件数 | 意味 |
|---|---|---|
| not_answered | 14 | 図に載っているのに「どの列にも属さない」と回答 |
| shifted | 9 | 別の列に入れた |
| invented | 7 | 図に無いunitに列を割り当てた |
| over | 4 | 正しい列を含むが余分な列も付けた |
| exact | 2 | 完全一致 |

混同表（行=正解、列=回答、unit数）:

|  | 西 | 中 | 東 |
|---|---|---|---|
| **西** | 5 | 6 | 4 |
| **中** | 3 | 3 | 3 |
| **東** | **8** | 5 | 2 |

**東部が正解のunitを8件も西部と答えている。** 左右の取り違えが最大の誤り。

### invented 7件の中身

`landslide deposits` / `flood-plain and valley-floor deposits` /
`colluvial and alluvial cone deposits` に列を割り当てていた。
しかし第2.1図の脚注は

> 地すべり堆積物，崖錐・沖積錐堆積物は省略した．

と明記しており、**GOLDが「どの列にも属さない」としているのは正しい**。
残り4件は Member（部層）で、図はFormation（層）までしか描いていない。

### not_answered 14件の中身

多くが段丘堆積物と新第三系の層。図には日本語でしか載っていない。
供給していたのは英語の翻字だけだった。

- `Rendaino terrace deposits` → 図では **蓮台野段丘堆積物**
- `Zyūmonzi Formation` → **十文字層**
- `Suenomatuyama Formation` → **末ノ松山層**
- `Kadonosawa Formation` → **門ノ沢層**

モデルは英語名を図の中に探しても見つけられない。無回答は妥当な振る舞いだった。

## 2. 実装した対策（測定は明日）

### 日本語名の併記（データ由来・推測なし）

workspace には既に検証済みの別名表があった。

`system/pdf_enrichment/unit_aliases.mapped.json`
（`japanese_aliases`、`pdf_alias_page`、`pdf_alias_quote` つき）

- 48 unit中 **26 unit** に日本語名がある
- 無回答だった14 unitのうち **8 unit** が該当

これを membership prompt に併記する。**別名表に無いunitには何も足さない**
（翻訳を推測して作らない）。列名も第2.1図の見出しどおり
西部／中央部／東部を併記する。出典は fixture の `column_name_ja_source` に記録した。

- `scripts/llm_constrained_vision.py`
  - `unit_name_ja` / `column_name_ja` があるときだけ prompt へ載せる
  - 規則を追加: 「図は日本語で印刷されている。日本語ラベルで照合せよ。
    英語名は翻字にすぎず図に無いことがある」
  - 規則を追加: 「図が示していないunitはどの列にも属さない。[] を返せ」
  - prompt version **`column-membership-batched-v1` → `v2`**
- `scripts/run_constrained_column_gold.py`
  - 別名表を読んで unit に `unit_name_ja` を付ける
- `config/llm_gold_column_vision.json`
  - `expected_columns` に `column_name_ja` と出典を追加
- `config/llm_qualification_constrained.json` / `config/llm_routing.json`
  - Column stage の契約 prompt version を v2 へ

`claude_work/tools/preview_membership_prompt.py`（新規）で、実際に送る
prompt を目視確認できる。日本語名が無いunitに捏造が無いことも確認済み。

### 描画倍率をfixtureへ

`render_scale` を fixture が持つようにした（既定2.0、現在3.0）。
実験用に `--render-scale` で上書きでき、使った値は結果文書へ記録される。

## 3. 解像度の効果（provider固定の比較）

同じ provider・同じページで倍率だけを変えた。

| 条件 | TP | precision | recall | 打ち切り |
|---|---|---|---|---|
| OpenRouter x2 | 6/42 | 0.273 | 0.143 | json_parse |
| OpenRouter x3 | 10/42 | 0.417 | 0.238 | json_parse |

倍率3.0の方が良い。ただし**どちらも途中でJSONが壊れて打ち切られており**、
打ち切り位置が違うため厳密な比較ではない。

## 4. 日本語名の効果は判定保留

| 条件 | TP | precision | recall | 返却数 |
|---|---|---|---|---|
| OpenRouter x3・prompt v1 | 10/42 | 0.417 | 0.238 | 24 |
| OpenRouter x3・prompt v2（日本語名あり） | 6/42 | 0.375 | 0.143 | 16 |

**この数字で結論を出してはいけない。** gemma-4-26b:free は毎回ちがう地点で
`json_parse` に落ち、6バッチのうち何バッチ通るかが実行ごとに変わる。
返却数が 24 と 16 では母数が違い、比較にならない。

**6バッチを完走できるprovider（Bedrock）で測るまで、日本語名の効果は不明。**
今日これ以上OpenRouterへ送っても、この不安定さは解決しない。

## 5. 明日やること（Bedrockの日次枠が戻ってから）

順番どおりに1つずつ。各stepの後に結果を確認する。

```json
// 1. preflight（外部送信なし・exit 0 を確認）
{"type":"python","script":"claude_work/tools/check_constrained_gold_attainability.py","args":[],"status":"pending"}

// 2. Column GOLD（prompt v2 + 日本語名 + x3、7 call）
{"type":"python","script":"scripts/run_constrained_column_gold.py",
 "args":["--workspace","data/02_review/05_青森/m1286_一戸 2018",
         "--provider","bedrock","--model","us.anthropic.claude-haiku-4-5-20251001-v1:0",
         "--output","data/00_management/llm_gold_bedrock_column_constrained_ja_20260813.json"],
 "status":"pending"}

// 3. 誤りの型を再分析（外部送信なし）
{"type":"python","script":"claude_work/tools/analyze_column_membership.py",
 "args":["data/00_management/llm_gold_bedrock_column_constrained_ja_20260813.json"],"status":"pending"}

// 4. Environment GOLD（第2.1図に差し替え済み、5 call）
{"type":"python","script":"scripts/run_constrained_environment_gold.py",
 "args":["--provider","bedrock","--model","us.anthropic.claude-haiku-4-5-20251001-v1:0",
         "--output","data/00_management/llm_gold_bedrock_environment_constrained_p16_20260813.json"],
 "status":"pending"}
```

その後は資格判定 → 昇格dry-run → 監査 → 回帰。
Bedrockは1日20 callなので、Column 7 + Environment 5 = 12 callに収まる。

比較する軸は次の3つ。今日の測定と同じ条件で並べれば効果が分離できる。

| 条件 | ページ | 倍率 | 日本語名 | 結果 |
|---|---|---|---|---|
| 済 | 15 | x2 | なし | TP 0（図が無いページ） |
| 済 | 16 | x2 | なし | TP 10 / P 0.250 / R 0.238 |
| 明日 | 16 | x3 | あり | ? |

## 5. 検証

- 回帰: **824件合格 / 0失敗**（通常pytest 241 + standalone 583）
  - 新規テスト `test_membership_prompt_carries_verified_japanese_labels_only`
    は、日本語名が無いunitに捏造が入らないことも固定している
- preflight: ページ束縛一致、Column 40/42、Environment 5/5
- 本日の外部送信: openrouter 28 call（無料枠40/日）、bedrock 20 call（上限到達）


---

## [制約版GOLD_v2実行結果_20260812.md]

# 制約版GOLD v2 実行結果（2026-08-12）

実行経路: Windows常駐ブリッジ（`claude_work/auto_trigger.json`）経由。
Claudeのサンドボックスからは外部送信できないため、全コマンドをホスト側で実行した。

## 1. 結論

| provider / model | stage | 判定 | 理由 |
|---|---|---|---|
| openrouter / google/gemma-4-26b-a4b-it:free | column_geography_vision | **BLOCKED** | recall_below_threshold |
| openrouter / google/gemma-4-26b-a4b-it:free | pdf_environment_multimodal | **BLOCKED** | validator_pass_rate / precision / recall / critical_failures |
| bedrock / us.anthropic.claude-haiku-4-5-20251001-v1:0 | column_geography_vision | **BLOCKED** | recall_below_threshold |
| bedrock / us.anthropic.claude-haiku-4-5-20251001-v1:0 | pdf_environment_multimodal | **BLOCKED** | validator_pass_rate / recall / critical_failures |

第3候補は**有効化しない**。本番画像routeは `Mistral → Gemini` を維持。
`activate_qualified_vision_backup.py` のdry-runは両stageとも
`activated: false / qualification_not_current / applied: false` を返した。

## 2. 送信前の確認（dry-run）

| stage | provider | 外部call | 画像 | 推定input | 予約output | modelのmax output |
|---|---|---|---|---|---|---|
| Column | openrouter | 7 | 1枚（PDF p.15, 770,228 B） | 25,219 | 6,912 | 32,768 |
| Column | bedrock | 7 | 同上 | 25,219 | 6,912 | 8,192 |
| Environment | openrouter | 5 | 2枚（p.27 / p.55） | 43,583 | 3,840 | 32,768 |
| Environment | bedrock | 5 | 同上 | 43,583 | 3,840 | 8,192 |

preflight（到達可能性）は両stageとも送信前にOK。
Column 40/42（max recall 0.952、一戸の表記揺れは許容と判断済み）、Environment 5/5。

## 3. 実測

### Column（両provider共通の結果）

両モデルとも `column_detection` で **3 Columnすべてに `present: false`** を返した。
仕様どおり membership バッチは送らず、外部callは検出1回のみ。

```
"column_detection": {"ichinohe-west": false, "ichinohe-central": false, "ichinohe-east": false}
```

- validator_decision は3 caseとも **accept**、critical_failures は **空**。
  修正前（v1）はここでrun全体が捨てられ `provider_validation` が付いていた。
  v2では「providerが見えないと答えた」という事実がそのまま記録される。
- TP 0/42、recall 0.0 → BLOCKED。

### Environment

| case | openrouter | bedrock |
|---|---|---|
| oritsumedake-fan | accept・**正解**（alluvial fan） | reject（validation） |
| esashika-fan | accept・不正解（別の環境語） | 未送信（circuit open） |
| shitazaki-sublittoral | reject（validation） | 未送信（circuit open） |
| yanagisawa-bathyal | 未送信（circuit open） | 未送信（circuit open） |
| tsukanaigawa-not-applicable | reject（json_parse） | 未送信（circuit open） |

OpenRouterは5 unit中1 unitで正解し、**`alluvial fan` がvalidatorを通過した**。
これは修正3（閉世界候補を語彙の権威とする）が実際に効いていることの確認になる。
修正前は正解語でも棄却されていた。

Bedrockは1 unit目でvalidation rejectとなり、朝の失敗が残した
`consecutive_failures=2` に1回足されてサーキットが開き、残り4 unitは無送信で停止した。

### 再送しなかった理由

サーキットで止まった unit を測るには回路をリセットして再実行する必要があるが、
`min_validator_pass_rate = 1.0` のため、この時点で両providerとも判定は
**不合格で確定**しており、追加送信では結論が変わらない。
引き継ぎ書の「同一payloadの無意味な再送をしない」に従い、再実行しなかった。

## 4. 会計（v2測定分のみ）

外部call **7回 / 合計 20,904 token**。

| provider | stage | job | status | token |
|---|---|---|---|---|
| openrouter | column | detection | accepted | 624 |
| openrouter | environment | p027 | accepted | 2,931 |
| openrouter | environment | p021 | accepted | 2,946 |
| openrouter | environment | p006 | rejected (json_parse) | 3,408 |
| openrouter | environment | p018 | rejected (validation) | 2,741 |
| bedrock | column | detection | accepted | 1,915 |
| bedrock | environment | p027 | rejected (validation) | 6,339 |

当日累計（朝のCodex分を含む）: 20 attempt / 123,680 token。
検出1回で止まる仕組みのおかげで、Column側は想定7 callが1 callで済んだ。

## 5. 新しい重要な発見: Column検出promptが実質のブロッカー

**独立した2モデル（Claude Haiku 4.5 と Gemma 4 26B）が、同じ画像に対して
3 Columnすべてを「見えない」と判定した。** これはモデル固有の弱さではなく、
prompt側の問題である可能性が高い。

`build_column_detection_prompt` には次の規則がある。

> - A time period, lithology, formation, diagram panel, or legend is not a Column.

一方、レビュー済みの正解は「PDF p.15 の図に描かれた**西部・中部・東部の3パネル**が
3つの地理Columnに対応する」というものである。つまりpromptは
「diagram panel はColumnではない」と禁じながら、正解は diagram panel を
Columnと認めることを要求している。**prompt内で矛盾している。**

加えて supplied column_name は英語（`Ichinohe District, western area`）だが、
図中の見出しは日本語（西部地域／中部地域／東部地域）と考えられる。
文字列一致を探すモデルは false を返しやすい。

### 提案（未実施・承認待ち）

1. 「diagram panel is not a Column」を、図の**凡例・柱状図の凡例パネル**に限定した
   表現へ書き換える。地理区分パネルは Column の証拠であると明示する。
2. supplied column に日本語別名（西部地域 等）を併記する。ただし別名は
   レビュー済み資料から取り、推測で作らない。
3. prompt version を `column-detection-closed-v2` に上げ、契約を分離する。
4. 変更後は preflight → dry-run → 1 provider で検出1 callだけ再測定（約600 token）。
   検出が通ってからmembershipへ進む。

GOLDに対してpromptを何度も調整すると過学習になるため、**変更は1回に限定し、
根拠（prompt内の矛盾）を持つ修正だけ**にすべきと考える。実施前に判断を仰ぐ。

## 6. 付随して直したもの

- `scripts/llm_qualification.py`
  GOLD文書のキー許可リストに `column_detection` を追加。
  値は「supplied Column ID → boolean」だけを許す型検査つき。
  これが無いと二段階Column GOLDの資格判定が `Unexpected gold keys` で落ちる。
- `.venv` に `pytest` と `pillow` を導入（プロジェクトのテスト実行に必要）。
- `claude_work/tools/run_regression.py`
  通常pytestとstandalone 6本をまとめて実行する。
  `%TEMP%\pytest-of-somas` が **Access is denied** で `tmp_path` 系が64件
  errorになる環境問題があるため、毎回新しい `--basetemp` を渡す。
  OneDrive上の `.pytest_cache` も書けないため `-p no:cacheprovider` を使う。
- `claude_work/tools/ensure_pytest.py`、`claude_work/tools/check_runtime_db.py`

## 7. 最終検証（すべてWindowsホスト上で実行）

- ルート監査 `--strict`: **0 error / 2 warning / 5 info**（warningは画像2ルートが2本構成）
- 回帰: **823件合格 / 0失敗**（通常pytest 240 + standalone 583）
  - Linuxサンドボックスで唯一落ちていた `test_bedrock_gold_payload_fits_output_capacity` も合格
- SQLite: `integrity_check = ok`、未解放 reservation **0**
- 開いているサーキット2件（どちらも30分で自動失効、本番routeには未参加）
  - `bedrock / claude-haiku-4-5 / pdf_environment_multimodal`
  - `openrouter / gemma-4-26b / pdf_environment_multimodal`

## 8. 現時点の状態

- 制約化: 本番接続まで完了、validator契約は **v2**
- 第3候補: **未有効化**（OpenRouter・Bedrockとも v2 でBLOCKED）
- 本番画像route: **Mistral → Gemini** を維持
- 次の一手: 第5節のColumn検出prompt修正（承認待ち）


---

## [json_parse耐性の修正_20260812.md]

# providerの `json_parse` 失敗への対処（2026-08-12 深夜）

## 1. 何が起きていたか

OpenRouter（gemma-4-26b:free）のmembershipバッチが毎回ちがう地点で
`json_parse` になり、6バッチを完走できなかった。

まず打ち切り（出力トークン上限）を疑ったが、実測は違った。

| job | status | 出力トークン | 予約 |
|---|---|---|---|
| membership_1 | accepted | 205 | 1024 |
| membership_2 | accepted | 205 | 1024 |
| membership_3 | **rejected (json_parse)** | 351 | 1024 |
| membership_4 | **rejected (json_parse)** | 282 | 1024 |

**上限の3分の1しか使っていない。打ち切りではない。**
成功時が一律205トークンなのに対し、失敗時は282〜351と多い。
JSONの後ろに余計な文字を足している可能性が高い。

## 2. 抽出側の欠陥

`scripts/llm_router.py` の `_parse_json_block` は、素のJSONとして
読めなかった場合に

```python
start = candidate.find("{")
end   = candidate.rfind("}")
json.loads(candidate[start:end + 1])
```

としていた。**最初の `{` から最後の `}` まで**を切り出すため、
モデルが正しいJSONを出した後に中括弧を含む注釈を足すと、
その全体を1つのJSONとして読もうとして失敗し、使える回答ごと捨てていた。

```
{"assignments":[ ... ]}          ← 正しい回答
Note: the figure uses {western, central, eastern} panels.   ← これで壊れる
```

## 3. 修正

括弧の対応を数えながら（文字列とエスケープを考慮して）
**最初に閉じた完全なオブジェクト**を取り出すようにした。

- 正しいJSONだけの応答は挙動が変わらない
- 前置き・後書き・囲み（```json）が付いても回復する
- 文字列の中の `{` `}` では分割しない
- 壊れたJSON・オブジェクトが無い応答は従来どおり `json_parse`
- 配列など非オブジェクトは従来どおり拒否

**これは輸送層だけの修正で、閉世界validatorは一切緩めていない。**
回復したJSONも、supplied ID以外を含めばvalidatorが拒否する。

テスト `claude_work/tests/test_json_block_recovery.py`（10件）で固定した。

## 4. 診断のための「形」の記録

原因究明を次回に持ち越さないよう、`json_parse` の失敗時に
**内容を含まない構造カウンタ**だけをエラーメッセージに残すようにした。

```
Provider response contained invalid JSON
  [chars=317 open_braces=3 close_braces=2 balanced_objects=0
   fenced=False starts_with_brace=True]
```

文字数・括弧数・囲みの有無・先頭が `{` か、それだけ。
prompt・response本文・unit ID・引用は一切入らない。
「生responseは永続化しない」という規則を守りつつ、
打ち切りなのか、注釈混入なのか、そもそもJSONを出していないのかを
後から区別できる。テストで漏洩しないことも固定した。

## 5. 効果の確認は保留

修正後にOpenRouterで1回試したところ、今度は**detection呼び出し自体**が
`json_parse`（出力81トークン、過去の成功時は46）になった。
gemma-4-26b:free の不安定さは今回直した1つの型だけではない。

これ以上同じproviderへ送っても新しい情報が得られないため、本日の送信は
ここで打ち切った。次回の失敗からは第4節の構造カウンタが記録されるので、
そのときに型を特定する。

## 6. 検証

- 回帰: **834件合格 / 0失敗**（通常pytest 251 + standalone 583）
- 本日の外部送信: openrouter 26 call（枠40）、bedrock 20 call（上限到達）


---

## [Column検出の真因_ページ束縛ミス_20260812.md]

# Column検出が失敗していた真因: GOLDが図の無いページに束縛されていた（2026-08-12）

## 1. 結論

Column GOLDは **PDF 15ページ**の画像をモデルへ送っていた。
このページは第2章の**本文だけ**で、図が1つも無い。

したがって Claude Haiku 4.5 と Gemma 4 26B が
「3 Columnはどれも見えない（`present: false`）」と答えたのは**正しい回答**だった。
prompt の問題でもモデルの弱さでもなく、**送っていた画像が違った**。

正しい図は **PDF 16ページ（印刷6ページ）第2.1図「一戸地域の地質総括図」**。
層序区分が **西部 / 中央部 / 東部** の3列に分かれ、各unitが箱で並んでいる。
これは `ichinohe-west / ichinohe-central / ichinohe-east` そのものである。

fixture は自分自身で矛盾していた。

```json
"pdf_page": 15,      // 実際は印刷5ページ（本文のみ）
"printed_page": 6,   // 印刷6ページ = PDF 16 = 第2.1図
```

しかも `references/m1286_pdfpages.json` の `printed` 配列に
PDF↔印刷ページの対応が最初から入っており、照合すれば検出できた。

## 2. 修正して再測定した結果（Bedrock Haiku 4.5）

| | 修正前（PDF 15） | 修正後（PDF 16） |
|---|---|---|
| column_detection | 3つとも `false` | **3つとも `true`** |
| membershipバッチ | 0回（送信せず） | **6回すべて実行** |
| 返ってきたmembership | 0 | 40 |
| TP | 0 / 42 | **10 / 42** |
| precision | 測定不能 | 0.250 |
| recall | 0.000 | 0.238 |

case別:

| case | expected | returned | TP | FP | FN |
|---|---|---|---|---|---|
| ichinohe-west | 19 | 19 | 5 | 14 | 14 |
| ichinohe-central | 8 | 13 | 3 | 10 | 5 |
| ichinohe-east | 15 | 8 | 2 | 6 | 13 |

判定は **BLOCKED**（precision・recallとも閾値未満）。
ただし中身は「構造的に測定不能」から「モデルの実力を測れている」へ変わった。
図は正しく読めており、west を19件返すなど列の規模感も掴めている。
誤りは**どのunitがどの列に属するかの取り違え**に集中している。

OpenRouter（gemma-4-26b:free）は同じ実行で `provider_rate_limit` となり測定できず。
無料枠の制限で、再測定は枠回復後。

## 3. Environment側にも同じ種類の問題がある

Environment GOLDが送っている2枚を実見した。

- **PDF 27（印刷17）**: 第3.4図「高屋敷層の柱状図」。高屋敷層はジュラ紀付加体で、
  レビュー対象5 unit（折爪岳扇状地・江刺家層・塚内川深成岩体・舌崎層・柳沢層）
  とは無関係。
- **PDF 55（印刷45）**: **本文のみ。図は無い。**

つまりEnvironmentも、図としての証拠がほぼ機能していない。
モデルは source_text の引用だけで判断させられている。

### 提案

**第2.1図（PDF 16）はEnvironmentの証拠としても最適**である。
この図には「堆積場」列があり、陸上／陸棚／漸深海／浅海／深海が
unitごとに読み取れる。レビュー済みの正解とも一致する。

- 舌崎層 → 陸棚（= sublittoral）
- 柳沢層 → 漸深海（= bathyal）
- 折爪岳扇状地堆積物 → 陸上（= alluvial fan）
- 塚内川深成岩体 → 深成岩体（= not_applicable）

Environment fixture の figures を PDF 16 中心へ差し替えることを提案する。
ただし fixture は `image_sha256` で図を束縛しており、差し替えはGOLD条件の変更にあたる。
**実施前に判断を仰ぐ。**

## 4. 変更したファイル

- `config/llm_gold_column_vision.json`: `pdf_page` 15 → **16**
  （原本は `claude_work/patches/backup_20260812/` に保存）
- `claude_work/tests/test_column_vision_gold.py`: 期待ページを16へ、理由をコメントで明記
- `claude_work/tools/check_constrained_gold_attainability.py`:
  **ページ束縛チェックを追加**。fixtureの `pdf_page` が
  workspace の PDF↔印刷ページ対応と食い違えば exit 1 で外部送信を止める。
  今回の事故はこの1行の照合で防げた。
- 新規: `claude_work/tools/render_pdf_page.py` / `render_gold_figure.py`
  （GOLDと同条件で任意ページを書き出す。目視確認用、外部送信なし）

## 5. 検証

- preflight: `page binding: PDF 16 -> 印刷 6 / fixture宣言 6 … 一致`、Column 40/42、Environment 5/5
- 回帰: **823件合格 / 0失敗**（通常pytest 240 + standalone 583）
- 資格記録: `column_geography_vision bedrock:... BLOCKED (precision, recall)`

## 6. 次にやるべきこと

1. Environment fixture の figures を第2.1図へ差し替えるか判断する（第3節）。
2. Column の誤りは「列の取り違え」に集中している。membership promptで
   図の左右位置（西部＝左、中央部＝中、東部＝右）を明示するのが次の候補。
   ただしこれはprompt変更なので version を上げ、1回だけ試す。
3. OpenRouterは無料枠回復後に再測定する。


---

## [ABC実施結果_図と解像度の修正_20260812.md]

# A・B・C 実施結果（2026-08-12）

A: Environmentの証拠図を第2.1図へ差し替え
B: Column画像の解像度を上げる
C: OpenRouterを修正後の条件で再測定

## 1. 効果まとめ

### Column（PDF 15 → 16、scale 2.0 → 3.0）

| 条件 | detection | membership実行 | TP | precision | recall |
|---|---|---|---|---|---|
| 修正前（PDF 15 / x2）Bedrock | 3つとも false | 0/6バッチ | 0/42 | — | 0.000 |
| ページ修正（PDF 16 / x2）Bedrock | 3つとも **true** | 6/6 | 10/42 | 0.250 | 0.238 |
| ページ+解像度（PDF 16 / x3）Bedrock | 3つとも true | 1/6で日次上限 | 4/42 | 0.333 | 0.095 |
| ページ+解像度（PDF 16 / x3）OpenRouter | 3つとも true | 途中でjson_parse | 10/42 | **0.417** | 0.238 |

解像度の効果は**まだ結論を出せない**。Bedrockはmembership 1バッチ目で
日次呼び出し上限（20回/日）に達して打ち切られ、6バッチ分の測定になっていない。
OpenRouterはprecisionが 0.250 → 0.417 に上がったが、providerが違うので
解像度の寄与とモデルの違いを分離できていない。

**Bedrockの日次枠が戻ってから、PDF 16 / x3 で6バッチ完走させるのが次の測定。**

### Environment（PDF 55 → 16）

| 条件 | 完全一致 | validator accept | サーキット |
|---|---|---|---|
| 修正前（55 + 27）OpenRouter | 1/5 | 2/5 (0.4) | 4件目で開いた |
| **図の差し替え後（16 + 27）OpenRouter** | **2/5** | **3/5 (0.6)** | 開かず、5 unit全て送信 |

新たに正解したのは `tsukanaigawa-not-applicable`。
第2.1図に「一戸深成岩体／塚内川深成岩体」が明示されているため、
非堆積性と判断できるようになった。図の差し替えが直接効いている。

判定は両stageとも **BLOCKED** のまま（precision 1.0 / pass rate 1.0 が要件）。
第3候補は有効化していない。本番画像routeは `Mistral → Gemini` を維持。

## 2. 変更内容

### A. Environment証拠図

`claude_work/tools/bind_environment_figure_p16.py`（新規、`--apply` で書き込み）

- PDF 16 を scale 3.0 でレンダリングし、環境図ディレクトリへ配置
  `page_0016_candidate.png` / sha256 `bcc00d86…`
- `environment_figure_candidates.json` に最優先候補として追記
- `config/llm_gold_environment.json`
  - `figures`: `[55, 27]` → **`[16, 27]`**
  - `figure_manifest_sha256` を再計算 `4ff15596…`
- PDF 27（高屋敷層の柱状図）は**あえて残した**。無関係な図を混ぜたときに
  誤って引用しないかを測るため。

### B. Column解像度

`scripts/run_constrained_column_gold.py`

- 描画倍率をコードの定数からfixtureへ移した（`render_scale`、既定2.0）。
  GOLDの条件はfixtureが持つべきで、コード変更なしに再現できる必要がある。
- `config/llm_gold_column_vision.json` に `"render_scale": 3.0` を追加。

画像は 1191×1684 → 1786×2526、画像トークンは 3,072 → 6,144/call。
推定input 25,219 → 46,723（7 call合計）。

### C. OpenRouter再測定

無料枠のrate limitが解けたため、Column・Environmentとも修正後の条件で実測できた。
結果は第1節のとおり。

## 3. Bedrockの日次上限に当たった件

`config/llm_routing.json` の `providers.bedrock.limits.max_calls_per_day = 20`。
本日のBedrock attempt はちょうど 20 で、B の測定が
`provider_budget` で打ち切られた。**上限は勝手に上げていない。**

選択肢は2つ。

1. 日付が変わってから（day_bucket 単位でリセット）測り直す。追加費用なし。
2. `max_calls_per_day` を引き上げる。課金方針の判断が必要。

## 4. 検証

- preflight: `page binding: PDF 16 -> 印刷 6 / fixture宣言 6 … 一致`、Column 40/42、Environment 5/5
- 回帰: **823件合格 / 0失敗**（通常pytest 240 + standalone 583）
  - `test_environment_gold.py` の束縛ページ期待値を `{27,55}` → `{16,27}` に更新
    （理由をコメントで明記）
- SQLite: `integrity_check = ok`、未解放 reservation 0
- 本日の外部送信合計: 40 attempt / 168,400 token
  （bedrock 101,187ほか。うち本セッションのA・B・C分は約47,000）
- 開いているサーキット: bedrock/pdf_environment_multimodal 1件のみ（自動失効、本番route未参加）

## 5. 次にやること

1. **Bedrockの枠回復後、PDF 16 / x3 でColumn GOLDを完走させる**（7 call）。
   これで初めて解像度の効果を正しく比較できる。
2. Bedrock Environment も新しい図で再測定する（5 call）。
3. その結果を見てから、membership promptに図の左右位置
   （西部＝左、中央部＝中、東部＝右）を入れるか判断する。
   prompt変更はGOLDへの過学習になりやすいので、データ側の改善を出し切ってから。


---

## [画像LLM三本化_Claude作業記録_20260812.md]

# 画像LLM三本化 Claude作業記録（2026-08-12）

引き継ぎ元: `claude_work/reports/画像LLM三本化_引き継ぎ_20260812.md`

## 0. 結論（先に）

**Bedrock制約版GOLDの TP 0/42・TP 0/5 は、provider品質の問題ではなくハーネス側の欠陥である。**
外部送信をせずに3件の欠陥を特定し、修正し、フェイクrouterによる通し検証で
「完璧な回答なら閾値を満たす」ことを確認した。

修正前は、Environment制約GOLDは**どんなproviderでも構造的に不合格**だった
（到達可能 3/5、max recall 0.600 < 閾値 0.85）。修正後は 5/5、max recall 1.000。

外部送信が必要な工程（OpenRouter GOLD）は未実行のまま。理由は次節。

## 1. 外部送信ができない理由（実測）

Claudeの実行環境は全HTTP通信がプロキシ経由で、ドメイン許可リストにより制御される。

| ドメイン | HTTP |
|---|---|
| `pypi.org` | 200 |
| `api.anthropic.com` | 401（到達はする） |
| `openrouter.ai` | 403（プロキシがCONNECTを拒否） |
| `api.mistral.ai` | 403 |
| `generativelanguage.googleapis.com` | 403 |

この許可リストはClaude側の実行基盤が強制するもので、プロジェクト設定でもAPI keyでも
解除できない。curl・python・別ライブラリでの迂回は行っていない（規約上も禁止）。
したがってOpenRouter GOLDはユーザーのWindows環境か、枠回復後のCodexで実行する。
そのための runbook を用意した（第5節）。

## 2. 特定した欠陥

### 欠陥1: Column検出の `present:false` を provider障害として扱っていた

`scripts/run_constrained_column_gold.py` の `validate_detection` は、
supplied columnのいずれかが `present:false` だと `ValidationReport(decision="reject")`
を返していた。GOLDは `max_attempts=1 / max_failovers=0` なので、これは即
`AllProvidersFailed` となり、**membership 6バッチが一度も送られずrun全体が捨てられた**。

`present:false` はpromptが明示的に許した回答であって、プロトコル違反ではない。
2026-08-12のBedrock実測（output 101 token、error_kind=validation）はこの経路と整合する。

**修正**: プロトコル違反（未知ID・重複・非boolean・欠落）だけをrejectにする。
`present:false` はacceptし、そのColumnを membership の閉世界から外す。
見逃したColumnはrecallで罰せられるが、残り42件の測定は続く。
Columnが1つも検出されなければ membership は送らない（無駄な外部送信をしない）。
検出結果は sanitized な boolean として GOLD 文書の `column_detection` に記録する。

### 欠陥2: Environmentの `unresolved` を provider障害として扱っていた

`scripts/run_constrained_environment_gold.py` は `accepted` が空なら常にrejectしていた。
`unresolved` はpromptが明示的に許した回答なのに provider障害として計上され、
**同一stageのサーキットブレーカーが2 unitで開き、残り3 unitは無送信のまま停止**した。
引き継ぎ書の「先頭2 unitがvalidation reject、残り3 unitを無送信で停止」はこれ。

**修正**: モデルが自ら申告した `unresolved`（`dropped` が無く、
`model_omitted_target_unit` でもない）はacceptとして扱い、GOLDでは「取りこぼし」
として採点する。unit省略・ID捏造・evidence未検証は従来どおりprovider障害。

### 欠陥3: レビュー済みGOLDの正解語を validator が語彙ゲートで棄却していた

`scripts/pdf_environment.py` の `_verified_environment` は、Macrostrat公式
environments表（83語）に無い語を常に棄却していた。しかしGOLD fixture
`config/llm_gold_environment.json` の正解には `sublittoral`（Shitazaki Formation）と
`bathyal`（Yanagisawa Formation）が含まれ、**どちらも公式表に無い**。

つまり2/5のcaseは、モデルが正解しても必ず棄却された。到達可能 3/5、
max recall 0.600 は閾値0.85を下回るため、**構造的に合格不能**だった。

これはプロジェクト自身の方針とも矛盾する。`config/vocab.json` の `_注意` は
Macrostrat公式仕様の environment が "free text ... or Macrostrat environment" であり、
公式表に無い語を使ってはいけないわけではない、と明記している。

**修正**: `vocab` 引数を「その呼び出しにおける唯一の閉世界」として扱う。
本番stageは従来どおり公式83語を渡すので**挙動は不変**。制約版は
レビュー由来の限定候補リストを渡すので、その中の語だけが通る。
候補リスト外の自由記述（`deep marine` 等）と複数値（`;` `,`）は従来どおり棄却。

### 欠陥4（未修正・要判断）: 一戸のunit名の綴り不一致

GOLD fixture の期待値2件が、モデルへ渡す48 unitのcanonical inventoryから
到達できない。原因は同一unitの綴り違い。

| 出所 | 綴り |
|---|---|
| `claude_work/reports/Ichinohe_reference_GOLD.xlsx`（レビュー済み） | `Floodplain and valley-floor deposits` |
| 作業用 canonical inventory（48 unit） | `flood-plain and valley-floor deposits` |

`_normalise_name` はハイフンを空白に置換するため `floodplain` ≠ `flood plain` となり、
membership IDのSHA256が変わる。west・central の2件が到達不能で、
**Column GOLDの上限recallは 40/42 = 0.952**（閾値0.85は超えるので不合格にはならない）。

どちらの綴りが原典（5万分の1地質図幅「一戸」）に忠実かはデータの判断なので、
推測で直さずに報告する。選択肢は次の3つ。

1. canonical inventory 側の綴りをレビュー済み workbook に合わせる。
2. `_normalise_name` を区切り記号非依存にする（全membership IDの再生成が必要）。
3. 現状維持（上限recall 0.952 を許容し、preflightで毎回可視化する）。

いまは3の状態で、preflightが2件を必ず表示する。

## 3. 検証

### 到達可能性 preflight（新規）

`claude_work/tools/check_constrained_gold_attainability.py`

| stage | 修正前 | 修正後 |
|---|---|---|
| `column_geography_vision` | 40/42（max recall 0.952） | 40/42（0.952） |
| `pdf_environment_multimodal` | 3/5（max recall 0.600）→ **送信禁止判定** | 5/5（1.000）→ OK |

外部枠を消費する前に必ず実行する。exit code 1 なら送信してはいけない。
（Windows上で実行すること。manifestに絶対パスが入っているため。）

### 通し検証テスト（新規）

`claude_work/tests/test_constrained_gold_recovery.py` — 8件。
フェイクrouterでvalidatorを本番と同じ順序で呼び、外部送信ゼロで通す。

- 修正前のコードに対して **5件が失敗**（欠陥1・2・3を再現）
- 修正後は **8件すべて合格**

特に `test_perfect_provider_reaches_qualification_thresholds` は、完璧な回答で
precision 1.000 / recall 0.952 に到達することを示す。
`test_perfect_environment_answers_pass_every_case` は修正前は失敗する
（＝完璧な回答でも不合格だった証拠）。

### 回帰

- 通常pytest: **239合格 / 1環境依存失敗**
  - 失敗は `test_column_vision_gold.py::test_bedrock_gold_payload_fits_output_capacity`
  - 原因は Linux サンドボックスに Windows 同梱のPDFレンダラが無いこと（`Bundled PDF render runtime is unavailable`）。Windows では合格する。
- standalone 6本: **583合格 / 0失敗**（25 + 19 + 27 + 55 + 17 + 440）
- 合計 **822合格**（引き継ぎ時点815 + 新規8 − 環境依存1）
- `llm_route_audit.py --strict`: **0 error / 2 warning / 5 info**（変化なし。warningは画像2ルートが2本構成であることの正しい検出）
- `PRAGMA integrity_check` = ok、active reservation 0

### 会計・サーキット状態

- 外部送信は1件も行っていない。SQLiteの `attempts` に新規行なし。
- `bedrock / pdf_environment_multimodal` のサーキットは 2026-08-12T05:51:24Z にopen、
  失効 06:21:24Z。現在は失効済みで、以後の実行を妨げない。

## 4. 変更したファイル

すべて `claude_work/patches/backup_20260812/` に**変更前の原本を保存**した。
差分は `claude_work/patches/20260812_constrained_gold_fixes.patch`。

| ファイル | 変更 |
|---|---|
| `scripts/pdf_environment.py` | `_verified_environment` を呼び出し側語彙優先に（本番挙動は不変） |
| `scripts/llm_constrained_vision.py` | `CONSTRAINED_VALIDATOR_VERSION` を v1 → **v2** |
| `scripts/run_constrained_column_gold.py` | `present:false` を採点対象化、`column_detection` を記録 |
| `scripts/run_constrained_environment_gold.py` | 申告済み `unresolved` を採点対象化 |
| `config/llm_qualification_constrained.json` | validator契約を v2 に |
| `config/llm_routing.json` | OpenRouter候補2件の `qualification_validator_version` を v2 に、理由を追記（`enabled:false` のまま） |
| `claude_work/tests/test_activate_qualified_vision_backup.py` | 契約テストの版数を v2 に |

新規:

- `claude_work/tools/check_constrained_gold_attainability.py`
- `claude_work/tests/test_constrained_gold_recovery.py`
- `claude_work/runbooks/openrouter_constrained_gold_20260812.ps1`

**validator版が v2 になったため、2026-08-11〜12に作られた v1 の資格記録
（`data/00_management/llm_qualification_constrained/` の2件）は契約不一致となり、
昇格判定に使えない。** これは意図した挙動で、記録は削除していない。
どのproviderも再測定が必要。Bedrockは v1 で不合格だったが、その不合格は
上記欠陥を含む条件下のものなので、**v2での再測定にはやり直しの価値がある**。

## 5. 次の実行（外部枠が使えるようになったら）

Windows PowerShell で、まず送信なしの確認:

```powershell
cd "C:\Users\somas\OneDrive\デスクトップ\summer research 2026\MacroStrat"
.\claude_work\runbooks\openrouter_constrained_gold_20260812.ps1
```

preflight・ルート監査・両stageのdry-run（対象／画像数／推定input／予約output／
provider・model）が表示される。内容に納得したときだけ:

```powershell
.\claude_work\runbooks\openrouter_constrained_gold_20260812.ps1 -Approve
```

runbookは資格記録の作成と昇格の**dry-runまで**で必ず止まる。
`currently_qualified=true` を目視した stage だけ、`--apply` を手動で付けて有効化する。

Bedrockを v2 で再測定する場合は同じrunbookの provider/model を差し替える:

```powershell
.\claude_work\runbooks\openrouter_constrained_gold_20260812.ps1 `
  -Provider bedrock -Model us.anthropic.claude-haiku-4-5-20251001-v1:0
```

（Bedrockのprobe結果は `bedrock_vision_probe_*_20260811.json` を使うため、
資格判定の `--probe-results` はrunbook内の既定値から手で変える必要がある。）

## 6. やっていないこと

- 外部API送信（プロキシが403で不可能。迂回もしていない）
- 閾値の緩和（`min_precision` 1.0 / `min_recall` 0.85 / `max_critical_failures` 0 は不変）
- GOLD正解値の書き換え（欠陥4は報告のみ）
- 既存資格記録・GOLD結果JSONの削除や上書き
- prompt本文の変更（3件の欠陥はすべてvalidator／runner側だったため）


---

## [画像LLM三本化_引き継ぎ_20260812.md]

# 画像LLM三本化 引き継ぎ（2026-08-12）

## 完成条件（朝まで）

1. Column Visionを「Column検出」と「unit所属判定」に分離する（本番接続済み）。
2. PDF Environmentをunitごとの限定候補分類へ変更する（本番接続済み）。
3. 分割版を既存Bedrock Claude Haiku 4.5でGOLD再評価する。
4. OpenRouterの無料Vision候補を公式model metadataから選び、固定画像probeとGOLDを行う。
5. GOLD合格モデルだけを画像ルートの第3候補へ入れ、順序を
   `Mistral → Gemini → 第3候補` とする。
6. 失敗候補は無効のまま、理由・利用量・資格判定を永続化する。
7. 障害訓練、ルート監査、SQLite integrity、全回帰を合格させる。

## 厳守事項

- `config/secret.json` は存在確認だけに使い、内容を標準出力・ログ・報告書へ出さない。
- API key、prompt、画像base64、生responseは永続化しない。
- 外部送信前にdry-runで対象、画像数、推定input、予約output、provider/modelを確認する。
- GOLDはprovider単独、`max_attempts=1`、`max_failovers=0`。
- connectivityやproduction validator通過だけで有効化しない。必ず資格GOLDの閾値を満たすこと。
- 不合格時に閾値を緩めない。同一payloadの無意味な再送もしない。
- 既存のユーザー変更を消さない。編集は`apply_patch`を使う。

## 現在地

### 運用ルート

- alias: Groq → Azure → Cohere
- body: Mistral API → Bedrock Mistral Large 3 → Cohere
- bootstrap: Mistral API → Bedrock Mistral Large 3 → Cohere
- Abstract: Mistral API → Cohere → Gemini
- Column Vision: Mistral API → Gemini（2本）
- PDF Environment: Mistral API → Gemini（2本）

共通層は`LLMRouter`、SQLite会計、出力容量ゲート、サーキットブレーカー、
validator付き順次failoverへ統合済み。監査は`0 error / 2 warning / 5 info`。
warningは画像2ルートだけが2本構成であること。

### Bedrock Claude Haiku 4.5の実測

モデル: `us.anthropic.claude-haiku-4-5-20251001-v1:0`

- AWS Anthropic申請は反映済み。
- 固定1画像probe: HTTP 200。
- 固定2画像probe: HTTP 200、multi-image対応。
- Column Vision GOLD: TP 31/42、FP 24、precision 0.563636、recall 0.738095でBLOCKED。
- PDF Environment GOLD: TP 1/5、FP 1、validator pass rate 0.4、precision 0.5、recall 0.2でBLOCKED。
- サーキットはCLOSED。接続ではなくタスク品質が問題。

秘密安全な結果:

- `data/00_management/llm_gold_bedrock_column_vision_20260811.json`
- `data/00_management/llm_gold_bedrock_environment_20260811.json`
- `data/00_management/bedrock_vision_probe_1img_postapproval_retry_20260811.json`
- `data/00_management/bedrock_vision_probe_2img_postapproval_20260811.json`

### 直近の重要修正

- `scripts/run_column_vision_gold.py`にdry-runとreviewed 48-unit inventory SHA束縛を追加。
- `scripts/llm_runtime.py`のstatusに`reported_tokens`と`estimated_error_tokens`を分離表示。
- `scripts/llm_router.py --reset-circuit`がコロンを含むBedrock model IDを正しく扱うよう修正。
- 通常pytest 221件 + standalone 583件 = 804件合格。

### 2026-08-12に追加した実装

- `scripts/llm_constrained_vision.py`
  - Column検出、最大8 unitの所属判定、unit単位Environment限定分類。
  - supplied ID・候補以外を拒否するclosed-world validator。
- `scripts/run_constrained_column_gold.py`
  - 1画像、Column検出1回、48 unitを8件ずつ6回、provider単独、再試行・failoverなし。
- `scripts/run_constrained_environment_gold.py`
  - reviewed 5 unitを1 unitずつ、2画像固定、provider単独、再試行・failoverなし。
- `scripts/list_openrouter_vision_models.py`
  - 公式model metadataから固定`:free`、画像入力、context/output容量を満たす候補だけを抽出。
- `scripts/probe_llm_providers.py`
  - OpenRouterの固定Vision modelと`--model`指定に対応。
- `config/llm_qualification_constrained.json`
  - 制約版prompt/validator専用の資格閾値。旧一括promptの資格と混同しない。
- `claude_work/tests/test_llm_constrained_vision.py`
- `claude_work/tests/test_openrouter_vision_models.py`
- `scripts/llm_column_vision.py`
  - `constrained=True`でColumn検出1 callと最大8 unitのmembership batchへ分離。
  - batchごとに独立して共通routerを通し、providerが混在した場合は`providers_used`をmanifestへ記録。
  - age・地域引用をモデルに要求せず、sort orderはcanonical入力順から決定的に生成。
- `scripts/pdf_environment.py`
  - `constrained=True`で1 request = 1 unit。unresolved/invalid responseは次providerへ送り、1 unitの失敗で全targetを捨てない。
- `scripts/pilot.py`
  - 両画像stageを`constrained=True`で呼び出す本番入口へ変更。
- `scripts/llm_route_audit.py`
  - `qualification_required`候補に、資格記録の存在とexact prompt/validator一致を強制。
  - 制約版資格ディレクトリ用`--additional-record-dir`を追加。
- `scripts/activate_qualified_vision_backup.py`
  - sanitized資格記録だけを読み、exact contractが現在有効なstageだけを独立して第3候補へ昇格。
  - dry-runが既定。資格欠落・期限切れ・version不一致では設定を書き換えない。

## 実装方針

### A. Column Vision二段階化

同一画像を使うが、出力責務を分ける。

1. `column_detection`
   - 入力: 画像、review済みexpected column ID/name、短い地域記述。
   - 出力: supplied columnごとの`present: bool`と画像上の短い根拠。
   - unit一覧・年代・座標・自由なcolumn追加を禁止。
2. `column_unit_membership`
   - 入力: 画像、確定column ID、unitを小バッチ（推奨8件以下）。
   - 出力: 各unitについてsupplied column IDのboolean配列だけ。
   - unit追加、column追加、sort order、年代、地域説明を禁止。
   - バッチ結果を決定的に結合し、既存`validate_response`相当へ渡す。

GOLDではColumn検出3件とmembership 42件を別々に採点する。最終資格は
membership precision 1.0、recall 0.85以上、critical 0を維持する。

### B. Environmentのunit単位限定分類

- 1 request = 1 unit（または最大2 unit）。
- 入力は該当unitの短いcontextと関連figureのみ。
- 出力候補はfixture/validatorが許す限定集合と`not_applicable`だけ。
- exact evidence span/figure IDを必須にする。
- 自由記述のenvironmentを禁止し、候補外はvalidator reject。
- 5 unitの結果を決定的に集約して既存GOLD形式へ変換する。

### C. OpenRouter

- `config/llm_routing.json`には固定Vision候補を両画像routeへ登録済み。ただしGOLD未完了のため`enabled:false`。
- `scripts/probe_llm_providers.py`はOpenRouterの固定Vision modelに対応済み。
- 公式`GET https://openrouter.ai/api/v1/models`のmetadataから、無料(`:free`)、
  image input対応、必要context/output上限を満たすモデルを列挙する。
- ルータに画像candidateを登録する前に固定1画像・2画像probeを行う。
- model IDを固定し、`openrouter/free`の自動選択をGOLDに使わない。
- actual modelがrequested modelと一致しない場合は資格不合格。

## 推奨ファイル構成

- `scripts/llm_column_detection.py`
- `scripts/llm_column_membership.py`
- `scripts/pdf_environment_classify.py`
- `scripts/run_constrained_column_gold.py`
- `scripts/run_constrained_environment_gold.py`
- `scripts/list_openrouter_vision_models.py`
- 対応する`claude_work/tests/test_*.py`
- 既存stageを置換する場合も、cache schema/prompt/validator versionを必ず更新する。

既存production入口は`pilot.py`から変更し、旧一括処理へのsilent fallbackは作らない。
移行途中はfeature flagで旧ルートを保ってもよいが、新旧responseを混ぜない。

## 実行順

1. 既存prompt/validator/cache境界を調査し、上記3モジュールの純粋関数とfake routerテストを先に作る。
2. production orchestratorへ接続し、外部通信なしのfixtureテストを通す（完了）。
3. Bedrock constrained GOLDのdry-run→単独live→資格記録。
4. OpenRouter model metadata取得→候補ランキング→固定probe。
5. OpenRouter constrained GOLD→資格記録。
6. 合格したstageだけ第3候補を追加し`max_failovers=2`。不合格stageは2本構成維持。
7. `qualification_required`とexact prompt/validatorをroute auditで確認する。
8. `scripts/llm_route_audit.py --strict --json`。
9. 通常pytest（standalone 6本をignore）とstandalone 6本を実行。
10. `PRAGMA integrity_check`とactive reservations=0を確認。
11. 本書の「結果」節を更新する。

## テストコマンド

```powershell
$env:PYTHONIOENCODING='utf-8'
python scripts/llm_route_audit.py --strict --json
python -m pytest claude_work/tests -q `
  --ignore=claude_work/tests/test_interval_whitelist.py `
  --ignore=claude_work/tests/test_vision_retry.py `
  --ignore=claude_work/tests/test_partial_column_assignment.py `
  --ignore=claude_work/tests/test_llm_retry.py `
  --ignore=claude_work/tests/test_unit_id_uniqueness.py `
  --ignore=claude_work/tests/test_roundtrip.py
```

Standalone:

- `test_interval_whitelist.py` = 25
- `test_vision_retry.py` = 19
- `test_partial_column_assignment.py` = 27
- `test_llm_retry.py` = 55
- `test_unit_id_uniqueness.py` = 17
- `test_roundtrip.py` = 440

## 2026-08-12 実行結果

### Bedrock制約版GOLD

- Column: **BLOCKED**。Column検出responseがclosed-world validationを通らず、membership 6バッチは送らなかった。TP 0/42、critical 3。
- Environment: **BLOCKED**。先頭2 unitがvalidation reject、同一stageのサーキットブレーカーが開き残り3 unitを無送信で停止。TP 0/5、critical 5。
- 結果:
  - `data/00_management/llm_gold_bedrock_column_constrained_20260812.json`
  - `data/00_management/llm_gold_bedrock_environment_constrained_20260812.json`
  - `data/00_management/llm_qualification_constrained/`
- 今日のBedrock会計は9 attempt、71,718 token。制約版の実送信はColumn検出1回とEnvironment 2 unitだけで、後続はvalidator/circuit breakerが遮断した。

### OpenRouter候補とprobe

公式metadataの無料Vision候補は5件。汎用候補の上位は次のとおり。

1. `google/gemma-4-26b-a4b-it:free` — context 262,144、max output 32,768、structured output対応。
2. `google/gemma-4-31b-it:free` — context 262,144、max output 32,768、structured output対応。
3. `nvidia/nemotron-nano-12b-v2-vl:free` — context 128,000、max output 128,000、structured output非対応。
4. `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` — context 256,000、max output 65,536、structured output非対応。
5. `nvidia/nemotron-3.5-content-safety:free` — safety専用なので第3候補から除外。

固定候補は`google/gemma-4-26b-a4b-it:free`。1画像probeと2画像probeはいずれもHTTP 200、requested/actual model一致で合格。

- `data/00_management/openrouter_free_vision_candidates_20260812.json`
- `data/00_management/openrouter_gemma4_vision_probe_1img_20260812.json`
- `data/00_management/openrouter_gemma4_vision_probe_2img_20260812.json`

### 残る外部送信ブロッカー

OpenRouterへ一戸の非公開GOLD payloadを送る実行は、実行環境の安全審査が個別承認不足として拒否した。固定probeだけでは資格化しないため、OpenRouterは無効のまま。再開にはユーザーから次の明示承認が必要。

> 一戸PDF 15ページ抽出画像1枚・48 unit英語名・3 Column名、およびPDF 27/55ページ画像2枚・レビュー済み5 unit情報を、OpenRouter経由の固定モデル google/gemma-4-26b-a4b-it:free へ制約付きGOLD評価のため送信することを承認します。

さらに2026-08-12時点でCodexの外部実行枠が上限に達し、次回利用可能表示は**2026-08-18 10:20 AM（America/Chicago）**。Mistral制約版GOLDの実行開始もこの上限で拒否された。Mistral/Gemini dry-runは完了し、各providerでColumn 7 call（推定input 25,219、予約output 6,912）、Environment 5 call（推定input 43,583、予約output 3,840）。回避実行は禁止。明示承認があっても、利用枠回復後に再開する。

利用枠回復・承認後の工程は次の4段階。

1. Column制約GOLDをOpenRouter単独1回で実行し、資格記録を作る。
2. Environment制約GOLDをOpenRouter単独で実行し、資格記録を作る。
3. 各stageを独立に資格判定し、合格したstageだけ`Mistral → Gemini → OpenRouter`、`max_failovers=2`にする。不合格stageは無効を維持する。
4. failover drill、exact-contract route audit、SQLite、全回帰を再実行する。

### 現時点の安全な結論

- 制約化: **本番接続まで完了**。Columnは検出＋membership batch、Environmentはunit単位。旧一括cacheとはjob ID・prompt/validator versionを分離。
- 第3候補: **未有効化**。Bedrockは不合格、OpenRouterはprobe合格・GOLD承認待ち。
- 本番画像route: **Mistral → Gemini**を維持。
- route audit: **0 error / 2 warning / 5 info**。2 warningは画像routeが2本であることを正しく検出。
- SQLite: `PRAGMA integrity_check = ok`、active reservation 0。
- 回帰: **815件合格**。通常pytest 232件、standalone 583件（25 + 19 + 27 + 55 + 17 + 440）。
- failover drill: 429同一provider再試行、503次provider、JSON rejectから第3provider、open circuit skip、全候補失敗、資格記録欠落、contract不一致を含む8 testが合格。

## 最終確認コマンド（Claude再開用）

```powershell
$env:PYTHONIOENCODING='utf-8'

# 承認後: OpenRouter Column GOLD
python scripts/run_constrained_column_gold.py `
  --workspace "data/02_review/05_青森/m1286_一戸 2018" `
  --provider openrouter `
  --model google/gemma-4-26b-a4b-it:free `
  --output data/00_management/llm_gold_openrouter_column_constrained_20260812.json

# 承認後: OpenRouter Environment GOLD
python scripts/run_constrained_environment_gold.py `
  --provider openrouter `
  --model google/gemma-4-26b-a4b-it:free `
  --output data/00_management/llm_gold_openrouter_environment_constrained_20260812.json

# それぞれprobe結果と制約版policyを使って資格判定する。
python scripts/llm_qualification.py `
  data/00_management/llm_gold_openrouter_column_constrained_20260812.json `
  --probe-results data/00_management/openrouter_gemma4_vision_probe_1img_20260812.json `
  --policy config/llm_qualification_constrained.json `
  --record-dir data/00_management/llm_qualification_constrained `
  --record

python scripts/llm_qualification.py `
  data/00_management/llm_gold_openrouter_environment_constrained_20260812.json `
  --probe-results data/00_management/openrouter_gemma4_vision_probe_2img_20260812.json `
  --policy config/llm_qualification_constrained.json `
  --record-dir data/00_management/llm_qualification_constrained `
  --record
```

手編集ではなく、まずdry-runでstage別の判定を見る。

```powershell
python scripts/activate_qualified_vision_backup.py `
  --record-dir data/00_management/llm_qualification_constrained `
  --provider openrouter `
  --model google/gemma-4-26b-a4b-it:free

# 判定内容が正しい場合だけ同じコマンドへ --apply を追加する。
```

各stageの資格判定で`currently_qualified=true`を確認する前に、そのstageのrouteで`enabled`や`max_failovers`を変更してはならない。有効化後の監査は次を使う。

```powershell
python scripts/llm_route_audit.py --strict `
  --additional-record-dir data/00_management/llm_qualification_constrained
```

## 2026-08-12 Claudeによる引き継ぎ（追記）

詳細は `claude_work/reports/画像LLM三本化_Claude作業記録_20260812.md`。

- Bedrock制約版GOLDの TP 0 は provider品質ではなく**ハーネス側の3欠陥**が原因だった。
  Column検出の`present:false`とEnvironmentの`unresolved`をprovider障害として扱い
  run全体を捨てていたこと、レビュー済み正解語 `sublittoral` / `bathyal` を
  Macrostrat公式表に無いという理由だけでvalidatorが棄却していたこと。
  修正前のEnvironment制約GOLDは**どのproviderでも構造的に不合格**だった（max recall 0.600）。
- 3件とも修正済み。フェイクrouterによる通し検証8件を新規追加（修正前は5件失敗）。
  回帰822合格、route audit `0 error / 2 warning / 5 info`、SQLite ok・reservation 0。
- **`CONSTRAINED_VALIDATOR_VERSION` は v1 → v2**。v1の資格記録は契約不一致となり
  昇格に使えない（記録は削除していない）。全provider再測定が必要。
- 外部送信は1件も行っていない。Claudeの実行環境はプロキシが
  `openrouter.ai` / `api.mistral.ai` へのCONNECTを403で拒否するため送信不可能。
- 外部枠が使えるようになったら
  `claude_work/runbooks/openrouter_constrained_gold_20260812.ps1` を使う。
  承認ゲート・到達可能性preflight・dry-run先行を内蔵し、昇格はdry-runで止まる。
- 未判断で残した1件: 一戸のunit名 `Floodplain and valley-floor deposits`（レビュー済み）と
  `flood-plain and valley-floor deposits`（canonical inventory）の綴り不一致。
  Column GOLDの上限recallが 40/42 = 0.952 に下がる。→ **2026-08-12 ユーザー判断: 許容**。

## 2026-08-12 v2測定の結果（追記）

詳細は `claude_work/reports/制約版GOLD_v2実行結果_20260812.md`。

- OpenRouter（gemma-4-26b:free）・Bedrock（Haiku 4.5）とも、Column・Environmentの
  両stageで **BLOCKED**。第3候補は有効化せず、本番画像routeは `Mistral → Gemini` 維持。
- 外部call 7回 / 20,904 token。Column検出で止まる設計のため想定より大幅に少ない。
- **Environment修正の効果を実測で確認**: OpenRouterが `alluvial fan` で1 unit正解し、
  レビュー済み語がvalidatorを通過した。修正前は正解でも棄却されていた。
- **新発見**: 独立した2モデルが3 Columnすべてを `present:false` と判定した。
  `build_column_detection_prompt` は「diagram panel はColumnではない」と禁じているが、
  正解は図中の西部・中部・東部パネルをColumnと認めることを要求しており、
  prompt内で矛盾している。次の一手はこのprompt修正（`column-detection-closed-v2`）。
- 最終検証（Windows）: ルート監査 0 error / 2 warning / 5 info、
  回帰 **823件合格 / 0失敗**、SQLite ok・reservation 0。

## 2026-08-12 夜 追記（真因はページ束縛ミスだった）

詳細は次の3本。

- `claude_work/reports/Column検出の真因_ページ束縛ミス_20260812.md`
- `claude_work/reports/ABC実施結果_図と解像度の修正_20260812.md`
- `claude_work/reports/誤りの型分析と日本語名の付与_20260812.md`

- **Column GOLDは図の無いページ（PDF 15＝印刷5、本文のみ）を送っていた。**
  2モデルの「Columnは見えない」は正しい回答だった。正しい図は
  PDF 16＝印刷6の第2.1図。fixtureは `pdf_page:15` と `printed_page:6` で
  自己矛盾しており、照合表は最初からworkspaceにあった。
  → 修正後、detectionは3つとも true になり membership 6バッチが動いた
  （Bedrock: TP 10/42、precision 0.250）。
- **Environmentの証拠図も1枚は図が無いページだった。** 第2.1図（堆積場列あり）へ
  差し替え、OpenRouterの完全一致が 1/5 → 2/5、accept 2/5 → 3/5 に改善。
- preflightに**ページ束縛チェック**を追加。fixtureのpdf_pageが印刷ページ対応表と
  食い違えば exit 1 で外部送信を止める。今回の事故はこれ1つで防げた。
- 誤りの型は「東部を西部と答える」左右取り違えが最多（8件）、次いで
  英語名しか渡していないための無回答（14件）。後者に対し、検証済み別名表の
  日本語地層名を prompt へ併記する実装を入れた（prompt version v2）。
  効果の測定はBedrockの日次枠が戻る明日。
- 一戸の表記揺れはユーザー判断で許容（上限recall 0.952）。
- 回帰 **824件合格 / 0失敗**。第3候補は未有効化、本番routeは Mistral → Gemini。


---

## [死んでいる部分_20260811.md]

# システムの「死んでいる部分」

**調査日**: 2026-08-11
**目的**: 設定はされているが実際には動かない箇所、二重化して片方が意味を失っている箇所を洗い出す

---

## 1. 完全に死んでいるAI

### NVIDIA — 6ルート中 6つとも無効（生存 0）

キーは登録済み（`nvapi-8O…`）、`providers` では `enabled: true`。
しかし**どのルートでも使われない**。

```
disabled_reason（抜粋）
  別名対応  : 0/19 mappings at the 2,048-token output cap
  towada    : hit the 16,384-token hosted-output cap,
              failed JSON parsing, accepted 0/87 fields
  本文抽出  : Enable after long-context GOLD validation
  Vision    : No verified image-input support
```

実測でも **64.8秒**（1図幅5コールで5分）。品質・速度とも実用外。

**判断**: 事実上の死。`providers` の `enabled` を `false` にして意図を明示するか、
削除したほうが設定を読む人を惑わせない。

### OpenRouter — providers に定義のみ。どのルートにも登場しない

```
providers.openrouter : enabled: true, キーも登録済み
routes 内の出現回数  : 0
```

**完全な死に設定**。定義があるので「使われている」と誤解される。

---

## 2. 生存数が薄いルート（実質1本足）

| ルート | 生/全 | 生きている候補 |
| :--- | :--- | :--- |
| ⚠ `column_geography_vision` | **2/5** | mistral-small / gemini-3.5-flash-lite |
| ⚠ `pdf_environment_multimodal` | **2/5** | mistral-small / gemini-3.5-flash-lite |
| `pdf_unit_bootstrap` | 3/5 | mistral-small / cohere / gemini |
| `towada_pdf_llm` | 3/4 | |
| `pdf_body_field_enrichment` | 4/5 | |
| `pdf_unit_alias_mapping` | 5/6 | |

**画像2ステージが最も薄い。**しかも生き残っているのは
`mistral-small-latest` と `gemini-3.5-flash-lite` という、
いずれも小型モデル。地質図の柱状図・凡例を読ませる工程としては手薄。

Bedrock の Claude Haiku が両方で無効化されている理由:

```
column_geography_vision:
  「One-image synthetic payload passed 2026-08-11;
   enable only after Ichinohe Column Vision GOLD and credit-spend review」

pdf_environment_multimodal:
  「the 2026-08-11 two-image synthetic probe was blocked by
   the Anthropic use-case-details account requirement」
```

2026-08-11の申請反映後、1画像probeはHTTP 200で通過した。続けて一戸
Column Vision GOLDを再実行したところ通信・JSON・production validatorは
通過したが、31/42 true positive、24 false positive、precision 0.563636、
recall 0.738095で資格基準未達だった。よってColumn Visionでは無効のまま。
アカウント制約は解消し、現在の阻害要因はColumn所属の過剰割当と中央Columnの
欠落である。PDF Environmentの2画像probeもHTTP 200で通過したが、続く日本語
GOLDは1/5 true positive、1 false positive、validator pass rate 0.4、
precision 0.5、recall 0.2で不合格だった。したがって画像2ルートとも阻害要因は
接続ではなくタスク品質である。

### Bedrock の非対称な無効化

同じ `mistral.mistral-large-3-675b-instruct` が
`pdf_body_field_enrichment` では**有効**、`pdf_unit_bootstrap` では**無効**。
理由の記録が読み取れなかった。意図的なら根拠を、そうでなければ設定ミスの可能性。

---

## 3. 予算管理が二重化していて、互いを知らない ★最も重い

**2つの独立した会計が並行して動いている。**

| | 保存先 | 使う場所 |
| :--- | :--- | :--- |
| 旧 | `config/llm_usage.json`（JSON） | `pdf_alias_mapping` `pdf_field_extract` `pilot` `pilot_llm` `llm_column_vision` `pdf_environment` |
| 新 | SQLite（`LLMRuntimeStore`） | `llm_router` `llm_runtime` `probe_llm_providers` |

**router を通った呼び出しは SQLite に、`call_gemini` を通った呼び出しは JSON に記録される。
どちらも相手の消費を知らない。**

`call_gemini` は今も5箇所から呼ばれている。

```
pdf_alias_mapping.py:333
pdf_field_extract.py:523
pdf_unit_bootstrap.py:358
pilot_llm.py:728
llm_extract.py:761,808
```

分岐は「`api_key` が渡されたら旧経路、渡されなければ router」。
つまり**呼び出し元によって会計先が変わる**。

### 何が起きるか

- 旧経路で20回使い切っても、SQLite側は「0回」と認識してさらに呼ぶ
- 逆も同様
- `config/llm_limits.json` の `max_calls_per_day: 200` は**実枠20と10倍ずれたまま**。
  旧経路の判定はこの値を見るので、事実上ガードとして機能していない

**どちらか一方に寄せる必要がある。**

---

## 4. 私が入れた修正のうち、router 経路では効かないもの

本日 `llm_extract.py` に入れた以下は、**router を通る呼び出しでは適用されない**。
router は独自にHTTPを叩いている（`request_json` を使っていない）。

| 修正 | 旧経路 | router経路 |
| :--- | :---: | :---: |
| 429/5xx リトライ + バックオフ | ○ | 独自実装あり（`llm_router.py:211,797`） |
| 日次枠切れの即時判定 | ○ | **要確認** |
| `GeminiAPIError(OSError)` | ○ | `AllProvidersFailed(OSError)` で同等 |
| トークン推定器の統一（utf8/3） | ○ | **要確認** |
| Vision 系のリトライ集約 | ○ | router 側に移行 |

router 側にもリトライはあるので致命的ではないが、
**同じ規則が2箇所に書かれている**状態。片方だけ直すと挙動が食い違う。

---

## 5. 重複しているツール

| 私が作ったもの | Codex が作ったもの | 判断 |
| :--- | :--- | :--- |
| `claude_work/scripts/test_bedrock.py` | `scripts/probe_llm_providers.py`（623行） | **Codex側に統合すべき** |
| `claude_work/scripts/test_all_providers.py` | 同上 | 同上 |
| `claude_work/scripts/compare_units.py` | `scripts/run_alias_mapping_gold.py` / `run_body_field_gold.py` | 目的が近いが**粒度が違う**。compare_units は Excel全体、run_*_gold はステージ単位。併存に意味あり |

私の `test_*.py` 2本は役目を終えている。残すなら「手元で1発叩く用」と割り切る。

---

## 6. 孤立モジュール

```
scripts/pdf_render_pages.py   48行
scripts/pdf_text_pages.py     43行
```

どこからも import されないが、いずれも `__main__` を持つCLIツール。
**死んではいない**（手動実行用）。ただし用途がドキュメントに無い。

---

## 7. 優先度

| 順 | 項目 | 深刻度 |
| :--- | :--- | :--- |
| 1 | **予算会計の二重化** | **高**。枠の二重消費・ガード不全 |
| 2 | `llm_limits.json` の 200 が実枠20と10倍ずれ | 高 |
| 3 | 画像2ステージが小型モデル2本のみ | 中〜高 |
| 4 | NVIDIA 全滅・OpenRouter 未使用が設定に残存 | 中（誤解の元） |
| 5 | Bedrock の非対称な無効化（bootstrap のみ無効） | 中（意図不明） |
| 6 | リトライ規則が2箇所 | 中 |
| 7 | 私のテストスクリプト2本が重複 | 低 |

---

## 信頼度

| 項目 | 信頼度 |
| :--- | :--- |
| ルート別の生存数・無効化理由 | **high**（`llm_routing.json` を直接集計） |
| 予算会計の二重化 | **high**（両系統の呼び出し箇所を grep で確認） |
| `call_gemini` が5箇所から現役 | **high** |
| router が `request_json` を使っていない | **high**（grep で不在を確認） |
| router 側の枠切れ判定・トークン推定の実装状況 | **未確認**（857行の詳細は未読） |
| Bedrock 非対称無効化の意図 | **不明** |


---

## [課題一覧_20260811.md]

# 課題一覧（2026-08-11 時点）

対象: m1286_一戸（2018）を題材に判明したもの。基準は `Ichinohe_reference_GOLD.xlsx`（人手）。

---

## 0. 現在地

```
GOLD 42 地層 / 出力 48 地層 / 名前で対応がついたのは 30

一致 48 / 不一致 18 / 捏造 0 / 取りこぼし 137
```

**捏造 0 は達成できている。**「推測で値を埋めない」という規則は守られている。
残る課題はほぼすべて「埋められるはずの値が埋まっていない」側にある。

---

## 1. 修正済み（本日）

| # | 内容 | ファイル |
| :--- | :--- | :--- |
| ✅1 | 429/5xx のリトライとバックオフを追加 | `llm_extract.py` |
| ✅2 | トークン推定が日本語で3.26倍過小評価 → `utf8/3` に統一 | `llm_extract.py` |
| ✅3 | 日次枠切れで225秒待つのをやめ、即座に理由を出して諦める | `llm_extract.py` |
| ✅4 | 例外型を `GeminiAPIError(OSError)` に。`pilot.py` の劣化動作から漏れないように | `llm_extract.py` |
| ✅5 | `unit_id` の重複（別々の地層が同じID）を解消 | `pdf_unit_bootstrap.py` |
| ✅6 | 同一ユニットで上下を挟む「自己ブラケット」による年代誤伝播を停止 | `age_resolution.py` |
| ✅7 | 提出前チェックに `unit_id` 整合性検査を追加 | `export_submission.py` |

効果: 不一致 48→18、捏造 1→0、`unit_id` 重複 4→0、誤った年代 15件→0件。

---

## 2. 残っている課題

### A. 構造の課題（投入品質に直結）

#### A-1. Column が分割されていない ★最優先

```
GOLD          : ichinohe-west 19 / ichinohe-central 5 / ichinohe-east 15
現在の出力     : unsplit 48（1列）
Vision の提案  : western 19 / central 5 / eastern 10
```

west と central は**件数まで GOLD と一致**している。Vision は人手の列分けをほぼ再現できているのに、
提案が丸ごと却下されて `unsplit` に落ちている。

地理的に離れた3地域の層序を1本の柱状図に混ぜているため、Macrostrat の構造として正しくない。
また、年代補完が地域をまたいで働くことになり、今回の誤伝播が起きやすい土壌にもなっている。

**却下の理由**（`llm_cache/cv_*.json` の `dropped`）:

```
18件  no valid Column membership          ← Vision が48件中30件しか返さなかった
 1件  response did not return every canonical unit
 1件  interval_not_in_controlled_list: "Early Pliocene"
```

全ユニットを網羅していないと提案ごと却下する all-or-nothing 設計。
**30件分の正しい割当も一緒に捨てられている。**

判断が要る点: 部分採用を許すか。許すと一部ユニットが未割当のまま残る。

#### A-2. ユニットの同定がずれている

GOLD 42 / 出力 48 のうち、名前で対応がついたのは 30。
GOLD にあって出力に無い 12、出力にあって GOLD に無い 18。

項目単位の精度より上位の問題。原因未調査。表記ゆれなのか、実際に別の地層を立てているのかを
切り分ける必要がある。

---

### B. 取りこぼし（137件の内訳と、埋められる見込み）

| 項目 | 取りこぼし | 埋められる見込み |
| :--- | ---: | :--- |
| b_int | 21 | **高**。Vision が30ユニットに年代を提案済み（A-1 を直せば入る） |
| t_int | 19 | **高**。同上 |
| environment | 18 | 中。`pdf_environment` は 39 targets 中 4 accepted、35 dropped |
| max_thickness | 17 | 中。本文に数値はある（GOLD が取れている） |
| lithology | 16 | 中 |
| basal_surface | 15 | 中 |
| min_thickness | 13 | 中 |
| strat_name | 10 | 低〜中。B-2 参照 |
| minor_lith | 8 | 中 |

**b_int / t_int の40件は、A-1 を直すだけで大きく減る見込み。**
Vision の年代提案を GOLD と年代値で照合すると、30件中 10件が完全一致・8件が片側一致だった。

#### B-1. Vision の年代は GOLD より1段粗い

```
Vision            GOLD
Late Miocene   →  Tortonian
Middle Miocene →  Serravallian
Early Miocene  →  Burdigalian
Middle Pleistocene → Chibanian（この2つは同一区間 0.774–0.129 Ma）
```

矛盾ではなく粒度の違い。epoch 級で埋めるか、stage 級を要求して空欄のままにするかは方針判断。

#### B-2. `strat_name` が10件空欄、一部に日本語

前回の実行では `Ichinohe Pluton` → 一戸深成岩体、`Tsukanaigawa Pluton` → 塚内川深成岩体 が入っていた。
GOLD は英語表記。今回の実行では空欄。挙動が実行ごとに揺れている。

---

### C. 小さいが確実に直せるもの

#### C-1. `Early Pliocene` が許可リストから漏れている

`common.py:650-654` が図幅で頻出する細分名を明示的に許可しているが、**Pliocene だけ入っていない**。

```python
for name in (
    "Early Pleistocene", "Middle Pleistocene", "Late Pleistocene",
    "Early Miocene", "Middle Miocene", "Late Miocene",
):
```

`intervals.json` には `Early Pliocene`（5.333–3.6 Ma、Zanclean と同一区間）が存在する。
Pleistocene と Miocene は許可され、Pliocene は弾かれるという非対称。
`Early Pliocene` / `Late Pliocene` を足せば直る。他図幅でも再発しうる。

**コスト: 1〜2行。リスクほぼ無し。**

#### C-2. Vision 系2ステージにリトライが無い

`llm_column_vision.py:378` と `pdf_environment.py:367` は `call_gemini` を通らず独自に `urlopen`
している。今日の実行では 503 と 429 を受けて即死した（2回とも）。

**コスト: 小〜中。**

---

### D. 枠と運用

#### D-1. `gemini-3.6-flash` の無料枠が 20回/日

1図幅5コールなので **1日4図幅**。Flash-Lite なら 1,500回/日（75倍）だが、
日本語の地質記載での精度は未検証。`compare_units.py` で測れる。

なお 503 の本文は `gemini-3.6-flash is currently experiencing high demand` で、
最新モデル特有の混雑。Flash-Lite に移れば安定性も上がる可能性がある。

#### D-2. キャッシュがモデル名と `prompt_version` に依存

どちらかを変えると全ステージのキャッシュが失効し、図幅あたり5回を再消費する。
枠が20回/日のうちは、変更をまとめて、リセット直後（日本時間の夕方）に実施したい。

---

### E. 設計判断が要るもの（実装保留）

#### E-1. 推論値を `b_int`/`t_int` の値列に書いてよいか

`age_resolution` の補完値は証拠層では `[C|Derived|INFERRED]`、状態 CHECK として
正しく記録されている。**出所の記録は壊れていない。**
しかし値そのものは通常の列に入る。「推測で値を埋めない」との整合をどう取るか。

- (a) 現状維持（CHECK と証拠で区別されている）
- (b) `REF_` 系の参考列にだけ出す
- (c) 補完の適用条件をさらに狭める

---

## 3. 推奨する順序

| 順 | 項目 | 理由 | コスト |
| :--- | :--- | :--- | :--- |
| 1 | **C-1** Early Pliocene を許可リストへ | 1行。今すぐ確実に直る | 極小 |
| 2 | **C-2** Vision 系のリトライ | 今日2回落ちている。枠を無駄にしている | 小 |
| 3 | **A-1** Column 割当の部分採用 | 効果が最大。b_int/t_int 40件にも波及 | 中（要判断） |
| 4 | **D-1** Flash-Lite の精度検証 | 枠75倍。判定手段は既にある | 小（枠5回） |
| 5 | **A-2** ユニット同定のズレ調査 | 上位の問題だが原因未調査 | 中〜大 |
| 6 | **E-1** 推論値の扱い | 方針決めが先 | — |

1と2はキャッシュを失効させないので、**枠を消費せずに入れられる**。

---

## 4. 測定の再現方法

```powershell
python claude_work/scripts/compare_units.py `
  "claude_work/reports/Ichinohe_reference_GOLD.xlsx" `
  "data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx" `
  --out claude_work/reports/比較_YYYYMMDD.md
```

現在の基準値: **一致 48 / 不一致 18 / 捏造 0 / 取りこぼし 137**

## 5. 信頼度

| 項目 | 信頼度 |
| :--- | :--- |
| 上記の各件数（GOLD 比較、dropped 内訳、Column 件数） | **high**（実測） |
| Early Pliocene が許可リストから漏れていること | **high**（`intervals_for_excel()` を実行して確認） |
| A-1 を直せば b_int/t_int の取りこぼしが減るという見込み | **medium**（未実行） |
| A-2 の原因 | **未調査** |
| Flash-Lite の精度 | **未検証** |


---

## [システム分析_20260811.md]

# 現行システムの分析

**分析日**: 2026-08-11
**きっかけ**: Codex が並行して大規模な実装を行っていたため、状態を把握し直した
**結論**: **私の仕様書は既に陳腐化していた。**Codex の実装は仕様書より進んでおり、設計も優れている

---

## 1. 規模

```
47 モジュール / 27,633 行
```

| ファイル | 行数 | 役割 |
| :--- | ---: | :--- |
| `common.py` | 1,995 | 共通処理・語彙・秘密情報 |
| `compiled_layer.py` | 1,727 | 監査可能な正規化データ層 |
| `pilot.py` | 1,463 | 5段階パイプラインの統括 |
| `column_map.py` | 1,377 | レビュー地図・KML |
| `pilot_llm.py` | 1,347 | 予算管理つき再開可能なLLM処理 |
| `export_submission.py` | 1,092 | 提出前チェック・出力 |
| `pdf_environment.py` | 1,017 | 環境解析（画像） |
| **`llm_router.py`** | **857** | **プロバイダのフェイルオーバー（新規）** |
| **`llm_runtime.py`** | **494** | **予算・回路状態のSQLite（新規）** |

---

## 2. Codex が構築したもの（本日 17:34 以降）

```
scripts/llm_router.py            857行   検証つき順次フェイルオーバー
scripts/llm_runtime.py           494行   トランザクショナルな実行時状態
scripts/llm_qualification.py     370行   モデル資格判定
scripts/probe_llm_providers.py   623行   プロバイダ疎通調査
scripts/run_alias_mapping_gold.py 331行  別名対応のGOLD検証
scripts/run_body_field_gold.py   341行   本文抽出のGOLD検証
config/llm_routing.json          16KB    8プロバイダ × 6ステージのルーティング
config/llm_gold_body_fields.json          GOLD基準データ
claude_work/tests/  5ファイル追加
```

**本番への接続も完了している。**未接続ではない。

```
llm_column_vision.py / pdf_alias_mapping.py / pdf_environment.py
pdf_field_extract.py / pdf_unit_bootstrap.py / pilot_llm.py
```

これら6モジュールすべてが `from llm_router import LLMRequest, LLMRouter, ValidationReport`
している。`api_key` が渡されない場合は router を使う設計。

---

## 3. 私の仕様書より優れている点

### 3-1. GOLD検証で資格を判定している（最重要）

私の仕様書は「接続確認 → あとで品質を測る」だった。
**Codex は既に測っており、落ちたモデルを根拠つきで無効化している。**

```
nvidia/nemotron-3-nano-30b-a3b (別名対応)
  enabled: false
  「GOLD returned parseable JSON but production validation accepted
   0/19 mappings at the 2,048-token output cap」

cohere/command-a-vision-07-2025 (Column Vision)
  enabled: false
  「GOLD failed JSON parsing at 2048 output tokens and
   accepted 0/42 memberships」

nvidia (towada)
  enabled: false
  「hit the 16,384-token hosted-output cap, failed JSON parsing,
   and accepted 0/87 fields」
```

**接続できることと使えることは違う**、という私が繰り返し言っていた原則を、
Codex は実際に測って適用している。`disabled_reason` に日付と数値が入っており、
後から検証できる。

### 3-2. 出力トークン上限という観点が私に無かった

無効化理由の多くが「出力トークンの上限でJSONが途中で切れ、パースに失敗」である。

```
2,048-token output cap   → 0/19 mappings
16,384-token output cap  → 0/87 fields
2048 output tokens       → 0/42 memberships
```

私は**入力**のコンテキスト長ばかり見ていた（10万トークンが入るか）。
**出力**の上限で落ちるという失敗モードを見落としていた。
48ユニット分のJSONを返すには相応の出力枠が要る。

### 3-3. トランザクショナルな予算管理

`llm_runtime.py` は SQLite で以下を持つ。

- `reserve` / `release` / `finalize` — **予約制**。同時実行でも枠を超えない
- `day_bucket(timestamp, timezone_name)` — **タイムゾーン別の日境界**
  （Gemini の `reset_timezone: America/Los_Angeles` と対応。私が実測した
  「PT深夜リセット」がそのまま設計に入っている）
- `claim` / `record_failure` / `circuits` / `reset_circuit` — **サーキットブレーカー**

私の仕様書は `record_usage` を呼ぶだけだった。予約も回路遮断も無い。

冒頭に方針が明記されている。

> The database contains operational metadata only.
> Prompts, responses and API keys must never be written here.

### 3-4. `context_headroom: 0.8`

コンテキストの2割を残す設計。私は「256Kに85,000が入る」としか見ていなかった。

### 3-5. `quota_group`

`gemini` の複数モデルが同じ Google プロジェクト枠を共有することを表現している。
モデル単位ではなくプロジェクト単位で枠を数える必要がある、という理解。

---

## 4. テスト

```
43 ファイル / 実質全通過
```

- 既存38ファイル: すべて成功
- 新規5ファイル: pytest 形式。`test_llm_router` 19 passed、`test_llm_qualification` 8 passed、
  `test_llm_routing_config` 2 passed
- GOLD系2ファイル: 実データを参照するため実機でのみ通る

**注意**: 新規テストは pytest を要求する。既存テストは単体実行できる素の
スクリプトなので、**実行方法が2系統に分かれている**。CI を組むなら統一が要る。

---

## 5. 指摘したい点

### 5-1. Gemini が全ルートで最後尾になっている ★要判断

6ルートすべてで `gemini` が候補リストの**末尾**。
`llm_router.py` は `for candidate in self._eligible(...)` とリスト順に試すので、
**Gemini は最後の手段**になっている。

```
[pdf_body_field_enrichment]
  1. mistral   mistral-small-latest
  2. bedrock   mistral.mistral-large-3-675b-instruct
  3. cohere    command-a-plus-05-2026
  4. nvidia    （無効）
  5. gemini    gemini-3.5-flash-lite     ← 最後
```

**利用者の意向は「Gemini の枠が余っているなら先に使う」。**現状はその逆。

またモデルが `gemini-3.5-flash-lite` に変わっている点は妥当（無料枠が
20回/日 → 1,500回/日）。**無料で枠も大きいものを最後に置く理由が、設定からは読めない。**

無効化の判断には `disabled_reason` が丁寧に書かれているのに、
**有効な候補の順序には根拠が記録されていない。**品質順なのか、
単に書いた順なのかが判別できない。

→ 順序の根拠を `_order_reason` のような形で残すか、Gemini を先頭に移すか、
どちらかにすべき。

### 5-2. 私の仕様書は破棄してよい

`claude_work/reports/AWS_Azure組み込み仕様_Codex向け_20260811.md` は、
Codex の実装が既に上回っているため**送る必要がない**。

ただし以下2点は仕様書にしか無い情報なので、口頭で伝える価値がある。

- **Groq の 403 error 1010 は User-Agent 起因**（urllib の既定UAが Cloudflare に弾かれる）。
  `llm_routing.json` に groq が有効で入っているが、UAを設定していないと落ちる
- **`config/llm_limits.json` の `max_calls_per_day` が 200**。
  Gemini 3.6-flash の実枠は20だった。flash-lite なら1,500。
  `llm_runtime` 側で管理するなら、この値は使われていない可能性がある（要確認）

### 5-3. 成果物の品質は依然として未改善

システムの配管は大きく前進したが、**Column の中身は止まったまま**。

```
一致 48 / 不一致 18 / 捏造 0 / 取りこぼし 137
Column: unsplit 1列（GOLD は west/central/east の3列）
```

Column 分割の修正（`assignment_ready` の部分採用）は実装済みだが、
キャッシュに `assignment_ready: False` が焼き付いているため**未反映**。
Vision の再実行が要る。

---

## 6. 次にやるべきこと

| 順 | 項目 | 理由 |
| :--- | :--- | :--- |
| 1 | **Gemini の順序を決める** | 利用者の意向と現状が食い違っている。設定1箇所 |
| 2 | **`run.py ichinohe --force`** | Column 分割の反映。本日の残枠で回せる |
| 3 | `compare_units.py` で再測定 | 3列に分かれたか、取りこぼし137がどう動くか |
| 4 | Groq の UA 設定を確認 | 有効になっているが落ちる可能性 |
| 5 | ユニット同定のズレ調査 | GOLD 42 / 出力48 / 対応30。未着手の最大の問題 |

---

## 信頼度

| 項目 | 信頼度 |
| :--- | :--- |
| モジュール構成・行数・テスト結果 | **high**（実測） |
| llm_router が本番6モジュールに接続済み | **high**（import を確認） |
| 候補はリスト順に試される | **high**（`_eligible` のループを確認） |
| GOLD検証の内容 | **high**（`disabled_reason` の記述） |
| Gemini を最後尾にした意図 | **不明**（設定に根拠の記録が無い） |
| `llm_limits.json` が今も使われているか | **未確認** |

