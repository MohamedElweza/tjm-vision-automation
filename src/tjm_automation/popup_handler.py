"""Generic pop-up dismissal.

Approach: OCR the screen, look for any window with a small button labelled
"Close", "Cancel", "Dismiss", "No", "Skip", or "X" near a corner. Click it.

This is intentionally label-driven (not appearance-driven) so we can dismiss
unknown pop-ups whose visuals we've never seen.
"""

from __future__ import annotations

import time
from typing import Sequence

import pyautogui

from .ocr_engine import read_text
from .screen import capture_desktop

DEFAULT_DISMISS_LABELS: tuple[str, ...] = (
    "close",
    "cancel",
    "dismiss",
    "no thanks",
    "not now",
    "skip",
    "later",
    "ok",
)


def dismiss_popups(
    dismiss_labels: Sequence[str] = DEFAULT_DISMISS_LABELS,
    max_dismissals: int = 3,
    settle_delay: float = 0.6,
) -> int:
    """Try to dismiss any unexpected pop-ups by OCR-matching a dismiss button.

    Returns the number of pop-ups dismissed.
    """
    dismissed = 0
    dismiss_norm = [lbl.lower() for lbl in dismiss_labels]

    for _ in range(max_dismissals):
        screenshot = capture_desktop()
        boxes = read_text(screenshot, min_confidence=0.4)
        target = None
        for box in boxes:
            t = box.text.strip().lower()
            if t in dismiss_norm or any(t == lbl for lbl in dismiss_norm):
                target = box
                break
        if target is None:
            break

        cx, cy = target.center
        pyautogui.click(cx, cy)
        dismissed += 1
        time.sleep(settle_delay)

    return dismissed
