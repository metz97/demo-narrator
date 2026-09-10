"""Stage 3 orchestration: prompt -> backend -> strict validation -> word-budget
repair -> glossary application."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from ..errors import ScriptGenerationError
from ..glossary import apply_glossary
from ..models import (
    ClaudeScriptResponse,
    NarrationSegment,
    Script,
    SegmentsFile,
    count_words,
    exceeds_budget,
    word_budget,
)
from .prompts import (
    INTRO,
    REPAIR_INSTRUCTIONS,
    SCRIPT_JSON_SCHEMA,
    context_block,
    segment_header,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SegmentPrompt:
    index: int
    start_sec: float
    end_sec: float
    max_words: int
    keyframes: list[Path]  # absolute paths


@dataclass(frozen=True)
class ScriptRequest:
    """Everything a backend needs for one model call."""

    prompt_text: str
    segments: list[SegmentPrompt] = field(default_factory=list)
    include_images: bool = True
    schema: dict[str, Any] = field(default_factory=lambda: SCRIPT_JSON_SCHEMA)


class ScriptModelBackend(Protocol):
    """One structured-output call to Claude. Implemented by agent_sdk / api_key."""

    def generate(self, request: ScriptRequest) -> dict[str, Any]: ...


class ScriptGenerator:
    def __init__(
        self,
        backend: ScriptModelBackend,
        *,
        style_guide: str,
        glossary: dict[str, str],
        max_repair_attempts: int = 1,
    ) -> None:
        self._backend = backend
        self._style_guide = style_guide
        self._glossary = glossary
        self._max_repair_attempts = max_repair_attempts

    # -- public API -------------------------------------------------------------

    def generate(self, segments_file: SegmentsFile, context: str | None, run_dir: Path) -> Script:
        seg_prompts = [
            SegmentPrompt(
                index=s.index,
                start_sec=s.start_sec,
                end_sec=s.end_sec,
                max_words=word_budget(s.duration_sec),
                keyframes=[run_dir / k.path for k in s.keyframes],
            )
            for s in segments_file.segments
        ]

        response = self._call(self._initial_request(seg_prompts, context), "script generation")
        response = self._repair_if_needed(response, seg_prompts)
        self._final_budget_report(response, seg_prompts)
        return self._to_script(response, seg_prompts)

    # -- request construction ------------------------------------------------

    def _initial_request(
        self, segments: list[SegmentPrompt], context: str | None
    ) -> ScriptRequest:
        lines = [INTRO, self._style_guide, context_block(context), "", "Segments:"]
        for sp in segments:
            lines.append(segment_header(sp.index, sp.start_sec, sp.end_sec, sp.max_words))
        prompt = "\n".join(lines)
        return ScriptRequest(prompt_text=prompt, segments=segments, include_images=True)

    def _repair_request(
        self,
        previous: ClaudeScriptResponse,
        offenders: list[tuple[int, int, int]],  # (index, word_count, budget)
    ) -> ScriptRequest:
        lines = [REPAIR_INSTRUCTIONS, "Segments to shorten:"]
        for index, words, budget in offenders:
            lines.append(f"- segment {index}: currently {words} words, budget {budget} words")
        lines += ["", "Current JSON document:", previous.model_dump_json(indent=2)]
        return ScriptRequest(prompt_text="\n".join(lines), segments=[], include_images=False)

    # -- backend call + validation ----------------------------------------------

    def _call(self, request: ScriptRequest, what: str) -> ClaudeScriptResponse:
        raw = self._backend.generate(request)
        try:
            return ClaudeScriptResponse.model_validate(raw)
        except ValidationError as exc:
            raise ScriptGenerationError(
                f"Claude returned JSON that does not match the expected schema during {what}:\n{exc}"
            ) from exc

    # -- word-budget enforcement -----------------------------------------------

    def _offenders(
        self, response: ClaudeScriptResponse, segments: list[SegmentPrompt]
    ) -> list[tuple[int, int, int]]:
        budgets = {sp.index: sp.max_words for sp in segments}
        out = []
        for seg in response.segments:
            budget = budgets.get(seg.index)
            if budget is not None and exceeds_budget(seg.narration, budget):
                out.append((seg.index, count_words(seg.narration), budget))
        return out

    def _repair_if_needed(
        self, response: ClaudeScriptResponse, segments: list[SegmentPrompt]
    ) -> ClaudeScriptResponse:
        self._check_coverage(response, segments)
        offenders = self._offenders(response, segments)
        attempts = 0
        while offenders and attempts < self._max_repair_attempts:
            attempts += 1
            logger.info(
                "%d segment(s) exceed their word budget by >15%% (%s) — sending repair request",
                len(offenders),
                ", ".join(str(i) for i, _, _ in offenders),
            )
            repaired = self._call(self._repair_request(response, offenders), "budget repair")
            self._check_coverage(repaired, segments)
            # Merge: only accept the repaired text for offending segments.
            offender_ids = {i for i, _, _ in offenders}
            repaired_by_index = {s.index: s for s in repaired.segments}
            merged = []
            for seg in response.segments:
                if seg.index in offender_ids and seg.index in repaired_by_index:
                    merged.append(repaired_by_index[seg.index])
                else:
                    merged.append(seg)
            response = ClaudeScriptResponse(
                segments=merged, title=response.title, detected_features=response.detected_features
            )
            offenders = self._offenders(response, segments)
        return response

    def _final_budget_report(
        self, response: ClaudeScriptResponse, segments: list[SegmentPrompt]
    ) -> None:
        offenders = self._offenders(response, segments)
        if offenders:
            logger.warning(
                "After repair, %d segment(s) still exceed their budget (%s). "
                "Proceeding — the video will be extended with freeze-frames where needed.",
                len(offenders),
                ", ".join(f"#{i}: {w}/{b} words" for i, w, b in offenders),
            )

    def _check_coverage(
        self, response: ClaudeScriptResponse, segments: list[SegmentPrompt]
    ) -> None:
        wanted = {sp.index for sp in segments}
        got = {s.index for s in response.segments}
        missing = sorted(wanted - got)
        if missing:
            raise ScriptGenerationError(
                f"Claude's script is missing narration for segment(s) {missing}."
            )

    # -- final assembly of the Script model --------------------------------------

    def _to_script(
        self, response: ClaudeScriptResponse, segments: list[SegmentPrompt]
    ) -> Script:
        by_index = {s.index: s for s in response.segments}
        out: list[NarrationSegment] = []
        for sp in segments:
            seg = by_index[sp.index]
            narration = seg.narration.strip()
            out.append(
                NarrationSegment(
                    index=sp.index,
                    start_sec=sp.start_sec,  # trust Stage 1 timings, not the echo
                    end_sec=sp.end_sec,
                    narration=narration,
                    tts_text=apply_glossary(narration, self._glossary),
                    max_words=sp.max_words,
                )
            )
        return Script(
            title=response.title.strip() or "Demo",
            detected_features=response.detected_features,
            segments=out,
        )
