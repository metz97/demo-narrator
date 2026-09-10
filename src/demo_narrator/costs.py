"""Cost estimation and hard guards, printed before the expensive stages."""

from __future__ import annotations

import logging
from pathlib import Path

from .errors import CostGuardError
from .models import Script, SegmentsFile

logger = logging.getLogger(__name__)

# Anthropic vision rule of thumb: tokens ~= width * height / 750.
# Keyframes are capped at 1568px long edge; assume 16:9 -> ~1.85k tokens/image.
_TOKENS_PER_IMAGE_ESTIMATE = 1850
_TOKENS_PER_PROMPT_ESTIMATE = 1500


def report_vision_estimate(segments_file: SegmentsFile, run_dir: Path, context: str | None) -> None:
    """Print an order-of-magnitude token estimate before calling Claude (Stage 3)."""
    n_keyframes = sum(len(s.keyframes) for s in segments_file.segments)
    context_tokens = int(len(context or "") / 3.5)
    estimate = (
        n_keyframes * _TOKENS_PER_IMAGE_ESTIMATE + _TOKENS_PER_PROMPT_ESTIMATE + context_tokens
    )
    logger.info(
        "Stage 3 estimate: %d segments, %d keyframes -> roughly %s input tokens "
        "(order of magnitude).",
        len(segments_file.segments),
        n_keyframes,
        f"{estimate:,}",
    )


def guard_tts_characters(script: Script, *, provider_name: str, is_paid: bool, cap: int) -> int:
    """Print total characters; enforce the hard cap for paid providers only."""
    total_chars = sum(len(seg.tts_text) for seg in script.segments)
    logger.info(
        "Stage 4: %d segments, %s characters to synthesize with provider %r.",
        len(script.segments),
        f"{total_chars:,}",
        provider_name,
    )
    if is_paid and cap > 0 and total_chars > cap:
        raise CostGuardError(
            f"TTS character guard: this run would synthesize {total_chars:,} characters "
            f"with the PAID provider {provider_name!r}, above the configured cap of {cap:,} "
            "(tts.max_tts_characters in config/config.yaml).\n"
            "Options: raise the cap, shorten the script (edit script.json, then "
            "'demo-narrator regen-audio <run-dir>'), or use the free provider "
            "(--provider kokoro / tts.provider: kokoro)."
        )
    return total_chars
