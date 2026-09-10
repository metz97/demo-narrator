"""Unit tests for word-budget calculation."""

from __future__ import annotations

from demo_narrator.models import count_words, exceeds_budget, word_budget


def test_word_budget_is_floor_of_2_3_words_per_second() -> None:
    assert word_budget(10.0) == 23
    assert word_budget(14.2) == 32  # floor(32.66)
    assert word_budget(4.0) == 9  # floor(9.2)


def test_word_budget_never_below_one() -> None:
    assert word_budget(0.1) == 1


def test_count_words_whitespace_separated() -> None:
    assert count_words("hello world") == 2
    assert count_words("  multiple   spaces\nand\tnewlines ") == 4
    assert count_words("") == 0
    assert count_words("A-P-I counts as one word") == 5


def test_exceeds_budget_allows_15_percent_over() -> None:
    text_23 = " ".join(["word"] * 23)
    text_26 = " ".join(["word"] * 26)  # 23 * 1.15 = 26.45 -> allowed
    text_27 = " ".join(["word"] * 27)  # over tolerance
    assert not exceeds_budget(text_23, 23)
    assert not exceeds_budget(text_26, 23)
    assert exceeds_budget(text_27, 23)
