# -*- coding: utf-8 -*-
"""Run closed-world Column detection and small membership batches through one provider."""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_column_vision import STAGE, _image_token_estimate, _mime_type
from llm_constrained_vision import (
    COLUMN_DETECTION_PROMPT_VERSION,
    COLUMN_MEMBERSHIP_PROMPT_VERSION,
    CONSTRAINED_VALIDATOR_VERSION,
    build_column_detection_prompt,
    build_membership_prompt,
    membership_item_id,
    validate_column_detection,
    validate_membership_batch,
)
from llm_router import AllProvidersFailed, LLMImage, LLMRequest, LLMRouter, ValidationReport
from column_figure_geometry import (
    BOX_LOCATOR_PROMPT_VERSION,
    TEXT_BOX_LOCATOR_PROMPT_VERSION,
    ColumnGeometryError,
    build_box_locator_prompt,
    build_text_box_locator_prompt,
    derive_memberships,
    extract_box_text_catalog,
    extract_column_geometry,
    render_box_catalog,
    resolve_box_assignments_locally,
    validate_box_locator,
)
from pdf_image_extract import _render_pages
from run_column_vision_gold import (
    DEFAULT_FIXTURE,
    DEFAULT_ROUTING,
    _atomic_json,
    _forced_config,
    _read,
    _route_failure_code,
    _source_context,
)


DEFAULT_RENDER_SCALE = 2.0
GEOMETRY_PROMPT_VERSION = f"column-vector-geometry-v1+{BOX_LOCATOR_PROMPT_VERSION}"
TEXT_GEOMETRY_PROMPT_VERSION = (
    f"column-vector-geometry-v1+{TEXT_BOX_LOCATOR_PROMPT_VERSION}"
)
GEOMETRY_BATCH_SIZE = 1
TEXT_GEOMETRY_BATCH_SIZE = 1
ALIAS_RELATIVES = (
    Path("system") / "pdf_enrichment" / "unit_aliases.mapped.json",
)


