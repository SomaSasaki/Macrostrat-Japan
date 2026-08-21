# -*- coding: utf-8 -*-
"""Resolve immutable, evaluation-only GOLD assets.

Production and cold-start runs must never consume files from this module.  It
exists solely so qualification runners do not depend on a mutable map
workspace.  Every returned file is constrained to ``claude_work/gold_snapshots``
and verified against the snapshot manifest before use.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
GOLD_SNAPSHOT_ROOT = (ROOT / "claude_work" / "gold_snapshots").resolve()
SNAPSHOT_SCHEMA = "gold-snapshot/1.0"


class GoldSnapshotError(ValueError):
    """A GOLD snapshot is missing, mutable, or escapes its evaluation root."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def load_snapshot(fixture: Mapping[str, Any]) -> tuple[dict[str, Any], Path]:
    manifest_name = str(fixture.get("gold_snapshot_manifest") or "").strip()
    if not manifest_name:
        raise GoldSnapshotError("GOLD fixture has no evaluation snapshot")
    manifest_path = (ROOT / manifest_name).resolve()
    if not _inside(manifest_path, GOLD_SNAPSHOT_ROOT):
        raise GoldSnapshotError("GOLD snapshot must stay under claude_work/gold_snapshots")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldSnapshotError(f"Cannot read GOLD snapshot manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SNAPSHOT_SCHEMA:
        raise GoldSnapshotError("Unsupported GOLD snapshot manifest")
    if str(manifest.get("map_id") or "") != str(fixture.get("map_id") or ""):
        raise GoldSnapshotError("GOLD snapshot map_id does not match the fixture")
    policy = manifest.get("policy") or {}
    if policy.get("purpose") != "evaluation_only" or policy.get("pipeline_input_allowed") is not False:
        raise GoldSnapshotError("GOLD snapshot is not marked evaluation-only")
    return manifest, manifest_path.parent.resolve()


def bound_path(
    fixture: Mapping[str, Any], binding: str, *, expected_sha256: str | None = None,
) -> Path:
    manifest, snapshot_dir = load_snapshot(fixture)
    record = (manifest.get("files") or {}).get(binding)
    if not isinstance(record, Mapping):
        raise GoldSnapshotError(f"Unknown GOLD snapshot binding: {binding}")
    relative = Path(str(record.get("path") or ""))
    if relative.is_absolute():
        raise GoldSnapshotError(f"GOLD snapshot path must be relative: {binding}")
    path = (snapshot_dir / relative).resolve()
    if not _inside(path, snapshot_dir) or not path.is_file():
        raise GoldSnapshotError(f"GOLD snapshot file is missing or escapes the snapshot: {binding}")
    recorded = str(record.get("sha256") or "").casefold()
    expected = str(expected_sha256 or recorded).casefold()
    if not recorded or (expected_sha256 and recorded != expected):
        raise GoldSnapshotError(f"GOLD fixture and snapshot disagree: {binding}")
    actual = sha256(path)
    if actual != recorded:
        raise GoldSnapshotError(f"GOLD snapshot file changed: {binding}")
    return path


def verify_snapshot(fixture: Mapping[str, Any]) -> dict[str, str]:
    manifest, _snapshot_dir = load_snapshot(fixture)
    return {
        str(binding): str(bound_path(fixture, str(binding)))
        for binding in sorted((manifest.get("files") or {}).keys())
    }


__all__ = [
    "GOLD_SNAPSHOT_ROOT", "GoldSnapshotError", "bound_path", "load_snapshot",
    "sha256", "verify_snapshot",
]
