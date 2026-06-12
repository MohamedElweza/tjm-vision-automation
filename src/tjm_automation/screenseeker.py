"""ScreenSeekeR: cascaded visual search for GUI grounding.

Implements the algorithm from arXiv:2504.07981 (Li et al., 2025 — ScreenSpot-Pro).
Three-tier decomposition driven by a single VLM:

    1. Planner — full screenshot + natural-language target -> ranked candidate regions.
    2. Grounder — crop of a candidate region -> precise bounding box of the target.
    3. Result-checker — verifies the candidate is actually the target before committing.

The planner result is scored with NMS, the top candidate is cropped, and the algorithm
recurses on the crop until the patch is small enough (or max_depth is reached) to
direct-ground. This narrowing-search pattern is the paper's key insight: it raised
ScreenSpot-Pro accuracy from 18.9 percent (single-pass grounding) to 48.1 percent.

The paper uses GPT-4o as planner and OS-Atlas-7B as grounder. This implementation supports
two interchangeable backends via the `provider` argument:

    - "google"    -> Gemini 1.5 Flash via google-generativeai (free tier, no card).
    - "anthropic" -> Claude Sonnet 4.6 via the Anthropic API (paid, higher quality).
    - "auto"      -> Prefer Google if GEMINI_API_KEY/GOOGLE_API_KEY is set, else Anthropic.

Off-the-shelf OS-Atlas-7B would require a GPU not available in this assessment's runtime,
so we substitute a hosted VLM. The cascade STRUCTURE (planner -> grounder -> score -> crop
-> recurse -> verify) is preserved exactly; only the underlying model changes.

Designed to drop in alongside the existing OCR pipeline as a high-flexibility option;
falls through to OCR if no API key is configured. See `grounding.ground_icon()`.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple, Type

import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
except ImportError:
    google_genai = None  # type: ignore
    google_genai_types = None  # type: ignore

logger = logging.getLogger(__name__)

# Hosted VLMs scale better to high resolution than they used to. We keep a generous
# long-edge cap so a 4K display gets downscaled, but for typical 1920x1080 desktops
# the image is sent at native resolution — small icons need every pixel they can get.
MAX_LONG_EDGE_PX = 1920

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

COORDINATE SYSTEM — READ CAREFULLY:
- The image's TOP-LEFT corner is (0, 0).
- The image's BOTTOM-RIGHT corner is (1000, 1000) — both x AND y normalized to 0-1000.
- x1 = LEFT edge of the region (smaller x).
- y1 = TOP edge of the region (smaller y).
- x2 = RIGHT edge of the region (larger x).
- y2 = BOTTOM edge of the region (larger y).
- IMPORTANT: format is [x_min, y_min, x_max, y_max], NOT [y_min, x_min, y_max, x_max].
- x is HORIZONTAL (left/right). y is VERTICAL (top/bottom).
- Upper-left region of the image -> x and y values should all be small (e.g. 0-300).
- Lower-right region -> x and y values should all be large (e.g. 700-1000).

Apply these heuristics:
- Use GUI conventions (desktop icons on the left, taskbar at the bottom, system tray \
bottom-right, title bars at the top, etc.) to predict where the target sits.
- For labeled icons (e.g. "Notepad"), the text label is the strongest visual cue.
- Reason about neighbors: related icons or labels tell you the target is nearby.
- Treat popups, modal dialogs, and floating windows as OBSTACLES unless the target is \
inside one of them. Look around them, not through them.
- Return 1-5 candidates in descending probability. Each region should be GENEROUSLY sized \
(at least 200x200 in 0-1000 space) — the goal is to localize an area for a closer look, \
not to draw a tight box around the target."""

