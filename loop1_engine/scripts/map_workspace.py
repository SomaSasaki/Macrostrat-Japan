# -*- coding: utf-8 -*-
"""Prepare one self-contained GSJ 1:50,000 review workspace safely.

This module owns the filesystem/network boundary for the generic pilot command.
It deliberately does not create or edit an Excel workbook.  The orchestration
layer can call :func:`prepare_map_workspace` and then pass the returned paths to
the existing ZFK/Shape/PDF processing stages.

Layout::

    data/02_review/<GSJ region>/m<id>_<Japanese title> <year>/
        references/                 # PDF and extracted Shapefile
        llm_cache/                  # created here; populated by the LLM stage
        system/source_manifest.json

ZFK remains in the established, deduplicated raw cache at
``data/raw/zfk/m<id>``.  Its URLs, hashes and availability are recorded in the
map workspace manifest, so the review directory still has a complete audit
trail without copying the same source data.

Safety invariants
-----------------
* An existing ``m<id>_*`` workspace is reused.
* User files are never overwritten.  Downloads and generated manifests use an
  adjacent temporary file followed by an atomic rename.
* ZIP paths are validated and existing extracted files are only accepted when
  byte-identical.
* Missing GSJ products are represented as ``unavailable``/``failed`` states;
  absence of Shape or ZFK is not confused with a pipeline crash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Mapping

from common import canonical_map_title, get_region_folder, normalize_sheet_code, safe_folder_name


ROOT = Path(__file__).resolve().parents[2]
PUBLICATION_URL = "https://gbank.gsj.jp/ld/resource/publication/map/g050/map{map_id}.json"
ZFK_MAP_URL = "https://gbank.gsj.jp/ld/resource/zfk/maps/m{map_id}.json"
ZFK_UNITS_URL = "https://gbank.gsj.jp/ld/resource/zfk/query/unitsInMap?map_id={map_id}"
ZFK_UNIT_URLS = (
    "https://gbank.gsj.jp/ld/resource/zfk/units/{unit_id}.json",
    "https://gbank.gsj.jp/ld/resource/zfk/unit/{unit_id}.json",
)
USER_AGENT = "MacroStrat-GSJ-workspace/1.0"
MANIFEST_SCHEMA = "map-workspace-sources/1.0"

Fetcher = Callable[[str, int], bytes | bytearray | BinaryIO]


class WorkspaceError(RuntimeError):
    """Base error for workspace preparation."""


class WorkspaceMetadataError(WorkspaceError):
    """The map cannot be named or located from authoritative metadata."""


class WorkspaceAmbiguityError(WorkspaceError):
    """More than one human workspace could be the requested map."""


class WorkspaceConflictError(WorkspaceError):
    """A source download conflicts with an existing user-owned file."""


@dataclass(frozen=True)
class AssetState:
    """One source route and its local, auditable state."""

    name: str
    status: str
    available: bool
    url: str = ""
    path: Path | None = None
    sha256: str = ""
    size: int | None = None
    error: str = ""
    files: tuple[Mapping[str, Any], ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self, root: Path) -> dict[str, Any]:
        return {
            "status": self.status,
            "available": self.available,
            "url": self.url,
            "path": _relative_or_absolute(self.path, root) if self.path else "",
            "sha256": self.sha256,
            "size": self.size,
            "error": self.error,
            "files": [dict(row) for row in self.files],
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class WorkspaceInfo:
    """All paths needed by the generic map pipeline."""

    map_id: str
    region_folder: str
    display_name: str
    publication_json: Path
    workspace_dir: Path
    references_dir: Path
    system_dir: Path
    llm_cache_dir: Path
    zfk_dir: Path
    manifest_path: Path
    assets: Mapping[str, AssetState] = field(default_factory=dict)

    def with_assets(self, assets: Mapping[str, AssetState]) -> "WorkspaceInfo":
        return WorkspaceInfo(
            map_id=self.map_id,
            region_folder=self.region_folder,
            display_name=self.display_name,
            publication_json=self.publication_json,
            workspace_dir=self.workspace_dir,
            references_dir=self.references_dir,
            system_dir=self.system_dir,
            llm_cache_dir=self.llm_cache_dir,
            zfk_dir=self.zfk_dir,
            manifest_path=self.manifest_path,
            assets=dict(assets),
        )


def _normalize_map_id(map_id: str | int) -> str:
    value = str(map_id).strip().lstrip("mM")
    if not value.isdigit():
        raise ValueError(f"map_id must be numeric: {map_id!r}")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write(path: Path, data: bytes) -> None:
    """Create ``path`` without ever replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".part-{os.getpid()}")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # On Windows os.link is the simplest atomic create-if-absent primitive.
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _sha256_bytes(data) != _sha256(path):
                raise WorkspaceConflictError(f"existing file differs: {path}")
        finally:
            temporary.unlink(missing_ok=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_replace_generated(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically update a pipeline-owned JSON document."""
    if path.exists():
        current = _load_json(path)
        if not current or current.get("schema_version") != MANIFEST_SCHEMA:
            raise WorkspaceConflictError(
                f"refusing to replace a non-pipeline manifest: {path}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _relative_or_absolute(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _file_record(path: Path, root: Path, *, url: str = "") -> dict[str, Any]:
    return {
        "path": _relative_or_absolute(path, root),
        "url": url,
        "sha256": _sha256(path),
        "size": path.stat().st_size,
    }


def _default_fetcher(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _fetch_bytes(fetcher: Fetcher | None, url: str, timeout: int) -> bytes:
    response = (fetcher or _default_fetcher)(url, timeout)
    if hasattr(response, "read"):
        response = response.read()  # type: ignore[union-attr]
    if not isinstance(response, (bytes, bytearray)):
        raise TypeError(f"fetcher returned {type(response).__name__}, expected bytes")
    return bytes(response)


def _fetch_json(fetcher: Fetcher | None, url: str, timeout: int) -> tuple[bytes, dict[str, Any]]:
    raw = _fetch_bytes(fetcher, url, timeout)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"GSJ returned invalid JSON for {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceError(f"GSJ returned non-object JSON for {url}")
    return raw, value


def _inventory_row(root: Path, map_id: str) -> dict[str, Any]:
    inventory = _load_json(root / "data" / "00_management" / "gsj_50k_inventory.json") or {}
    for row in inventory.get("maps") or []:
        if isinstance(row, dict) and str(row.get("map_id") or "") == map_id:
            return row
    return {}


def _zfk_index_row(root: Path, map_id: str) -> dict[str, Any]:
    index = _load_json(root / "config" / "zfk_index.json") or {}
    rows = index.get("maps") if isinstance(index, dict) else []
    for row in rows or []:
        if isinstance(row, dict) and str(row.get("map_id") or "") == map_id:
            return row
    return {}


def _zfk_map_meta(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    nested = value.get("map")
    return dict(nested) if isinstance(nested, Mapping) else dict(value)


def _download_rows(publication: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in publication.get("downloadData") or [] if isinstance(row, Mapping)]


def _sheet_code(publication: Mapping[str, Any], zfk: Mapping[str, Any],
                inventory: Mapping[str, Any]) -> str:
    for row in _download_rows(publication):
        url = str(row.get("@id") or "")
        match = re.search(r"G0?50[_-](\d{5})", url, flags=re.IGNORECASE)
        if match:
            return normalize_sheet_code(match.group(1))
    for value in (
        zfk.get("sheet_code"), inventory.get("sheet_code"), publication.get("sheet_code")
    ):
        normalized = normalize_sheet_code(value)
        if len(normalized) == 5:
            return normalized
    for page in publication.get("page") or []:
        if not isinstance(page, Mapping):
            continue
        for key in ("tilejson", "tms_dir"):
            normalized = normalize_sheet_code(page.get(key))
            if len(normalized) == 5:
                return normalized
    return ""


def _display_name(publication: Mapping[str, Any], zfk: Mapping[str, Any],
                  inventory: Mapping[str, Any]) -> str:
    title = canonical_map_title(
        pub_title=(publication.get("title_j") or publication.get("title_ja")
                   or inventory.get("title_ja") or ""),
        zfk_title=zfk.get("title_ja") or inventory.get("title_ja") or "",
        pub_year=(publication.get("pub_year") or zfk.get("pub_year")
                  or inventory.get("pub_year") or ""),
    )
    return safe_folder_name(title)


def _existing_workspaces(review_root: Path, map_id: str) -> list[Path]:
    if not review_root.is_dir():
        return []
    hits: list[Path] = []
    for region in review_root.iterdir():
        if not region.is_dir():
            continue
        for child in region.iterdir():
            if child.is_dir() and (
                child.name == f"m{map_id}" or child.name.startswith(f"m{map_id}_")
            ):
                hits.append(child.resolve())
    return sorted(set(hits), key=lambda value: str(value).casefold())


def resolve_map_workspace(
    map_id: str | int,
    *,
    root: Path = ROOT,
    publication: Mapping[str, Any] | None = None,
    zfk_metadata: Mapping[str, Any] | None = None,
    create: bool = True,
) -> WorkspaceInfo:
    """Resolve or create the canonical review directory for any 50k map.

    Resolution is independent of source availability.  Publication metadata is
    preferred, followed by ZFK/inventory metadata.  Existing workspaces win over
    a newly computed name, which keeps human work stable when GSJ labels change.
    """
    root = Path(root).resolve()
    mid = _normalize_map_id(map_id)
    publication_path = root / "data" / "50k" / "raw" / "publication" / "g050" / f"m{mid}.json"
    pub = dict(publication or _load_json(publication_path) or {})
    local_zfk = root / "data" / "50k" / "raw" / "zfk" / f"m{mid}"
    zfk_doc = zfk_metadata or _load_json(local_zfk / "map.json") or _zfk_index_row(root, mid)
    zfk = _zfk_map_meta(zfk_doc)
    inventory = _inventory_row(root, mid)

    title = _display_name(pub, zfk, inventory)
    sheet = _sheet_code(pub, zfk, inventory)
    region = get_region_folder(sheet)
    review_root = root / "data" / "50k" / "02_review"
    existing = _existing_workspaces(review_root, mid)

    expected = None
    if title and region != "Unknown_Region":
        expected = (review_root / region / f"m{mid}_{title}").resolve()
    if len(existing) == 1:
        workspace = existing[0]
        region = workspace.parent.name
        title = workspace.name.split("_", 1)[1] if "_" in workspace.name else title
    elif len(existing) > 1:
        exact = [path for path in existing if expected is not None and path == expected]
        if len(exact) == 1:
            workspace = exact[0]
        else:
            joined = "; ".join(str(path) for path in existing)
            raise WorkspaceAmbiguityError(f"m{mid} workspaces are ambiguous: {joined}")
    else:
        if not title:
            raise WorkspaceMetadataError(f"m{mid}: Japanese title/year metadata is unavailable")
        if region == "Unknown_Region":
            raise WorkspaceMetadataError(f"m{mid}: five-digit GSJ sheet code is unavailable")
        workspace = expected
        assert workspace is not None

    references = workspace / "references"
    system = workspace / "system"
    llm_cache = workspace / "llm_cache"
    if create:
        references.mkdir(parents=True, exist_ok=True)
        system.mkdir(parents=True, exist_ok=True)
        llm_cache.mkdir(parents=True, exist_ok=True)
    return WorkspaceInfo(
        map_id=mid,
        region_folder=region,
        display_name=title,
        publication_json=publication_path,
        workspace_dir=workspace,
        references_dir=references,
        system_dir=system,
        llm_cache_dir=llm_cache,
        zfk_dir=local_zfk,
        manifest_path=system / "source_manifest.json",
    )


def _select_download(publication: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for row in _download_rows(publication):
        url = str(row.get("@id") or "")
        dtype = str(row.get("data_type") or "").casefold()
        title = str(row.get("title") or "").casefold()
        lower = url.casefold()
        if kind == "pdf" and (dtype == "pdf" or lower.endswith(".pdf")):
            candidates.append(row)
        elif kind == "shape" and (
            "shapefile" in dtype or "shapefile" in title or "shape" in title
        ) and lower.endswith(".zip"):
            candidates.append(row)
    if not candidates:
        return None
    if kind == "pdf":
        candidates.sort(key=lambda row: (not str(row.get("@id") or "").casefold().endswith("_d.pdf"),
                                         str(row.get("@id") or "")))
    else:
        candidates.sort(key=lambda row: str(row.get("@id") or ""))
    return candidates[0]


def _url_filename(url: str, fallback: str) -> str:
    value = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
    value = safe_folder_name(value.replace(".", "__DOT__")).replace("__DOT__", ".")
    return value or fallback


def _download_file(url: str, target: Path, *, fetcher: Fetcher | None,
                   timeout: int, validator: Callable[[bytes], None]) -> bool:
    """Download an absent file.  Return True only for a new local file."""
    if target.exists():
        return False
    data = _fetch_bytes(fetcher, url, timeout)
    validator(data)
    _atomic_write(target, data)
    return True


def _validate_pdf(data: bytes) -> None:
    if not data.lstrip().startswith(b"%PDF-"):
        raise WorkspaceError("downloaded PDF does not have a PDF header")


def _validate_zip(data: bytes) -> None:
    if not data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raise WorkspaceError("downloaded Shapefile archive is not a ZIP")


def _find_pdf(references: Path) -> Path | None:
    candidates = sorted(
        (path for path in references.rglob("*") if path.is_file() and path.suffix.casefold() == ".pdf"),
        key=lambda path: (not path.name.casefold().endswith("_d.pdf"), str(path).casefold()),
    )
    return candidates[0] if candidates else None


def _find_shape(references: Path) -> Path | None:
    candidates = sorted(
        path for path in references.rglob("*")
        if path.is_file() and path.name.casefold() == "geo_a.shp"
        and path.with_suffix(".dbf").is_file()
    )
    return candidates[0] if candidates else None


def _source_pdf(workspace: WorkspaceInfo, publication: Mapping[str, Any], *,
                fetch_missing: bool, fetcher: Fetcher | None, timeout: int,
                root: Path) -> AssetState:
    selected = _select_download(publication, "pdf")
    url = str((selected or {}).get("@id") or "")
    existing = _find_pdf(workspace.references_dir)
    if existing:
        try:
            with existing.open("rb") as handle:
                _validate_pdf(handle.read(16))
        except WorkspaceError as exc:
            return AssetState("pdf", "invalid_existing", False, url=url, path=existing,
                              sha256=_sha256(existing), size=existing.stat().st_size,
                              error=str(exc))
        return AssetState("pdf", "existing", True, url=url, path=existing,
                          sha256=_sha256(existing), size=existing.stat().st_size)
    if not url:
        return AssetState("pdf", "unavailable", False)
    if not fetch_missing:
        return AssetState("pdf", "missing", False, url=url)
    target = workspace.references_dir / _url_filename(url, f"m{workspace.map_id}_D.pdf")
    try:
        created = _download_file(url, target, fetcher=fetcher, timeout=timeout,
                                 validator=_validate_pdf)
        return AssetState("pdf", "downloaded" if created else "existing", True,
                          url=url, path=target, sha256=_sha256(target),
                          size=target.stat().st_size)
    except Exception as exc:
        return AssetState("pdf", "failed", False, url=url, error=str(exc))


def _safe_zip_members(archive: zipfile.ZipFile, *, max_files: int = 5000,
                      max_total_size: int = 536_870_912) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > max_files:
        raise WorkspaceError(f"ZIP contains too many entries: {len(members)}")
    if sum(max(0, item.file_size) for item in members) > max_total_size:
        raise WorkspaceError("ZIP uncompressed size exceeds the safety limit")
    for item in members:
        if item.file_size > 268_435_456:
            raise WorkspaceError(f"ZIP entry exceeds the safety limit: {item.filename}")
        posix = PurePosixPath(item.filename.replace("\\", "/"))
        if posix.is_absolute() or ".." in posix.parts or re.match(r"^[A-Za-z]:", item.filename):
            raise WorkspaceError(f"unsafe ZIP path: {item.filename}")
        mode = (item.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise WorkspaceError(f"ZIP symbolic link is not allowed: {item.filename}")
    return members


def _extract_zip_no_overwrite(zip_path: Path, destination: Path) -> tuple[list[Path], list[str]]:
    """Extract safely while preserving every pre-existing byte."""
    extracted: list[Path] = []
    conflicts: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        members = _safe_zip_members(archive)
        for item in members:
            posix = PurePosixPath(item.filename.replace("\\", "/"))
            if not posix.parts:
                continue
            target = destination.joinpath(*posix.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            data = archive.read(item)
            if target.exists():
                if not target.is_file() or _sha256(target) != _sha256_bytes(data):
                    conflicts.append(str(target))
                continue
            _atomic_write(target, data)
            extracted.append(target)
    return extracted, conflicts


def _shape_file_records(shape: Path, root: Path) -> tuple[Mapping[str, Any], ...]:
    rows = []
    for path in sorted(shape.parent.glob(f"{shape.stem}.*")):
        if path.is_file():
            rows.append(_file_record(path, root))
    return tuple(rows)


def _source_shape(workspace: WorkspaceInfo, publication: Mapping[str, Any], *,
                  fetch_missing: bool, fetcher: Fetcher | None, timeout: int,
                  root: Path) -> AssetState:
    selected = _select_download(publication, "shape")
    url = str((selected or {}).get("@id") or "")
    existing = _find_shape(workspace.references_dir)
    if existing:
        return AssetState("shape", "existing", True, url=url, path=existing,
                          sha256=_sha256(existing), size=existing.stat().st_size,
                          files=_shape_file_records(existing, root))
    if not url:
        return AssetState("shape", "unavailable", False)
    if not fetch_missing:
        return AssetState("shape", "missing", False, url=url)
    archive_path = workspace.references_dir / _url_filename(
        url, f"m{workspace.map_id}_shapefile.zip"
    )
    try:
        created = _download_file(url, archive_path, fetcher=fetcher, timeout=timeout,
                                 validator=_validate_zip)
        extract_dir = workspace.references_dir / archive_path.stem
        _new_files, conflicts = _extract_zip_no_overwrite(archive_path, extract_dir)
        shape = _find_shape(workspace.references_dir)
        if conflicts:
            return AssetState(
                # A complete-looking pair can still mix old and new bytes when
                # one member conflicted.  Keep the path for diagnosis, but do
                # not advertise the source as safe for downstream processing.
                "shape", "conflict", False, url=url, path=shape,
                sha256=_sha256(shape) if shape else "",
                size=shape.stat().st_size if shape else None,
                error="existing extracted files differ; none were overwritten",
                files=_shape_file_records(shape, root) if shape else (),
                details={"archive": _file_record(archive_path, root, url=url),
                         "conflicts": conflicts},
            )
        if not shape:
            return AssetState(
                "shape", "failed", False, url=url, path=archive_path,
                sha256=_sha256(archive_path), size=archive_path.stat().st_size,
                error="archive contains no complete geo_A.shp/geo_A.dbf pair",
                details={"archive": _file_record(archive_path, root, url=url)},
            )
        return AssetState(
            "shape", "downloaded" if created else "extracted", True,
            url=url, path=shape, sha256=_sha256(shape), size=shape.stat().st_size,
            files=_shape_file_records(shape, root),
            details={"archive": _file_record(archive_path, root, url=url)},
        )
    except Exception as exc:
        return AssetState("shape", "failed", False, url=url, error=str(exc))


def _unit_rows(index: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = (index.get("result") or {}).get("units") or index.get("units") or []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _unit_id(row: Mapping[str, Any]) -> str:
    value = row.get("id") or row.get("unit_id") or row.get("@id") or ""
    value = str(value).rstrip("/").split("/")[-1].removesuffix(".json")
    return value


def _zfk_complete(zfk_dir: Path) -> tuple[bool, int, int, list[dict[str, Any]]]:
    index = _load_json(zfk_dir / "units-index.json") or {}
    rows = _unit_rows(index)
    ids = [_unit_id(row) for row in rows if _unit_id(row)]
    have = sum(
        _load_json(zfk_dir / "units" / f"{unit_id}.json") is not None
        for unit_id in ids
    )
    geo_have = sum(
        _load_json(zfk_dir / "geojson" / f"{unit_id}.geojson") is not None
        for unit_id in ids
    )
    return bool(ids) and have == len(ids) and geo_have == len(ids), len(ids), have, rows


def _zfk_records(zfk_dir: Path, rows: list[dict[str, Any]], root: Path) -> tuple[Mapping[str, Any], ...]:
    urls: dict[str, str] = {
        "map.json": ZFK_MAP_URL.format(map_id=zfk_dir.name.lstrip("m")),
        "units-index.json": ZFK_UNITS_URL.format(map_id=zfk_dir.name.lstrip("m")),
    }
    for row in rows:
        uid = _unit_id(row)
        if uid:
            urls[f"units/{uid}.json"] = ZFK_UNIT_URLS[0].format(unit_id=uid)
            urls[f"geojson/{uid}.geojson"] = str(
                row.get("geojson_url") or f"https://cdn.gsj.jp/ld/zfk/units_geojson/{uid}.geojson"
            )
    records = []
    for rel, url in sorted(urls.items()):
        path = zfk_dir / Path(rel)
        if path.is_file():
            records.append(_file_record(path, root, url=url))
    return tuple(records)


def _source_zfk(workspace: WorkspaceInfo, *, fetch_missing: bool,
                fetcher: Fetcher | None, timeout: int, root: Path) -> AssetState:
    zfk = workspace.zfk_dir
    complete, expected, have, rows = _zfk_complete(zfk)
    if complete:
        files = _zfk_records(zfk, rows, root)
        return AssetState(
            "zfk", "existing", True, url=ZFK_MAP_URL.format(map_id=workspace.map_id),
            path=zfk, files=files,
            details={"expected_units": expected, "unit_json": have,
                     "geojson": sum((zfk / "geojson" / f"{_unit_id(row)}.geojson").is_file()
                                    for row in rows)},
        )
    if not fetch_missing:
        unit_complete = bool(expected) and have == expected
        return AssetState(
            "zfk", ("existing_partial" if unit_complete else
                    ("missing" if _zfk_index_row(root, workspace.map_id) else "unavailable")),
            unit_complete, url=ZFK_MAP_URL.format(map_id=workspace.map_id), path=zfk,
            details={"expected_units": expected, "unit_json": have},
        )

    downloaded = 0
    errors: list[str] = []
    map_path = zfk / "map.json"
    map_url = ZFK_MAP_URL.format(map_id=workspace.map_id)
    if not map_path.is_file():
        try:
            raw, value = _fetch_json(fetcher, map_url, timeout)
            if value.get("ok") is False or not (value.get("map") or value.get("self")):
                return AssetState("zfk", "unavailable", False, url=map_url)
            _atomic_write(map_path, raw)
            downloaded += 1
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return AssetState("zfk", "unavailable", False, url=map_url)
            errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))

    index_path = zfk / "units-index.json"
    units_url = ZFK_UNITS_URL.format(map_id=workspace.map_id)
    if not index_path.is_file():
        try:
            raw, value = _fetch_json(fetcher, units_url, timeout)
            if not _unit_rows(value):
                return AssetState("zfk", "unavailable", False, url=map_url,
                                  error="GSJ ZFK returned no units")
            _atomic_write(index_path, raw)
            downloaded += 1
        except Exception as exc:
            errors.append(str(exc))

    index = _load_json(index_path) or {}
    rows = _unit_rows(index)
    if index_path.is_file() and not rows:
        return AssetState(
            "zfk", "unavailable", False, url=map_url, path=zfk,
            error="GSJ ZFK unit index contains no units",
        )
    (zfk / "units").mkdir(parents=True, exist_ok=True)
    (zfk / "geojson").mkdir(parents=True, exist_ok=True)
    for row in rows:
        uid = _unit_id(row)
        if not uid:
            continue
        unit_path = zfk / "units" / f"{uid}.json"
        if not unit_path.is_file():
            unit_error = ""
            for template in ZFK_UNIT_URLS:
                unit_url = template.format(unit_id=uid)
                try:
                    raw, value = _fetch_json(fetcher, unit_url, timeout)
                    if value.get("ok") is False:
                        unit_error = f"not available: {unit_url}"
                        continue
                    _atomic_write(unit_path, raw)
                    downloaded += 1
                    unit_error = ""
                    break
                except Exception as exc:
                    unit_error = str(exc)
            if unit_error:
                errors.append(f"{uid}: {unit_error}")
        geo_path = zfk / "geojson" / f"{uid}.geojson"
        geo_url = str(row.get("geojson_url") or
                      f"https://cdn.gsj.jp/ld/zfk/units_geojson/{uid}.geojson")
        if not geo_path.is_file():
            try:
                data = _fetch_bytes(fetcher, geo_url, timeout)
                # A GeoJSON source must parse, but its top-level shape varies.
                json.loads(data.decode("utf-8"))
                _atomic_write(geo_path, data)
                downloaded += 1
            except Exception as exc:
                errors.append(f"{uid} GeoJSON: {exc}")

    complete, expected, have, rows = _zfk_complete(zfk)
    files = _zfk_records(zfk, rows, root)
    geo_have = sum((zfk / "geojson" / f"{_unit_id(row)}.geojson").is_file() for row in rows)
    unit_complete = bool(expected) and have == expected
    status = "downloaded" if complete and not errors else ("partial" if have else "failed")
    return AssetState(
        "zfk", status, unit_complete, url=map_url, path=zfk, files=files,
        error="; ".join(errors[:20]),
        details={"expected_units": expected, "unit_json": have,
                 "geojson": geo_have, "downloaded_files": downloaded,
                 "error_count": len(errors)},
    )


def _publication_state(path: Path, root: Path, url: str, status: str) -> AssetState:
    if not path.is_file():
        return AssetState("publication", status, False, url=url, path=path)
    return AssetState(
        "publication", status, True, url=url, path=path,
        sha256=_sha256(path), size=path.stat().st_size,
    )


def _manifest(workspace: WorkspaceInfo, root: Path) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "generated_at": _utc_now(),
        "map_id": workspace.map_id,
        "workspace": {
            "path": _relative_or_absolute(workspace.workspace_dir, root),
            "region_folder": workspace.region_folder,
            "display_name": workspace.display_name,
            "references": _relative_or_absolute(workspace.references_dir, root),
            "llm_cache": _relative_or_absolute(workspace.llm_cache_dir, root),
            "zfk_cache": _relative_or_absolute(workspace.zfk_dir, root),
        },
        "overwrite_policy": "existing source and human files are never overwritten",
        "sources": {
            name: state.as_dict(root) for name, state in sorted(workspace.assets.items())
        },
    }


def prepare_map_workspace(
    map_id: str | int,
    *,
    root: Path = ROOT,
    fetch_missing: bool = True,
    fetcher: Fetcher | None = None,
    timeout: int = 60,
) -> WorkspaceInfo:
    """Resolve/create a map workspace and acquire only missing source files.

    ``fetcher`` is an injectable ``(url, timeout) -> bytes`` function.  It makes
    all network behavior deterministic in tests and keeps test suites offline.
    Asset failures are recorded in the returned state/manifest instead of
    deleting or replacing any local source.
    """
    root = Path(root).resolve()
    mid = _normalize_map_id(map_id)
    publication_path = root / "data" / "raw" / "publication" / "g050" / f"m{mid}.json"
    publication_url = PUBLICATION_URL.format(map_id=mid)
    pub_status = "existing"
    publication = _load_json(publication_path)
    if publication is None and fetch_missing:
        raw, publication = _fetch_json(fetcher, publication_url, timeout)
        _atomic_write(publication_path, raw)
        pub_status = "downloaded"
    if publication is None:
        raise WorkspaceMetadataError(
            f"m{mid}: publication metadata is missing ({publication_path})"
        )

    workspace = resolve_map_workspace(mid, root=root, publication=publication, create=True)
    assets: dict[str, AssetState] = {
        "publication": _publication_state(
            publication_path, root, publication_url, pub_status
        )
    }
    assets["pdf"] = _source_pdf(
        workspace, publication, fetch_missing=fetch_missing,
        fetcher=fetcher, timeout=timeout, root=root,
    )
    assets["shape"] = _source_shape(
        workspace, publication, fetch_missing=fetch_missing,
        fetcher=fetcher, timeout=timeout, root=root,
    )
    assets["zfk"] = _source_zfk(
        workspace, fetch_missing=fetch_missing,
        fetcher=fetcher, timeout=timeout, root=root,
    )
    workspace = workspace.with_assets(assets)
    _atomic_replace_generated(workspace.manifest_path, _manifest(workspace, root))
    return workspace


__all__ = [
    "AssetState",
    "WorkspaceAmbiguityError",
    "WorkspaceConflictError",
    "WorkspaceError",
    "WorkspaceInfo",
    "WorkspaceMetadataError",
    "prepare_map_workspace",
    "resolve_map_workspace",
]
