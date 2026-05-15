"""Notepad-specific automation: launch verification, type, save, close."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import pyautogui

try:
    import pygetwindow as gw
except Exception:
    gw = None

logger = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


def wait_for_notepad_window(timeout: float = 15.0) -> bool:
    """Wait for a Notepad window to appear and bring it to the foreground."""
    if gw is None:
        time.sleep(2.0)
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        w = get_notepad_window()
        if w is not None:
            try:
                w.activate()
            except Exception:
                pass
            return True
        time.sleep(0.3)
    return False


def get_notepad_window():
    """Return the first Notepad window, or None."""
    if gw is None:
        return None
    for w in gw.getAllWindows():
        title = (w.title or "").lower()
        if (
            title.endswith(" - notepad")
            or title == "notepad"
            or "untitled" in title and "notepad" in title
        ):
            return w
    return None


def _set_clipboard(text: str) -> None:
    """Set Windows clipboard using PowerShell (no extra deps)."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard"],
            input=text,
            text=True,
            timeout=10,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or "Set-Clipboard failed")
    except Exception as e:
        logger.warning("Clipboard set failed (%s); falling back to typewrite.", e)
        pyautogui.typewrite(text, interval=0.005)


def write_text_in_notepad(text: str) -> None:
    """Clear current content and write new text via clipboard paste."""
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    _set_clipboard(text)
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.25)


def save_as(file_path: Path, overwrite: bool = True) -> None:
    """Save the current Notepad buffer to file_path via the Save As dialog."""
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    pyautogui.hotkey("ctrl", "shift", "s")
    time.sleep(1.5)

    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    _set_clipboard(str(file_path))
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.3)

    pyautogui.press("enter")
    time.sleep(0.8)

    if overwrite and file_path.exists():
        pyautogui.press("enter")
        time.sleep(0.4)


def close_notepad() -> None:
    """Close Notepad. Press 'Don't save' (Alt+N) if a save prompt appears."""
    pyautogui.hotkey("alt", "f4")
    time.sleep(0.6)
    pyautogui.hotkey("alt", "n")
    time.sleep(0.4)


def is_notepad_running() -> bool:
    """Best-effort check for an open Notepad window."""
    return get_notepad_window() is not None
