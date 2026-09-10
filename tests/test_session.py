"""ensure_session: the preflight that keeps jobs off the sign-in page."""

from __future__ import annotations

from pathlib import Path

import pytest

from demo_narrator.demo import runner
from demo_narrator.demo.models import Step
from demo_narrator.demo.profile import AuthConfig, LoginRecipe, ProjectProfile
from demo_narrator.errors import PlaywrightError


def _profile(tmp_path: Path, *, with_recipe: bool) -> ProjectProfile:
    recipe = LoginRecipe(steps=[Step(action="goto", path="/sign-in")]) if with_recipe else None
    return ProjectProfile(
        name="app",
        base_url="http://localhost:1",
        auth=AuthConfig(
            strategy="storage_state",
            storage_state=str(tmp_path / "state.json"),
            login_recipe=recipe,
        ),
    )


def test_valid_session_passes_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prof = _profile(tmp_path, with_recipe=False)
    Path(prof.auth.storage_state).write_text("{}", encoding="utf-8")  # type: ignore[arg-type]
    monkeypatch.setattr(runner, "session_is_valid", lambda *a, **k: True)
    assert runner.ensure_session(prof) == prof.auth.storage_state


def test_expired_without_recipe_raises_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prof = _profile(tmp_path, with_recipe=False)
    Path(prof.auth.storage_state).write_text("{}", encoding="utf-8")  # type: ignore[arg-type]
    monkeypatch.setattr(runner, "session_is_valid", lambda *a, **k: False)
    with pytest.raises(PlaywrightError, match="demo-auth"):
        runner.ensure_session(prof)


def test_expired_with_recipe_auto_refreshes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prof = _profile(tmp_path, with_recipe=True)
    Path(prof.auth.storage_state).write_text("{}", encoding="utf-8")  # type: ignore[arg-type]
    calls = {"capture": 0, "checks": 0}

    def fake_valid(*a: object, **k: object) -> bool:
        calls["checks"] += 1
        return calls["checks"] > 1  # invalid first, valid after the refresh

    monkeypatch.setattr(runner, "session_is_valid", fake_valid)
    monkeypatch.setattr(
        runner, "capture_session",
        lambda *a, **k: calls.__setitem__("capture", calls["capture"] + 1),
    )
    assert runner.ensure_session(prof) == prof.auth.storage_state
    assert calls["capture"] == 1


def test_missing_file_with_recipe_refreshes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prof = _profile(tmp_path, with_recipe=True)  # state file never created
    monkeypatch.setattr(runner, "session_is_valid", lambda *a, **k: True)
    captured = []
    monkeypatch.setattr(runner, "capture_session", lambda *a, **k: captured.append(1))
    assert runner.ensure_session(prof) == prof.auth.storage_state
    assert captured == [1]


def test_recipe_that_does_not_fix_gate_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prof = _profile(tmp_path, with_recipe=True)
    monkeypatch.setattr(runner, "session_is_valid", lambda *a, **k: False)
    monkeypatch.setattr(runner, "capture_session", lambda *a, **k: None)
    with pytest.raises(PlaywrightError, match="still shows its sign-in gate"):
        runner.ensure_session(prof)


def test_non_storage_strategies_untouched(tmp_path: Path) -> None:
    prof = ProjectProfile(name="x", base_url="http://h", auth=AuthConfig(strategy="none"))
    assert runner.ensure_session(prof) is None


def test_goto_url_normalization() -> None:
    prof = ProjectProfile(
        name="x", base_url="http://localhost:4200", route_prefix="/{locale}", default_locale="en"
    )
    # documented convention: locale-free path
    assert runner.goto_url(prof, "/search?search=door", "en") == "http://localhost:4200/en/search?search=door"
    # model copied the locale prefix from a hint URL — must NOT double it
    assert runner.goto_url(prof, "/en/search/objects?search=door", "en") == (
        "http://localhost:4200/en/search/objects?search=door"
    )
    # full URLs pass through verbatim
    assert runner.goto_url(prof, "http://localhost:4200/en/search?search=door", "en") == (
        "http://localhost:4200/en/search?search=door"
    )
    # a path merely STARTING with the locale letters is not a prefix match
    assert runner.goto_url(prof, "/enterprise", "en") == "http://localhost:4200/en/enterprise"
    # bare prefix collapses to the app root
    assert runner.goto_url(prof, "/en", "en") == "http://localhost:4200/en/"


def test_looks_signed_in_gate_detection() -> None:
    prof = ProjectProfile(name="x", base_url="http://localhost:4200")
    ok = runner._looks_signed_in
    assert ok(prof, "http://localhost:4200/en/projects") is True
    assert ok(prof, "http://localhost:4200/sign-in") is False            # the gate
    assert ok(prof, "http://localhost:4200/en/callback?code=1") is False  # OAuth bounce
    assert ok(prof, "https://auth.staging.se.hubexo.dev/login?x=1") is False  # external IdP

