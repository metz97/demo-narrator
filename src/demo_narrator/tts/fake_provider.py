"""Fake provider for tests: emits silent WAVs of a computed duration so the
assembly stage can be exercised without any real TTS engine."""

from __future__ import annotations

import math
import wave
from collections.abc import Callable
from pathlib import Path

_SAMPLE_RATE = 24000


def write_silent_wav(out_path: Path, duration_sec: float, sample_rate: int = _SAMPLE_RATE) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, math.ceil(duration_sec * sample_rate))
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit PCM
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * n_frames)


class FakeTTSProvider:
    """Silent-audio provider. `duration_for_text` maps narration -> seconds."""

    name = "fake"
    is_paid = False

    def __init__(self, duration_for_text: Callable[[str], float] | None = None) -> None:
        # Default approximates real speech: 2.3 words per second.
        self._duration_for_text = duration_for_text or (
            lambda text: max(0.5, len(text.split()) / 2.3)
        )
        self.calls: list[str] = []

    def check(self) -> list[str]:
        return []

    def synthesize(self, text: str, out_path: Path) -> Path:
        self.calls.append(text)
        write_silent_wav(out_path, self._duration_for_text(text))
        return out_path
