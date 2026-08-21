# -*- coding: utf-8 -*-
"""
common.py — MacroStrat GSJ パイプライン共通モジュール

Macrostrat column-ingestion format v0.1.1 に準拠。
  https://github.com/Macrostrat/column-ingestion/blob/main/Format%20documentation.md

このモジュールは make_review_sheet.py と export_submission.py の両方から使われる。
列名・シート名の唯一の正（single source of truth）はここに置く。
"""

import json
import os
import re
import unicodedata

# ---------------------------------------------------------------------------
# 1. Macrostrat 公式フォーマット定義 (v0.1.1)
# ---------------------------------------------------------------------------

FORMAT_VERSION = "0.1.1"

# 提出用 units シートの列順（Ichinohe_Composite_column.xlsx と一致）
# 末尾の source_* は仕様外の追加列。公式仕様上「Extra columns will be skipped」
# とされているため取り込み時は無視されるが、出典追跡のために残す。
SUBMISSION_UNIT_COLS = [
    "unit_id",
    "col_id",
    "section_id",
    "position",
    "b_int",
    "b_prop",
    "t_int",
    "t_prop",
    "unit_name",
    "strat_name",
    "environment",
    "unit_description",
    "lithology",
    "minor_lith",
    "min_thickness",
    "max_thickness",
    "basal_surface",
    "lateral_relationship",
    "comments",
    "t_pos",
    # --- 仕様外（取り込み時に無視される。出典追跡用） ---
    "source_unit_id",
    "source_unit_name_ja",
]

SUBMISSION_COLUMN_COLS = [
    "col_id",
    "col_name",
    "col_group",
    "ref_ids",
    "date_collected",
    "col_type",
    "axis_type",
    "b_int",
    "t_int",
    "b_prop",
    "t_prop",
    "geom",
    "rgeom",
    "comments",
]

SUBMISSION_REF_COLS = [
    "ref_id",
    "title",
    "authors",
    "publication",
    "compilation",
    "organization",
    "date",
    "doi",
    "url",
    "comments",
]

SUBMISSION_IMAGE_COLS = [
    "col_ids",
    "image_name",
    "ref_id",
    "page_no",
    "fig_no",
    "description",
    "comments",
]

# metadata シートのキー順（key-value レイアウト）
METADATA_KEYS = [
    "project_name",
    "organization",
    "url",
    "project_id",
    "compile_date",
    "compiler_name",
    "compiler_orcid",
    "col_type",
    "axis_type",
    "b_int",
    "t_int",
    "b_prop",
    "t_prop",
    "rgeom",
    "position_unit",
    "time_unit",
    "timescale",
    "srid",
    "comments",
]

METADATA_DEFAULTS = {
    "organization": "UW Madison - Macrostrat Lab",
    "col_type": "column",
    "axis_type": "age",
    "position_unit": "meters",
    "time_unit": "Ma",
    "timescale": "international intervals",
    "srid": "EPSG:4326",
}

# ---------------------------------------------------------------------------
# 2. レビュー用シート定義
# ---------------------------------------------------------------------------

# REF_ 列 = 自動取得の参照専用（グレー）
#
# 情報源は2系統ある。混ぜずに並べて、人が見比べて選べるようにする。
#   [本文]  ZFKの derived（GSJが図幅説明書の日本語本文から抽出済みの構造化データ）
#           → 情報が細かい。確信度と該当箇所つき。LLMを通さないので幻覚が無い。
#   [要約]  英文Abstract を LLM が読んだもの
#           → 最初からMacrostrat向けの英語。ただし要約なので粗い。
REVIEW_REF_COLS = [
    "REF_unit_name_en",
    "REF_unit_name_ja",
    "REF_source",             # [本文] §節番号 / PDFページ（印刷ページ）/ 小見出し一覧
    "REF_age_text",
    "REF_age_from_abstract",  # [要約] 年代候補（原文引用つき）
    "REF_desc",               # [本文] 地層の説明文 **全文**（切らない）
    "REF_thickness",          # [本文] 層厚に触れた文を全部＋GSJ抽出値＋出典ページ
    "REF_lith_text",
    "REF_lith_candidates",
    "REF_lithology_gsj",      # [本文] 主岩相の候補（term_jp→term_en・該当文つき）
    "REF_minor_lith_gsj",     # [本文] 副次岩相の候補
    "REF_lithology",          # [要約] 主岩相の候補
    "REF_minor_lith",         # [要約] 副次岩相の候補
    "REF_strat_name",         # [本文] ZFK凡例の階層から組み立てた層序名
    "REF_environment",        # [要約] 堆積環境（config/vocab.json の公式語彙を優先）
    "REF_basal_surface",      # [本文]+[要約] 基底面の関係
    "REF_unit_description",   # [要約] 英文の地層記載（そのまま unit_description になる）
    "REF_column_id",           # [Vision/PDF] Column所属候補（west/east/...）
    "REF_sort_order",          # [Vision/PDF] Column別の重なり順候補（例: "3, 2"）
    "REF_place_names",         # [PDF] 抽出地名（ジオコーディング・重心算出用）
    # [Shape] geo_A.dbf のネイティブ属性。major_code で ZFK と結合する。
    "REF_shape_source",
    "REF_shape_match",
    "REF_shape_unit_name",
    "REF_shape_age_text",
    "REF_shape_lith_text",
    "REF_confidence_class",
    "REF_conflict",
]

# ★ 削除した列（復活させないこと）
#   REF_age_numeric   本文「時代」節からの正規表現抽出。十和田23層・一戸ともに0件。
#                     年代は REF_age_from_abstract（引用照合つき）のほうが確実だった。
#   REF_thickness_llm 英文Abstractからの層厚。同じく0件。Abstractは要約なので
#                     層厚を書かない。層厚は本文にしか無い → REF_thickness に一本化。
RETIRED_REF_COLS = ["REF_age_numeric", "REF_thickness_llm"]

# 編集対象列。順序はユーザー作業動線に合わせてある。
# ★ unit_name は sort_order と t_int の間（ユーザー指定位置）。
REVIEW_EDIT_COLS = [
    "unit_id",
    "column_id",
    "sort_order",
    "unit_name",        # ← (lithology) を除いた地層名。submission の unit_name になる
    "t_int",
    "b_int",
    "t_age_ma",         # ← 地層の上限（若い側）の年代 [Ma]。入れると t_prop が自動計算される
    "b_age_ma",         # ← 地層の下限（古い側）の年代 [Ma]。入れると b_prop が自動計算される
    "t_prop",           # ← 数式。年代を入れると自動で入る。直接上書きも可
    "b_prop",           # ← 同上
    "strat_name",
    "environment",
    "unit_description",
    "lithology",
    "minor_lith",
    "min_thickness",
    "max_thickness",
    "basal_surface",
    "lateral_relationship",
    "section_id",
    "t_pos",
    "comments",
]

REVIEW_UNIT_COLS = REVIEW_REF_COLS + REVIEW_EDIT_COLS

REVIEW_COLUMN_COLS = [
    "col_id",
    "col_name",
    "col_group",
    "ref_ids",
    "lat",
    "lng",
    "geom",
    "rgeom",
    "col_type",
    "axis_type",
    "b_int",
    "t_int",
    "b_prop",
    "t_prop",
    "date_collected",
    "comments",
]

# Excel の列幅。make_review_sheet と repair_layout の両方がここを見る。
# ★ 二箇所に持つと、片方に列を足し忘れて「値はあるのに見えない」状態になる（実際に起きた）。
#   REVIEW_UNIT_COLS に列を足したら、長い文字列を入れる列はここにも幅を足すこと。
COLUMN_WIDTHS = {
    # units_review
    "REF_unit_name_en": 32, "REF_unit_name_ja": 26, "REF_age_text": 15,
    "REF_source": 50, "REF_age_from_abstract": 52, "REF_thickness": 60,
    "REF_strat_name": 34, "REF_lithology": 40, "REF_minor_lith": 40,
    "REF_lithology_gsj": 46, "REF_minor_lith_gsj": 46,
    "REF_environment": 40, "REF_basal_surface": 46, "REF_unit_description": 60,
    "REF_column_id": 40, "REF_sort_order": 20, "REF_place_names": 50,
    "REF_lith_text": 24, "REF_desc": 60, "REF_lith_candidates": 28,
    "REF_shape_source": 48, "REF_shape_match": 18, "REF_shape_unit_name": 34,
    "REF_shape_age_text": 26, "REF_shape_lith_text": 34,
    "REF_confidence_class": 18, "REF_conflict": 28,
    "unit_name": 30, "strat_name": 28, "environment": 18, "unit_description": 45,
    "lithology": 24, "minor_lith": 20, "comments": 28,
    # columns_review
    "col_name": 38, "col_group": 24, "geom": 28, "rgeom": 20,
    # refs_review / images_review
    "title": 40, "authors": 45, "publication": 26, "organization": 32, "url": 45,
    "description": 55, "image_name": 34, "col_ids": 34,
    # key-value 系
    "key": 22, "value": 62, "項目": 30, "説明": 105,
    # intervals / abstract
    "interval": 30, "int_type": 14, "no": 6, "text": 130,
    "unit_name_ja": 26, "thickness_note": 100,
    "source_locator": 65, "field_name": 24, "candidate_value": 48,
    "source_type": 18, "confidence_class": 18, "selected": 12, "conflict": 28,
}
DEFAULT_COLUMN_WIDTH = 15


def column_width(name):
    return COLUMN_WIDTHS.get(str(name), DEFAULT_COLUMN_WIDTH)


