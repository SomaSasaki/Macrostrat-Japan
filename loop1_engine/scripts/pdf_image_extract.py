# -*- coding: utf-8 -*-
"""Rank and render PDF pages that may contain stratigraphic column figures.

This module is deliberately local and deterministic: it never calls an LLM
and it does not select the first large embedded image.  Candidates are ranked
from the cached page-text index, rendered as complete pages, and recorded with
their PDF page number plus source/render SHA256 values.

``extract_columnar_images`` is retained as the small public interface used by
the experimental code, but now returns ranked full-page renders and writes a
``figure_candidates.json`` provenance manifest beside them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


FIGURE_RANKING_VERSION = "stratigraphic-figure-ranker/2.0"

PROMPT_TERMS: tuple[tuple[str, int], ...] = (
    ("stratigraphy", 20),
    ("stratigraphic", 16),
    ("geologicalcolumn", 20),
    ("geologiccolumn", 20),
    ("summaryofthegeology", 18),
    ("correlationchart", 14),
    ("correlation", 7),
    ("westernarea", 7),
    ("easternarea", 7),
    ("inascendingorder", 5),
    ("層序", 20),
    ("層序区分", 28),
    ("柱状図", 20),
    ("地質総括", 18),
    ("地質時代", 10),
    ("火成作用", 12),
    ("堆積場", 18),
    ("構造場", 12),
    ("対比表", 14),
    ("対比", 6),
    ("geologicage", 10),
    ("igneousactivity", 12),
    ("depositionalenvironment", 18),
    ("tectonics", 12),
)

NEGATIVE_TERMS: tuple[tuple[str, int], ...] = (
    ("locationmap", -25),
    ("indexmap", -25),
    ("位置図", -25),
    ("索引図", -25),
    ("露頭の位置図", -35),
)

ENVIRONMENT_FIGURE_TERMS: tuple[str, ...] = (
    "summaryofthegeology",
    "correlationchart",
    "correlation",
    "geologicalcolumn",
    "geologiccolumn",
    "地質総括",
    "対比表",
    "柱状図",
    "層序",
)


@dataclass(frozen=True)
class PageCandidate:
    pdf_page: int
    score: int
    matched_terms: tuple[str, ...]
    negative_terms: tuple[str, ...]
    text_excerpt: str
    selection_signals: tuple[str, ...] = ()
    image_file: str | None = None
    image_sha256: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compact(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _pages(index: Mapping[str, Any] | Sequence[Any]) -> list[str]:
    value: Any = index.get("pages") if isinstance(index, Mapping) else index
    return [str(item or "") for item in value] if isinstance(value, list) else []


def _structural_figure_signals(normalized: str) -> tuple[int, tuple[str, ...]]:
    """Score co-occurring labels that identify a summary chart, not prose.

    Individual words such as ``層序`` occur throughout geological reports.
    A full summary figure instead exposes several axis/header labels on the
    same PDF page.  These layout-derived text signals work on searchable
    vector PDFs without OCR, an LLM, or a known page number.
    """

    score = 0
    signals: list[str] = []
    caption_titles = (
        "地質総括図", "層序総括図", "層序概念図",
        "summaryofgeology", "summaryofthegeology",
        "stratigraphicsummary", "correlationchart",
    )
    if re.search(r"第\d+(?:[.・]\d+)?図.{0,48}(?:地質総括図|層序総括図|層序概念図)", normalized):
        score += 28
        signals.append("caption-before-summary-title")
    elif re.search(r"fig(?:ure)?\d+.{0,48}(?:summaryof(?:the)?geology|stratigraphicsummary)", normalized):
        score += 28
        signals.append("caption-before-summary-title")

    japanese_axes = ("地質時代", "層序区分", "火成作用", "堆積場", "構造場")
    english_axes = (
        "geologicage", "geologictime", "geologicunits",
        "igneousactivity", "depositionalenvironment", "tectonics",
    )
    axis_count = max(
        sum(term in normalized for term in japanese_axes),
        sum(term in normalized for term in english_axes),
    )
    if axis_count >= 3:
        score += 8 + (axis_count - 3) * 5
        signals.append(f"summary-axis-cooccurrence:{axis_count}")

    japanese_regions = all(term in normalized for term in ("西部", "中央部", "東部"))
    english_regions = all(
        term in normalized for term in ("western", "central", "eastern")
    )
    if japanese_regions or english_regions:
        score += 14
        signals.append("three-region-axis")

    if any(title in normalized for title in caption_titles) and axis_count >= 3:
        score += 18
        signals.append("summary-title-with-multiple-axes")
    return score, tuple(signals)


def rank_column_pages(
    index: Mapping[str, Any] | Sequence[Any],
    *,
    max_pages: int = 5,
    minimum_score: int = 8,
) -> list[PageCandidate]:
    """Return the strongest stratigraphic-figure page candidates.

    Page numbers are one-based.  Ties are resolved by page number so the same
    index always yields the same result.
    """

    ranked: list[PageCandidate] = []
    for position, raw_text in enumerate(_pages(index), start=1):
        if _looks_like_reference_page(str(raw_text or "")):
            continue
        normalized = _compact(raw_text)
        positive = tuple(term for term, _weight in PROMPT_TERMS if term in normalized)
        negative = [term for term, _weight in NEGATIVE_TERMS if term in normalized]
        score = sum(weight for term, weight in PROMPT_TERMS if term in normalized)
        score += sum(weight for term, weight in NEGATIVE_TERMS if term in normalized)
        structural_score, structural_signals = _structural_figure_signals(normalized)
        score += structural_score
        figure_references = len(re.findall(r"第\d+図", normalized))
        if figure_references >= 5:
            # The table of figures contains many perfect keywords but is not a
            # diagram page.  Density is more robust than relying on one title.
            score -= min(60, figure_references * 3)
            negative.append("figure-index-density")
        # Formation-rich summary pages are stronger than pages with one
        # incidental keyword, but the contribution is capped.
        score += min(10, normalized.count("formation"))
        score += min(6, normalized.count("deposits") // 2)
        if score < minimum_score:
            continue
        excerpt = re.sub(r"\s+", " ", str(raw_text or "")).strip()[:320]
        ranked.append(PageCandidate(
            position,
            score,
            positive,
            tuple(negative),
            excerpt,
            structural_signals,
        ))
    ranked.sort(key=lambda item: (-item.score, item.pdf_page))
    return ranked[: max(1, int(max_pages))]


def _looks_like_reference_page(text: str) -> bool:
    """Reject bibliography pages that mention stratigraphy in paper titles."""

    compact = _compact(text)
    heading = compact[:180]
    years = len(re.findall(r"(?:18|19|20)\d{2}", compact))
    volume_markers = compact.count("vol")
    issue_markers = compact.count("no")
    page_markers = compact.count("p")
    citation_heavy = volume_markers >= 3 and issue_markers >= 3 and page_markers >= 10
    return (
        ("文献" in heading or "references" in heading)
        and years >= 4
    ) or citation_heavy


def _looks_like_english_abstract(text: str) -> bool:
    """Avoid spending image tokens on text-only English summary pages."""

    compact = _compact(text)
    japanese = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return (
        latin > max(400, japanese * 8)
        and "inascendingorder" in compact
        and "summaryofthegeology" not in compact
    )


def rank_environment_pages(
    index: Mapping[str, Any] | Sequence[Any],
    *,
    max_pages: int = 3,
) -> list[PageCandidate]:
    """Return figure pages useful for environment and facies interpretation.

    The ordinary Column ranker intentionally has broad recall.  Environment
    analysis is more expensive because every selected page is sent to a
    multimodal model, so bibliography pages and text-only English abstracts
    are removed here.  Complete page renders remain reviewable evidence.
    """

    pages = _pages(index)
    broad = rank_column_pages(
        index,
        max_pages=max(20, max_pages * 6),
        minimum_score=8,
    )
    selected: list[PageCandidate] = []
    for candidate in broad:
        text = pages[candidate.pdf_page - 1] if candidate.pdf_page <= len(pages) else ""
        compact = _compact(text)
        if _looks_like_reference_page(text) or _looks_like_english_abstract(text):
            continue
        if not any(term in compact for term in ENVIRONMENT_FIGURE_TERMS):
            continue
        selected.append(candidate)
        if len(selected) >= max(1, int(max_pages)):
            break
    return selected


def _load_index(pdf: Path, pdf_index: str | os.PathLike[str] | Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(pdf_index, Mapping):
        return dict(pdf_index)
    candidates: list[Path] = []
    if pdf_index:
        candidates.append(Path(pdf_index).expanduser().resolve())
    stem_match = re.search(r"m?(\d+)", pdf.stem, re.I)
    if stem_match:
        candidates.append(pdf.parent / f"m{stem_match.group(1)}_pdfpages.json")
    for candidate in candidates:
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(document, dict) and isinstance(document.get("pages"), list):
            return document
    from pdf_locate import build_index

    document = build_index(str(pdf), quiet=True)
    return document if isinstance(document, dict) else {}


def _bundled_python() -> Path:
    configured = os.environ.get("CODEX_PRIMARY_RUNTIME")
    runtime = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
    )
    return runtime / "dependencies" / "python" / (
        "python.exe" if os.name == "nt" else "bin/python"
    )


def _render_pages(pdf: Path, output_dir: Path, pages: Sequence[int], *, scale: float = 2.0) -> list[Path]:
    helper = Path(__file__).with_name("pdf_render_pages.py")
    python = _bundled_python()
    if not python.is_file() or not helper.is_file():
        raise RuntimeError("Bundled PDF render runtime is unavailable")
    completed = subprocess.run(
        [str(python), str(helper), str(pdf), str(output_dir), json.dumps(list(pages)), str(scale)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError("PDF candidate rendering failed: " + (completed.stderr or completed.stdout)[-800:])
    try:
        rendered = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("PDF renderer returned invalid JSON") from exc
    paths = [Path(value).resolve() for value in rendered] if isinstance(rendered, list) else []
    if len(paths) != len(pages) or not all(path.is_file() for path in paths):
        raise RuntimeError("PDF renderer did not produce every requested candidate page")
    return paths


def extract_columnar_images(
    pdf_path: str,
    output_dir: str,
    max_images: int = 5,
    min_width: int = 400,
    min_height: int = 400,
    *,
    pdf_index: str | os.PathLike[str] | Mapping[str, Any] | None = None,
) -> list[str]:
    """Render ranked complete-page candidates and write their provenance.

    ``min_width`` and ``min_height`` remain accepted for source compatibility;
    page renders are always larger than the legacy thresholds.
    """

    del min_width, min_height
    pdf = Path(pdf_path).expanduser().resolve()
    if not pdf.is_file():
        return []
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    index = _load_index(pdf, pdf_index)
    ranking_pool = rank_column_pages(index, max_pages=max(8, int(max_images)))
    candidates = ranking_pool[: max(1, int(max_images))]
    if not candidates:
        manifest = {
            "schema_version": "pdf-figure-candidates/2.0",
            "status": "no_candidate",
            "pdf": str(pdf),
            "pdf_sha256": _sha256(pdf),
            "ranking_version": FIGURE_RANKING_VERSION,
            "candidates": [],
        }
        (destination / "figure_candidates.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return []

    rendered = _render_pages(pdf, destination, [item.pdf_page for item in candidates])
    completed: list[PageCandidate] = []
    for candidate, image in zip(candidates, rendered):
        completed.append(PageCandidate(
            **{**asdict(candidate), "image_file": str(image), "image_sha256": _sha256(image)}
        ))
    manifest = {
        "schema_version": "pdf-figure-candidates/2.0",
        "status": "selected",
        "pdf": str(pdf),
        "pdf_sha256": _sha256(pdf),
        "ranking_version": FIGURE_RANKING_VERSION,
        "selection_policy": (
            "highest deterministic page-text score with summary-caption, axis-label, "
            "and regional-column co-occurrence; complete-page render; no LLM"
        ),
        "selected_candidate": asdict(completed[0]),
        "selection": {
            "pdf_page": completed[0].pdf_page,
            "score": completed[0].score,
            "score_margin": (
                completed[0].score - ranking_pool[1].score
                if len(ranking_pool) > 1 else completed[0].score
            ),
            "matched_terms": list(completed[0].matched_terms),
            "structural_signals": list(completed[0].selection_signals),
            "gold_page_number_used": False,
            "llm_used": False,
        },
        "candidates": [asdict(item) for item in completed],
        "ranked_alternatives": [asdict(item) for item in ranking_pool[len(completed):]],
    }
    temporary = destination / "figure_candidates.json.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination / "figure_candidates.json")
    return [str(path) for path in rendered]


def extract_environment_images(
    pdf_path: str,
    output_dir: str,
    max_images: int = 3,
    *,
    pdf_index: str | os.PathLike[str] | Mapping[str, Any] | None = None,
) -> list[str]:
    """Render a small, provenance-recorded environment figure set."""

    pdf = Path(pdf_path).expanduser().resolve()
    if not pdf.is_file():
        return []
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    index = _load_index(pdf, pdf_index)
    candidates = rank_environment_pages(index, max_pages=max_images)
    manifest_path = destination / "environment_figure_candidates.json"
    if not candidates:
        manifest = {
            "schema_version": "pdf-environment-figure-candidates/1.0",
            "status": "no_candidate",
            "pdf": str(pdf),
            "pdf_sha256": _sha256(pdf),
            "candidates": [],
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return []

    rendered = _render_pages(pdf, destination, [item.pdf_page for item in candidates])
    completed: list[PageCandidate] = []
    for candidate, image in zip(candidates, rendered):
        completed.append(PageCandidate(
            **{**asdict(candidate), "image_file": str(image), "image_sha256": _sha256(image)}
        ))
    manifest = {
        "schema_version": "pdf-environment-figure-candidates/1.0",
        "status": "candidate_review",
        "pdf": str(pdf),
        "pdf_sha256": _sha256(pdf),
        "selection_policy": (
            "ranked stratigraphic/correlation pages; bibliography and text-only "
            "English abstract pages excluded; complete-page render; no LLM"
        ),
        "candidates": [asdict(item) for item in completed],
    }
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    return [str(path) for path in rendered]
