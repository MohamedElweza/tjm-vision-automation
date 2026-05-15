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
            logger.info("  -> hit: center=%s conf=%.2f via %s",
                        result.center, result.confidence, result.method)
            return result

        logger.warning("  -> miss: %s", result.reason)
        n_dismissed = dismiss_popups()
        if n_dismissed:
            logger.info("Dismissed %d pop-up(s) before retry.", n_dismissed)
        time.sleep(delay)
    return last


def launch_notepad_via_icon(label: str, template: Optional[str],
                            attempts: int = 3) -> bool:
    """Ground the icon and double-click it. Validate Notepad launched."""
    result = attempt_ground(label, template, attempts=attempts)
    if result is None or not result.found or result.center is None:
        logger.error("Could not ground '%s' after retries.", label)
        return False

    cx, cy = result.center
    logger.info("Double-clicking (%d, %d).", cx, cy)
    pyautogui.moveTo(cx, cy, duration=0.2)
    # interval=0.1 gives Windows time to register two separate clicks rather
    # than coalescing them into one fast click on slower machines.
    pyautogui.doubleClick(cx, cy, interval=0.1)

    if not wait_for_notepad_window(timeout=30.0):
        logger.error("Notepad did not appear within 30s.")
        titles = list_open_window_titles()
        logger.error("Open window titles at failure: %s", titles)
        debug_path = Path("debug") / f"launch_failed_{int(time.time())}.png"
        save_screenshot(capture_desktop(), debug_path)
        logger.error("Wrote debug screenshot: %s", debug_path)
        dismiss_popups()
        return False

    logger.info("Notepad is running.")
    return True


def process_one_post(post: Post, out_dir: Path) -> bool:
    """Write a single post into Notepad and save it."""
    text = format_post(post)
    file_path = out_dir / f"post_{post.id}.txt"

    if not is_notepad_running():
        logger.error("Notepad not running when trying to write post %d.", post.id)
        return False

    write_text_in_notepad(text)
    save_as(file_path, overwrite=True)

    time.sleep(0.5)
    if not file_path.exists():
        logger.error("Save failed for %s", file_path)
        return False
    logger.info("Saved %s (%d bytes).", file_path.name, file_path.stat().st_size)
    return True


def run(label: str, template: Optional[str], limit: int,
        attempts: int, reuse_window: bool) -> int:
    out = output_dir()
    logger.info("Output directory: %s", out)

    try:
        posts = fetch_posts(limit=limit)
        logger.info("Fetched %d posts from JSONPlaceholder.", len(posts))
    except Exception as e:
        logger.error("API unavailable (%s). Using local fallback posts.", e)
        posts = [
            Post(id=i, title=f"Offline title {i}",
                 body=f"This is fallback body text {i}.\nLine 2.")
            for i in range(1, limit + 1)
        ]

    succeeded = 0
    failed_ids: list[int] = []

    for idx, post in enumerate(posts, start=1):
        logger.info("[%d/%d] Post id=%d", idx, len(posts), post.id)

        if not is_notepad_running():
            ok = launch_notepad_via_icon(label, template, attempts=attempts)
            if not ok:
                failed_ids.append(post.id)
                continue

        try:
            if process_one_post(post, out):
                succeeded += 1
            else:
                failed_ids.append(post.id)
        except Exception as e:
            logger.exception("Unhandled error for post %d: %s", post.id, e)
            failed_ids.append(post.id)
        finally:
            if not reuse_window:
                close_notepad()
                time.sleep(0.5)

    logger.info("Done. %d/%d posts saved.", succeeded, len(posts))
    if failed_ids:
        logger.warning("Failed post ids: %s", failed_ids)
    return 0 if succeeded == len(posts) else 1


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
    )


if __name__ == "__main__":
    sys.exit(main())
