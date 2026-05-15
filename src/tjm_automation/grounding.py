"""Dynamic icon grounding.

Approach (label-first, image-fallback):
    1. OCR the screenshot and look for the icon's text label (e.g. "Notepad").
       Desktop icons on Windows render the label centered *below* the image.
       So once we know the label box, the icon center is directly above it.
    2. If OCR fails (label hidden / theme contrast issue), fall back to
       OpenCV template matching against an optional reference image.
    3. Discriminate against look-alikes by exact-name matching when there
       are multiple candidates (e.g. "Notepad" vs "Notepad++").

This is intentionally label-driven so it generalises: to ground a different
icon, change the query string. No pre-trained image template required.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .ocr_engine import TextBox, find_text, read_text

# Heuristic: Windows desktop icons. The bitmap sits above the label.
# At 1920x1080 with default icon size, the icon is roughly ~48px tall
# and the label sits ~6-10px below it. We aim a click slightly above the
# label center to reliably hit the icon image (where the double-click registers).
LABEL_TO_ICON_OFFSET_PX = 40


@dataclass
class GroundingResult:
    """Result of an icon grounding attempt."""

    found: bool
    center: Optional[Tuple[int, int]] = None
    bbox: Optional[Tuple[int, int, int, int]] = None
    method: str = ""
    confidence: float = 0.0
    candidates: Optional[List[TextBox]] = None
    reason: str = ""


def _select_best_label(
    matches: List[TextBox], query: str, prefer_exact: bool = True
) -> Optional[TextBox]:
    """Pick the best matching label, preferring exact (case-insensitive) matches.

    This is how we discriminate Notepad from Notepad++.
    """
    if not matches:
        return None

    query_norm = query.strip().lower()
    exact = [m for m in matches if m.text.strip().lower() == query_norm]
    pool = exact if (prefer_exact and exact) else matches
    return max(pool, key=lambda m: m.confidence)


def ground_by_label(
    image: np.ndarray,
    label: str,
    min_confidence: float = 0.3,
    prefer_exact: bool = True,
) -> GroundingResult:
    """Locate a desktop icon by OCR'ing its text label.

    Args:
        image: BGR or RGB screenshot.
        label: Text label below the icon (e.g. "Notepad").
        min_confidence: OCR confidence threshold.
        prefer_exact: Prefer exact label match to disambiguate similar names.
    """
    matches = find_text(image, label, min_confidence=min_confidence, exact=False)
    if not matches:
        return GroundingResult(
            found=False, method="ocr_label", reason=f"No OCR hit for '{label}'."
        )

    best = _select_best_label(matches, label, prefer_exact=prefer_exact)
    if best is None:
        return GroundingResult(
            found=False, method="ocr_label", candidates=matches, reason="No best match."
        )

    label_cx, label_cy = best.center
    icon_cx = label_cx
    icon_cy = max(0, label_cy - LABEL_TO_ICON_OFFSET_PX)

    half = max(best.width, best.height) // 2 + LABEL_TO_ICON_OFFSET_PX
    bbox = (
        max(0, icon_cx - half),
        max(0, icon_cy - half),
        icon_cx + half,
        icon_cy + half,
    )

    return GroundingResult(
        found=True,
        center=(icon_cx, icon_cy),
        bbox=bbox,
        method="ocr_label",
        confidence=best.confidence,
        candidates=matches,
        reason=f"Matched label '{best.text}' (conf={best.confidence:.2f}).",
    )


def ground_by_template(
    image: np.ndarray,
    template_path: str | Path,
    threshold: float = 0.75,
) -> GroundingResult:
    """Fallback: OpenCV template matching."""
    template_path = Path(template_path)
    if not template_path.exists():
        return GroundingResult(
            found=False,
            method="template",
            reason=f"Template not found at {template_path}.",
        )

    template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    if template is None:
        return GroundingResult(
            found=False, method="template", reason="Failed to read template image."
        )

    if image.shape[2] == 3 and image.dtype == np.uint8:
        haystack = image
    else:
        haystack = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

    result = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    if max_val < threshold:
        return GroundingResult(
            found=False,
            method="template",
            confidence=float(max_val),
            reason=f"Best template score {max_val:.2f} below threshold {threshold}.",
        )

    th, tw = template.shape[:2]
    x, y = max_loc
    bbox = (x, y, x + tw, y + th)
    center = (x + tw // 2, y + th // 2)
    return GroundingResult(
        found=True,
        center=center,
        bbox=bbox,
        method="template",
        confidence=float(max_val),
        reason=f"Template match score={max_val:.2f}.",
    )


def ground_icon(
    image: np.ndarray,
    label: str,
    template_path: Optional[str | Path] = None,
    min_confidence: float = 0.3,
) -> GroundingResult:
    """Top-level grounding: try OCR label, then template fallback."""
    result = ground_by_label(image, label, min_confidence=min_confidence)
    if result.found:
        return result

    if template_path is not None:
        fallback = ground_by_template(image, template_path)
        if fallback.found:
            fallback.reason = f"OCR failed ({result.reason}); template hit."
            return fallback

    result.reason = result.reason or "All grounding methods failed."
    return result


def annotate_image(
    image: np.ndarray,
    result: GroundingResult,
    label: str = "",
) -> np.ndarray:
    """Draw the grounding result onto an image for debug/demo output.

    Returns a BGR-ordered image (OpenCV convention).
    """
    if image.shape[2] == 3:
        canvas = image.copy()
    else:
        canvas = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

    if result.candidates:
        for cand in result.candidates:
            x1, y1, x2, y2 = cand.bbox
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 200, 200), 1)
            cv2.putText(
                canvas,
                f"{cand.text} ({cand.confidence:.2f})",
                (x1, max(0, y1 - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                (0, 200, 200),
                1,
                cv2.LINE_AA,
            )

    if result.found and result.bbox and result.center:
        x1, y1, x2, y2 = result.bbox
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 3)
        cx, cy = result.center
        cv2.drawMarker(
            canvas, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 30, 3, cv2.LINE_AA
        )
        text = f"{label or 'icon'} @ ({cx},{cy}) via {result.method} ({result.confidence:.2f})"
        cv2.putText(
            canvas,
            text,
            (max(0, x1), max(20, y1 - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
    else:
        cv2.putText(
            canvas,
            f"NOT FOUND: {result.reason}",
            (40, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )

    return canvas
