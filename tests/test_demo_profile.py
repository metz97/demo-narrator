"""Project profile loading and env expansion (demo mode)."""

from __future__ import annotations

from pathlib import Path

import pytest

from demo_narrator.demo.profile import (
    ProjectProfile,
    expand_env,
    load_profile,
    resolve_profile_path,
)
from demo_narrator.errors import ProfileError

REAL_PROFILE = Path("profiles/lca-tool.yaml")


def test_expand_env_default_and_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DN_TEST_VAR", raising=False)
    assert expand_env("${DN_TEST_VAR:-fallback}") == "fallback"
    assert expand_env("${DN_TEST_VAR}") == ""
    monkeypatch.setenv("DN_TEST_VAR", "real")
    assert expand_env("${DN_TEST_VAR:-fallback}") == "real"
    assert expand_env({"a": ["${DN_TEST_VAR}", 1]}) == {"a": ["real", 1]}


def test_route_prefix_applies_locale() -> None:
    prof = ProjectProfile(
        name="x", base_url="http://h/", route_prefix="/{locale}", default_locale="en"
    )
    assert prof.route("/search") == "http://h/en/search"
    assert prof.route("/search", locale="de") == "http://h/de/search"


def test_real_profile_loads() -> None:
    prof = load_profile(REAL_PROFILE)
    assert prof.name == "lca-tool"
    assert prof.base_url.startswith("http")
    assert prof.selectors.test_id_attribute == "data-testid"
    assert prof.route("/projects").endswith("/en/projects")
    # login recipe is present as a fallback
    assert prof.auth.login_recipe is not None
    assert len(prof.auth.login_recipe.steps) >= 4


def test_resolve_profile_path() -> None:
    assert resolve_profile_path("lca-tool") == Path("profiles/lca-tool.yaml")


def test_missing_profile_errors(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load_profile(tmp_path / "nope.yaml")
