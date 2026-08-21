# -*- coding: utf-8 -*-
"""Build the auditable Review-v2 artifact set from a legacy review workbook.

The source workbook is read-only.  By default every generated artifact is put
under ``outputs/review_v2/<source stem>/`` at the project root:

* ``compiled.json`` and ``evidence.json`` (canonical durable data)
* ``column_map.png``, ``column_map.json`` and ``column_map.kml`` when a
  sibling ``references/**/geo_A.shp`` is available
* ``<source stem>_v2.xlsx`` (the compact four-sheet review interface)

The spreadsheet builder uses Codex's bundled ``@oai/artifact-tool`` runtime.
An explicit local Node/runtime can also be supplied for use outside Codex.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from column_map import generate_column_map
from compiled_layer import compile_review_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "review_v2"
DEFAULT_BUILDER = Path(__file__).with_name("build_review_v2.mjs")
DEFAULT_HELPER = Path(__file__).with_name("run_artifact_builder.ps1")
OPTIONAL_PATH_SENTINEL = "__MACROSTRAT_NONE__"


class ReviewV2Error(RuntimeError):
    """A user-facing workflow failure with an actionable message."""


@dataclass(frozen=True)
class ArtifactRuntime:
    node: Path
    node_modules: Path
    powershell: Path | None = None


@dataclass(frozen=True)
class WorkflowPaths:
    source_review: Path
    output_dir: Path
    review_v2: Path
    compiled_json: Path
    evidence_json: Path
    map_png: Path
    map_json: Path
    map_kml: Path


@dataclass(frozen=True)
class WorkflowResult:
    paths: WorkflowPaths
    shape_path: Path | None
    unit_count: int
    evidence_count: int
    map_generated: bool
    map_warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_review": str(self.paths.source_review),
            "review_v2": str(self.paths.review_v2),
            "compiled_json": str(self.paths.compiled_json),
            "evidence_json": str(self.paths.evidence_json),
            "shape_path": str(self.shape_path) if self.shape_path else None,
            "unit_count": self.unit_count,
            "evidence_count": self.evidence_count,
            "map_generated": self.map_generated,
            "map_warnings": list(self.map_warnings),
        }
        if self.map_generated:
            result.update({
                "map_png": str(self.paths.map_png),
                "map_json": str(self.paths.map_json),
                "map_kml": str(self.paths.map_kml),
            })
        return result


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve()


def _ensure_repo_path(path: str | os.PathLike[str], *, label: str) -> Path:
    resolved = _resolved(path)
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ReviewV2Error(f"{label} must resolve inside the repository root: {resolved}") from exc
    return resolved


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except (FileNotFoundError, OSError):
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


SUBPROCESS_TIMEOUT = 300  # seconds


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Write one small workflow document without leaving a partial JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _append_log(output_dir: Path, event: str, level: str = "info", **details: Any) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / "build_log.jsonl"
        entry = {"ts": time.time(), "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "level": level}
        entry.update(details)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        # Logging must not break the main flow; swallow errors.
        return


def detect_shape(review_path: str | os.PathLike[str]) -> Path | None:
    """Find the unique sibling ``references/**/geo_A.shp`` if it exists.

    A file directly under ``references`` is preferred.  Multiple nested copies
    are ambiguous and therefore require an explicit ``--shape`` selection.
    """
    review = _resolved(review_path)
    references = review.parent / "references"
    direct = references / "geo_A.shp"
    if direct.is_file():
        return direct.resolve()
    if not references.is_dir():
        return None
    candidates = sorted(
        (path.resolve() for path in references.rglob("geo_A.shp") if path.is_file()),
        key=lambda path: os.path.normcase(str(path)),
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        choices = "\n  - ".join(str(path) for path in candidates)
        raise ReviewV2Error(
            "Multiple geo_A.shp files were found. Select one with --shape:\n  - " + choices
        )
    return candidates[0]


def resolve_paths(
    review_path: str | os.PathLike[str],
    *,
    output: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
) -> WorkflowPaths:
    """Resolve safe output paths; the source workbook can never be overwritten."""
    if output is not None and output_dir is not None:
        raise ReviewV2Error("Use either --output or --output-dir, not both.")
    source = _resolved(review_path)
    if not source.is_file():
        raise ReviewV2Error(f"Review workbook not found: {source}")
    if source.suffix.casefold() != ".xlsx":
        raise ReviewV2Error(f"Review input must be an .xlsx workbook: {source}")

    explicitly_selected = output is not None or output_dir is not None
    if output is not None:
        review_v2 = _resolved(output)
        destination = review_v2.parent
    else:
        destination = (
            _resolved(output_dir)
            if output_dir is not None
            else (DEFAULT_OUTPUT_ROOT / source.stem).resolve()
        )
        review_v2 = destination / f"{source.stem}_v2.xlsx"

    if review_v2.suffix.casefold() != ".xlsx":
        raise ReviewV2Error(f"Review-v2 output must end in .xlsx: {review_v2}")
    if _same_path(source, review_v2):
        raise ReviewV2Error(
            "The Review-v2 output is the source workbook. Choose a different --output path."
        )
    if not explicitly_selected and _same_path(destination, source.parent):
        raise ReviewV2Error(
            "Default output unexpectedly resolves to the source folder; use --output-dir."
        )

    destination.mkdir(parents=True, exist_ok=True)

    return WorkflowPaths(
        source_review=source,
        output_dir=destination,
        review_v2=review_v2,
        compiled_json=destination / "compiled.json",
        evidence_json=destination / "evidence.json",
        map_png=destination / "column_map.png",
        map_json=destination / "column_map.json",
        map_kml=destination / "column_map.kml",
    )


def _runtime_candidates() -> list[tuple[Path, Path]]:
    candidates: list[tuple[Path, Path]] = []
    configured = os.environ.get("CODEX_PRIMARY_RUNTIME")
    if configured:
        root = Path(configured).expanduser()
        candidates.append((
            root / "dependencies" / "node" / "bin" / ("node.exe" if os.name == "nt" else "node"),
            root / "dependencies" / "node" / "node_modules",
        ))
    bundled = (
        Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
        / "dependencies" / "node"
    )
    candidates.append((
        bundled / "bin" / ("node.exe" if os.name == "nt" else "node"),
        bundled / "node_modules",
    ))
    local_node = shutil.which("node")
    local_modules = PROJECT_ROOT / "node_modules"
    if local_node:
        candidates.append((Path(local_node), local_modules))
    return candidates


def find_artifact_runtime(
    *,
    node: str | os.PathLike[str] | None = None,
    node_modules: str | os.PathLike[str] | None = None,
) -> ArtifactRuntime:
    """Locate Node plus a module tree containing ``@oai/artifact-tool``."""
    if (node is None) != (node_modules is None):
        raise ReviewV2Error("--node and --node-modules must be supplied together.")
    candidates = (
        [(_ensure_repo_path(node, label="--node"), _ensure_repo_path(node_modules, label="--node-modules"))]
        if node is not None and node_modules is not None
        else _runtime_candidates()
    )
    for node_path, modules_path in candidates:
        package = modules_path / "@oai" / "artifact-tool" / "package.json"
        if node_path.is_file() and package.is_file():
            powershell_name = "powershell.exe" if os.name == "nt" else "pwsh"
            powershell = shutil.which(powershell_name) or shutil.which("pwsh")
            return ArtifactRuntime(
                node=node_path.resolve(),
                node_modules=modules_path.resolve(),
                powershell=Path(powershell).resolve() if powershell else None,
            )
    raise ReviewV2Error(
        "The @oai/artifact-tool runtime was not found. Run inside Codex Desktop, "
        "or supply both --node and --node-modules."
    )


def _cleanup_generation_artifacts(paths: WorkflowPaths) -> None:
    for candidate in (paths.review_v2, paths.compiled_json, paths.evidence_json, paths.map_png, paths.map_json, paths.map_kml):
        try:
            if candidate.exists() and candidate.is_file():
                candidate.unlink()
        except FileNotFoundError:
            continue


def run_spreadsheet_builder(
    paths: WorkflowPaths,
    *,
    map_generated: bool,
    builder: str | os.PathLike[str] = DEFAULT_BUILDER,
    helper: str | os.PathLike[str] = DEFAULT_HELPER,
    node: str | os.PathLike[str] | None = None,
    node_modules: str | os.PathLike[str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Invoke the Artifact Tool builder through its isolated runtime helper."""
    builder_path = _ensure_repo_path(builder, label="--builder")
    helper_path = _ensure_repo_path(helper, label="--helper")
    if not builder_path.is_file():
        raise ReviewV2Error(f"Review-v2 spreadsheet builder not found: {builder_path}")
    if not helper_path.is_file():
        raise ReviewV2Error(f"Artifact Tool runtime helper not found: {helper_path}")
    runtime = find_artifact_runtime(node=node, node_modules=node_modules)
    if runtime.powershell is None:
        raise ReviewV2Error(
            "PowerShell is required to isolate the bundled Artifact Tool runtime, but was not found."
        )

    # PowerShell may omit empty native-command arguments. These paths are
    # positional in build_review_v2.mjs, so preserve each empty map slot with
    # an explicit value and let the builder translate it back to null.
    map_png = (
        str(paths.map_png) if map_generated and paths.map_png.is_file()
        else OPTIONAL_PATH_SENTINEL
    )
    map_json = (
        str(paths.map_json) if map_generated and paths.map_json.is_file()
        else OPTIONAL_PATH_SENTINEL
    )
    map_kml = (
        str(paths.map_kml) if map_generated and paths.map_kml.is_file()
        else OPTIONAL_PATH_SENTINEL
    )
    command = [
        str(runtime.powershell),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper_path),
        "-NodePath",
        str(runtime.node),
        "-NodeModulesPath",
        str(runtime.node_modules),
        "-BuilderPath",
        str(builder_path),
        "-InputPath",
        str(paths.source_review),
        "-OutputPath",
        str(paths.review_v2),
        "-MapPath",
        map_png,
        "-MapJsonPath",
        map_json,
        "-KmlPath",
        map_kml,
        "-EvidenceJsonPath",
        str(paths.evidence_json),
    ]

    # Human-friendly progress message and structured log
    print(f"[INFO] Starting spreadsheet builder: {builder_path.name}")
    _append_log(paths.output_dir, "builder.start", command=command)
    start = time.perf_counter()
    try:
        completed = run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        _cleanup_generation_artifacts(paths)
        _append_log(paths.output_dir, "builder.timeout", level="error", timeout=SUBPROCESS_TIMEOUT, error=str(exc))
        print(f"[ERROR] Builder timed out after {SUBPROCESS_TIMEOUT}s")
        raise ReviewV2Error(f"Spreadsheet builder timed out after {SUBPROCESS_TIMEOUT}s") from exc
    except Exception as exc:
        _cleanup_generation_artifacts(paths)
        _append_log(paths.output_dir, "builder.exception", level="error", error=str(exc), tb=traceback.format_exc())
        print(f"[ERROR] Builder failed: {exc}")
        raise
    duration = time.perf_counter() - start
    _append_log(
        paths.output_dir,
        "builder.finish",
        level="info",
        returncode=completed.returncode,
        duration_s=duration,
        stdout=(completed.stdout or "")[:10000],
        stderr=(completed.stderr or "")[:10000],
    )
    print(f"[INFO] Builder finished (rc={completed.returncode}) in {duration:.1f}s")
    if completed.returncode != 0:
        _cleanup_generation_artifacts(paths)
        detail = (completed.stderr or completed.stdout or "no process output").strip()
        print(f"[ERROR] Build failed: {detail}")
        raise ReviewV2Error(
            "Review-v2 spreadsheet generation failed. "
            f"Artifact Tool reported:\n{detail}"
        )
    if not paths.review_v2.is_file():
        _cleanup_generation_artifacts(paths)
        _append_log(paths.output_dir, "builder.no_output", level="error")
        raise ReviewV2Error(
            "Artifact Tool exited successfully but did not create the Review-v2 workbook: "
            f"{paths.review_v2}"
        )