# セルに入れる文字数の上限。
#
# ★ 方針転換（2026-08-07）: 長文の列は **切らずに全文を入れる**。
#   以前は「折り返しが数百行になって読めない」ことを理由に180字で切っていたが、
#   そのせいで肝心の情報（層厚がどの地点の話か、岩相の主従）が消えていて
#   判断そのものができなかった。折り返し（wrapText）を切って1行表示にすれば、
#   セルを選んで数式バーで全文を読める。切るより残すほうがよい。
#
# Excel のセル上限は 32,767 文字。それだけは超えないようにする。
EXCEL_CELL_HARD_MAX = 32_000

CELL_TEXT_MAX = {
    "REF_desc": EXCEL_CELL_HARD_MAX,          # 全文
    "REF_thickness": EXCEL_CELL_HARD_MAX,     # 全文（層厚は場所ごとに違うので全部要る）
    "REF_source": 500,
    "REF_unit_name_en": 110,
    "REF_unit_name_ja": 110,
    "REF_lith_candidates": 100,
    "REF_lith_text": 100,
    "REF_age_from_abstract": 300,
    "REF_lithology_gsj": 1200, "REF_minor_lith_gsj": 1200,
    "REF_strat_name": 200, "REF_lithology": 300, "REF_minor_lith": 300,
    "REF_environment": 300, "REF_basal_surface": 1200,
    "REF_unit_description": 2000,
    "unit_description": 2000,
    "comments": 600,
}
CELL_TEXT_DEFAULT_MAX = 1000

# 折り返しをしない列（長文なので1行表示にして、数式バーで読む）
NO_WRAP_COLS = {"REF_desc", "REF_thickness", "REF_lithology_gsj",
                "REF_minor_lith_gsj", "REF_basal_surface", "REF_source",
                "REF_unit_description"}


def truncate_for_cell(value, name):
    """列ごとの上限で切り詰める。切ったことが分かるよう末尾に … を付ける。"""
    if value is None:
        return value
    s = str(value)
    limit = CELL_TEXT_MAX.get(str(name), CELL_TEXT_DEFAULT_MAX)
    if len(s) <= limit:
        return value
    return s[:limit - 1] + "…"


# 実質的に必要なのは unit_name だけ。
# 公式仕様上、lithology・thickness・environment 等は空欄でも取り込める。
# t_int / b_int も "Only one of b_int/t_int is required" なので片方あればよい。
# 未入力は「エラー」ではなく「お知らせ」として扱う（推測で埋めないことを優先する）。
REQUIRED_UNIT_FIELDS = ["unit_name"]
OPTIONAL_REPORTED_FIELDS = ["lithology", "t_age_ma", "b_age_ma",
                            "min_thickness", "max_thickness", "environment"]
REQUIRED_CHRONO_FIELDS = ["t_int", "b_int"]

# ---------------------------------------------------------------------------
# 3. 地域コードマッピング
# ---------------------------------------------------------------------------

REGION_MAP = {
    "01": "01_宗谷",
    "02": "02_網走",
    "03": "03_根室",
    "04": "04_札幌",
    "05": "05_青森",
    "06": "06_秋田",
    "07": "07_岩手",
    "08": "08_宮城・山形",
    "09": "09_福島・新潟",
    "10": "10_関東",
    "11": "11_中部",
    "12": "12_関西",
    "13": "13_中国東部",
    "14": "14_中国西部",
    "15": "15_四国",
    "16": "16_九州北部",
    "17": "17_九州中部",
    "18": "18_九州南部",
    "19": "19_南西諸島",
}


# 地域コードの別名。ローマ字・漢字・都道府県名のどれでも引けるようにする。
# 注意: これは GSJ の図幅区画であって都道府県境ではない。
#       例えば 05_青森 には岩手県北部の図幅（一戸など）も含まれる。
REGION_ALIASES = {
    "01": ["soya", "wakkanai", "宗谷", "稚内", "北海道北部"],
    "02": ["abashiri", "kitami", "網走", "北見", "北海道東部"],
    "03": ["nemuro", "kushiro", "根室", "釧路", "北海道南東部"],
    "04": ["sapporo", "hakodate", "札幌", "函館", "北海道南西部"],
    "05": ["aomori", "青森", "青森県"],
    "06": ["akita", "秋田", "秋田県"],
    "07": ["morioka", "iwate", "盛岡", "岩手", "岩手県"],
    "08": ["sendai", "miyagi", "yamagata", "仙台", "宮城", "山形", "宮城県", "山形県"],
    "09": ["fukushima", "niigata", "福島", "新潟", "福島県", "新潟県"],
    "10": ["tokyo", "kanto", "東京", "関東"],
    "11": ["nagoya", "chubu", "名古屋", "中部", "東海"],
    "12": ["kyoto", "osaka", "kansai", "kinki", "京都", "大阪", "関西", "近畿"],
    "13": ["okayama", "chugoku-east", "岡山", "中国東部"],
    "14": ["hiroshima", "yamaguchi", "chugoku-west", "広島", "山口", "中国西部"],
    "15": ["kochi", "shikoku", "matsuyama", "高知", "四国", "松山"],
    "16": ["fukuoka", "kyushu-north", "福岡", "九州北部"],
    "17": ["oita", "kumamoto", "kyushu-central", "大分", "熊本", "九州中部"],
    "18": ["kagoshima", "miyazaki", "kyushu-south", "鹿児島", "宮崎", "九州南部"],
    "19": ["okinawa", "naha", "nansei", "沖縄", "那覇", "南西諸島"],
}


def resolve_region(query):
    """
    'aomori' / '青森' / '05' / '05_青森' のいずれからも地域コードを引く。
    見つからなければ None。
    """
    if not query:
        return None
    q = str(query).strip().lower()

    if q in REGION_MAP:                       # '05'
        return q
    if q.isdigit() and q.zfill(2) in REGION_MAP:
        return q.zfill(2)

    for code, folder in REGION_MAP.items():   # '05_青森' / '青森'
        if q == folder.lower() or q == folder.split("_", 1)[-1].lower():
            return code

    for code, aliases in REGION_ALIASES.items():
        if any(q == a.lower() for a in aliases):
            return code
    # 部分一致は最後の手段（'aomo' -> 05）
    for code, aliases in REGION_ALIASES.items():
        if any(q in a.lower() or a.lower() in q for a in aliases if len(a) > 2):
            return code
    return None


def region_label(code):
    """'05' -> '05_青森 (aomori)'"""
    folder = REGION_MAP.get(code, f"Region_{code}")
    romaji = next((a for a in REGION_ALIASES.get(code, []) if a.isascii()), "")
    return f"{folder} ({romaji})" if romaji else folder


def normalize_sheet_code(raw):
    """ZFK の 'G50_05_031' と 出版APIの '05031' / 'G050_05031' を 5桁 '05031' に正規化。"""
    if not raw:
        return ""
    s = str(raw)
    m = re.search(r"G0?50[_-](\d{5})", s)
    if m:
        return m.group(1)
    m = re.match(r"G?0?50?[_-](\d{2})[_-](\d{3})$", s)
    if m:
        return m.group(1) + m.group(2)
    digits = re.findall(r"\d+", s)
    joined = "".join(digits)
    # 'G50_05_031' -> '5005031' のような場合、末尾5桁を採用
    if len(joined) > 5 and joined.startswith("50"):
        joined = joined[2:]
    if len(joined) >= 5:
        return joined[-5:]
    return joined


def get_region_folder(sheet_code):
    code = normalize_sheet_code(sheet_code)
    if len(code) >= 2 and code[:2].isdigit():
        return REGION_MAP.get(code[:2], f"Region_{code[:2]}")
    return "Unknown_Region"


def gsj_doc_url(sheet_code):
    """図幅コード 05048 -> https://www.gsj.jp/Map/JP/docs/5man_doc/05/05_048.htm"""
    code = normalize_sheet_code(sheet_code)
    if len(code) != 5:
        return ""
    return f"https://www.gsj.jp/Map/JP/docs/5man_doc/{code[:2]}/{code[:2]}_{code[2:]}.htm"


# ---------------------------------------------------------------------------
# 4. 文字列ユーティリティ
# ---------------------------------------------------------------------------

