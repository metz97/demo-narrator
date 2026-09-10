"""End-to-end pipeline orchestration with run-directory resume."""

from __future__ import annotations

import logging
from pathlib import Path

from .assembly import assemble
from .config import AppConfig, Secrets
from .context import gather_context
from .costs import guard_tts_characters, report_vision_estimate
from .errors import ConfigError, ScriptGenerationError
from .ffmpeg import FFmpeg
from .glossary import load_glossary
from .logging_setup import attach_run_log, console
from .models import Script
from .rundir import RunDir
from .scriptgen.agent_backend import AgentSDKBackend
from .scriptgen.api_backend import APIKeyBackend
from .scriptgen.generator import ScriptGenerator, ScriptModelBackend
from .segmentation import segment_video
from .synthesis import synthesize_script
from .tts.base import create_provider

logger = logging.getLogger(__name__)

STAGE_SEGMENTATION = "segmentation"
STAGE_CONTEXT = "context"
STAGE_SCRIPT = "script"
STAGE_AUDIO = "audio"
STAGE_ASSEMBLY = "assembly"


def make_ffmpeg(config: AppConfig) -> FFmpeg:
    return FFmpeg(config.ffmpeg.ffmpeg_path, config.ffmpeg.ffprobe_path)


def _script_backend(config: AppConfig, secrets: Secrets, run_dir: Path) -> ScriptModelBackend:
    if config.claude.mode == "api_key":
        if not secrets.anthropic_api_key:
            raise ConfigError(
                "claude.mode is 'api_key' but ANTHROPIC_API_KEY is not set. "
                "Add it to your environment or .env file, or switch to claude.mode: agent_sdk."
            )
        return APIKeyBackend(config.claude.api_key, secrets.anthropic_api_key)
    return AgentSDKBackend(config.claude.agent_sdk, run_dir)


def _load_style_guide(config: AppConfig) -> str:
    from .scriptgen.prompts import DEFAULT_STYLE_GUIDE

    style_path = Path(config.style_path)
    if style_path.exists():
        return style_path.read_text(encoding="utf-8")
    logger.debug("No %s found — using the embedded default style guide", style_path)
    return DEFAULT_STYLE_GUIDE


def run_generate(
    *,
    video: Path,
    config: AppConfig,
    secrets: Secrets,
    sprint: int | None = None,
    task: int | None = None,
    context_file: Path | None = None,
    review: bool = False,
    output_dir: Path | None = None,
    resume: Path | None = None,
) -> tuple[Path, Path, Path]:
    """The full generate pipeline. Returns (video, script.md, srt) paths."""
    ffmpeg = make_ffmpeg(config)
    ffmpeg.check_available()
    out_dir = output_dir or Path(config.output_dir)

    if resume is not None:
        run = RunDir.load(resume)
        logger.info("Resuming run %s (completed stages: %s)", run.path,
                    ", ".join(run.manifest().stages_completed) or "none")
    else:
        run = RunDir.create(out_dir, video, config.model_dump(mode="json"))
    attach_run_log(run.log_file)

    # -- Stage 1: segmentation -------------------------------------------------
    if run.stage_complete(STAGE_SEGMENTATION):
        logger.info("Stage 1 (segmentation): already complete — skipping")
        segments_file = run.load_segments()
    else:
        segments_file = segment_video(ffmpeg, video, run.path, config.segmentation)
        run.save_segments(segments_file)
        run.mark_stage_complete(STAGE_SEGMENTATION)

    # -- Stage 2: context ---------------------------------------------------------
    if run.stage_complete(STAGE_CONTEXT):
        logger.info("Stage 2 (context): already complete — skipping")
        context = run.load_context()
    else:
        context = gather_context(
            cfg=config.azure_devops,
            sprint=sprint,
            task=task,
            context_file=context_file,
            pat=secrets.azure_devops_pat,
        )
        run.save_context(context)
        run.mark_stage_complete(STAGE_CONTEXT)

    # -- Stage 3: script ------------------------------------------------------------
    if run.stage_complete(STAGE_SCRIPT):
        logger.info("Stage 3 (script): already complete — skipping")
        script = run.load_script()
    else:
        report_vision_estimate(segments_file, run.path, context)
        generator = ScriptGenerator(
            _script_backend(config, secrets, run.path),
            style_guide=_load_style_guide(config),
            glossary=load_glossary(Path(config.glossary_path)),
            max_repair_attempts=config.claude.max_repair_attempts,
        )
        logger.info("Generating narration script with Claude (%s mode)...", config.claude.mode)
        script = generator.generate(segments_file, context, run.path)
        run.save_script(script)
        run.mark_stage_complete(STAGE_SCRIPT)
        logger.info('Script "%s": %d segments', script.title, len(script.segments))

    if review:
        script_path = run.path / "script.json"
        console.print()
        console.print(f"[bold]Script written to:[/bold] {script_path}")
        console.print("Review (and optionally edit) the narration before audio synthesis.")
        if not _confirm("Proceed with voice synthesis?"):
            console.print(
                "Stopped before synthesis. After editing the script, continue with:\n"
                f"  demo-narrator regen-audio \"{run.path}\""
            )
            raise SystemExit(0)
        script = run.load_script()  # pick up any manual edits made during the pause

    return run_synthesis_and_assembly(
        run=run, config=config, secrets=secrets, provider_name=None, out_dir=out_dir
    )


def run_synthesis_and_assembly(
    *,
    run: RunDir,
    config: AppConfig,
    secrets: Secrets,
    provider_name: str | None,
    out_dir: Path,
) -> tuple[Path, Path, Path]:
    """Stages 4-5 (used by generate and by regen-audio)."""
    ffmpeg = make_ffmpeg(config)
    script = run.load_script()
    name = provider_name or config.tts.provider
    provider = create_provider(name, config.tts, secrets, ffmpeg)

    guard_tts_characters(
        script, provider_name=provider.name, is_paid=provider.is_paid,
        cap=config.tts.max_tts_characters,
    )
    synthesize_script(script, provider, run)
    run.mark_stage_complete(STAGE_AUDIO, tts_provider=provider.name)

    basename = Path(run.manifest().source_video).stem
    outputs = assemble(ffmpeg, run, script, config.assembly, out_dir, basename)
    run.mark_stage_complete(STAGE_ASSEMBLY)
    return outputs


def run_regen_audio(
    *,
    run_dir: Path,
    config: AppConfig,
    secrets: Secrets,
    provider_name: str | None,
    output_dir: Path | None,
) -> tuple[Path, Path, Path]:
    """Re-run Stages 4-5 only (after manual script edits or to switch provider)."""
    run = RunDir.load(run_dir)
    attach_run_log(run.log_file)
    run.invalidate_stages([STAGE_AUDIO, STAGE_ASSEMBLY])
    if not run.load_script().segments:
        raise ScriptGenerationError("script.json contains no segments — nothing to synthesize.")
    return run_synthesis_and_assembly(
        run=run,
        config=config,
        secrets=secrets,
        provider_name=provider_name,
        out_dir=output_dir or Path(config.output_dir),
    )


def _confirm(question: str) -> bool:
    from rich.prompt import Confirm

    return Confirm.ask(question, console=console)
