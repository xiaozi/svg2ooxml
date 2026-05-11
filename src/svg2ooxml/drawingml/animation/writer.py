"""DrawingML animation writer using handler architecture.

Orchestrates all animation handlers to convert SVG animations into
PowerPoint timing XML. Handlers may return either legacy ``<p:par>``
elements or typed :class:`NativeFragment` instances; the writer normalizes
both to a single planning surface before serializing once at the end.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from lxml import etree

from svg2ooxml.common.units import UnitConverter
from svg2ooxml.drawingml.xml_builder import to_string
from svg2ooxml.ir.animation import (
    AnimationDefinition,
    AnimationScene,
    AnimationType,
    TransformType,
)

from .handlers import (
    AnimationHandler,
    ColorAnimationHandler,
    MotionAnimationHandler,
    NumericAnimationHandler,
    OpacityAnimationHandler,
    SetAnimationHandler,
    TransformAnimationHandler,
)
from .id_allocator import TimingIDAllocator
from .motion_fragments import (
    merge_concurrent_simple_motion_fragments,
    renumber_generated_timing_ids,
)
from .native_fragment import NativeFragment
from .oracle import default_oracle
from .policy import AnimationAction, AnimationPolicy
from .tav_builder import TAVBuilder
from .value_processors import ValueProcessor
from .xml_builders import AnimationXMLBuilder

if TYPE_CHECKING:
    from svg2ooxml.core.tracing import ConversionTracer

__all__ = ["DrawingMLAnimationWriter"]

_logger = logging.getLogger(__name__)


class DrawingMLAnimationWriter:
    """Render animation definitions as DrawingML timing XML."""

    def __init__(self) -> None:
        self._unit_converter = UnitConverter()
        self._xml_builder = AnimationXMLBuilder()
        self._value_processor = ValueProcessor()
        self._tav_builder = TAVBuilder(self._xml_builder)
        self._id_allocator = TimingIDAllocator()
        self._policy: AnimationPolicy | None = None
        self._flipbook_frame_shape_ids: dict[str, list[str]] = {}

        # Handlers in priority order (most specific first, catch-all last)
        self._handlers: list[AnimationHandler] = [
            OpacityAnimationHandler(
                self._xml_builder, self._value_processor,
                self._tav_builder, self._unit_converter,
            ),
            ColorAnimationHandler(
                self._xml_builder, self._value_processor,
                self._tav_builder, self._unit_converter,
            ),
            SetAnimationHandler(
                self._xml_builder, self._value_processor,
                self._tav_builder, self._unit_converter,
            ),
            MotionAnimationHandler(
                self._xml_builder, self._value_processor,
                self._tav_builder, self._unit_converter,
            ),
            TransformAnimationHandler(
                self._xml_builder, self._value_processor,
                self._tav_builder, self._unit_converter,
            ),
            NumericAnimationHandler(
                self._xml_builder, self._value_processor,
                self._tav_builder, self._unit_converter,
            ),
        ]

    def build(
        self,
        animations: Sequence[AnimationDefinition],
        timeline: Sequence[AnimationScene],
        *,
        tracer: ConversionTracer | None = None,
        options: Mapping[str, Any] | None = None,
        animated_shape_ids: list[str] | None = None,
        start_id: int = 1,
        flipbook_frame_shape_ids: Mapping[str, list[str]] | None = None,
    ) -> str:
        """Build PowerPoint timing XML for a sequence of animations.

        ``flipbook_frame_shape_ids`` maps animation_id → list of resolved
        shape_ids for each pre-spliced flipbook frame. The caller must
        already have run :class:`FlipbookPipeline` against the IR scene
        and mapped frame element_ids through the writer's shape registry.
        """
        options = dict(options or {})
        self._policy = AnimationPolicy(options)
        self._flipbook_frame_shape_ids = dict(flipbook_frame_shape_ids or {})

        # Pre-allocate IDs for the complete timing tree, starting after shape IDs
        ids = self._id_allocator.allocate(n_animations=len(animations), start_id=start_id)

        native_fragments: list[NativeFragment] = []
        animation_element_records: list[tuple[NativeFragment, Any]] = []
        id_index = 0

        for animation in animations:
            anim_ids = ids.animations[id_index]
            id_index += 1

            fragment, meta = self._build_animation(
                animation, options, anim_ids.par, anim_ids.behavior
            )

            _logger.debug(
                "Animation fragment for %s (%s): %s",
                animation.element_id,
                animation.target_attribute,
                "SUCCESS" if fragment is not None else f"SKIPPED ({meta.get('reason') if meta else 'unknown'})",
            )

            if fragment is not None:
                native_fragments.append(fragment)
                animation_element_records.append((fragment, anim_ids))
                if tracer is not None:
                    emitted_metadata: dict[str, Any] = {
                        "element_id": animation.element_id,
                        "animation_type": (
                            animation.animation_type.value
                            if hasattr(animation.animation_type, "value")
                            else str(animation.animation_type)
                        ),
                        "attribute": animation.target_attribute,
                        "fallback_mode": options.get("fallback_mode", "native"),
                        "fragment_source": fragment.source,
                        "fragment_strategy": fragment.strategy,
                    }
                    tracer.record_stage_event(
                        stage="animation",
                        action="fragment_emitted",
                        metadata=emitted_metadata,
                    )
                    rotate_mode = str(getattr(animation, "motion_rotate", "")).strip().lower()
                    if rotate_mode in {"auto", "auto-reverse"}:
                        tracer.record_stage_event(
                            stage="animation",
                            action="fidelity_downgrade",
                            metadata={
                                "reason": "rotate_auto_approximated",
                                "element_id": animation.element_id,
                                "rotate_mode": rotate_mode,
                            },
                        )
            elif tracer is not None:
                metadata: dict[str, Any] = {
                    "element_id": animation.element_id,
                    "animation_type": (
                        animation.animation_type.value
                        if hasattr(animation.animation_type, "value")
                        else str(animation.animation_type)
                    ),
                    "attribute": animation.target_attribute,
                    "fallback_mode": options.get("fallback_mode", "native"),
                }
                if meta:
                    metadata.update(meta)
                tracer.record_stage_event(
                    stage="animation",
                    action="fragment_skipped",
                    metadata=metadata,
                )

        if not native_fragments:
            return ""

        # Check if timing should be globally suppressed by policy.
        # Per-fragment suppression is handled in should_skip().
        should_suppress = False
        if self._policy:
            should_suppress = self._policy.should_suppress_timing()

        if should_suppress:
            return ""

        reserved_ids = {
            ids.root,
            ids.main_seq,
            ids.click_group,
            *(
                timing_id
                for anim_ids in ids.animations
                for timing_id in (anim_ids.par, anim_ids.behavior)
            ),
        }
        renumber_generated_timing_ids(
            animation_element_records,
            reserved_ids=reserved_ids,
        )

        animation_elements = [fragment.par for fragment in native_fragments]
        animation_elements = merge_concurrent_simple_motion_fragments(
            animation_elements
        )

        # Build the complete timing tree as a single element, then serialize once
        timing_tree = self._xml_builder.build_timing_tree(
            ids=ids,
            animation_elements=animation_elements,
            animated_shape_ids=animated_shape_ids or [],
        )
        self._inject_flipbook_bld_entries(timing_tree, native_fragments)
        return to_string(timing_tree)

    @staticmethod
    def _inject_flipbook_bld_entries(
        timing_tree: etree._Element,
        native_fragments: list[NativeFragment],
    ) -> None:
        """Add ``<p:bldP>`` entries for flipbook frame shapes into ``<p:bldLst>``.

        Each flipbook frame shape needs a build-list entry whose ``grpId``
        matches the par_id of the animation's ``<p:cTn>``; otherwise PPT
        silently ignores the visibility ``<p:set>`` calls inside the par.
        Native bldLst assembly does not know about these extra shapes, so we
        append them after the timing tree has been built.
        """
        from svg2ooxml.drawingml.xml_builder import NS_P, p_sub

        extra_entries: list[tuple[str, int]] = []
        for fragment in native_fragments:
            entries = fragment.metadata.get("flipbook_bld_entries") if fragment.metadata else None
            if entries:
                extra_entries.extend(entries)
        if not extra_entries:
            return

        bld_lst = timing_tree.find(f".//{{{NS_P}}}bldLst")
        if bld_lst is None:
            bld_lst = p_sub(timing_tree, "bldLst")
        for shape_id, grp_id in extra_entries:
            p_sub(bld_lst, "bldP", spid=str(shape_id), grpId=str(grp_id), animBg="1")

    def _build_animation(
        self,
        animation: AnimationDefinition,
        options: Mapping[str, Any],
        par_id: int,
        behavior_id: int,
    ) -> tuple[NativeFragment | None, dict[str, Any] | None]:
        """Build a normalized native fragment for a single animation."""
        if self._policy is None:
            self._policy = AnimationPolicy(options)

        animation = self._bake_accumulate(animation)
        animation = self._clamp_duration(animation)

        max_error = self._policy.estimate_spline_error(animation)
        action, action_reason = self._policy.decide_action(animation, max_error)
        if action == AnimationAction.SKIP:
            return None, {"reason": action_reason}
        if action == AnimationAction.FLIPBOOK:
            return self._build_flipbook_fragment(animation, par_id, action_reason)

        handler = self._find_handler(animation)
        if handler is None:
            return None, {"reason": self._unsupported_reason(animation)}

        try:
            result = handler.build(animation, par_id, behavior_id)
            if result is None:
                return None, {"reason": "handler_returned_empty"}
            fragment = self._coerce_native_fragment(result)
            self._xml_builder.apply_native_timing_overrides(
                par=fragment.par,
                repeat_duration_ms=animation.repeat_duration_ms,
                restart=animation.restart,
                end_triggers=animation.end_triggers,
                default_target_shape=animation.element_id,
            )
            return fragment, None
        except Exception as e:
            return None, {"reason": f"handler_error: {str(e)}"}

    def _build_flipbook_fragment(
        self,
        animation: AnimationDefinition,
        par_id: int,
        policy_reason: str | None,
    ) -> tuple[NativeFragment | None, dict[str, Any] | None]:
        """Assemble a flipbook ``<p:par>`` for a dead-path animation.

        Requires that :class:`FlipbookPipeline` has already pre-spliced
        frame elements into the IR and that the writer was given a
        ``flipbook_frame_shape_ids`` map resolving animation_id to the
        list of frame shape IDs allocated during the IR walk.
        """
        anim_key = animation.animation_id or animation.element_id
        frame_shape_ids = self._flipbook_frame_shape_ids.get(anim_key)
        if not frame_shape_ids or len(frame_shape_ids) < 2:
            return None, {
                "reason": "flipbook_frames_unavailable",
                "policy_reason": policy_reason,
                "animation_id": anim_key,
            }

        oracle = default_oracle()
        try:
            par, bld_entries = oracle.instantiate_flipbook(
                frame_shape_ids=list(frame_shape_ids),
                par_id=par_id,
                duration_ms=animation.duration_ms,
                delay_ms=animation.begin_ms,
            )
        except Exception as exc:
            return None, {"reason": f"flipbook_oracle_error: {exc}"}

        fragment = NativeFragment(
            par=par,
            source="flipbook",
            strategy="oracle-flipbook",
            metadata={
                "flipbook_bld_entries": bld_entries,
                "policy_reason": policy_reason,
                "animation_id": anim_key,
            },
        )
        return fragment, None

    @staticmethod
    def _coerce_native_fragment(
        result: etree._Element | NativeFragment,
    ) -> NativeFragment:
        """Normalize handler output into a typed native fragment."""
        if isinstance(result, NativeFragment):
            return result
        return NativeFragment.from_legacy_par(result)

    @staticmethod
    def _unsupported_reason(animation: AnimationDefinition) -> str:
        """Return a stable reason code for animations with no registered handler."""
        if (
            animation.animation_type == AnimationType.ANIMATE_TRANSFORM
            and animation.transform_type in {TransformType.SKEWX, TransformType.SKEWY}
        ):
            return f"unsupported_transform_{animation.transform_type.value.lower()}"
        if animation.target_attribute == "color":
            return "unsupported_attribute_color"
        return "no_handler_found"

    @staticmethod
    def _bake_accumulate(animation: AnimationDefinition) -> AnimationDefinition:
        """Bake accumulate="sum" into expanded keyframe values.

        Each repetition builds on the previous end value by adding the
        end-start delta for numeric values.
        """
        if animation.accumulate != "sum":
            return animation
        if animation.repeat_count in (None, "indefinite", 1, "1"):
            return animation
        if len(animation.values) < 2:
            return animation
        try:
            repeat_n = int(animation.repeat_count)
        except (ValueError, TypeError):
            return animation
        if repeat_n <= 1:
            return animation

        base_vals = animation.values
        try:
            start_f = float(base_vals[0])
            end_f = float(base_vals[-1])
        except ValueError:
            return animation  # non-numeric — can't accumulate

        delta = end_f - start_f
        expanded: list[str] = list(base_vals)
        for rep in range(1, repeat_n):
            offset = delta * rep
            expanded.extend(str(float(v) + offset) for v in base_vals[1:])
        return replace(animation, values=expanded, accumulate="none")

    @staticmethod
    def _clamp_duration(animation: AnimationDefinition) -> AnimationDefinition:
        """Apply min/max duration constraints from SMIL."""
        if animation.min_ms is None and animation.max_ms is None:
            return animation
        dur = animation.duration_ms
        if dur == float("inf"):
            return animation
        if animation.min_ms is not None:
            dur = max(dur, animation.min_ms)
        if animation.max_ms is not None:
            dur = min(dur, animation.max_ms)
        if dur == animation.duration_ms:
            return animation
        return replace(
            animation,
            timing=replace(animation.timing, duration=dur / 1000.0),
        )

    def _find_handler(self, animation: AnimationDefinition) -> AnimationHandler | None:
        for handler in self._handlers:
            if handler.can_handle(animation):
                return handler
        return None
