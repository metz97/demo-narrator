"""Demo-mode orchestrator: record each feature (beat) as its own narrated,
captioned clip, then assemble a reel — intro card, then a section card + the
feature for each beat, then an outro card — with subtitles and a script.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AppConfig, Secrets
from ..errors import CostGuardError, PlaywrightError
from ..ffmpeg import FFmpeg
from ..glossary import apply_glossary, load_glossary
from ..progress import ProgressFn, report
from ..tts.base import TTSProvider, create_provider
from . import cards
from .captions import CaptionSegment, build_ass
from .models import Beat, FlowSpec
from .profile import ProjectProfile
from .runner import run_flow

logger = logging.getLogger(__name__)

_TARGET_FPS = 25.0  # Playwright records ~25fps; cards + concat are normalised to this.


@dataclass(frozen=True)
class DemoArtifacts:
    video: Path
    srt: Path
    script_md: Path
    run_dir: Path
    # One entry per beat, in play order, with timings on the final timeline.
    chapters: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Narration:
    index: int          # step index within the beat
    display: str        # original text (subtitles)
    tts_text: str       # glossary-applied text (spoken)
    wav: Path
    duration: float = 0.0


def _srt_timestamp(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _beat_summary(beat: Beat) -> str:
    """First narrated line of a beat — what the feature is, in the demo's own words."""
    for step in beat.steps:
        if step.say and step.say.strip():
            text = " ".join(step.say.split())
            return text if len(text) <= 180 else text[:177].rstrip() + "…"
    return ""


def build_demo(
    profile: ProjectProfile,
    flow: FlowSpec,
    config: AppConfig,
    secrets: Secrets,
    *,
    provider_name: str | None = None,
    output_dir: Path | None = None,
    headless: bool = True,
    captions: bool = True,
    bookends: bool = True,
    sprint: int | None = None,
    on_progress: ProgressFn | None = None,
) -> DemoArtifacts:
    ffmpeg = FFmpeg(config.ffmpeg.ffmpeg_path, config.ffmpeg.ffprobe_path)
    provider = create_provider(provider_name or config.tts.provider, config.tts, secrets, ffmpeg)
    problems = provider.check()
    if problems:
        raise PlaywrightError(f"TTS provider {provider.name!r} is not usable:\n" + "\n".join(problems))

    storage = _resolve_storage_state(profile)
    glossary_path = config.resolve(profile.glossary_path or config.glossary_path)
    glossary = load_glossary(glossary_path)

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = output_dir or config.resolve(config.output_dir)
    run_dir = base / "demo-runs" / f"{stamp}-{flow.name}"
    run_dir.mkdir(parents=True, exist_ok=True)

    vp = flow.viewport or profile.viewport
    width, height = vp.width, vp.height
    features = [b.title or f"Feature {i + 1}" for i, b in enumerate(flow.beats)]

    _cost_guard(flow, glossary, provider, config, bookends, sprint, features)

    cards_dir = run_dir / "cards"
    cards_dir.mkdir(exist_ok=True)

    segments: list[Path] = []
    srt: list[tuple[float, str, float]] = []          # (global_start, text, dur)
    script_sections: list[tuple[str, list[tuple[float, str]]]] = []
    cumulative = 0.0

    def emit(clip: Path, local_entries: list[tuple[float, str, float]]) -> None:
        nonlocal cumulative
        for local_start, text, dur in local_entries:
            srt.append((cumulative + local_start, text, dur))
        cumulative += ffmpeg.media_duration(clip)
        segments.append(clip)

    def card(name: str, html: str, say: str) -> tuple[Path, list[tuple[float, str, float]]]:
        clip, narr = _card_clip(
            name, html, say, cards_dir, provider, ffmpeg, config, glossary, width, height, captions
        )
        return clip, [(0.0, say, narr)]

    if bookends:
        emit(*card("intro", cards.intro_card_html(sprint, features), cards.intro_narration(sprint, features)))

    chapters: list[dict[str, Any]] = []
    for i, beat in enumerate(flow.beats):
        title = features[i]
        # A chapter opens on its section card, so jumping to it lands on the
        # title rather than mid-action.
        chapter_start = cumulative
        if bookends:
            emit(*card(
                f"section{i}",
                cards.section_card_html(i, len(flow.beats), title),
                cards.section_narration(i, len(flow.beats), title),
            ))
        beat_flow = FlowSpec(
            version=1, name=f"{flow.name}-b{i}", project=flow.project,
            locale=flow.locale, viewport=flow.viewport, beats=[beat],
        )
        logger.info("Recording feature %d/%d: %s", i + 1, len(flow.beats), title)
        report(on_progress, "record", f"feature {i + 1}/{len(flow.beats)}: {title}")
        beat_clip, beat_entries = _render_beat(
            profile, beat_flow, config, provider, ffmpeg, glossary,
            run_dir / f"beat{i}", storage, captions, headless,
        )
        heading = title + (f" (task {beat.task})" if beat.task else "")
        script_sections.append((heading, [(cumulative + ls, txt) for ls, txt, _ in beat_entries]))
        emit(beat_clip, beat_entries)
        chapters.append({
            "index": i,
            "title": title,
            "task": beat.task,
            "start_sec": round(chapter_start, 2),
            "end_sec": round(cumulative, 2),
            "summary": _beat_summary(beat),
        })

    if bookends:
        emit(*card("outro", cards.outro_card_html(sprint, features), cards.outro_narration(sprint, features)))

    report(on_progress, "assemble", f"{len(segments)} segments")
    final = run_dir / f"{flow.name}-final.mp4"
    if len(segments) == 1:
        shutil.copyfile(segments[0], final)
    else:
        a = config.assembly
        ffmpeg.concat_reencode(
            segments, final, fps=_TARGET_FPS, crf=a.video_crf, preset=a.video_preset,
            sample_rate=a.audio_sample_rate, audio_bitrate=a.audio_bitrate,
        )

    srt_path = _write_srt(run_dir / f"{flow.name}-narration.srt", srt)
    script_path = _write_script(run_dir / f"{flow.name}-script.md", flow.title or flow.name, script_sections)
    (run_dir / "chapters.json").write_text(
        json.dumps(chapters, indent=2), encoding="utf-8"
    )
    logger.info("Assembled %d segments into %s", len(segments), final.name)
    return DemoArtifacts(
        video=final, srt=srt_path, script_md=script_path, run_dir=run_dir, chapters=chapters
    )


