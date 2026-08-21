# Macrostrat Japan: 先行研究包括データベース (PRIOR_ART_DATABASE.md)

本データベースは、地質図抽出、文書解析（OCR/LLM）、地質学的リレーション抽出、層序柱状図自動構築、および Human-in-the-loop モデリングに関する国内外の主要先行研究を体系的に追跡・記録する恒久台帳である。

---

## 1. 先行研究一覧マスターテーブル (Master Corpus Table)

| ID     | 文献・プロジェクト名                                                                                                                                         | 著者・機関                        | 年 / 国       | 査読区分                      | 入力データ種別              | 採用技術 (NLP/CV/GIS)               | 成果物                      | Macrostrat Japan との比較確信度 |
| :----- | :------------------------------------------------------------------------------------------------------------------------------------------------- | :--------------------------- | :---------- | :------------------------ | :------------------- | :------------------------------ | :----------------------- | :----------------------- |
| **A1** | [CriticalMAAS Project](https://github.com/UW-Macrostrat/CriticalMAAS)                                                                              | UW-Macrostrat / DARPA        | 2023- / 米国  | Repository                | 地質図・報告書・鉱床DB         | LLM / TA1 Map Pipeline / CDR    | 鉱物資源評価基盤                 | **高** (アーキテクチャの直接参考)     |
| **A2** | [USGS CriticalMAAS Workshop](https://www.usgs.gov/news/featured-story/collaborative-workshop-spotlights-machine-learning-accelerate-usgs-critical) | USGS                         | 2024 / 米国   | Official Report           | 地質図・報告書              | Human-in-the-loop / ML          | 鉱床評価・教師データ               | **高** (専門家フィードバック設計)     |
| **A3** | [LARA / Geological Map Extraction](https://github.com/DARPA-CRITICALMAAS/uncharted-ta1)                                                            | Uncharted / DARPA TA1        | 2023 / 米国   | Repository                | ラスタ地質図画像             | CV (Segmentation / OCR)         | ベクトルGIS (Lines/Poly)     | **中** (ラスタ→GIS変換特化)      |
| **A4** | [USGS Map Extraction Benchmark](https://pubs.usgs.gov/publication/70271124)                                                                        | USGS                         | 2024 / 米国   | Peer-reviewed             | ラスタ地質図画像             | CV Benchmark (Poly F1:0.77)     | 精度ベンチマーク                 | **高** (評価指標の基準)          |
| **B1** | [PaleoDeepDive](https://doi.org/10.1371/journal.pone.0113523)                                                                                      | Peters et al. (UW-Madison)   | 2014 / 米国   | PLOS ONE (査読付)            | 学術論文 (PDF/HTML)      | OCR / Layout NLP / Rules        | 古生物・地質年代DB               | **極高** (PDF→DB抽出の元祖)     |
| **C1** | [BGS Geological NER/RE Dataset](https://www2.bgs.ac.uk/nationalgeosciencedatacentre/citeddata/catalogue/afba2d1d-8a5d-4b96-a6fa-c13b5d8d32cd.html) | British Geological Survey    | 2023 / 英国   | Official Dataset          | BGS 地質報告書 (Memoir)   | BERT / OCR / Rules              | Entity/Relation アノテーション  | **極高** (地質Relation抽出の基準) |
| **C2** | [BGS TextViewer](https://webapps.bgs.ac.uk/Memoirs/)                                                                                               | British Geological Survey    | 2022 / 英国   | Web System                | BGS 古文書 Memoir (PDF) | OCR / Full-text Search          | 検索閲覧・原典対照UI              | **高** (Provenance設計の基準)  |
| **D1** | [GeoERE-Net](https://doi.org/10.1016/j.cageo.2022.105178)                                                                                          | Liu et al. (China)           | 2022 / 中国   | Comput. Geosci. (査読付)     | 中国地域地質報告書            | Deep Learning (F1: 90.05%)      | 地質ナレッジグラフ                | **高** (Relation抽出アルゴリズム) |
| **D2** | [Geological Relation Extraction](https://doi.org/10.1016/j.cageo.2024.105654)                                                                      | Zhang et al. (China)         | 2024 / 中国   | Comput. Geosci. (査読付)     | 7編の地域地質報告書           | Complex Relation NLP (24種)      | 地質Relationデータセット         | **極高** (Relation語彙設計の基準) |
| **D3** | [Geological Profiles + Context Text](https://doi.org/10.1016/j.oregeorev.2023.105655)                                                              | Wang et al. (China)          | 2023 / 中国   | Ore Geol. Rev. (査読付)      | 地質断面図 + 説明テキスト       | Vectorization + Text Mining     | 断面・テキスト統合KG              | **中** (将来の断面図解析参考)       |
| **D4** | [StraKG (Stratigraphic KG)](https://doi.org/10.1016/j.jaesx.2024.100171)                                                                           | Chen et al. (China)          | 2024 / 中国   | J. Asian Earth Sci. (査読付) | 層序テキスト・岩相記録          | Graph DB / NLP                  | 層序対比・時空間解析KG             | **極高** (層序KGの代表例)        |
| **D5** | [Lithium Deposit KG](https://doi.org/10.1016/j.oregeorev.2025.106400)                                                                              | Li et al. (China)            | 2025 / 中国   | Ore Geol. Rev. (査読付)      | 鉱床報告書 (1066エンティティ)   | Knowledge Graph                 | リチウム鉱床形成モデル              | **中** (将来の鉱床拡張参考)        |
| **E1** | [Stratigraphically Homogeneous Zones](https://doi.org/10.1080/17445647.2025.2504064)                                                               | Tondi et al. (Italy)         | 2025 / イタリア | J. Maps (査読付)             | 地質図 + 手動層序位置/層厚      | Spatial Raster/Vector Algo      | 全ピクセル層序柱状図マップ            | **極高** (柱状図空間生成の直接比較)    |
| **F1** | [Human-in-the-loop Modeling](https://meetingorganizer.copernicus.org/EGU26/EGU26-10918.html)                                                       | Geological Survey of Belgium | 2026 / ベルギー | EGU26 Abstract            | ボーリング柱状図・技術報告書       | LLM + Geological Axioms + GemPy | 3D 地質モデル (Human-in-loop) | **極高** (ループ設計思想の一致)      |
| **G1** | [MinMod Knowledge Graph](https://github.com/DARPA-CRITICALMAAS/ta2-minmod-kg)                                                                      | CriticalMAAS TA2             | 2024 / 米国   | Repository                | 鉱床データベース・文献          | Knowledge Graph / RDF           | 鉱物資源KG                   | **低** (将来拡張用)            |
| **G3** | [Protagonist / Latigo](https://github.com/DARPA-CRITICALMAAS/protagonist-latigo-geochemical-analysis)                                              | CriticalMAAS                 | 2023 / 米国   | Repository                | 地球化学報告書 (PDF)        | PyMuPDF / Camelot / Claude      | 地球化学分析データテーブル            | **中** (将来の表抽出技術参考)       |
|        |                                                                                                                                                    |                              |             |                           |                      |                                 |                          |                          |

---

## 2. 各研究の詳細プロファイルと Macrostrat Japan との差異分析

### B1. PaleoDeepDive (Peters et al., 2014)
- **概要**: 論文 PDF から OCR、レイアウト解析、ルールベース NLP を用いて地層名、地質年代、化石産出記録を大規模自動抽出した先駆的研究。
- **差異点**: PaleoDeepDive は「文献本文中の事実抽出」に特化しており、1:50,000 地質図幅の空間幾何（ポリゴン）と結合して「地域層序柱状図（Column）」を合成する機能は持たない。

### E1. Stratigraphically Homogeneous Zones (Italy, 2025)
- **概要**: 地質図の各地点（ピクセル）に対して stratigraphic position（相対位置）と thickness（層厚）を割り当て、全域の柱状図マップを生成。
- **差異点**: イタリアの研究では、入力となる層序位置や層厚データを「人間があらかじめ手動で整備」している。Macrostrat Japan は **「PDF/凡例から層序関係・相対位置を半自動抽出して Column を導出する」** ため、イタリア研究の入力データ作成自体を自動化するスコープを含む。

### F1. Geological Survey of Belgium (EGU 2026)
- **概要**: 過去の地質報告書から LLM で属性を抽出し、地質公理（Geological Axioms）による自動検証を経て、専門家が不確実性を修正する Human-in-the-loop 基盤。
- **差異点**: 思想（AI に定型データ整理を任せ、人間を地質解釈・不確実性評価に集中させる）は Macrostrat Japan の 3 重ループと完全に一致する。ベルギーは 3D 地質モデル（GemPy）を最終成果物とするのに対し、Macrostrat Japan は Macrostrat 標準の 2D/1D 地域柱状図（Column）を中核成果物とする。