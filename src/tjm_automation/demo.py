"""Demo script: capture screen, ground the Notepad icon, save annotated PNG.

Usage:
    uv run tjm-demo                       # writes screenshots/grounding_<ts>.png
    uv run tjm-demo --label Notepad
    uv run tjm-demo --label "Recycle Bin" --out screenshots/bin.png
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

from .grounding import annotate_image, ground_icon
from .screen import capture_desktop, restore_windows, show_desktop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Annotate an icon grounding attempt.")
    parser.add_argument("--label", default="Notepad", help="Icon label to ground.")
    parser.add_argument(
        "--template",
        default=None,
        help="Optional path to a template image to fall back to.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output PNG path. Default: screenshots/grounding_<label>_<ts>.png",
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=2,
        help="Seconds to wait before grabbing the screenshot.",
    )
    parser.add_argument(
        "--no-show-desktop",
        action="store_true",
        help="Don't minimize windows before capture (use to ground a popup/dialog).",
    )
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="Don't restore windows after capture.",
    )
    args = parser.parse_args(argv)

    if not args.no_show_desktop:
        print("Minimizing windows to expose the real desktop...")
        show_desktop()

    if args.countdown > 0:
        print(f"Capturing in {args.countdown}s...")
        for i in range(args.countdown, 0, -1):
            print(f"  {i}...")
            time.sleep(1)

    print("Capturing screen...")
    image = capture_desktop()

    print(f"Grounding label='{args.label}'...")
    result = ground_icon(image, args.label, template_path=args.template)
    print(f"  found={result.found} method={result.method} reason={result.reason}")
    if result.found:
        print(f"  center={result.center} bbox={result.bbox} conf={result.confidence:.2f}")

    annotated = annotate_image(image, result, label=args.label)

    if args.out is None:
        ts = int(time.time())
        safe_label = args.label.replace(" ", "_").lower()
        out_path = Path("screenshots") / f"grounding_{safe_label}_{ts}.png"
    else:
        out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), annotated)
    print(f"Wrote {out_path}")

    if not args.no_show_desktop and not args.no_restore:
        restore_windows()

    return 0 if result.found else 2


if __name__ == "__main__":
    sys.exit(main())
