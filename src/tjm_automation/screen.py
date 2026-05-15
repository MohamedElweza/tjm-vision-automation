"""Screen capture utilities."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import mss
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


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


def show_desktop(settle_delay: float = 0.5) -> bool:
    """Minimize all visible windows so the real desktop is exposed for capture.

    Prefers the Shell COM API (explicit MinimizeAll, no focus theft) and
    falls back to the Win+M keyboard shortcut. Returns True on success.
    """
    try:
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("Shell.Application")
        shell.MinimizeAll()
        time.sleep(settle_delay)
        return True
    except Exception as e:
        logger.debug("Shell.MinimizeAll failed (%s); trying Win+M hotkey.", e)

    try:
        import pyautogui

        pyautogui.hotkey("winleft", "m")
        time.sleep(settle_delay)
        return True
    except Exception as e:
        logger.warning("Could not minimize windows: %s", e)
        return False


def restore_windows() -> bool:
    """Undo a prior show_desktop() call (restore previously-minimized windows)."""
    try:
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("Shell.Application")
        shell.UndoMinimizeALL()
        return True
    except Exception:
        try:
            import pyautogui

            pyautogui.hotkey("winleft", "shift", "m")
            return True
        except Exception:
            return False


def save_screenshot(image: np.ndarray, path: str | Path, is_bgr: bool = True) -> Path:
    """Save a numpy image array to disk as PNG."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_bgr:
        image = image[:, :, ::-1]
    Image.fromarray(image).save(path)
    return path
