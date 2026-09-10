"""Stage 5 — assembly & duration reconciliation.

Per segment:
  * audio <= video: audio starts at the segment start; natural silence fills
    the remainder. Audio is never stretched.
  * audio  > video: the segment is extended with a freeze-frame of its last
    frame until the narration finishes. Narration is never sped up.

Every piece is encoded with identical codec parameters so the final
concatenation is a lossless stream copy with +faststart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from .config import AssemblyConfig
from .errors import RunDirError
from .ffmpeg import FFmpeg
from .logging_setup import console
from .models import Script, SegmentTiming
from .rundir import RunDir

logger = logging.getLogger(__name__)

# Freeze-frames shorter than this are imperceptible and not worth an encode.
_FREEZE_EPSILON_SEC = 0.15


def reconcile(
    indices: list[int],
    video_durations: list[float],
    audio_durations: list[float],
) -> list[SegmentTiming]:
    """Pure duration-reconciliation math (unit tested).

    output duration = max(video, audio); freeze_extra = max(0, audio - video)
    (zeroed below the epsilon); output_start accumulates on the FINAL timeline.
    """
    if not (len(indices) == len(video_durations) == len(audio_durations)):
        raise ValueError("reconcile() requires equal-length lists")
    timings: list[SegmentTiming] = []
    cursor = 0.0
    for index, video_dur, audio_dur in zip(indices, video_durations, audio_durations):
        freeze = max(0.0, audio_dur - video_dur)
        if freeze < _FREEZE_EPSILON_SEC:
            freeze = 0.0
        out_dur = video_dur + freeze
        timings.append(
            SegmentTiming(
                index=index,
                video_duration_sec=round(video_dur, 3),
                audio_duration_sec=round(audio_dur, 3),
                output_duration_sec=round(out_dur, 3),
                freeze_extra_sec=round(freeze, 3),
                output_start_sec=round(cursor, 3),
            )
        )
        cursor += out_dur
    return timings


def _srt_timestamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(script: Script, timings: list[SegmentTiming]) -> str:
    """Subtitles from the narration segments, on the output timeline."""
    by_index = {t.index: t for t in timings}
    blocks: list[str] = []
    counter = 1
    for seg in script.segments:
        timing = by_index.get(seg.index)
        if timing is None:
            continue
        start = timing.output_start_sec
        end = start + min(timing.audio_duration_sec, timing.output_duration_sec)
        if end <= start:
            end = start + min(2.0, timing.output_duration_sec)
        blocks.append(
            f"{counter}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{seg.narration}\n"
        )
        counter += 1
    return "\n".join(blocks)


def build_script_md(script: Script, timings: list[SegmentTiming]) -> str:
    """Human-readable script with output-timeline timestamps, for the record."""
    by_index = {t.index: t for t in timings}
    lines = [f"# {script.title}", ""]
    if script.detected_features:
        lines.append("Detected features:")
        lines += [f"- {feature}" for feature in script.detected_features]
        lines.append("")
    for seg in script.segments:
        timing = by_index.get(seg.index)
        if timing is None:
            continue
        start = timing.output_start_sec
        end = start + timing.output_duration_sec
        note = f" (freeze-frame +{timing.freeze_extra_sec:.1f}s)" if timing.freeze_extra_sec else ""
        lines.append(f"## Segment {seg.index} — {_srt_timestamp(start)} → {_srt_timestamp(end)}{note}")
        lines.append("")
        lines.append(seg.narration)
        lines.append("")
    return "\n".join(lines)


def assemble(
    ffmpeg: FFmpeg,
    run_dir: RunDir,
    script: Script,
    cfg: AssemblyConfig,
    output_dir: Path,
    output_basename: str,
) -> tuple[Path, Path, Path]:
    """Build the final narrated MP4 + script.md + narration.srt.

    Returns (video_path, script_md_path, srt_path).
    """
    segments_file = run_dir.load_segments()
    source = Path(segments_file.source_video)
    if not source.exists():
        raise RunDirError(
            f"Source video {source} (recorded in segments.json) no longer exists."
        )
    info = ffmpeg.video_info(source)
    work = run_dir.work_dir
    work.mkdir(parents=True, exist_ok=True)

    seg_by_index = {s.index: s for s in segments_file.segments}
    ordered = sorted(script.segments, key=lambda s: s.index)

    # Measure real narration durations via ffprobe (never trust assumptions).
    audio_durations: list[float] = []
    video_durations: list[float] = []
    indices: list[int] = []
    for seg in ordered:
        src_seg = seg_by_index.get(seg.index)
        if src_seg is None:
            raise RunDirError(
                f"script.json references segment {seg.index} which is not in segments.json."
            )
        wav = run_dir.audio_path(seg.index)
        if not wav.exists():
            raise RunDirError(f"Narration audio missing: {wav}. Run regen-audio first.")
        indices.append(seg.index)
        video_durations.append(src_seg.duration_sec)
        audio_durations.append(ffmpeg.media_duration(wav))

    timings = reconcile(indices, video_durations, audio_durations)
    timing_by_index = {t.index: t for t in timings}
    for t in timings:
        if t.freeze_extra_sec:
            logger.info(
                "Segment %d: narration %.1fs > video %.1fs — extending with a %.1fs freeze-frame",
                t.index, t.audio_duration_sec, t.video_duration_sec, t.freeze_extra_sec,
            )

    final_parts: list[Path] = []
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Assembling segments", total=len(ordered))
        for seg in ordered:
            src_seg = seg_by_index[seg.index]
            timing = timing_by_index[seg.index]
            progress.update(task, description=f"Assembling segment {seg.index}")

            clip = work / f"seg_{seg.index:03d}_video.mp4"
            ffmpeg.cut_segment_video(
                source, src_seg.start_sec, src_seg.end_sec, clip,
                crf=cfg.video_crf, preset=cfg.video_preset, fps=info.fps,
            )

            video_track = clip
            if timing.freeze_extra_sec > 0:
                frame = work / f"seg_{seg.index:03d}_last.png"
                freeze = work / f"seg_{seg.index:03d}_freeze.mp4"
                extended = work / f"seg_{seg.index:03d}_extended.mp4"
                ffmpeg.extract_last_frame(clip, frame)
                ffmpeg.still_clip(
                    frame, timing.freeze_extra_sec, freeze,
                    crf=cfg.video_crf, preset=cfg.video_preset, fps=info.fps,
                )
                ffmpeg.concat_copy([clip, freeze], extended)
                video_track = extended

            final_seg = work / f"seg_{seg.index:03d}_final.mp4"
            ffmpeg.mux_segment(
                video_track,
                run_dir.audio_path(seg.index),
                final_seg,
                out_duration=timing.output_duration_sec,
                audio_duration=timing.audio_duration_sec,
                fade_ms=cfg.fade_ms,
                sample_rate=cfg.audio_sample_rate,
                audio_bitrate=cfg.audio_bitrate,
            )
            final_parts.append(final_seg)
            progress.advance(task)

    output_dir.mkdir(parents=True, exist_ok=True)
    video_out = output_dir / f"{output_basename}-narrated.mp4"
    logger.info("Concatenating %d segments -> %s", len(final_parts), video_out)
    ffmpeg.concat_copy(final_parts, video_out, faststart=True)

    script_md = output_dir / f"{output_basename}-script.md"
    script_md.write_text(build_script_md(script, timings), encoding="utf-8")
    srt = output_dir / f"{output_basename}-narration.srt"
    srt.write_text(build_srt(script, timings), encoding="utf-8")

    run_dir.save_json(
        "timings.json", [t.model_dump() for t in timings]
    )
    total = sum(t.output_duration_sec for t in timings)
    logger.info("Final video: %s (%.1fs)", video_out, total)
    return video_out, script_md, srt