def build_review_v2(
    review_path: str | os.PathLike[str],
    *,
    output: str | os.PathLike[str] | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    force: bool = False,
    shape: str | os.PathLike[str] | None = None,
    map_id: str | None = None,
    skip_map: bool = False,
    node: str | os.PathLike[str] | None = None,
    node_modules: str | os.PathLike[str] | None = None,
    builder: str | os.PathLike[str] = DEFAULT_BUILDER,
    helper: str | os.PathLike[str] = DEFAULT_HELPER,
    builder_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> WorkflowResult:
    """Run canonical compilation, optional map generation, and XLSX authoring."""
    paths = resolve_paths(review_path, output=output, output_dir=output_dir)
    if paths.review_v2.exists() and not force:
        raise ReviewV2Error(
            "Review-v2 already exists and may contain human edits: "
            f"{paths.review_v2}\n"
            "Use the existing workbook with `python run.py check <map>` or "
            "`python run.py export <map>`. Use --force only when you intend to "
            "discard that Review-v2 workbook and rebuild it."
        )
    source_digest = _sha256(paths.source_review)
    selected_shape = _resolved(shape) if shape is not None else detect_shape(paths.source_review)
    if selected_shape is not None:
        if not selected_shape.is_file() or selected_shape.suffix.casefold() != ".shp":
            raise ReviewV2Error(f"Selected Shapefile does not exist or is not .shp: {selected_shape}")
        matching_dbf = selected_shape.with_suffix(".dbf")
        if not matching_dbf.is_file():
            raise ReviewV2Error(f"Selected Shapefile has no matching .dbf: {matching_dbf}")

    paths.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        print(f"[INFO] Compiling canonical bundle for {paths.source_review.name}...")
        _append_log(paths.output_dir, "compile.start", input=str(paths.source_review))
        comp_start = time.perf_counter()
        compiled, evidence, written = compile_review_workbook(
            paths.source_review,
            output_dir=paths.output_dir,
            map_id=map_id,
            shape_root=selected_shape.parent if selected_shape is not None else None,
        )
        comp_dur = time.perf_counter() - comp_start
        _append_log(paths.output_dir, "compile.finish", duration_s=comp_dur)
        print(f"[INFO] Compilation finished in {comp_dur:.1f}s")
    except Exception as exc:
        _append_log(paths.output_dir, "compile.exception", level="error", error=str(exc), tb=traceback.format_exc())
        raise ReviewV2Error(
            f"Canonical compiled/evidence generation failed: {exc}"
        ) from exc
    if written is None:
        _append_log(paths.output_dir, "compile.no_output", level="error")
        raise ReviewV2Error("Canonical compilation returned no output paths.")

    map_generated = bool(selected_shape is not None and not skip_map)
    map_warnings: tuple[str, ...] = ()
    if map_generated:
        try:
            print(f"[INFO] Generating column map from shapefile {selected_shape.name}...")
            _append_log(paths.output_dir, "map.start", shape=str(selected_shape))
            map_start = time.perf_counter()
            map_result = generate_column_map(
                paths.source_review,
                selected_shape,
                paths.map_png,
                paths.map_kml,
                title=f"Column review: {paths.source_review.stem}",
            )
            map_dur = time.perf_counter() - map_start
            _write_json(paths.map_json, map_result.as_dict())
            map_warnings = tuple(map_result.warnings)
            _append_log(paths.output_dir, "map.finish", duration_s=map_dur)
            print(f"[INFO] Map generation finished in {map_dur:.1f}s")
        except Exception as exc:
            _append_log(paths.output_dir, "map.exception", level="error", error=str(exc), tb=traceback.format_exc())
            raise ReviewV2Error(f"Column map generation failed: {exc}") from exc

    run_spreadsheet_builder(
        paths,
        map_generated=map_generated,
        builder=builder,
        helper=helper,
        node=node,
        node_modules=node_modules,
        run=builder_runner,
    )

    if _sha256(paths.source_review) != source_digest:
        raise ReviewV2Error(
            "Safety check failed: the source review workbook changed during generation. "
            "The generated outputs should not be accepted until the source is inspected."
        )

    summary = compiled.get("summary", {})
    return WorkflowResult(
        paths=paths,
        shape_path=selected_shape,
        unit_count=int(summary.get("unit_count") or 0),
        evidence_count=int(summary.get("evidence_count") or len(evidence.get("evidence", []))),
        map_generated=map_generated,
        map_warnings=map_warnings,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", type=Path, help="Legacy m####_review.xlsx input (read-only)")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path, help="Explicit Review-v2 .xlsx output")
    destination.add_argument(
        "--output-dir",
        type=Path,
        help="Artifact directory (default: outputs/review_v2/<source stem>)",
    )
    parser.add_argument(
        "--shape",
        type=Path,
        help="Explicit geo_A.shp (default: sibling references/**/geo_A.shp)",
    )
    parser.add_argument("--map-id", help="Override the inferred GSJ map id")
    parser.add_argument(
        "--skip-map",
        action="store_true",
        help="Compile Shape evidence but do not generate PNG/JSON/KML",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Discard an existing Review-v2 workbook and rebuild it",
    )
    parser.add_argument("--node", type=Path, help="Explicit Node executable")
    parser.add_argument(
        "--node-modules",
        type=Path,
        help="Module tree containing @oai/artifact-tool (requires --node)",
    )
    parser.add_argument("--builder", type=Path, default=DEFAULT_BUILDER, help=argparse.SUPPRESS)
    parser.add_argument("--helper", type=Path, default=DEFAULT_HELPER, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_review_v2(
            args.review,
            output=args.output,
            output_dir=args.output_dir,
            force=args.force,
            shape=args.shape,
            map_id=args.map_id,
            skip_map=args.skip_map,
            node=args.node,
            node_modules=args.node_modules,
            builder=args.builder,
            helper=args.helper,
        )
    except (ReviewV2Error, FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
