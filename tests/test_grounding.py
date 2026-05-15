"""Unit tests for the grounding helper logic that doesn't require a real screen."""

from __future__ import annotations

from tjm_automation.grounding import _select_best_label
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
