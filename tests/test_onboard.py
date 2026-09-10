"""Setup-assistant conversation logic (pure; app inspection is stubbed)."""

from __future__ import annotations

from typing import Any

import yaml

from demo_narrator.demo import onboard
from demo_narrator.demo.onboard import (
    _merge_decision,
    build_onboard_prompt,
    empty_state,
    merged_profile_yaml,
    run_onboard_turn,
)
from demo_narrator.demo.profile import ProjectProfile, expand_env


def test_merge_decision_updates_draft() -> None:
    state = empty_state()
    _merge_decision(state, {
        "context": "  An app.  ",
        # display-label names must be slugified to safe keys
        "feature_map": [{"name": "Object Search", "path": "/search"},
                        {"name": "Projects Overview (draft)", "path": "/projects"}],
        "sign_in_path": "/login",
        "look_at": ["/x", "/y"],
        "ready": True,
    })
    d = state["draft"]
    assert d["context"] == "An app."
    assert d["feature_map"] == {"object_search": "/search", "projects_overview_draft": "/projects"}
    assert d["sign_in_path"] == "/login"
    assert d["ready"] is True
    assert state["pending_look_at"] == ["/x", "/y"]


def test_merge_decision_blank_context_kept_and_feature_map_replaced() -> None:
    state = empty_state()
    state["draft"]["feature_map"] = {"a": "/a"}
    state["draft"]["context"] = "keep"
    _merge_decision(state, {"context": "   ", "feature_map": [{"name": "b", "path": "/b"}]})
    assert state["draft"]["context"] == "keep"          # a blank context must not wipe it
    assert state["draft"]["feature_map"] == {"b": "/b"}  # full list replaces the previous one
    assert state["pending_look_at"] == []               # no look_at -> cleared


def test_merge_decision_empty_feature_map_does_not_wipe() -> None:
    state = empty_state()
    state["draft"]["feature_map"] = {"a": "/a"}
    _merge_decision(state, {"feature_map": []})          # nothing proposed this turn
    assert state["draft"]["feature_map"] == {"a": "/a"}  # keep what we had


def test_merged_profile_yaml_applies_and_validates() -> None:
    current = "name: app\nbase_url: http://x\n"
    draft = {"context": "About the app.", "feature_map": {"search": "/search"},
             "sign_in_path": "/login", "ready": True}
    out = merged_profile_yaml(current, draft)
    assert "About the app." in out and "/search" in out and "/login" in out
    prof = ProjectProfile.model_validate(expand_env(yaml.safe_load(out)))
    assert prof.context == "About the app."
    assert prof.feature_map["search"] == "/search"
    assert prof.auth.sign_in_path == "/login"


def test_run_onboard_turn_with_fake_backend(monkeypatch: Any) -> None:
    prof = ProjectProfile(name="app", base_url="http://x")
    # stub inspection so no browser launches
    monkeypatch.setattr(
        onboard, "inspect_app",
        lambda *a, **k: [{"path": "/", "signed_in": False, "final_path": "/sign-in"}],
    )

    class FakeBackend:
        name = "fake"

        def ask(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            assert "SETUP ASSISTANT" in prompt      # the onboarding prompt was built
            assert "hello there" in prompt          # the user message reached the model
            return {
                "reply": "Hi! I can see a sign-in page.",
                "questions": ["What does this app do?"],
                "context": "An app for X.",
                "feature_map": [{"name": "home", "path": "/"}],
                "sign_in_path": "/sign-in",
                "look_at": ["/home"],
                "ready": False,
            }

    state = run_onboard_turn(prof, FakeBackend(), empty_state(), "hello there", storage_state=None)
    roles = [m["role"] for m in state["messages"]]
    assert roles[0] == "user" and roles[-1] == "assistant"
    assert "system" in roles                        # the "🔎 Looked at…" line
    assert state["messages"][-1]["text"].startswith("Hi!")
    assert state["messages"][-1]["questions"] == ["What does this app do?"]
    assert state["draft"]["context"] == "An app for X."
    assert state["draft"]["feature_map"] == {"home": "/"}
    assert state["pending_look_at"] == ["/home"]


def test_build_prompt_greets_when_no_message() -> None:
    prof = ProjectProfile(name="app", base_url="http://x")
    prompt = build_onboard_prompt(prof, empty_state(), "", "(no inspection)")
    assert "opened the assistant without typing" in prompt