def _cost_guard(
    flow: FlowSpec, glossary: dict[str, str], provider: TTSProvider, config: AppConfig,
    bookends: bool, sprint: int | None, features: list[str],
) -> None:
    says = [st.say for _, st in flow.flat_steps() if st.say]
    if bookends:
        says.append(cards.intro_narration(sprint, features))
        says.append(cards.outro_narration(sprint, features))
        says += [cards.section_narration(i, len(features), t) for i, t in enumerate(features)]
    total = sum(len(apply_glossary(s, glossary)) for s in says)
    logger.info("Demo: %d narrated segments, %s characters, provider %r.", len(says), f"{total:,}", provider.name)
    cap = config.tts.max_tts_characters
    if provider.is_paid and cap > 0 and total > cap:
        raise CostGuardError(
            f"TTS character guard: {total:,} characters with the PAID provider {provider.name!r}, "
            f"above the cap of {cap:,} (tts.max_tts_characters). "
            "Raise the cap, shorten narration, or use --provider kokoro."
        )


def _render_beat(
    profile: ProjectProfile, beat_flow: FlowSpec, config: AppConfig, provider: TTSProvider,
    ffmpeg: FFmpeg, glossary: dict[str, str], out_dir: Path, storage: str | None,
    captions: bool, headless: bool,
) -> tuple[Path, list[tuple[float, str, float]]]:
    """Record one beat into its own narrated (+captioned) clip."""
    a = config.assembly
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    narrations = [
        _Narration(gi, st.say, apply_glossary(st.say, glossary), audio_dir / f"n{gi:02d}.wav")
        for gi, (_task, st) in enumerate(beat_flow.flat_steps()) if st.say
    ]
    for n in narrations:
        provider.synthesize(n.tts_text, n.wav)
        if not n.wav.exists() or n.wav.stat().st_size == 0:
            raise PlaywrightError(f"Provider {provider.name!r} produced no audio for step {n.index}.")
        n.duration = ffmpeg.media_duration(n.wav)

    result = run_flow(
        profile, beat_flow, out_dir=out_dir, storage_state=storage,
        durations={n.index: n.duration for n in narrations}, headless=headless,
        default_timeout_ms=8000,
    )
    info = ffmpeg.video_info(result.video_path)
    starts = {tm.index: tm.start_sec for tm in result.timings}

    narrated = out_dir / "narrated.mp4"
    ffmpeg.mux_timed_narration(
        result.video_path, [(n.wav, starts[n.index]) for n in narrations if n.index in starts],
        narrated, crf=a.video_crf, preset=a.video_preset, fps=info.fps,
        sample_rate=a.audio_sample_rate, audio_bitrate=a.audio_bitrate, duration=info.duration_sec,
    )

    body = narrated
    if captions:
        segs = [
            CaptionSegment(n.display, starts[n.index], starts[n.index] + n.duration)
            for n in narrations if n.index in starts
        ]
        ass = out_dir / "captions.ass"
        ass.write_text(build_ass(segs, width=info.width, height=info.height), encoding="utf-8")
        body = out_dir / "captioned.mp4"
        ffmpeg.burn_subtitles(narrated, ass, body, crf=a.video_crf, preset=a.video_preset)

    entries = sorted(
        (starts[n.index], n.display, n.duration) for n in narrations if n.index in starts
    )
    return body, entries


