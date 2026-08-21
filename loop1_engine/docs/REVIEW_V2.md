# Review v2 運用ガイド

Review v2 は、元の review workbook を上書きせず、次の優先順位で候補値と根拠をまとめる仕組みです。

1. ZFK
2. GSJ Shapefile
3. PDF（まず英語 Abstract、足りない項目だけ本文・LLM）

値を自動的に確定するのではなく、field ごとの候補・出典・引用を残し、人間が最終判断できることを優先します。

## 実行

```powershell
python run.py towada
```

`python run.py review towada` と書いても同じ動作です。従来の `review-v2` も互換用に残しています。

初回生成後、このファイルが十和田パイロットの正本です。

```text
outputs/review_v2/m1050_review/m1050_review_v2.xlsx
```

通常の再実行では、この正本を上書きしません。既にファイルがある場合は停止し、`check` または `export` を案内します。人の編集を捨てて最初から作り直すと明示的に決めた場合だけ、次を使います。

```powershell
python run.py towada --force
```

出力先を指定する場合:

```powershell
python run.py towada --output-dir outputs/towada_review_v2
```

Shapefile がない地域でも実行できます。Shapefile がある場合は、review workbook と同じフォルダの `references/**/geo_A.shp` を自動検出します。複数候補がある場合だけ `--shape` で明示します。

地図を一時的に作らず、Shapeの根拠だけ統合する場合:

```powershell
python run.py towada --skip-map
```

## 出力

- `*_review_v2.xlsx`: 人間が確認・修正する4シートのworkbook
- `compiled.json`: 生成時点の候補値を保持する機械可読データ
- `evidence.json`: 候補値と根拠をfield単位で保持する根拠台帳
- `column_map.png`: Column色分けと候補点の確認図
- `column_map.kml`: Google Earth用のColumn領域・候補点
- `column_map.json`: 地図作成条件と座標

Excel の役割は次のとおりです。

- `Review`: unit ごとの採用値、短い根拠、確認状態
- `Columns`: Column 名・代表点・参照資料・色分け地図
- `Evidence`: field ごとの全候補、出典位置、全文脈、競合フラグ
- `Project`: map metadata、references、images、再現性情報

## 自動計算と人間確認

`position`、`section_id`、`t_pos`、`t_prop`、`b_prop` は参照用に Review に表示します。提出ファイル作成時には、`sort_order` と年代から再計算します。年代がない場合だけ、reviewで確認済みのprop表示値をfallbackとして使います。

これらの青いセルは生成時点のpreviewです。Review上で `sort_order`、`column_id`、年代を変更しても、正本を再生成しないでください。提出時には編集後の値から自動計算されます。青い表示が古い場合も、提出値には影響しません。

空欄に安全な自動候補を表示できる場合は黄色で示し、根拠列に `AUTO CANDIDATE` と表示します。候補値から出典注記を分離し、lithology・minor_lith・environment はMacrostrat語彙で検証できた語だけを仮入力します。未検証語は `Evidence` に残し、Review入力欄は空欄のままにします。

人間が主に確認するのは次です。

- PDF層序図に基づく `column_id`
- GSJ年代表記からMacrostrat intervalへの対応
- environment、lithology、minor_lith の統制語彙
- thickness、basal surface、lateral relationship の根拠
- Column代表点と色分け領域

LLMはPDFにしかない項目の候補抽出に限定し、引用・ページ・sectionを `Evidence` に残します。ZFKまたはShapeで取得できる情報をLLMに再抽出させる必要はありません。

## 提出前チェック

```powershell
python run.py check towada
```

`CHECK` は未完成という意味ではなく、人間の判断が必要な候補や競合が残っている状態です。最終採用後にMacrostrat提出形式へexportします。

```powershell
python run.py export towada
```

`check` と `export` は、正本の Review-v2 を自動的に選びます。旧形式を明示的に確認したい場合だけ `--legacy` を付けます。
