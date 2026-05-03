from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
from tools.visual.corpus_audit import (
    AuditResult,
    _apply_trace_metrics,
    _artifact_subdir,
    _classify_corpus,
    _default_output_dir,
    _known_audit_outcome_for_svg,
    _stale_w3c_reference_reason,
    _svg_has_animation,
    _w3c_reference_png_for_svg,
    audit_svg,
    audit_svgs,
    build_run_metadata,
    build_summary,
    discover_svg_paths,
    render_markdown_summary,
    resolve_audit_inputs,
    score_audit_result,
    write_audit_report,
)


def test_discover_svg_paths_skips_generated_dirs(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "keep.svg").write_text("<svg/>", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "also_keep.svg").write_text("<svg/>", encoding="utf-8")
    skipped_output = root / "output"
    skipped_output.mkdir()
    (skipped_output / "skip.svg").write_text("<svg/>", encoding="utf-8")
    skipped_baselines = root / "baselines"
    skipped_baselines.mkdir()
    (skipped_baselines / "skip.svg").write_text("<svg/>", encoding="utf-8")

    discovered = discover_svg_paths([root])

    assert discovered == [
        root / "keep.svg",
        nested / "also_keep.svg",
    ]


def test_resolve_audit_inputs_adds_named_corpus_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "resvg-test-suite" / "tests"
    checkout.mkdir(parents=True)
    (checkout / "sample.svg").write_text("<svg/>", encoding="utf-8")

    resolved = resolve_audit_inputs(
        named_corpora=["resvg-test-suite"],
        corpus_root=tmp_path,
    )

    assert resolved == [checkout]


def test_resolve_audit_inputs_adds_local_w3c_corpus(tmp_path: Path) -> None:
    resolved = resolve_audit_inputs(
        named_corpora=["w3c"],
        corpus_root=tmp_path,
    )

    assert resolved == [Path("tests/svg")]


def test_artifact_subdir_keeps_external_paths_unique(tmp_path: Path) -> None:
    external_svg = tmp_path / "resvg-test-suite" / "tests" / "shapes" / "sample.svg"
    external_svg.parent.mkdir(parents=True)
    external_svg.write_text("<svg/>", encoding="utf-8")

    artifact_subdir = _artifact_subdir(external_svg)

    assert artifact_subdir.as_posix().endswith(
        "_external/" + "/".join(external_svg.resolve().parts[1:-1]) + "/sample"
    )


def test_classify_corpus_names_known_inputs() -> None:
    assert _classify_corpus(Path("tests/corpus/w3c/sample.svg")) == "w3c"
    assert _classify_corpus(Path("tests/svg/sample.svg")) == "w3c"
    assert (
        _classify_corpus(Path("tests/visual/fixtures/resvg/sample.svg"))
        == "visual-fixtures"
    )
    assert (
        _classify_corpus(Path("/tmp/resvg-test-suite/tests/sample.svg"))
        == "resvg-test-suite"
    )
    assert _classify_corpus(Path("external/sample.svg")) == "external"


def test_known_audit_outcome_marks_transform_torture_accepted_limitation() -> None:
    outcome = _known_audit_outcome_for_svg(
        Path("tests/visual/fixtures/resvg/transform_torture.svg")
    )

    assert outcome is not None
    assert outcome[0] == "accepted-limitation"
    assert "<g> child inside <clipPath>" in outcome[1]
    assert (
        _known_audit_outcome_for_svg(
            Path.cwd() / "tests/visual/fixtures/resvg/transform_torture.svg"
        )
        == outcome
    )


def test_known_audit_outcome_marks_scripted_text_dom_accepted_limitation() -> None:
    outcome = _known_audit_outcome_for_svg(Path("tests/svg/text-dom-01-f.svg"))

    assert outcome is not None
    assert outcome[0] == "accepted-limitation"
    assert "onload JavaScript" in outcome[1]
    assert "ignores SVG scripts" in outcome[1]


