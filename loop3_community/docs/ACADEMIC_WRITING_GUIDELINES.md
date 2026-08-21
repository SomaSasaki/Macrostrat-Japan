# Macrostrat Japan: 学術論文執筆規程およびデータ記述標準ガイドライン
**Academic Writing Guidelines & Geoinformatics Data Standards**

本ガイドラインは、Macrostrat Japan プロジェクトにおいて作成されるすべての学術論文、技術仕様書、およびリポジトリ公開文書が準拠すべき公式執筆規範である。

---

## 1. 準拠する公的機関・学会標準

本プロジェクトの記述形式は、以下の学術標準およびデータベーススキーマに厳密に準拠する：
1. **産総研 地質調査総合センター (GSJ)**: 地質図幅引用規程・JIS A 0204（地質図凡例表示規格）
2. **日本地質学会 (JGS)**: 学術論文執筆の手引き（事実記載と解釈の明確な分離）
3. **UW-Macrostrat / EarthCube**: Crustal Database Ingestion Schema (Peters et al., 2018)
4. **国際層序委員会 (ICS)**: International Chronostratigraphic Chart (v2023/09)
5. **米国地質調査所 (USGS)**: Geologic Map Schema (GeMS) Standard

---

## 2. 執筆時の絶対原則 (Writing Principles)

### ① 客観的事実に基づく学術的文体の徹底
- 「画期的な」「完全解決」「独自技術」「驚異的な」等の主観的・誇大広告的修飾語を **完全禁止** とする。
- 「〜を実装した」「〜の条件を満たすことを実測検証した」「誤差は0件であった」など、測定可能な事実のみを記述する。

### ② 地質学的属性の厳密な定義と決定論的根拠
地質層序カラム（Column）を構成する各属性は、以下の基準により一意に決定・記載されなければならない：

| 属性名 | 定義と決定方法 | 参照データソース |
| :--- | :--- | :--- |
| **`unit_id`** | 図幅記号（`m1286`等）と連番による一意の層序識別子 | プロジェクト規約 |
| **`unit_name`** | 地層・岩体名（和名・英名） | GSJ `geo_A.dbf` / 説明書各論 |
| **`strat_name`** | 層群・累層・部層等の階層的層序名 | GSJ 説明書本文 |
| **`lithology`** | 主要岩相（Macrostrat 公式語彙準拠） | GSJ 凡例 / `vocab.json` |
| **`t_int` / `b_int`** | 上限・下限の地質時代区間名 | ICS 2023 / `intervals.json` |
| **`t_age_ma` / `b_age_ma`**| 上限・下限の数値年代 (Ma) | ICS 2023 対比表 |
| **`b_prop` / `t_prop`** | 時代区間内における相対層厚位置 ($0.0 \le b\_prop < t\_prop \le 1.0$) | 層序関係に基づく幾何計算 |
| **`environment`** | 堆積環境・形成場（Macrostrat 公式語彙） | GSJ 説明書堆積相記載 |
| **`basal_surface`** | 下限境界の接触関係 (`conformable`, `unconformable`, `fault`, `intrusive`) | GSJ 凡例境界線記号 / 本文 |
| **`Evidence`** | 抽出値の根拠となる説明書頁番号および原文引用 | GSJ 説明書 PDF |

---

## 3. 出典・エビデンス引用形式 (GSJ Memoir Citation Standard)

GSJ 地質図幅および説明書を引用する際は、以下の形式を厳守する：

> 著者名（発行年）5万分の1地質図幅『図幅名地域の地質』. 産業技術総合研究所 地質調査総合センター, 該当ページ.