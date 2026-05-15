"""OCR engine wrapper around EasyOCR with a lazily loaded reader."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import List, Tuple

import numpy as np


@dataclass
class TextBox:
    """A detected text region with its bounding box and confidence."""

    text: str
    bbox: Tuple[int, int, int, int]
    confidence: float

    @property
    def center(self) -> Tuple[int, int]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]


@lru_cache(maxsize=1)
def _get_reader():
    import easyocr

    return easyocr.Reader(["en"], gpu=False, verbose=False)


def read_text(image: np.ndarray, min_confidence: float = 0.3) -> List[TextBox]:
    """Run OCR on an image and return all detected text boxes above a confidence threshold.

    Args:
        image: Image as a numpy array (BGR or RGB both work; EasyOCR autodetects).
        min_confidence: Minimum confidence to keep a detection.
    """
    reader = _get_reader()
    raw = reader.readtext(image, detail=1, paragraph=False)
    boxes: List[TextBox] = []
    for poly, text, conf in raw:
        if conf < min_confidence:
            continue
        xs = [int(p[0]) for p in poly]
        ys = [int(p[1]) for p in poly]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        boxes.append(TextBox(text=text.strip(), bbox=bbox, confidence=float(conf)))
    return boxes


def find_text(
    image: np.ndarray,
    query: str,
    min_confidence: float = 0.3,
    exact: bool = False,
) -> List[TextBox]:
    """Find text boxes whose text matches the query (case-insensitive substring by default)."""
    query_norm = query.strip().lower()
    matches: List[TextBox] = []
    for box in read_text(image, min_confidence=min_confidence):
        text_norm = box.text.lower()
        if exact:
            if text_norm == query_norm:
                matches.append(box)
        else:
            if query_norm in text_norm:
                matches.append(box)
    return matches
