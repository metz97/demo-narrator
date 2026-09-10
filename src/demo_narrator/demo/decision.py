"""Model backends for the supervised generation loop.

One contract: a prompt plus a JSON schema in, a validated dict out.

- ``agent_sdk`` — the local Claude Code CLI/subscription (developer machines).
- ``api_key``  — the Anthropic API (servers; per-developer subscription auth
  doesn't apply there). Selected via ``claude.mode`` in config.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Protocol, runtime_checkable

from ..config import ClaudeAgentSDKConfig, ClaudeAPIKeyConfig, ClaudeConfig, Secrets
from ..errors import ScriptGenerationError


@runtime_checkable
class DecisionBackend(Protocol):
    """A model that answers one prompt with schema-validated JSON."""

    name: str

    def ask(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


class AgentSDKDecision:
    """Single-turn structured query through the Claude Agent SDK (no tools)."""

    name = "agent_sdk"

    def __init__(self, cfg: ClaudeAgentSDKConfig) -> None:
        self._cfg = cfg

    def ask(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        from ..claude_cli import ensure_claude_on_path

        ensure_claude_on_path()

        async def _run() -> dict[str, Any]:
            from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

            options = ClaudeAgentOptions(
                allowed_tools=[],
                disallowed_tools=[
                    "Bash", "Read", "Write", "Edit", "WebSearch", "WebFetch", "Glob", "Grep",
                ],
                max_turns=1,
                model=self._cfg.model,
                setting_sources=[],
                output_format={"type": "json_schema", "schema": schema},
            )
            structured: dict[str, Any] | None = None
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    structured = getattr(message, "structured_output", None)
                    if getattr(message, "is_error", False):
                        raise ScriptGenerationError(
                            f"Claude error: {getattr(message, 'result', message)}"
                        )
            if structured is None:
                raise ScriptGenerationError("Claude returned no structured decision.")
            return structured

        # The sync Playwright API holds a running event loop in the caller's
        # thread, so the SDK's asyncio.run must happen in a fresh worker thread.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(_run())).result()


class APIKeyDecision:
    """Single structured Messages API call (server mode)."""

    name = "api_key"

    def __init__(self, cfg: ClaudeAPIKeyConfig, api_key: str) -> None:
        self._cfg = cfg
        self._api_key = api_key

    def ask(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        from ..scriptgen.api_backend import request_structured

        return request_structured(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            api_key=self._api_key,
            content=[{"type": "text", "text": prompt}],
            schema=schema,
            description="Anthropic decision call",
        )


def create_decision_backend(cfg: ClaudeConfig, secrets: Secrets) -> DecisionBackend:
    """Pick the backend from claude.mode (same switch the script pipeline uses)."""
    if cfg.mode == "api_key":
        if not secrets.anthropic_api_key:
            raise ScriptGenerationError(
                "claude.mode is 'api_key' but ANTHROPIC_API_KEY is not set. "
                "Export it (or switch claude.mode to agent_sdk)."
            )
        return APIKeyDecision(cfg.api_key, secrets.anthropic_api_key)
    return AgentSDKDecision(cfg.agent_sdk)
