"""Decision backends (factory, api_key path with a faked anthropic SDK) and
workspace path resolution."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from demo_narrator.config import AppConfig, ClaudeConfig, Secrets
from demo_narrator.demo.decision import (
    AgentSDKDecision,
    APIKeyDecision,
    create_decision_backend,
)
from demo_narrator.errors import ScriptGenerationError

_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def test_factory_selects_backend_by_mode() -> None:
    secrets = Secrets(anthropic_api_key="sk-test")
    assert isinstance(
        create_decision_backend(ClaudeConfig(mode="api_key"), secrets), APIKeyDecision
    )
    assert isinstance(
        create_decision_backend(ClaudeConfig(mode="agent_sdk"), secrets), AgentSDKDecision
    )


def test_factory_requires_key_in_api_mode() -> None:
    with pytest.raises(ScriptGenerationError, match="ANTHROPIC_API_KEY"):
        create_decision_backend(ClaudeConfig(mode="api_key"), Secrets())


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.stop_reason = stop_reason
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()


def _install_fake_anthropic(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Messages:
        def create(self, **kwargs: Any) -> _FakeResponse:
            captured.update(kwargs)
            return response

    class _Client:
        def __init__(self, api_key: str) -> None:
            captured["api_key"] = api_key
            self.messages = _Messages()

    fake = ModuleType("anthropic")
    fake.Anthropic = _Client  # type: ignore[attr-defined]
    for name in ("RateLimitError", "APIStatusError", "APIConnectionError"):
        setattr(fake, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return captured


def test_api_key_decision_calls_messages_api(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_fake_anthropic(monkeypatch, _FakeResponse('{"ok": true}'))
    backend = create_decision_backend(ClaudeConfig(mode="api_key"), Secrets(anthropic_api_key="sk-x"))
    result = backend.ask("hello", _SCHEMA)
    assert result == {"ok": True}
    assert captured["api_key"] == "sk-x"
    assert captured["messages"][0]["content"] == [{"type": "text", "text": "hello"}]
    assert captured["output_config"]["format"]["schema"] == _SCHEMA


def test_api_key_decision_surfaces_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_anthropic(monkeypatch, _FakeResponse("", stop_reason="refusal"))
    backend = create_decision_backend(ClaudeConfig(mode="api_key"), Secrets(anthropic_api_key="sk-x"))
    with pytest.raises(ScriptGenerationError, match="refusal"):
        backend.ask("hello", _SCHEMA)


def test_workspace_resolve() -> None:
    cfg = AppConfig(workspace_root="C:/ws")
    assert cfg.resolve("profiles") == Path("C:/ws/profiles")
    # absolute paths pass through untouched
    assert cfg.resolve("D:/abs/x.json") == Path("D:/abs/x.json")
    # default keeps CWD-relative behaviour
    assert AppConfig().resolve("output") == Path("output")
