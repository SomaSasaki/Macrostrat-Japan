# -*- coding: utf-8 -*-
"""Create a Review-v2 input bundle from the local sources available for one map.

This pilot never reads a legacy Excel workbook.  It preserves conservative,
machine-readable candidates and provenance; unresolved column splitting and
Macrostrat vocabulary normalization remain explicit review gaps.
"""
from __future__ import annotations

import argparse, hashlib, json, os, re
from pathlib import Path
from typing import Any, Mapping, Sequence

from compiled_layer import _shape_evidence_rows, build_canonical_layer
from gsj_derived import basal_surface, best_thickness, lithologies, strat_name, thickness_block
from shape_source import load_shape_units

SCHEMA_VERSION = "raw-review-bundle/1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

def _source(path: Path, kind: str, root: Path) -> dict[str, Any]:
    return {"type": kind, "path": os.path.relpath(path, root), "sha256": _sha(path)}

def _pick(obj: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value: Any = obj
        for part in key.split("."):
            value = value.get(part) if isinstance(value, Mapping) else None
        if value not in (None, "", []): return value
    return None

def _evidence(unit_id: str, field: str, candidate: Any, source_file: str,
              locator: str, quote: Any, confidence: str = "A",
              *, explicit: bool = True, method: str = "local ZFK structured JSON",
              **metadata: Any) -> dict[str, Any]:
    row = {"unit_id": unit_id, "column_id": "unsplit", "field": field,
            "candidate": candidate, "source_type": "ZFK", "source_file": source_file,
            "source_locator": locator, "full_context_quote": quote,
            "confidence_class": confidence, "explicit": explicit,
            "extraction_method": method}
    row.update(metadata)
    return row


def _load_age_mapping() -> dict[str, Any]:
    path = PROJECT_ROOT / "config" / "age_mapping.json"
    return _load(path) if path.is_file() else {}


def _page_locator(doc: Mapping[str, Any], quote: str, page_index: Mapping[str, Any] | None) -> str:
    """Return an auditable ZFK section/PDF locator for a source excerpt."""
    label = str((doc.get("target") or {}).get("label") or "").strip()
    bits = [f"§{label}" if label else "ZFK target section"]
    if page_index and quote:
        try:
            from pdf_locate import locate
            hit = locate(page_index, quote)
        except Exception:
            hit = None
        if hit:
            bits.append(f"PDF page {hit['pdf_page']}")
            if hit.get("printed_page") is not None:
                bits.append(f"printed page {hit['printed_page']}")
    return "; ".join(bits)

def _zfk_unit(path: Path, order: int, root: Path,
              page_index: Mapping[str, Any] | None = None,
              age_mapping: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    doc = _load(path); focus = doc.get("legend", {}).get("focus", {})
    age = doc.get("legend", {}).get("parent_age", {})
    facies = doc.get("legend", {}).get("parent_facies", {})
    self_row = doc.get("self", {})
    uid = str(doc.get("id") or path.stem)
    unit_name = _pick(facies, "text_en", "label_en") or _pick(focus, "label_en")
    lith = _pick(focus, "label_en") or _pick(self_row, "label_en")
    age_text = _pick(age, "text_en", "label_en")
    bottom = _pick(age, "lower_age_ma"); top = _pick(age, "upper_age_ma")
    mapped = (age_mapping or {}).get(str(_pick(age, "label_ja") or ""), {})
    t_int = mapped.get("t_int") or _pick(age, "label_en")
    b_int = mapped.get("b_int") or _pick(age, "label_en")
    row = {"unit_id": uid, "column_id": "unsplit", "sort_order": order,
           "unit_name": unit_name, "t_int": t_int,
           "b_int": b_int, "t_age_ma": top, "b_age_ma": bottom,
           "unit_description": None, "lithology": None, "minor_lith": None,
           "environment": None, "comments": "Column assignment is provisional."}
    rel = os.path.relpath(path, root); locator = f"{uid}; legend structured fields"
    ev = []
    for field, value in (("unit_name", unit_name), ("age_text", age_text),
                         ("t_int", t_int), ("b_int", b_int),
                         ("t_age_ma", top), ("b_age_ma", bottom),
                         ("lithology_context", lith)):
        if value not in (None, ""):
            ev.append(_evidence(uid, field, value, rel, locator, value))
    target = doc.get("target", {}); text = target.get("text")
    if text:
        ev.append(_evidence(uid, "description_context", None, rel,
                            str(target.get("label") or target.get("sec_id") or uid), text, "B"))

    # GSJ's derived block is deterministic machine-readable evidence extracted
    # from the report body.  Final-field candidates are kept separate from the
    # full Japanese context, so provenance can never leak into a Macrostrat cell.
    derived_method = "GSJ ZFK derived block plus deterministic section parsing"
    thick_context = thickness_block(doc, page_index)
    min_thickness, max_thickness = best_thickness(doc)
    if thick_context:
        loc = _page_locator(doc, thick_context, page_index)
        ev.append(_evidence(uid, "thickness_context", None, rel, loc,
                            thick_context, "B", method=derived_method))
        if min_thickness is not None:
            ev.append(_evidence(uid, "min_thickness", min_thickness, rel, loc,
                                thick_context, "B", method=derived_method))
        if max_thickness is not None:
            ev.append(_evidence(uid, "max_thickness", max_thickness, rel, loc,
                                thick_context, "B", method=derived_method))

    lith = lithologies(doc, page_index)
    if lith:
        if lith.get("major_terms"):
            major_cues = sorted({
                item.get("role_cue") for item in lith.get("items") or []
                if item.get("role") == "major" and item.get("role_cue")
            })
            ev.append(_evidence(uid, "lithology", lith["major_terms"], rel,
                                _page_locator(doc, lith.get("major") or "", page_index),
                                lith.get("major"), "B", method=derived_method,
                                role="major", role_cue="; ".join(major_cues),
                                normalized_terms=lith["major_terms"].split("; "),
                                parser="gsj_lithology_role_parser/v2"))
        if lith.get("minor_terms"):
            minor_cues = sorted({
                item.get("role_cue") for item in lith.get("items") or []
                if item.get("role") == "minor" and item.get("role_cue")
            })
            ev.append(_evidence(uid, "minor_lith", lith["minor_terms"], rel,
                                _page_locator(doc, lith.get("minor") or "", page_index),
                                lith.get("minor"), "B", method=derived_method,
                                role="minor", role_cue="; ".join(minor_cues),
                                normalized_terms=lith["minor_terms"].split("; "),
                                parser="gsj_lithology_role_parser/v2"))
        if lith.get("unknown_terms"):
            ev.append(_evidence(
                uid, "lithology_context", lith["unknown_terms"], rel,
                _page_locator(doc, lith.get("unknown") or "", page_index),
                lith.get("unknown"), "B", method=derived_method,
                explicit=False, role="unknown",
                normalized_terms=lith["unknown_terms"].split("; "),
                parser="gsj_lithology_role_parser/v2",
            ))

    basal = basal_surface(doc, page_index)
    if basal and basal.get("value"):
        ev.append(_evidence(uid, "basal_surface", basal["value"], rel,
                            _page_locator(doc, basal.get("text") or "", page_index),
                            basal.get("text"), "B", method=derived_method))

    strat = strat_name(doc)
    if strat:
        ev.append(_evidence(uid, "strat_name", strat, rel,
                            "legend.parent_facies hierarchy", strat, "B",
                            method="local ZFK legend hierarchy"))
    return row, ev

def _pdf_evidence(rows: list[dict[str, Any]], abstract: Path | None, root: Path,
                  pages_index: Path | None) -> list[dict[str, Any]]:
    if not abstract or not abstract.is_file(): return []
    text = abstract.read_text(encoding="utf-8", errors="replace")
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pages = _load(pages_index).get("pages", []) if pages_index and pages_index.is_file() else []
    out = []
    for row in rows:
        name = str(row.get("unit_name") or "").strip()
        if len(name) < 4: continue
        for paragraph in paragraphs:
            if name.casefold() not in paragraph.casefold(): continue
            needle = re.sub(r"[^a-z0-9]", "", paragraph[:100].casefold())
            page_no = next((i + 1 for i, page in enumerate(pages)
                            if needle[:35] and needle[:35] in re.sub(r"[^a-z0-9]", "", str(page).casefold())), None)
            locator = "English Abstract" + (f"; PDF page {page_no}" if page_no else "")
            out.append({"unit_id": row["unit_id"], "column_id": "unsplit",
                        "field": "unit_description", "candidate": None,
                        "source_type": "PDF", "source_file": os.path.relpath(abstract, root),
                        "source_locator": locator, "full_context_quote": paragraph,
                        "confidence_class": "C", "explicit": True,
                        "extraction_method": "deterministic English-name paragraph match"})
            break
    return out

def _shape_seed_rows(map_id: str, shape: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Seed a conservative unit inventory when ZFK is unavailable.

    GSJ ``geo_A.dbf`` is structured data, so it is safer than asking an LLM to
    invent an inventory from prose.  Raw age/lithology phrases remain evidence;
    only an exact Macrostrat interval name is promoted into ``t_int/b_int``.
    """
    intervals_path = PROJECT_ROOT / "config" / "intervals.json"
    intervals = set(_load(intervals_path)) if intervals_path.is_file() else set()
    rows: list[dict[str, Any]] = []
    for order, item in enumerate(shape.get("units") or [], start=1):
        record_index = int(item.get("record_index") or order)
        unit_name = (
            str(item.get("unit_name_en") or "").strip()
            or str(item.get("lithology_en") or "").strip()
            or f"GSJ map unit {item.get('major_code') or record_index}"
        )
        raw_age = str(item.get("age_en") or "").strip()
        interval = raw_age if raw_age in intervals else None
        rows.append({
            "unit_id": f"m{map_id}_s{record_index:03d}",
            "column_id": "unsplit",
            "sort_order": order,
            "unit_name": unit_name,
            "t_int": interval,
            "b_int": interval,
            "t_age_ma": None,
            "b_age_ma": None,
            "unit_description": None,
            "lithology": None,
            "minor_lith": None,
            "environment": None,
            "comments": "Shape-derived unit inventory; column and order require review.",
        })
    return rows


def _no_data_seed(map_id: str) -> list[dict[str, Any]]:
    """Keep workbook generation possible when neither ZFK nor Shape has units."""
    return [{
        "unit_id": f"m{map_id}_p001",
        "column_id": "unsplit",
        "sort_order": 1,
        "unit_name": "NO_DATA",
        "t_int": None,
        "b_int": None,
        "comments": "No structured unit inventory; PDF unit extraction requires review.",
    }]


def build_raw_bundle(map_id: str, zfk_root: Path, references: Path,
                     publication_json: Path | None = None,
                     generated_at: str = "1970-01-01T00:00:00+00:00") -> dict[str, Any]:
    zfk_root, references = Path(zfk_root).resolve(), Path(references).resolve()
    references.mkdir(parents=True, exist_ok=True)
    units_dir = zfk_root / "units"
    unit_files = sorted(units_dir.glob(f"m{map_id}_u*.json"))
    page_index_path = next(iter(sorted(references.glob(f"m{map_id}_pdfpages.json"))), None)
    page_index = _load(page_index_path) if page_index_path and page_index_path.is_file() else None
    age_mapping = _load_age_mapping()
    shape = load_shape_units(references)
    rows, evidence = [], []
    for order, path in enumerate(unit_files, 1):
        row, ev = _zfk_unit(path, order, zfk_root, page_index, age_mapping)
        rows.append(row); evidence.extend(ev)
    inventory_source = "ZFK"
    if not rows and shape.get("available"):
        rows = _shape_seed_rows(str(map_id), shape)
        inventory_source = "Shapefile"
    if not rows:
        rows = _no_data_seed(str(map_id))
        inventory_source = "PDF_PENDING"
    shape_ev, shape_meta = _shape_evidence_rows(rows, references)
    evidence.extend(shape_ev)
    abstract = next(iter(sorted(references.glob(f"m{map_id}_abstract.txt"))), None)
    evidence.extend(_pdf_evidence(rows, abstract, references, page_index_path))
    publication = _load(publication_json) if publication_json and publication_json.is_file() else {}
    center = publication.get("page", [{}])[0].get("bbox") if publication.get("page") else None
    lng = (center[0] + center[2]) / 2 if center else (shape.get("centroid") or {}).get("lng")
    lat = (center[1] + center[3]) / 2 if center else (shape.get("centroid") or {}).get("lat")
    ref_id = "kudo2005" if str(map_id) == "1050" else f"gsj{map_id}"
    columns = [{"col_id": "unsplit", "col_name": "Unsplit candidate", "lng": lng,
                "lat": lat, "status": "CHECK", "ref_ids": ref_id,
                "comments": "PDF stratigraphic subdivision required."}]
    metadata = {"title": publication.get("title_e") or publication.get("title_j"),
                "publication_year": publication.get("pub_year"), "shape": shape_meta,
                "column_split_status": "unresolved",
                "unit_inventory_source": inventory_source}
    compiled, evidence_doc = build_canonical_layer(rows, column_rows=columns,
        evidence_rows=evidence, metadata=metadata, map_id=map_id,
        source_review=None, generated_at=generated_at)
    source_files = [_source(p, "ZFK", zfk_root) for p in unit_files]
    for name in ("manifest.json", "map.json", "units-index.json"):
        metadata_file = zfk_root / name
        if metadata_file.is_file():
            source_files.append(_source(metadata_file, "ZFK_METADATA", zfk_root))
    for p, kind in ((abstract, "PDF_ABSTRACT"), (page_index_path, "PDF_INDEX"), (publication_json, "PUBLICATION")):
        if p and Path(p).is_file(): source_files.append(_source(Path(p), kind, references if Path(p).is_relative_to(references) else Path(p).parent))
    if shape.get("dbf_path"):
        for p in (Path(shape["dbf_path"]), Path(shape["shp_path"])):
            if p.is_file(): source_files.append(_source(p, "SHAPE", references))
    title_en = publication.get("title_e") or f"GSJ 1:50,000 map {map_id}"
    authors_en = publication.get("authors_e") or ""
    year = publication.get("pub_year") or ""
    reference_url = publication.get("@id") or publication.get("viewer_url") or ""
    refs = [{"ref_id": ref_id, "title": title_en,
             "authors": authors_en, "publication": publication.get("map_type_e") or "Quadrangle Series, 1:50,000",
             "compilation": "Geological Survey of Japan 1:50,000 quadrangle compilation",
             "organization": publication.get("publisher_e") or "Geological Survey of Japan, AIST",
             "date": year, "doi": None, "url": reference_url,
             "comments": "Official GSJ publication metadata."}]
    images = []
    for page in publication.get("page") or []:
        for legend in page.get("legend") or []:
            resource = (legend.get("resource") or [{}])[0]
            images.append({"col_ids": "unsplit", "image_name": resource.get("@id"),
                           "ref_id": ref_id, "page_no": None,
                           "fig_no": f"legend-{legend.get('id')}",
                           "description": "Geologic map legend", "comments": "Official GSJ image resource."})
        for section in page.get("section") or []:
            resource = (section.get("resource") or [{}])[0]
            images.append({"col_ids": "unsplit", "image_name": resource.get("@id"),
                           "ref_id": ref_id, "page_no": None,
                           "fig_no": str(section.get("label") or section.get("id") or "section"),
                           "description": "Geologic cross section", "comments": "Official GSJ image resource."})
    project_meta = [
        {"key": "project_name", "value": title_en},
        {"key": "organization", "value": "UW Madison - Macrostrat Lab"},
        {"key": "compiler_name", "value": "Soma Sasaki"},
        {"key": "col_type", "value": "column"},
        {"key": "axis_type", "value": "age"},
        {"key": "position_unit", "value": "meters"},
        {"key": "time_unit", "value": "Ma"},
        {"key": "timescale", "value": "international intervals"},
        {"key": "srid", "value": "EPSG:4326"},
    ]
    map_doc_path = zfk_root / "map.json"
    map_doc = _load(map_doc_path) if map_doc_path.is_file() else {}
    zfk_map = map_doc.get("map") or {}
    gsj_meta = [
        {"key": "map_id", "value": str(map_id)},
        {"key": "title_en", "value": title_en},
        {"key": "title_ja", "value": publication.get("title_j")},
        {"key": "sheet_code", "value": zfk_map.get("sheet_code")},
        {"key": "pub_year", "value": year},
        {"key": "series", "value": publication.get("map_type_e")},
        {"key": "publisher", "value": publication.get("publisher_e")},
        {"key": "source_zfk_map", "value": map_doc.get("@id")},
        {"key": "source_publication", "value": publication.get("@id")},
    ]
    gaps = ["column split requires PDF stratigraphic interpretation",
            "environment inference remains a review task when the Abstract is silent",
            "t_prop, b_prop, position, section_id and t_pos are deferred to submission"]
    if inventory_source == "Shapefile":
        gaps.append("ZFK unavailable; unit inventory and provisional order come from geo_A.dbf")
    elif inventory_source == "PDF_PENDING":
        gaps.append("ZFK and Shape unit inventories unavailable; PDF unit inventory requires review")
    document = {"schema_version": SCHEMA_VERSION, "map_id": str(map_id),
            "sources": source_files,
            "review_v2_input": {"unit_rows": rows, "column_rows": columns,
                                "evidence_rows": evidence, "project": metadata},
            "compiled": compiled, "evidence": evidence_doc,
            "gaps": gaps,
            # Builder-compatible top-level aliases.  The durable canonical
            # documents remain compiled.json and evidence.json.
            "units": rows, "columns": columns, "refs": refs, "images": images,
            "project_meta": project_meta, "gsj_meta": gsj_meta,
            "source_evidence": evidence}
    return document

def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__); p.add_argument("map_id")
    p.add_argument("--zfk-root", type=Path, required=True); p.add_argument("--references", type=Path, required=True)
    p.add_argument("--publication", type=Path); p.add_argument("--output", type=Path, required=True)
    a = p.parse_args(argv); bundle = build_raw_bundle(a.map_id, a.zfk_root, a.references, a.publication)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.output.with_suffix(a.output.suffix + ".tmp"); tmp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2)+"\n", encoding="utf-8"); os.replace(tmp, a.output)
    print(json.dumps({"output": str(a.output), "units": len(bundle["review_v2_input"]["unit_rows"]), "evidence": len(bundle["evidence"]["evidence"])}, ensure_ascii=False)); return 0

if __name__ == "__main__": raise SystemExit(main())
