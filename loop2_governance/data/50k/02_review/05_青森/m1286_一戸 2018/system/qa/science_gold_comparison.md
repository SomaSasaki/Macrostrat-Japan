# 抽出結果の比較

- 正解: `data/02_review/05_青森/m1286_一戸 2018/m1286_review.xlsx` （シート Review） — GOLD
- 候補: `data/02_review/05_青森/m1286_一戸 2018/m1286_review.candidate-20260814T074455Z.xlsx` （シート Review） — CANDIDATE

## 地層の対応

- 対応がついた地層: **30**（うち名称が完全一致 30、類似で対応 0）
- 正解にあって候補に無い: **12**
- 候補にあって正解に無い: **0**

正解にあって候補に無い地層:

- river-bed deposits
- flood-plain and valley-floor deposits
- Zyūmonzi Formation
- Suenomatuyama Formation
- Kadonosawa Formation
- Yotuyaku Formation
- Nisatai Formation
- Kuzumaki Formation
- river-bed deposits
- Towada-Hachinohe Pyroclastic Flow Deposits
- Zyūmonzi Formation
- Kadonosawa Formation

## 項目ごとの照合

「捏造」は正解が空欄なのに候補が値を入れた件数。プロジェクト規則「推測で値を埋めない」に直接反するため、ここが増えるモデルは採用すべきでない。

「（参考）」の付いた項目は自由記述で、文字列の完全一致で測っても意味がないため合計には入れていない。

| 項目 | 一致 | 不一致 | 捏造 | 取りこぼし | 両方空 | 一致率 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| strat_name | 16 | 0 | 0 | 0 | 14 | 100% |
| lithology | 18 | 12 | 0 | 0 | 0 | 60% |
| minor_lith | 0 | 10 | 1 | 3 | 16 | 0% |
| environment | 15 | 4 | 2 | 5 | 4 | 58% |
| b_int | 25 | 5 | 0 | 0 | 0 | 83% |
| t_int | 26 | 2 | 2 | 0 | 0 | 87% |
| min_thickness | 14 | 0 | 1 | 1 | 14 | 88% |
| max_thickness | 16 | 1 | 0 | 2 | 11 | 84% |
| basal_surface | 18 | 1 | 0 | 5 | 6 | 75% |
| unit_description（参考） | 2 | 26 | 0 | 2 | 0 | 7% |
| **合計** | **148** | **35** | **6** | **16** | 65 | **72%** |

## 判定の目安

- **捏造 6件。**空欄であるべき箇所に値が入っている。採用前に中身を確認すること。
- 取りこぼし 16件。埋められるはずの値が空欄になっている。

## 差分の一覧