GROUNDER_SYSTEM = """You are a GUI grounding model. Given a screenshot (which may be a \
cropped patch of a larger screen) and a natural-language description, return the precise \
bounding box of the target.

COORDINATE SYSTEM — READ CAREFULLY:
- The image's TOP-LEFT corner is (0, 0).
- The image's BOTTOM-RIGHT corner is (1000, 1000) — both x AND y normalized to 0-1000.
- x1 = LEFT edge of the box (smaller x).
- y1 = TOP edge of the box (smaller y).
- x2 = RIGHT edge of the box (larger x).
- y2 = BOTTOM edge of the box (larger y).
- IMPORTANT: the format is [x_min, y_min, x_max, y_max], NOT [y_min, x_min, y_max, x_max].
- x is HORIZONTAL position (left/right). y is VERTICAL position (top/bottom).

Rules:
- If the target is clearly visible, set found=true and return a tight box around its \
CLICKABLE area.
- For Windows desktop icons specifically, the click target is the ICON IMAGE itself, \
which sits ABOVE the text label. Box the icon, not the label.
- If the target is not visible, set found=false and leave coordinates at 0.
- Be precise: the center of your box becomes the click point. If the target is in the \
upper-left of the image, your box's x and y values should all be small."""

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
        provider: "auto" (default), "google", or "anthropic". `auto` picks Google if
            GEMINI_API_KEY/GOOGLE_API_KEY is set, else Anthropic.
        model: Override the per-provider default model ID.
            - google default: "gemini-1.5-flash" (free tier)
            - anthropic default: "claude-sonnet-4-6"
        max_depth: Max recursion depth before forcing a direct grounding call.
        min_patch_px: If the shorter image dim drops to this, stop recursing.
        api_key: Override the relevant env var for the chosen provider.
    """

    PROVIDER_DEFAULT_MODELS = {
        "google": "gemini-2.5-flash",
        "anthropic": "claude-sonnet-4-6",
    }

    def __init__(
        self,
        provider: str = "auto",
        model: Optional[str] = None,
        max_depth: int = 2,
        min_patch_px: int = 480,
        api_key: Optional[str] = None,
    ):
        provider = self._resolve_provider(provider)
        self.provider = provider
        self.model = model or self.PROVIDER_DEFAULT_MODELS[provider]
        self._init_client(api_key)
        self.max_depth = max_depth
        self.min_patch_px = min_patch_px
        self._api_calls = 0

    @staticmethod
    def _resolve_provider(provider: str) -> str:
        if provider != "auto":
            if provider not in ScreenSeekeR.PROVIDER_DEFAULT_MODELS:
                raise ValueError(
                    f"Unknown provider {provider!r}. Use 'auto', 'google', or 'anthropic'."
                )
            return provider
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return "google"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        raise RuntimeError(
            "No VLM API key found. Set GEMINI_API_KEY (free, "
            "https://aistudio.google.com/app/apikey) or ANTHROPIC_API_KEY (paid)."
        )

    def _init_client(self, api_key: Optional[str]) -> None:
        if self.provider == "google":
            if google_genai is None:
                raise RuntimeError(
                    "The 'google-genai' package is not installed. "
                    "Run: uv add google-genai"
                )
            key = (
                api_key
                or os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")
            )
            if not key:
                raise RuntimeError(
                    "Google provider selected but no API key. Set GEMINI_API_KEY."
                )
            self._google_client = google_genai.Client(api_key=key)
        else:  # anthropic
            if anthropic is None:
                raise RuntimeError(
                    "The 'anthropic' package is not installed. Run: uv add anthropic"
                )
            if api_key is None and not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY is not set.")
            self.client = (
                anthropic.Anthropic(api_key=api_key)
                if api_key
                else anthropic.Anthropic()
            )

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

    # -------- VLM dispatch (provider-agnostic) --------

    def _vlm_call(
        self,
        system_prompt: str,
        image: np.ndarray,
        user_text: str,
        schema: Type[BaseModel],
        max_tokens: int = 2048,
    ) -> BaseModel:
        """Single VLM round-trip. Dispatches to the active provider."""
        self._api_calls += 1
        png_bytes = _image_png_bytes(image)
        if self.provider == "google":
            return self._call_google(system_prompt, png_bytes, user_text, schema)
        return self._call_anthropic(
            system_prompt, png_bytes, user_text, schema, max_tokens
        )

    def _call_anthropic(
        self,
        system_prompt: str,
        png_bytes: bytes,
        user_text: str,
        schema: Type[BaseModel],
        max_tokens: int,
    ) -> BaseModel:
        msg = self.client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_format=schema,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.standard_b64encode(png_bytes).decode(
                                    "ascii"
                                ),
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )
        return msg.parsed_output

    def _call_google(
        self,
        system_prompt: str,
        png_bytes: bytes,
        user_text: str,
        schema: Type[BaseModel],
    ) -> BaseModel:
        # New google-genai SDK: system_instruction lives on the per-call config,
        # not on a model object. response_schema accepts a Pydantic class directly.
        response = self._google_client.models.generate_content(
            model=self.model,
            contents=[
                google_genai_types.Part.from_bytes(
                    data=png_bytes, mime_type="image/png"
                ),
                user_text,
            ],
            config=google_genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        # The SDK populates `response.parsed` with a validated Pydantic instance
        # when response_schema is a BaseModel subclass; `response.text` carries
        # the JSON string as a fallback.
        if getattr(response, "parsed", None) is not None:
            return response.parsed  # type: ignore[return-value]
        return schema.model_validate_json(response.text)

    # -------- One method per algorithmic role --------

    def _plan(self, image: np.ndarray, target: str) -> PlannerResponse:
        return self._vlm_call(
            PLANNER_SYSTEM,
            image,
            f"Target: {target}\n\nList 1-5 candidate regions ordered by descending probability.",
            PlannerResponse,
            max_tokens=2048,
        )  # type: ignore[return-value]

    def _ground(self, image: np.ndarray, target: str) -> GroundResponse:
        return self._vlm_call(
            GROUNDER_SYSTEM,
            image,
            f"Target: {target}\n\nReturn the bounding box.",
            GroundResponse,
            max_tokens=1024,
        )  # type: ignore[return-value]

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
            check = self._vlm_call(
                CHECKER_SYSTEM,
                annotated,
                f"Target: {target}\n\nIs the red-boxed element the target?",
                CheckResponse,
                max_tokens=512,
            )
            return check.verdict == "is_target"  # type: ignore[attr-defined]
        except Exception as e:
            # Failing the verifier open avoids penalising network blips on the happy
            # path; if both planner and leaf grounder agree, that's already strong.
            logger.warning("Result-check call failed (%s); accepting candidate.", e)
            return True


# ------------------------------- Helpers ------------------------------------


def _image_png_bytes(image: np.ndarray) -> bytes:
    """Encode a BGR ndarray as PNG bytes, downscaling so the long edge fits the API."""
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
    return buf.getvalue()


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
