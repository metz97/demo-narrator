"""Configuration: config/config.yaml (settings) + environment variables (secrets).

Secrets are NEVER stored in the YAML file and NEVER logged. They are read from
the environment (optionally populated from a .env file by the CLI):

    ANTHROPIC_API_KEY      -- Stage 3 when claude.mode == "api_key"
    ELEVENLABS_API_KEY     -- Stage 4 when tts.provider == "elevenlabs"
    AZURE_DEVOPS_EXT_PAT   -- Stage 2 Azure DevOps CLI authentication
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path("config") / "config.yaml"
DEFAULT_STYLE_PATH = Path("config") / "style.md"
DEFAULT_GLOSSARY_PATH = Path("config") / "glossary.json"


class FFmpegConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"


class SegmentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_threshold: float = Field(default=0.25, gt=0.0, lt=1.0)
    min_segment_sec: float = Field(default=4.0, gt=0.0)
    max_keyframes_per_segment: int = Field(default=3, ge=1, le=3)
    keyframe_max_edge: int = Field(default=1568, ge=256)
    min_video_duration_sec: float = Field(default=10.0, gt=0.0)


class ClaudeAgentSDKConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None inherits the user's Claude Code default model; or an alias like "opus".
    model: str | None = None
    max_turns: int = Field(default=40, ge=1)


class ClaudeAPIKeyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "claude-opus-4-8"
    max_tokens: int = Field(default=16000, ge=1024)


class ClaudeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["agent_sdk", "api_key"] = "agent_sdk"
    max_repair_attempts: int = Field(default=1, ge=0)
    agent_sdk: ClaudeAgentSDKConfig = ClaudeAgentSDKConfig()
    api_key: ClaudeAPIKeyConfig = ClaudeAPIKeyConfig()


class KokoroConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice: str = "af_heart"
    lang_code: str = "a"  # 'a' = American English, 'b' = British English
    speed: float = Field(default=1.0, gt=0.25, lt=4.0)


class ElevenLabsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # "Rachel", a stock narration voice
    model_id: str = "eleven_multilingual_v2"
    stability: float = Field(default=0.5, ge=0.0, le=1.0)
    similarity_boost: float = Field(default=0.75, ge=0.0, le=1.0)


class TTSConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["kokoro", "elevenlabs"] = "kokoro"
    # Hard cap on characters synthesized by PAID providers only.
    max_tts_characters: int = Field(default=10000, ge=0)
    kokoro: KokoroConfig = KokoroConfig()
    elevenlabs: ElevenLabsConfig = ElevenLabsConfig()


class AssemblyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fade_ms: int = Field(default=50, ge=0, le=1000)
    video_crf: int = Field(default=20, ge=0, le=51)
    video_preset: str = "medium"
    audio_sample_rate: int = 44100
    audio_bitrate: str = "192k"


class AzureDevOpsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization: str = ""  # e.g. https://dev.azure.com/yourorg
    project: str = ""
    team: str = ""  # optional; used to resolve the iteration path for --sprint


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ffmpeg: FFmpegConfig = FFmpegConfig()
    segmentation: SegmentationConfig = SegmentationConfig()
    claude: ClaudeConfig = ClaudeConfig()
    tts: TTSConfig = TTSConfig()
    assembly: AssemblyConfig = AssemblyConfig()
    azure_devops: AzureDevOpsConfig = AzureDevOpsConfig()
    # Root that profiles/, output/ and the config-relative paths below resolve
    # against. "." keeps CLI behaviour (current directory); servers point it at
    # their workspace checkout.
    workspace_root: str = "."
    output_dir: str = "output"
    style_path: str = str(DEFAULT_STYLE_PATH)
    glossary_path: str = str(DEFAULT_GLOSSARY_PATH)

    @property
    def workspace(self) -> Path:
        return Path(self.workspace_root)

    def resolve(self, path_value: str | Path) -> Path:
        """Resolve a possibly-relative configured path against the workspace root.

        Absolute paths pass through unchanged (pathlib joining with an absolute
        path yields that absolute path).
        """
        return self.workspace / Path(path_value)


class Secrets(BaseModel):
    """Secret values read from the environment. Never log this object."""

    model_config = ConfigDict(extra="forbid")

    anthropic_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    azure_devops_pat: str | None = None

    def __repr__(self) -> str:  # defence in depth: never leak values via repr
        present = [k for k, v in self.__dict__.items() if v]
        return f"Secrets(present={present})"

    __str__ = __repr__


def load_config(path: Path | None = None) -> AppConfig:
    """Load config/config.yaml; a missing file yields all defaults."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        if path is not None:
            raise ConfigError(
                f"Config file not found: {cfg_path}. "
                "Copy config/config.example.yaml to config/config.yaml to get started."
            )
        return AppConfig()
    try:
        raw: Any = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path} must contain a YAML mapping at the top level.")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {cfg_path}:\n{exc}") from exc


def load_secrets() -> Secrets:
    return Secrets(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY") or None,
        azure_devops_pat=os.environ.get("AZURE_DEVOPS_EXT_PAT") or None,
    )