def strip_trailing_paren(text):
    """
    末尾の括弧グループを1つだけ取り除く（入れ子対応）。

    'Shitazaki Formation (siltstone)'      -> 'Shitazaki Formation'
    'Towada Deposits (dacite (hbl) tuff)'  -> 'Towada Deposits'
    'Gravel and sand'                      -> 'Gravel and sand'   (変化なし)
    '(Gravel and sand)'                    -> '(Gravel and sand)'  (中身が空になるので保持)
    """
    if not text:
        return ""
    s = str(text).strip()
    # 全角括弧も対象にする
    s_norm = s.replace("（", "(").replace("）", ")")
    if not s_norm.endswith(")"):
        return s
    depth = 0
    for i in range(len(s_norm) - 1, -1, -1):
        ch = s_norm[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                head = s[:i].strip()
                return head if head else s
    return s


TITLE_SUFFIXES = ("地域の地質", "地域地質", "の地質")

# 末尾の西暦。括弧付き「(2005)」「（2005）」「[2005]」と裸の「2005」の両方に対応。
# GSJ出版APIの title_j は '十和田 (2005)' 形式で来るため、括弧を見落とすと
# 年が二重に付いて '十和田 2005 2005' というフォルダ名になる。
_TRAILING_YEAR = re.compile(r"[\s　]*[（(\[]?\s*(1[89]\d{2}|20\d{2})\s*[)）\]]?[\s　]*$")


def _strip_trailing_years(text):
    """末尾の西暦を（複数あっても）全て取り除き、(残りの文字列, 最初に見つけた年) を返す。"""
    s = str(text or "").strip()
    year = ""
    while True:
        m = _TRAILING_YEAR.search(s)
        if not m or m.start() == 0:
            break
        year = year or m.group(1)
        s = s[: m.start()].strip()
    return s, year


def canonical_map_title(pub_title="", zfk_title="", pub_year=""):
    """
    図幅の表示名を、取得元に依存しない形に正規化する。

    同じ図幅でもタイトルの出どころで表記が違う:
      出版API  -> '十和田 2005'
      ZFK      -> '十和田地域の地質'
    どちらから来ても '十和田 2005' に揃える。通信状況でフォルダ名が変わると
    同じ図幅に対して別フォルダができてしまうため、この正規化は必須。

    出版APIの title_j は '十和田 (2005)' のように年が括弧付きで来る。
    この関数は冪等（何度かけても結果が変わらない）。

    >>> canonical_map_title('十和田 (2005)', '十和田地域の地質', 2005)
    '十和田 2005'
    >>> canonical_map_title('', '十和田地域の地質', 2005)
    '十和田 2005'
    >>> canonical_map_title('十和田 2005', '', '')      # 冪等性
    '十和田 2005'
    >>> canonical_map_title('一戸 2018', '', '')
    '一戸 2018'
    """
    year = ""
    m = re.search(r"(1[89]\d{2}|20\d{2})", str(pub_year) if pub_year else "")
    if m:
        year = m.group(1)

    base = ""
    for src in (pub_title, zfk_title):
        s, found_year = _strip_trailing_years(src)
        if not s:
            continue
        year = year or found_year
        for suf in TITLE_SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)].strip()
                break
        s = s.replace("5万分の1地質図幅", "").replace("「", "").replace("」", "").strip()
        # 接尾辞を落とした結果、また年が末尾に出てくる場合に備えてもう一度
        s, found_year2 = _strip_trailing_years(s)
        year = year or found_year2
        if s:
            base = s
            break

    if not base:
        return ""
    return f"{base} {year}".strip() if year else base


def safe_folder_name(text):
    """フォルダ名に使える文字だけ残す。"""
    return "".join(c for c in str(text or "") if c.isalnum() or c in " -_").strip()


def format_gsj_author(name_en):
    """
    GSJ の name_en 'Takashi KUDO' / 'Tomohiro TUZINO' を 'Kudo, T.' 形式にする。
    姓は全大文字で表記されている前提。判別できない場合は原文を返す。
    """
    if not name_en:
        return ""
    tokens = [t for t in str(name_en).replace(",", " ").split() if t]
    if not tokens:
        return ""
    surname_tokens = [t for t in tokens if t.isupper() and len(t) > 1]
    if not surname_tokens:
        return str(name_en).strip()
    given_tokens = [t for t in tokens if t not in surname_tokens]
    surname = " ".join(w.capitalize() for w in surname_tokens)
    initials = " ".join(f"{t[0].upper()}." for t in given_tokens)
    return f"{surname}, {initials}".strip().rstrip(",")


def join_authors(formatted):
    """['Kudo, T.', 'Nakae, S.'] -> 'Kudo, T. and Nakae, S.'"""
    items = [f for f in formatted if f]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def make_ref_id(authors_en, pub_year, map_id):
    """先頭著者の姓 + 出版年 で ref_id を作る（例: kudo2005）。"""
    year = str(pub_year) if pub_year else ""
    if authors_en:
        first = authors_en[0]
        tokens = [t for t in str(first).replace(",", " ").split() if t]
        surname = next((t for t in tokens if t.isupper() and len(t) > 1), None)
        if surname is None and tokens:
            surname = tokens[-1]
        if surname:
            slug = re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", surname).lower())
            if slug:
                return f"{slug}{year}"
    return f"gsj{map_id}{('_' + year) if year else ''}"


def slugify_col_id(title_en_or_ja, suffix=None):
    """'Ichinohe' + 'west' -> 'ichinohe-west'。日本語しかない場合は map_id ベースにフォールバック。"""
    base = unicodedata.normalize("NFKD", str(title_en_or_ja or "")).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    if not base:
        return None
    return f"{base}-{suffix}" if suffix else base


# ---------------------------------------------------------------------------
# 年代 → t_prop / b_prop の計算
# ---------------------------------------------------------------------------

_INTERVALS_CACHE = None


def load_intervals(path=None):
    """config/intervals.json（Macrostrat公式intervalの b_age/t_age）を読む。"""
    global _INTERVALS_CACHE
    if _INTERVALS_CACHE is None:
        p = path or os.path.join("loop2_governance", "config", "intervals.json") if not os.path.exists(os.path.join("config", "intervals.json")) else os.path.join("config", "intervals.json")
        if not os.path.exists(p):
            here = os.path.dirname(os.path.abspath(__file__))
            p = os.path.join(os.path.dirname(here), "config", "intervals.json")
        _INTERVALS_CACHE = load_json(p) or {}
    return _INTERVALS_CACHE


# Excel の intervals シートに載せる範囲。
# 1715件すべてを載せると sharedStrings が肥大化し、利用者にとってもノイズになる。
# 「age_mapping が生成しうるもの」＋「国際年代表」に絞れば 193 件で足りる
# （一戸完成形15種・十和田7種のいずれも欠落ゼロを確認済み）。
# なお Python 側の interval_bounds() は常に全1715件を見るので、
# 表に無い interval を手入力しても export では正しく計算される。
INTERNATIONAL_TIMESCALES = {
    "international intervals", "international ages", "international epochs",
    "international periods", "international eras", "international eons",
}


def intervals_for_excel():
    """Excel の参照表に載せる interval を絞り込んで返す。"""
    iv = load_intervals()
    keep = {k for k, v in iv.items()
            if INTERNATIONAL_TIMESCALES & {t for t in (v.get("timescales") or []) if t}}
    # age_mapping が生成しうるものは必ず含める
    am = load_json(os.path.join("config", "age_mapping.json")) or {}
    if not am:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        am = load_json(os.path.join(here, "config", "age_mapping.json")) or {}
    for v in am.values():
        for k in ("t_int", "b_int"):
            if v.get(k) in iv:
                keep.add(v[k])
    # Figure legends and legacy GSJ text commonly use these formal,
    # internationally-defined subdivision names even when age_mapping.json
    # currently maps a Japanese label to a broader parent interval.  They are
    # valid review choices and must remain available to the vision validator.
    #
    # 中生代の Early/Late Cretaceous などは ICS の正式な epoch なので
    # INTERNATIONAL_TIMESCALES のタグ経由で自動的に入る。
    # いっぽう新生代の Early Pliocene などは epoch（Pliocene）の非公式な細分で
    # あってタグが付かないため、ここに明示的に並べないと弾かれる。
    # 系列ごと揃えておかないと「Late Miocene は通るのに Early Pliocene は通らない」
    # という非対称が生じ、図幅ごとに再発する。
    for name in (
        "Early Pleistocene", "Middle Pleistocene", "Late Pleistocene",
        "Early Pliocene", "Late Pliocene",
        "Early Miocene", "Middle Miocene", "Late Miocene",
        "Early Oligocene", "Late Oligocene",
        "Early Eocene", "Middle Eocene", "Late Eocene",
        "Early Paleocene", "Late Paleocene",
    ):
        if name in iv:
            keep.add(name)
    return {k: iv[k] for k in keep}


def interval_bounds(name):
    """interval名 -> (b_age, t_age)。見つからなければ (None, None)。大小文字は無視。"""
    if is_blank(name):
        return (None, None)
    iv = load_intervals()
    key = str(name).strip()
    v = iv.get(key)
    if v is None:
        low = key.lower()
        v = next((val for k, val in iv.items() if k.lower() == low), None)
    if not v:
        return (None, None)
    return (v.get("b_age"), v.get("t_age"))


# 数値年代の表記ゆれ。Ma / ka / 年BP / 単位なし（Ma扱い）に対応。
_AGE_PAT = re.compile(
    r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"      # 数値（1,000区切りも許容）
    r"\s*(?:±\s*\d+(?:\.\d+)?\s*)?"                       # 誤差は読み飛ばす
    r"\s*(Ma|ka|Ka|kyr|年BP|yBP|ybp)?",                   # 単位
    re.IGNORECASE,
)