def test_known_audit_outcome_marks_native_text_renderer_rows_deferred() -> None:
    intro_02 = _known_audit_outcome_for_svg(Path("tests/svg/text-intro-02-b.svg"))
    intro_09 = _known_audit_outcome_for_svg(Path("tests/svg/text-intro-09-b.svg"))
    tspan_02 = _known_audit_outcome_for_svg(Path("tests/svg/text-tspan-02-b.svg"))
    text_text_07 = _known_audit_outcome_for_svg(Path("tests/svg/text-text-07-t.svg"))
    text_text_09 = _known_audit_outcome_for_svg(Path("tests/svg/text-text-09-t.svg"))

    assert intro_02 is not None
    assert intro_02[0] == "deferred"
    assert "bidi text content and placement" in intro_02[1]
    assert intro_09 is not None
    assert intro_09[0] == "deferred"
    assert "webfont-backed bidi text content" in intro_09[1]
    assert tspan_02 is not None
    assert tspan_02[0] == "deferred"
    assert "rotated tspan text content" in tspan_02[1]
    assert text_text_07 is not None
    assert text_text_07[0] == "deferred"
    assert "per-glyph x/y/rotate text placement" in text_text_07[1]
    assert text_text_09 is not None
    assert text_text_09[0] == "deferred"
    assert "shortened per-glyph x/y/rotate text placement" in text_text_09[1]


def test_known_audit_outcome_marks_convolution_bitmap_fallback_accepted() -> None:
    outcome = _known_audit_outcome_for_svg(Path("tests/svg/filters-conv-05-f.svg"))

    assert outcome is not None
    assert outcome[0] == "accepted-limitation"
    assert "feConvolveMatrix edgeMode" in outcome[1]
    assert "passes" in outcome[1]


def test_known_audit_outcome_marks_filter_overview_rows_deferred() -> None:
    overview_02 = _known_audit_outcome_for_svg(
        Path("tests/svg/filters-overview-02-b.svg")
    )
    overview_03 = _known_audit_outcome_for_svg(
        Path("tests/svg/filters-overview-03-b.svg")
    )

    assert overview_02 is not None
    assert overview_02[0] == "deferred"
    assert "filter input-source parity" in overview_02[1]
    assert overview_03 is not None
    assert overview_03[0] == "deferred"
    assert "BackgroundImage/BackgroundAlpha" in overview_03[1]


def test_w3c_reference_png_resolves_sibling_png_oracle(tmp_path: Path) -> None:
    svg_path = tmp_path / "svg" / "sample.svg"
    png_path = tmp_path / "png" / "sample.png"
    svg_path.parent.mkdir()
    png_path.parent.mkdir()
    svg_path.write_text("<svg/>", encoding="utf-8")
    png_path.write_bytes(b"png")

    assert _w3c_reference_png_for_svg(svg_path) == png_path
    assert _w3c_reference_png_for_svg(tmp_path / "other" / "sample.svg") is None