| 地層 | 項目 | 種別 | 正解 | 候補 |
| :--- | :--- | :--- | :--- | :--- |
| river-bed deposits | unit_description | 不一致 | other young and minor deposits, such as landslide deposits, … | River-bed deposits occur in the district as young and minor … |
| flood-plain and valley-floor deposits | environment | 不一致 | fluvial indet. | floodplain |
| flood-plain and valley-floor deposits | unit_description | 不一致 | other young and minor deposits, such as landslide deposits, … | Flood-plain and valley-floor deposits occur in the district … |
| Horino terrace deposits | unit_description | 不一致 | The deposits of the lower lower terrace are the Horino and t… | The Horino terrace deposits are lower lower terrace deposits… |
| Maisawa terrace deposits | unit_description | 不一致 | The deposits of the higher lower terrace are the Maisawa and… | The Maisawa terrace deposits are higher lower terrace deposi… |
| Towada-Hachinohe Pyroclastic Flow Deposits | lithology | 不一致 | pumice lapilli; ash | pumice; ash |
| Towada-Hachinohe Pyroclastic Flow Deposits | unit_description | 取りこぼし | The pyroclastic flow deposits, derived from Towada volcano, … | (空) |
| Towada-Ofudo Pyroclastic Flow Deposits | lithology | 不一致 | pumice lapilli; ash | pumice; ash |
| Towada-Ofudo Pyroclastic Flow Deposits | unit_description | 取りこぼし | The pyroclastic flow deposits, derived from Towada volcano, … | (空) |
| Kusagi terrace deposits | unit_description | 不一致 | The middle terrace deposits are subdivided into the Kusagi a… | The Kusagi terrace deposits are middle terrace deposits deve… |
| Asanai terrace deposits | unit_description | 不一致 | The higher terrace deposits are subdivided into the Asanai a… | The Asanai terrace deposits are higher terrace deposits dist… |
| Nanashigure Volcanic Fan Deposits | lithology | 不一致 | gravel; sand; silt | gravel |
| Nanashigure Volcanic Fan Deposits | unit_description | 不一致 | The Nanashigure Volcanic Fan Deposits, distributed only in t… | The Nanashigure Volcanic Fan Deposits are distributed only i… |
| Shitazaki Formation | minor_lith | 不一致 | sandstone; tuff | sandstone |
| Shitazaki Formation | environment | 捏造 | (空) | shelf |
| Shitazaki Formation | min_thickness | 取りこぼし | 200 | (空) |
| Shitazaki Formation | max_thickness | 取りこぼし | 200 | (空) |
| Shitazaki Formation | unit_description | 不一致 | The Shitazaki Formation conformably overlies the Yanagisawa … | The Shitazaki Formation conformably overlies the Yanagisawa … |
| Yanagisawa Formation | lithology | 不一致 | diatomite; diatomaceous mudstone; hard shale; porcellanite | diatomite; porcellanite |
| Yanagisawa Formation | minor_lith | 捏造 | (空) | mudstone; shale |
| Yanagisawa Formation | unit_description | 不一致 | The Yanagisawa Formation conformably overlies the Zyūmonzi F… | The Yanagisawa Formation conformably overlies the Zyūmonzi F… |
| Zyūmonzi Formation | minor_lith | 不一致 | conglomerate; coquina conglomerate; volcaniclastic | conglomerate; volcaniclastic |
| Zyūmonzi Formation | max_thickness | 不一致 | 150 | 200 |
| Zyūmonzi Formation | unit_description | 不一致 | The Zyūmonzi Formation overlies the Suenomatuyama Formation … | The Zyūmonzi Formation overlies the Suenomatuyama Formation … |
| Suenomatuyama Formation | minor_lith | 不一致 | conglomerate; volcaniclastic; lava; intrusive rocks; mudston… | conglomerate |
| Suenomatuyama Formation | environment | 不一致 | shallow marine | marine |
| Suenomatuyama Formation | unit_description | 不一致 | The Suenomatuyama Formation conformably / slightly-unconform… | The Suenomatuyama Formation conformably / slightly-unconform… |
| Kadonosawa Formation | minor_lith | 不一致 | mudstone; sandstone; sandy mudstone; conglomerate | sandstone |
| Kadonosawa Formation | environment | 不一致 | shallow marine to bathyal | shallow marine |
| Kadonosawa Formation | basal_surface | 不一致 | conformable | unconformable |
| Kadonosawa Formation | unit_description | 不一致 | The Kadonosawa Formation conformably overlies the Yotuyaku F… | The Kadonosawa Formation conformably overlies the Yotuyaku F… |
| Yotuyaku Formation | minor_lith | 不一致 | volcaniclastic; intrusive rocks; muddy sandstone | muddy sandstone |
| Yotuyaku Formation | environment | 不一致 | fluvial indet.; lacustrine indet.; shallow marine | non-marine |
| Yotuyaku Formation | max_thickness | 取りこぼし | 600 | (空) |
| Yotuyaku Formation | unit_description | 不一致 | The Yotuyaku Formation unconformably overlies the previous r… | The Yotuyaku Formation unconformably overlies the previous r… |
| Ainoyama Formation | lithology | 不一致 | dacite lava | dacite; conglomerate |
| Ainoyama Formation | minor_lith | 取りこぼし | conglomerate | (空) |
| Ainoyama Formation | environment | 取りこぼし | non-marine | (空) |
| Ainoyama Formation | basal_surface | 取りこぼし | fault | (空) |
| Ainoyama Formation | unit_description | 不一致 | The Ainoyama Formation is composed of dacitic lava and congl… | The Ainoyama Formation is composed of dacitic lava and congl… |
| Nisatai Formation | lithology | 不一致 | rhyolite lapilli tuff | dacite; volcaniclastic |
| Nisatai Formation | minor_lith | 不一致 | tuff breccia; conglomerate; sandstone; mudstone; lignite | conglomerate; sandstone; mudstone |
| Nisatai Formation | unit_description | 不一致 | The Nisatai Formation is composed of upper welded rhyolitic … | The Nisatai Formation is composed of upper welded rhyolitic … |
| Ichinohe Pluton | lithology | 不一致 | monzodiorite; quartz monzonite | gabbro; quartz monzonite |
| Ichinohe Pluton | basal_surface | 取りこぼし | intrusive | (空) |
| Ichinohe Pluton | unit_description | 不一致 | The Ichinohe Pluton is lithologically characterised by two f… | The Ichinohe Pluton is lithologically characterised by two f… |
| Kuzumaki Formation | lithology | 不一致 | phyllitic mudstone; pelitic mixed rock | phyllite; mudstone; chert |
| Kuzumaki Formation | minor_lith | 不一致 | mafic; limestone; chert; siliceous mudstone; sandstone | mafic; limestone; sandstone |
| Kuzumaki Formation | environment | 取りこぼし | deep marine | (空) |
| Kuzumaki Formation | b_int | 不一致 | Middle Jurassic | Early Jurassic |
| Kuzumaki Formation | min_thickness | 捏造 | (空) | 2000 |
| Kuzumaki Formation | unit_description | 不一致 | The Kuzumaki Formation consists mainly of phyllitic mudstone… | The Kuzumaki Formation consists mainly of phyllitic mudstone… |
| Ibonai terrace deposits | lithology | 不一致 | gravel; sand | gravel; sand; silt |
| Ibonai terrace deposits | unit_description | 不一致 | The deposits of the lower lower terrace are the Horino and t… | The Ibonai terrace deposits are lower lower terrace deposits… |
| Rendaino terrace deposits | unit_description | 不一致 | The deposits of the higher lower terrace are the Maisawa and… | The Rendaino terrace deposits are higher lower terrace depos… |
| Hayawatari terrace deposits | unit_description | 不一致 | The middle terrace deposits are subdivided into the Kusagi a… | The Hayawatari terrace deposits are middle terrace deposits … |
| Mukaikawara terrace deposits | unit_description | 不一致 | The higher terrace deposits are subdivided into the Asanai a… | The Mukaikawara terrace deposits are higher terrace deposits… |
| Oritsumedake fan deposits | environment | 捏造 | (空) | alluvial fan |
| Esashika Formation | minor_lith | 取りこぼし | sand; mud | (空) |
| Esashika Formation | b_int | 不一致 | Calabrian | Jurassic |
| Esashika Formation | t_int | 不一致 | Chibanian | Jurassic |
| Esashika Formation | unit_description | 不一致 | The Esashika Formation, distributed only along the eastern f… | The Esashika Formation is distributed only along the eastern… |
| Toya Formation | lithology | 不一致 | pumice lapilli tuff | tuff; conglomerate; sandstone; mudstone |
| Toya Formation | minor_lith | 取りこぼし | tuff; mudstone; sandstone; conglomerate; lignite | (空) |
| Toya Formation | unit_description | 不一致 | The Toya Formation unconformably overlies the Jurassic strat… | The Toya Formation unconformably overlies the Jurassic strat… |
| Tsukanaigawa Pluton | basal_surface | 取りこぼし | intrusive | (空) |
| Kassenba Formation | minor_lith | 不一致 | siliceous mudstone; slaty mudstone; laminated mudstone; cher… | chert; mudstone; slate |
| Kassenba Formation | environment | 取りこぼし | deep marine | (空) |
| Kassenba Formation | b_int | 不一致 | Oxfordian | Middle Jurassic |
| Kassenba Formation | t_int | 不一致 | Kimmeridgian | Late Jurassic |
| Kassenba Formation | basal_surface | 取りこぼし | fault | (空) |
| Kassenba Formation | unit_description | 不一致 | The Kassenba Formation is characterised by at least two repe… | The Kassenba Formation is characterised by at least two repe… |
| Seki Formation | lithology | 不一致 | slaty mudstone; laminated mudstone | slate; mudstone |
| Seki Formation | minor_lith | 不一致 | chert; siliceous mudstone; sandstone | chert; sandstone |
| Seki Formation | environment | 取りこぼし | deep marine | (空) |
| Seki Formation | b_int | 不一致 | Kimmeridgian | Late Jurassic |
| Seki Formation | t_int | 捏造 | (空) | Late Jurassic |
| Seki Formation | basal_surface | 取りこぼし | fault | (空) |
| Seki Formation | unit_description | 不一致 | The Seki Formation is characterised by at least three repeti… | The Seki Formation is characterised by at least three repeti… |
| Takayashiki Formation | lithology | 不一致 | dismembered sandstone; dismembered mudstone; slaty mudstone | sandstone; mudstone |
| Takayashiki Formation | minor_lith | 不一致 | chert; siliceous mudstone; mafic | chert; mafic |
| Takayashiki Formation | environment | 取りこぼし | deep marine | (空) |
| Takayashiki Formation | b_int | 不一致 | Oxfordian | Jurassic |
| Takayashiki Formation | t_int | 捏造 | (空) | Jurassic |
| Takayashiki Formation | unit_description | 不一致 | The Takayashiki Formation consists mainly of alternating bed… | The Takayashiki Formation consists mainly of alternating bed… |

