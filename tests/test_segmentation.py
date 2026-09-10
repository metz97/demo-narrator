"""Unit tests for the segment-merging and keyframe-placement logic (pure functions)."""

from __future__ import annotations

import pytest

from demo_narrator.segmentation import build_segments, keyframe_times


class TestBuildSegments:
    def test_no_scene_changes_yields_single_segment(self) -> None:
        assert build_segments([], 30.0, 4.0) == [(0.0, 30.0)]

    def test_simple_split(self) -> None:
        assert build_segments([10.0, 20.0], 30.0, 4.0) == [
            (0.0, 10.0),
            (10.0, 20.0),
            (20.0, 30.0),
        ]

    def test_short_segment_merges_into_previous(self) -> None:
        # 12-14 is 2s < 4s -> merged into the previous span
        assert build_segments([12.0, 14.0], 24.0, 4.0) == [(0.0, 12.0), (12.0, 24.0)]

    def test_short_first_segment_merges_forward(self) -> None:
        # 0-2 is too short and has no previous segment -> merged into the next
        assert build_segments([2.0, 10.0], 20.0, 4.0) == [(0.0, 10.0), (10.0, 20.0)]

    def test_cascade_of_short_segments(self) -> None:
        # every span except the first is short; they all fold into one neighbour chain
        assert build_segments([5.0, 6.0, 7.0, 8.0], 9.0, 4.0) == [(0.0, 5.0), (5.0, 9.0)]

    def test_boundaries_outside_duration_ignored(self) -> None:
        assert build_segments([-1.0, 0.0, 35.0, 40.0], 30.0, 4.0) == [(0.0, 30.0)]

    def test_duplicate_and_unsorted_boundaries(self) -> None:
        assert build_segments([20.0, 10.0, 10.0], 30.0, 4.0) == [
            (0.0, 10.0),
            (10.0, 20.0),
            (20.0, 30.0),
        ]

    def test_everything_short_collapses_to_full_span(self) -> None:
        assert build_segments([1.0, 2.0], 3.0, 4.0) == [(0.0, 3.0)]

    def test_segments_cover_full_duration_without_gaps(self) -> None:
        spans = build_segments([3.0, 9.5, 11.0, 26.0], 33.3, 4.0)
        assert spans[0][0] == 0.0
        assert spans[-1][1] == 33.3
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
            assert prev_end == next_start
        for start, end in spans:
            assert end - start >= 4.0 or len(spans) == 1


class TestKeyframeTimes:
    def test_short_segment_gets_single_middle_frame(self) -> None:
        times = keyframe_times(10.0, 14.0, 3)
        assert times == [12.0]

    def test_medium_segment_gets_two_frames(self) -> None:
        times = keyframe_times(0.0, 10.0, 3)
        assert len(times) == 2
        assert times[0] < times[1]

    def test_long_segment_gets_three_frames(self) -> None:
        times = keyframe_times(0.0, 30.0, 3)
        assert len(times) == 3
        assert times[1] == pytest.approx(15.0)

    def test_max_keyframes_respected(self) -> None:
        assert len(keyframe_times(0.0, 30.0, 1)) == 1
        assert len(keyframe_times(0.0, 30.0, 2)) == 2

    def test_frames_are_inside_the_segment(self) -> None:
        for start, end in [(0.0, 4.5), (3.0, 40.0), (7.0, 13.0)]:
            for t in keyframe_times(start, end, 3):
                assert start < t < end