def test_w3c_reference_png_skips_known_stale_oracle(tmp_path: Path) -> None:
    svg_path = tmp_path / "svg" / "text-tspan-02-b.svg"
    png_path = tmp_path / "png" / "text-tspan-02-b.png"
    svg_path.parent.mkdir()
    png_path.parent.mkdir()
    svg_path.write_text("<svg/>", encoding="utf-8")
    png_path.write_bytes(b"stale")

    assert _w3c_reference_png_for_svg(svg_path) is None
    assert "Revision: 1.10" in (_stale_w3c_reference_reason(svg_path) or "")

    filter_svg_path = tmp_path / "svg" / "filters-overview-03-b.svg"
    filter_png_path = tmp_path / "png" / "filters-overview-03-b.png"
    filter_svg_path.write_text("<svg/>", encoding="utf-8")
    filter_png_path.write_bytes(b"stale")

    assert _w3c_reference_png_for_svg(filter_svg_path) is None
    assert "Revision: 1.1" in (_stale_w3c_reference_reason(filter_svg_path) or "")

    conv_svg_path = tmp_path / "svg" / "filters-conv-05-f.svg"
    conv_png_path = tmp_path / "png" / "filters-conv-05-f.png"
    conv_svg_path.write_text("<svg/>", encoding="utf-8")
    conv_png_path.write_bytes(b"stale")

    assert _w3c_reference_png_for_svg(conv_svg_path) is None
    assert "Revision: 1.1" in (_stale_w3c_reference_reason(conv_svg_path) or "")

    intro_svg_path = tmp_path / "svg" / "text-intro-02-b.svg"
    intro_png_path = tmp_path / "png" / "text-intro-02-b.png"
    intro_svg_path.write_text("<svg/>", encoding="utf-8")
    intro_png_path.write_bytes(b"stale")

    assert _w3c_reference_png_for_svg(intro_svg_path) is None
    assert "Revision: 1.2" in (_stale_w3c_reference_reason(intro_svg_path) or "")

    intro_webfont_svg_path = tmp_path / "svg" / "text-intro-09-b.svg"
    intro_webfont_png_path = tmp_path / "png" / "text-intro-09-b.png"
    intro_webfont_svg_path.write_text("<svg/>", encoding="utf-8")
    intro_webfont_png_path.write_bytes(b"stale")

    assert _w3c_reference_png_for_svg(intro_webfont_svg_path) is None
    assert "Revision: 1.3" in (
        _stale_w3c_reference_reason(intro_webfont_svg_path) or ""
    )

    text_text_07_svg_path = tmp_path / "svg" / "text-text-07-t.svg"
    text_text_07_png_path = tmp_path / "png" / "text-text-07-t.png"
    text_text_07_svg_path.write_text("<svg/>", encoding="utf-8")
    text_text_07_png_path.write_bytes(b"stale")

    assert _w3c_reference_png_for_svg(text_text_07_svg_path) is None
    assert "Revision: 1.4" in (_stale_w3c_reference_reason(text_text_07_svg_path) or "")

    text_text_09_svg_path = tmp_path / "svg" / "text-text-09-t.svg"
    text_text_09_png_path = tmp_path / "png" / "text-text-09-t.png"
    text_text_09_svg_path.write_text("<svg/>", encoding="utf-8")
    text_text_09_png_path.write_bytes(b"stale")

    assert _w3c_reference_png_for_svg(text_text_09_svg_path) is None
    assert "Revision: 1.5" in (_stale_w3c_reference_reason(text_text_09_svg_path) or "")


def test_audit_svg_uses_w3c_png_reference_when_browser_is_skipped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    svg_path = tmp_path / "svg" / "sample.svg"
    png_path = tmp_path / "png" / "sample.png"
    svg_path.parent.mkdir()
    png_path.parent.mkdir()
    svg_path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    Image.new("RGB", (4, 4), "white").save(png_path)

    class StubBuilder:
        def build_from_svg(self, _svg_text, output_path, **_kwargs):
            output_path.write_bytes(b"pptx")

    class StubRenderer:
        def render(self, _pptx_path, output_dir):
            render_path = Path(output_dir) / "presentation.png"
            Image.new("RGB", (4, 4), "white").save(render_path)
            return type("Rendered", (), {"images": (render_path,)})()

    class BrowserShouldNotRun:
        def render_svg(self, *_args, **_kwargs):
            raise AssertionError("browser renderer should not run")

    class StubDiffer:
        def compare(self, _baseline, _actual, *, generate_diff):
            assert generate_diff is True
            return type(
                "Comparison",
                (),
                {
                    "ssim_score": 1.0,
                    "pixel_diff_percentage": 0.0,
                    "passed": True,
                    "diff_image": None,
                },
            )()

    class StubStructure:
        source_count = 0
        target_count = 0
        count_delta = 0

        def rasterized_pairs(self):
            return ()

        def top_bbox_mismatches(self, *, limit):
            return ()

    monkeypatch.setattr(
        "tools.visual.corpus_audit.compare_substructures",
        lambda *_args, **_kwargs: StubStructure(),
    )

    result = audit_svg(
        svg_path,
        output_dir=tmp_path / "audit",
        builder=StubBuilder(),
        renderer=StubRenderer(),
        render_available=True,
        browser_renderer=BrowserShouldNotRun(),
        browser_available=False,
        differ=StubDiffer(),
        skip_render=False,
        skip_browser=True,
        check_animation=False,
        animation_duration=4.0,
        animation_fps=4.0,
    )

    reference = Path(result.artifact_dir) / "browser" / "reference.png"
    assert result.browser_status == "ok"
    assert result.diff_status == "ok"
    assert reference.read_bytes() == png_path.read_bytes()


