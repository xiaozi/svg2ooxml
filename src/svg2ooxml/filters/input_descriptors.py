"""Helpers for SVG filter input-surface metadata."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

PAINT_INPUT_NAMES = ("FillPaint", "StrokePaint")


def paint_input_descriptors(
    filter_inputs: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return explicit or SourceGraphic-derived paint input descriptors."""

    if not isinstance(filter_inputs, Mapping):
        return {}

    source_graphic = filter_inputs.get("SourceGraphic")
    descriptors: dict[str, dict[str, Any]] = {}
    for input_name in PAINT_INPUT_NAMES:
        explicit = filter_inputs.get(input_name)
        if isinstance(explicit, dict):
            descriptors[input_name] = copy.deepcopy(explicit)
            continue
        derived = derive_paint_input_descriptor(source_graphic, input_name)
        if derived is not None:
            descriptors[input_name] = derived
    return descriptors


def derive_paint_input_descriptor(
    source_graphic: Any,
    input_name: str,
) -> dict[str, Any] | None:
    """Derive a paint-only descriptor from a SourceGraphic descriptor."""

    if input_name not in PAINT_INPUT_NAMES or not isinstance(source_graphic, dict):
        return None
    paint, opacity = _first_paint_descriptor(source_graphic, input_name)
    if paint is None:
        return None
    if _paint_uses_object_bounding_box(paint):
        descriptor = copy.deepcopy(source_graphic)
        _apply_geometry_paint_input_mode(descriptor, input_name)
        descriptor["paint_source"] = input_name
        return descriptor
    return {
        "shape_type": "PaintSurface",
        "paint_surface": True,
        "paint_source": input_name,
        "fill": paint,
        "stroke": None,
        "opacity": opacity,
        "bbox": copy.deepcopy(source_graphic.get("bbox")),
    }


def _first_paint_descriptor(
    descriptor: Mapping[str, Any],
    input_name: str,
    *,
    inherited_opacity: float = 1.0,
) -> tuple[dict[str, Any] | None, float]:
    opacity = inherited_opacity * _coerce_opacity(descriptor.get("opacity"))
    if input_name == "FillPaint":
        fill = descriptor.get("fill")
        if _has_paint(fill):
            return copy.deepcopy(fill), opacity
    else:
        stroke = descriptor.get("stroke")
        if isinstance(stroke, Mapping):
            paint = stroke.get("paint")
            if _has_paint(paint):
                stroke_opacity = opacity * _coerce_opacity(stroke.get("opacity"))
                return copy.deepcopy(paint), stroke_opacity

    children = descriptor.get("children")
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, Mapping):
                continue
            paint, child_opacity = _first_paint_descriptor(
                child,
                input_name,
                inherited_opacity=opacity,
            )
            if paint is not None:
                return paint, child_opacity
    return None, opacity


def _apply_geometry_paint_input_mode(
    descriptor: dict[str, Any], input_name: str
) -> None:
    children = descriptor.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                _apply_geometry_paint_input_mode(child, input_name)
        return
    if input_name == "FillPaint":
        descriptor["stroke"] = None
    else:
        descriptor["fill"] = None


def _paint_uses_object_bounding_box(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    paint_type = str(value.get("type") or "").strip().lower()
    if paint_type not in {"lineargradient", "radialgradient"}:
        return False
    gradient_units = str(value.get("gradient_units") or "objectBoundingBox")
    return gradient_units.strip() == "objectBoundingBox"


def _has_paint(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    paint_type = str(value.get("type") or "").strip().lower()
    return bool(paint_type and paint_type != "none")


def _coerce_opacity(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


__all__ = [
    "PAINT_INPUT_NAMES",
    "derive_paint_input_descriptor",
    "paint_input_descriptors",
]
