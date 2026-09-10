"""TTSProvider protocol and provider factory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..config import Secrets, TTSConfig
from ..errors import TTSError

if TYPE_CHECKING:
    from ..ffmpeg import FFmpeg


@runtime_checkable
class TTSProvider(Protocol):
    """A text-to-speech engine that renders one narration segment to a WAV file."""

    name: str
    is_paid: bool  # paid providers are subject to the max_tts_characters cost cap

    def synthesize(self, text: str, out_path: Path) -> Path:
        """Render `text` to a WAV file at `out_path` and return that path."""
        ...

    def check(self) -> list[str]:
        """Return a list of problems preventing use (empty list = usable)."""
        ...


def create_provider(
    provider_name: str, cfg: TTSConfig, secrets: Secrets, ffmpeg: "FFmpeg"
) -> TTSProvider:
    """Instantiate the configured provider ('kokoro' or 'elevenlabs')."""
    if provider_name == "kokoro":
        from .kokoro_provider import KokoroProvider

        return KokoroProvider(cfg.kokoro)
    if provider_name == "elevenlabs":
        from .elevenlabs_provider import ElevenLabsProvider

        return ElevenLabsProvider(cfg.elevenlabs, secrets.elevenlabs_api_key, ffmpeg)
    raise TTSError(
        f"Unknown TTS provider {provider_name!r}. Valid providers: kokoro, elevenlabs."
    )