def test_audit_svg_uses_browser_reference_when_w3c_png_is_known_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    svg_path = tmp_path / "svg" / "text-tspan-02-b.svg"
    png_path = tmp_path / "png" / "text-tspan-02-b.png"
    svg_path.parent.mkdir()
    png_path.parent.mkdir()
    svg_path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    Image.new("RGB", (4, 4), "red").save(png_path)

    class StubBuilder:
        def build_from_svg(self, _svg_text, output_path, **_kwargs):
            output_path.write_bytes(b"pptx")

    class StubRenderer:
        def render(self, _pptx_path, output_dir):
            render_path = Path(output_dir) / "presentation.png"
            Image.new("RGB", (4, 4), "white").save(render_path)
            return type("Rendered", (), {"images": (render_path,)})()

    class StubBrowser:
        available = True

        def render_svg(self, _svg_text, output_path, **_kwargs):
            Image.new("RGB", (4, 4), "white").save(output_path)

    class StubDiffer:
        def compare(self, baseline, actual, *, generate_diff):
            assert generate_diff is True
            assert baseline.getpixel((0, 0)) == (255, 255, 255)
            assert actual.getpixel((0, 0)) == (255, 255, 255)
            return type(
                "Comparison",
                (),
                {
                    "ssim_score": 1.0,
                    "pixel_diff_percentage": 0.0,
                    "passed": True,
                    "diff_image": None,
                },
            )()

    class StubStructure:
        source_count = 0
        target_count = 0
        count_delta = 0

        def rasterized_pairs(self):
            return ()

        def top_bbox_mismatches(self, *, limit):
            return ()

    monkeypatch.setattr(
        "tools.visual.corpus_audit.compare_substructures",
        lambda *_args, **_kwargs: StubStructure(),
    )

    result = audit_svg(
        svg_path,
        output_dir=tmp_path / "audit",
        builder=StubBuilder(),
        renderer=StubRenderer(),
        render_available=True,
        browser_renderer=StubBrowser(),
        browser_available=True,
        differ=StubDiffer(),
        skip_render=False,
        skip_browser=False,
        check_animation=False,
        animation_duration=4.0,
        animation_fps=4.0,
    )

    reference = Path(result.artifact_dir) / "browser" / "reference.png"
    assert result.browser_status == "ok"
    assert result.diff_status == "ok"
    assert Image.open(reference).getpixel((0, 0)) == (255, 255, 255)
    assert any("W3C PNG reference skipped" in note for note in result.notes)


def test_audit_svg_suppresses_known_accepted_limitation_priority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    svg_path = Path("tests/visual/fixtures/resvg/transform_torture.svg")

    class StubBuilder:
        def build_from_svg(self, _svg_text, output_path, **_kwargs):
            output_path.write_bytes(b"pptx")

    class StubRenderer:
        def render(self, _pptx_path, output_dir):
            render_path = Path(output_dir) / "presentation.png"
            Image.new("RGB", (4, 4), "black").save(render_path)
            return type("Rendered", (), {"images": (render_path,)})()

    class StubBrowser:
        available = True

        def render_svg(self, _svg_text, output_path, **_kwargs):
            Image.new("RGB", (4, 4), "white").save(output_path)

    class StubDiffer:
        def compare(self, _baseline, _actual, *, generate_diff):
            assert generate_diff is True
            return type(
                "Comparison",
                (),
                {
                    "ssim_score": 0.5,
                    "pixel_diff_percentage": 40.0,
                    "passed": False,
                    "diff_image": None,
                },
            )()

    class StubBBoxMismatch:
        max_abs_delta = 100.0

    class StubStructure:
        source_count = 10
        target_count = 4
        count_delta = -6

        def rasterized_pairs(self):
            return (object(),)

        def top_bbox_mismatches(self, *, limit):
            assert limit == 1
            return (StubBBoxMismatch(),)

    monkeypatch.setattr(
        "tools.visual.corpus_audit.compare_substructures",
        lambda *_args, **_kwargs: StubStructure(),
    )

    result = audit_svg(
        svg_path,
        output_dir=tmp_path / "audit",
        builder=StubBuilder(),
        renderer=StubRenderer(),
        render_available=True,
        browser_renderer=StubBrowser(),
        browser_available=True,
        differ=StubDiffer(),
        skip_render=False,
        skip_browser=False,
        check_animation=False,
        animation_duration=4.0,
        animation_fps=4.0,
    )

    assert result.triage_outcome == "accepted-limitation"
    assert result.raw_score is not None and result.raw_score > 0
    assert result.score == 0.0
    assert any("ADR-037 outcome accepted-limitation" in note for note in result.notes)


