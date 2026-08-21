# -*- coding: utf-8 -*-
"""GSJ 1:50,000 Shapefile の凡例属性を確定的に読む。

GSJ のベクトル配布物では、地質ポリゴン ``geo_A.shp`` の属性表
``geo_A.dbf`` に時代・地層名・岩相（和英）が格納されている。ここでは
外部 GIS ライブラリに依存せず、DBF と Shapefile ヘッダだけを読む。

このモジュールが返す値は LLM 推定ではなく GSJ 配布物の原値である。
ただし DBF のレコード順を層序順とみなす処理は「初期候補」であり、最終的な
上下関係は図幅凡例・説明書と照合する必要がある。
"""

from __future__ import annotations

import os
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class DbfField:
    name: str
    kind: str
    length: int
    decimals: int


def _decode_text(raw: bytes, encodings: Iterable[str]) -> str:
    raw = raw.rstrip(b"\x00 ")
    if not raw:
        return ""
    candidates: list[str] = []
    for encoding in encodings:
        try:
            decoded = raw.decode(encoding).strip()
            if decoded not in candidates:
                candidates.append(decoded)
        except UnicodeDecodeError:
            continue
    if not candidates:
        return raw.decode("cp932", errors="replace").strip()

    # 一部の GSJ DBF は列ごとに UTF-8 と CP932 が混在する。両方で decode
    # できてしまう UTF-8 日本語は、CP932 では典型的な文字化けになるため、
    # 半角カナ・制御文字・文字化けで頻出する漢字を減点して選ぶ。
    mojibake = set("縺繧蜿蝗螳荳逕豁莨陦譁隨髫繝驥謚鬥蛯")

    def score(text: str) -> int:
        value = 0
        for char in text:
            cp = ord(char)
            if char == "�" or (cp < 32 and char not in "\t\r\n"):
                value -= 20
            elif 0xFF61 <= cp <= 0xFF9F:
                value -= 4
            elif char in mojibake:
                value -= 3
            elif char.isprintable():
                value += 1
        return value

    return max(candidates, key=score)


def _parse_value(raw: bytes, field: DbfField, encodings: Iterable[str]) -> Any:
    text = _decode_text(raw, encodings)
    if text == "":
        return ""
    if field.kind in {"N", "F"}:
        try:
            value = float(text)
            return int(value) if field.decimals == 0 and value.is_integer() else value
        except ValueError:
            return text
    if field.kind == "L":
        if text.upper() in {"Y", "T"}:
            return True
        if text.upper() in {"N", "F"}:
            return False
    return text


def read_dbf(path: os.PathLike[str] | str) -> list[dict[str, Any]]:
    """DBF を読み、フィールド名をキーとする辞書のリストを返す。"""
    path = Path(path)
    with path.open("rb") as f:
        header = f.read(32)
        if len(header) != 32:
            raise ValueError(f"DBF header is incomplete: {path}")
        n_records = struct.unpack("<I", header[4:8])[0]
        header_len = struct.unpack("<H", header[8:10])[0]
        record_len = struct.unpack("<H", header[10:12])[0]

        fields: list[DbfField] = []
        while f.tell() < header_len:
            desc = f.read(32)
            if not desc or desc[0] == 0x0D:
                break
            name = desc[:11].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
            fields.append(DbfField(name.upper(), chr(desc[11]), desc[16], desc[17]))
        f.seek(header_len)

        # GSJ の従来 DBF は Shift_JIS/CP932。UTF-8 の配布物にも対応する。
        encodings = ("cp932", "shift_jis", "utf-8")
        rows: list[dict[str, Any]] = []
        for record_index in range(1, n_records + 1):
            record = f.read(record_len)
            if len(record) < record_len:
                break
            if record[:1] == b"*":  # deleted record
                continue
            offset = 1
            row: dict[str, Any] = {"_record_index": record_index}
            for field in fields:
                chunk = record[offset:offset + field.length]
                offset += field.length
                row[field.name] = _parse_value(chunk, field, encodings)
            rows.append(row)
        return rows


def read_shp_bbox(path: os.PathLike[str] | str) -> tuple[float, float, float, float] | None:
    """Shapefile ヘッダから (xmin, ymin, xmax, ymax) を返す。"""
    path = Path(path)
    try:
        with path.open("rb") as f:
            header = f.read(100)
        if len(header) < 100 or struct.unpack(">I", header[:4])[0] != 9994:
            return None
        xmin, ymin, xmax, ymax = struct.unpack("<4d", header[36:68])
        if xmin > xmax or ymin > ymax:
            return None
        return xmin, ymin, xmax, ymax
    except (OSError, struct.error):
        return None


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key, "")
    return "" if value is None else str(value).strip()


