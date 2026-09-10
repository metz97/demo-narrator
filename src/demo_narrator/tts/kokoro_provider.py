"""Kokoro-82M provider (DEFAULT) — free, local, CPU-friendly, Apache 2.0.

Installed via the optional extra:  pip install "demo-narrator[kokoro]"
Model weights (~330 MB) are downloaded from Hugging Face on first use and
cached in the standard HF hub cache (~/.cache/huggingface/hub, or
%USERPROFILE%\\.cache\\huggingface\\hub on Windows; override with HF_HOME).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from ..config import KokoroConfig
from ..errors import TTSError

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 24000

_INSTALL_HINT = (
    'Kokoro is not installed. Install it with:  pip install "demo-narrator[kokoro]"\n'
    "(Kokoro also requires the espeak-ng system package for text-to-phoneme "
    "conversion: winget install eSpeak-NG.eSpeak-NG / brew install espeak-ng / "
    "apt install espeak-ng.)"
)


class KokoroProvider:
    name = "kokoro"
    is_paid = False

    def __init__(self, cfg: KokoroConfig) -> None:
        self._cfg = cfg
        self._pipeline: Any = None

    def check(self) -> list[str]:
        problems = []
        for module in ("kokoro", "soundfile"):
            if importlib.util.find_spec(module) is None:
                problems.append(_INSTALL_HINT)
                break
        return problems

    def _load_pipeline(self) -> Any:
        if self._pipeline is None:
            try:
                from kokoro import KPipeline
            except ImportError as exc:
                raise TTSError(_INSTALL_HINT) from exc
            logger.info(
                "Loading Kokoro-82M (first run downloads ~330 MB of weights into "
                "the Hugging Face cache — this can take a few minutes)..."
            )
            self._pipeline = KPipeline(lang_code=self._cfg.lang_code)
        return self._pipeline

    def synthesize(self, text: str, out_path: Path) -> Path:
        import numpy as np
        import soundfile as sf

        pipeline = self._load_pipeline()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        chunks = []
        try:
            for _graphemes, _phonemes, audio in pipeline(
                text, voice=self._cfg.voice, speed=self._cfg.speed
            ):
                chunks.append(np.asarray(audio, dtype=np.float32))
        except Exception as exc:
            raise TTSError(
                f"Kokoro synthesis failed: {exc}\n"
                "If this mentions espeak or phonemes, install the espeak-ng system package."
            ) from exc
        if not chunks:
            raise TTSError(f"Kokoro produced no audio for text: {text[:80]!r}")
        sf.write(str(out_path), np.concatenate(chunks), _SAMPLE_RATE)
        return out_path
