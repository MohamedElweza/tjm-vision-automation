# TJM Vision Automation — Project Documentation

Detailed reference for the codebase. Companion to [README.md](README.md) (quickstart) and [ALTERNATIVES.md](ALTERNATIVES.md) (design decisions and the options we rejected).

---

## 1. What the project does

A Python CLI that automates Notepad on Windows by **seeing** the desktop rather than knowing fixed pixel coordinates:

1. Capture a screenshot of the live desktop.
2. Find the `Notepad` icon **wherever it is** (no template image required).
3. Double-click it and verify Notepad actually launched.
4. Fetch 10 posts from the JSONPlaceholder API.
5. For each post: type the content, save as `post_{id}.txt` into `Desktop\tjm-project\`, close Notepad.
6. Show a popup + beep when finished.

The vision part is **not** Notepad-specific. To ground a different icon, change one CLI flag (`--label`). The pipeline was deliberately designed to generalise to any labelled UI element.

---

## 2. High-level pipeline

```
                  ┌─────────────────────────────────────┐
                  │            main.run()               │
                  └──────────────────┬──────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
        ▼                            ▼                            ▼
┌──────────────┐           ┌──────────────────┐         ┌──────────────────┐
│ api_client   │           │  launch via icon │         │ process_one_post │
│ fetch_posts()│           │  (per iteration) │         │ type + save      │
└──────────────┘           └────────┬─────────┘         └──────────────────┘
                                    │
            ┌───────────────────────┼─────────────────────────┐
            ▼                       ▼                         ▼
   ┌──────────────────┐   ┌──────────────────┐    ┌─────────────────────┐
   │ screen           │   │   grounding      │    │   notepad           │
   │  capture_desktop │   │   ground_icon    │    │   wait_for_window   │
   │  show_desktop    │   │   ┌──────────┐   │    │   write_text_in_…   │
   └──────────────────┘   │   │ ocr_engine│  │    │   save_as           │
                          │   └──────────┘   │    │   close_notepad     │
                          └──────────────────┘    └─────────────────────┘
