from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from demo_narrator.config import load_config
from demo_narrator.ffmpeg import FFmpeg

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_ffmpeg() -> FFmpeg | None:
    """Use config/config.yaml paths if present, otherwise PATH lookup."""
    cfg_file = PROJECT_ROOT / "config" / "config.yaml"
    if cfg_file.exists():
        cfg = load_config(cfg_file)
        ff = FFmpeg(cfg.ffmpeg.ffmpeg_path, cfg.ffmpeg.ffprobe_path)
    else:
        ff = FFmpeg()
    try:
        ff.check_available()
    except Exception:
        return None
    return ff


@pytest.fixture(scope="session")
def ffmpeg() -> FFmpeg:
    ff = _resolve_ffmpeg()
    if ff is None:
        pytest.skip("ffmpeg/ffprobe not available")
    return ff


@pytest.fixture(scope="session")
def tiny_video(ffmpeg: FFmpeg, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 12-second test video with two clearly distinct scenes (color + testsrc)."""
    out = tmp_path_factory.mktemp("video") / "tiny.mp4"
    ffmpeg.run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=c=0x3355bb:s=320x180:d=5",
            "-f", "lavfi", "-i", "testsrc=s=320x180:d=7",
            "-filter_complex", "[0][1]concat=n=2:v=1:a=0[v]",
            "-map", "[v]",
            "-pix_fmt", "yuv420p",
            "-r", "25",
            str(out),
        ]
    )
    assert out.exists()
    return out
