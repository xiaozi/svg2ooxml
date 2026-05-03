"""Animation audit and trace metrics for ``corpus_audit``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree as ET
from PIL import Image

from tools.visual.browser_renderer import BrowserRenderError
from tools.visual.diff import VisualDiffer
from tools.visual.renderer import VisualRendererError

if TYPE_CHECKING:
    from tools.visual.corpus_audit import AuditResult


def _run_animation_audit(
    result: AuditResult,
    *,
    svg_text: str,
    svg_path: Path,
    pptx_path: Path,
    artifact_dir: Path,
    renderer: object | None,
    browser_renderer: object,
    browser_available: bool,
    differ: VisualDiffer,
    duration: float,
    fps: float,
) -> None:
    capture_animation = getattr(renderer, "capture_animation", None)
    if renderer is None or not callable(capture_animation):
        result.animation_status = "unavailable"
        result.notes.append("Animation audit requires a renderer with live capture.")
        return
    if not browser_available:
        result.animation_status = "unavailable"
        result.notes.append("Animation audit requires a browser renderer.")
        return

    render_frames: list[Path] | None = None
    browser_frames: list[Path] | None = None

    try:
        render_frames = list(
            capture_animation(
                pptx_path,
                artifact_dir / "render_animation",
                duration=duration,
                fps=fps,
            )
        )
        browser_frames = list(
            browser_renderer.capture_animation(
                svg_text,
                artifact_dir / "browser_animation",
                duration=duration,
                fps=fps,
                source_path=svg_path,
            )
        )
    except (
        BrowserRenderError,
        VisualRendererError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        result.animation_status = "error"
        result.errors["animation"] = str(exc)
        result.notes.append("Animation capture failed.")
        return

    frame_count = min(len(render_frames), len(browser_frames))
    result.animation_frame_count = frame_count
    if frame_count <= 0:
        result.animation_status = "error"
        result.errors["animation"] = "Animation capture produced no comparable frames."
        result.notes.append("Animation capture produced no comparable frames.")
        return
    if len(render_frames) != len(browser_frames):
        result.notes.append(
            "Animation frame counts differ between PowerPoint and browser capture."
        )

    ssim_scores: list[float] = []
    pixel_diffs: list[float] = []
    worst_comparison = None
    worst_index = -1

    for index in range(frame_count):
        comparison = differ.compare(
            Image.open(browser_frames[index]),
            Image.open(render_frames[index]),
            generate_diff=True,
        )
        ssim_scores.append(comparison.ssim_score)
        pixel_diffs.append(comparison.pixel_diff_percentage)
        if (
            worst_comparison is None
            or comparison.ssim_score < worst_comparison.ssim_score
        ):
            worst_comparison = comparison
            worst_index = index

    result.animation_avg_ssim = sum(ssim_scores) / len(ssim_scores)
    result.animation_min_ssim = min(ssim_scores)
    result.animation_max_pixel_diff_percentage = max(pixel_diffs)
    result.animation_status = (
        "ok" if all(score >= differ.threshold for score in ssim_scores) else "mismatch"
    )
    if result.animation_status == "mismatch":
        result.notes.append("Animation parity mismatch.")

    if worst_comparison is not None and worst_comparison.diff_image is not None:
        diff_dir = artifact_dir / "animation_diff"
        diff_dir.mkdir(exist_ok=True)
        worst_comparison.save_diff(diff_dir / f"frame_{worst_index:04d}.png")


def _svg_has_animation(svg_text: str) -> bool:
    try:
        parser = ET.XMLParser(recover=True)
        root = ET.fromstring(svg_text.encode("utf-8"), parser)
    except ET.XMLSyntaxError:
        return False
    animation_tags = {
        "animate",
        "animateMotion",
        "animateTransform",
        "animateColor",
        "set",
    }
    for element in root.iter():
        tag = element.tag
        if isinstance(tag, str) and tag.split("}")[-1] in animation_tags:
            return True
    return False


def _apply_animation_trace_metrics(
    result: AuditResult,
    trace_report: dict[str, object] | None,
) -> None:
    if not isinstance(trace_report, dict):
        return
    stage_events = trace_report.get("stage_events")
    if not isinstance(stage_events, list):
        return

    emitted = 0
    skipped = 0
    reason_counts: dict[str, int] = {}

    def _bump(reason: str | None) -> None:
        if not reason:
            return
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    for event in stage_events:
        if not isinstance(event, dict):
            continue
        if event.get("stage") != "animation":
            continue
        action = event.get("action")
        metadata = event.get("metadata")
        metadata_dict = metadata if isinstance(metadata, dict) else {}
        if action == "fragment_emitted":
            emitted += 1
            continue
        if action == "fragment_skipped":
            skipped += 1
            _bump(
                str(metadata_dict.get("reason"))
                if metadata_dict.get("reason")
                else None
            )
            continue
        if action == "parse_fallback":
            reason = metadata_dict.get("reason")
            count = metadata_dict.get("count")
            try:
                count_value = int(count)
            except (TypeError, ValueError):
                count_value = 1
            if reason:
                reason_counts[str(reason)] = (
                    reason_counts.get(str(reason), 0) + count_value
                )
            continue
        if action in {"timing_skipped", "unmapped_begin_trigger_target"}:
            _bump(action)
            reason = metadata_dict.get("reason")
            if reason:
                _bump(str(reason))

    if emitted or skipped or reason_counts:
        result.animation_emitted_count = emitted
        result.animation_skipped_count = skipped
        result.animation_reason_counts = dict(
            sorted(reason_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        )
