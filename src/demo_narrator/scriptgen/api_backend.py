"""Anthropic API backend (claude.mode=api_key) — direct Messages API with vision.

Keyframes are sent as base64 image blocks interleaved with per-segment headers;
structured JSON is enforced via output_config.format (json_schema).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, cast

from ..config import ClaudeAPIKeyConfig
from ..errors import ScriptGenerationError
from ..retry import RetryableError, with_backoff
from .generator import ScriptRequest
from .prompts import segment_header

logger = logging.getLogger(__name__)


def request_structured(
    *,
    model: str,
    max_tokens: int,
    api_key: str,
    content: list[dict[str, Any]],
    schema: dict[str, Any],
    description: str = "Anthropic API call",
) -> dict[str, Any]:
    """One structured-output Messages API call: content + JSON schema -> dict.

    Shared by the script-generation backend and the demo-generation decision
    backend. Handles retry/backoff (429/5xx/connection), refusals, and JSON
    parsing; raises ScriptGenerationError on non-retryable failures.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    def attempt() -> Any:
        try:
            return client.messages.create(
                model=model,
                max_tokens=max_tokens,
                output_config=cast(Any, {"format": {"type": "json_schema", "schema": schema}}),
                messages=cast(Any, [{"role": "user", "content": content}]),
            )
        except anthropic.RateLimitError as exc:
            retry_after = None
            try:
                retry_after = float(exc.response.headers.get("retry-after", ""))
            except (ValueError, AttributeError):
                pass
            raise RetryableError(f"rate limited: {exc.message}", retry_after) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise RetryableError(f"server error {exc.status_code}") from exc
            raise ScriptGenerationError(
                f"Anthropic API rejected the request ({exc.status_code}): {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise RetryableError(f"connection error: {exc}") from exc

    response = with_backoff(attempt, description=description)

    if response.stop_reason == "refusal":
        raise ScriptGenerationError(
            "Claude declined to process this request (stop_reason=refusal)."
        )
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ScriptGenerationError("Anthropic API response contained no text block.")
    logger.debug(
        "API usage: input=%s output=%s",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )
    try:
        result: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScriptGenerationError(f"Claude returned invalid JSON: {exc}") from exc
    return result


class APIKeyBackend:
    def __init__(self, cfg: ClaudeAPIKeyConfig, api_key: str) -> None:
        self._cfg = cfg
        self._api_key = api_key

    def generate(self, request: ScriptRequest) -> dict[str, Any]:
        return request_structured(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            api_key=self._api_key,
            content=self._build_content(request),
            schema=request.schema,
        )

    def _build_content(self, request: ScriptRequest) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt_text}]
        if not request.include_images:
            return content
        for sp in request.segments:
            content.append(
                {
                    "type": "text",
                    "text": segment_header(sp.index, sp.start_sec, sp.end_sec, sp.max_words),
                }
            )
            for kf in sp.keyframes:
                if not kf.exists():
                    raise ScriptGenerationError(f"Keyframe missing on disk: {kf}")
                data = base64.standard_b64encode(kf.read_bytes()).decode("ascii")
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": data,
                        },
                    }
                )
        return content
