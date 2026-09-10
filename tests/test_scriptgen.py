"""Unit tests for schema validation, budget repair, and the generator (backend mocked)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from demo_narrator.errors import ScriptGenerationError
from demo_narrator.models import ClaudeScriptResponse, Keyframe, Segment, SegmentsFile
from demo_narrator.scriptgen.generator import ScriptGenerator, ScriptRequest


def _segments_file() -> SegmentsFile:
    return SegmentsFile(
        source_video="video.mp4",
        duration_sec=24.0,
        scene_threshold=0.25,
        min_segment_sec=4.0,
        segments=[
            Segment(index=0, start_sec=0.0, end_sec=10.0,
                    keyframes=[Keyframe(time_sec=5.0, path="keyframes/a.jpg")]),
            Segment(index=1, start_sec=10.0, end_sec=24.0,
                    keyframes=[Keyframe(time_sec=17.0, path="keyframes/b.jpg")]),
        ],
    )


def _response(narrations: dict[int, str]) -> dict[str, Any]:
    return {
        "title": "Sprint demo",
        "detected_features": ["feature one"],
        "segments": [
            {"index": i, "start_sec": 0.0, "end_sec": 1.0, "narration": text}
            for i, text in narrations.items()
        ],
    }


class StubBackend:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[ScriptRequest] = []

    def generate(self, request: ScriptRequest) -> dict[str, Any]:
        self.requests.append(request)
        return self.responses[len(self.requests) - 1]


def _generator(backend: StubBackend, glossary: dict[str, str] | None = None) -> ScriptGenerator:
    return ScriptGenerator(
        backend, style_guide="style", glossary=glossary or {}, max_repair_attempts=1
    )


class TestSchemaValidation:
    def test_valid_response_parses(self) -> None:
        parsed = ClaudeScriptResponse.model_validate(_response({0: "hello there"}))
        assert parsed.segments[0].narration == "hello there"

    def test_missing_key_rejected(self) -> None:
        bad = _response({0: "hi"})
        del bad["title"]
        with pytest.raises(Exception):
            ClaudeScriptResponse.model_validate(bad)

    def test_extra_key_rejected(self) -> None:
        bad = _response({0: "hi"})
        bad["segments"][0]["mood"] = "happy"
        with pytest.raises(Exception):
            ClaudeScriptResponse.model_validate(bad)

    def test_wrong_type_rejected(self) -> None:
        bad = _response({0: "hi"})
        bad["segments"][0]["index"] = "zero"
        with pytest.raises(Exception):
            ClaudeScriptResponse.model_validate(bad)

    def test_empty_narration_rejected(self) -> None:
        with pytest.raises(Exception):
            ClaudeScriptResponse.model_validate(_response({0: "   "}))

    def test_generator_wraps_schema_error(self, tmp_path: Path) -> None:
        backend = StubBackend([{"totally": "wrong"}])
        with pytest.raises(ScriptGenerationError, match="schema"):
            _generator(backend).generate(_segments_file(), None, tmp_path)


class TestGenerator:
    def test_happy_path_builds_script(self, tmp_path: Path) -> None:
        backend = StubBackend([_response({0: "First segment story.", 1: "Second segment story."})])
        script = _generator(backend).generate(_segments_file(), None, tmp_path)
        assert script.title == "Sprint demo"
        assert [s.index for s in script.segments] == [0, 1]
        # timings come from Stage 1, not from Claude's echo
        assert script.segments[0].end_sec == 10.0
        assert script.segments[0].max_words == 23  # floor(10 * 2.3)
        assert len(backend.requests) == 1
        assert backend.requests[0].include_images

    def test_glossary_applied_to_tts_text_only(self, tmp_path: Path) -> None:
        backend = StubBackend([_response({0: "Our SaaS rocks.", 1: "Second."})])
        script = _generator(backend, {"SaaS": "sass"}).generate(_segments_file(), None, tmp_path)
        assert script.segments[0].narration == "Our SaaS rocks."
        assert script.segments[0].tts_text == "Our sass rocks."

    def test_missing_segment_raises(self, tmp_path: Path) -> None:
        backend = StubBackend([_response({0: "only one"})])
        with pytest.raises(ScriptGenerationError, match=r"missing narration for segment\(s\) \[1\]"):
            _generator(backend).generate(_segments_file(), None, tmp_path)


class TestBudgetRepair:
    def test_over_budget_triggers_single_text_only_repair(self, tmp_path: Path) -> None:
        # segment 0 budget = 23 words; 30 words is >15% over
        long_text = " ".join(["word"] * 30)
        short_text = " ".join(["word"] * 20)
        backend = StubBackend(
            [
                _response({0: long_text, 1: "fine text"}),
                _response({0: short_text, 1: "SHOULD BE IGNORED"}),
            ]
        )
        script = _generator(backend).generate(_segments_file(), None, tmp_path)
        assert len(backend.requests) == 2
        repair_req = backend.requests[1]
        assert not repair_req.include_images  # repair is text-only
        assert "segment 0" in repair_req.prompt_text
        # only the offending segment is replaced; the good one is kept verbatim
        assert script.segments[0].narration == short_text
        assert script.segments[1].narration == "fine text"

    def test_within_budget_makes_no_repair_call(self, tmp_path: Path) -> None:
        backend = StubBackend([_response({0: "short", 1: "also short"})])
        _generator(backend).generate(_segments_file(), None, tmp_path)
        assert len(backend.requests) == 1

    def test_still_over_after_repair_proceeds_with_warning(self, tmp_path: Path) -> None:
        long_text = " ".join(["word"] * 40)
        backend = StubBackend(
            [
                _response({0: long_text, 1: "fine"}),
                _response({0: long_text, 1: "fine"}),  # repair does not help
            ]
        )
        script = _generator(backend).generate(_segments_file(), None, tmp_path)
        assert len(backend.requests) == 2  # exactly ONE repair attempt
        assert script.segments[0].narration == long_text
