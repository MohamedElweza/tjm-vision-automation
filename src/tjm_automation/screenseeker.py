"""ScreenSeekeR: cascaded visual search for GUI grounding.

Implements the algorithm from arXiv:2504.07981 (Li et al., 2025 — ScreenSpot-Pro).
Three-tier decomposition driven by a single VLM (Claude Sonnet 4.6 here):

    1. Planner — full screenshot + natural-language target -> ranked candidate regions.
    2. Grounder — crop of a candidate region -> precise bounding box of the target.
    3. Result-checker — verifies the candidate is actually the target before committing.

The planner result is scored with NMS, the top candidate is cropped, and the algorithm
recurses on the crop until the patch is small enough (or max_depth is reached) to
direct-ground. This narrowing-search pattern is the paper's key insight: it raised
ScreenSpot-Pro accuracy from 18.9 percent (single-pass grounding) to 48.1 percent.

The paper uses GPT-4o as planner and OS-Atlas-7B as grounder. This implementation uses
Claude Sonnet 4.6 for both roles via the Anthropic API. The structure (planner -> grounder
-> score -> crop -> recurse -> verify) is preserved exactly; only the model is different.
Off-the-shelf OS-Atlas-7B would require a GPU not available in this assessment's runtime.

Designed to drop in alongside the existing OCR pipeline as a high-flexibility option;
falls through to OCR if no API key is configured. See `grounding.ground_icon()`.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple

import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

logger = logging.getLogger(__name__)

# Sonnet 4.6 vision API caps the long edge at ~1568px. We downscale before sending
# the planner the full desktop; saves tokens and avoids silent server-side resize.
MAX_LONG_EDGE_PX = 1280

# Coordinate normalization: the model returns boxes in [0, 1000] regardless of the
# actual image resolution. This is the convention most VLM grounders are trained on
# and keeps prompts identical across recursion levels.
NORM_MAX = 1000


# ---------------------------- Structured outputs ----------------------------


class CandidateRegion(BaseModel):
    """One candidate region the planner thinks may contain the target."""

    rationale: str = Field(description="Brief reason this region is a candidate.")
    x1: int = Field(ge=0, le=NORM_MAX)
    y1: int = Field(ge=0, le=NORM_MAX)
    x2: int = Field(ge=0, le=NORM_MAX)
    y2: int = Field(ge=0, le=NORM_MAX)
    confidence: float = Field(ge=0.0, le=1.0)


class PlannerResponse(BaseModel):
    candidates: List[CandidateRegion] = Field(
        default_factory=list,
        description="1-5 candidate regions ordered by descending probability.",
    )


class GroundResponse(BaseModel):
    found: bool
    rationale: str
    x1: int = Field(ge=0, le=NORM_MAX, default=0)
    y1: int = Field(ge=0, le=NORM_MAX, default=0)
    x2: int = Field(ge=0, le=NORM_MAX, default=0)
    y2: int = Field(ge=0, le=NORM_MAX, default=0)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class CheckResponse(BaseModel):
    verdict: Literal["is_target", "target_elsewhere", "target_not_found"]
    rationale: str


@dataclass
class ScreenSeekeRResult:
    """Final result of a recursive visual search. Mirrors GroundingResult fields."""

    found: bool
    center: Optional[Tuple[int, int]] = None
    bbox: Optional[Tuple[int, int, int, int]] = None
    confidence: float = 0.0
    method: str = "screenseeker"
    reason: str = ""
    depth: int = 0
    api_calls: int = 0


# ----------------------------- System prompts -------------------------------

PLANNER_SYSTEM = """You are a GUI grounding planner. Given a screenshot and a \
natural-language description of a target UI element, identify candidate regions of the \
image most likely to contain the target.

Coordinate system: the image's top-left is (0, 0) and bottom-right is (1000, 1000). All \
region coordinates are in this normalized space, regardless of the image's pixel size.

Apply these heuristics:
- Use GUI conventions (desktop icons on the left, taskbar at the bottom, system tray \
bottom-right, title bars at the top, etc.) to predict where the target sits.
- For labeled icons (e.g. "Notepad"), the text label is the strongest visual cue.
- Reason about neighbors: related icons or labels tell you the target is nearby.
- Treat popups, modal dialogs, and floating windows as OBSTACLES unless the target is \
inside one of them. Look around them, not through them.
- Return 1-5 candidates in descending probability. Prefer slightly oversized regions \
(easier to crop into) over tight ones (might miss the target)."""

GROUNDER_SYSTEM = """You are a GUI grounding model. Given a screenshot (which may be a \
cropped patch of a larger screen) and a natural-language description, return the precise \
bounding box of the target.

Coordinate system: top-left is (0, 0), bottom-right is (1000, 1000), normalized.

