"""Stage 1 — scene segmentation and keyframe extraction."""

from __future__ import annotations

import logging
from pathlib import Path

from .config import SegmentationConfig
from .errors import InputVideoError
from .ffmpeg import FFmpeg, VideoInfo
from .models import Keyframe, Segment, SegmentsFile

logger = logging.getLogger(__name__)

# Keyframes are sampled slightly inside the segment so we never grab the frame
# of the transition itself.
_EDGE_INSET_SEC = 0.35


def build_segments(
    scene_times: list[float], duration: float, min_segment_sec: float
) -> list[tuple[float, float]]:
    """Turn scene-change timestamps into (start, end) spans covering [0, duration].

    Segments shorter than min_segment_sec are merged into their neighbour
    (previous segment if one exists, otherwise the next one). Pure function —
    unit tested in tests/test_segmentation.py.
    """
    boundaries = [t for t in sorted(set(scene_times)) if 0.0 < t < duration]
    spans: list[tuple[float, float]] = []
    prev = 0.0
    for t in [*boundaries, duration]:
        if t > prev:
            spans.append((prev, t))
            prev = t
    if not spans:
        spans = [(0.0, duration)]

    # Forward accumulation: short spans absorb following spans until the
    # accumulated span reaches the minimum length; a short trailing remainder
    # merges into the previous segment. This preserves as many detected scene
    # boundaries as possible.
    merged: list[tuple[float, float]] = []
    current: tuple[float, float] | None = None
    for span in spans:
        current = span if current is None else (current[0], span[1])
        if current[1] - current[0] >= min_segment_sec:
            merged.append(current)
            current = None
    if current is not None:
        if merged:
            merged[-1] = (merged[-1][0], current[1])
        else:
            merged.append(current)
    return merged


def keyframe_times(start: float, end: float, max_keyframes: int) -> list[float]:
    """1-3 representative timestamps: start, middle, end (inset from the edges)."""
    duration = end - start
    inset = min(_EDGE_INSET_SEC, duration / 4)
    first, last = start + inset, end - inset
    if duration <= 6.0 or max_keyframes == 1:
        return [start + duration / 2]
    if duration <= 12.0 or max_keyframes == 2:
        return [first, last]
    return [first, start + duration / 2, last]


def validate_input_video(ffmpeg: FFmpeg, video: Path, min_duration: float) -> VideoInfo:
    """Cheap validation before any expensive work."""
    if not video.exists():
        raise InputVideoError(f"Input video not found: {video}")
    if video.suffix.lower() not in {".mp4", ".mkv"}:
        raise InputVideoError(
            f"Unsupported input format {video.suffix!r} — expected .mp4 or .mkv."
        )
    info = ffmpeg.video_info(video)
    if info.duration_sec < min_duration:
        raise InputVideoError(
            f"{video} is only {info.duration_sec:.1f}s long "
            f"(minimum is {min_duration:.0f}s). Is this the right recording?"
        )
    return info


def segment_video(
    ffmpeg: FFmpeg,
    video: Path,
    run_dir: Path,
    cfg: SegmentationConfig,
) -> SegmentsFile:
    """Run scene detection, merge short segments, extract keyframes.

    Writes keyframes to <run_dir>/keyframes/ and returns the SegmentsFile
    (the caller persists it as segments.json).
    """
    info = validate_input_video(ffmpeg, video, cfg.min_video_duration_sec)
    logger.info(
        "Input: %s  (%.1fs, %dx%d @ %.2f fps)",
        video.name, info.duration_sec, info.width, info.height, info.fps,
    )

    logger.info("Detecting scene changes (threshold %.2f)...", cfg.scene_threshold)
    scene_times = ffmpeg.detect_scene_changes(video, cfg.scene_threshold)
    logger.info("Found %d raw scene changes", len(scene_times))

    spans = build_segments(scene_times, info.duration_sec, cfg.min_segment_sec)
    logger.info(
        "%d segments after merging spans shorter than %.1fs", len(spans), cfg.min_segment_sec
    )

    keyframes_dir = run_dir / "keyframes"
    segments: list[Segment] = []
    for i, (start, end) in enumerate(spans):
        frames: list[Keyframe] = []
        for j, t in enumerate(keyframe_times(start, end, cfg.max_keyframes_per_segment)):
            out = keyframes_dir / f"segment_{i:03d}_kf{j}.jpg"
            ffmpeg.extract_keyframe(video, t, out, cfg.keyframe_max_edge)
            frames.append(Keyframe(time_sec=round(t, 3), path=str(out.relative_to(run_dir))))
        segments.append(
            Segment(index=i, start_sec=round(start, 3), end_sec=round(end, 3), keyframes=frames)
        )
        logger.debug("segment %d: %.2f-%.2f (%d keyframes)", i, start, end, len(frames))

    return SegmentsFile(
        source_video=str(video.resolve()),
        duration_sec=round(info.duration_sec, 3),
        scene_threshold=cfg.scene_threshold,
        min_segment_sec=cfg.min_segment_sec,
        segments=segments,
    )
