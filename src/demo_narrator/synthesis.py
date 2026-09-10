"""Stage 4 runner — synthesize every narration segment to WAV with progress."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from .errors import TTSError
from .logging_setup import console
from .models import Script
from .rundir import RunDir
from .tts.base import TTSProvider

logger = logging.getLogger(__name__)


def synthesize_script(script: Script, provider: TTSProvider, run_dir: RunDir) -> list[Path]:
    """Render each segment's tts_text to <run>/audio/segment_NNN.wav (sequentially)."""
    problems = provider.check()
    if problems:
        raise TTSError(
            f"TTS provider {provider.name!r} is not usable:\n" + "\n".join(problems)
        )
    paths: list[Path] = []
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"Synthesizing narration ({provider.name})", total=len(script.segments)
        )
        for seg in script.segments:
            out = run_dir.audio_path(seg.index)
            progress.update(task, description=f"Synthesizing segment {seg.index} ({provider.name})")
            provider.synthesize(seg.tts_text, out)
            if not out.exists() or out.stat().st_size == 0:
                raise TTSError(f"Provider {provider.name!r} produced no audio for segment {seg.index}.")
            paths.append(out)
            progress.advance(task)
    logger.info("Synthesized %d narration files into %s", len(paths), run_dir.audio_dir)
    return paths
