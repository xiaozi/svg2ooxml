"""Animation pipeline that remaps and emits DrawingML timing fragments."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from svg2ooxml.ir.animation import AnimationDefinition, BeginTriggerType
from svg2ooxml.ir.scene import IRElement

from .animation import DrawingMLAnimationWriter
from .animation.flipbook import (
    DEFAULT_FRAME_COUNT,
    FlipbookConfigError,
    FlipbookPipeline,
    FlipbookRenderer,
    assert_flipbook_renderer_present,
)

_FLIPBOOK_MIN_FRAMES = 2
_FLIPBOOK_MAX_FRAMES = 64
from .animation.policy import AnimationPolicy

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from svg2ooxml.core.tracing import ConversionTracer


def _iter_string_items(value: object) -> Iterable[str]:
    """Yield string items from metadata lists without treating strings as lists."""

    if not isinstance(value, (list, tuple, set)):
        return ()
    return (item for item in value if isinstance(item, str))


class AnimationPipeline:
    """Track animation mappings and build timing XML."""

    def __init__(
        self,
        *,
        writer: DrawingMLAnimationWriter | None = None,
        trace_writer: Callable[..., None] | None = None,
    ) -> None:
        self._writer = writer or DrawingMLAnimationWriter()
        self._trace_writer = trace_writer
        self._payload: dict[str, Any] | None = None
        self._shape_map: dict[str, str] = {}
        self._animation_target_map: dict[str, str] = {}
        self._bookmark_trigger_map: dict[str, str] = {}
        self._animation_element_ids: set[str] = set()
        self._policy: dict[str, object] = {}
        self._tracer: ConversionTracer | None = None
        self._flipbook_pipeline: FlipbookPipeline | None = None

    def reset(self, payload: dict[str, Any] | None, *, tracer: ConversionTracer | None = None) -> None:
        self._payload = payload
        self._shape_map = {}
        self._animation_target_map = {}
        self._bookmark_trigger_map = {}
        self._animation_element_ids = set()
        self._policy = {}
        self._tracer = tracer
        self._flipbook_pipeline = None
        if isinstance(payload, dict):
            payload_policy = payload.get("policy")
            if isinstance(payload_policy, dict):
                self._policy = dict(payload_policy)
            definitions = payload.get("definitions") or []
            for definition in definitions:
                element_id = getattr(definition, "element_id", None)
                if isinstance(element_id, str):
                    self._animation_element_ids.add(element_id)
            self._configure_flipbook(payload)

    def _configure_flipbook(self, payload: dict[str, Any]) -> None:
        renderer = payload.get("flipbook_renderer")
        assert_flipbook_renderer_present(self._policy, renderer)
        if renderer is None:
            return
        raw_n_frames = payload.get("flipbook_n_frames", DEFAULT_FRAME_COUNT)
        try:
            n_frames = int(raw_n_frames)
        except (TypeError, ValueError):
            n_frames = DEFAULT_FRAME_COUNT
        if not _FLIPBOOK_MIN_FRAMES <= n_frames <= _FLIPBOOK_MAX_FRAMES:
            raise FlipbookConfigError(
                f"flipbook_n_frames must be between {_FLIPBOOK_MIN_FRAMES} and "
                f"{_FLIPBOOK_MAX_FRAMES} inclusive, got {raw_n_frames!r}."
            )
        self._flipbook_pipeline = FlipbookPipeline(
            renderer, AnimationPolicy(self._policy), n_frames=n_frames
        )

    def run_flipbook_prepass(self, scene: list[IRElement]) -> None:
        """Splice flipbook frames into ``scene`` before the IR walk.

        Safe to call unconditionally — no-op when no FlipbookRenderer was
        provided in the payload. Must be invoked after :meth:`reset` and
        before the writer renders the scene, so the regular IR walk
        registers the frame element_ids → shape_ids alongside everything
        else.
        """
        if self._flipbook_pipeline is None or not isinstance(self._payload, dict):
            return
        definitions = self._payload.get("definitions") or []
        self._flipbook_pipeline.process(scene, list(definitions))

    def register_mapping(self, metadata: dict[str, object] | None, shape_id: int) -> None:
        if not isinstance(metadata, dict):
            return
        for element_id in _iter_string_items(metadata.get("element_ids")):
            self._shape_map[element_id] = str(shape_id)
        self._register_navigation_trigger(metadata, shape_id)

    def register_element_ids(self, element_ids: Iterable[object], shape_id: int) -> None:
        for element_id in _iter_string_items(element_ids):
            self._shape_map[element_id] = str(shape_id)

    def metadata_targets_animation(self, metadata: dict[str, object] | None) -> bool:
        if not isinstance(metadata, dict) or not self._animation_element_ids:
            return False
        return any(
            element_id in self._animation_element_ids
            for element_id in _iter_string_items(metadata.get("element_ids"))
        )

    def _register_navigation_trigger(self, metadata: dict[str, object], shape_id: int) -> None:
        navigation = metadata.get("navigation")
        entries = navigation if isinstance(navigation, list) else [navigation]
        for entry in entries:
            if isinstance(entry, dict):
                self._register_bookmark_navigation_entry(entry, shape_id)

    def _register_bookmark_navigation_entry(
        self,
        navigation: dict[str, object],
        shape_id: int,
    ) -> None:
        if navigation.get("kind") != "bookmark":
            return
        bookmark = navigation.get("bookmark")
        name: object | None = None
        if isinstance(bookmark, dict):
            name = bookmark.get("name")
        if name is None:
            name = navigation.get("bookmark_name")
        if isinstance(name, str) and name:
            self._bookmark_trigger_map.setdefault(name, str(shape_id))

    def build(self, *, max_shape_id: int = 0) -> str:
        if not self._payload:
            return ""

        definitions = self._payload.get("definitions") or []
        timeline = self._payload.get("timeline") or []
        if not definitions:
            return ""

        self._populate_flipbook_shape_map(definitions)

        self._animation_target_map = {}
        for definition in definitions:
            element_id = getattr(definition, "element_id", None)
            animation_id = getattr(definition, "animation_id", None)
            if not isinstance(element_id, str) or not isinstance(animation_id, str):
                continue
            shape_id = self._shape_map.get(element_id)
            if shape_id:
                self._animation_target_map.setdefault(animation_id, shape_id)

        remapped: list[AnimationDefinition] = []
        animated_shape_ids: set[str] = set()
        for definition in definitions:
            element_id = getattr(definition, "element_id", None)
            if not isinstance(element_id, str):
                self._trace(
                    "invalid_animation_definition",
                    metadata={"reason": "missing_element_id"},
                )
                continue
            shape_id = self._shape_map.get(element_id)
            if not shape_id:
                self._trace(
                    "unmapped_animation",
                    metadata={
                        "element_id": element_id,
                        "animation_type": definition.animation_type.value,
                    },
                )
                continue
            remapped_definition = replace(definition, element_id=shape_id)
            remapped_definition = self._remap_trigger_targets(remapped_definition, shape_id=shape_id)
            remapped.append(remapped_definition)
            animated_shape_ids.add(shape_id)
            self._trace(
                "mapped_animation",
                metadata={
                    "element_id": element_id,
                    "shape_id": shape_id,
                    "animation_type": definition.animation_type.value,
                },
            )

        if not remapped:
            if definitions:
                self._trace(
                    "timing_skipped",
                    metadata={"reason": "no_mapped_definitions", "animation_count": len(definitions)},
                )
            return ""

        # Build complete timing XML, including bldLst
        # Start timing IDs after the last shape ID to avoid collisions
        start_id = max(max_shape_id + 1, 1)
        flipbook_frame_shape_ids = self._resolve_flipbook_frame_shape_ids(remapped)
        animation_xml = self._writer.build(
            remapped,
            timeline,
            tracer=self._tracer,
            options=self._policy,
            animated_shape_ids=sorted(list(animated_shape_ids), key=int),
            start_id=start_id,
            flipbook_frame_shape_ids=flipbook_frame_shape_ids or None,
        )
        if animation_xml:
            self._trace(
                "timing_emitted",
                metadata={
                    "animation_count": len(remapped),
                    "timeline_frames": len(timeline),
                    "fallback_mode": self._policy.get("fallback_mode", "native"),
                },
            )
        else:
            self._trace(
                "timing_skipped",
                metadata={
                    "reason": "writer_returned_empty",
                    "animation_count": len(remapped),
                    "fallback_mode": self._policy.get("fallback_mode", "native"),
                },
            )
        return animation_xml

    def _populate_flipbook_shape_map(
        self, definitions: list[AnimationDefinition]
    ) -> None:
        """Map each flipbook animation's original element_id to its first
        frame's shape_id so the standard remapping loop treats the
        animation as targeting a real slide shape rather than dropping it
        as unmapped.
        """
        if self._flipbook_pipeline is None:
            return
        for definition in definitions:
            svg_element_id = getattr(definition, "element_id", None)
            if not isinstance(svg_element_id, str):
                continue
            if svg_element_id in self._shape_map:
                continue
            flip_key = (
                getattr(definition, "animation_id", None) or svg_element_id
            )
            frame_element_ids = self._flipbook_pipeline.frame_element_ids(flip_key)
            if not frame_element_ids:
                continue
            first_shape_id = self._shape_map.get(frame_element_ids[0])
            if first_shape_id is not None:
                self._shape_map[svg_element_id] = first_shape_id

    def _resolve_flipbook_frame_shape_ids(
        self, remapped_definitions: list[AnimationDefinition]
    ) -> dict[str, list[str]]:
        """Build the animation_id → list[frame_shape_id] map the writer
        consumes to call ``instantiate_flipbook``.

        The key matches what ``_build_flipbook_fragment`` looks up:
        ``animation.animation_id`` if present, otherwise the post-remap
        ``animation.element_id`` (which is the first frame's shape_id by
        the time ``_populate_flipbook_shape_map`` has run).
        """
        if self._flipbook_pipeline is None or not isinstance(self._payload, dict):
            return {}
        result: dict[str, list[str]] = {}
        originals = self._payload.get("definitions") or []
        for original in originals:
            svg_element_id = getattr(original, "element_id", None)
            anim_id = getattr(original, "animation_id", None)
            flip_key = anim_id or svg_element_id
            if not isinstance(flip_key, str):
                continue
            frame_element_ids = self._flipbook_pipeline.frame_element_ids(flip_key)
            if not frame_element_ids:
                continue
            shape_ids = [
                self._shape_map[fe]
                for fe in frame_element_ids
                if fe in self._shape_map
            ]
            if not shape_ids:
                continue
            if isinstance(anim_id, str):
                writer_key = anim_id
            else:
                writer_key = self._shape_map.get(svg_element_id or "")
                if not writer_key:
                    continue
            result[writer_key] = shape_ids
        return result

    def _remap_trigger_targets(
        self,
        definition: AnimationDefinition,
        *,
        shape_id: str,
    ) -> AnimationDefinition:
        """Remap trigger target element IDs to slide shape IDs."""
        timing = getattr(definition, "timing", None)
        if timing is None:
            return definition

        begin_triggers, begin_changed = self._remap_trigger_list(
            getattr(timing, "begin_triggers", None),
            definition=definition,
            shape_id=shape_id,
            unmapped_trace_action="unmapped_begin_trigger_target",
            remap_indefinite_bookmark=True,
        )
        end_triggers, end_changed = self._remap_trigger_list(
            getattr(timing, "end_triggers", None),
            definition=definition,
            shape_id=shape_id,
            unmapped_trace_action="unmapped_end_trigger_target",
            remap_indefinite_bookmark=False,
        )
        if not begin_changed and not end_changed:
            return definition

        remapped_timing = replace(
            timing,
            begin_triggers=begin_triggers,
            end_triggers=end_triggers,
        )
        return replace(definition, timing=remapped_timing)

    def _remap_trigger_list(
        self,
        triggers,
        *,
        definition: AnimationDefinition,
        shape_id: str,
        unmapped_trace_action: str,
        remap_indefinite_bookmark: bool,
    ) -> tuple[list, bool]:
        if not triggers:
            return triggers, False

        remapped_triggers = []
        changed = False
        for trigger in triggers:
            trigger_type_enum = getattr(trigger, "trigger_type", None)
            if remap_indefinite_bookmark and trigger_type_enum == BeginTriggerType.INDEFINITE:
                mapped_click_shape = None
                animation_id = getattr(definition, "animation_id", None)
                if isinstance(animation_id, str):
                    mapped_click_shape = self._bookmark_trigger_map.get(animation_id)
                if mapped_click_shape is not None:
                    changed = True
                    remapped_triggers.append(
                        replace(
                            trigger,
                            trigger_type=BeginTriggerType.CLICK,
                            target_element_id=mapped_click_shape,
                            delay_seconds=0.0,
                        )
                    )
                else:
                    remapped_triggers.append(trigger)
                continue
            target_id = getattr(trigger, "target_element_id", None)
            if not target_id:
                remapped_triggers.append(trigger)
                continue

            mapped = self._shape_map.get(target_id)
            if mapped is None:
                mapped = self._animation_target_map.get(target_id)
            if mapped is None:
                trigger_type = getattr(getattr(trigger, "trigger_type", None), "value", None)
                # Fallback for unresolved explicit click target: click defaults to current shape.
                if trigger_type == "click":
                    mapped = shape_id
                else:
                    mapped = None
                    self._trace(
                        unmapped_trace_action,
                        metadata={
                            "element_id": getattr(definition, "element_id", None),
                            "target_element_id": target_id,
                            "trigger_type": trigger_type,
                        },
                    )

            if mapped != target_id:
                changed = True
                remapped_triggers.append(replace(trigger, target_element_id=mapped))
            else:
                remapped_triggers.append(trigger)
        return remapped_triggers, changed

    def _trace(self, action: str, *, metadata: dict[str, object] | None = None) -> None:
        if self._trace_writer is None:
            return
        self._trace_writer(action, stage="animation", metadata=metadata)


__all__ = ["AnimationPipeline"]
