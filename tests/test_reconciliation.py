"""Unit tests for duration reconciliation math and subtitle generation."""

from __future__ import annotations

import pytest

from demo_narrator.assembly import build_srt, reconcile
from demo_narrator.models import NarrationSegment, Script


def test_audio_shorter_than_video_leaves_natural_silence() -> None:
    [t] = reconcile([0], [10.0], [6.0])
    assert t.output_duration_sec == 10.0
    assert t.freeze_extra_sec == 0.0


def test_audio_longer_than_video_extends_with_freeze() -> None:
    [t] = reconcile([0], [7.0], [13.5])
    assert t.freeze_extra_sec == 6.5
    assert t.output_duration_sec == 13.5


def test_tiny_overrun_below_epsilon_is_ignored() -> None:
    [t] = reconcile([0], [10.0], [10.1])
    assert t.freeze_extra_sec == 0.0
    assert t.output_duration_sec == 10.0


def test_output_start_accumulates_on_output_timeline() -> None:
    timings = reconcile([0, 1, 2], [10.0, 7.0, 5.0], [6.0, 13.0, 5.0])
    assert [t.output_start_sec for t in timings] == [0.0, 10.0, 23.0]
    assert timings[1].freeze_extra_sec == 6.0
    total = timings[-1].output_start_sec + timings[-1].output_duration_sec
    assert total == pytest.approx(28.0)


def test_mismatched_lengths_raise() -> None:
    with pytest.raises(ValueError):
        reconcile([0, 1], [10.0], [5.0])


def _script(narrations: list[str]) -> Script:
    return Script(
        title="t",
        detected_features=[],
        segments=[
            NarrationSegment(
                index=i, start_sec=0.0, end_sec=1.0, narration=n, tts_text=n, max_words=100
            )
            for i, n in enumerate(narrations)
        ],
    )


def test_srt_uses_output_timeline_and_audio_duration() -> None:
    timings = reconcile([0, 1], [10.0, 7.0], [6.0, 13.0])
    srt = build_srt(_script(["first line", "second line"]), timings)
    blocks = srt.strip().split("\n\n")
    assert blocks[0].splitlines() == [
        "1",
        "00:00:00,000 --> 00:00:06,000",
        "first line",
    ]
    # second segment starts at 10s on the output timeline, narration lasts 13s
    assert blocks[1].splitlines() == [
        "2",
        "00:00:10,000 --> 00:00:23,000",
        "second line",
    ]
