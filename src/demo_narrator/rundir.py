"""Run directory management: every run keeps all intermediates so any stage
can be re-run or resumed without repeating earlier stages."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .errors import RunDirError
from .models import Script, SegmentsFile

logger = logging.getLogger(__name__)

SEGMENTS_JSON = "segments.json"
CONTEXT_MD = "context.md"
SCRIPT_JSON = "script.json"
MANIFEST_JSON = "manifest.json"
AUDIO_DIR = "audio"
WORK_DIR = "work"
RUN_LOG = "run.log"


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_at: str
    source_video: str
    stages_completed: list[str] = []
    tts_provider: str | None = None
    config_snapshot: dict[str, Any] = {}


class RunDir:
    """A timestamped directory holding every intermediate artifact of one run."""

    def __init__(self, path: Path) -> None:
        self.path = path

    # -- creation / loading --------------------------------------------------

    @classmethod
    def create(cls, output_dir: Path, video: Path, config_snapshot: dict[str, Any]) -> "RunDir":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = output_dir / "runs" / f"{stamp}-{video.stem}"
        path.mkdir(parents=True, exist_ok=False)
        run = cls(path)
        manifest = RunManifest(
            created_at=datetime.now().isoformat(timespec="seconds"),
            source_video=str(video.resolve()),
            config_snapshot=config_snapshot,
        )
        run._write_manifest(manifest)
        logger.info("Run directory: %s", path)
        return run

    @classmethod
    def load(cls, path: Path) -> "RunDir":
        if not path.is_dir():
            raise RunDirError(f"Run directory not found: {path}")
        if not (path / MANIFEST_JSON).exists():
            raise RunDirError(
                f"{path} is not a demo-narrator run directory (missing {MANIFEST_JSON})."
            )
        return cls(path)

    # -- manifest --------------------------------------------------------------

    def manifest(self) -> RunManifest:
        raw = (self.path / MANIFEST_JSON).read_text(encoding="utf-8")
        return RunManifest.model_validate_json(raw)

    def _write_manifest(self, manifest: RunManifest) -> None:
        (self.path / MANIFEST_JSON).write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )

    def mark_stage_complete(self, stage: str, *, tts_provider: str | None = None) -> None:
        manifest = self.manifest()
        if stage not in manifest.stages_completed:
            manifest.stages_completed.append(stage)
        if tts_provider is not None:
            manifest.tts_provider = tts_provider
        self._write_manifest(manifest)

    def stage_complete(self, stage: str) -> bool:
        return stage in self.manifest().stages_completed

    def invalidate_stages(self, stages: list[str]) -> None:
        manifest = self.manifest()
        manifest.stages_completed = [s for s in manifest.stages_completed if s not in stages]
        self._write_manifest(manifest)

    # -- typed artifact accessors ----------------------------------------------

    @property
    def log_file(self) -> Path:
        return self.path / RUN_LOG

    @property
    def audio_dir(self) -> Path:
        return self.path / AUDIO_DIR

    @property
    def work_dir(self) -> Path:
        return self.path / WORK_DIR

    def audio_path(self, index: int) -> Path:
        return self.audio_dir / f"segment_{index:03d}.wav"

    def save_segments(self, segments: SegmentsFile) -> None:
        (self.path / SEGMENTS_JSON).write_text(
            segments.model_dump_json(indent=2), encoding="utf-8"
        )

    def load_segments(self) -> SegmentsFile:
        f = self.path / SEGMENTS_JSON
        if not f.exists():
            raise RunDirError(f"{f} not found — Stage 1 has not run in this directory.")
        return SegmentsFile.model_validate_json(f.read_text(encoding="utf-8"))

    def save_context(self, text: str | None) -> None:
        if text:
            (self.path / CONTEXT_MD).write_text(text, encoding="utf-8")

    def load_context(self) -> str | None:
        f = self.path / CONTEXT_MD
        return f.read_text(encoding="utf-8") if f.exists() else None

    def save_script(self, script: Script) -> None:
        (self.path / SCRIPT_JSON).write_text(script.model_dump_json(indent=2), encoding="utf-8")

    def load_script(self) -> Script:
        f = self.path / SCRIPT_JSON
        if not f.exists():
            raise RunDirError(
                f"{f} not found — Stage 3 has not run in this directory. "
                "Run 'demo-narrator generate' first."
            )
        try:
            return Script.model_validate_json(f.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RunDirError(f"{f} is not a valid script file: {exc}") from exc

    def save_json(self, name: str, data: Any) -> None:
        (self.path / name).write_text(json.dumps(data, indent=2), encoding="utf-8")