def _code(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else str(number)
    except (TypeError, ValueError):
        return str(value).strip()


def _join_nonblank(*values: str) -> str:
    out: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in out:
            out.append(value)
    return " / ".join(out)


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(ch for ch in text if not ch.isspace() and ch not in "・,，、;；")


def find_geo_a_dbf(root: os.PathLike[str] | str) -> Path | None:
    root = Path(root)
    if not root.exists():
        return None
    hits = sorted(p for p in root.rglob("*") if p.is_file() and p.name.casefold() == "geo_a.dbf")
    return hits[0] if hits else None


def load_shape_units(root: os.PathLike[str] | str) -> dict[str, Any]:
    """``references`` 以下を探し、凡例ユニットと地質図の bbox を返す。"""
    dbf_path = find_geo_a_dbf(root)
    if dbf_path is None:
        return {"available": False, "dbf_path": "", "shp_path": "", "bbox": None,
                "centroid": None, "units": [], "excluded_records": 0}

    units: list[dict[str, Any]] = []
    excluded = 0
    for row in read_dbf(dbf_path):
        unit = {
            "record_index": row["_record_index"],
            "major_code": _code(row.get("MAJOR_CODE")),
            "symbol": _text(row, "SYMBOL"),
            "era_ja": _text(row, "LEGEND01"),
            "era_en": _text(row, "LEGEND01E"),
            "age_ja": _text(row, "LEGEND02"),
            "age_en": _text(row, "LEGEND02E"),
            "unit_name_ja": _text(row, "LEGEND03"),
            "unit_name_en": _text(row, "LEGEND03E"),
            "lithology_ja": _text(row, "LEGEND04"),
            "lithology_en": _text(row, "LEGEND04E"),
        }
        # 水域・図郭外などの作業コードは属性が空。地層ユニットにしない。
        if not unit["major_code"] or not any(
            unit[k] for k in ("era_ja", "era_en", "age_ja", "age_en",
                              "unit_name_ja", "unit_name_en")
        ):
            excluded += 1
            continue
        unit["age_text_ja"] = _join_nonblank(unit["era_ja"], unit["age_ja"])
        unit["age_text_en"] = _join_nonblank(unit["era_en"], unit["age_en"])
        unit["display_name_ja"] = (
            f"{unit['unit_name_ja']} ({unit['lithology_ja']})"
            if unit["unit_name_ja"] and unit["lithology_ja"]
            else unit["unit_name_ja"] or unit["lithology_ja"]
        )
        unit["display_name_en"] = (
            f"{unit['unit_name_en']} ({unit['lithology_en']})"
            if unit["unit_name_en"] and unit["lithology_en"]
            else unit["unit_name_en"] or unit["lithology_en"]
        )
        units.append(unit)

    shp_path = dbf_path.with_suffix(".shp")
    bbox = read_shp_bbox(shp_path) if shp_path.exists() else None
    centroid = None if bbox is None else {
        "lng": (bbox[0] + bbox[2]) / 2,
        "lat": (bbox[1] + bbox[3]) / 2,
    }
    return {
        "available": True,
        "dbf_path": str(dbf_path),
        "shp_path": str(shp_path) if shp_path.exists() else "",
        "bbox": bbox,
        "centroid": centroid,
        "units": units,
        "excluded_records": excluded,
    }


def match_status(shape_unit: dict[str, Any] | None, zfk: dict[str, Any]) -> tuple[str, str]:
    """major_code で結合後、和文の年代・地層名・岩相の競合を検出する。"""
    if not shape_unit:
        return "not_available", ""
    comparisons = [
        ("unit_name_ja", shape_unit.get("unit_name_ja"), zfk.get("unit_name_ja")),
        ("lithology_ja", shape_unit.get("lithology_ja"), zfk.get("lithology_ja")),
        ("age_ja", shape_unit.get("age_ja"), zfk.get("age_ja")),
    ]
    conflicts = [name for name, left, right in comparisons
                 if left and right and _normalize(left) != _normalize(right)]
    if conflicts:
        return "conflict", ", ".join(conflicts)
    compared = sum(1 for _, left, right in comparisons if left and right)
    return ("exact" if compared else "major_code_only"), ""