def test_default_output_dir_uses_powerpoint_reports_subtree() -> None:
    assert _default_output_dir("powerpoint") == Path("reports/visual/powerpoint/audit")
    assert _default_output_dir("soffice") == Path("reports/visual/audit")


def test_score_audit_result_prioritizes_build_failures() -> None:
    build_failure = AuditResult(
        svg_path="broken.svg",
        artifact_dir="out/broken",
        build_status="error",
    )
    visual_mismatch = AuditResult(
        svg_path="mismatch.svg",
        artifact_dir="out/mismatch",
        build_status="ok",
        render_status="ok",
        browser_status="ok",
        diff_status="mismatch",
        ssim_score=0.91,
        pixel_diff_percentage=6.5,
        rasterized_count=1,
        max_bbox_delta=2.0,
    )

    assert score_audit_result(build_failure) > score_audit_result(visual_mismatch)


def test_score_audit_result_includes_animation_mismatch_penalty() -> None:
    static_only = AuditResult(
        svg_path="static.svg",
        artifact_dir="out/static",
        build_status="ok",
        render_status="ok",
        browser_status="ok",
        diff_status="ok",
        score=0.0,
    )
    animated_mismatch = AuditResult(
        svg_path="animated.svg",
        artifact_dir="out/animated",
        build_status="ok",
        render_status="ok",
        browser_status="ok",
        diff_status="ok",
        animation_status="mismatch",
        animation_min_ssim=0.62,
        animation_max_pixel_diff_percentage=18.5,
    )

    assert score_audit_result(animated_mismatch) > score_audit_result(static_only)


def test_score_audit_result_suppresses_structural_penalty_for_filter_rasters() -> None:
    base = AuditResult(
        svg_path="filters.svg",
        artifact_dir="out/filters",
        build_status="ok",
        render_status="ok",
        browser_status="ok",
        diff_status="mismatch",
        ssim_score=0.75,
        pixel_diff_percentage=20.0,
        rasterized_count=6,
        count_delta=-12,
        max_bbox_delta=462.0,
    )
    suppressed = AuditResult(
        svg_path="filters.svg",
        artifact_dir="out/filters",
        build_status="ok",
        render_status="ok",
        browser_status="ok",
        diff_status="mismatch",
        ssim_score=0.75,
        pixel_diff_percentage=20.0,
        rasterized_count=6,
        count_delta=-12,
        max_bbox_delta=462.0,
        structure_penalty_suppressed=True,
    )

    assert score_audit_result(suppressed) == score_audit_result(base) - 1164.0


def test_render_markdown_summary_lists_highest_score_first() -> None:
    low = AuditResult(
        svg_path="low.svg",
        artifact_dir="out/low",
        build_status="ok",
        render_status="ok",
        browser_status="ok",
        diff_status="ok",
        score=10.0,
    )
    high = AuditResult(
        svg_path="high.svg",
        artifact_dir="out/high",
        build_status="ok",
        render_status="error",
        browser_status="ok",
        diff_status="skipped",
        score=250.0,
        notes=["PPTX render failed."],
    )

    markdown = render_markdown_summary([high, low], top_n=1)
    summary = build_summary([high, low])

    assert "high.svg" in markdown
    assert "low.svg" not in markdown
    assert summary["render_errors"] == 1


def test_render_markdown_summary_includes_animation_columns() -> None:
    item = AuditResult(
        svg_path="animated.svg",
        artifact_dir="out/animated",
        build_status="ok",
        render_status="ok",
        browser_status="ok",
        diff_status="ok",
        animation_status="mismatch",
        animation_min_ssim=0.8123,
        animation_emitted_count=3,
        animation_skipped_count=1,
        animation_reason_counts={"unsupported_begin_target_missing": 1},
        score=42.0,
    )

    markdown = render_markdown_summary([item], top_n=1)
    summary = build_summary([item])

    assert "| Anim |" in markdown
    assert "| Reason | Count |" in markdown
    assert "unsupported_begin_target_missing" in markdown
    assert "3/1" in markdown
    assert "0.8123" in markdown
    assert summary["animation_mismatches"] == 1
    assert summary["animation_fragments_emitted"] == 3
    assert summary["animation_fragments_skipped"] == 1
    assert summary["animation_reason_totals"] == {"unsupported_begin_target_missing": 1}


