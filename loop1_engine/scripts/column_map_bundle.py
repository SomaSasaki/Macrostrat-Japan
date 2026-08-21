# -*- coding: utf-8 -*-
"""Generate column review PNG/KML/JSON directly from a canonical bundle.

This is a deliberately thin adapter around :mod:`column_map`.  It replaces
only the legacy-XLSX input step; Shapefile parsing, shared-unit partitioning,
representative-point selection, rendering, CRS safeguards, and KML generation
all use the existing implementation unchanged.

Accepted bundle inputs
----------------------

``--bundle`` may point either to a directory containing ``compiled.json`` (and
optionally ``evidence.json``), or directly to ``compiled.json``.  Columns are
read from ``compiled.map.columns`` and assignments from
``compiled.units[].column_ids``.  When sibling evidence is available, a GSJ
``MAJOR_CODE`` is recovered from Shapefile evidence locators, making the join
independent of English-name spelling.

Example::

    python scripts/column_map_bundle.py \
      --bundle outputs/review_v2/m1050_data \
      --shape references/.../shp/geo_A.shp \
      --output-dir outputs/maps/m1050 \
      --stem column_map

The command writes ``column_map.png``, ``column_map.kml``, and
``column_map.json``.  Neither canonical source JSON nor Shape source files are
modified.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from column_map import (
    ColumnDefinition,
    ColumnMapResult,
    UnitAssignment,
    _as_float,
    _assign_components,
    _attach_attributes,
    _build_components,
    _clean_id,
    _column_regions,
    _match_assignments,
    _read_crs,
    _split_ids,
    read_polygon_shapefile,
    write_kml,
    write_map_png,
)


@dataclass(frozen=True)
class CanonicalBundleInput:
    compiled_path: Path
    evidence_path: Path | None
    columns: tuple[ColumnDefinition, ...]
    assignments: tuple[UnitAssignment, ...]
    map_id: str
    title: str


class CanonicalBundleError(ValueError):
    """Raised when compiled/evidence JSON cannot supply map assignments."""


def _validated_output_stem(stem: str) -> str:
    """Return a safe output stem that will not escape the chosen output directory."""
    if not isinstance(stem, str):
        raise CanonicalBundleError("Output stem must be a string.")
    candidate = stem.strip()
    if not candidate or candidate in {".", ".."}:
        raise CanonicalBundleError("Output stem must be a non-empty basename.")
    if os.path.isabs(candidate):
        raise CanonicalBundleError(f"Output stem must not be absolute: {candidate}")
    if "/" in candidate or "\\" in candidate:
        raise CanonicalBundleError(f"Output stem must not contain path separators: {candidate}")
    if re.fullmatch(r"[^/\\]+", candidate) is None:
        raise CanonicalBundleError(f"Output stem contains unsupported characters: {candidate}")
    return candidate


def _load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CanonicalBundleError(f"Canonical JSON not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CanonicalBundleError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise CanonicalBundleError(f"Canonical JSON root must be an object: {path}")
    return document


def _bundle_paths(bundle: str | os.PathLike[str]) -> tuple[Path, Path | None]:
    selected = Path(bundle).expanduser().resolve()
    if selected.is_dir():
        compiled = selected / "compiled.json"
        evidence = selected / "evidence.json"
    else:
        compiled = selected
        evidence = selected.with_name("evidence.json")
    if compiled.name.casefold() != "compiled.json":
        raise CanonicalBundleError(
            "--bundle must be a bundle directory or a file named compiled.json: "
            f"{selected}"
        )
    if not compiled.is_file():
        raise CanonicalBundleError(f"compiled.json not found: {compiled}")
    return compiled, evidence if evidence.is_file() else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_present(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _column_ids(unit: Mapping[str, Any]) -> tuple[str, ...]:
    direct = unit.get("column_ids")
    if isinstance(direct, list):
        output: list[str] = []
        for value in direct:
            col_id = _clean_id(value)
            if col_id and col_id not in output:
                output.append(col_id)
        if output:
            return tuple(output)
    for values in (_mapping(unit.get("values")), _mapping(unit.get("review_values"))):
        ids = _split_ids(values.get("column_id") or values.get("col_id"))
        if ids:
            return ids
    return ()


def _unit_value(unit: Mapping[str, Any], key: str) -> Any:
    for source in (unit, _mapping(unit.get("values")), _mapping(unit.get("review_values"))):
        value = source.get(key)
        if value not in (None, ""):
            return value
    return None


def _major_codes(evidence_document: Mapping[str, Any]) -> dict[str, str]:
    """Recover unit-to-MAJOR_CODE joins from canonical Shape evidence."""

    result: dict[str, str] = {}
    for record in _list(evidence_document.get("evidence")):
        if not isinstance(record, Mapping):
            continue
        source = _mapping(record.get("source"))
        if str(source.get("type") or "").casefold() != "shapefile":
            continue
        unit_id = _clean_id(record.get("unit_id"))
        locator = str(source.get("locator") or "")
        match = re.search(r"\bMAJOR_CODE\s*=\s*([^;,\s]+)", locator, re.I)
        if not unit_id or not match:
            continue
        code = _clean_id(match.group(1))
        existing = result.get(unit_id)
        if existing and existing != code:
            raise CanonicalBundleError(
                f"Conflicting MAJOR_CODE evidence for {unit_id}: {existing} vs {code}"
            )
        result[unit_id] = code
    return result


def _merge_assignments(assignments: Sequence[UnitAssignment]) -> list[UnitAssignment]:
    """Merge canonical rows that represent one source geological unit."""

    merged: dict[tuple[str, str], UnitAssignment] = {}
    order: list[tuple[str, str]] = []
    for assignment in assignments:
        key = (
            "major_code" if assignment.major_code else "unit_id",
            assignment.major_code or assignment.unit_id or assignment.unit_name,
        )
        if not key[1]:
            key = ("row", str(assignment.row_number))
        existing = merged.get(key)
        if existing is None:
            merged[key] = assignment
            order.append(key)
            continue
        column_ids = list(existing.columns)
        for col_id in assignment.columns:
            if col_id not in column_ids:
                column_ids.append(col_id)
        merged[key] = UnitAssignment(
            unit_id=existing.unit_id or assignment.unit_id,
            columns=tuple(column_ids),
            unit_name=existing.unit_name or assignment.unit_name,
            reference_name=existing.reference_name or assignment.reference_name,
            major_code=existing.major_code or assignment.major_code,
            row_number=min(existing.row_number, assignment.row_number),
            place_names=existing.place_names or assignment.place_names,
        )
    return [merged[key] for key in order]


def load_canonical_bundle(bundle: str | os.PathLike[str]) -> CanonicalBundleInput:
    """Load just the columns and unit assignments needed by the map engine."""

    compiled_path, evidence_path = _bundle_paths(bundle)
    compiled = _load_json(compiled_path)
    schema_version = str(compiled.get("schema_version") or "")
    if not schema_version:
        raise CanonicalBundleError(f"compiled.json has no schema_version: {compiled_path}")
    if schema_version.split(".", 1)[0] != "1":
        raise CanonicalBundleError(
            f"Unsupported compiled schema_version {schema_version!r}; expected 1.x"
        )

    map_document = _mapping(compiled.get("map"))
    column_rows = _list(map_document.get("columns"))
    columns: list[ColumnDefinition] = []
    seen_columns: set[str] = set()
    for index, row in enumerate(column_rows, start=1):
        if not isinstance(row, Mapping):
            raise CanonicalBundleError(f"compiled.map.columns[{index - 1}] is not an object")
        col_id = _clean_id(_first_present(row, "col_id", "column_id"))
        if not col_id:
            raise CanonicalBundleError(f"Column row {index} has no col_id")
        if col_id in seen_columns:
            raise CanonicalBundleError(f"Duplicate col_id in compiled bundle: {col_id}")
        seen_columns.add(col_id)
        columns.append(
            ColumnDefinition(
                col_id=col_id,
                name=str(_first_present(row, "col_name", "column_name") or col_id).strip(),
                lat=_as_float(_first_present(row, "lat", "latitude")),
                lng=_as_float(_first_present(row, "lng", "lon", "longitude")),
            )
        )
    if not columns:
        raise CanonicalBundleError(f"compiled.map.columns is empty: {compiled_path}")

    evidence_document = _load_json(evidence_path) if evidence_path is not None else {}
    codes = _major_codes(evidence_document)
    assignments: list[UnitAssignment] = []
    for index, unit in enumerate(_list(compiled.get("units")), start=1):
        if not isinstance(unit, Mapping):
            raise CanonicalBundleError(f"compiled.units[{index - 1}] is not an object")
        unit_id = _clean_id(_unit_value(unit, "unit_id"))
        col_ids = _column_ids(unit)
        if not col_ids:
            continue
        assignments.append(
            UnitAssignment(
                unit_id=unit_id,
                columns=col_ids,
                unit_name=str(_unit_value(unit, "unit_name") or "").strip(),
                reference_name=str(_unit_value(unit, "source_unit_name_en") or "").strip(),
                major_code=_clean_id(_unit_value(unit, "major_code")) or codes.get(unit_id, ""),
                row_number=index,
                place_names="",
            )
        )
    assignments = _merge_assignments(assignments)
    if not assignments:
        raise CanonicalBundleError(f"compiled.units has no column assignments: {compiled_path}")

    metadata = _mapping(map_document.get("metadata"))
    map_id = _clean_id(map_document.get("map_id"))
    project_name = str(metadata.get("project_name") or "").strip()
    title = f"Column review: {project_name or ('map ' + map_id if map_id else compiled_path.parent.name)}"
    return CanonicalBundleInput(
        compiled_path=compiled_path,
        evidence_path=evidence_path,
        columns=tuple(columns),
        assignments=tuple(assignments),
        map_id=map_id,
        title=title,
    )


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _cleanup_outputs(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()
        except FileNotFoundError:
            continue


def _validate_bundle_shape_pair(bundle: CanonicalBundleInput, shape_path: Path) -> None:
    bundle_dir = bundle.compiled_path.parent
    if not bundle_dir.exists():
        return
    try:
        shape_relative = shape_path.resolve().relative_to(bundle_dir.resolve())
    except ValueError:
        return
    if shape_relative.parts and shape_relative.parts[0] == "..":
        return


def generate_column_map_from_bundle(
    bundle: str | os.PathLike[str],
    shp_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    stem: str = "column_map",
    width: int = 1200,
    height: int = 900,
    title: str | None = None,
) -> ColumnMapResult:
    """Generate PNG/KML/JSON from compiled/evidence JSON plus ``geo_A.shp``."""

    canonical = load_canonical_bundle(bundle)
    shape = Path(shp_path).expanduser().resolve()
    if shape.suffix.casefold() != ".shp":
        raise CanonicalBundleError(f"Expected geo_A.shp input: {shape}")
    dbf_path = shape.with_suffix(".dbf")
    if not dbf_path.is_file():
        raise CanonicalBundleError(f"Matching DBF not found: {dbf_path}")
    destination = Path(output_dir).expanduser().resolve()
    safe_stem = _validated_output_stem(stem)
    png_path = destination / f"{safe_stem}.png"
    kml_path = destination / f"{safe_stem}.kml"
    json_path = destination / f"{safe_stem}.json"
    _validate_bundle_shape_pair(canonical, shape)

    destination.mkdir(parents=True, exist_ok=True)
    temp_png_path = destination / f"{safe_stem}.png.tmp"
    temp_kml_path = destination / f"{safe_stem}.kml.tmp"
    temp_json_path = destination / f"{safe_stem}.json.tmp"

    records, bbox = read_polygon_shapefile(shape)
    source_crs, warnings = _read_crs(shape, bbox)
    _attach_attributes(records, dbf_path)
    record_columns, matched, unmatched, match_warnings = _match_assignments(
        records, list(canonical.assignments)
    )
    warnings.extend(match_warnings)
    components = _build_components(records, record_columns)
    _assign_components(components, list(canonical.columns), bbox)
    regions = _column_regions(list(canonical.columns), components, bbox)

    defined = {column.col_id for column in canonical.columns}
    unknown = sorted({
        col_id
        for assignment in canonical.assignments
        for col_id in assignment.columns
        if col_id not in defined
    })
    if unknown:
        warnings.append("Canonical units refer to undefined column IDs: " + ", ".join(unknown))
    for region in regions:
        if not region.candidate_inside_region:
            warnings.append(
                f"{region.definition.col_id}: representative point is a visible fallback and needs review."
            )

    # 地下のみの地層（Shapefileにポリゴンがない地層）の特定
    underground_units = sorted({a.unit_name for a in unmatched if a.unit_name})

    # Map rendering is deterministic and offline.  Column coordinates already
    # act as the representative-point candidates.  Text-derived place names
    # may be rendered only after a separate geocoding stage has cached and
    # bounds-validated them; the map generator must never call a web API.
    place_name_pins: list[dict[str, Any]] = []
    centroid_pins: list[dict[str, Any]] = []

    destination.mkdir(parents=True, exist_ok=True)
    map_title = title or canonical.title
    try:
        write_map_png(
            temp_png_path,
            components,
            regions,
            bbox,
            width=width,
            height=height,
            title=map_title,
            place_name_pins=place_name_pins,
            centroid_pins=centroid_pins,
            underground_units=underground_units,
        )
        write_kml(
            temp_kml_path,
            regions,
            source_crs,
            warnings,
            document_name=map_title,
            place_name_pins=place_name_pins,
            centroid_pins=centroid_pins,
            underground_units=underground_units,
        )
        result = ColumnMapResult(
            png_path=png_path,
            kml_path=kml_path,
            columns=regions,
            warnings=warnings,
            source_crs=source_crs,
            matched_units=matched,
            unmatched_units=len(unmatched),
            unassigned_records=sum(1 for record in records if record.record_index not in record_columns),
        )
        payload = result.as_dict()
        payload["json_path"] = str(json_path)
        payload["input"] = {
            "mode": "canonical_bundle",
            "compiled": str(canonical.compiled_path),
            "evidence": str(canonical.evidence_path) if canonical.evidence_path else None,
            "shape": str(shape),
            "map_id": canonical.map_id or None,
        }
        _write_json_atomic(temp_json_path, payload)
        os.replace(temp_png_path, png_path)
        os.replace(temp_kml_path, kml_path)
        os.replace(temp_json_path, json_path)
        return result
    except Exception:
        _cleanup_outputs([temp_png_path, temp_kml_path, temp_json_path, png_path, kml_path, json_path])
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help="Directory containing compiled.json, or compiled.json itself",
    )
    parser.add_argument("--shape", required=True, type=Path, help="GSJ geo_A.shp")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stem", default="column_map", help="Output basename (default: column_map)")
    parser.add_argument("--title", default=None)
    parser.add_argument("--width", type=int, default=1200)
    parser.add_argument("--height", type=int, default=900)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = generate_column_map_from_bundle(
            args.bundle,
            args.shape,
            args.output_dir,
            stem=args.stem,
            width=args.width,
            height=args.height,
            title=args.title,
        )
    except (CanonicalBundleError, FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    payload = result.as_dict()
    payload["json_path"] = str(Path(args.output_dir).expanduser().resolve() / f"{args.stem}.json")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
