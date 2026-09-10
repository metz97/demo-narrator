"""Claude Agent SDK backend (default) — draws on the user's Claude subscription.

The Agent SDK has no direct image parameter: Claude reads the keyframe JPEGs
itself via the Read tool (the run directory is the working directory and only
Read is allowed). Structured JSON is enforced with output_format=json_schema
and returned on ResultMessage.structured_output.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config import ClaudeAgentSDKConfig
from ..errors import ScriptGenerationError
from .generator import ScriptRequest

logger = logging.getLogger(__name__)

_SDK_HINT = (
    "The Claude Agent SDK is required for claude.mode=agent_sdk.\n"
    "  pip install claude-agent-sdk\n"
    "It also needs the Claude Code CLI installed and logged in "
    "(https://claude.com/claude-code). Alternatively set claude.mode=api_key "
    "in config/config.yaml and export ANTHROPIC_API_KEY."
)


class AgentSDKBackend:
    def __init__(self, cfg: ClaudeAgentSDKConfig, run_dir: Path) -> None:
        self._cfg = cfg
        self._run_dir = run_dir

    def generate(self, request: ScriptRequest) -> dict[str, Any]:
        from ..claude_cli import ensure_claude_on_path

        ensure_claude_on_path()
        try:
            return asyncio.run(self._generate_async(request))
        except ScriptGenerationError:
            raise
        except Exception as exc:  # surface SDK/transport failures with guidance
            raise ScriptGenerationError(
                f"Claude Agent SDK call failed: {exc}\n{_SDK_HINT}"
            ) from exc

    async def _generate_async(self, request: ScriptRequest) -> dict[str, Any]:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
        except ImportError as exc:
            raise ScriptGenerationError(_SDK_HINT) from exc

        prompt = self._build_prompt(request)
        options = ClaudeAgentOptions(
            cwd=str(self._run_dir),
            allowed_tools=["Read"],
            disallowed_tools=["Bash", "Write", "Edit", "WebSearch", "WebFetch", "Glob", "Grep"],
            max_turns=self._cfg.max_turns,
            model=self._cfg.model,
            setting_sources=[],
            output_format={"type": "json_schema", "schema": request.schema},
        )

        structured: dict[str, Any] | None = None
        result_text: str | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                structured = getattr(message, "structured_output", None)
                result_text = getattr(message, "result", None)
                if getattr(message, "is_error", False):
                    raise ScriptGenerationError(
                        f"Claude Agent SDK reported an error: {result_text or message}"
                    )
        if structured is None:
            raise ScriptGenerationError(
                "Claude Agent SDK finished without structured output. "
                f"Last result: {str(result_text)[:500]!r}"
            )
        return structured

    def _build_prompt(self, request: ScriptRequest) -> str:
        parts = [request.prompt_text]
        if request.include_images and request.segments:
            parts.append(
                "\nKeyframe images (use the Read tool to view EVERY file below "
                "before writing the script):"
            )
            for sp in request.segments:
                for kf in sp.keyframes:
                    parts.append(f"- segment {sp.index}: {kf}")
        return "\n".join(parts)
