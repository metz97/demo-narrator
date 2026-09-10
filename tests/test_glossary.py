"""Unit tests for pronunciation glossary substitution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo_narrator.errors import ConfigError
from demo_narrator.glossary import apply_glossary, load_glossary


def test_whole_word_replacement() -> None:
    glossary = {"SaaS": "sass"}
    assert apply_glossary("Our SaaS platform.", glossary) == "Our sass platform."


def test_partial_words_untouched() -> None:
    glossary = {"API": "A-P-I"}
    assert apply_glossary("APIs and RAPID stay as-is", glossary) == "APIs and RAPID stay as-is"


def test_case_sensitive() -> None:
    glossary = {"nginx": "engine-ex"}
    assert apply_glossary("Nginx vs nginx", glossary) == "Nginx vs engine-ex"


def test_longest_term_wins_on_overlap() -> None:
    glossary = {"SQL": "sequel", "PostgreSQL": "post-gress-cue-ell"}
    assert apply_glossary("PostgreSQL and SQL", glossary) == "post-gress-cue-ell and sequel"


def test_replacement_result_not_reprocessed() -> None:
    # "Hub-exo" contains "exo"; a glossary entry for "exo" must not corrupt it
    glossary = {"Hubexo": "Hub-exo"}
    assert apply_glossary("Hubexo dashboard", glossary) == "Hub-exo dashboard"


def test_empty_glossary_is_identity() -> None:
    assert apply_glossary("anything at all", {}) == "anything at all"


def test_load_glossary_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_glossary(tmp_path / "nope.json") == {}


def test_load_glossary_rejects_bad_json(tmp_path: Path) -> None:
    f = tmp_path / "glossary.json"
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_glossary(f)


def test_load_glossary_rejects_non_string_values(tmp_path: Path) -> None:
    f = tmp_path / "glossary.json"
    f.write_text(json.dumps({"API": 3}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_glossary(f)
