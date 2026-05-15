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


def wait_for_notepad_window(timeout: float = 30.0) -> bool:
    """Wait for a Notepad window or notepad.exe process to appear.

    Also brings the window to the foreground if found. Uses both window-title
    enumeration (fast) and a tasklist process check (robust against the Win11
    new Notepad UWP app whose title may briefly be empty during startup).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        w = get_notepad_window()
        if w is not None:
            try:
                w.activate()
            except Exception:
                pass
            return True
        if is_notepad_process_running():
            time.sleep(0.5)
            continue
        time.sleep(0.3)

    # Last chance: process is running but title still unrecognised.
    return is_notepad_process_running()


def get_notepad_window():
    """Return a Notepad window if one is open, else None.

    Matches any window whose title contains 'notepad' but explicitly excludes
    Notepad++ to avoid false positives when both are installed.
    """
    if gw is None:
        return None
    for w in gw.getAllWindows():
        title = (w.title or "").lower().strip()
        if not title:
            continue
        if "notepad++" in title:
            continue
        if "notepad" in title:
            return w
    return None


def is_notepad_process_running() -> bool:
    """True if notepad.exe is in the current process list."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq notepad.exe", "/NH"],
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "notepad.exe" in out.lower()
    except Exception:
        return False


def list_open_window_titles(limit: int = 30) -> list[str]:
    """Return non-empty top-level window titles, useful for launch-failure diagnostics."""
    if gw is None:
        return []
    titles: list[str] = []
    for w in gw.getAllWindows():
        t = (w.title or "").strip()
        if t:
            titles.append(t)
        if len(titles) >= limit:
            break
    return titles


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
    """True if a Notepad window OR notepad.exe process is alive."""
    return get_notepad_window() is not None or is_notepad_process_running()
