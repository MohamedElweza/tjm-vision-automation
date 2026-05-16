# TJM Vision Automation

Vision-based desktop automation for Windows. Dynamically locates the **Notepad**
desktop icon at any position on a 1920x1080 desktop, double-clicks it, and writes
the first 10 posts from the JSONPlaceholder API to individual `.txt` files in
`%USERPROFILE%\Desktop\tjm-project\`.

**Documentation:**
- [DOCS.md](DOCS.md) — full architecture, module reference, grounding algorithm, CLI reference, performance notes, known limitations.
- [ALTERNATIVES.md](ALTERNATIVES.md) — every design decision: options considered, trade-off tables, why we picked what we picked.

## Prerequisites

- Windows 10 / 11, primary display at **1920x1080** recommended.
- A **Notepad shortcut on your Desktop** named `Notepad`.
- Python 3.10–3.12 (managed by [`uv`](https://github.com/astral-sh/uv)).
- Internet access for the first run (EasyOCR downloads model weights once,
  ~80 MB) and to reach `jsonplaceholder.typicode.com`.

## Install

```powershell
# in the project root
uv sync
```

## Run

```powershell
# Full workflow: ground icon -> launch Notepad -> write & save 10 posts
uv run tjm-run

# Faster: keep Notepad open and just re-use it between posts
uv run tjm-run --reuse-window

# Different icon (the grounding is label-driven, so the same code works)
uv run tjm-run --label "Notepad++"

# Generate an annotated screenshot showing what the grounder found
uv run tjm-demo
uv run tjm-demo --label "Recycle Bin" --out screenshots/bin.png

# One-time: capture a template image of Notepad from your live desktop
# (used by the template-matching fallback when OCR fails)
uv run tjm-capture-template
uv run tjm-run --template assets/notepad.png
```

`tjm-run` prints a live log and exits 0 if every post was saved. Files land in
`Desktop\tjm-project\post_<id>.txt`.

## Capturing the three required demo screenshots

After `uv sync`, move the Notepad shortcut on your desktop and run:

```powershell
# 1. Move the icon to the top-left of the desktop, then:
uv run tjm-demo --out screenshots/01_top_left.png

# 2. Move it to the bottom-right:
uv run tjm-demo --out screenshots/02_bottom_right.png

# 3. Move it to the center:
uv run tjm-demo --out screenshots/03_center.png
```

Each output PNG shows the full desktop with a green bounding box around the
detected icon, a red crosshair at the click point, and yellow annotations for
every text the OCR saw (helpful for debugging false negatives).

## How the grounding works

The grounding strategy is **label-first, image-fallback** — generalisable
because the prompt was deliberately about *any* icon, not just Notepad.

1. **OCR pass.** Capture the desktop, run EasyOCR across the full frame, and
   collect every text box with its bounding rectangle and confidence.
2. **Label match.** Filter for the target label (`Notepad`, by default).
   Exact case-insensitive matches are preferred over substring matches — this
   is how we distinguish `Notepad` from `Notepad++` when both icons exist.
3. **Geometric inversion.** A Windows desktop icon renders its label
   centered *below* the icon image. So once we have the label box, the
   icon's clickable center is `(label_x, label_y - 40px)`. We click there.
4. **Verification.** Wait for a window whose title ends in `- Notepad` to
   appear (via `pygetwindow`). If it doesn't, we count that as a failure and
   retry from step 1.
5. **Fallback.** If OCR fails entirely (extreme contrast, language pack
   missing, etc.), `--template path/to/notepad.png` activates OpenCV
   `matchTemplate` as a backup.

### Why this approach over alternatives

| Approach | Pros | Cons | Used? |
|---|---|---|---|
| Template matching | Fast, deterministic | Needs a per-icon image; brittle to theme/scale | Fallback |
| OCR + label | Works for any **labelled** UI element with no per-icon training | Slower (~0.5–1.5s); needs readable text | **Primary** |
| Pixel-perfect icon paths | Trivial for Notepad (`notepad.exe`) | Sidesteps the actual challenge | Rejected |
| VLM grounding (paper) | Highest flexibility | Needs API key / heavy local model | Out of scope (no key) |

### Pop-up handling

`popup_handler.dismiss_popups()` is invoked between failed ground attempts.
It OCRs the screen and clicks the center of any text box whose content is in
a dismiss-words list (`Close`, `Cancel`, `Skip`, `Not now`, …). This works
for pop-ups we've never seen before, because we never relied on their
appearance — only on the universal convention that they have a dismiss
button somewhere on screen.

### Error handling & retries

- Grounding retries up to **3 times** with **1 s** delay between attempts.
- Between retries we dismiss any pop-ups that may be covering the desktop.
- Launch is verified by polling top-level window titles; 15 s timeout.
- API failure → local fallback posts (`Offline title 1` … `10`) so the
  vision pipeline can still be tested without network.
- Existing files in `Desktop\tjm-project\` are silently overwritten by
  pressing **Enter** on Notepad's "replace?" prompt.

## Project layout

```
tjm-vision-automation/
├── pyproject.toml          # uv config + entry points
├── README.md
├── src/tjm_automation/
│   ├── api_client.py       # JSONPlaceholder client
│   ├── capture_template.py # tjm-capture-template: snip icon -> assets/<label>.png
│   ├── demo.py             # tjm-demo: annotated screenshot generator
│   ├── grounding.py        # OCR label grounding + template fallback
│   ├── main.py             # tjm-run: full workflow
│   ├── notepad.py          # Type / save-as / close primitives
│   ├── ocr_engine.py       # EasyOCR wrapper with lazy init
│   ├── popup_handler.py    # Generic pop-up dismissal
│   └── screen.py           # mss screenshot helpers
├── assets/                 # Captured icon templates (gitignored except .gitkeep)
├── screenshots/            # Output of tjm-demo
├── tests/                  # pytest unit tests
└── .github/workflows/ci.yml
```

## Discussion notes (for the interview)

**When detection fails.** The two realistic failure modes I've observed:

- Very busy wallpaper with text behind the icon → OCR picks up the wrong box.
  Mitigation: prefer the higher-confidence exact match; the heuristic above
  the label is robust because the wallpaper text wouldn't have an icon above it.
- Dark theme + dark wallpaper → label contrast collapses. Mitigation: run
  EasyOCR with `min_confidence=0.2`, or pass `--template` for fallback.

**Latency.** End-to-end ground call is ~0.6 s warm (EasyOCR initialisation
~3 s cold; cached after first call). At 1920x1080, full-frame OCR is the
bottleneck; restricting to the leftmost ~600px would cut this roughly in
half for typical icon layouts, at the cost of generality.

**Scaling.** Extending to arbitrary icons requires no code change — pass
`--label "<text>"`. For unlabelled or icon-only targets, the template
fallback covers the gap; for true zero-shot grounding, swap `grounding.py`'s
primary path for a VLM call (e.g. Claude's `computer-use` tool or
GPT-4o with `detail=high`).

**Different resolutions.** The `LABEL_TO_ICON_OFFSET_PX` constant in
`grounding.py` is tuned for 1920x1080 with default icon size. For other
DPIs, scale this by `dpi_scale * icon_size_factor`. The rest of the pipeline
is resolution-agnostic.

**Light/dark themes.** EasyOCR handles both reasonably; if accuracy drops,
binarising the screenshot with adaptive thresholding before OCR helps.