def _card_clip(
    name: str, html: str, say: str, cards_dir: Path, provider: TTSProvider, ffmpeg: FFmpeg,
    config: AppConfig, glossary: dict[str, str], width: int, height: int, captions: bool,
) -> tuple[Path, float]:
    a = config.assembly
    png = cards.render_card(html, cards_dir / f"{name}.png", width=width, height=height)
    wav = cards_dir / f"{name}.wav"
    provider.synthesize(apply_glossary(say, glossary), wav)
    narr = ffmpeg.media_duration(wav)
    clip = cards_dir / f"{name}.mp4"
    ffmpeg.narrated_still_clip(
        png, wav, clip, duration=narr + 1.0, fps=_TARGET_FPS, crf=a.video_crf,
        preset=a.video_preset, sample_rate=a.audio_sample_rate, audio_bitrate=a.audio_bitrate,
    )
    if captions:
        ass = cards_dir / f"{name}.ass"
        ass.write_text(build_ass([CaptionSegment(say, 0.0, narr)], width=width, height=height), encoding="utf-8")
        capped = cards_dir / f"{name}-cap.mp4"
        ffmpeg.burn_subtitles(clip, ass, capped, crf=a.video_crf, preset=a.video_preset)
        return capped, narr
    return clip, narr


def _resolve_storage_state(profile: ProjectProfile) -> str | None:
    """Preflight: return a VALIDATED session, auto-refreshing via the profile's
    login recipe when expired — jobs must never start on a sign-in page."""
    from .runner import ensure_session  # noqa: PLC0415

    return ensure_session(profile)


def _write_srt(path: Path, entries: list[tuple[float, str, float]]) -> Path:
    blocks = []
    for i, (start, text, dur) in enumerate(sorted(entries), start=1):
        end = start + dur
        blocks.append(f"{i}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _write_script(path: Path, title: str, sections: list[tuple[str, list[tuple[float, str]]]]) -> Path:
    lines = [f"# {title}", ""]
    for heading, entries in sections:
        lines += [f"## {heading}", ""]
        for start, text in entries:
            ts = _srt_timestamp(start).replace(",", ".")[:-4]
            lines.append(f"- **[{ts}]** {text}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
