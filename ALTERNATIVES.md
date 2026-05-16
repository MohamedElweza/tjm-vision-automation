# Design Decisions and Alternatives

Every non-obvious choice in this project. For each: the options we considered, the trade-offs, and the reason we shipped what we shipped. Read this alongside [DOCS.md](DOCS.md).

---

## 1. Icon-grounding strategy (the central decision)

The assessment explicitly asks for the **most flexible implementation that can bypass things like unexpected pop-ups without knowing what they look like in advance**, and points at the paper [arXiv 2504.07981](https://arxiv.org/pdf/2504.07981) (VLM-driven GUI grounding) as a reference.

| Approach | What it is | Pros | Cons | Used? |
|---|---|---|---|---|
| **Hardcoded coordinates** | `pyautogui.doubleClick(50, 100)` | Trivial | Fails the spec the moment the icon moves | ❌ Rejected |
| **Direct shell launch** | `os.startfile("notepad")` | Bulletproof, no vision required | Sidesteps the assessment entirely; vision is the point | ❌ Rejected |
| **Template matching (OpenCV)** | Pre-recorded icon image + `cv2.matchTemplate` | Fast (~50 ms), deterministic | Needs a per-icon image; brittle to theme/scale/DPI/wallpaper | ✓ As **fallback** only |
| **OCR + label** | EasyOCR reads desktop labels, click above the matched label | Generalises to any **labelled** UI element with zero pre-training | Slower (~8–10 s on CPU); needs readable label | ✓ **Primary** |
| **OmniParser (Microsoft)** | Vision model + ICON DET pretrained for UI elements | Strong UI element segmentation | Heavy (GPU recommended), ~1 GB models, complex install | ❌ Rejected (overkill for this assessment) |
| **VLM grounding (paper)** | Screenshot → Claude/GPT-4V → "give me the click coords for the Notepad icon" | Highest flexibility, follows the paper exactly | Needs API key, $$ per call, network round-trip | ❌ Rejected (no API key) |
| **Windows accessibility tree (UI Automation)** | `IUIAutomation` COM → enumerate desktop items | Pixel-precise, fast, no vision needed | Cheats the assessment (no vision); doesn't generalise to non-AX-exposed elements like games or canvas web apps | ❌ Rejected (not vision-based) |
| **Hybrid: OCR-first, template-fallback, VLM-future** | What we shipped | Best of label-driven generalisation + a safety net | Doesn't reach VLM flexibility ceiling | ✓ **Shipped** |

### Why label-first OCR is the right *primary*

- **Generalises by name**, not by appearance. Change `--label "Recycle Bin"` and the same code works.
- **No artifact to maintain.** Themes change, icons get redesigned, screen DPI varies — none of that breaks an OCR-label grounder as long as the label is still rendered.
- **Closest local equivalent to a VLM grounder** without paying for API calls or shipping multi-GB models.

### Why we kept template matching as a fallback

OCR can fail in two realistic scenarios:
1. Extreme contrast collapse (dark theme + dark wallpaper, label barely visible).
2. Non-English Windows installs where the label isn't in our EasyOCR language list.

In those cases, a stored template image (~5 KB) is a robust safety net. The CLI `tjm-capture-template` exists exactly to seed it.

### If we had VLM access

We'd add a third option in `ground_icon`: try OCR first (cheap, fast, offline), then template, then VLM. The VLM call would be a `claude-haiku-4-5` or `claude-sonnet-4-6` invocation with the screenshot and a prompt like:

> *"Return the (x, y) pixel coordinates of the centre of the Notepad icon on this desktop screenshot. If not visible, return null."*

That gives the paper-flexibility tier without sacrificing the local-fast-cheap default.

---

## 2. OCR library — EasyOCR vs alternatives

| Library | Install | Models | Speed (CPU, 1080p) | Notes |
|---|---|---|---|---|
| **EasyOCR** | `pip install easyocr` | Auto-download on first run (~80 MB) | ~8 s | PyTorch under the hood; works out of the box | ✓ **Chosen** |
| Tesseract (`pytesseract`) | Requires Tesseract binary installed separately | Bundled | ~1 s | Bad UX for assessors who must install Tesseract before `uv sync`; older neural backend less accurate on small text |
| PaddleOCR | `pip install paddleocr paddlepaddle` | ~150 MB | ~6 s | Heavy install (PaddlePaddle); great accuracy but install footprint isn't worth it here |
| **RapidOCR (ONNX)** | `pip install rapidocr-onnxruntime` | ~15 MB | ~3 s | Genuinely tempting — lighter and faster than EasyOCR. We chose EasyOCR for one reason: install reliability across Win10 + Win11. RapidOCR's ONNX backend occasionally hits CRT mismatches |
| MMOCR / docTR | Heavy | Heavy | Varies | Overkill for short labels |

EasyOCR's main weakness is the PyTorch dependency (~1 GB total install). We accepted that because:
1. **One-time cost** — `uv` caches the wheel between projects.
2. **Future flexibility** — if we ever want to fine-tune for icon labels, EasyOCR's CRAFT detector + CRNN recognizer is the easiest pipeline to retrain.

---

## 3. Screen capture

| Library | API | Speed | Notes |
|---|---|---|---|
| **mss** | `with mss.mss() as s: s.grab(...)` | ~5–10 ms per frame | Pure Python, no compile step, cross-platform | ✓ **Chosen** |
| PIL.ImageGrab | `ImageGrab.grab()` | ~50 ms | Default for Windows; slower; multi-monitor handling is awkward |
| pyscreenshot | Wrapper around backends | Variable | Adds a layer; no advantage over mss directly |
| Win32 BitBlt | `windll.gdi32.BitBlt(...)` | ~3 ms | Fastest, but requires HWND plumbing — not worth the ceremony for this throughput |
| **DXGI Desktop Duplication** | DirectX-based, GPU path | ~1 ms | The absolute fastest, but only via a heavy library like `D3DShot`. Overkill here. |

mss is the standard answer for "fast Python screenshots on Windows" and is what `tjm-demo`, `tjm-run`, and `popup_handler` all use.

---

## 4. Mouse / keyboard automation

| Library | Notes |
|---|---|
| **pyautogui** | The standard. `hotkey()`, `click()`, `doubleClick()`, `typewrite()`, `moveTo()`, failsafe-on-corner. Cross-platform. | ✓ **Chosen** |
| pynput | Lower-level; better for listeners; needs more boilerplate for what we do. |
| keyboard | Requires admin on Windows for keyboard hooks; overkill. |
| Raw Win32 SendInput | Fastest, but ~30 lines of ctypes per gesture. Not justified. |
| AutoIt (`pyautoit`) | Mature but requires AutoIt installed; external dependency outside Python ecosystem. |

The one gotcha with pyautogui: `doubleClick(x, y)` defaults to `interval=0.0`, which Windows occasionally registers as a single fast click. We pass `interval=0.1` everywhere to give the OS a clean two-click signal — see [`main.py:108`](src/tjm_automation/main.py#L108).

---

## 5. Clipboard

| Approach | Why we rejected it |
|---|---|
| `subprocess.run(["powershell", "-c", "Set-Clipboard"], input=text)` | Originally chosen for "no extra deps". **Silently broken**: `Set-Clipboard` does not read stdin without an explicit `$input \|` upstream — we discovered this when 10/10 saves failed in real testing. |
| `subprocess.run(["clip"], input=text)` | Win10's `clip.exe` reads stdin but mangles non-ASCII / multibyte input. |
| **pyperclip** (`pyperclip.copy(text)`) | ✓ **Chosen.** Already a transitive dep via pyautogui. Uses Win32 `OpenClipboard`/`SetClipboardData` directly. Verified with a round-trip test. |
| pywin32 `win32clipboard` | Works, but pyperclip wraps it for us with a more pleasant API. |
| Type via `pyautogui.typewrite` | Always available as a fallback; ~5–10× slower for long strings. Used only when pyperclip raises. |

---

## 6. Window / process detection

| Mechanism | Strengths | Weaknesses |
|---|---|---|
| **pygetwindow** | One-line `getAllWindows()`, `.title`, `.activate()` | Misses windows during their empty-title startup phase |
| **`tasklist` subprocess** | Authoritative process check | Slower (~200 ms), text-parsing |
| win32gui `EnumWindows` | Native, fast | Verbose boilerplate, harder to read |

We use **both**: pygetwindow for the title match (fast common case), tasklist as a backup signal so the Win11 new Notepad UWP app — which briefly has an empty title during startup — doesn't trip the 30-second timeout.

---

## 7. End-of-run notification

| Option | Pros | Cons |
|---|---|---|
| **`ctypes.windll.user32.MessageBoxW`** | Stdlib only; modal so the user *cannot* miss it; topmost flag forces it above everything | Modal blocks the script until OK is clicked (fine because the script is done at that point) |
| Win10/11 toast via PowerShell `BurntToast` | Native look, non-blocking | Requires `BurntToast` module installed (extra setup) |
| `win10toast` / `win11toast` | Self-contained | Extra dependency |
| Plyer | Cross-platform | Extra dependency |
| `winsound.MessageBeep` alone | Audio only | User might be away; easy to miss |
| Console output only | Free | Defeats the request ("I want notifying when finished") |

We ship MessageBox + winsound — zero new deps, hard to miss visually and audibly. See [`notifications.py`](src/tjm_automation/notifications.py).

---

## 8. Save flow — UI vs direct file write

| Approach | What | Why we rejected it (or didn't) |
|---|---|---|
| **Drive Save As via keyboard** | Ctrl+Shift+S → paste full path → Enter | ✓ **Shipped.** This is the assessment's spirit: demonstrate UI automation. |
| Direct file write (`file_path.write_text(text)`) | Skip Notepad's dialog entirely | Faster and 100 % reliable, but **cheats** the assignment. We already typed the content into Notepad; saving via Python sidesteps the UI test. |
| Notepad's `/A` launch arg | Open with predetermined filename | Doesn't exist for Notepad. Win11 new Notepad's `--/?` is undocumented. |
| **Hybrid** | UI typing + filesystem-level save | Same cheat; rejected. |
| Use SendKeys via VBScript | Older approach | No advantage over pyautogui. |

The trade-off is that Save As is **timing-sensitive**: the dialog's filename field must have focus when we paste. We use Alt+N (the field's accelerator key) plus Ctrl+A as a belt-and-braces, pre-delete the target so the Replace dialog never appears, and retry the entire write+save cycle once if the file doesn't appear within 8 s.

---

## 9. Pop-up / dialog handling

| Approach | Generalisability |
|---|---|
| **OCR for dismiss words** (`Close`, `Cancel`, `Skip`, `Not now`, …) | ✓ Works on *unknown* pop-ups; never relies on visual template |
| Template-match known pop-ups | Need a library of pop-up screenshots; can't handle the unexpected case the assessment explicitly asks for |
| UI Automation tree | Specific to apps that expose AX; misses many real pop-ups |
| Spam Escape | Often closes the wrong thing (e.g. Save As mid-flight) |
| Send WM_CLOSE to non-target HWNDs | Risky; could close the user's other work |

We ship the OCR-words approach in [`popup_handler.py`](src/tjm_automation/popup_handler.py). It's invoked between failed grounding attempts, not preemptively, so it doesn't slow down the happy path.

---

## 10. HTTP client for JSONPlaceholder

| Library | Decision |
|---|---|
| **requests** | ✓ Chosen. Universal, ubiquitous, simplest API. |
| httpx | Async support we don't need; slightly slower import. |
| urllib (stdlib) | Works, but the timeout / JSON / error handling boilerplate is ugly enough that requests pays its weight in readability. |
| aiohttp | Async; unjustified complexity for one GET. |

---

## 11. Build / packaging system

| System | Decision |
|---|---|
| **uv** | ✓ Required by the assessment ("uv configuration"). Also: 10–100× faster than pip for installs; lockfile-first; manages Python versions; the modern default. |
| pip + venv | Works; slower; no managed Python; no lockfile by default. |
| Poetry | Heavier; slower; uv supersedes it for new projects. |
| pipenv | Effectively superseded by uv and Poetry. |
| conda | Wrong tool for a pure-Python project. |
| hatch / flit / setuptools | We use `hatchling` as the *build backend* under uv. No reason to expose it as the user-facing tool. |

---

## 12. CI runner

| Runner | Decision |
|---|---|
| **windows-latest** | ✓ Chosen. Project is Windows-only (pywin32, pygetwindow, the entire Win32 keyboard/clipboard stack). CI on Ubuntu would not exercise the real platform. |
| ubuntu-latest | Faster minutes, but `pyautogui`/`pywin32`/`mss` behaviour diverges enough that green Ubuntu CI would give false confidence. |
| Matrix Win + Ubuntu | Considered; rejected because Ubuntu adds noise without value here. |
| Self-hosted Windows | Future option if EasyOCR install time becomes a CI cost issue. |

---

## 13. Configuration: env vars vs CLI flags vs config file

| Option | Pros | Cons |
|---|---|---|
| **CLI flags only** | ✓ Chosen. Self-documenting, ephemeral per-run | None for this scope |
| `.env` / config file | Persistent | Adds a configuration layer the user has to learn |
| Env vars | Universal | Less discoverable than flags |

The flag surface (`--label`, `--template`, `--limit`, `--attempts`, `--reuse-window`, `--no-notify`, `-v`) is small enough that a config file is unnecessary.

---

## 14. Closing thoughts: what we'd do with more time

In rough priority order:

1. **Add the VLM path** (Claude / GPT-4V) as the highest-flexibility option for icons that lack labels or where the user can't articulate a label. Drop-in replacement for `ground_by_label`.
2. **Cache the icon position** after a successful ground. If the icon doesn't move, every iteration after the first is a 1-ms position lookup instead of an 8 s OCR pass — pulls 10-iteration runtime under 30 s without `--reuse-window`.
3. **Enable EasyOCR GPU mode** when CUDA is detected. ~10× speedup on a typical laptop with a GPU.
4. **Pop-up handling at start-of-run**, not just between failures. Proactively dismiss anything occluding the desktop before we even capture.
5. **Multi-language pop-up dictionary** for the dismiss-words list.
6. **Per-DPI tuning** of `LABEL_TO_ICON_OFFSET_PX` based on `GetDpiForWindow`.
7. **Replace the keyboard-driven Save As** with a Win32 file-dialog automation library (e.g. `pywinauto`) for sub-second saves with native focus handling.
