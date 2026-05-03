from __future__ import annotations

from lxml import etree

from svg2ooxml.filters.base import FilterContext, FilterResult
from svg2ooxml.filters.registry import FilterRegistry


def test_seed_base_inputs_includes_filter_inputs_metadata() -> None:
    registry = FilterRegistry()
    filter_element = etree.Element("filter")
    context = FilterContext(
        filter_element=filter_element,
        options={
            "filter_inputs": {
                "SourceGraphic": {
                    "geometry": [
                        {"type": "line", "start": (0.0, 0.0), "end": (10.0, 0.0)}
                    ],
                    "closed": False,
                    "fill": {"type": "solid", "rgb": "FF0000", "opacity": 1.0},
                },
                "FillPaint": {
                    "geometry": [
                        {"type": "line", "start": (0.0, 0.0), "end": (10.0, 0.0)}
                    ],
                    "closed": False,
                    "fill": {"type": "solid", "rgb": "FF0000", "opacity": 1.0},
                    "stroke": None,
                },
                "StrokePaint": {
                    "geometry": [
                        {"type": "line", "start": (0.0, 0.0), "end": (10.0, 0.0)}
                    ],
                    "closed": False,
                    "fill": None,
                    "stroke": {"width": 2.0},
                },
            }
        },
    )
    pipeline: dict[str, FilterResult] = {}

    registry._seed_base_inputs(pipeline, context)

    assert "SourceGraphic" in pipeline
    assert "SourceAlpha" in pipeline

    graphic_meta = pipeline["SourceGraphic"].metadata
    assert graphic_meta["ref"] == "SourceGraphic"
    assert graphic_meta["geometry"][0]["type"] == "line"
    assert graphic_meta["fill"]["type"] == "solid"

    alpha_meta = pipeline["SourceAlpha"].metadata
    assert alpha_meta["ref"] == "SourceAlpha"
    assert alpha_meta["alpha_source"] == "SourceGraphic"
    assert alpha_meta["geometry"][0]["type"] == "line"

    assert pipeline["FillPaint"].metadata["ref"] == "FillPaint"
    assert pipeline["FillPaint"].metadata["stroke"] is None
    assert pipeline["StrokePaint"].metadata["ref"] == "StrokePaint"
    assert pipeline["StrokePaint"].metadata["fill"] is None


def test_seed_base_inputs_derives_paint_inputs_from_source_graphic() -> None:
    registry = FilterRegistry()
    filter_element = etree.Element("filter")
    context = FilterContext(
        filter_element=filter_element,
        options={
            "filter_inputs": {
                "SourceGraphic": {
                    "geometry": [
                        {"type": "line", "start": (0.0, 0.0), "end": (10.0, 0.0)}
                    ],
                    "closed": False,
                    "fill": {"type": "solid", "rgb": "FF0000", "opacity": 1.0},
                    "stroke": {
                        "width": 2.0,
                        "paint": {"type": "solid", "rgb": "0000FF", "opacity": 1.0},
                    },
                },
            }
        },
    )
    pipeline: dict[str, FilterResult] = {}

    registry._seed_base_inputs(pipeline, context)

    fill_meta = pipeline["FillPaint"].metadata
    stroke_meta = pipeline["StrokePaint"].metadata
    assert fill_meta["paint_source"] == "FillPaint"
    assert fill_meta["paint_surface"] is True
    assert fill_meta["fill"]["rgb"] == "FF0000"
    assert fill_meta["stroke"] is None
    assert stroke_meta["paint_source"] == "StrokePaint"
    assert stroke_meta["paint_surface"] is True
    assert stroke_meta["fill"]["rgb"] == "0000FF"
    assert stroke_meta["stroke"] is None


def test_seed_base_inputs_flattens_group_paint_inputs_into_surface() -> None:
    registry = FilterRegistry()
    filter_element = etree.Element("filter")
    child_descriptor = {
        "shape_type": "Circle",
        "geometry": [
            {"type": "line", "start": (0.0, 0.0), "end": (10.0, 0.0)}
        ],
        "closed": True,
        "fill": {"type": "solid", "rgb": "FF0000", "opacity": 1.0},
        "stroke": {
            "width": 2.0,
            "paint": {"type": "solid", "rgb": "0000FF", "opacity": 1.0},
        },
    }
    context = FilterContext(
        filter_element=filter_element,
        options={
            "filter_inputs": {
                "SourceGraphic": {
                    "shape_type": "Group",
                    "children": [child_descriptor],
                },
            }
        },
    )
    pipeline: dict[str, FilterResult] = {}

    registry._seed_base_inputs(pipeline, context)

    fill_meta = pipeline["FillPaint"].metadata
    stroke_meta = pipeline["StrokePaint"].metadata
    assert fill_meta["paint_source"] == "FillPaint"
    assert fill_meta["paint_surface"] is True
    assert fill_meta["fill"]["rgb"] == "FF0000"
    assert fill_meta["stroke"] is None
    assert stroke_meta["paint_source"] == "StrokePaint"
    assert stroke_meta["paint_surface"] is True
    assert stroke_meta["fill"]["rgb"] == "0000FF"
    assert stroke_meta["stroke"] is None