def parse_age_ma(value):
    """
    '15.3ka' / '0.0153' / '0.40Ma' / '10,400年BP' などを Ma の float にする。
    解釈できなければ None（推測はしない）。
    """
    if is_blank(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    s = str(value).strip().replace("　", " ")
    m = _AGE_PAT.match(s)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit in ("ka", "kyr"):
        return num / 1000.0
    if unit in ("年bp", "ybp"):
        return num / 1_000_000.0
    return num          # Ma、または単位なし（Ma とみなす）


def compute_prop(age_ma, interval_name):
    """
    interval 内での相対位置（0〜1）を返す。

    Macrostrat の定義:
        prop = (interval の b_age − 地層の年代) / (interval の b_age − interval の t_age)
        0 = interval の下端（古い側） / 1 = interval の上端（若い側）

    完成形 Ichinohe での検証:
        Shitazaki Formation  b_int=Tortonian(11.63–7.246) b_prop=0.258 -> 10.499 Ma
        Yanagisawa Formation t_int=Tortonian              t_prop=0.258 -> 10.499 Ma
        整合関係で境界を共有しているので同じ値になる（辻褄が合う）。

    ★ ここは純粋な計算だけを行い、0〜1 の範囲確認はしない（呼び出し側が
      「範囲外です」と具体的に知らせられるようにするため）。
      interval が不明・境界が不正なときだけ None。
    """
    age = parse_age_ma(age_ma)
    if age is None:
        return None
    b, t = interval_bounds(interval_name)
    if b is None or t is None or b <= t:
        return None
    return (b - age) / (b - t)


def age_from_prop(prop, interval_name):
    """compute_prop の逆。prop から Ma を戻す（検算・表示用）。"""
    if is_blank(prop):
        return None
    try:
        p = float(prop)
    except (TypeError, ValueError):
        return None
    b, t = interval_bounds(interval_name)
    if b is None or t is None:
        return None
    return b - p * (b - t)


_NUM = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"
_ERR = r"(?:\s*±\s*\d+(?:\.\d+)?)?"
_UNIT = r"(Ma|ka|年BP)"

# 「6.8～6.4Ma」「10,400～14,000年BP」のような範囲表記。
# 単位は後ろの数値にしか付かないので、前の数値にも同じ単位を適用する。
_TEXT_AGE_RANGE = re.compile(
    rf"({_NUM}){_ERR}\s*[～〜~－—–-]\s*({_NUM}){_ERR}\s*{_UNIT}"
)
# 単独表記「0.40Ma」「55ka」
_TEXT_AGE_PAT = re.compile(rf"({_NUM}){_ERR}\s*{_UNIT}")


def extract_age_candidates(text, max_items=8):
    """
    「時代」節から数値年代の候補を拾って Ma 昇順の文字列にする。

    ★ これはあくまで候補の提示。1つの地層に矛盾する複数の値が並ぶことが多く
      （例: 八甲田第1期火砕流は 0.53〜1.28Ma の7個）、どれを採るかは
      「本研究では〜を採用」等の文を読んだ人間の判断が必要。自動で選ばない。
    """
    if not text:
        return ""
    m = re.search(r"時代\n(.*?)(?:\n岩石記載|\n対比|\Z)", str(text), re.S)
    section = m.group(1) if m else ""
    if not section:
        return ""

    found = {}

    def add(num, unit):
        raw = f"{num}{unit}"
        ma = parse_age_ma(raw)
        if ma is not None and ma > 0:
            found.setdefault(round(ma, 6), raw)

    # 範囲表記を先に処理し、消費した部分は取り除く
    rest = section
    for m in _TEXT_AGE_RANGE.finditer(section):
        lo, hi, unit = m.group(1), m.group(2), m.group(3)
        add(lo, unit)
        add(hi, unit)
        rest = rest.replace(m.group(0), " ")

    for num, unit in _TEXT_AGE_PAT.findall(rest):
        add(num, unit)

    if not found:
        return ""
    items = [f"{found[k]}={k:g}Ma" for k in sorted(found)]
    return "; ".join(items[:max_items])


# ---------------------------------------------------------------------------
# area（Column）ごとの値をカンマで書く
# ---------------------------------------------------------------------------

# ★ カンマで area（Column）ごとに分けてよい列の **ホワイトリスト**。
#
#   以前は「REF_ と _ 以外は全部分解」にしていたが、それだと英文の
#   unit_description が「, 」の数がたまたま Column 数と一致したときに
#   真っ二つに割れてしまった（実際に十和田で6行が壊れた）。
#   分解してよいのは「短い値が1つ入る欄」だけ。散文や、カンマが本来の
#   区切りである列（strat_name の階層、lithology の属性列挙）は入れない。
PER_COLUMN_SPLIT_FIELDS = (
    "min_thickness", "max_thickness",
    "t_age_ma", "b_age_ma", "t_prop", "b_prop",
    "t_int", "b_int",
    "section_id", "t_pos", "sort_order",
    "basal_surface", "lateral_relationship", "environment",
)

# そのうち、カンマが「複数値の列挙」としても使われうる列。
# area分割と見分けがつかないので、分解したら警告を出す。
COMMA_AMBIGUOUS_FIELDS = ("environment", "basal_surface", "lateral_relationship")

# ★ 絶対に分解してはいけない列（散文・カンマが仕様上の区切り）。
#   公式仕様:
#     strat_name  「Use commas to separate child and parent within a single chain」
#     lithology   「<attribute> <lith>, <attribute> <lith>; ...」
NEVER_SPLIT_FIELDS = ("unit_name", "unit_description", "comments",
                      "strat_name", "lithology", "minor_lith")


def split_per_column(value, n_columns):
    """
    1つのセルに Column ごとの値をカンマで並べたものを分解する。

      column_id = "1, 2" の行で min_thickness = "10, 20"
        -> Column1 に 10、Column2 に 20

    ★ 安全側に倒す。次の場合だけ分解し、それ以外は値をそのまま全Columnに使う:
        ・その行が複数Columnにまたがっている（n_columns >= 2）
        ・カンマ区切りの個数が Column 数とちょうど一致する

      lithology の "gravel, sand" のように、カンマが本来の区切りである列もある。
      個数が一致しないものを勝手に割り振ると壊れるので分解しない。
      複数の岩相を書きたいときは公式仕様どおり ';' を使う。

    戻り値: (Columnごとの値のリスト, 分解したか)
    """
    # カンマが本来の区切りとして使われる列は、area分割と見分けがつかない。
    # 誤分解が起きたことに気づけるよう、呼び出し側で警告を出す（NEEDS_CARE を参照）。
    if is_blank(value):
        return [""] * max(1, n_columns), False
    s = str(value).strip()
    if n_columns < 2 or "," not in s:
        return [value] * max(1, n_columns), False

    parts = [p.strip() for p in s.split(",")]
    if len(parts) != n_columns:
        return [value] * n_columns, False       # 個数が合わない -> 触らない
    return parts, True


# ---------------------------------------------------------------------------
# 層厚の記述を本文から拾う
# ---------------------------------------------------------------------------

# 「層厚」という語を含む文
_THICK_WORD = re.compile(r"層厚|厚さ|厚は|thickness")
# 層厚の続きを述べる文（「一方，北半部では最大で10mである」のように語が無いことがある）
_THICK_HINT = re.compile(r"最大|最小|以上|以下|前後|程度")
# メートルの数値。cm / mm を誤って拾わないようにする
_THICK_HAS_NUM = re.compile(r"(?<![cmｃｍ\d])\d+(?:\.\d+)?\s*(?:m|ｍ|メートル)(?![mｍ])")


def extract_thickness_notes(text, max_items=6):
    """
    本文から層厚に触れている文を抜き出す。

    層厚は場所によって変わることが多く（「南半部では最大20m、北半部では最大10m」）、
    1つの数値に決められない。判断できるように文ごと残す。

    ★ 「層厚」の語がある文だけでは足りない。続きの文で area 別の値を述べることが
      多いので（「一方，北半部では最大で10mである」）、直前が層厚の文なら
      最大/最小などを含む文も拾う。cm・mm は層厚ではないので除く。
    """
    if not text:
        return []
    out, prev_was_thick = [], False
    for raw in re.split(r"[。．\n]", str(text)):
        s = " ".join(raw.split()).strip()
        if len(s) < 6 or not _THICK_HAS_NUM.search(s):
            prev_was_thick = False
            continue
        is_thick = bool(_THICK_WORD.search(s))
        # 層厚の文、または層厚の文に続く「最大…m」のような文
        if is_thick or (prev_was_thick and _THICK_HINT.search(s)):
            if s not in out:
                out.append(s)
            prev_was_thick = True
            if len(out) >= max_items:
                break
        else:
            prev_was_thick = False
    return out


# ---------------------------------------------------------------------------
# section_id / t_pos の導出
# ---------------------------------------------------------------------------

def derive_sections(bounds, rel_gap=0.15, min_gap_ma=0.5):
    """
    年代の「大きな」すき間から section を切る。

    公式仕様: "Sections can also be inferred from gaps in
               chronostratigraphic position fields"
    section = すき間で区切られた地層のまとまり（不整合など）。

    ★ すき間を少しでも見つけたら切る、では駄目。
      地層はほぼ必ず微小なすき間を持つので、全層が別sectionになってしまう。
      Ichinohe完成形は42層すべて section_id が空。つまり section は
      「はっきりした断絶があるときだけ」使うもの。

      そこで次の両方を満たすときだけ切る:
        ・すき間が Column 全体の年代幅の rel_gap（既定15%）を超える
        ・すき間が min_gap_ma（既定0.5 Ma）以上ある
      さらに、結果として section が層数の半分を超える（＝切りすぎ）場合は
      判断できなかったとみなして全て None を返す。

    引数 bounds: 上（新しい）から下（古い）に並べた (b_age_ma, t_age_ma) のリスト。
    戻り値: section 番号のリスト（1始まり）。判断できなければ全て None。
    """
    n = len(bounds)
    if not n:
        return []
    known = [(b, t) for b, t in bounds if b is not None or t is not None]
    if len(known) < 2:
        return [None] * n

    ages = [v for b, t in bounds for v in (b, t) if v is not None]
    span = max(ages) - min(ages)
    if span <= 0:
        return [None] * n
    threshold = max(span * rel_gap, min_gap_ma)

    sections, cur = [], 1
    for i, (b, t) in enumerate(bounds):
        if i > 0:
            prev_b = bounds[i - 1][0]        # 1つ上の層の下限（古い側）
            if prev_b is not None and t is not None and (t - prev_b) > threshold:
                cur += 1
        sections.append(cur)

    if cur > max(1, n // 2):                 # 切りすぎ = 判断できていない
        return [None] * n
    if cur == 1:                             # すき間なし = section を使う必要がない
        return [None] * n
    return sections


def derive_t_pos(positions):
    """
    t_pos（上端の位置）を求める。

    ★ 最上位の層には必ず t_pos を入れる。公式仕様にこうある:

        "Units that are unbounded at the top or bottom of a section are
         **dropped during ingestion**, but their t_pos, b_pos values are still
         used to infer the bounds of units above or below."
        "this can allow a section to be defined with a single `position`
         column, **if an unbounded unit is included at the top**"

      position（= b_pos）だけだと最上位の層は上端が決まらず「unbounded」になり、
      取り込み時に落ちる。一戸完成形も各Columnの最上位に t_pos = max+1 を
      入れている（central: position 7 → t_pos 8 / west: 18 → 19 / east: 15 → 16）。

    それ以外の層は、上に隣接する層から推定されるので空欄でよい。
    ただし position が重複している（横に並ぶ層がある）場合は、
    上に隣接する層の position を明示する。

    引数 positions: 上（新しい）から下（古い）に並べた position のリスト。
    戻り値: 同じ長さの t_pos リスト（不要な要素は None）。
    """
    out = [None] * len(positions)
    valid = [p for p in positions if p is not None]
    if not valid:
        return out
    top = max(valid)
    for i, p in enumerate(positions):
        if p is None:
            continue
        if p == top:
            out[i] = top + 1                  # ★ 最上位 = 上端が無い → 必ず入れる
        elif positions.count(p) >= 2:
            above = [q for q in positions if q is not None and q > p]
            out[i] = min(above) if above else p + 1
    return out


def _is_ics(entry):
    """Macrostrat の国際年代層序（international *）に属する interval か。"""
    return any(t and str(t).startswith("international")
               for t in (entry.get("timescales") or []))


def best_interval_for_age(age_ma, like=None):
    """
    その年代に合う国際年代層序の interval を1つ選ぶ。

    `like` に今の interval 名を渡すと、**その区分と幅が近いもの**を選ぶ。
    「元の資料がどのくらいの細かさで言っていたか」を保つための工夫で、
    これをしないと 0 Ma に対して Meghalayan（4.2 千年前以降）のような、
    資料が主張していない細かさの区分を勝手に当ててしまう。

    実際の図幅データでの挙動:
        0.4 Ma / 元 Early Pleistocene(幅0.78) -> Chibanian(幅0.645)
        0.99 Ma / 元 Early Pleistocene        -> Calabrian(幅1.03)
        0 Ma   / 元 Late Pleistocene(幅0.117) -> Holocene(幅0.0117)
        5.1 Ma / 元 Late Miocene(幅6.3)       -> Pliocene(幅2.75)

    見つからなければ None。
    """
    import math

    a = parse_age_ma(age_ma)
    if a is None:
        return None
    iv = load_intervals() or {}
    want = want_type = None
    if like and not is_blank(like):
        e = iv.get(str(like).strip())
        if e:
            try:
                want = float(e["b_age"]) - float(e["t_age"])
                want_type = e.get("int_type")
            except (KeyError, TypeError, ValueError):
                want = None

    best, best_key = None, None
    for name, v in iv.items():
        if not _is_ics(v):
            continue
        try:
            b, t = float(v["b_age"]), float(v["t_age"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (t <= a <= b) or b <= t:
            continue
        w = b - t
        if want and want > 0:
            # 幅は桁で比べる（0.0042 と 0.0117 の差は、2.58 と 2.75 の差より大きい）。
            # 小数第1位で丸めて「だいたい同じ細かさ」をひとまとめにし、
            # 同点なら元と同じランク（age / epoch / period）を優先、次に狭いほう。
            key = (round(abs(math.log(w / want)), 1),
                   0 if v.get("int_type") == want_type else 1, w)
        else:
            key = (w, 0, w)                       # 手がかりが無ければ最も狭いもの
        if best_key is None or key < best_key:
            best, best_key = name, key
    return best


def fits_interval(age_ma, interval):
    """年代がその interval の範囲に収まっているか。判定できなければ None。"""
    a = parse_age_ma(age_ma)
    b, t = interval_bounds(interval)
    if a is None or b is None or t is None:
        return None
    return t <= a <= b


def intervals_containing(age_ma, max_items=4):
    """
    その年代を含む interval の名前を返す（細かい順）。

    年代を入れたのに prop が 0〜1 に収まらないとき、「では正しくはどれか」を
    その場で示すために使う。ZFK の粗い時代区分（「更新世」）が
    age_mapping で Late Pleistocene に落ちてしまう取り違えがよく起きる。
    """
    # ★ 国際年代層序の区分だけを候補にする。
    #   生層序帯（zone）や古地磁気（chron）まで出すと、
    #   「NN20 / Collosphaera tuberosa」のような使いどころの無い候補が並ぶ。
    chrono = ("age", "sub-age", "epoch", "sub-epoch", "superepoch", "period")
    a = parse_age_ma(age_ma)
    if a is None:
        return []
    hits = []
    for name, v in (load_intervals() or {}).items():
        if str(v.get("int_type", "")).lower() not in chrono:
            continue
        try:
            b, t = float(v["b_age"]), float(v["t_age"])
        except (KeyError, TypeError, ValueError):
            continue
        if t <= a <= b:
            hits.append((b - t, name))
    hits.sort()                       # 幅の狭い＝細かい区分を先に
    return [n for _, n in hits[:max_items]]


def split_aligned_values(value, count):
    """Expand a scalar or comma-list to exactly ``count`` aligned values.

    Empty comma-list elements are preserved because ``"4, "`` means a value
    for the first Column and no value for the second.  A scalar is replicated,
    retaining backward compatibility with older review rows.
    """
    if count <= 0:
        return []
    if isinstance(value, (list, tuple)):
        parts = list(value)
    elif count > 1 and isinstance(value, str) and "," in value:
        parts = [part.strip() for part in value.split(",")]
    else:
        parts = [value]
    if len(parts) == count:
        return parts
    if len(parts) == 1:
        return parts * count
    return [value] * count


def _sort_number(value):
    if is_blank(value) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def auto_t_pos(column_ids, sort_orders):
    """
    レビューシート用に t_pos を求める。

    ★ column_id は「1, 2」のようにカンマで複数Columnを指せる。
      文字列のままグループ分けすると「1, 2」が1つのColumnとして扱われ、
      間違った t_pos が入る（実際にそうなった）。必ず展開してから計算する。

    ``sort_order`` 自体も Column 数と1:1に整列したカンマ列を受け付ける。
    複数Column行の戻り値は、値が同じ場合も必ず同数のカンマ列にする。

    戻り値: 行ごとの t_pos（値なしは ""）
    """
    n = len(column_ids)
    members = [[c.strip() for c in str(cid or "").split(",") if c.strip()]
               for cid in column_ids]
    all_cols = list(dict.fromkeys(c for m in members for c in m))
    per_row = [[None] * len(row_members) for row_members in members]
    aligned_sorts = [
        split_aligned_values(sort_orders[index], len(row_members))
        for index, row_members in enumerate(members)
    ]

    for col in all_cols:
        idxs = [i for i in range(n) if col in members[i]]
        sorts = [
            _sort_number(aligned_sorts[i][members[i].index(col)])
            for i in idxs
        ]
        pos = derive_positions(sorts)
        for i, tp in zip(idxs, derive_t_pos(pos)):
            per_row[i][members[i].index(col)] = tp

    out = []
    for i, vals in enumerate(per_row):
        if not vals or all(v is None for v in vals):
            out.append(", ".join("" for _ in vals) if len(vals) > 1 else "")
        elif len(vals) == 1:
            out.append(vals[0])
        else:
            # 複数Columnは常に1:1のカンマ列（欠けは空文字で位置を保つ）。
            out.append(", ".join("" if v is None else str(v) for v in vals))
    return out


# ---------------------------------------------------------------------------
# prop（interval 内の相対位置）の丸め
# ---------------------------------------------------------------------------

PROP_DECIMALS = 3            # Excel の表示桁数
PROP_NUMBER_FORMAT = "0.000"


def event_prop_bracket(prop, decimals=PROP_DECIMALS):
    """
    瞬間的なイベント（噴火など）の prop を、表示桁で同じ値に丸まる範囲に広げる。

    火砕流やテフラは一瞬で積もるので、本来 b_prop と t_prop は同じ値になる。
    しかし公式仕様は「b_prop must be less than t_prop」を要求する。そこで
    **表示桁（小数第3位）に四捨五入すると同じ値になる範囲** を上下端として使う。

        prop = 0.13212  →  表示は 0.132
          下端 b_prop = 0.132 - 0.0005   = 0.1315
          上端 t_prop = 0.132 + 0.0005 - ε
                      = 0.1324999…       → 下5桁で切り捨て → 0.13249

    どちらも小数第3位に丸めれば 0.132 に戻る。0〜1 をはみ出す場合は端で止める。

    戻り値: (b_prop, t_prop)
    """
    if prop is None:
        return None, None
    try:
        p = float(prop)
    except (TypeError, ValueError):
        return None, None
    step = 10 ** (-decimals)                     # 0.001
    n = round(p / step)                          # 表示される値を整数で持つ
    lo = (n * 100 - 50) / (100 / step)           # r - 0.0005
    hi = (n * 100 + 49) / (100 / step)           # r + 0.00049（切り捨て済み）
    return max(0.0, round(lo, decimals + 2)), min(1.0, round(hi, decimals + 2))


def props_from_ages(unit_name, t_int, b_int, t_age_ma, b_age_ma, *extra_names):
    """
    年代から (b_prop, t_prop, 噴火イベントか) を求める。

    ふつうの地層:
        b_prop = 下限の年代を b_int の中で見た割合
        t_prop = 上限の年代を t_int の中で見た割合

    噴火イベント（年代が1点 かつ 地層名が火砕流・テフラ・溶岩など）:
        本来は上下が同じ位置。しかし仕様が b_prop < t_prop を求めるので、
        表示桁で同じ値に丸まる最小の幅にする（event_prop_bracket 参照）。

    ★ 「年代が1点」だけでは噴火と判定しない。段丘堆積物などが1点の年代で
      出てくることがあり、それらは実際には期間をもって堆積している。
      地層名の語（火砕流／テフラ／pyroclastic ...）と両方揃ったときだけ。
    """
    from gsj_derived import is_eruption_unit
    b_age, t_age = parse_age_ma(b_age_ma), parse_age_ma(t_age_ma)

    # 年代が1点しかない = 上端と下端が同じ位置になる。
    # ★ 噴火かどうかに関わらず、そのままだと b_prop == t_prop になって
    #   公式仕様の「b_prop must be less than t_prop」に反する。無効なデータを
    #   出すわけにはいかないので、どちらの場合も丸め幅を使う。
    #   噴火かどうかは「なぜ1点なのか」の説明であって、扱いは同じ。
    one_point = (b_age is not None and t_age is not None and b_age == t_age) or \
                (b_age is not None and t_age is None) or \
                (b_age is None and t_age is not None)
    if one_point:
        age = b_age if b_age is not None else t_age
        iv = b_int or t_int
        p = compute_prop(age, iv)
        eruption = is_eruption_unit(unit_name, *extra_names)
        # ★ 範囲外なら何も返さない。無効な値（b_prop=2.79 など）を
        #   こちらから作ってしまうと、あとでそれが「手入力」として
        #   尊重され、直す機会を失う（実際にそうなった）。
        if p is None or not (0 <= p <= 1):
            return None, None, eruption
        bp, tp = event_prop_bracket(p)
        return bp, tp, eruption

    return (compute_prop(b_age, b_int) if b_age is not None else None,
            compute_prop(t_age, t_int) if t_age is not None else None,
            False)


# ---------------------------------------------------------------------------
# Macrostrat 公式語彙（config/vocab.json）との照合
# ---------------------------------------------------------------------------

_VOCAB_CACHE = None


def load_vocab():
    """config/vocab.json を読む。`python run.py vocab` で作られる。"""
    global _VOCAB_CACHE
    if _VOCAB_CACHE is None:
        p = os.path.join("loop2_governance", "config", "vocab.json") if not os.path.exists(os.path.join("config", "vocab.json")) else os.path.join("config", "vocab.json")
        if not os.path.exists(p):
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            p = os.path.join(here, "config", "vocab.json")
        _VOCAB_CACHE = load_json(p) or {}
    return _VOCAB_CACHE


def _vocab_index(key):
    v = load_vocab().get(key) or []
    return {str(t).strip().lower(): str(t).strip() for t in v}


# ``lith_att`` に無い修飾語を無制限に捨てると、別の岩石名を誤って
# 作り出してしまう。ここには、除去しても末尾のコア岩相を変えないと
# 判断できる記載上の修飾語だけを置く。
LITHOLOGY_FALLBACK_MODIFIERS = {
    "pumice", "pumiceous", "pumice-bearing",
    "altered", "strongly altered", "weakly altered",
}

# The extraction proposal asks these descriptive prefixes to be reduced to
# their core Macrostrat lithology.  ``tuffaceous`` is intentionally narrowed to
# sedimentary cores: ``tuffaceous breccia`` is the controlled representation
# of GSJ ``tuff breccia`` and must not be collapsed to generic breccia.
LITHOLOGY_CORE_MODIFIERS = {
    "pumiceous", "calcareous", "siliceous", "argillaceous",
    "conglomeratic", "felsic", "mafic", "porphyritic",
}
_TUFFACEOUS_CORE_LITHOLOGIES = {"sandstone", "siltstone", "mudstone"}

# Source terminology that is more specific than the locally cached
# Macrostrat vocabulary.  These are conservative parent-term mappings, not
# free-form synonyms.  The raw phrase remains in evidence metadata.
LITHOLOGY_TERM_ALIASES = {
    "lapilli tuff": "tuff",
    "pumice lapilli tuff": "tuff",
    "pumice-lapilli tuff": "tuff",
    "scoria lapilli tuff": "tuff",
    "scoria-lapilli tuff": "tuff",
    "pumice tuff": "tuff",
    "tuff breccia": "tuffaceous breccia",
    "pyroclastic": "volcaniclastic",
    "pyroclastics": "volcaniclastic",
    "pyroclastic rocks": "volcaniclastic",
    "lava": "volcanic",
}

_MINERAL_DESCRIPTOR_WORDS = {
    "olivine", "orthopyroxene", "clinopyroxene", "pyroxene",
    "hornblende", "quartz", "biotite", "plagioclase", "feldspar",
}

_JAPANESE_LITHOLOGY = {
    "軽石火山礫凝灰岩": "lapilli tuff",
    "火山礫凝灰岩": "lapilli tuff",
    "軽石凝灰岩": "tuff",
    "凝灰角礫岩": "tuff breccia",
    "凝灰質シルト岩": "tuffaceous siltstone",
    "凝灰質砂岩": "tuffaceous sandstone",
    "凝灰質泥岩": "tuffaceous mudstone",
    "軽石細礫岩": "conglomerate",
    "礫岩": "conglomerate",
    "シルト岩": "siltstone",
    "砂岩": "sandstone",
    "泥岩": "mudstone",
    "頁岩": "shale",
    "凝灰岩": "tuff",
    "玄武岩": "basalt",
    "安山岩": "andesite",
    "デイサイト": "dacite",
    "流紋岩": "rhyolite",
    "石灰岩": "limestone",
    "火山灰": "ash",
    "スコリア": "scoria",
    "軽石": "pumice",
    "シルト": "silt",
    "砂": "sand",
    "泥": "mud",
    "礫": "gravel",
}


def _canonical_attribute_prefix(words, attributes):
    """Return a canonical Macrostrat attribute prefix or ``None``."""
    if not words:
        return ""
    phrase = " ".join(words)
    if phrase in attributes:
        return attributes[phrase]
    if all(word in attributes for word in words):
        return " ".join(attributes[word] for word in words)
    return None


def _safe_descriptor_prefix(words):
    """Whether a rejected prefix is a narrow mineral/alteration descriptor."""
    if not words:
        return False
    phrase = " ".join(words)
    if phrase in LITHOLOGY_FALLBACK_MODIFIERS:
        return True
    for word in words:
        pieces = [piece for piece in word.split("-") if piece]
        if word.endswith("-bearing"):
            mineral = word[:-8].rstrip("-")
            if mineral in _MINERAL_DESCRIPTOR_WORDS:
                continue
        if pieces and all(piece in _MINERAL_DESCRIPTOR_WORDS for piece in pieces):
            continue
        if word in LITHOLOGY_FALLBACK_MODIFIERS:
            continue
        return False
    return True


def _strip_core_lithology_modifiers(value):
    """Return ``(core, dropped)`` for proposal-defined adjective fallbacks."""
    words = str(value or "").split()
    dropped = []
    while len(words) > 1:
        prefix = words[0]
        remainder = " ".join(words[1:])
        if prefix == "tuffaceous":
            if remainder not in _TUFFACEOUS_CORE_LITHOLOGIES:
                break
        elif prefix not in LITHOLOGY_CORE_MODIFIERS:
            break
        dropped.append(prefix)
        words = words[1:]
    return " ".join(words), dropped


def resolve_lithology_term(value, *, allow_modifier_fallback=True):
    """Resolve one lithology phrase while preserving valid attributes.

    The result is deliberately structured so callers can keep an audit trail.
    ``term`` is ``None`` when no safe Macrostrat value can be produced.
    """
    raw = " ".join(str(value or "").replace("‐", "-").split()).strip(" ;,、")
    result = {
        "raw_phrase": raw,
        "term": None,
        "match_type": "unresolved",
        "dropped_modifiers": [],
    }
    if not raw:
        return result

    lithologies = _vocab_index("lithology")
    attributes = _vocab_index("lith_att")
    low = raw.casefold()
    low, proposal_dropped = _strip_core_lithology_modifiers(low)
    if low in lithologies:
        result.update(
            term=lithologies[low],
            match_type="modifier_fallback" if proposal_dropped else "exact",
            dropped_modifiers=proposal_dropped,
        )
        return result
    alias = LITHOLOGY_TERM_ALIASES.get(low)
    if alias:
        nested = resolve_lithology_term(alias, allow_modifier_fallback=False)
        if nested.get("term"):
            result.update(
                term=nested["term"],
                match_type="controlled_parent_alias",
                dropped_modifiers=[*proposal_dropped, raw],
            )
            return result

    words = low.split()
    # Longest lithology suffix first.  This keeps ``lapilli tuff`` intact
    # instead of resolving only its final word ``tuff``.
    for index in range(len(words)):
        suffix = " ".join(words[index:])
        if suffix not in lithologies:
            continue
        head = words[:index]
        canonical_head = _canonical_attribute_prefix(head, attributes)
        if canonical_head is not None:
            term = " ".join(part for part in (canonical_head, lithologies[suffix]) if part)
            result.update(
                term=term,
                match_type=(
                    "modifier_fallback" if proposal_dropped
                    else "attribute" if head else "exact"
                ),
                dropped_modifiers=proposal_dropped,
            )
            return result
        if allow_modifier_fallback and _safe_descriptor_prefix(head):
            result.update(
                term=lithologies[suffix],
                match_type="modifier_fallback",
                dropped_modifiers=[*proposal_dropped, " ".join(head)],
            )
            return result

    if allow_modifier_fallback:
        # GSJ English legends commonly use ``pumice-lapilli tuff``.  Pumice
        # describes the clast type; the controlled core term is lapilli tuff.
        special = re.sub(r"^pumice(?:ous)?[- ]+", "", low)
        if special != low and special in lithologies:
            result.update(
                term=lithologies[special],
                match_type="modifier_fallback",
                dropped_modifiers=[low[: len(low) - len(special)].rstrip("- ")],
            )
    return result


def resolve_lithology_value(value, *, allow_modifier_fallback=True):
    """Resolve a semicolon/comma separated lithology value.

    Partial resolution is returned for review, but ``value`` is populated only
    when every non-empty term is safe.  This preserves the fail-closed behavior
    used by the canonical compiler.
    """
    if is_blank(value):
        return {"value": None, "known": [], "unknown": [], "details": []}
    raw_terms = [
        term.strip()
        for term in re.split(r"\s*[;,、]\s*", str(value))
        if term.strip()
    ]
    details = [
        resolve_lithology_term(term, allow_modifier_fallback=allow_modifier_fallback)
        for term in raw_terms
    ]
    known = []
    unknown = []
    for detail in details:
        term = detail.get("term")
        if term:
            if term not in known:
                known.append(term)
        else:
            unknown.append(detail.get("raw_phrase"))
    return {
        "value": "; ".join(known) if known and not unknown else None,
        "known": known,
        "unknown": unknown,
        "details": details,
    }


def _japanese_lithology_terms(text):
    """Extract non-overlapping Japanese lithology names using longest match."""
    hits = []
    occupied = []
    for japanese, english in sorted(_JAPANESE_LITHOLOGY.items(), key=lambda item: -len(item[0])):
        for match in re.finditer(re.escape(japanese), str(text or "")):
            span = match.span()
            if any(not (span[1] <= old[0] or span[0] >= old[1]) for old in occupied):
                continue
            occupied.append(span)
            hits.append((span[0], english, japanese))
    output = []
    for _position, english, japanese in sorted(hits):
        resolved = resolve_lithology_term(english)
        term = resolved.get("term")
        if term and term not in [item["term"] for item in output]:
            output.append({**resolved, "raw_phrase": japanese})
    return output


def _english_lithology_terms(text):
    cleaned = str(text or "")
    cleaned = re.sub(
        r"(?i)^\s*(?:mainly|predominantly|chiefly|partly|partially)?\s*"
        r"(?:composed of|consists? of|comprising|formed by)\s+",
        "",
        cleaned,
    )
    cleaned = re.sub(r"(?i)\b(?:minor amounts? of|rarely|locally)\b", "", cleaned)
    # A compositional adjective/noun followed by a pyroclastic deposit names
    # two useful controlled concepts (for example dacite + welded tuff).
    cleaned = re.sub(
        r"(?i)\b(basalt|andesite|dacite|rhyolite)\s+"
        r"((?:pumice|scoria)[- ]lapilli tuff|welded tuff)\b",
        r"\1; \2",
        cleaned,
    )
    # Compositional ranges in GSJ legends are commonly hyphenated and followed
    # by ``lava``.  Resolve the rock-name endpoints; ``lava`` is a form, not a
    # third composition in this construction.
    cleaned = re.sub(
        r"(?i)\b(basalt|andesite|dacite|rhyolite)\s*[-–—]\s*"
        r"(basalt|andesite|dacite|rhyolite)(?:\s+lava)?\b",
        r"\1; \2",
        cleaned,
    )
    pieces = re.split(
        r"\s*(?:;|,|/|&|\band\b|\bor\b|\bas well as\b|\bto\b)\s*",
        cleaned,
        flags=re.IGNORECASE,
    )
    return [resolve_lithology_term(piece) for piece in pieces if piece.strip()]


def _terms_from_lithology_text(text):
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", str(text or "")):
        return _japanese_lithology_terms(text)
    return _english_lithology_terms(text)


def parse_lithology_relations(value):
    """Parse a legend/body phrase into major, minor and unresolved terms.

    This parser is intentionally conservative.  It recognizes explicit
    dominance/subordination grammar and otherwise treats a plain legend list
    as major lithology.  Confidence is not used as a proxy for abundance.
    """
    text = " ".join(str(value or "").split()).strip()
    result = {
        "raw_phrase": text,
        "major": [],
        "minor": [],
        "unknown": [],
        "major_value": None,
        "minor_value": None,
        "role_cues": {},
        "details": [],
        "role_conflicts": [],
    }
    if not text:
        return result

    major_text = text
    minor_text = ""
    major_cue = "legend_list"
    minor_cue = ""

    if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text):
        dominant = re.search(r"(.+?)(?:を)?(?:主体とし|主体とする|主体として)", text)
        composed = re.search(r"主に(.+?)(?:から構成され|より構成され|からなる)", text)
        plain_composed = re.search(r"(.+?)(?:から構成される|より構成される|からなる)", text)
        if dominant:
            major_text = dominant.group(1)
            minor_text = text[dominant.end():]
            major_cue = dominant.group(0)[-5:]
        elif composed:
            major_text = composed.group(1)
            minor_text = text[composed.end():]
            major_cue = "主に…から構成される"
        elif plain_composed:
            major_text = plain_composed.group(1)
            minor_text = text[plain_composed.end():]
            major_cue = "構成される"
        else:
            subordinate_only = re.search(
                r"(?:まれに|一部で)?(.+?)(を伴う|を挟む|が挟在する|と互層する)",
                text,
            )
            leading_minor = re.match(r"\s*(まれに|一部で)", text)
            if subordinate_only or leading_minor:
                major_text = ""
                minor_text = subordinate_only.group(1) if subordinate_only else text
                minor_cue = (
                    subordinate_only.group(2) if subordinate_only else leading_minor.group(1)
                )
        subordinate = re.search(r"(?:を伴う|を挟む|が挟在する|と互層する|まれに|一部で)", minor_text)
        if minor_text:
            minor_cue = minor_cue or (subordinate.group(0) if subordinate else "dominant_clause_tail")
    else:
        subordinate = re.search(
            r"(?i)\b((?:partially|partialy|partly)\s+with|with|containing|contains|accompanied by|intercalated with|"
            r"including|minor amounts? of|rarely includes?)\b",
            text,
        )
        if subordinate:
            major_text = text[:subordinate.start()]
            minor_text = text[subordinate.end():]
            major_cue = "leading legend clause"
            minor_cue = subordinate.group(1).casefold()

    major_details = _terms_from_lithology_text(major_text)
    minor_details = _terms_from_lithology_text(minor_text) if minor_text else []
    result["details"] = [
        *({**detail, "role": "major"} for detail in major_details),
        *({**detail, "role": "minor"} for detail in minor_details),
    ]
    for role, details in (("major", major_details), ("minor", minor_details)):
        for detail in details:
            term = detail.get("term")
            if term and term not in result[role]:
                result[role].append(term)
            elif not term and detail.get("raw_phrase"):
                result["unknown"].append(detail["raw_phrase"])
    overlaps = set(result["major"]).intersection(result["minor"])
    if overlaps:
        # Explicitly leading/dominant terms win.  This also handles GSJ legend
        # phrases where ``partly with`` introduces a mineralogical variant of
        # the same andesite/dacite rather than a subordinate lithology.
        result["minor"] = [term for term in result["minor"] if term not in overlaps]
        result["role_conflicts"] = sorted(overlaps)
    result["major_value"] = "; ".join(result["major"]) or None
    result["minor_value"] = "; ".join(result["minor"]) or None
    if result["major"]:
        result["role_cues"]["major"] = major_cue
    if result["minor"]:
        result["role_cues"]["minor"] = minor_cue
    return result


def lithology_role_from_context(term, term_jp, context):
    """Classify one GSJ derived term from explicit wording in its snippet."""
    parsed = parse_lithology_relations(context)
    resolved = resolve_lithology_term(term)
    canonical = resolved.get("term") or str(term or "").strip().casefold()
    in_major = canonical in parsed["major"]
    in_minor = canonical in parsed["minor"]
    if in_major != in_minor:
        role = "major" if in_major else "minor"
        cue = parsed["role_cues"].get(role, "")
        # A plain list is meaningful as a GSJ Shape legend, but an individual
        # derived-body hit needs explicit wording before it can establish
        # abundance.  Otherwise every incidental mention would become major.
        if cue != "legend_list":
            return role, cue

    text = str(context or "")
    japanese = str(term_jp or "").strip()
    position = text.find(japanese) if japanese else -1
    dominant = re.search(r"主体とし|主体とする|主体として", text)
    if position >= 0 and dominant:
        if position < dominant.start():
            return "major", dominant.group(0)
        return "minor", "dominant_clause_tail"
    return "unknown", ""


def check_vocab(value, kind, sep=";"):
    """
    セルの値を Macrostrat 公式語彙と照合する。

    ★ エラーにはしない。公式仕様の environment は
      "free text ... or Macrostrat environment" と自由記述を許しており、
      仕様の例に出てくる "shallow marine" 自体が公式表に無い。
      「照合できたか」を知らせるだけ。

    lithology は "<attribute> <lith>" の形が正式（例: "siliceous mudstone"）。
    語を後ろから順に見て、末尾が既知の岩相なら、前の語は属性として照合する。

    戻り値: (照合できた語のリスト, できなかった語のリスト)
    """
    if is_blank(value):
        return [], []
    liths = _vocab_index("lithology")
    atts = _vocab_index("lith_att")
    envs = _vocab_index("environment")

    # ★ lithology は公式仕様上「;」だけでなく「,」でも区切る:
    #     "<attribute> <lith>, <attribute> <lith>; <attribute> <lith>"
    #   両方で割らないと "fine-grained sandstone, coarse-grained sandstone" が
    #   まるごと1語として「表に無い」と報告されてしまう。
    #   strat_name の「,」は階層なので、ここでは扱わない（呼ばれない）。
    seps = (sep, ",") if kind != "environment" else (sep,)
    terms = [str(value)]
    for s in seps:
        terms = [x for t in terms for x in t.split(s)]

    known, unknown = [], []
    for raw in terms:
        term = " ".join(raw.split()).strip()
        if not term:
            continue
        low = term.lower()
        if kind == "environment":
            (known if low in envs else unknown).append(term)
            continue
        # lithology / minor_lith
        if low in liths:
            known.append(term)
            continue
        words = low.split()
        # 末尾から最長一致で岩相を探し、残りが全部属性なら OK とみなす
        matched = False
        for i in range(len(words) - 1, -1, -1):
            if " ".join(words[i:]) in liths:
                head = words[:i]
                if not head or all(
                        w in atts or " ".join(head) in atts for w in head):
                    known.append(term)
                    matched = True
                break
        if not matched:
            unknown.append(term)
    return known, unknown


def normalize_vocab(value, kind, sep=";"):
    """
    公式語彙に **機械的に** 寄せられる語だけ寄せる。解釈はしない。

    やること:
      1. 大文字小文字の違いを公式表の綴りに合わせる（Mudstone → mudstone）
      2. 「X」が表に無くて「X indet.」があるなら「X indet.」にする
         （fluvial → fluvial indet. / deltaic → deltaic indet.）

    やらないこと:
      「deep marine」→「deep-water indet.」のような **言い換え**。
      これは解釈であって機械が決めることではない。表に無ければそのまま残す
      （公式仕様は environment の自由記述を認めている）。

    戻り値: (正規化後の文字列, 変えた語のリスト)
    """
    if is_blank(value):
        return value, []
    table = _vocab_index("environment" if kind == "environment" else "lithology")
    if not table:
        return value, []
    out, changed = [], []
    for raw in str(value).split(sep):
        term = " ".join(raw.split())
        if not term:
            continue
        low = term.lower()
        if low in table:
            fixed = table[low]                      # 公式表の綴りに合わせる
        elif f"{low} indet." in table:
            fixed = table[f"{low} indet."]
        else:
            fixed = term
        if fixed != term:
            changed.append(f"{term} → {fixed}")
        out.append(fixed)
    return f"{sep} ".join(out), changed


def vocab_quality(value, kind):
    """
    語彙としての良さ。語ごとに点を付けて平均する。上書きの可否を決める目安。

        2.0  公式表にあり、かつ具体的
        1.0  公式表にあるが最上位の大分類（下記）
        0.0  公式表に無い（自由記述。仕様上は許容だが、公式語より情報が少ない）

    ★ なぜ「具体的か」まで見るか。
      Macrostrat の environments では `marine` / `non-marine` /
      `marginal marine` / `inferred marine` の4語だけ `type` が空で、
      これは一番外側の大分類。`deep-water indet.`（type: siliciclastic）を
      `marine` で上書きすると、公式語のままなのに情報が落ちる。
      実際に 'deep-water indet.' → 'marine' という格下げが起きた。
    """
    known, unknown = check_vocab(value, kind)
    if not known and not unknown:
        return 1.0                                    # 空欄は判定しない
    detail = load_vocab().get(f"{kind}_detail") or {}
    pts = [0.0] * len(unknown)
    for t in known:
        d = detail.get(t) or detail.get(t.strip()) or {}
        pts.append(1.0 if kind == "environment" and not d.get("type") else 2.0)
    return sum(pts) / len(pts)


def disp_width(text):
    """端末上の表示幅。日本語などの全角文字は2桁として数える。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def pad(text, width, align="left"):
    """全角文字を考慮して桁を揃える。はみ出す場合は切り詰める。"""
    s = str(text)
    if disp_width(s) > width:
        out = ""
        for c in s:
            if disp_width(out) + disp_width(c) > width:
                break
            out += c
        s = out
    space = " " * max(0, width - disp_width(s))
    return space + s if align == "right" else s + space


def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


# 単数形・複数形どちらの綴りでも受け付ける（打ち間違いで動かなくなるのを防ぐ）
SECRET_FILENAMES = ("secrets.json", "secret.json")

# キー名の揺れも吸収する
SECRET_ALIASES = {
    "gemini_api_key": ["gemini_api_key", "GEMINI_API_KEY", "gemini_key",
                       "google_api_key", "api_key"],
    "groq_api_key": ["groq_api_key", "GROQ_API_KEY", "groq_key"],
    "github_token": ["github_token", "github_api_key", "gfthub_api_key", "GITHUB_TOKEN"],
    "hf_token": ["hf_token", "huggingface_api_key", "huggingface_token", "HF_TOKEN"],
    "nvidia_api_key": ["nvidia_api_key", "NVIDIA_API_KEY"],
    "deepseek_api_key": ["deepseek_api_key", "deepsheek_api_key", "DEEPSEEK_API_KEY"],
    "cohere_api_key": ["cohere_api_key", "COHERE_API_KEY"],
    "openrouter_api_key": ["openrouter_api_key", "OPENROUTER_API_KEY"],
    "mistral_api_key": ["mistral_api_key", "MISTRAL_API_KEY"],
    "bedrock_api_key": ["bedrock_api_key", "AWS_BEARER_TOKEN_BEDROCK"],
    "foundry_api_key": ["foundry_api_key", "AZURE_OPENAI_API_KEY"],
    "azure_ai_endpoint": ["azure_ai_endpoint", "AZURE_OPENAI_ENDPOINT"],
    "azure_ai_model": ["azure_ai_model", "AZURE_OPENAI_MODEL"],
}


def secret_paths():
    """探索するファイルパスを列挙する。カレント配下とリポジトリ直下の両方。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (os.path.join("config"), os.path.join(repo, "config")):
        for fn in SECRET_FILENAMES:
            yield os.path.join(base, fn)


def load_secret(name, env_var=None):
    """
    APIキー等を読む。探す順番:
      1. 環境変数（例: GEMINI_API_KEY）
      2. config/secrets.json または config/secret.json

    ファイル名は単数形・複数形どちらでもよく、キー名も多少の揺れを許容する。
    プレースホルダのまま（「ここに…」）の場合は未設定として扱う。
    見つからなければ None を返す（エラーにはしない）。
    """
    if env_var and os.environ.get(env_var):
        return os.environ[env_var].strip()

    keys = SECRET_ALIASES.get(name, [name])
    for p in secret_paths():
        if not os.path.exists(p):
            continue
        data = load_json(p)
        for k in keys:
            v = data.get(k)
            if v and not str(v).startswith(("ここに", "<", "your")):
                return str(v).strip()
    return None


def secret_status(name="gemini_api_key", env_var="GEMINI_API_KEY"):
    """設定状況を人に見せるための要約。キーそのものは返さない。"""
    found = [p for p in secret_paths() if os.path.exists(p)]
    v = load_secret(name, env_var)
    return {
        "見つかったファイル": found,
        "キー設定済み": bool(v),
        "形式": (f"{v[:4]}…{v[-2:]}（{len(v)}文字）" if v else "—"),
    }


def is_blank(v):
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s.lower() in ("nan", "none", "nat")


def valid_strat_name(value, unit_name=None):
    """Reject chronostratigraphic labels and the unit's own Formation name.

    ``strat_name`` is a lithostratigraphic parent/proper name.  Japanese
    ``統`` and English Series/Epoch/Age/Period labels belong in chronology,
    while a value that merely repeats the reviewed unit is not a parent name.
    """
    if is_blank(value):
        return False
    text = unicodedata.normalize("NFKC", str(value)).strip()
    compact = re.sub(r"[^\w]+", "", text, flags=re.UNICODE).casefold()
    if unit_name:
        unit_compact = re.sub(
            r"[^\w]+", "", unicodedata.normalize("NFKC", str(unit_name)),
            flags=re.UNICODE,
        ).casefold()
        if compact and compact == unit_compact:
            return False
    if re.search(r"(?:統|世|紀|期)\s*$", text):
        return False
    if re.search(r"\b(?:series|epoch|age|period|era|eon)\s*$", text, re.IGNORECASE):
        return False
    if re.search(r"(?:堆積物|堆積層)\s*$", text) or re.search(
        r"\bdeposits?\s*$", text, re.IGNORECASE
    ):
        return False
    # A Japanese Formation label is commonly an untranslated repeat of the
    # unit itself.  Group names (層群) remain eligible proper names.
    if text.endswith("層") and not text.endswith("層群"):
        return False
    return True


# ---------------------------------------------------------------------------
# 5. position（層序位置）の導出
# ---------------------------------------------------------------------------

def derive_positions(sort_orders):
    """
    sort_order（1 = 最上位 / 最も新しい）を Macrostrat の position
    （1 = 最下位 / 最も古い、古い→新しいの昇順）へ反転変換する。

    公式仕様:
      "in the case of axis-type==age this should be a sequence from oldest to youngest"

    Ichinohe 完成形の検証:
      sort_order 1..18 (18層) -> position 18..1
      最下位の Kuzumaki Formation が position=1、最上位の河床堆積物が position=18。

    同じ sort_order を複数行に付けると position も同値になり、
    公式仕様の「重なり合うユニット」を表現できる（Ichinohe の unit 1/2 と同じ挙動）。

    引数: sort_order のリスト（None 可）
    戻り値: position のリスト（None は None のまま）
    """
    valid = [s for s in sort_orders if s is not None]
    if not valid:
        return [None] * len(sort_orders)
    top = max(valid)
    return [None if s is None else int(top - s + 1) for s in sort_orders]
