# -*- coding: utf-8 -*-
"""Build and audit an input-isolated cold-start workspace.

The staging boundary admits only primary source material: publication metadata,
the source PDF, official GSJ map assets (GeoTIFF/world file/KMZ/legend), an
optional Shapefile dataset, and optional ZFK records.  Review workbooks, GOLD
fixtures, derived JSON, PDF text indexes, caches, and reviewed Column
configuration are deliberately excluded.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pilot import PilotSources, run_pilot


ROOT = Path(__file__).resolve().parents[2]
COLD_START_SCHEMA = "macrostrat-cold-start/1.0"
SHAPE_SUFFIXES = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"}
MAP_ASSET_SUFFIXES = {".tif", ".tiff", ".tfw", ".kmz", ".jpg", ".jpeg", ".png", ".txt"}
FORBIDDEN_NAMES = {
    "compiled.json",
    "derived_previews.json",
    "environment_figure_candidates.json",
    "evidence.json",
    "pilot_llm_stage.json",
    "pilot_manifest.json",
    "raw_bundle.json",
    "review_input.json",
    "routed_contexts.mapped.json",
}
FORBIDDEN_SUFFIXES = {".xlsx", ".xls", ".xlsm"}


class ColdStartError(RuntimeError):
    """The requested run is not a clean or source-only cold start."""


@dataclass(frozen=True)
class ColdStartInputs:
    map_id: str
    source_workspace: Path
    publication: Path
    pdf: Path
    map_assets: tuple[Path, ...]
    shape: Path | None
    zfk_root: Path | None


@dataclass(frozen=True)
class PreparedColdStart:
    run_root: Path
    sources: PilotSources
    manifest_path: Path


def _map_id(value: str | int) -> str:
    result = str(value).strip().lstrip("mM")
    if not result.isdigit():
        raise ColdStartError(f"map_id must be numeric: {value!r}")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _find_pdf(references: Path) -> Path:
    candidates = sorted(
        (path for path in references.rglob("*") if path.is_file() and path.suffix.casefold() == ".pdf"),
        key=lambda path: (not path.name.casefold().endswith("_d.pdf"), str(path).casefold()),
    )
    if not candidates:
        raise ColdStartError(f"No source PDF found under {references}")
    return candidates[0].resolve()


def _find_shape(references: Path) -> Path | None:
    candidates = sorted(
        path for path in references.rglob("*")
        if path.is_file() and path.name.casefold() == "geo_a.shp"
        and path.with_suffix(".dbf").is_file()
    )
    return candidates[0].resolve() if candidates else None


def _find_map_assets(references: Path) -> tuple[Path, ...]:
    """Discover files from an official GSJ raster bundle, not loose images.

    Requiring a paired F1 GeoTIFF/world file keeps the admission rule narrow and
    excludes extracted figures, review screenshots, and other derived images.
    All whitelisted sibling files in that official bundle are primary map
    products (including KMZ and L1 stratigraphic legend assets).
    """

    bundle_dirs = {
        path.parent.resolve()
        for path in references.rglob("*_F1_geotiff.tif")
        if path.is_file()
        and path.with_name(path.name.replace("_geotiff.tif", ".tfw")).is_file()
    }
    return tuple(sorted(
        (
            path.resolve()
            for directory in bundle_dirs
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.casefold() in MAP_ASSET_SUFFIXES
            and path.name.casefold() not in FORBIDDEN_NAMES
            and path.suffix.casefold() not in FORBIDDEN_SUFFIXES
        ),
        key=lambda path: str(path).casefold(),
    ))


def discover_inputs(
    map_id: str | int, source_workspace: Path, *, project_root: Path = ROOT,
) -> ColdStartInputs:
    mid = _map_id(map_id)
    source_workspace = Path(source_workspace).resolve()
    references = source_workspace / "references"
    publication = (
        Path(project_root).resolve() / "data" / "raw" / "publication" / "g050"
        / f"m{mid}.json"
    )
    if not publication.is_file():
        raise ColdStartError(f"Publication metadata is missing: {publication}")
    zfk = Path(project_root).resolve() / "data" / "raw" / "zfk" / f"m{mid}"
    return ColdStartInputs(
        map_id=mid,
        source_workspace=source_workspace,
        publication=publication.resolve(),
        pdf=_find_pdf(references),
        map_assets=_find_map_assets(references),
        shape=_find_shape(references),
        zfk_root=zfk.resolve() if zfk.is_dir() else None,
    )


def _copy_file(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ColdStartError(f"Cold-start target already exists: {destination}")
    shutil.copy2(source, destination)
    return {
        "path": str(destination),
        "source_name": source.name,
        "sha256": _sha256(destination),
        "bytes": destination.stat().st_size,
    }


def _copy_tree_files(source: Path, destination: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not source.is_dir():
        return records
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        records.append(_copy_file(path, destination / path.relative_to(source)))
    return records


def _shape_files(shape: Path | None) -> Iterable[Path]:
    if shape is None:
        return ()
    return tuple(sorted(
        path for path in shape.parent.iterdir()
        if path.is_file() and path.stem.casefold() == shape.stem.casefold()
        and path.suffix.casefold() in SHAPE_SUFFIXES
    ))


def prepare_cold_start(inputs: ColdStartInputs, run_root: Path) -> PreparedColdStart:
    run_root = Path(run_root).resolve()
    if run_root.exists() and any(run_root.iterdir()):
        raise ColdStartError(f"Cold-start root must not exist or must be empty: {run_root}")
    run_root.mkdir(parents=True, exist_ok=True)
    workspace = run_root / f"m{inputs.map_id}_cold_start"
    references = workspace / "references"
    system = workspace / "system"
    cache = workspace / "llm_cache"
    zfk = run_root / "primary_sources" / "zfk" / f"m{inputs.map_id}"
    publication = run_root / "primary_sources" / "publication" / inputs.publication.name
    references.mkdir(parents=True)
    system.mkdir(parents=True)
    cache.mkdir(parents=True)

    staged: dict[str, Any] = {
        "publication": _copy_file(inputs.publication, publication),
        "pdf": _copy_file(inputs.pdf, references / inputs.pdf.name),
        "map_assets": [],
        "shape": [],
        "zfk": [],
    }
    staged_pdf = Path(staged["pdf"]["path"])
    source_references = inputs.source_workspace / "references"
    for path in inputs.map_assets:
        relative = path.relative_to(source_references)
        record = _copy_file(path, references / relative)
        record["source_relative_path"] = str(relative)
        staged["map_assets"].append(record)
    staged_shape: Path | None = None
    if inputs.shape is not None:
        shape_dir = references / "shape"
        for path in _shape_files(inputs.shape):
            record = _copy_file(path, shape_dir / path.name)
            staged["shape"].append(record)
            if path.suffix.casefold() == ".shp":
                staged_shape = Path(record["path"])
    if inputs.zfk_root is not None:
        staged["zfk"] = _copy_tree_files(inputs.zfk_root, zfk)

    manifest_path = system / "cold_start_manifest.json"
    source_manifest = system / "source_manifest.json"
    sources = PilotSources(
        map_id=inputs.map_id,
        workspace=workspace,
        zfk_root=zfk,
        references=references,
        publication=publication,
        pdf=staged_pdf,
        abstract=references / f"m{inputs.map_id}_abstract.txt",
        pdf_index=references / f"m{inputs.map_id}_pdfpages.json",
        shape=staged_shape,
        column_config=None,
        llm_cache=cache,
        source_manifest=source_manifest,
    )
    manifest = {
        "schema_version": COLD_START_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "map_id": inputs.map_id,
        "status": "prepared",
        "run_root": str(run_root),
        "workspace": str(workspace),
        "input_policy": {
            "primary_sources_only": True,
            "gold_allowed": False,
            "existing_review_allowed": False,
            "existing_cache_allowed": False,
            "reviewed_column_config_allowed": False,
        },
        "staged_inputs": staged,
    }
    _atomic_json(manifest_path, manifest)
    _atomic_json(source_manifest, {
        "schema_version": "cold-start-source-manifest/1.0",
        "map_id": inputs.map_id,
        "primary_sources_only": True,
        "staged_inputs": staged,
    })
    prepared = PreparedColdStart(run_root, sources, manifest_path)
    audit_pre_run(prepared)
    return prepared


def audit_pre_run(prepared: PreparedColdStart) -> dict[str, Any]:
    sources = prepared.sources
    if not _inside(sources.workspace, prepared.run_root):
        raise ColdStartError("Cold-start workspace escapes its run root")
    for path in (
        sources.references, sources.llm_cache, sources.zfk_root,
        sources.publication, sources.pdf, sources.source_manifest,
    ):
        if not _inside(path, prepared.run_root):
            raise ColdStartError(f"Cold-start input escapes its run root: {path}")
    if sources.column_config is not None:
        raise ColdStartError("Reviewed Column configuration is forbidden in a cold start")
    if any(path.is_file() for path in sources.llm_cache.rglob("*")):
        raise ColdStartError("Cold-start LLM cache is not empty")
    violations: list[str] = []
    for path in sources.workspace.rglob("*"):
        if not path.is_file() or path in {prepared.manifest_path, sources.source_manifest, sources.pdf}:
            continue
        name = path.name.casefold()
        if name in FORBIDDEN_NAMES or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            violations.append(str(path))
        if name.endswith("_abstract.txt") or name.endswith("_pdfpages.json"):
            violations.append(str(path))
        if "gold" in {part.casefold() for part in path.parts}:
            violations.append(str(path))
    if violations:
        raise ColdStartError("Forbidden pre-existing derivatives: " + "; ".join(sorted(set(violations))))
    return {
        "status": "clean",
        "workspace": str(sources.workspace),
        "cache_files": 0,
        "column_config": None,
        "violations": [],
    }


def execute_cold_start(prepared: PreparedColdStart, *, model: str, use_llm: bool = True) -> dict[str, Any]:
    audit = audit_pre_run(prepared)
    result = run_pilot(
        prepared.sources.map_id,
        output_dir=prepared.sources.workspace,
        model=model,
        force=False,
        use_llm=use_llm,
        sources=prepared.sources,
        allow_legacy_cache_migration=False,
    )
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    manifest.update({"status": "complete", "pre_run_audit": audit, "result": result})
    _atomic_json(prepared.manifest_path, manifest)
    return result


def load_cold_start_run(run_root: Path) -> PreparedColdStart:
    """Reopen a previously prepared run without importing outside artifacts.

    Resume is intentionally narrower than arbitrary ``PilotSources`` loading:
    every staged primary input is recovered from the original cold-start
    manifest, constrained to the run root, and re-hashed.  In-run LLM caches
    and generated artifacts may exist because they are the point of resuming.
    """

    run_root = Path(run_root).resolve()
    manifests = sorted(run_root.glob("m*_cold_start/system/cold_start_manifest.json"))
    if len(manifests) != 1:
        raise ColdStartError("Resume requires exactly one cold-start manifest")
    manifest_path = manifests[0].resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ColdStartError(f"Cannot read cold-start manifest: {manifest_path}") from exc
    if manifest.get("schema_version") != COLD_START_SCHEMA:
        raise ColdStartError("Unsupported cold-start manifest")
    policy = manifest.get("input_policy") or {}
    if policy.get("primary_sources_only") is not True or policy.get("gold_allowed") is not False:
        raise ColdStartError("Cold-start manifest does not preserve the source-only boundary")
    map_id = _map_id(manifest.get("map_id") or "")
    workspace = manifest_path.parents[1].resolve()
    if not _inside(workspace, run_root):
        raise ColdStartError("Cold-start workspace escapes its run root")
    staged = manifest.get("staged_inputs") or {}

    def verified_record(record: Any, label: str) -> Path:
        if not isinstance(record, dict):
            raise ColdStartError(f"Missing staged input record: {label}")
        path = Path(str(record.get("path") or "")).resolve()
        if not _inside(path, run_root) or not path.is_file():
            raise ColdStartError(f"Staged input is missing or escapes the run root: {label}")
        if _sha256(path) != str(record.get("sha256") or ""):
            raise ColdStartError(f"Staged primary input changed: {label}")
        return path

    publication = verified_record(staged.get("publication"), "publication")
    pdf = verified_record(staged.get("pdf"), "pdf")
    shape: Path | None = None
    for position, record in enumerate(staged.get("map_assets") or []):
        verified_record(record, f"map_assets[{position}]")
    for position, record in enumerate(staged.get("shape") or []):
        path = verified_record(record, f"shape[{position}]")
        if path.suffix.casefold() == ".shp":
            shape = path
    zfk_records = staged.get("zfk") or []
    for position, record in enumerate(zfk_records):
        verified_record(record, f"zfk[{position}]")
    zfk_root = run_root / "primary_sources" / "zfk" / f"m{map_id}"
    references = workspace / "references"
    system = workspace / "system"
    sources = PilotSources(
        map_id=map_id,
        workspace=workspace,
        zfk_root=zfk_root,
        references=references,
        publication=publication,
        pdf=pdf,
        abstract=references / f"m{map_id}_abstract.txt",
        pdf_index=references / f"m{map_id}_pdfpages.json",
        shape=shape,
        column_config=None,
        llm_cache=workspace / "llm_cache",
        source_manifest=system / "source_manifest.json",
    )
    for derived in (sources.abstract, sources.pdf_index, sources.source_manifest):
        if not derived.is_file() or not _inside(derived, run_root):
            raise ColdStartError(f"Cold-start resume prerequisite is missing: {derived}")
    return PreparedColdStart(run_root, sources, manifest_path)


def refresh_primary_map_assets(run_root: Path, source_workspace: Path) -> PreparedColdStart:
    """Admit newly supported official map assets into an existing clean run.

    This is an explicit boundary expansion for runs prepared by older code.  It
    accepts only a source workspace whose primary PDF hash matches the already
    staged PDF, then records and hashes every added official GSJ asset in both
    manifests.  Generated artifacts and caches are never imported.
    """

    prepared = load_cold_start_run(run_root)
    source_workspace = Path(source_workspace).resolve()
    source_references = source_workspace / "references"
    source_pdf = _find_pdf(source_references)
    if _sha256(source_pdf) != _sha256(prepared.sources.pdf):
        raise ColdStartError("Source workspace PDF does not match the staged primary PDF")
    assets = _find_map_assets(source_references)
    if not assets:
        raise ColdStartError("No official GSJ F1 GeoTIFF/world-file bundle was found")

    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    staged = manifest.get("staged_inputs") or {}
    records = list(staged.get("map_assets") or [])
    recorded_paths = {
        Path(str(record.get("path") or "")).resolve()
        for record in records
        if isinstance(record, dict)
    }
    for source in assets:
        relative = source.relative_to(source_references)
        destination = (prepared.sources.references / relative).resolve()
        if not _inside(destination, prepared.sources.references):
            raise ColdStartError(f"Map asset escapes staged references: {relative}")
        if destination in recorded_paths:
            if not destination.is_file() or _sha256(destination) != _sha256(source):
                raise ColdStartError(f"Previously staged map asset changed: {relative}")
            continue
        if destination.exists():
            raise ColdStartError(f"Unrecorded map asset already exists in run: {destination}")
        record = _copy_file(source, destination)
        record["source_relative_path"] = str(relative)
        records.append(record)
        recorded_paths.add(destination)

    staged["map_assets"] = sorted(records, key=lambda row: str(row.get("path") or "").casefold())
    manifest["staged_inputs"] = staged
    _atomic_json(prepared.manifest_path, manifest)
    source_manifest_path = prepared.sources.source_manifest
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_manifest["staged_inputs"] = staged
    _atomic_json(source_manifest_path, source_manifest)
    return load_cold_start_run(prepared.run_root)


def resume_cold_start(run_root: Path, *, model: str, use_llm: bool = True) -> dict[str, Any]:
    """Resume solely from verified staged inputs and caches created in-run."""

    prepared = load_cold_start_run(run_root)
    result = run_pilot(
        prepared.sources.map_id,
        output_dir=prepared.sources.workspace,
        model=model,
        force=False,
        use_llm=use_llm,
        sources=prepared.sources,
        allow_legacy_cache_migration=False,
    )
    manifest = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    history = manifest.get("resume_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "resumed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_hashes_reverified": True,
        "external_calls": int((result.get("llm") or {}).get("external_calls") or 0),
        "result_status": result.get("status"),
    })
    manifest.update({"status": "complete", "result": result, "resume_history": history})
    _atomic_json(prepared.manifest_path, manifest)
    return result


__all__ = [
    "COLD_START_SCHEMA", "ColdStartError", "ColdStartInputs", "PreparedColdStart",
    "audit_pre_run", "discover_inputs", "execute_cold_start", "load_cold_start_run",
    "prepare_cold_start", "resume_cold_start",
]
