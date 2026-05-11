"""Plumbing tests for the flipbook fallback path.

Covers the wiring built in ADR-038-adjacent work: policy routing of dead
paths to FLIPBOOK, IR pre-pass scene mutation, writer-side flipbook par
emission, bldLst injection of frame shape entries, and config-time
failure when no renderer is provided.

These are plumbing tests — a `MockFlipbookRenderer` returns N copies of
the input IR element. No actual interpolation or rendering happens; the
goal is to prove the seams hold so a real renderer slots in later
without further structural changes.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from svg2ooxml.drawingml.animation.flipbook import (
    FlipbookConfigError,
    FlipbookPipeline,
    FlipbookPipelineError,
    FlipbookRenderer,
    assert_flipbook_renderer_present,
)
from svg2ooxml.drawingml.animation.policy import AnimationAction, AnimationPolicy
from svg2ooxml.drawingml.animation.writer import DrawingMLAnimationWriter
from svg2ooxml.drawingml.xml_builder import NS_P
from svg2ooxml.ir.animation import (
    AnimationDefinition,
    AnimationTiming,
    AnimationType,
)
from svg2ooxml.ir.geometry import Rect
from svg2ooxml.ir.scene import Group, IRElement, Scene
from svg2ooxml.ir.shapes import Rectangle


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


def _stroke_width_anim(
    *, element_id: str = "shape1", animation_id: str = "anim-001"
) -> AnimationDefinition:
    return AnimationDefinition(
        element_id=element_id,
        animation_type=AnimationType.ANIMATE,
        target_attribute="stroke-width",
        values=["1", "10"],
        timing=AnimationTiming(begin=0.0, duration=2.0),
        animation_id=animation_id,
    )


def _opacity_anim(*, element_id: str = "shape1") -> AnimationDefinition:
    return AnimationDefinition(
        element_id=element_id,
        animation_type=AnimationType.ANIMATE,
        target_attribute="opacity",
        values=["0", "1"],
        timing=AnimationTiming(begin=0.0, duration=1.0),
    )


def _rect(element_id: str) -> Rectangle:
    return Rectangle(
        bounds=Rect(x=0, y=0, width=100, height=50), element_id=element_id
    )


class MockFlipbookRenderer:
    """Returns N copies of the input IR element with synthetic element_ids."""

    def __init__(self) -> None:
        self.calls: list[tuple[IRElement, AnimationDefinition, int]] = []

    def render_frames(
        self,
        element: IRElement,
        animation: AnimationDefinition,
        n_frames: int,
    ) -> list[IRElement]:
        self.calls.append((element, animation, n_frames))
        base_id = getattr(element, "element_id", "frame")
        return [
            replace(element, element_id=f"{base_id}__f{i}") for i in range(n_frames)
        ]


# ------------------------------------------------------------------ #
# Policy routing                                                       #
# ------------------------------------------------------------------ #


class TestPolicyRouting:
    def test_native_mode_dead_path_routes_to_skip(self) -> None:
        policy = AnimationPolicy({"fallback_mode": "native"})
        action, reason = policy.decide_action(_stroke_width_anim(), 0.0)
        assert action == AnimationAction.SKIP
        assert reason == "dead_path_stroke_weight"

    def test_flipbook_mode_dead_path_routes_to_flipbook(self) -> None:
        policy = AnimationPolicy({"fallback_mode": "flipbook"})
        action, reason = policy.decide_action(_stroke_width_anim(), 0.0)
        assert action == AnimationAction.FLIPBOOK
        assert reason == "dead_path_stroke_weight"

    def test_flipbook_mode_normal_animation_routes_to_emit(self) -> None:
        policy = AnimationPolicy({"fallback_mode": "flipbook"})
        action, reason = policy.decide_action(_opacity_anim(), 0.0)
        assert action == AnimationAction.EMIT
        assert reason is None

    def test_raster_mode_still_blanket_skips(self) -> None:
        policy = AnimationPolicy({"fallback_mode": "raster"})
        action, reason = policy.decide_action(_opacity_anim(), 0.0)
        assert action == AnimationAction.SKIP
        assert reason == "fallback_mode_not_native"


# ------------------------------------------------------------------ #
# IR pre-pass                                                          #
# ------------------------------------------------------------------ #


class TestIRPrePass:
    def test_splices_top_level_element(self) -> None:
        scene = Scene(elements=[_rect("r1")])
        renderer = MockFlipbookRenderer()
        pipeline = FlipbookPipeline(
            renderer, AnimationPolicy({"fallback_mode": "flipbook"}), n_frames=4
        )
        anim = _stroke_width_anim(element_id="r1", animation_id="a1")

        pipeline.process(scene, [anim])

        assert len(scene.elements) == 4
        assert [e.element_id for e in scene.elements] == [
            "r1__f0",
            "r1__f1",
            "r1__f2",
            "r1__f3",
        ]
        assert pipeline.frame_element_ids("a1") == [
            "r1__f0",
            "r1__f1",
            "r1__f2",
            "r1__f3",
        ]

    def test_calls_renderer_with_n_frames(self) -> None:
        renderer = MockFlipbookRenderer()
        scene = Scene(elements=[_rect("r1")])
        FlipbookPipeline(
            renderer,
            AnimationPolicy({"fallback_mode": "flipbook"}),
            n_frames=7,
        ).process(scene, [_stroke_width_anim(element_id="r1")])
        assert len(renderer.calls) == 1
        _, _, n_frames = renderer.calls[0]
        assert n_frames == 7

    def test_splices_nested_in_group(self) -> None:
        inner = _rect("inner")
        scene = Scene(elements=[Group(children=[inner])])
        pipeline = FlipbookPipeline(
            MockFlipbookRenderer(),
            AnimationPolicy({"fallback_mode": "flipbook"}),
            n_frames=3,
        )
        pipeline.process(scene, [_stroke_width_anim(element_id="inner")])

        group = scene.elements[0]
        assert isinstance(group, Group)
        assert len(group.children) == 3
        assert all(
            c.element_id == f"inner__f{i}" for i, c in enumerate(group.children)
        )

    def test_native_mode_no_op(self) -> None:
        scene = Scene(elements=[_rect("r1")])
        renderer = MockFlipbookRenderer()
        FlipbookPipeline(
            renderer, AnimationPolicy({"fallback_mode": "native"})
        ).process(scene, [_stroke_width_anim(element_id="r1")])
        assert len(scene.elements) == 1
        assert scene.elements[0].element_id == "r1"
        assert renderer.calls == []

    def test_renderer_returning_single_frame_raises(self) -> None:
        class OneFrame:
            def render_frames(self, element, animation, n_frames):
                return [replace(element, element_id="only")]

        scene = Scene(elements=[_rect("r1")])
        pipeline = FlipbookPipeline(
            OneFrame(),
            AnimationPolicy({"fallback_mode": "flipbook"}),
            n_frames=4,
        )
        with pytest.raises(FlipbookPipelineError, match="minimum is 2"):
            pipeline.process(scene, [_stroke_width_anim(element_id="r1")])

    def test_renderer_returning_frame_without_element_id_raises(self) -> None:
        class NoId:
            def render_frames(self, element, animation, n_frames):
                return [
                    replace(element, element_id=None),
                    replace(element, element_id="ok"),
                ]

        scene = Scene(elements=[_rect("r1")])
        pipeline = FlipbookPipeline(
            NoId(),
            AnimationPolicy({"fallback_mode": "flipbook"}),
            n_frames=2,
        )
        with pytest.raises(FlipbookPipelineError, match="without element_id"):
            pipeline.process(scene, [_stroke_width_anim(element_id="r1")])

    def test_unmapped_element_is_silently_skipped(self) -> None:
        # Pipeline should not crash when an animation targets an
        # element_id that is not present in the scene.
        scene = Scene(elements=[_rect("r1")])
        renderer = MockFlipbookRenderer()
        pipeline = FlipbookPipeline(
            renderer, AnimationPolicy({"fallback_mode": "flipbook"})
        )
        pipeline.process(scene, [_stroke_width_anim(element_id="ghost")])
        assert len(scene.elements) == 1
        assert renderer.calls == []


# ------------------------------------------------------------------ #
# Config-time check                                                    #
# ------------------------------------------------------------------ #


class TestConfigCheck:
    def test_native_mode_no_renderer_required(self) -> None:
        assert_flipbook_renderer_present({"fallback_mode": "native"}, None)

    def test_default_mode_no_renderer_required(self) -> None:
        assert_flipbook_renderer_present({}, None)

    def test_flipbook_mode_without_renderer_raises(self) -> None:
        with pytest.raises(FlipbookConfigError, match="requires a FlipbookRenderer"):
            assert_flipbook_renderer_present({"fallback_mode": "flipbook"}, None)

    def test_flipbook_mode_with_renderer_ok(self) -> None:
        assert_flipbook_renderer_present(
            {"fallback_mode": "flipbook"}, MockFlipbookRenderer()
        )


# ------------------------------------------------------------------ #
# Writer end-to-end                                                    #
# ------------------------------------------------------------------ #


class TestWriterFlipbookEmission:
    def test_flipbook_par_includes_frame_set_actions(self) -> None:
        """instantiate_flipbook is called and its <p:par> is in the output."""
        writer = DrawingMLAnimationWriter()
        anim = _stroke_width_anim(element_id="r1", animation_id="a1")
        # Simulate that the IR pre-pass spliced 3 frames and the writer
        # assigned them shape_ids 10, 11, 12.
        frame_shape_ids = {"a1": ["10", "11", "12"]}

        xml = writer.build(
            [anim],
            timeline=[],
            options={"fallback_mode": "flipbook"},
            animated_shape_ids=["10", "11", "12"],
            start_id=100,
            flipbook_frame_shape_ids=frame_shape_ids,
        )

        # Flipbook par produces <p:set> visibility toggles on each frame.
        # Every frame shape must appear as a spTgt in the timing XML.
        for shape_id in ("10", "11", "12"):
            assert f'spid="{shape_id}"' in xml, (
                f"frame shape {shape_id} not referenced in timing XML:\n{xml}"
            )

    def test_flipbook_bld_entries_carry_matching_grpid(self) -> None:
        """Each frame shape must get a <p:bldP> entry with grpId=par_id.

        Per the oracle README: mismatched grpId causes PPT to silently
        ignore the visibility sets. The grpId is the par_id allocated
        for this animation by the id_allocator (writer assigns it).
        """
        writer = DrawingMLAnimationWriter()
        anim = _stroke_width_anim(element_id="r1", animation_id="a1")
        xml = writer.build(
            [anim],
            timeline=[],
            options={"fallback_mode": "flipbook"},
            animated_shape_ids=["10", "11"],
            start_id=100,
            flipbook_frame_shape_ids={"a1": ["10", "11"]},
        )

        # bldLst should have a bldP entry per frame, with grpId matching
        # the par_id (allocated by the writer from start_id). Writer emits
        # p:/a: prefixed XML without namespace declarations (per
        # to_string contract); wrap in a root that declares them.
        from lxml import etree

        wrapped = f'<root xmlns:p="{NS_P}">{xml}</root>'
        root = etree.fromstring(wrapped.encode("utf-8"))
        ns = {"p": NS_P}
        # Collect every bldP with a frame-shape spid:
        frame_blds = [
            bld
            for bld in root.findall(".//p:bldLst/p:bldP", ns)
            if bld.get("spid") in {"10", "11"}
        ]
        # The pre-existing animated_shape_ids loop already emits a
        # grpId="0" entry per shape. The flipbook injection adds another
        # entry per frame shape with the par's grpId. We assert the
        # latter exists.
        flipbook_blds = [bld for bld in frame_blds if bld.get("grpId") != "0"]
        assert flipbook_blds, (
            f"no flipbook bldP entries found with non-zero grpId in:\n{xml}"
        )
        # All flipbook entries should share the same grpId (= par_id).
        grp_ids = {bld.get("grpId") for bld in flipbook_blds}
        assert len(grp_ids) == 1, f"flipbook grpIds inconsistent: {grp_ids}"

    def test_flipbook_without_frame_map_skips_animation(self) -> None:
        """If FLIPBOOK is requested but no frame shape IDs were provided
        (i.e. the FlipbookPipeline didn't run upstream), the animation
        is dropped with a clear reason rather than emitting broken XML.
        """
        writer = DrawingMLAnimationWriter()
        anim = _stroke_width_anim(element_id="r1", animation_id="a1")
        xml = writer.build(
            [anim],
            timeline=[],
            options={"fallback_mode": "flipbook"},
            animated_shape_ids=[],
            start_id=100,
            flipbook_frame_shape_ids=None,
        )
        # Empty XML output — animation was skipped, no native fragments
        # were built.
        assert xml == ""

    def test_native_mode_dead_path_still_skipped(self) -> None:
        """Default behavior preserved: in native mode, stroke-width
        animations skip silently."""
        writer = DrawingMLAnimationWriter()
        anim = _stroke_width_anim(element_id="r1", animation_id="a1")
        xml = writer.build(
            [anim],
            timeline=[],
            options={"fallback_mode": "native"},
            animated_shape_ids=[],
            start_id=100,
        )
        assert xml == ""


# ------------------------------------------------------------------ #
# End-to-end through AnimationPipeline                                 #
# ------------------------------------------------------------------ #


class TestAnimationPipelineIntegration:
    """Exercise the full path: scene + payload → pre-pass → IR-walk-sim
    → AnimationPipeline.build() → flipbook XML."""

    def _make_pipeline_with_scene(self, n_frames: int = 3):
        """Set up an AnimationPipeline against a single-rect scene with
        a stroke-width animation routed to flipbook. Returns
        (pipeline, scene, renderer)."""
        from svg2ooxml.drawingml.animation_pipeline import AnimationPipeline

        scene = [_rect("r1")]
        renderer = MockFlipbookRenderer()
        anim = _stroke_width_anim(element_id="r1", animation_id="a1")
        payload = {
            "definitions": [anim],
            "timeline": [],
            "policy": {"fallback_mode": "flipbook"},
            "flipbook_renderer": renderer,
            "flipbook_n_frames": n_frames,
        }
        pipeline = AnimationPipeline()
        pipeline.reset(payload)
        return pipeline, scene, renderer

    def _simulate_ir_walk(self, pipeline, scene, start_shape_id: int = 10) -> int:
        """Mimic what the writer's IR walk does — assign sequential
        shape_ids to each top-level element and register the mapping."""
        next_id = start_shape_id
        for element in scene:
            element_id = getattr(element, "element_id", None)
            if isinstance(element_id, str):
                pipeline.register_element_ids([element_id], next_id)
            next_id += 1
        return next_id - 1  # max_shape_id

    def test_prepass_splices_frames_into_scene(self) -> None:
        pipeline, scene, renderer = self._make_pipeline_with_scene(n_frames=3)
        pipeline.run_flipbook_prepass(scene)
        assert len(scene) == 3
        assert [e.element_id for e in scene] == ["r1__f0", "r1__f1", "r1__f2"]
        assert len(renderer.calls) == 1

    def test_build_emits_flipbook_xml_end_to_end(self) -> None:
        pipeline, scene, _ = self._make_pipeline_with_scene(n_frames=3)
        pipeline.run_flipbook_prepass(scene)
        max_shape_id = self._simulate_ir_walk(pipeline, scene, start_shape_id=10)

        xml = pipeline.build(max_shape_id=max_shape_id)

        assert xml, "AnimationPipeline.build() returned empty for flipbook animation"
        # All frame shape_ids should appear as spTgt references in the
        # timing XML — proves the pipeline resolved frame element_ids →
        # shape_ids and passed them to instantiate_flipbook.
        for shape_id in ("10", "11", "12"):
            assert f'spid="{shape_id}"' in xml, (
                f"frame shape {shape_id} missing from XML:\n{xml}"
            )

    def test_build_skips_when_renderer_not_provided(self) -> None:
        """No renderer in payload + native fallback_mode = legacy skip."""
        from svg2ooxml.drawingml.animation_pipeline import AnimationPipeline

        scene = [_rect("r1")]
        anim = _stroke_width_anim(element_id="r1", animation_id="a1")
        pipeline = AnimationPipeline()
        pipeline.reset(
            {
                "definitions": [anim],
                "policy": {"fallback_mode": "native"},
            }
        )
        pipeline.run_flipbook_prepass(scene)
        # Scene unchanged in native mode
        assert len(scene) == 1
        max_shape_id = self._simulate_ir_walk(pipeline, scene)
        xml = pipeline.build(max_shape_id=max_shape_id)
        # Stroke-width dead path drops silently in native mode
        assert xml == ""

    def test_frames_have_own_metadata_element_ids(self) -> None:
        """Each spliced frame must carry its own metadata.element_ids
        pointing at its own element_id — not the shared original SVG id.

        The production IR walk registers shape_ids via
        ``register_mapping(metadata, shape_id)`` which reads
        ``metadata["element_ids"]``. Renderers commonly produce frames
        via ``dataclasses.replace(...)`` which shares the metadata dict;
        the pipeline must normalize this or every frame's
        register_mapping call would key on the same original id and
        ``_shape_map`` would only keep the last one.
        """
        from svg2ooxml.drawingml.animation_pipeline import AnimationPipeline

        scene = [_rect("r1")]
        # The original rect carries metadata["element_ids"] = ["r1"]:
        scene[0].metadata["element_ids"] = ["r1"]
        renderer = MockFlipbookRenderer()
        anim = _stroke_width_anim(element_id="r1", animation_id="a1")
        pipeline = AnimationPipeline()
        pipeline.reset(
            {
                "definitions": [anim],
                "policy": {"fallback_mode": "flipbook"},
                "flipbook_renderer": renderer,
                "flipbook_n_frames": 3,
            }
        )
        pipeline.run_flipbook_prepass(scene)

        # Each frame has its own metadata dict (no shared mutation):
        assert scene[0].metadata is not scene[1].metadata
        # And metadata["element_ids"] reflects each frame's own id:
        for frame, expected in zip(
            scene, ["r1__f0", "r1__f1", "r1__f2"], strict=True
        ):
            assert frame.metadata.get("element_ids") == [expected], (
                f"frame {expected} has wrong metadata.element_ids: "
                f"{frame.metadata.get('element_ids')!r}"
            )

    def test_real_register_mapping_path_resolves_frame_shape_ids(self) -> None:
        """Drive registration through the production code path that the
        real IR walk uses (``register_mapping(metadata, shape_id)``) and
        verify ``_shape_map`` ends up with distinct frame shape_ids.

        This is the test that would have caught the
        shared-metadata-dict regression. ``_simulate_ir_walk`` short-cuts
        via ``register_element_ids`` and does not exercise the metadata
        path.
        """
        from svg2ooxml.drawingml.animation_pipeline import AnimationPipeline

        scene = [_rect("r1")]
        scene[0].metadata["element_ids"] = ["r1"]
        renderer = MockFlipbookRenderer()
        anim = _stroke_width_anim(element_id="r1", animation_id="a1")
        pipeline = AnimationPipeline()
        pipeline.reset(
            {
                "definitions": [anim],
                "timeline": [],
                "policy": {"fallback_mode": "flipbook"},
                "flipbook_renderer": renderer,
                "flipbook_n_frames": 3,
            }
        )
        pipeline.run_flipbook_prepass(scene)

        # Mimic the writer's IR walk exactly: register each spliced
        # frame via metadata, not via .element_id.
        for i, frame in enumerate(scene):
            pipeline.register_mapping(frame.metadata, 20 + i)

        xml = pipeline.build(max_shape_id=22)
        assert xml, "build() returned empty — registration failed"
        for shape_id in ("20", "21", "22"):
            assert f'spid="{shape_id}"' in xml, (
                f"frame shape {shape_id} missing from XML; "
                f"metadata-keyed registration likely broken:\n{xml}"
            )

    def test_reset_raises_when_flipbook_mode_without_renderer(self) -> None:
        from svg2ooxml.drawingml.animation_pipeline import AnimationPipeline

        pipeline = AnimationPipeline()
        with pytest.raises(FlipbookConfigError, match="requires a FlipbookRenderer"):
            pipeline.reset(
                {
                    "definitions": [_stroke_width_anim()],
                    "policy": {"fallback_mode": "flipbook"},
                    # no flipbook_renderer key
                }
            )

    @pytest.mark.parametrize("bad_n_frames", [0, 1, -5, 65, 1000])
    def test_reset_raises_on_out_of_range_n_frames(self, bad_n_frames: int) -> None:
        from svg2ooxml.drawingml.animation_pipeline import AnimationPipeline

        pipeline = AnimationPipeline()
        with pytest.raises(FlipbookConfigError, match="flipbook_n_frames must be"):
            pipeline.reset(
                {
                    "definitions": [_stroke_width_anim()],
                    "policy": {"fallback_mode": "flipbook"},
                    "flipbook_renderer": MockFlipbookRenderer(),
                    "flipbook_n_frames": bad_n_frames,
                }
            )

    def test_reset_accepts_valid_n_frames_range(self) -> None:
        from svg2ooxml.drawingml.animation_pipeline import AnimationPipeline

        # Both boundary values should be accepted.
        for n in (2, 64):
            pipeline = AnimationPipeline()
            pipeline.reset(
                {
                    "definitions": [_stroke_width_anim()],
                    "policy": {"fallback_mode": "flipbook"},
                    "flipbook_renderer": MockFlipbookRenderer(),
                    "flipbook_n_frames": n,
                }
            )