def test_build_summary_aggregates_animation_reason_totals() -> None:
    first = AuditResult(
        svg_path="one.svg",
        artifact_dir="out/one",
        animation_reason_counts={
            "unsupported_begin_target_missing": 2,
            "timing_skipped": 1,
        },
        animation_emitted_count=4,
        animation_skipped_count=2,
    )
    second = AuditResult(
        svg_path="two.svg",
        artifact_dir="out/two",
        animation_reason_counts={
            "unsupported_begin_target_missing": 1,
            "begin_expression_invalid": 3,
        },
        animation_emitted_count=1,
        animation_skipped_count=1,
    )

    summary = build_summary([first, second])

    assert summary["animation_fragments_emitted"] == 5
    assert summary["animation_fragments_skipped"] == 3
    assert summary["animation_reason_totals"] == {
        "begin_expression_invalid": 3,
        "unsupported_begin_target_missing": 3,
        "timing_skipped": 1,
    }


def test_apply_trace_metrics_collects_converter_report_fields() -> None:
    item = AuditResult(svg_path="sample.svg", artifact_dir="out/sample")
    trace_report = {
        "geometry_totals": {"emf": 2, "bad": "ignored", 4: 1},
        "paint_totals": {"bitmap": 1},
        "stage_totals": {"filter:resvg_attempt": 2},
        "resvg_metrics": {"attempts": 2, "successes": 1},
        "geometry_events": [
            {
                "decision": "emf",
                "metadata": {
                    "fallback_assets": [
                        {"type": "emf"},
                        {"type": "bitmap"},
                        {"no_type": True},
                    ],
                    "fallback_reason": "complex_path",
                },
            }
        ],
        "paint_events": [
            {
                "decision": "bitmap",
                "metadata": {
                    "fallback_assets": [{"type": "bitmap"}],
                    "fallback": "bitmap",
                },
            }
        ],
        "stage_events": [
            {
                "stage": "animation",
                "action": "fragment_emitted",
                "metadata": {},
            },
            {
                "stage": "animation",
                "action": "fragment_skipped",
                "metadata": {"reason": "unsupported_begin_target_missing"},
            },
            {
                "stage": "filter",
                "action": "descriptor_fallback",
                "metadata": {},
            },
        ],
    }

    _apply_trace_metrics(item, trace_report)

    assert item.geometry_totals == {"emf": 2}
    assert item.paint_totals == {"bitmap": 1}
    assert item.stage_totals == {"filter:resvg_attempt": 2}
    assert item.resvg_metrics == {"attempts": 2, "successes": 1}
    assert item.fallback_asset_counts == {"bitmap": 2, "emf": 1, "unknown": 1}
    assert item.fallback_reason_counts == {
        "action:descriptor_fallback": 1,
        "complex_path": 1,
        "fallback:bitmap": 1,
    }
    assert item.animation_emitted_count == 1
    assert item.animation_skipped_count == 1
    assert item.animation_reason_counts == {"unsupported_begin_target_missing": 1}


def test_build_summary_aggregates_trace_and_report_coverage() -> None:
    first = AuditResult(
        svg_path="one.svg",
        artifact_dir="out/one",
        corpus_name="w3c",
        fidelity_tier="direct",
        geometry_totals={"emf": 2},
        paint_totals={"bitmap": 1},
        stage_totals={"filter:resvg_attempt": 1},
        resvg_metrics={"attempts": 1},
        fallback_asset_counts={"emf": 1},
        fallback_reason_counts={"complex_path": 1},
    )
    second = AuditResult(
        svg_path="two.svg",
        artifact_dir="out/two",
        corpus_name="w3c",
        fidelity_tier="bitmap",
        geometry_totals={"bitmap": 1},
        fallback_asset_counts={"bitmap": 2},
        fallback_reason_counts={"complex_path": 1, "fallback:bitmap": 1},
    )

    summary = build_summary([first, second])

    assert summary["by_corpus"] == {"w3c": 2}
    assert summary["by_fidelity_tier"] == {"bitmap": 1, "direct": 1}
    assert summary["geometry_totals"] == {"emf": 2, "bitmap": 1}
    assert summary["paint_totals"] == {"bitmap": 1}
    assert summary["stage_totals"] == {"filter:resvg_attempt": 1}
    assert summary["resvg_metrics"] == {"attempts": 1}
    assert summary["fallback_asset_totals"] == {"bitmap": 2, "emf": 1}
    assert summary["fallback_reason_totals"] == {
        "complex_path": 2,
        "fallback:bitmap": 1,
    }