```

Plus orthogonal helpers:
- `popup_handler.dismiss_popups()` — generic OCR-driven cleanup between failed attempts.
- `notifications.notify_completion()` — end-of-run MessageBox + beep.

---

## 3. Module reference

### `screen.py` — screen capture + window management

| Function | Purpose |
|---|---|
| `capture_desktop()` | BGR numpy array of the primary monitor (mss-backed; ~30 ms). |
| `capture_desktop_rgb()` | Same but RGB-ordered for PIL. |
| `show_desktop(settle_delay=0.5)` | Calls `Shell.Application.MinimizeAll()` so OCR sees the actual wallpaper instead of whatever editor/terminal happens to be in front. Hotkey fallback (Win+M) if COM unavailable. |
| `restore_windows()` | The inverse — `UndoMinimizeALL` / Win+Shift+M. Used by `tjm-demo` to leave the user's session as they found it. |
| `save_screenshot(image, path)` | PNG write helper with parent-dir creation. |

### `ocr_engine.py` — EasyOCR wrapper

- `TextBox` dataclass: text + bbox `(x1, y1, x2, y2)` + confidence + derived `.center`, `.width`, `.height`.
- `_get_reader()` is `@lru_cache`d so EasyOCR's PyTorch model only loads once per process (~3 s cold).
- `read_text(image, min_confidence=0.3, canvas_size=1280)` — runs OCR on the image. `canvas_size=1280` is the critical performance lever (see [§7 — performance](#7-performance)).
- `find_text(image, query, exact=False)` — substring/exact filter on top of `read_text`.

### `grounding.py` — the actual icon grounding

The heart of the project. Three public functions:

- `ground_by_label(image, label)` — OCR-driven; the primary path.
- `ground_by_template(image, template_path)` — OpenCV `matchTemplate`; the fallback.
- `ground_icon(image, label, template_path=None)` — tries label first, then template, returns a `GroundingResult`.
- `annotate_image(image, result, label)` — paints a green bounding box, red crosshair, and yellow candidate boxes onto the image for the `tjm-demo` deliverable screenshots.

Internal helpers:
- `_select_best_label(candidates, query)` — selection rules (see [§4](#4-the-grounding-algorithm-in-detail)).
- `_is_code_like(text)` — heuristic to deprioritise OCR hits inside editors/terminals.

`GroundingResult` carries `found`, `center (x, y)`, `bbox`, `method` (`"ocr_label"` / `"template"`), `confidence`, the full candidate list, and a human-readable `reason` for failures.

### `popup_handler.py` — generic dismissal of unknown pop-ups

OCR the screen, look for a text box whose content matches any of `("close", "cancel", "dismiss", "no thanks", "not now", "skip", "later", "ok")`, click its center. Repeat up to 3 times. Called between failed grounding attempts.

**Why it's general:** it never relies on the *appearance* of a pop-up, only on the universal convention that the dismiss button has a label. Works for pop-ups we've never seen before.

### `notepad.py` — UI primitives for Notepad

| Function | Purpose |
|---|---|
| `wait_for_notepad_window(timeout=30.0)` | Polls for a Notepad window OR notepad.exe process. Tasklist check covers Win11 UWP Notepad's brief empty-title startup window. Brings the window to the foreground. |
| `get_notepad_window()` | First top-level window whose title contains `notepad` (excluding `notepad++`). |
| `is_notepad_process_running()` | `tasklist /FI "IMAGENAME eq notepad.exe"`. |
| `is_notepad_running()` | Logical OR of the two above. |
| `write_text_in_notepad(text)` | Ctrl+A → Delete → set clipboard → Ctrl+V. |
| `save_as(path, overwrite=True)` | Pre-deletes target → Escape (kill stale dialogs) → Ctrl+Shift+S → Alt+N (focus filename) → Ctrl+A → paste → Enter → Alt+Y (defensive yes-to-replace). |
| `close_notepad()` | Alt+F4 → Alt+N (Don't Save). |
| `_set_clipboard(text)` | pyperclip-backed; typewrite fallback if pyperclip fails. |
| `list_open_window_titles(limit=30)` | Diagnostic for launch failures. |

### `api_client.py` — JSONPlaceholder

`Post` dataclass + `fetch_posts(limit=10)` + `format_post(post)` returning `"Title: {title}\n\n{body}"`.

Falls back to local `Offline title N` stubs in `main.run()` if the API is unreachable, so the rest of the pipeline can be demonstrated even on a restricted network.

### `notifications.py` — end-of-run popup + beep

- `_play_sound(success)` — `winsound.MessageBeep(MB_OK)` on success, `MB_ICONHAND` on failure.
- `_show_message_box(title, message, success)` — `ctypes.windll.user32.MessageBoxW` with `MB_TOPMOST | MB_SETFOREGROUND` so the dialog is hard to miss.
- `notify_completion(succeeded, total, output_dir, failed_ids)` — combines both, picks success vs. error variant.

No new dependencies — both stdlib.

### `demo.py` — annotated screenshot generator (`tjm-demo`)

1. `show_desktop()` (unless `--no-show-desktop`)
2. countdown
3. capture
4. ground
5. annotate
6. write PNG
7. `restore_windows()`

Returns `0` on hit, `2` on miss.

### `capture_template.py` — template extractor (`tjm-capture-template`)

OCR-grounds the icon, crops a configurable square around the click point, writes to `assets/<label>.png`. Used to seed the OpenCV fallback path when OCR is unreliable on a specific theme.

### `main.py` — entry point (`tjm-run`)

- `setup_logging()` — Rich console handler.
- `desktop_dir()` — resolves the *real* desktop via `SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY)`. Correctly handles OneDrive redirection (`%OneDrive%\Desktop` ≠ `%USERPROFILE%\Desktop`).
- `attempt_ground(label, template, attempts)` — show desktop → capture → ground; retry on miss with `dismiss_popups()` between tries.
- `launch_notepad_via_icon(label, template, attempts)` — ground + double-click + wait + verify.
- `process_one_post(post, out_dir, max_attempts=2)` — type + save; retries the entire write+save cycle once with an Escape between, polling for the file with a non-zero size check.
- `run(...)` — top-level loop.

---

## 4. The grounding algorithm in detail

```python
def ground_by_label(image, label):
    # 1. OCR
    matches = find_text(image, label)  # substring match by default

    # 2. Select best candidate
    best = _select_best_label(matches, label, prefer_exact=True)

    # 3. Geometric inversion: icon center is ~40 px above label center
    icon_cx = best.center.x
    icon_cy = best.center.y - LABEL_TO_ICON_OFFSET_PX  # 40

    # 4. Build a generous square bbox for visualisation
    return GroundingResult(found=True, center=(icon_cx, icon_cy), bbox=..., ...)