Rules:
- If the target is clearly visible, set found=true and return a tight box around its \
CLICKABLE area.
- For Windows desktop icons specifically, the click target is the ICON IMAGE itself, \
which sits ABOVE the text label. Box the icon, not the label.
- If the target is not visible, set found=false and leave coordinates at 0.
- Be precise: the center of your box becomes the click point."""

CHECKER_SYSTEM = """You are a verification model. The image you see has a candidate \
target highlighted with a red rectangle. Classify whether the highlighted element is the \
actual target described.

Possible verdicts:
- "is_target": the red-boxed element is clearly the requested target.
- "target_elsewhere": the box is wrong, but the actual target is visible somewhere else.
- "target_not_found": neither the box nor the target appears in this image."""


# ------------------------------- Main class ---------------------------------


class ScreenSeekeR:
    """GUI grounding via cascaded visual search (paper-faithful structure).

    Args:
        model: Claude model ID. Defaults to Sonnet 4.6 per the configured assessment.
        max_depth: Max recursion depth before forcing a direct grounding call.
        min_patch_px: If the shorter image dim drops to this, stop recursing.
        api_key: Override `ANTHROPIC_API_KEY` env var if needed.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_depth: int = 2,
        min_patch_px: int = 480,
        api_key: Optional[str] = None,
    ):
        if anthropic is None:
            raise RuntimeError(
                "The 'anthropic' package is not installed. Run: uv add anthropic"
            )
        if api_key is None and not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set and no api_key argument was provided."
            )
        self.client = (
            anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        )
        self.model = model
        self.max_depth = max_depth
        self.min_patch_px = min_patch_px
        self._api_calls = 0

    # -------- Public entry point --------

    def search(self, image: np.ndarray, target: str) -> ScreenSeekeRResult:
        """Recursively search `image` (BGR ndarray) for `target` (NL description)."""
        self._api_calls = 0
        h, w = image.shape[:2]
        result = self._search(
            image, target, depth=0, offset=(0, 0), orig_size=(w, h)
        )
        result.api_calls = self._api_calls
        return result

    # -------- Recursive core --------

    def _search(
        self,
        image: np.ndarray,
        target: str,
        depth: int,
        offset: Tuple[int, int],
        orig_size: Tuple[int, int],
    ) -> ScreenSeekeRResult:
        h, w = image.shape[:2]
        logger.debug(
            "ScreenSeekeR depth=%d offset=%s size=(%d,%d)", depth, offset, w, h
        )

        # Base case: image is small enough or recursion bottomed out -> grounder.
        if depth >= self.max_depth or min(h, w) <= self.min_patch_px:
            return self._direct_ground(image, target, depth, offset)

        try:
            plan = self._plan(image, target)
        except Exception as e:
            logger.warning("Planner call failed: %s", e)
            return ScreenSeekeRResult(
                found=False, depth=depth, reason=f"Planner error: {e}"
            )

        # If the planner returns nothing, fall through to a direct grounding pass.
        if not plan.candidates:
            return self._direct_ground(image, target, depth, offset)

        # Score: planner already orders by probability; we just drop overlapping boxes.
        candidates_px = [_norm_to_px(c, w, h) for c in plan.candidates]
        candidates_px = _nms(candidates_px, iou_threshold=0.5)

        for cand_box in candidates_px:
            crop, crop_offset = _crop(image, cand_box, pad_px=20)
            abs_offset = (offset[0] + crop_offset[0], offset[1] + crop_offset[1])
            sub_result = self._search(
                crop, target, depth + 1, abs_offset, orig_size
            )
            if not sub_result.found:
                continue

            # Verify the candidate region (not the leaf bbox) is actually the target.
            # Cheap insurance against the planner picking a confidently-wrong region.
            if self._verify(image, cand_box, target):
                return sub_result
            logger.debug(
                "Result-check rejected candidate %s; trying next.", cand_box
            )

        return ScreenSeekeRResult(
            found=False,
            depth=depth,
            reason="No candidate produced a verified target after recursion.",
        )

    def _direct_ground(
        self,
        image: np.ndarray,
        target: str,
        depth: int,
        offset: Tuple[int, int],
    ) -> ScreenSeekeRResult:
        try:
            g = self._ground(image, target)
        except Exception as e:
            logger.warning("Grounder call failed: %s", e)
            return ScreenSeekeRResult(
                found=False, depth=depth, reason=f"Grounder error: {e}"
            )

        if not g.found:
            return ScreenSeekeRResult(found=False, depth=depth, reason=g.rationale)

        h, w = image.shape[:2]
        x1_p = _denorm(g.x1, w)
        y1_p = _denorm(g.y1, h)
        x2_p = _denorm(g.x2, w)
        y2_p = _denorm(g.y2, h)
        x1_abs, y1_abs = x1_p + offset[0], y1_p + offset[1]
        x2_abs, y2_abs = x2_p + offset[0], y2_p + offset[1]
        cx = (x1_abs + x2_abs) // 2
        cy = (y1_abs + y2_abs) // 2

        return ScreenSeekeRResult(
            found=True,
            center=(cx, cy),
            bbox=(x1_abs, y1_abs, x2_abs, y2_abs),
            confidence=g.confidence,
            depth=depth,
            reason=g.rationale,
        )

    # -------- API calls (one per role) --------

    def _plan(self, image: np.ndarray, target: str) -> PlannerResponse:
        self._api_calls += 1
        msg = self.client.messages.parse(
            model=self.model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": PLANNER_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_format=PlannerResponse,
            messages=[
                {
                    "role": "user",
                    "content": [
                        _image_block(image),
                        {
                            "type": "text",
                            "text": (
                                f"Target: {target}\n\n"
                                "List 1-5 candidate regions ordered by descending "
                                "probability."
                            ),
                        },
                    ],
                }
            ],
        )
        return msg.parsed_output

    def _ground(self, image: np.ndarray, target: str) -> GroundResponse:
        self._api_calls += 1
        msg = self.client.messages.parse(
            model=self.model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": GROUNDER_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_format=GroundResponse,
            messages=[
                {
                    "role": "user",
                    "content": [
                        _image_block(image),
                        {
                            "type": "text",
                            "text": (
                                f"Target: {target}\n\nReturn the bounding box."
                            ),
                        },
                    ],
                }
            ],
        )
        return msg.parsed_output

    def _verify(
        self,
        image: np.ndarray,
        bbox_px: Tuple[int, int, int, int],
        target: str,
    ) -> bool:
        """Annotate the candidate with a red box and ask the checker if it matches."""
        annotated = image.copy()
        x1, y1, x2, y2 = bbox_px
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 4)
        try:
            self._api_calls += 1
            msg = self.client.messages.parse(
                model=self.model,
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": CHECKER_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_format=CheckResponse,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            _image_block(annotated),
                            {
                                "type": "text",
                                "text": (
                                    f"Target: {target}\n\n"
                                    "Is the red-boxed element the target?"
                                ),
                            },
                        ],
                    }
                ],
            )
            return msg.parsed_output.verdict == "is_target"
        except Exception as e:
            # Failing the verifier open avoids penalising network blips on the happy
            # path; if both planner and leaf grounder agree, that's already strong.
            logger.warning("Result-check call failed (%s); accepting candidate.", e)
            return True


