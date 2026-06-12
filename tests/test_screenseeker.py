"""Tests for the deterministic parts of the ScreenSeekeR algorithm.

Covers IoU, NMS, coordinate denormalisation, and cropping with offset tracking.
The API-driven planner/grounder/checker calls are out of scope here — they need a
live ANTHROPIC_API_KEY and live image input and are exercised end-to-end via
`tjm-run --grounder screenseeker`.
"""

from __future__ import annotations

import numpy as np

from tjm_automation.screenseeker import (
    CandidateRegion,
    _crop,
    _denorm,
    _iou,
    _nms,
    _norm_to_px,
)


def test_iou_identical_boxes_is_one() -> None:
    assert _iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0


def test_iou_disjoint_boxes_is_zero() -> None:
    assert _iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0


def test_iou_half_overlap() -> None:
    # Two 10x10 boxes overlapping in a 5x10 strip -> inter=50, union=150 -> 1/3
    iou = _iou((0, 0, 10, 10), (5, 0, 15, 10))
    assert abs(iou - (50 / 150)) < 1e-9


def test_nms_drops_overlap_keeps_first() -> None:
    boxes = [
        (0, 0, 100, 100),     # kept
        (10, 10, 90, 90),     # dropped (high IoU with first)
        (200, 200, 300, 300), # kept (no overlap)
    ]
    kept = _nms(boxes, iou_threshold=0.5)
    assert kept == [(0, 0, 100, 100), (200, 200, 300, 300)]


def test_nms_keeps_low_overlap_boxes() -> None:
    boxes = [(0, 0, 100, 100), (90, 90, 200, 200)]  # very small intersection
    kept = _nms(boxes, iou_threshold=0.5)
    assert kept == boxes


def test_denorm_scales_correctly() -> None:
    # 500/1000 of 1920 -> 960
    assert _denorm(500, 1920) == 960
    # 0 -> 0
    assert _denorm(0, 1080) == 0
    # 1000 -> dim
    assert _denorm(1000, 1080) == 1080


def test_norm_to_px_clamps_to_image_bounds() -> None:
    cand = CandidateRegion(
        rationale="any", x1=0, y1=0, x2=1000, y2=1000, confidence=0.9
    )
    bbox = _norm_to_px(cand, w=1920, h=1080)
    assert bbox == (0, 0, 1920, 1080)


def test_crop_returns_offset_for_absolute_remap() -> None:
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    sub, offset = _crop(img, (50, 25, 150, 75), pad_px=0)
    assert sub.shape == (50, 100, 3)
    assert offset == (50, 25)


def test_crop_padding_is_clamped_to_image() -> None:
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    # Padding would push x1 to -5; expect 0 instead.
    sub, offset = _crop(img, (0, 0, 50, 50), pad_px=5)
    assert offset == (0, 0)
    assert sub.shape == (55, 55, 3)