def test_write_audit_report_includes_run_metadata_and_trace_sections(
    tmp_path: Path,
) -> None:
    result = AuditResult(
        svg_path="tests/corpus/w3c/sample.svg",
        artifact_dir="out/sample",
        corpus_name="w3c",
        fidelity_tier="emf",
        build_status="ok",
        render_status="ok",
        browser_status="ok",
        diff_status="mismatch",
        fallback_asset_counts={"emf": 1},
        fallback_reason_counts={"complex_path": 1},
        geometry_totals={"emf": 1},
        resvg_metrics={"attempts": 1},
        score=10.0,
    )
    metadata = build_run_metadata(
        command=["python", "-m", "tools.visual.corpus_audit"],
        inputs=[Path("tests/corpus/w3c")],
        output_dir=tmp_path,
        renderer="powerpoint",
        browser_threshold=0.9,
        skip_render=False,
        skip_browser=False,
        check_animation=False,
        animation_duration=4.0,
        animation_fps=4.0,
        fidelity_tier="emf",
        powerpoint_backend="auto",
    )

    json_path, markdown_path = write_audit_report(
        [result],
        tmp_path,
        top_n=1,
        run_metadata=metadata,
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert payload["metadata"]["renderer"] == "powerpoint"
    assert payload["metadata"]["fidelity_tier"] == "emf"
    assert payload["summary"]["fallback_asset_totals"] == {"emf": 1}
    assert "## Run Metadata" in markdown
    assert "## Fallback Asset Totals" in markdown
    assert "## Fallback Reason Codes" in markdown
    assert "tests/corpus/w3c/sample.svg" in markdown


def test_svg_has_animation_detects_smil_tags() -> None:
    assert _svg_has_animation(
        "<svg xmlns='http://www.w3.org/2000/svg'><rect><animate attributeName='x' from='0' to='10' dur='1s'/></rect></svg>"
    )
    assert not _svg_has_animation(
        "<svg xmlns='http://www.w3.org/2000/svg'><rect x='0' y='0' width='10' height='10'/></svg>"
    )


def test_svg_has_animation_recovers_malformed_header() -> None:
    assert _svg_has_animation(
        """<svg xmlns='http://www.w3.org/2000/svg' width='1000' height='1000'>
<
<path d='M0,0 L10,10'>
  <animateMotion dur='1s' path='M0,0 L10,10'/>
</path>
</svg>"""
    )


def test_audit_svgs_passes_fidelity_tier_to_builder(
    monkeypatch, tmp_path: Path
) -> None:
    svg_path = tmp_path / "sample.svg"
    svg_path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

    recorded: dict[str, object] = {}

    class StubBuilder:
        def __init__(self, **kwargs) -> None:
            recorded.update(kwargs)

    def fake_audit_svg(*args, **kwargs):
        recorded["audit_fidelity_tier"] = kwargs["fidelity_tier"]
        return AuditResult(
            svg_path=svg_path.as_posix(),
            artifact_dir=(tmp_path / "out").as_posix(),
            build_status="ok",
            render_status="skipped",
            browser_status="skipped",
            diff_status="skipped",
        )

    monkeypatch.setattr("tools.visual.corpus_audit.PptxBuilder", StubBuilder)
    monkeypatch.setattr("tools.visual.corpus_audit.audit_svg", fake_audit_svg)

    results = audit_svgs(
        [svg_path],
        output_dir=tmp_path / "audit",
        skip_render=True,
        skip_browser=True,
        fidelity_tier="bitmap",
    )

    assert len(results) == 1
    assert recorded["fidelity_tier"] == "bitmap"
    assert recorded["audit_fidelity_tier"] == "bitmap"