# ------------------------------- Helpers ------------------------------------


def _image_block(image: np.ndarray) -> dict:
    """Encode a BGR ndarray as a base64 PNG content block, downscaled to fit the API."""
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB) if image.shape[2] == 3 else image
    pil = Image.fromarray(rgb)
    w, h = pil.size
    long_edge = max(w, h)
    if long_edge > MAX_LONG_EDGE_PX:
        scale = MAX_LONG_EDGE_PX / long_edge
        pil = pil.resize(
            (int(w * scale), int(h * scale)), Image.Resampling.LANCZOS
        )
    buf = io.BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(buf.getvalue()).decode("ascii"),
        },
    }


def _denorm(v: int, dim: int) -> int:
    return int(round(v * dim / NORM_MAX))


def _norm_to_px(
    cand: CandidateRegion, w: int, h: int
) -> Tuple[int, int, int, int]:
    x1 = max(0, _denorm(cand.x1, w))
    y1 = max(0, _denorm(cand.y1, h))
    x2 = min(w, _denorm(cand.x2, w))
    y2 = min(h, _denorm(cand.y2, h))
    return (x1, y1, x2, y2)


def _crop(
    image: np.ndarray, bbox: Tuple[int, int, int, int], pad_px: int = 0
) -> Tuple[np.ndarray, Tuple[int, int]]:
    h, w = image.shape[:2]
    x1, y1, x2, y2 = bbox
    x1 = max(0, x1 - pad_px)
    y1 = max(0, y1 - pad_px)
    x2 = min(w, x2 + pad_px)
    y2 = min(h, y2 + pad_px)
    return image[y1:y2, x1:x2].copy(), (x1, y1)


def _iou(b1: Tuple[int, int, int, int], b2: Tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = b1
    bx1, by1, bx2, by2 = b2
    iw = max(0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    if inter == 0:
        return 0.0
    a_area = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    b_area = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0


def _nms(
    boxes: List[Tuple[int, int, int, int]], iou_threshold: float = 0.5
) -> List[Tuple[int, int, int, int]]:
    """Greedy NMS. Assumes input is already in descending priority order."""
    keep: List[Tuple[int, int, int, int]] = []
    for b in boxes:
        if all(_iou(b, k) < iou_threshold for k in keep):
            keep.append(b)
    return keep
