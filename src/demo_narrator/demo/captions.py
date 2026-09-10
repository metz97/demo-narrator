"""TikTok-style karaoke captions: a rolling window of a few words with the word
currently being spoken highlighted, burned into the video.

Kokoro does not emit word timestamps, so word timings are estimated by
distributing each narration segment's measured duration across its words
(weighted by length). That is accurate enough for the karaoke effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ASS colours are &HBBGGRR. White base text, yellow active word, black outline.
_ACTIVE_COLOUR = "&H00FFFF&"  # yellow
_BASE_COLOUR = "&H00FFFFFF"


@dataclass(frozen=True)
class CaptionSegment:
    text: str
    start: float
    end: float


def estimate_word_timings(text: str, start: float, end: float) -> list[tuple[str, float, float]]:
    words = text.split()
    if not words or end <= start:
        return []
    weights = [len(w) + 1 for w in words]
    total = sum(weights)
    span = end - start
    out: list[tuple[str, float, float]] = []
    t = start
    for w, wt in zip(words, weights):
        d = span * wt / total
        out.append((w, t, t + d))
        t += d
    return out


def _ass_ts(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(word: str) -> str:
    # ASS override blocks use { }; strip any stray braces/backslashes from words.
    return re.sub(r"[{}\\]", "", word)


def _header(width: int, height: int, font_size: int) -> str:
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,Arial,{font_size},{_BASE_COLOUR},&H000000FF,&H00000000,&H64000000,"
        "1,0,0,0,100,100,0,0,1,3,1,2,80,80,90,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text\n"
    )


def build_ass(
    segments: list[CaptionSegment],
    *,
    width: int = 1280,
    height: int = 720,
    window: int = 3,
    font_size: int = 54,
) -> str:
    """Render caption segments to an ASS subtitle document with karaoke windows."""
    lines = [_header(width, height, font_size)]
    for seg in segments:
        timings = estimate_word_timings(seg.text, seg.start, seg.end)
        for base in range(0, len(timings), window):
            group = timings[base : base + window]
            group_words = [_clean(w) for (w, _, _) in group]
            for active, (_word, ws, we) in enumerate(group):
                rendered = []
                for j, gw in enumerate(group_words):
                    if j == active:
                        rendered.append(f"{{\\c{_ACTIVE_COLOUR}\\b1}}{gw}{{\\c{_BASE_COLOUR}\\b1}}")
                    else:
                        rendered.append(gw)
                text = " ".join(rendered)
                # Fields: Layer,Start,End,Style,Name,MarginL,MarginR,Effect,Text
                lines.append(f"Dialogue: 0,{_ass_ts(ws)},{_ass_ts(we)},Cap,,0,0,,{text}")
    return "\n".join(lines) + "\n"
