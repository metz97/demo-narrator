"""Karaoke caption engine + flow merge (pure, no ffmpeg/browser)."""

from __future__ import annotations

from demo_narrator.demo.captions import CaptionSegment, build_ass, estimate_word_timings
from demo_narrator.demo.flow import merge_flows
from demo_narrator.demo.models import Beat, FlowSpec, Step


def test_word_timings_cover_span_in_order() -> None:
    timings = estimate_word_timings("one two three four", 10.0, 14.0)
    assert len(timings) == 4
    assert timings[0][1] == 10.0
    assert abs(timings[-1][2] - 14.0) < 1e-6
    # monotonic, non-overlapping
    for (_, _, e), (_, s2, _) in zip(timings, timings[1:]):
        assert abs(e - s2) < 1e-6


def test_word_timings_empty() -> None:
    assert estimate_word_timings("", 0.0, 5.0) == []
    assert estimate_word_timings("x", 5.0, 5.0) == []  # zero span


def test_build_ass_has_styles_and_highlight() -> None:
    ass = build_ass([CaptionSegment("alpha beta gamma delta", 0.0, 4.0)], window=3)
    assert "[V4+ Styles]" in ass
    assert "Style: Cap" in ass
    assert ass.count("Dialogue:") == 4  # one event per word
    assert "&H00FFFF&" in ass  # active-word highlight colour present


def test_merge_flows_combines_beats() -> None:
    def mk(name: str, task: int) -> FlowSpec:
        return FlowSpec(
            name=name, project="p", beats=[Beat(task=task, steps=[Step(action="dwell", say="hi")])]
        )

    merged = merge_flows([mk("a", 1), mk("b", 2)], name="combo", title="Combo")
    assert merged.name == "combo"
    assert [b.task for b in merged.beats] == [1, 2]
