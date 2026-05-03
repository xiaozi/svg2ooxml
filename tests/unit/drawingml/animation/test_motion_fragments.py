"""Tests for motion fragment merge compatibility around non-motion siblings."""

from __future__ import annotations

from lxml import etree

from svg2ooxml.drawingml.animation.motion_fragments import (
    merge_concurrent_simple_motion_fragments,
)
from svg2ooxml.drawingml.xml_builder import NS_P, p_elem, p_sub


def _simple_motion_par(
    *,
    include_anim_rot: bool,
    include_anim_scale: bool,
    path: str = "M 0 0 L 0.1 0.2 E",
) -> etree._Element:
    par = p_elem("par")
    c_tn = p_sub(par, "cTn", fill="hold")
    st_cond_lst = p_sub(c_tn, "stCondLst")
    p_sub(st_cond_lst, "cond", delay="0")
    child_tn_list = p_sub(c_tn, "childTnLst")

    if include_anim_scale:
        p_sub(child_tn_list, "animScale")
    if include_anim_rot:
        p_sub(child_tn_list, "animRot", by="360")

    anim_motion = p_sub(
        child_tn_list,
        "animMotion",
        origin="layout",
        path=path,
        pathEditMode="relative",
    )
    behavior = p_sub(anim_motion, "cBhvr")
    behavior_ctn = p_sub(behavior, "cTn", fill="remove")
    p_sub(behavior_ctn, "stCondLst")
    target = p_sub(behavior, "tgtEl")
    p_sub(target, "spTgt", spid="shape1")

    return par


def _count_anim_motion(output: list[etree._Element]) -> int:
    return sum(
        len(par.findall(f".//{{{NS_P}}}animMotion")) for par in output
    )


def test_does_not_merge_simple_motion_when_non_motion_children_differ():
    # Animated path + sibling rotation should stay separate from plain path motion.
    par_with_rot = _simple_motion_par(include_anim_rot=True, include_anim_scale=False)
    par_plain = _simple_motion_par(include_anim_rot=False, include_anim_scale=False)

    merged = merge_concurrent_simple_motion_fragments([par_with_rot, par_plain])

    assert len(merged) == 2
    assert _count_anim_motion(merged) == 2


def test_allows_merge_when_only_non_motion_child_is_anim_scale():
    # Scale-origin compensation motion is intentionally mergeable with pure motion.
    par_with_scale_motion = _simple_motion_par(
        include_anim_rot=False,
        include_anim_scale=True,
    )
    par_plain = _simple_motion_par(
        include_anim_rot=False,
        include_anim_scale=False,
        path="M 0 0 L 0.1 0.2 E",
    )

    merged = merge_concurrent_simple_motion_fragments([par_with_scale_motion, par_plain])

    assert len(merged) == 1
    assert _count_anim_motion(merged) == 1
    merged_paths = [
        p.get("path")
        for par in merged
        for p in par.findall(f".//{{{NS_P}}}animMotion")
    ]
    assert merged_paths == ["M 0 0 L 0.2 0.4 E"]
