"""Flipbook fallback protocol and IR pre-pass for dead-path animations.

A `FlipbookRenderer` produces N pre-rendered IR keyframes for an animation
that PowerPoint cannot play natively (e.g. `<animate>` on `stroke-width`).
The `FlipbookPipeline` runs *before* the DrawingML writer: it walks the
scene, finds animations the policy has routed to FLIPBOOK, asks the
renderer for keyframe IR, and splices those frames into the scene in
place of the original element. After the writer assigns shape IDs, the
animation builder looks up the frame element_ids → shape_ids and calls
`AnimationOracle.instantiate_flipbook(...)` to emit the timing XML.

Responsibilities are split deliberately:

- The protocol does rendering only — given an element, animation, and
  frame count, return N IR elements with unique `element_id`s.
- The pipeline does scene mutation only — find the target, ask the
  renderer, splice the result into the parent list, and record the
  animation_id → frame_element_ids mapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from svg2ooxml.drawingml.animation.policy import AnimationAction, AnimationPolicy
from svg2ooxml.ir.animation_definition import AnimationDefinition
from svg2ooxml.ir.scene import Group, IRElement, Scene

DEFAULT_FRAME_COUNT = 16


class FlipbookConfigError(ValueError):
    """Raised when ``fallback_mode="flipbook"`` is set without a renderer."""


def assert_flipbook_renderer_present(
    options: Mapping[str, Any], renderer: object | None
) -> None:
    """Fail loudly at config time when flipbook mode lacks a renderer.

    There is no default :class:`FlipbookRenderer`. Callers that want
    flipbook fallback must inject one explicitly; otherwise dead-path
    animations would silently fall through to SKIP and produce
    unanimated output, which is the failure mode we are trying to avoid.
    """
    mode = str(options.get("fallback_mode", "native")).lower()
    if mode == "flipbook" and renderer is None:
        raise FlipbookConfigError(
            "fallback_mode='flipbook' requires a FlipbookRenderer to be "
            "injected; no default is provided. Pass an explicit renderer "
            "or use fallback_mode='native'."
        )


class FlipbookRenderer(Protocol):
    """Produces pre-rendered IR keyframes for a single animation."""

    def render_frames(
        self,
        element: IRElement,
        animation: AnimationDefinition,
        n_frames: int,
    ) -> list[IRElement]:
        """Return `n_frames` IR elements representing the animation at
        evenly spaced time samples. Frame 0 is the initial state; the
        last frame is the final state. Each returned element must carry
        a unique non-None `element_id` so the writer can allocate shape
        IDs and the animation builder can resolve frame_shape_ids later.
        """
        ...


class FlipbookPipeline:
    """Pre-writer IR pass that splices flipbook frames into the scene."""

    def __init__(
        self,
        renderer: FlipbookRenderer,
        policy: AnimationPolicy,
        n_frames: int = DEFAULT_FRAME_COUNT,
    ) -> None:
        self._renderer = renderer
        self._policy = policy
        self._n_frames = n_frames
        self._frame_element_ids: dict[str, list[str]] = {}

    def process(
        self,
        scene: Scene | list[IRElement],
        definitions: list[AnimationDefinition],
    ) -> None:
        """Mutate `scene` in place: replace each FLIPBOOK-routed animation's
        target element with the renderer-produced keyframe sequence.

        Accepts either a :class:`Scene` (mutates ``scene.elements``) or a
        raw list of :class:`IRElement` (mutates the list directly).

        After this returns, the writer can walk the scene as usual; frame
        elements receive sequential shape IDs alongside everything else.
        Frame element_ids per animation are exposed via
        :meth:`frame_element_ids` for the animation builder to resolve into
        shape IDs after the writer runs.
        """
        elements = scene.elements if isinstance(scene, Scene) else scene
        for definition in definitions:
            action, _reason = self._policy.decide_action(definition, max_error=0.0)
            if action != AnimationAction.FLIPBOOK:
                continue

            target_id = definition.element_id
            element = _find_element(elements, target_id)
            if element is None:
                continue

            frames = self._renderer.render_frames(element, definition, self._n_frames)
            if len(frames) < 2:
                raise FlipbookPipelineError(
                    f"Flipbook renderer returned {len(frames)} frame(s) for "
                    f"element_id={target_id!r}; minimum is 2."
                )

            missing = [i for i, f in enumerate(frames) if not getattr(f, "element_id", None)]
            if missing:
                raise FlipbookPipelineError(
                    f"Flipbook renderer returned frames without element_id at "
                    f"indices {missing} for element_id={target_id!r}."
                )

            frames = _normalize_frame_metadata(frames)

            if not _splice_replace(elements, target_id, frames):
                raise FlipbookPipelineError(
                    f"Failed to splice flipbook frames into scene for "
                    f"element_id={target_id!r}."
                )

            key = definition.animation_id or target_id
            self._frame_element_ids[key] = [f.element_id for f in frames]

    def frame_element_ids(self, animation_id: str) -> list[str]:
        """Return the per-frame element_ids spliced for `animation_id`, or []."""
        return list(self._frame_element_ids.get(animation_id, ()))


class FlipbookPipelineError(RuntimeError):
    """Raised when the pipeline cannot fulfil a flipbook routing decision."""


def _normalize_frame_metadata(frames: list[IRElement]) -> list[IRElement]:
    """Rewrite each frame's ``metadata["element_ids"]`` to its own element_id.

    Renderers commonly produce frames via ``dataclasses.replace(element, ...)``
    which shares the original ``metadata`` dict by reference. The writer's
    IR walk registers shape IDs via ``metadata["element_ids"]`` (see
    ``writer_elements.py:_render_element`` → ``register_mapping``), so
    without this rewrite every frame would re-register the same original
    SVG id and ``_shape_map`` would only retain the last one. We give each
    frame its own metadata dict pointing at its own element_id.
    """
    normalized: list[IRElement] = []
    for frame in frames:
        base_meta = getattr(frame, "metadata", None)
        new_meta = dict(base_meta) if isinstance(base_meta, dict) else {}
        new_meta["element_ids"] = [frame.element_id]
        try:
            normalized.append(replace(frame, metadata=new_meta))
        except TypeError:
            # Element type without a metadata field — keep as-is and
            # trust the renderer to have populated metadata correctly.
            normalized.append(frame)
    return normalized


def _find_element(elements: list[IRElement], element_id: str) -> IRElement | None:
    for candidate in _walk_elements(elements):
        if getattr(candidate, "element_id", None) == element_id:
            return candidate
    return None


def _walk_elements(elements: list[IRElement]):
    for element in elements:
        yield element
        if isinstance(element, Group):
            yield from _walk_elements(element.children)


def _splice_replace(
    elements: list[IRElement], element_id: str, frames: list[IRElement]
) -> bool:
    if _replace_in_list(elements, element_id, frames):
        return True
    for element in _walk_elements(elements):
        if isinstance(element, Group) and _replace_in_list(
            element.children, element_id, frames
        ):
            return True
    return False


def _replace_in_list(
    container: list[IRElement], element_id: str, frames: list[IRElement]
) -> bool:
    for index, child in enumerate(container):
        if getattr(child, "element_id", None) == element_id:
            container[index : index + 1] = frames
            return True
    return False


__all__ = [
    "DEFAULT_FRAME_COUNT",
    "FlipbookConfigError",
    "FlipbookPipeline",
    "FlipbookPipelineError",
    "FlipbookRenderer",
    "assert_flipbook_renderer_present",
]
