"""Prompt construction and the JSON schema requested from Claude."""

from __future__ import annotations

from typing import Any

# Embedded fallback; the editable copy lives at config/style.md.
DEFAULT_STYLE_GUIDE = """\
# Narration style guide

- Audience: internal colleagues from other teams; plain business English.
- Arc per feature: the problem it solves -> what users can now do -> the impact.
- Never narrate obvious UI mechanics ("now I click save", "I scroll down").
- Short sentences (20 words or fewer). Active voice. No idioms or slang.
- TTS-friendly output:
  - Spell acronyms letter-by-letter with hyphens (A-P-I, S-S-O, C-R-M).
  - Write numbers as spoken words ("twenty three", not "23").
  - No markdown, bullets, quotes, or special characters in narration text.
- Confident, warm, professional tone -- a colleague proudly showing new work.
- Do not invent features you cannot see in the frames or the sprint context.
"""

SCRIPT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "start_sec": {"type": "number"},
                    "end_sec": {"type": "number"},
                    "narration": {"type": "string"},
                },
                "required": ["index", "start_sec", "end_sec", "narration"],
                "additionalProperties": False,
            },
        },
        "title": {"type": "string"},
        "detected_features": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["segments", "title", "detected_features"],
    "additionalProperties": False,
}

INTRO = """\
You are writing the voiceover script for a demo video of a web application.
The video was split into segments by scene detection. For each segment you get
its time range, a hard word budget, and one to three representative screenshots
(keyframes). Write narration for EVERY segment.

Hard rules:
- Output exactly one narration per segment, keyed by the segment's index.
- Respect each segment's word budget (the narration is spoken at roughly two
  point three words per second; longer text will not fit the segment).
- Echo start_sec and end_sec unchanged.
- Narration must describe what is actually visible / plausible from the frames
  and the provided context.
"""

REPAIR_INSTRUCTIONS = """\
Some narrations exceed their word budget and must be shortened. Rewrite ONLY
the segments listed below so each fits its budget; keep every other segment's
narration EXACTLY as given. Return the complete JSON document again (same
schema, all segments).
"""


def segment_header(index: int, start_sec: float, end_sec: float, max_words: int) -> str:
    return (
        f"Segment {index}: {start_sec:.1f}s to {end_sec:.1f}s "
        f"({end_sec - start_sec:.1f}s long, word budget: {max_words} words)"
    )


def context_block(context: str | None) -> str:
    if context is None:
        return (
            "No sprint/ticket context was provided. Base the narration purely on "
            "what is visible in the keyframes."
        )
    return (
        "Context about the work being demonstrated (sprint work items / notes):\n"
        "<context>\n" + context.strip() + "\n</context>"
    )