```

### Selection rules (`_select_best_label`)

Given a set of OCR text boxes that contain the query string somewhere in their text:

1. **Exact match wins.** Any box whose text is exactly the query (case-insensitive) beats every substring match, regardless of confidence. This is how we discriminate `Notepad` from `Notepad++` and from longer strings like `notepad_via_icon` that happen to appear in source code visible in editor windows.
2. **Non-code-like preferred.** Among non-exact matches, boxes whose text doesn't look like a programming identifier (no underscores, no `()=;/\{}`, etc.) win over those that do.
3. **Highest confidence within the surviving pool.**

### Geometric inversion

Windows desktop icons render their label **centered below the bitmap**. At 1920×1080 with default Medium icons:
- Icon bitmap: ~48×48
- Gap: ~6 px
- Label: 14 px font, usually one line

Vertical distance from label center to icon-bitmap center ≈ `label_height/2 + gap + icon_height/2` ≈ `7 + 6 + 24` = **~37 px**. We use **40 px** (the `LABEL_TO_ICON_OFFSET_PX` constant) as a slight overshoot — clicking the centre of the bitmap is more reliable than clicking near its bottom edge (which can hit the label and trigger F2-style rename mode).

For different icon sizes / DPI scaling, this constant is the one thing that needs adjusting.

### Why label-first beats template-first as the *primary*

- Templates encode the icon's pixel appearance. Theme change → broken. Custom icon → broken. Light/dark switch → broken. Resolution change → broken without a new template.
- OCR labels encode the icon's *identity*. A "Notepad" shortcut is a "Notepad" shortcut whether its overlay arrow rotates, whether the wallpaper is dark, or whether you're on a Surface at 150 % scaling.

The template path is still useful as a **fallback** when OCR fails (e.g. extreme contrast collapse with a dark theme on a dark wallpaper), so we keep both.

---

## 5. Workflow walkthrough — one iteration

```
                                      ┌─────────────────────────────┐
                                      │ is_notepad_running()?       │
                                      └──────┬──────────────────────┘
                                  yes        │ no
                                ┌────────────┴────────────┐
                                ▼                         ▼
                       reuse open window     ┌─────────────────────────┐
                                              │ show_desktop()          │
                                              │ capture                 │
                                              │ ground_icon('Notepad')  │
                                              └────────────┬────────────┘
                                                ┌──────────┴──────────┐
                                                ▼ found               ▼ miss
                                  double-click (interval=0.1)    dismiss_popups()
                                                │                     │ retry up to 3×
                                                ▼                     ▼
                                  wait_for_notepad_window(30s) ─── fail → debug dump
                                                │
                                                ▼
                                ┌─────────────────────────────┐
                                │ write_text_in_notepad(text) │
                                │ save_as(post_N.txt)         │
                                └─────────────────────────────┘
                                                │
                                                ▼
                                  poll filesystem (≤8 s, non-zero size)
                                                │ exists & sized
                                                ▼
                                            success
                                                │
                                                ▼
                              if not --reuse-window: close_notepad()
```

---

## 6. CLI reference

### `tjm-run` — full automation

```
uv run tjm-run [OPTIONS]

  --label LABEL          Icon label to ground (default: Notepad)
  --template PATH        Optional template image for OCR fallback
  --limit N              Number of posts to process (default: 10)
  --attempts N           Grounding retries per launch (default: 3)
  --reuse-window         Keep Notepad open across iterations (much faster)
  --no-notify            Suppress end-of-run popup + beep
  -v, --verbose          DEBUG-level logging
```

Exit codes: `0` if every post saved, `1` otherwise.

### `tjm-demo` — annotated screenshot generator

```
uv run tjm-demo [OPTIONS]

  --label LABEL          Icon to locate (default: Notepad)
  --template PATH        Optional template image
  --out PATH             Output PNG (default: screenshots/grounding_<label>_<ts>.png)
  --countdown N          Seconds before capture (default: 2)
  --no-show-desktop      Don't minimize windows before capture
  --no-restore           Don't restore windows after capture
```

Exit codes: `0` on hit, `2` on miss.

### `tjm-capture-template` — seed an OCR-fallback template

```
uv run tjm-capture-template [OPTIONS]

  --label LABEL          Icon to capture (default: Notepad)
  --out PATH             Output PNG (default: assets/<label>.png)
  --pad N                Pixels of padding around the icon (default: 8)
  --icon-size N          Estimated icon bitmap height/width (default: 56)
  --countdown N          Seconds before capture (default: 3)
