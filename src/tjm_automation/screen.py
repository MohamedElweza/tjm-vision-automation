"""Screen capture utilities."""

from __future__ import annotations

from pathlib import Path

import mss
import numpy as np
from PIL import Image


def capture_desktop() -> np.ndarray:
    """Capture the primary monitor as a BGR numpy array (OpenCV-compatible)."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        img = np.array(raw)
        return img[:, :, :3]


def capture_desktop_rgb() -> np.ndarray:
    """Capture the primary monitor as an RGB numpy array (PIL-compatible)."""
    bgr = capture_desktop()
    return bgr[:, :, ::-1].copy()


def save_screenshot(image: np.ndarray, path: str | Path, is_bgr: bool = True) -> Path:
    """Save a numpy image array to disk as PNG."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_bgr:
        image = image[:, :, ::-1]
    Image.fromarray(image).save(path)
    return path
