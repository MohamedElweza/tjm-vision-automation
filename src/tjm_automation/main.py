"""Main entry: full Notepad automation across the first 10 JSONPlaceholder posts."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import pyautogui
from rich.console import Console
from rich.logging import RichHandler

from .api_client import Post, fetch_posts, format_post
from .grounding import GroundingResult, ground_icon
from .notepad import (
    close_notepad,
    is_notepad_running,
    list_open_window_titles,
    save_as,
    wait_for_notepad_window,
    write_text_in_notepad,
)
from .notifications import notify_completion
from .popup_handler import dismiss_popups
from .screen import capture_desktop, save_screenshot, show_desktop

console = Console()
logger = logging.getLogger("tjm")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def desktop_dir() -> Path:
    """Return the Windows Shell "Desktop" folder, respecting OneDrive redirection.

    Falls back to %USERPROFILE%\\Desktop only if the Shell API call fails.
    """
    try:
        import ctypes
        from ctypes import wintypes

        CSIDL_DESKTOPDIRECTORY = 0x10
        SHGFP_TYPE_CURRENT = 0
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(
            None, CSIDL_DESKTOPDIRECTORY, None, SHGFP_TYPE_CURRENT, buf
        )
        if buf.value:
            return Path(buf.value)
    except Exception:
        pass
    user_profile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(user_profile) / "Desktop"


def output_dir() -> Path:
    out = desktop_dir() / "tjm-project"
    out.mkdir(parents=True, exist_ok=True)
    return out


def attempt_ground(label: str, template: Optional[str], attempts: int = 3,
                   delay: float = 1.0, save_debug: bool = False) -> Optional[GroundingResult]:
    """Try to ground the icon with retries, optionally dismissing pop-ups between tries."""
    last: Optional[GroundingResult] = None
    for i in range(1, attempts + 1):
        logger.info("Grounding '%s' (attempt %d/%d)...", label, i, attempts)
        # Expose the real desktop so OCR can't latch onto text inside an editor,
        # terminal, or browser that happens to contain our label as a substring.
        show_desktop()
        image = capture_desktop()
        if save_debug:
            save_screenshot(image, Path("debug") / f"attempt_{i}.png")
        result = ground_icon(image, label, template_path=template)
        last = result
        if result.found:
            logger.info("  -> Found '%s' at %s (confidence=%.0f%%, method=%s).",
                        label, result.center, result.confidence * 100, result.method)
            return result

        logger.warning("  -> Icon not found: %s", result.reason)
        n_dismissed = dismiss_popups()
        if n_dismissed:
            logger.info("  -> Dismissed %d pop-up(s) before next attempt.", n_dismissed)
        time.sleep(delay)
    return last


def launch_notepad_via_icon(label: str, template: Optional[str],
                            attempts: int = 3) -> bool:
    """Ground the icon and double-click it. Validate Notepad launched."""
    result = attempt_ground(label, template, attempts=attempts)
    if result is None or not result.found or result.center is None:
        logger.error(
            "\n"
            "  Could not find the '%s' icon on your desktop after %d attempt(s).\n"
            "\n"
            "  Things to check:\n"
            "    1. Press Win+D to make sure the desktop is fully visible.\n"
            "    2. Confirm that a '%s' shortcut actually exists on the desktop.\n"
            "    3. If the icon has a different label (e.g. 'Text Editor'),\n"
            "       re-run with:  --label \"Text Editor\"\n"
            "    4. If the label is hard to read (dark theme, small icons),\n"
            "       run  tjm-capture-template  while the icon is visible to\n"
            "       create a visual fallback, then re-run with:  --template template.png",
            label, attempts, label,
        )
        return False

    cx, cy = result.center
    logger.info("Double-clicking (%d, %d).", cx, cy)
    pyautogui.moveTo(cx, cy, duration=0.2)
    # interval=0.1 gives Windows time to register two separate clicks rather
    # than coalescing them into one fast click on slower machines.
    pyautogui.doubleClick(cx, cy, interval=0.1)

    if not wait_for_notepad_window(timeout=30.0):
        debug_path = Path("debug") / f"launch_failed_{int(time.time())}.png"
        save_screenshot(capture_desktop(), debug_path)
        titles = list_open_window_titles()
        logger.error(
            "\n"
            "  The Notepad icon was found and double-clicked, but Notepad\n"
            "  didn't open within 30 seconds.\n"
            "\n"
            "  Things to check:\n"
            "    1. Did a dialog appear (e.g. 'How do you want to open this')?  \n"
            "       Dismiss it, then re-run.\n"
            "    2. Try double-clicking the Notepad icon yourself to confirm it opens.\n"
            "    3. On a slow machine try:  --attempts 5  to allow more retries.\n"
            "    4. A debug screenshot was saved to:  %s\n"
            "  Windows open at the time of failure: %s",
            debug_path, titles or ["(none detected)"],
        )
        dismiss_popups()
        return False

    logger.info("Notepad is running.")
    return True


def process_one_post(post: Post, out_dir: Path, max_attempts: int = 2) -> bool:
    """Write a single post into Notepad and save it, with retry on save failure."""
    text = format_post(post)
    file_path = out_dir / f"post_{post.id}.txt"

    for attempt in range(1, max_attempts + 1):
        if not is_notepad_running():
            logger.error(
                "\n"
                "  Notepad closed unexpectedly before writing post %d.\n"
                "  It may have crashed or been closed manually during the run.\n"
                "  The automation will try to re-open it for the next post.",
                post.id,
            )
            return False

        if attempt > 1:
            # Dismiss any stuck dialog left over from a failed save before retrying.
            pyautogui.press("escape")
            time.sleep(0.4)
            logger.info("Retrying save for post %d (attempt %d/%d)...",
                        post.id, attempt, max_attempts)

        write_text_in_notepad(text)
        save_as(file_path, overwrite=True)

        # Poll for the saved file. OneDrive-backed paths can be slow to flush.
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if file_path.exists() and file_path.stat().st_size > 0:
                logger.info("Saved %s (%d bytes).", file_path.name, file_path.stat().st_size)
                return True
            time.sleep(0.25)

        if attempt < max_attempts:
            logger.warning(
                "Post %d: file didn't appear on disk after Save As — "
                "dismissing any stuck dialog and retrying.",
                post.id,
            )

    logger.error(
        "\n"
        "  Could not save post %d after %d attempt(s).\n"
        "\n"
        "  Things to check:\n"
        "    1. Is the output folder writable?\n"
        "       Folder: %s\n"
        "    2. Is a 'Save As' dialog still open on screen? Press Escape and re-run.\n"
        "    3. If the Desktop is on OneDrive and syncing slowly, try:\n"
        "       --reuse-window  (keeps Notepad open, reduces Save As calls)",
        post.id, max_attempts, file_path.parent,
    )
    return False


def run(label: str, template: Optional[str], limit: int,
        attempts: int, reuse_window: bool, notify: bool = True) -> int:
    out = output_dir()
    logger.info("Output directory: %s", out)

    posts = fetch_posts(limit=limit)
    logger.info("Fetched %d posts.", len(posts))

    succeeded = 0
    failed_ids: list[int] = []

    for idx, post in enumerate(posts, start=1):
        logger.info("[%d/%d] Post id=%d", idx, len(posts), post.id)

        if not is_notepad_running():
            ok = launch_notepad_via_icon(label, template, attempts=attempts)
            if not ok:
                # Abort the whole run: if the icon can't be grounded once, it
                # won't ground on the next post either — fail fast instead of
                # repeating the same error for every remaining post.
                remaining = [p.id for p in posts[idx - 1:]]
                failed_ids.extend(remaining)
                logger.error(
                    "Aborting run: could not launch Notepad. "
                    "Skipping %d remaining post(s): %s",
                    len(remaining) - 1, remaining[1:] or "(none)",
                )
                break

        try:
            if process_one_post(post, out):
                succeeded += 1
            else:
                failed_ids.append(post.id)
        except Exception as e:
            logger.exception(
                "Unexpected error while processing post %d: %s\n"
                "  The automation will continue with the next post.",
                post.id, e,
            )
            failed_ids.append(post.id)
        finally:
            if not reuse_window:
                close_notepad()
                time.sleep(0.5)

    total = len(posts)
    if succeeded == total:
        logger.info("All %d posts saved successfully.", total)
    else:
        logger.info("%d of %d posts saved.", succeeded, total)

    if failed_ids:
        logger.warning(
            "\n"
            "  %d post(s) were not saved: ids %s\n"
            "  Re-run the same command to retry — already-saved posts will be overwritten\n"
            "  cleanly, so it is safe to run again.",
            len(failed_ids), failed_ids,
        )

    hint = _failure_hint(succeeded, total, failed_ids)
    if notify:
        try:
            notify_completion(succeeded, total, out, failed_ids, hint=hint)
        except Exception as e:
            logger.debug("Completion notification failed: %s", e)

    return 0 if succeeded == total else 1


def _failure_hint(succeeded: int, total: int, failed_ids: list[int]) -> str:
    if succeeded == total:
        return ""
    if succeeded == 0:
        return (
            "Nothing was saved. Most likely the Notepad icon could not be found "
            "on the desktop, or Notepad failed to open.\n\n"
            "Quick fixes:\n"
            "  • Press Win+D and confirm a Notepad shortcut is on the desktop.\n"
            "  • If the icon label differs, re-run with: --label \"YourLabel\"\n"
            "  • Run  tjm-capture-template  to add a visual fallback."
        )
    return (
        f"{len(failed_ids)} post(s) were not saved: ids {failed_ids}\n\n"
        "Re-run the same command to retry the failed posts.\n"
        "Already-saved files will be overwritten cleanly."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vision-based Notepad automation.")
    parser.add_argument("--label", default="Notepad", help="Desktop icon label.")
    parser.add_argument("--template", default=None,
                        help="Optional template image path for fallback.")
    parser.add_argument("--limit", type=int, default=10,
                        help="Number of posts to process.")
    parser.add_argument("--attempts", type=int, default=3,
                        help="Grounding retry attempts per launch.")
    parser.add_argument("--reuse-window", action="store_true",
                        help="Don't close Notepad between posts (much faster).")
    parser.add_argument("--no-notify", action="store_true",
                        help="Don't show the end-of-run popup + beep.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    console.rule("[bold]TJM Vision Automation[/bold]")
    console.print(f"Label:    [cyan]{args.label}[/cyan]")
    console.print(f"Posts:    [cyan]{args.limit}[/cyan]")
    console.print(f"Attempts: [cyan]{args.attempts}[/cyan]")
    console.print(f"Reuse:    [cyan]{args.reuse_window}[/cyan]")
    console.rule()

    return run(
        label=args.label,
        template=args.template,
        limit=args.limit,
        attempts=args.attempts,
        reuse_window=args.reuse_window,
        notify=not args.no_notify,
    )


if __name__ == "__main__":
    sys.exit(main())
