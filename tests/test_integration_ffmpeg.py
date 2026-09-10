"""Integration test: real ffmpeg stages (1, 4-fake, 5) on a generated tiny video.

Skipped automatically when ffmpeg/ffprobe are unavailable. No network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from demo_narrator.assembly import assemble
from demo_narrator.config import AssemblyConfig, SegmentationConfig
from demo_narrator.errors import InputVideoError
from demo_narrator.ffmpeg import FFmpeg
from demo_narrator.models import NarrationSegment, Script
from demo_narrator.rundir import RunDir
from demo_narrator.segmentation import segment_video, validate_input_video
from demo_narrator.synthesis import synthesize_script
from demo_narrator.tts.fake_provider import FakeTTSProvider


@pytest.mark.integration
def test_full_ffmpeg_pipeline(ffmpeg: FFmpeg, tiny_video: Path, tmp_path: Path) -> None:
    run = RunDir.create(tmp_path / "out", tiny_video, {})

    # -- Stage 1 ------------------------------------------------------------
    seg_cfg = SegmentationConfig(min_segment_sec=3.0, min_video_duration_sec=5.0)
    segments_file = segment_video(ffmpeg, tiny_video, run.path, seg_cfg)
    run.save_segments(segments_file)

    assert segments_file.duration_sec == pytest.approx(12.0, abs=0.3)
    assert len(segments_file.segments) == 2  # color scene + testsrc scene
    assert segments_file.segments[0].start_sec == 0.0
    assert segments_file.segments[-1].end_sec == pytest.approx(12.0, abs=0.3)
    for seg in segments_file.segments:
        assert seg.keyframes, "every segment gets at least one keyframe"
        for kf in seg.keyframes:
            assert (run.path / kf.path).exists()

    # -- Stages 4+5: short narration on seg 0, over-long narration on seg 1 -----
    script = Script(
        title="integration",
        detected_features=[],
        segments=[
            NarrationSegment(index=0, start_sec=segments_file.segments[0].start_sec,
                             end_sec=segments_file.segments[0].end_sec,
                             narration="short", tts_text="short", max_words=10),
            NarrationSegment(index=1, start_sec=segments_file.segments[1].start_sec,
                             end_sec=segments_file.segments[1].end_sec,
                             narration="long", tts_text="long", max_words=10),
        ],
    )
    run.save_script(script)
    seg1_video_dur = segments_file.segments[1].end_sec - segments_file.segments[1].start_sec
    durations = {"short": 2.0, "long": seg1_video_dur + 3.0}  # force a freeze-frame
    provider = FakeTTSProvider(duration_for_text=lambda t: durations[t])
    synthesize_script(script, provider, run)

    video, script_md, srt = assemble(
        ffmpeg, run, script, AssemblyConfig(), tmp_path / "final", "tiny"
    )

    assert video.exists() and script_md.exists() and srt.exists()
    # expected output: seg0 keeps its video length, seg1 extended by ~3s
    expected = segments_file.duration_sec + 3.0
    assert ffmpeg.media_duration(video) == pytest.approx(expected, abs=0.5)
    info = ffmpeg.video_info(video)
    assert info.has_audio
    assert (info.width, info.height) == (320, 180)
    assert "-->" in srt.read_text(encoding="utf-8")


@pytest.mark.integration
def test_input_validation_rejects_short_video(ffmpeg: FFmpeg, tmp_path: Path) -> None:
    short = tmp_path / "short.mp4"
    ffmpeg.run_ffmpeg(
        ["-f", "lavfi", "-i", "color=c=red:s=160x90:d=2", "-pix_fmt", "yuv420p", str(short)]
    )
    with pytest.raises(InputVideoError, match="minimum"):
        validate_input_video(ffmpeg, short, min_duration=10.0)


@pytest.mark.integration
def test_input_validation_rejects_missing_and_wrong_extension(
    ffmpeg: FFmpeg, tmp_path: Path
) -> None:
    with pytest.raises(InputVideoError, match="not found"):
        validate_input_video(ffmpeg, tmp_path / "nope.mp4", min_duration=10.0)
    avi = tmp_path / "wrong.avi"
    avi.write_bytes(b"not a video")
    with pytest.raises(InputVideoError, match="mp4"):
        validate_input_video(ffmpeg, avi, min_duration=10.0)
