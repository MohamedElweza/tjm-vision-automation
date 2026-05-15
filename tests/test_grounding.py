"""Unit tests for the grounding helper logic that doesn't require a real screen."""

from __future__ import annotations

from tjm_automation.grounding import _is_code_like, _select_best_label
from tjm_automation.ocr_engine import TextBox


def _tb(text: str, conf: float = 0.9, x: int = 100, y: int = 100, w: int = 60, h: int = 14) -> TextBox:
    return TextBox(text=text, bbox=(x, y, x + w, y + h), confidence=conf)


def test_exact_match_wins_over_substring() -> None:
    candidates = [_tb("Notepad++", conf=0.95), _tb("Notepad", conf=0.80)]
    best = _select_best_label(candidates, "Notepad", prefer_exact=True)
    assert best is not None
    assert best.text == "Notepad"


def test_substring_used_when_no_exact() -> None:
    candidates = [_tb("Notepad++", conf=0.95), _tb("MyNotepad", conf=0.70)]
    best = _select_best_label(candidates, "Notepad", prefer_exact=True)
    assert best is not None
    assert best.text == "Notepad++"


def test_returns_none_for_empty() -> None:
    assert _select_best_label([], "Notepad") is None


def test_textbox_center_and_size() -> None:
    box = _tb("X", x=10, y=20, w=40, h=30)
    assert box.center == (30, 35)
    assert box.width == 40
    assert box.height == 30


def test_code_like_detects_identifiers_and_punctuation() -> None:
    assert _is_code_like("notepad_via_icon")
    assert _is_code_like("self.detect(img)")
    assert _is_code_like("a = b")
    assert _is_code_like("path/to/file")
    assert _is_code_like("C:\\Users\\moahm")


def test_code_like_passes_real_labels() -> None:
    assert not _is_code_like("Notepad")
    assert not _is_code_like("Recycle Bin")
    assert not _is_code_like("Microsoft Edge")
    assert not _is_code_like("Google Chrome")


def test_substring_prefers_non_code_over_code_when_no_exact_match() -> None:
    candidates = [
        _tb("notepad_via_icon", conf=0.97),
        _tb("My Notepad Folder", conf=0.80),
    ]
    best = _select_best_label(candidates, "Notepad", prefer_exact=True)
    assert best is not None
    assert best.text == "My Notepad Folder"
