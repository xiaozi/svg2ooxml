"""Scoring, classification, and known-outcome handling for ``corpus_audit``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.visual.corpus_audit import AuditResult


_KNOWN_AUDIT_OUTCOMES = {
    "tests/svg/filters-conv-05-f.svg": (
        "accepted-limitation",
        "live browser comparison passes; remaining priority comes from "
        "expected bitmap fallback for feConvolveMatrix edgeMode filters, "
        "which OOXML cannot express natively",
    ),
    "tests/svg/text-dom-01-f.svg": (
        "accepted-limitation",
        "scripted SVG DOM conformance test mutates the rendered tree from "
        "onload JavaScript, while the converter intentionally ignores SVG "
        "scripts and event handlers for security",
    ),
    "tests/svg/text-intro-02-b.svg": (
        "deferred",
        "live browser and PPTX agree on bidi text content and placement, but "
        "native editable text rasterizes with different font fallback and "
        "antialiasing between Chromium and the PPTX renderer; closing the "
        "pixel row needs broader text renderer parity or semantic text oracles",
    ),
    "tests/svg/text-intro-09-b.svg": (
        "deferred",
        "live browser and PPTX agree on webfont-backed bidi text content and "
        "placement, but native editable text rasterizes with different font "
        "fallback and antialiasing between Chromium and the PPTX renderer; "
        "closing the pixel row needs broader text renderer parity or semantic "
        "text oracles",
    ),
    "tests/svg/text-tspan-02-b.svg": (
        "deferred",
        "live browser and PPTX agree on rotated tspan text content and "
        "placement, but native editable text rasterizes differently between "
        "Chromium and the PPTX renderer; closing the pixel row needs broader "
        "text renderer parity or semantic text oracles",
    ),
    "tests/svg/text-text-07-t.svg": (
        "deferred",
        "live browser and PPTX agree on per-glyph x/y/rotate text placement, "
        "but native editable text rasterizes differently between Chromium and "
        "the PPTX renderer; closing the residual pixel score needs broader "
        "text renderer parity or semantic text oracles",
    ),
    "tests/svg/text-text-09-t.svg": (
        "deferred",
        "live browser and PPTX agree on shortened per-glyph x/y/rotate text "
        "placement, but native editable text rasterizes differently between "
        "Chromium and the PPTX renderer; closing the residual pixel score "
        "needs broader text renderer parity or semantic text oracles",
    ),
    "tests/svg/filters-overview-02-b.svg": (
        "deferred",
        "remaining mismatch is exact SVG filter input-source parity after "
        "bounded fixes for background input bounds and user-space paint "
        "surfaces; closing the row requires broader SourceGraphic, "
        "SourceAlpha, BackgroundImage, and paint-input renderer work",
    ),
    "tests/svg/filters-overview-03-b.svg": (
        "deferred",
        "bundled PNG oracle is stale and the live browser reference does not "
        "render SVG 1.1 BackgroundImage/BackgroundAlpha inputs, while the "
        "converter now emits those background input surfaces; remaining "
        "object-bounding-box paint/source parity needs broader filter work",
    ),
    "tests/visual/fixtures/resvg/transform_torture.svg": (
        "accepted-limitation",
        "browser reference treats the fixture's SVG 1.1-incompatible "
        "<g> child inside <clipPath> as an empty clip, while the converter "
        "intentionally resolves nested clip geometry for resvg stress coverage",
    ),
}


def _set_error_category(result: AuditResult) -> None:
    if result.error_category is None and result.errors:
        result.error_category = next(iter(result.errors))


def _apply_structure_penalty_policy(result: AuditResult) -> None:
    if not _has_group_filter_bitmap_fallback(result):
        return
    result.structure_penalty_suppressed = True
    note = (
        "Structure count/bbox priority suppressed: filter group bitmap fallback "
        "collapses multiple source leaves into one target image."
    )
    if note not in result.notes:
        result.notes.append(note)


def _has_group_filter_bitmap_fallback(result: AuditResult) -> bool:
    reasons = result.fallback_reason_counts or {}
    if reasons.get("action:group_filter_fallback_rendered", 0) <= 0:
        return False
    return (result.rasterized_count or 0) > 0 or reasons.get("fallback:bitmap", 0) > 0


def score_audit_result(result: AuditResult) -> float:
    """Compute a priority score for a result, higher means more urgent."""
    score = 0.0
    if result.build_status == "error":
        score += 1000.0
    if result.render_status == "error":
        score += 250.0
    elif result.render_status == "unavailable":
        score += 25.0
    if result.browser_status == "error":
        score += 120.0
    elif result.browser_status == "unavailable":
        score += 10.0
    if result.diff_status == "error":
        score += 80.0
    elif result.diff_status == "mismatch":
        score += 40.0
    if result.animation_status == "error":
        score += 180.0
    elif result.animation_status == "unavailable":
        score += 20.0
    elif result.animation_status == "mismatch":
        score += 90.0

    if result.ssim_score is not None:
        score += max(0.0, (1.0 - result.ssim_score) * 200.0)
    if result.pixel_diff_percentage is not None:
        score += result.pixel_diff_percentage
    if result.rasterized_count is not None:
        score += result.rasterized_count * 8.0
    if not result.structure_penalty_suppressed and result.max_bbox_delta is not None:
        score += result.max_bbox_delta * 2.0
    if not result.structure_penalty_suppressed and result.count_delta is not None:
        score += abs(result.count_delta) * 20.0
    if result.animation_min_ssim is not None:
        score += max(0.0, (1.0 - result.animation_min_ssim) * 250.0)
    if result.animation_max_pixel_diff_percentage is not None:
        score += result.animation_max_pixel_diff_percentage * 0.5
    return round(score, 3)


def _finalize_score(result: AuditResult) -> None:
    """Persist raw score and apply ADR-037 known-outcome priority handling."""

    raw_score = score_audit_result(result)
    result.raw_score = raw_score
    if _known_outcome_suppresses_priority(result):
        note = f"ADR-037 priority suppressed; raw score {raw_score:.1f}."
        if note not in result.notes:
            result.notes.append(note)
        result.score = 0.0
        return
    result.score = raw_score


def _known_outcome_suppresses_priority(result: AuditResult) -> bool:
    if result.triage_outcome not in {"accepted-limitation", "deferred"}:
        return False
    return not result.errors


def _apply_known_audit_outcome(result: AuditResult, svg_path: Path) -> None:
    outcome = _known_audit_outcome_for_svg(svg_path)
    if outcome is None:
        return
    triage_outcome, triage_reason = outcome
    result.triage_outcome = triage_outcome
    result.triage_reason = triage_reason
    note = f"ADR-037 outcome {triage_outcome}: {triage_reason}."
    if note not in result.notes:
        result.notes.append(note)


def _known_audit_outcome_for_svg(svg_path: Path) -> tuple[str, str] | None:
    normalized = Path(svg_path).as_posix()
    try:
        normalized = (
            Path(svg_path).resolve().relative_to(Path.cwd().resolve()).as_posix()
        )
    except ValueError:
        pass
    direct = _KNOWN_AUDIT_OUTCOMES.get(normalized)
    if direct is not None:
        return direct
    for known_path, outcome in _KNOWN_AUDIT_OUTCOMES.items():
        if normalized.endswith(f"/{known_path}"):
            return outcome
    return None
