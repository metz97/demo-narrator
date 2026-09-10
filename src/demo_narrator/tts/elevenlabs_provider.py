"""ElevenLabs provider (optional, paid) — REST API with retry + backoff.

Sequential requests only (respects rate limits); MP3 responses are converted
to WAV via ffmpeg so all providers emit the same format.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ..config import ElevenLabsConfig
from ..errors import TTSError
from ..retry import RetryableError, with_backoff

if TYPE_CHECKING:
    from ..ffmpeg import FFmpeg

logger = logging.getLogger(__name__)

_API_BASE = "https://api.elevenlabs.io/v1"
_WAV_SAMPLE_RATE = 44100


class ElevenLabsProvider:
    name = "elevenlabs"
    is_paid = True

    def __init__(self, cfg: ElevenLabsConfig, api_key: str | None, ffmpeg: "FFmpeg") -> None:
        self._cfg = cfg
        self._api_key = api_key
        self._ffmpeg = ffmpeg

    def check(self) -> list[str]:
        if not self._api_key:
            return [
                "ELEVENLABS_API_KEY is not set. Add it to your environment or .env file, "
                "or switch to the free local provider: tts.provider: kokoro."
            ]
        return []

    def synthesize(self, text: str, out_path: Path) -> Path:
        if not self._api_key:
            raise TTSError("ELEVENLABS_API_KEY is not set — cannot use the elevenlabs provider.")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        def attempt() -> bytes:
            try:
                response = httpx.post(
                    f"{_API_BASE}/text-to-speech/{self._cfg.voice_id}",
                    headers={"xi-api-key": self._api_key or "", "accept": "audio/mpeg"},
                    json={
                        "text": text,
                        "model_id": self._cfg.model_id,
                        "voice_settings": {
                            "stability": self._cfg.stability,
                            "similarity_boost": self._cfg.similarity_boost,
                        },
                    },
                    timeout=120.0,
                )
            except httpx.HTTPError as exc:
                raise RetryableError(f"network error: {exc}") from exc
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = None
                try:
                    retry_after = float(response.headers.get("retry-after", ""))
                except ValueError:
                    pass
                raise RetryableError(f"HTTP {response.status_code}", retry_after)
            if response.status_code == 401:
                raise TTSError(
                    "ElevenLabs rejected the API key (401). Check ELEVENLABS_API_KEY."
                )
            if response.status_code != 200:
                raise TTSError(
                    f"ElevenLabs API error {response.status_code}: {response.text[:300]}"
                )
            return response.content

        audio_mp3 = with_backoff(attempt, description="ElevenLabs TTS request")
        mp3_path = out_path.with_suffix(".mp3")
        mp3_path.write_bytes(audio_mp3)
        try:
            self._ffmpeg.to_wav(mp3_path, out_path, sample_rate=_WAV_SAMPLE_RATE)
        finally:
            mp3_path.unlink(missing_ok=True)
        return out_path
