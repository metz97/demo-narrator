"""Pronunciation glossary: term -> phonetic spelling, applied to TTS text."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import ConfigError


def load_glossary(path: Path) -> dict[str, str]:
    """Load config/glossary.json; a missing file is simply an empty glossary."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Glossary file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ConfigError(f"Glossary file {path} must map strings to strings.")
    return raw


def apply_glossary(text: str, glossary: dict[str, str]) -> str:
    """Replace whole-word occurrences of each term with its phonetic spelling.

    Longest terms first so overlapping terms ("PostgreSQL" vs "SQL") resolve
    deterministically. Matching is case-sensitive: glossary keys are product
    names / acronyms whose casing is meaningful.
    """
    for term in sorted(glossary, key=len, reverse=True):
        pattern = r"(?<![\w-])" + re.escape(term) + r"(?![\w-])"
        text = re.sub(pattern, glossary[term], text)
    return text
