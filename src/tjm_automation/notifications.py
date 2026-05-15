"""End-of-run system notifications.

Uses ctypes MessageBoxW (no extra dependency) for a modal popup and
winsound.MessageBeep for the audio cue. Different icons / sounds are
used for success and failure so the user can tell them apart by ear
or from a glance at the dialog icon.
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Win32 MessageBox style flags
_MB_OK = 0x00000000
_MB_ICONINFORMATION = 0x00000040
_MB_ICONERROR = 0x00000010
_MB_TOPMOST = 0x00040000
_MB_SETFOREGROUND = 0x00010000


def _play_sound(success: bool) -> None:
    try:
        import winsound

        if success:
            winsound.MessageBeep(winsound.MB_OK)
        else:
            winsound.MessageBeep(winsound.MB_ICONHAND)
    except Exception as e:
        logger.debug("MessageBeep failed: %s", e)


def _show_message_box(title: str, message: str, success: bool) -> None:
    flags = _MB_OK | _MB_TOPMOST | _MB_SETFOREGROUND
    flags |= _MB_ICONINFORMATION if success else _MB_ICONERROR
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
    except Exception as e:
        logger.warning("MessageBoxW failed: %s", e)


def notify_completion(
    succeeded: int,
    total: int,
    output_dir: Path,
    failed_ids: Iterable[int] = (),
) -> None:
    """Beep + popup at the end of the workflow."""
    failed_list = list(failed_ids)
    success = succeeded == total and not failed_list

    title = (
        "TJM Vision Automation — Done"
        if success
        else "TJM Vision Automation — Finished with errors"
    )
    lines = [
        f"Saved: {succeeded}/{total} posts",
        f"Folder: {output_dir}",
    ]
    if failed_list:
        lines.append(f"Failed post ids: {failed_list}")
    message = "\n".join(lines)

    _play_sound(success)
    _show_message_box(title, message, success)
