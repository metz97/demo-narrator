"""Unit tests for the TTS provider abstraction (all network mocked)."""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from demo_narrator.config import ElevenLabsConfig, Secrets, TTSConfig
from demo_narrator.costs import guard_tts_characters
from demo_narrator.errors import CostGuardError, TTSError
from demo_narrator.models import NarrationSegment, Script
from demo_narrator.tts.base import TTSProvider, create_provider
from demo_narrator.tts.elevenlabs_provider import ElevenLabsProvider
from demo_narrator.tts.fake_provider import FakeTTSProvider


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


class TestFakeProvider:
    def test_satisfies_protocol(self) -> None:
        assert isinstance(FakeTTSProvider(), TTSProvider)

    def test_writes_silent_wav_of_requested_duration(self, tmp_path: Path) -> None:
        provider = FakeTTSProvider(duration_for_text=lambda _: 3.25)
        out = provider.synthesize("anything", tmp_path / "seg.wav")
        assert out.exists()
        assert _wav_duration(out) == pytest.approx(3.25, abs=0.01)

    def test_default_duration_tracks_word_count(self, tmp_path: Path) -> None:
        provider = FakeTTSProvider()
        out = provider.synthesize(" ".join(["word"] * 23), tmp_path / "a.wav")
        assert _wav_duration(out) == pytest.approx(10.0, abs=0.5)  # 23 words / 2.3 wps


class TestElevenLabs:
    def _provider(self, tmp_path: Path, api_key: str | None = "key") -> ElevenLabsProvider:
        class FakeFFmpeg:
            def to_wav(self, src: Path, out_wav: Path, *, sample_rate: int) -> None:
                out_wav.write_bytes(b"RIFFfake")

        return ElevenLabsProvider(ElevenLabsConfig(), api_key, FakeFFmpeg())  # type: ignore[arg-type]

    def test_check_fails_without_key(self, tmp_path: Path) -> None:
        assert self._provider(tmp_path, api_key=None).check()

    def test_success_posts_and_converts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def fake_post(url: str, **kwargs: Any) -> httpx.Response:
            seen["url"] = url
            seen["json"] = kwargs["json"]
            return httpx.Response(200, content=b"mp3bytes", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        out = self._provider(tmp_path).synthesize("hello", tmp_path / "seg.wav")
        assert out.read_bytes() == b"RIFFfake"
        assert "text-to-speech" in seen["url"]
        assert seen["json"]["text"] == "hello"
        assert not (tmp_path / "seg.mp3").exists()  # intermediate cleaned up

    def test_429_retries_then_succeeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def fake_post(url: str, **kwargs: Any) -> httpx.Response:
            calls["n"] += 1
            status = 429 if calls["n"] < 3 else 200
            return httpx.Response(status, content=b"mp3", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr("demo_narrator.retry.time.sleep", lambda _s: None)
        out = self._provider(tmp_path).synthesize("hello", tmp_path / "seg.wav")
        assert calls["n"] == 3
        assert out.exists()

    def test_401_fails_immediately_with_actionable_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_post(url: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(401, content=b"", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(TTSError, match="ELEVENLABS_API_KEY"):
            self._provider(tmp_path).synthesize("hello", tmp_path / "seg.wav")


class TestFactoryAndCostGuard:
    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(TTSError, match="Unknown TTS provider"):
            create_provider("espeak", TTSConfig(), Secrets(), None)  # type: ignore[arg-type]

    def _script(self, chars: int) -> Script:
        text = "x" * chars
        return Script(
            title="t", detected_features=[],
            segments=[NarrationSegment(index=0, start_sec=0, end_sec=1,
                                       narration=text, tts_text=text, max_words=10)],
        )

    def test_paid_provider_over_cap_aborts(self) -> None:
        with pytest.raises(CostGuardError, match="max_tts_characters"):
            guard_tts_characters(
                self._script(11000), provider_name="elevenlabs", is_paid=True, cap=10000
            )

    def test_free_provider_ignores_cap(self) -> None:
        total = guard_tts_characters(
            self._script(50000), provider_name="kokoro", is_paid=False, cap=10000
        )
        assert total == 50000

    def test_paid_provider_under_cap_passes(self) -> None:
        guard_tts_characters(self._script(900), provider_name="elevenlabs", is_paid=True, cap=10000)