def _with_japanese_names(
    workspace: Path, units: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """検証済みaliasの日本語地層名をunitへ付ける（無ければ英語名のみ）。

    図の見出しは日本語なのに、供給していたのは英語の翻字だけだった。
    2026-08-12のBedrock実測では48 unit中14 unitが「どの列にも属さない」と
    返され、その多くは日本語名が図に載っている段丘堆積物・層だった。
    日本語名は `unit_aliases.mapped.json`（出典ページと引用つき）にある
    ものだけを使う。翻訳を推測して作らない。
    """

    japanese_by_name: dict[str, str] = {}
    japanese_by_id_and_name: dict[tuple[str, str], str] = {}
    for relative in ALIAS_RELATIVES:
        path = workspace / relative
        if not path.is_file():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        for row in document.get("units") or []:
            if not isinstance(row, Mapping):
                continue
            names = [
                str(value).strip() for value in row.get("japanese_aliases") or []
                if str(value or "").strip()
            ]
            unit_id = str(row.get("unit_id") or "").strip()
            unit_name = " ".join(str(row.get("unit_name") or "").casefold().split())
            if unit_name and names:
                japanese_by_name[unit_name] = names[0]
            if unit_id and unit_name and names:
                japanese_by_id_and_name[(unit_id, unit_name)] = names[0]
    merged = []
    for row in units:
        entry = dict(row)
        unit_id = str(row.get("unit_id") or "").strip()
        unit_name = " ".join(str(row.get("unit_name") or "").casefold().split())
        japanese_name = (
            japanese_by_id_and_name.get((unit_id, unit_name))
            or japanese_by_name.get(unit_name)
        )
        if japanese_name:
            entry["unit_name_ja"] = japanese_name
        merged.append(entry)
    return merged


def _render_scale(fixture: Mapping[str, Any]) -> float:
    """GOLDの描画倍率はfixtureが決める（再現性のため記録された値だけを使う）。

    既定の2.0では第2.1図の日本語ラベルが潰れ、どの列にunitが載っているかを
    読み違えやすい。倍率はGOLDの条件そのものなのでコードではなくfixtureに置く。
    """

    try:
        value = float(fixture.get("render_scale") or DEFAULT_RENDER_SCALE)
    except (TypeError, ValueError):
        return DEFAULT_RENDER_SCALE
    return value if 1.0 <= value <= 6.0 else DEFAULT_RENDER_SCALE


def _tokens(prompt: str, image: Path) -> int:
    return math.ceil(len(prompt.encode("utf-8")) / 3) + _image_token_estimate(image)


def payload_summary(
    workspace: Path, fixture_path: Path, provider: str, model: str,
    routing_path: Path = DEFAULT_ROUTING,
) -> dict[str, Any]:
    fixture, pdf, _abstract, _index, units = _source_context(workspace, fixture_path)
    units = _with_japanese_names(workspace.resolve(), units)
    columns = fixture.get("expected_columns") or []
    forced = _forced_config(_read(routing_path.resolve()), provider, model)
    with tempfile.TemporaryDirectory(prefix="constrained_column_dry_") as temp:
        image = _render_pages(pdf, Path(temp) / "rendered", [int(fixture["pdf_page"])], scale=_render_scale(fixture))[0]
        geometry = extract_column_geometry(
            pdf, int(fixture["pdf_page"]), column_count=len(columns),
        )
        catalog = render_box_catalog(image, geometry, Path(temp) / "box_catalog.png")
        batches = [
            units[index:index + GEOMETRY_BATCH_SIZE]
            for index in range(0, len(units), GEOMETRY_BATCH_SIZE)
        ]
        prompts = [build_box_locator_prompt(batch, geometry.boxes) for batch in batches]
        image_tokens = _image_token_estimate(catalog)
        image_bytes = catalog.stat().st_size
        estimated_input = sum(
            math.ceil(len(prompt.encode("utf-8")) / 3) + image_tokens for prompt in prompts
        )
    return {
        "stage": STAGE,
        "provider": provider,
        "model": model,
        "failover": False,
        "max_attempts_per_call": 1,
        "external_calls": len(batches),
        "image": {
            "pdf_page": int(fixture["pdf_page"]), "bytes": image_bytes,
            "kind": "vector_box_catalog", "boxes": len(geometry.boxes),
        },
        "image_tokens_per_call": image_tokens,
        "canonical_units": len(units),
        "membership_batches": [len(batch) for batch in batches],
        "expected_memberships": sum(len(row.get("expected_items") or []) for row in fixture.get("cases") or []),
        "estimated_input_tokens": estimated_input,
        "reserved_output_tokens": len(batches) * 1024,
        "candidate_max_output_tokens": forced["routes"][STAGE]["candidates"][0].get("max_output_tokens"),
    }


def _run_legacy(
    *, workspace: Path, fixture_path: Path, provider: str, model: str,
    output_path: Path, routing_path: Path, render_scale: float | None,
) -> dict[str, Any]:
    """Preserve the original detection/membership GOLD contract."""

    fixture, pdf, _abstract, _index, units = _source_context(workspace, fixture_path)
    units = _with_japanese_names(workspace.resolve(), units)
    columns = fixture.get("expected_columns") or []
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scale = float(render_scale) if render_scale else _render_scale(fixture)
    memberships: dict[str, tuple[str, ...]] = {}
    detection_result: dict[str, bool] = {}
    route_error: str | None = None
    with tempfile.TemporaryDirectory(prefix="constrained_column_gold_", dir=output_path.parent) as temp:
        scratch = Path(temp)
        image = _render_pages(
            pdf, scratch / "rendered", [int(fixture["pdf_page"])], scale=scale,
        )[0]
        forced_path = scratch / "routing.json"
        _atomic_json(
            forced_path,
            _forced_config(_read(routing_path.resolve()), provider, model),
        )
        router = LLMRouter(config_path=forced_path)
        detection_prompt = build_column_detection_prompt(columns)

        def validate_detection(response: Mapping[str, Any]) -> ValidationReport:
            try:
                detected = validate_column_detection(response, columns)
            except ValueError as exc:
                return ValidationReport(decision="reject", fatal_errors=(str(exc),))
            return ValidationReport(
                decision="accept", accepted=detected,
                unresolved=[key for key, present in detected.items() if not present],
            )

        try:
            routed_detection = router.execute(
                LLMRequest(
                    stage=STAGE,
                    logical_job_id=f"gold_column_detection_{fixture['map_id']}",
                    prompt=detection_prompt,
                    estimated_input_tokens=_tokens(detection_prompt, image),
                    reserved_output_tokens=768,
                    required_capabilities=("text", "json", "vision"),
                    images=(LLMImage(path=image, mime_type=_mime_type(image)),),
                ),
                validate_detection,
            )
            detected = dict(routed_detection.validation.accepted or {})
            present_columns = [
                row for row in columns
                if detected.get(str(row["column_id"]), False)
            ]
            detection_result = {
                str(row["column_id"]): bool(
                    detected.get(str(row["column_id"]), False)
                )
                for row in columns
            }
            for batch_index, start in enumerate(range(0, len(units), 8), start=1):
                if not present_columns:
                    break
                batch = units[start:start + 8]
                prompt = build_membership_prompt(batch, present_columns)

                def validate_batch(
                    response: Mapping[str, Any], batch=batch,
                    present_columns=present_columns,
                ) -> ValidationReport:
                    try:
                        accepted = validate_membership_batch(
                            response, batch, present_columns,
                        )
                    except ValueError as exc:
                        return ValidationReport(
                            decision="reject", fatal_errors=(str(exc),),
                        )
                    return ValidationReport(decision="accept", accepted=accepted)

                routed = router.execute(
                    LLMRequest(
                        stage=STAGE,
                        logical_job_id=(
                            f"gold_column_membership_{fixture['map_id']}_{batch_index}"
                        ),
                        prompt=prompt,
                        estimated_input_tokens=_tokens(prompt, image),
                        reserved_output_tokens=1024,
                        required_capabilities=("text", "json", "vision"),
                        images=(LLMImage(path=image, mime_type=_mime_type(image)),),
                    ),
                    validate_batch,
                )
                memberships.update(dict(routed.validation.accepted or {}))
        except AllProvidersFailed as exc:
            route_error = _route_failure_code(exc)

    by_name = {str(row["unit_id"]): str(row["unit_name"]) for row in units}
    actual_by_column: dict[str, set[str]] = {
        str(row["column_id"]): set() for row in fixture.get("cases") or []
    }
    for unit_id, column_ids in memberships.items():
        for column_id in column_ids:
            if column_id in actual_by_column:
                actual_by_column[column_id].add(
                    membership_item_id(by_name[unit_id], column_id)
                )
    cases = []
    for source in fixture.get("cases") or []:
        column_id = str(source["column_id"])
        cases.append({
            "case_id": str(source["case_id"]),
            "validator_decision": "accept" if route_error is None else "reject",
            "expected_items": list(source.get("expected_items") or []),
            "actual_items": sorted(actual_by_column[column_id]),
            "critical_failures": [route_error] if route_error else [],
        })
    document = {
        "schema_version": "llm-gold-results/1.0",
        "generated_at": generated_at,
        "stage": STAGE,
        "provider": provider,
        "requested_model": model,
        "prompt_version": (
            f"{COLUMN_DETECTION_PROMPT_VERSION}+{COLUMN_MEMBERSHIP_PROMPT_VERSION}"
        ),
        "validator_version": CONSTRAINED_VALIDATOR_VERSION,
        "render_scale": scale,
        "box_locator_mode": "legacy",
        "column_detection": detection_result,
        "cases": cases,
    }
    _atomic_json(output_path, document)
    return document


def run(
    *, workspace: Path, fixture_path: Path, provider: str, model: str,
    output_path: Path, routing_path: Path = DEFAULT_ROUTING,
    render_scale: float | None = None,
    minimum_call_interval: float = 0.0,
    box_locator_mode: str = "legacy",
    fallback_box_assignments: Path | None = None,
) -> dict[str, Any]:
    if box_locator_mode == "legacy":
        return _run_legacy(
            workspace=workspace, fixture_path=fixture_path,
            provider=provider, model=model, output_path=output_path,
            routing_path=routing_path, render_scale=render_scale,
        )
    fixture, pdf, _abstract, _index, units = _source_context(workspace, fixture_path)
    units = _with_japanese_names(workspace.resolve(), units)
    columns = fixture.get("expected_columns") or []
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_path.with_suffix(".box-checkpoint.json")
    run_prompt_version = (
        "column-vector-geometry-local-v1"
        if box_locator_mode == "local" else
        TEXT_GEOMETRY_PROMPT_VERSION
        if box_locator_mode == "text" else GEOMETRY_PROMPT_VERSION
    )
    # 契約はfixtureの値。--render-scale は「解像度が効くか」を同一providerで
    # 比べるための実験用の上書きで、使った値は結果文書へ必ず記録する。
    scale = float(render_scale) if render_scale else _render_scale(fixture)
    memberships: dict[str, tuple[str, ...]] = {}
    box_assignments: dict[str, tuple[str, ...]] = {}
    if checkpoint_path.is_file():
        checkpoint = _read(checkpoint_path)
        if (
            checkpoint.get("provider") == provider
            and checkpoint.get("requested_model") == model
            and checkpoint.get("prompt_version") == run_prompt_version
        ):
            box_assignments = {
                str(unit_id): tuple(str(value) for value in values)
                for unit_id, values in (checkpoint.get("box_assignments") or {}).items()
                if isinstance(values, list)
            }
    detection_result: dict[str, bool] = {}
    geometry_box_count = 0
    geometry = None
    route_error: str | None = None
    with tempfile.TemporaryDirectory(prefix="constrained_column_gold_", dir=output_path.parent) as temp:
        scratch = Path(temp)
        image = _render_pages(pdf, scratch / "rendered", [int(fixture["pdf_page"])], scale=scale)[0]
        forced_path = scratch / "routing.json"
        _atomic_json(forced_path, _forced_config(_read(routing_path.resolve()), provider, model))
        router = LLMRouter(config_path=forced_path)
        try:
            geometry = extract_column_geometry(
                pdf, int(fixture["pdf_page"]), column_count=len(columns),
            )
            geometry_box_count = len(geometry.boxes)
            column_ids = [str(row["column_id"]) for row in columns]
            if box_locator_mode == "local":
                fallback_document = (
                    _read(fallback_box_assignments.resolve())
                    if fallback_box_assignments is not None else {}
                )
                fallback_values = fallback_document.get("box_assignments") or {}
                box_assignments, _local_evidence = resolve_box_assignments_locally(
                    pdf, int(fixture["pdf_page"]), geometry, units,
                    fallback=fallback_values,
                )
                catalog = None
                batches = []
            elif box_locator_mode == "text":
                box_text_catalog = extract_box_text_catalog(
                    pdf, int(fixture["pdf_page"]), geometry, column_ids,
                )
                catalog = None
                # Member rows are subordinate names in the canonical inventory,
                # while the closed-world catalog consists of the chart's boxed
                # map units.  Resolve them locally and only spend provider calls
                # on units that still need a box match.
                for unit in units:
                    unit_id = str(unit.get("unit_id") or "").strip()
                    unit_name = str(unit.get("unit_name") or "").casefold()
                    if unit_id not in box_assignments and " member" in unit_name:
                        box_assignments[unit_id] = ()
                pending_units = [
                    unit for unit in units
                    if str(unit.get("unit_id") or "").strip() not in box_assignments
                ]
                batches = [
                    pending_units[index:index + TEXT_GEOMETRY_BATCH_SIZE]
                    for index in range(0, len(pending_units), TEXT_GEOMETRY_BATCH_SIZE)
                ]
            else:
                box_text_catalog = []
                catalog = render_box_catalog(image, geometry, scratch / "box_catalog.png")
                batches = [
                    units[index:index + GEOMETRY_BATCH_SIZE]
                    for index in range(0, len(units), GEOMETRY_BATCH_SIZE)
                ]
            detection_result = {
                str(row["column_id"]): True
                for row in columns
            }
            for batch_index, batch in enumerate(batches, start=1):
                if all(str(row["unit_id"]) in box_assignments for row in batch):
                    continue
                prompt = (
                    build_text_box_locator_prompt(batch, box_text_catalog)
                    if box_locator_mode == "text"
                    else build_box_locator_prompt(batch, geometry.boxes)
                )

                def validate_batch(
                    response: Mapping[str, Any], batch=batch,
                ) -> ValidationReport:
                    try:
                        accepted = validate_box_locator(response, batch, geometry.boxes)
                    except ValueError as exc:
                        return ValidationReport(decision="reject", fatal_errors=(str(exc),))
                    return ValidationReport(decision="accept", accepted=accepted)

                routed = router.execute(
                    LLMRequest(
                        stage=STAGE,
                        logical_job_id=f"gold_column_box_locator_{fixture['map_id']}_{batch_index}",
                        prompt=prompt,
                        estimated_input_tokens=(
                            math.ceil(len(prompt.encode("utf-8")) / 3)
                            if catalog is None else _tokens(prompt, catalog)
                        ),
                        reserved_output_tokens=(4096 if catalog is None else 1024),
                        required_capabilities=(
                            ("text", "json", "japanese")
                            if catalog is None else ("text", "json", "vision")
                        ),
                        images=(
                            () if catalog is None else
                            (LLMImage(path=catalog, mime_type=_mime_type(catalog)),)
                        ),
                    ),
                    validate_batch,
                )
                box_assignments.update(dict(routed.validation.accepted or {}))
                _atomic_json(checkpoint_path, {
                    "schema_version": "column-box-checkpoint/1.0",
                    "provider": provider,
                    "requested_model": model,
                    "prompt_version": run_prompt_version,
                    "box_assignments": {
                        unit_id: list(box_ids)
                        for unit_id, box_ids in box_assignments.items()
                    },
                })
                if minimum_call_interval > 0:
                    time.sleep(minimum_call_interval)
        except ColumnGeometryError as exc:
            route_error = f"geometry:{type(exc).__name__}"
        except AllProvidersFailed as exc:
            route_error = _route_failure_code(exc)
        if geometry is not None and box_assignments:
            memberships = derive_memberships(
                box_assignments, geometry,
                [str(row["column_id"]) for row in columns],
            )

    by_name = {str(row["unit_id"]): str(row["unit_name"]) for row in units}
    actual_by_column: dict[str, set[str]] = {
        str(row["column_id"]): set() for row in fixture.get("cases") or []
    }
    for unit_id, column_ids in memberships.items():
        for column_id in column_ids:
            if column_id in actual_by_column:
                actual_by_column[column_id].add(membership_item_id(by_name[unit_id], column_id))
    cases = []
    for source in fixture.get("cases") or []:
        column_id = str(source["column_id"])
        cases.append({
            "case_id": str(source["case_id"]),
            "validator_decision": "accept" if route_error is None else "reject",
            "expected_items": list(source.get("expected_items") or []),
            "actual_items": sorted(actual_by_column[column_id]),
            "critical_failures": [route_error] if route_error else [],
        })
    document = {
        "schema_version": "llm-gold-results/1.0",
        "generated_at": generated_at,
        "stage": STAGE,
        "provider": provider,
        "requested_model": model,
        "prompt_version": run_prompt_version,
        "validator_version": CONSTRAINED_VALIDATOR_VERSION,
        "render_scale": scale,
        "minimum_call_interval": minimum_call_interval,
        "box_locator_mode": box_locator_mode,
        "geometry_box_count": geometry_box_count,
        "box_assignments": {
            unit_id: list(box_ids) for unit_id, box_ids in box_assignments.items()
        },
        "unresolved_unit_ids": sorted(
            str(row["unit_id"]) for row in units
            if str(row["unit_id"]) not in box_assignments
        ),
        "column_detection": detection_result,
        "cases": cases,
    }
    _atomic_json(output_path, document)
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--routing", type=Path, default=DEFAULT_ROUTING)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--render-scale", type=float, default=None,
        help="実験用の描画倍率の上書き。既定はfixtureの render_scale。使った値は結果に記録される。",
    )
    parser.add_argument(
        "--minimum-call-interval", type=float, default=0.0,
        help="Minimum seconds to wait after each accepted box-locator call.",
    )
    parser.add_argument(
        "--box-locator-mode",
        choices=("legacy", "vision", "text", "local"), default="legacy",
        help="Match units from a rendered box catalog or extracted Unicode text.",
    )
    parser.add_argument(
        "--fallback-box-assignments", type=Path,
        help="Optional source-only model checkpoint used for aliases absent from the PDF index.",
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        result = payload_summary(args.workspace, args.fixture, args.provider, args.model, args.routing)
    else:
        if args.output is None:
            parser.error("--output is required unless --dry-run is used")
        result = run(
            workspace=args.workspace, fixture_path=args.fixture, provider=args.provider,
            model=args.model, output_path=args.output, routing_path=args.routing,
            render_scale=args.render_scale,
            minimum_call_interval=max(0.0, args.minimum_call_interval),
            box_locator_mode=args.box_locator_mode,
            fallback_box_assignments=args.fallback_box_assignments,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