```

---

## 7. Performance

Per-iteration time budget (1920×1080, CPU PyTorch, no GPU):

| Step | Cold (1st call) | Warm |
|---|---|---|
| EasyOCR model load | ~3 s | 0 (cached in module) |
| `show_desktop()` | 500 ms | 500 ms |
| `capture_desktop()` (mss) | ~30 ms | ~30 ms |
| `read_text(canvas_size=1280)` | ~8–10 s | ~8–10 s |
| Selection + bbox math | <1 ms | <1 ms |
| Double-click + window wait | 1–3 s | 1–3 s |
| Type via clipboard + save | 4–6 s | 4–6 s |

`canvas_size=1280` is the dominant performance lever. EasyOCR's default of 2560 was almost 3× slower without any accuracy gain for default-sized icon labels.

`--reuse-window` skips re-grounding for posts 2..N, which cuts 10-iteration runtime from ~5 minutes to ~1 minute.

---

## 8. Output

```
%OneDrive%\Desktop\tjm-project\
├── post_1.txt
├── post_2.txt
├── ...
└── post_10.txt
```

Each file contains, exactly:

```
Title: <post title>

<post body>
```

If the API is unreachable, content is the fallback `Title: Offline title N\n\nThis is fallback body text N.\nLine 2.` so the rest of the pipeline can still be demonstrated.

---

## 9. Error handling

- **Grounding miss** → up to `--attempts` retries with `dismiss_popups()` between each.
- **Launch detection miss** → 30-second window-and-process wait, then a debug screenshot at `debug/launch_failed_<ts>.png` plus a dump of every open top-level window title for diagnostics.
- **API unreachable** → ConnectionError caught, swap in 10 offline-stub posts, continue.
- **Save failed** → second save attempt with Escape between (clears stuck dialog) before declaring failure.
- **Existing files** → pre-deleted in `save_as` so the Replace? dialog never appears.
- **OneDrive desktop redirection** → resolved via `SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY)`, not `%USERPROFILE%\Desktop`.

---

## 10. Tests

- `tests/test_grounding.py` — 7 pytest cases covering `_select_best_label` selection rules, `_is_code_like` heuristic, and `TextBox` geometry. All non-UI logic, no screen needed.
- Run with `uv run pytest -q`.

---

## 11. Configuration / extension points

| To do this | Change this |
|---|---|
| Ground a different icon | `--label "Whatever"` |
| Tune for different DPI / icon size | `LABEL_TO_ICON_OFFSET_PX` in `grounding.py` |
| Tweak OCR speed/accuracy | `canvas_size` argument to `read_text` |
| Accept lower-confidence labels | `min_confidence` in `find_text` |
| Use template matching only | `ground_by_template` directly |
| Add pop-up words to dismiss | `DEFAULT_DISMISS_LABELS` in `popup_handler.py` |
| Swap notification style | Replace `_show_message_box` in `notifications.py` |
| Plug in a VLM | Replace the body of `ground_by_label` |

---

## 12. Known limitations

- **CPU OCR latency.** ~10 s per full-desktop pass even with `canvas_size=1280`. Enabling EasyOCR's GPU mode (`gpu=True`) cuts this to ~1 s if CUDA is available. Not done by default because the project must run on stock laptops without GPU setup.
- **DPI awareness.** Tested at 100 % scaling on 1920×1080. At fractional DPI (125 %, 150 %), `LABEL_TO_ICON_OFFSET_PX` needs to scale proportionally — or use a runtime call to `GetDpiForWindow`.
- **Locale.** Pop-up dismissal uses English words. For non-English Windows, extend `DEFAULT_DISMISS_LABELS`.
- **Notepad++ disambiguation.** Handled in code via exact-match preference, but if the user passes `--label "Notepad++"` we'll happily click *that* — the explicit exclusion is for the default `Notepad` case only.
- **Save As dialog races.** The Save flow uses fixed sleeps tuned for a typical Win10 machine. On heavily-loaded systems the dialog can take >2 s to render and the path-paste may happen before the field gains focus. Retry logic catches one such failure; two consecutive races will still fail.
- **No real input-method support.** Pasted text goes through the system clipboard, which means non-ASCII content depends on Notepad's encoding (defaults to UTF-8 in current Notepad versions).
