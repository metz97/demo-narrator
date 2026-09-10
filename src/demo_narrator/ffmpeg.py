"""Thin, typed wrapper around ffmpeg/ffprobe (subprocess, explicit args, no shell)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import FFmpegError, FFmpegNotFoundError, InputVideoError

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "Install ffmpeg:\n"
    "  Windows:  winget install Gyan.FFmpeg   (then restart the terminal)\n"
    "  macOS:    brew install ffmpeg\n"
    "  Linux:    sudo apt install ffmpeg  /  sudo dnf install ffmpeg\n"
    "or set ffmpeg.ffmpeg_path / ffmpeg.ffprobe_path in config/config.yaml."
)


@dataclass(frozen=True)
class VideoInfo:
    duration_sec: float
    width: int
    height: int
    fps: float
    has_audio: bool


class FFmpeg:
    """All ffmpeg/ffprobe invocations used by the pipeline."""

    def __init__(self, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    # -- binary discovery / validation ------------------------------------

    def check_available(self) -> tuple[str, str]:
        """Return (ffmpeg version line, ffprobe version line) or raise."""
        versions = []
        for name, binary in (("ffmpeg", self.ffmpeg_path), ("ffprobe", self.ffprobe_path)):
            resolved = shutil.which(binary) or (binary if Path(binary).exists() else None)
            if resolved is None:
                raise FFmpegNotFoundError(f"{name} not found ({binary!r}). {_INSTALL_HINT}")
            try:
                proc = subprocess.run(
                    [resolved, "-version"], capture_output=True, text=True, timeout=15
                )
            except OSError as exc:
                raise FFmpegNotFoundError(f"Could not execute {name} at {resolved}: {exc}") from exc
            first_line = (proc.stdout or proc.stderr).splitlines()[0].strip()
            versions.append(first_line)
        return versions[0], versions[1]

    # -- low-level runners --------------------------------------------------

    def _run(
        self, args: list[str], *, tool: str | None = None, cwd: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        binary = tool or self.ffmpeg_path
        cmd = [binary, *args]
        logger.debug("exec: %s", subprocess.list2cmdline(cmd))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd
            )
        except FileNotFoundError as exc:
            raise FFmpegNotFoundError(f"{binary} not found. {_INSTALL_HINT}") from exc
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or "").splitlines()[-15:])
            raise FFmpegError(
                f"{Path(binary).name} failed (exit {proc.returncode}).\n"
                f"Command: {subprocess.list2cmdline(cmd)}\n"
                f"Output (last lines):\n{tail}"
            )
        return proc

    def run_ffmpeg(
        self, args: list[str], *, cwd: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._run(["-hide_banner", "-nostdin", "-y", *args], cwd=cwd)

    def run_ffprobe(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run(args, tool=self.ffprobe_path)

    # -- probing -------------------------------------------------------------

    def probe(self, path: Path) -> dict[str, Any]:
        proc = self.run_ffprobe(
            [
                "-v", "error",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ]
        )
        result: dict[str, Any] = json.loads(proc.stdout)
        return result

    def video_info(self, path: Path) -> VideoInfo:
        if not path.exists():
            raise InputVideoError(f"Input video not found: {path}")
        data = self.probe(path)
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise InputVideoError(f"{path} has no video stream.")
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        duration = float(data.get("format", {}).get("duration", 0.0))
        num, _, den = str(video.get("avg_frame_rate", "30/1")).partition("/")
        try:
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            fps = 30.0
        if fps <= 0 or fps > 240:
            fps = 30.0
        return VideoInfo(
            duration_sec=duration,
            width=int(video.get("width", 0)),
            height=int(video.get("height", 0)),
            fps=fps,
            has_audio=has_audio,
        )

    def media_duration(self, path: Path) -> float:
        proc = self.run_ffprobe(
            [
                "-v", "error",
                "-show_entries", "format=duration",
                "-print_format", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
        try:
            return float(proc.stdout.strip())
        except ValueError as exc:
            raise FFmpegError(f"Could not read duration of {path}: {proc.stdout!r}") from exc

    # -- Stage 1: scene detection & keyframes ---------------------------------

    def detect_scene_changes(self, video: Path, threshold: float) -> list[float]:
        """Timestamps (seconds) where ffmpeg detects a scene change."""
        proc = self.run_ffmpeg(
            [
                "-i", str(video),
                "-vf", f"select='gt(scene,{threshold})',metadata=print",
                "-an",
                "-f", "null",
                "-",
            ]
        )
        # metadata=print writes lines like "frame:12 pts:345 pts_time:11.5" to stderr
        times = [float(m) for m in re.findall(r"pts_time:([0-9]+\.?[0-9]*)", proc.stderr)]
        return sorted(set(times))

    def extract_keyframe(self, video: Path, time_sec: float, out_jpg: Path, max_edge: int) -> None:
        """Extract one frame as JPEG, downscaled so the long edge <= max_edge."""
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        scale = (
            f"scale=w='if(gte(iw,ih),min(iw,{max_edge}),-2)'"
            f":h='if(gte(iw,ih),-2,min(ih,{max_edge}))'"
        )
        self.run_ffmpeg(
            [
                "-ss", f"{time_sec:.3f}",
                "-i", str(video),
                "-frames:v", "1",
                "-vf", scale,
                "-q:v", "3",
                str(out_jpg),
            ]
        )

    # -- Stage 5: cutting, freeze-frames, muxing, concat ----------------------

    def _video_encode_args(self, crf: int, preset: str, fps: float) -> list[str]:
        # Constant frame rate + identical codec parameters across every piece so
        # the final concat can stream-copy safely.
        return [
            "-an",
            "-c:v", "libx264",
            "-crf", str(crf),
            "-preset", preset,
            "-pix_fmt", "yuv420p",
            "-r", f"{fps:.6f}",
            "-fps_mode", "cfr",
            "-video_track_timescale", "90000",
        ]

    def cut_segment_video(
        self,
        video: Path,
        start: float,
        end: float,
        out_mp4: Path,
        *,
        crf: int,
        preset: str,
        fps: float,
    ) -> None:
        """Re-encode [start, end) of the source as a video-only clip."""
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        self.run_ffmpeg(
            [
                "-ss", f"{start:.3f}",
                "-to", f"{end:.3f}",
                "-i", str(video),
                *self._video_encode_args(crf, preset, fps),
                str(out_mp4),
            ]
        )

    def extract_last_frame(self, clip: Path, out_png: Path) -> None:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        self.run_ffmpeg(
            [
                "-sseof", "-0.5",
                "-i", str(clip),
                "-update", "1",
                "-frames:v", "1",
                str(out_png),
            ]
        )

    def still_clip(
        self,
        image: Path,
        duration: float,
        out_mp4: Path,
        *,
        crf: int,
        preset: str,
        fps: float,
    ) -> None:
        """A freeze-frame clip of `duration` seconds from a still image."""
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        self.run_ffmpeg(
            [
                "-loop", "1",
                "-framerate", f"{fps:.6f}",
                "-i", str(image),
                "-t", f"{duration:.3f}",
                *self._video_encode_args(crf, preset, fps),
                str(out_mp4),
            ]
        )

    def concat_copy(self, parts: list[Path], out_path: Path, *, faststart: bool = False) -> None:
        """Concatenate media files with identical codec parameters (stream copy)."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        list_file = out_path.with_suffix(".concat.txt")
        lines = []
        for p in parts:
            escaped = p.resolve().as_posix().replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        args = [
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
        ]
        if faststart:
            args += ["-movflags", "+faststart"]
        self.run_ffmpeg([*args, str(out_path)])
        list_file.unlink(missing_ok=True)

    def mux_segment(
        self,
        video_only: Path,
        narration_wav: Path | None,
        out_mp4: Path,
        *,
        out_duration: float,
        audio_duration: float,
        fade_ms: int,
        sample_rate: int,
        audio_bitrate: str,
    ) -> None:
        """Attach narration audio (faded, padded with silence) to a video clip.

        If narration_wav is None, a silent track is generated instead so every
        segment has a uniform audio stream for the final concat.
        """
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        fade = fade_ms / 1000.0
        if narration_wav is None:
            self.run_ffmpeg(
                [
                    "-i", str(video_only),
                    "-f", "lavfi",
                    "-i", f"anullsrc=r={sample_rate}:cl=stereo",
                    "-map", "0:v",
                    "-map", "1:a",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", audio_bitrate,
                    "-ar", str(sample_rate),
                    "-t", f"{out_duration:.3f}",
                    str(out_mp4),
                ]
            )
            return
        filters = [f"aresample={sample_rate}", "aformat=channel_layouts=stereo"]
        if fade > 0 and audio_duration > 2 * fade:
            filters.append(f"afade=t=in:st=0:d={fade:.3f}")
            filters.append(f"afade=t=out:st={max(0.0, audio_duration - fade):.3f}:d={fade:.3f}")
        filters.append("apad")  # natural silence after narration ends
        self.run_ffmpeg(
            [
                "-i", str(video_only),
                "-i", str(narration_wav),
                "-filter_complex", f"[1:a]{','.join(filters)}[a]",
                "-map", "0:v",
                "-map", "[a]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", audio_bitrate,
                "-ar", str(sample_rate),
                "-t", f"{out_duration:.3f}",
                str(out_mp4),
            ]
        )

    def mux_timed_narration(
        self,
        video: Path,
        clips: list[tuple[Path, float]],
        out_mp4: Path,
        *,
        crf: int,
        preset: str,
        fps: float,
        sample_rate: int,
        audio_bitrate: str,
        duration: float,
    ) -> None:
        """Re-encode ``video`` to H.264 and overlay narration ``clips`` — each a
        (wav, start_sec) placed at its timestamp on one mixed audio track.

        Used by demo mode: the recording already has audio-driven pacing, so the
        narration slots in at fixed offsets (no overlap → summed, not ducked).
        """
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        inputs: list[str] = ["-i", str(video)]
        filt: list[str] = []
        mixed: list[str] = []
        for i, (wav, start) in enumerate(clips, start=1):
            inputs += ["-i", str(wav)]
            ms = max(0, int(start * 1000))
            filt.append(f"[{i}:a]aresample={sample_rate},adelay={ms}:all=1[d{i}]")
            mixed.append(f"[d{i}]")

        if mixed:
            n = len(mixed)
            filter_complex = (
                ";".join(filt)
                + ";"
                + "".join(mixed)
                + f"amix=inputs={n}:normalize=0:dropout_transition=0[aout]"
            )
            audio_map = ["-map", "[aout]"]
            audio_filter = ["-filter_complex", filter_complex]
        else:  # no narration at all → silent track
            audio_filter = ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo"]
            audio_map = ["-map", "1:a"]

        self.run_ffmpeg(
            [
                *inputs,
                *audio_filter,
                "-map", "0:v",
                *audio_map,
                "-c:v", "libx264",
                "-crf", str(crf),
                "-preset", preset,
                "-pix_fmt", "yuv420p",
                "-r", f"{fps:.6f}",
                "-c:a", "aac",
                "-ar", str(sample_rate),
                "-ac", "2",
                "-b:a", audio_bitrate,
                "-t", f"{duration:.3f}",
                "-movflags", "+faststart",
                str(out_mp4),
            ]
        )

    def narrated_still_clip(
        self,
        image: Path,
        narration_wav: Path | None,
        out_mp4: Path,
        *,
        duration: float,
        fps: float,
        crf: int,
        preset: str,
        sample_rate: int,
        audio_bitrate: str,
    ) -> None:
        """A title-card clip: a still image for `duration` seconds with optional
        narration (padded with silence), encoded to match the main video."""
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        video_in = ["-loop", "1", "-framerate", f"{fps:.6f}", "-i", str(image)]
        if narration_wav is not None:
            audio_in = ["-i", str(narration_wav)]
            audio_filter = ["-filter_complex", f"[1:a]aresample={sample_rate},apad[a]"]
            audio_map = ["-map", "[a]"]
        else:
            audio_in = ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo"]
            audio_filter = []
            audio_map = ["-map", "1:a"]
        self.run_ffmpeg(
            [
                *video_in, *audio_in, *audio_filter,
                "-map", "0:v", *audio_map,
                "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                "-r", f"{fps:.6f}",
                "-c:a", "aac", "-ar", str(sample_rate), "-ac", "2", "-b:a", audio_bitrate,
                "-t", f"{duration:.3f}",
                str(out_mp4),
            ]
        )

    def concat_reencode(
        self,
        parts: list[Path],
        out_mp4: Path,
        *,
        fps: float,
        crf: int,
        preset: str,
        sample_rate: int,
        audio_bitrate: str,
    ) -> None:
        """Concatenate a/v clips by re-encoding (robust to minor param differences)."""
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        inputs: list[str] = []
        for part in parts:
            inputs += ["-i", str(part)]
        streams = "".join(f"[{i}:v][{i}:a]" for i in range(len(parts)))
        filom = f"{streams}concat=n={len(parts)}:v=1:a=1[v][a]"
        self.run_ffmpeg(
            [
                *inputs,
                "-filter_complex", filom,
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                "-r", f"{fps:.6f}",
                "-c:a", "aac", "-ar", str(sample_rate), "-ac", "2", "-b:a", audio_bitrate,
                "-movflags", "+faststart",
                str(out_mp4),
            ]
        )

    def burn_subtitles(
        self, video: Path, ass_file: Path, out_mp4: Path, *, crf: int, preset: str
    ) -> None:
        """Burn an ASS subtitle file into the video (re-encodes video, copies audio)."""
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        # Avoid Windows path-escaping pain in the filtergraph: run ffmpeg from the
        # subtitle's directory and reference it by filename only.
        self.run_ffmpeg(
            [
                "-i", str(video.resolve()),
                "-vf", f"subtitles={ass_file.name}",
                "-c:v", "libx264",
                "-crf", str(crf),
                "-preset", preset,
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(out_mp4.resolve()),
            ],
            cwd=str(ass_file.resolve().parent),
        )

    def to_wav(self, src: Path, out_wav: Path, *, sample_rate: int) -> None:
        """Convert any audio file to PCM WAV (used by the ElevenLabs provider)."""
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        self.run_ffmpeg(
            [
                "-i", str(src),
                "-ar", str(sample_rate),
                "-ac", "1",
                "-c:a", "pcm_s16le",
                str(out_wav),
            ]
        )
