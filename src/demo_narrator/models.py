"""Data models shared across pipeline stages (all pydantic, strictly validated)."""

from __future__ import annotations

import math
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

WORDS_PER_SECOND = 2.3
BUDGET_TOLERANCE = 0.15  # a segment may exceed its budget by up to 15%


def word_budget(duration_sec: float) -> int:
    """Maximum comfortable narration length for a segment: floor(duration * 2.3)."""
    return max(1, math.floor(duration_sec * WORDS_PER_SECOND))


def count_words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def exceeds_budget(text: str, budget: int, tolerance: float = BUDGET_TOLERANCE) -> bool:
    return count_words(text) > budget * (1.0 + tolerance)


class Keyframe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_sec: float
    path: str  # relative to the run directory


class Segment(BaseModel):
    """A logical scene of the source recording (Stage 1 output)."""

    model_config = ConfigDict(extra="forbid")

    index: int
    start_sec: float
    end_sec: float
    keyframes: list[Keyframe] = Field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


class SegmentsFile(BaseModel):
    """segments.json — the persisted Stage 1 result."""

    model_config = ConfigDict(extra="forbid")

    source_video: str
    duration_sec: float
    scene_threshold: float
    min_segment_sec: float
    segments: list[Segment]


class ClaudeNarrationSegment(BaseModel):
    """One segment as returned by Claude (strict schema validation)."""

    model_config = ConfigDict(extra="forbid")

    index: int
    start_sec: float
    end_sec: float
    narration: str

    @field_validator("narration")
    @classmethod
    def _narration_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("narration must not be empty")
        return v.strip()


class ClaudeScriptResponse(BaseModel):
    """The exact JSON shape requested from Claude (strict)."""

    model_config = ConfigDict(extra="forbid")

    segments: list[ClaudeNarrationSegment]
    title: str
    detected_features: list[str]


class NarrationSegment(BaseModel):
    """One narrated segment in the final script (script.json)."""

    model_config = ConfigDict(extra="forbid")

    index: int
    start_sec: float
    end_sec: float
    narration: str  # display text (used for script.md and subtitles)
    tts_text: str  # glossary-applied text actually sent to the TTS engine
    max_words: int

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


class Script(BaseModel):
    """script.json — the persisted Stage 3 result."""

    model_config = ConfigDict(extra="forbid")

    title: str
    detected_features: list[str]
    segments: list[NarrationSegment]


class SegmentTiming(BaseModel):
    """Stage 5 reconciliation result for one segment (see reconcile())."""

    model_config = ConfigDict(extra="forbid")

    index: int
    video_duration_sec: float
    audio_duration_sec: float
    output_duration_sec: float
    freeze_extra_sec: float
    output_start_sec: float  # start position on the FINAL output timeline
