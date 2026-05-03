"""Tests for DrawingML animation pipeline mapping."""

from __future__ import annotations

from svg2ooxml.drawingml.animation_pipeline import AnimationPipeline
from svg2ooxml.ir.animation import (
    AnimationDefinition,
    AnimationTiming,
    AnimationType,
    BeginTrigger,
    BeginTriggerType,
)


def test_bookmark_navigation_list_remaps_indefinite_begin_to_click_trigger() -> None:
    pipeline = AnimationPipeline()
    animation = AnimationDefinition(
        element_id="target",
        animation_id="fadein",
        animation_type=AnimationType.ANIMATE,
        target_attribute="fill",
        values=["FFFFFF", "0000FF"],
        timing=AnimationTiming(
            begin=0.0,
            duration=1.0,
            begin_triggers=[BeginTrigger(BeginTriggerType.INDEFINITE)],
        ),
    )
    pipeline.reset({"definitions": [animation]})
    pipeline.register_mapping({"element_ids": ["target"]}, 2)
    pipeline.register_mapping(
        {
            "element_ids": ["button"],
            "navigation": [
                {"kind": "external", "href": "https://example.com"},
                {"kind": "bookmark", "bookmark": {"name": "fadein"}},
            ],
        },
        3,
    )

    xml = pipeline.build(max_shape_id=3)

    assert 'evt="onClick"' in xml
    assert '<p:spTgt spid="3"/>' in xml


def test_register_mapping_overwrites_existing_shape_id() -> None:
    pipeline = AnimationPipeline()
    animation = AnimationDefinition(
        element_id="wing",
        animation_type=AnimationType.ANIMATE,
        target_attribute="x",
        values=["10", "20"],
        timing=AnimationTiming(begin=0.0, duration=1.0),
    )
    pipeline.reset({"definitions": [animation]})
    pipeline.register_mapping({"element_ids": ["wing"]}, 10)
    pipeline.register_mapping({"element_ids": ["wing"]}, 11)

    xml = pipeline.build(max_shape_id=11)

    assert '<p:spTgt spid="11"/>' in xml
    assert '<p:spTgt spid="10"/>' not in xml


def test_register_element_ids_overwrites_existing_shape_id() -> None:
    pipeline = AnimationPipeline()
    animation = AnimationDefinition(
        element_id="segment",
        animation_type=AnimationType.SET,
        target_attribute="visibility",
        values=["hidden"],
        timing=AnimationTiming(begin=0.0, duration=1.0),
    )
    pipeline.reset({"definitions": [animation]})
    pipeline.register_element_ids(["segment"], 10)
    pipeline.register_element_ids(["segment"], 11)

    xml = pipeline.build(max_shape_id=11)

    assert '<p:spTgt spid="11"/>' in xml
    assert '<p:spTgt spid="10"/>' not in xml
