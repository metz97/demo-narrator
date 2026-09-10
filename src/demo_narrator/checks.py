"""`demo-narrator config-check` — validate config, binaries, credentials, connectivity."""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import AppConfig, Secrets
from .errors import DemoNarratorError
from .ffmpeg import FFmpeg
from .glossary import load_glossary
from .tts.base import create_provider


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    fatal: bool = True  # warnings (fatal=False) don't fail the check


def run_all_checks(config: AppConfig, secrets: Secrets) -> list[CheckResult]:
    results: list[CheckResult] = []
    ffmpeg = FFmpeg(config.ffmpeg.ffmpeg_path, config.ffmpeg.ffprobe_path)

    # ffmpeg / ffprobe
    try:
        ffmpeg_version, ffprobe_version = ffmpeg.check_available()
        results.append(CheckResult("ffmpeg", True, ffmpeg_version))
        results.append(CheckResult("ffprobe", True, ffprobe_version))
    except DemoNarratorError as exc:
        results.append(CheckResult("ffmpeg/ffprobe", False, str(exc)))

    # glossary + style files
    try:
        glossary = load_glossary(Path(config.glossary_path))
        results.append(
            CheckResult("glossary", True, f"{len(glossary)} term(s) in {config.glossary_path}")
        )
    except DemoNarratorError as exc:
        results.append(CheckResult("glossary", False, str(exc)))
    style = Path(config.style_path)
    results.append(
        CheckResult(
            "style guide", True,
            f"{style} (found)" if style.exists() else "using embedded default (config/style.md not found)",
            fatal=False,
        )
    )

    # Claude backend
    if config.claude.mode == "agent_sdk":
        from .claude_cli import ensure_claude_on_path

        sdk_ok = importlib.util.find_spec("claude_agent_sdk") is not None
        cli_path = ensure_claude_on_path()  # also probes ~/.local/bin, %APPDATA%/npm, etc.
        cli_ok = cli_path is not None
        if sdk_ok and cli_ok:
            results.append(
                CheckResult("claude (agent_sdk)", True, f"claude-agent-sdk + Claude Code CLI ({cli_path})")
            )
        elif not sdk_ok:
            results.append(
                CheckResult(
                    "claude (agent_sdk)", False,
                    "claude-agent-sdk is not installed (pip install claude-agent-sdk), "
                    "or set claude.mode: api_key.",
                )
            )
        else:
            results.append(
                CheckResult(
                    "claude (agent_sdk)", False,
                    "Claude Code CLI ('claude') not found on PATH. Install it from "
                    "https://claude.com/claude-code and log in, or set claude.mode: api_key.",
                )
            )
    else:
        if not secrets.anthropic_api_key:
            results.append(
                CheckResult(
                    "claude (api_key)", False,
                    "ANTHROPIC_API_KEY is not set (required for claude.mode: api_key).",
                )
            )
        else:
            results.append(_check_anthropic_connectivity(config, secrets.anthropic_api_key))

    # TTS provider
    try:
        provider = create_provider(config.tts.provider, config.tts, secrets, ffmpeg)
        problems = provider.check()
        if problems:
            results.append(CheckResult(f"tts ({provider.name})", False, " ".join(problems)))
        else:
            detail = "ready"
            if provider.name == "elevenlabs" and secrets.elevenlabs_api_key:
                detail = _check_elevenlabs_connectivity(secrets.elevenlabs_api_key)
            results.append(CheckResult(f"tts ({provider.name})", True, detail))
    except DemoNarratorError as exc:
        results.append(CheckResult("tts", False, str(exc)))

    # Azure DevOps (optional feature -> warnings only)
    if config.azure_devops.organization:
        az_found = shutil.which("az") is not None or shutil.which("az.cmd") is not None
        results.append(
            CheckResult(
                "azure devops cli", az_found,
                "az CLI found" if az_found
                else "az CLI not found — --sprint/--task will not work "
                     "(https://aka.ms/azure-cli, then: az extension add --name azure-devops)",
                fatal=False,
            )
        )
        results.append(
            CheckResult(
                "azure devops pat", bool(secrets.azure_devops_pat),
                "AZURE_DEVOPS_EXT_PAT is set" if secrets.azure_devops_pat
                else "AZURE_DEVOPS_EXT_PAT not set — --sprint/--task will be skipped",
                fatal=False,
            )
        )
    return results


def _check_anthropic_connectivity(config: AppConfig, api_key: str) -> CheckResult:
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        model = client.models.retrieve(config.claude.api_key.model)
        return CheckResult("claude (api_key)", True, f"API reachable; model {model.id} available")
    except Exception as exc:  # connectivity is best-effort; classify auth failures as fatal
        return CheckResult("claude (api_key)", False, f"Anthropic API check failed: {exc}")


def _check_elevenlabs_connectivity(api_key: str) -> str:
    try:
        response = httpx.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": api_key},
            timeout=15.0,
        )
        if response.status_code == 200:
            return "API key valid, ElevenLabs reachable"
        return f"ElevenLabs responded with HTTP {response.status_code} — check ELEVENLABS_API_KEY"
    except httpx.HTTPError as exc:
        return f"ElevenLabs not reachable right now ({exc}) — key presence OK"
