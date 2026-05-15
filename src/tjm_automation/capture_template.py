"""Capture a template image of an icon from the live desktop.

This is a convenience tool. It grounds the icon by OCR label, crops the
estimated icon region from the screenshot, and writes it to `assets/<label>.png`
so `--template` can be used as a fallback later (e.g. when EasyOCR can't
read the label due to a high-contrast dark theme).

Usage:
    uv run tjm-capture-template
    uv run tjm-capture-template --label "Recycle Bin" --pad 12
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

from .grounding import ground_by_label
from .screen import capture_desktop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Crop a template image of a desktop icon and save it to assets/."
    )
    parser.add_argument("--label", default="Notepad", help="Icon label to capture.")
    parser.add_argument(
        "--out",
        default=None,
        help="Output PNG path. Default: assets/<label>.png",
    )
    parser.add_argument(
        "--pad",
        type=int,
        default=8,
        help="Pixels of padding around the icon image when cropping.",
    )
    parser.add_argument(
        "--icon-size",
        type=int,
        default=56,
        help="Estimated icon image height/width in pixels (default sized for "
             "1920x1080 Medium icons).",
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=3,
        help="Seconds to wait before capturing (lets you switch focus to the desktop).",
    )
    args = parser.parse_args(argv)

    if args.countdown > 0:
        print(f"Capturing in {args.countdown}s... show your desktop now.")
        for i in range(args.countdown, 0, -1):
            print(f"  {i}...")
            time.sleep(1)

    image = capture_desktop()
    result = ground_by_label(image, args.label)
    if not result.found or result.center is None:
        print(f"Could not ground '{args.label}': {result.reason}", file=sys.stderr)
        return 2

    cx, cy = result.center
    half = args.icon_size // 2 + args.pad
    h, w = image.shape[:2]
    x1 = max(0, cx - half)
    y1 = max(0, cy - half)
    x2 = min(w, cx + half)
    y2 = min(h, cy + half)

    crop = image[y1:y2, x1:x2]

    if args.out is None:
        safe = args.label.replace(" ", "_").lower()
        out_path = Path("assets") / f"{safe}.png"
    else:
        out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), crop)
    print(f"Saved template ({x2 - x1}x{y2 - y1}) -> {out_path}")
    print(f"Use it later with: --template {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
