"""Background job worker and the job handlers (generate / render / session).

One worker thread executes jobs serially — recording and TTS are resource-heavy,
and single-tenant volume doesn't need parallelism (see the plan doc). All engine
logs for a job are captured to its log file, which the UI tails.
"""

from __future__ import annotations

import logging
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from ..config import AppConfig, Secrets
from ..progress import capture_logs
from .db import Database, Job

logger = logging.getLogger(__name__)

Handler = Callable[["JobContext"], dict[str, Any]]


class JobContext:
    def __init__(self, job: Job, db: Database, config: AppConfig, secrets: Secrets) -> None:
        self.job = job
        self.db = db
        self.config = config
        self.secrets = secrets
        self.params = job.params

    def log(self, phase: str, detail: str) -> None:
        # Progress lands in the captured job log (and server console).
        logging.getLogger("demo_narrator.job").info("[%s] %s", phase, detail)

    def profile(self) -> Any:
        from ..demo.profile import load_profile, resolve_profile_path

        profiles_dir = self.config.resolve("profiles")
        return load_profile(resolve_profile_path(str(self.params["project"]), profiles_dir))


def _handle_generate(ctx: JobContext) -> dict[str, Any]:
    from ..demo.decision import create_decision_backend
    from ..demo.flow import save_flow
    from ..demo.generate import generate_flow, refine_flow, resolve_hint
    from ..demo.runner import ensure_session
    from ..trackers import tracker_from_profile

    profile = ctx.profile()
    ctx.log("generate", "validating session (auto-refreshes if expired)")
    storage_state = ensure_session(profile)
    task_id = int(ctx.params["task_id"])
    profiles_dir = ctx.config.resolve("profiles")

    ctx.log("generate", f"fetching work item {task_id}")
    tracker = tracker_from_profile(profile)
    task_text = tracker.work_item_text(task_id)

    guidance = str(ctx.params.get("guidance") or "").strip() or None
    if guidance:  # persist so later regenerations reuse it (it IS the hints file)
        hint_path = profiles_dir / profile.name / "hints" / f"task-{task_id}.md"
        hint_path.parent.mkdir(parents=True, exist_ok=True)
        hint_path.write_text(guidance, encoding="utf-8")
    else:
        guidance = resolve_hint(profile.name, task_id, None, profiles_dir)

    backend = create_decision_backend(ctx.config.claude, ctx.secrets)
    flow = generate_flow(
        profile, task_id, task_text, backend,
        storage_state=storage_state,
        max_steps=int(ctx.params.get("max_steps", 14)),
        hint=guidance,
        on_progress=ctx.log,
    )
    if bool(ctx.params.get("polish", True)):
        flow = refine_flow(flow, task_text, backend, polish=True)

    target = profiles_dir / profile.name / "flows" / f"{flow.name}.demoflow.yaml"
    save_flow(flow, target)
    ctx.log("generate", f"flow written: {target.name} ({len(flow.flat_steps())} steps)")
    return {
        "flow": flow.name,
        "file": str(target),
        "steps": len(flow.flat_steps()),
        "used_hint": bool(guidance),
    }


def _handle_render(ctx: JobContext) -> dict[str, Any]:
    from ..demo.build import build_demo
    from ..demo.flow import load_flow, merge_flows
    from ..ffmpeg import FFmpeg

    profile = ctx.profile()
    profiles_dir = ctx.config.resolve("profiles")
    flows_dir = profiles_dir / profile.name / "flows"

    names = [str(n) for n in ctx.params.get("flows", [])]
    if not names:
        raise ValueError("render job needs at least one flow")
    paths = []
    for name in names:
        p = Path(name)
        if not p.exists():
            stem = name if name.endswith(".demoflow.yaml") else f"{name}.demoflow.yaml"
            p = flows_dir / stem
        if not p.exists():
            raise FileNotFoundError(f"flow not found: {name}")
        paths.append(p)
    flows = [load_flow(p) for p in paths]

    sprint_raw = ctx.params.get("sprint")
    sprint = int(sprint_raw) if sprint_raw is not None and sprint_raw != "" else None
    if len(flows) == 1:
        flow = flows[0]
    else:
        stem = f"sprint-{sprint}-demo" if sprint else "combined-demo"
        flow = merge_flows(flows, name=stem, title=stem.replace("-", " ").title())

    art = build_demo(
        profile, flow, ctx.config, ctx.secrets,
        provider_name=ctx.params.get("provider") or None,
        captions=bool(ctx.params.get("captions", True)),
        bookends=bool(ctx.params.get("bookends", True)),
        sprint=sprint,
        on_progress=ctx.log,
    )
    ffmpeg = FFmpeg(ctx.config.ffmpeg.ffmpeg_path, ctx.config.ffmpeg.ffprobe_path)
    duration = ffmpeg.media_duration(art.video)
    render = ctx.db.add_render(
        job_id=ctx.job.id,
        project=profile.name,
        title=flow.title or flow.name,
        video_path=str(art.video),
        srt_path=str(art.srt),
        script_path=str(art.script_md),
        duration_sec=round(duration, 2),
        chapters=art.chapters,
    )
    ctx.log("render", f"done: {art.video.name} ({duration:.1f}s)")
    return {"render_id": render.id, "video": str(art.video), "duration_sec": round(duration, 2)}


def _handle_test_beat(ctx: JobContext) -> dict[str, Any]:
    """Record ONE beat of a flow, no cards, for a quick preview clip in the editor.

    The flow content comes from the request (the editor's current, possibly
    unsaved state) so users can iterate without committing every attempt.
    """
    from ..demo.build import build_demo
    from ..demo.models import FlowSpec

    profile = ctx.profile()
    flow = FlowSpec.model_validate(ctx.params["flow"])
    beat_index = int(ctx.params.get("beat_index", 0))
    if not 0 <= beat_index < len(flow.beats):
        raise ValueError(f"beat_index {beat_index} out of range (flow has {len(flow.beats)} beats)")

    preview = FlowSpec(
        version=1,
        name=f"{flow.name}-preview-b{beat_index}",
        title=flow.beats[beat_index].title or flow.title,
        project=flow.project,
        locale=flow.locale,
        viewport=flow.viewport,
        beats=[flow.beats[beat_index]],
    )
    art = build_demo(
        profile, preview, ctx.config, ctx.secrets,
        captions=bool(ctx.params.get("captions", False)),
        bookends=False,
        on_progress=ctx.log,
    )
    ctx.log("test_beat", f"preview ready: {art.video.name}")
    return {"video": str(art.video), "run_dir": str(art.run_dir)}


def _handle_session(ctx: JobContext) -> dict[str, Any]:
    from ..demo.runner import capture_session

    profile = ctx.profile()
    target = profile.auth.storage_state
    if not target:
        raise ValueError(f"profile {profile.name!r} has no auth.storage_state path")
    out = capture_session(profile, out_state=Path(target), headless=True)
    ctx.log("session", f"storage state saved: {out}")
    return {"storage_state": str(out)}


def _handle_session_interactive(ctx: JobContext) -> dict[str, Any]:
    """Manual login: a visible browser window opens ON THE SERVER MACHINE; the
    user signs in themselves (MFA works), then the session is captured."""
    from ..demo.runner import capture_session_interactive

    profile = ctx.profile()
    target = profile.auth.storage_state
    if not target:
        raise ValueError(f"profile {profile.name!r} has no auth.storage_state path")
    timeout_sec = int(ctx.params.get("timeout_sec", 300))
    ctx.log(
        "session",
        f"browser window opened on the server machine — complete the login there "
        f"(waiting up to {timeout_sec}s; MFA is fine)",
    )
    out = capture_session_interactive(profile, out_state=Path(target), timeout_sec=timeout_sec)
    ctx.log("session", f"manual login captured: {out}")
    return {"storage_state": str(out)}


def _handle_onboard(ctx: JobContext) -> dict[str, Any]:
    """One turn of the setup-assistant conversation: (optionally) inspect the app,
    ask Claude, merge the proposal into the draft, and persist the conversation."""
    from ..demo.decision import create_decision_backend
    from ..demo.onboard import empty_state, run_onboard_turn
    from ..demo.runner import session_is_valid

    profile = ctx.profile()
    state = ctx.db.get_onboarding(profile.name) or empty_state()
    user_message = str(ctx.params.get("message") or "")

    # Use the stored session only if it's still valid — inspection of a signed-in
    # app is richer, but an expired session must not block onboarding.
    storage_state = profile.auth.storage_state
    if storage_state and not session_is_valid(profile, storage_state):
        ctx.log("onboard", "stored session is invalid — inspecting as a signed-out visitor")
        storage_state = None

    backend = create_decision_backend(ctx.config.claude, ctx.secrets)
    ctx.log("onboard", "thinking (inspecting the app + consulting Claude)…")
    state = run_onboard_turn(
        profile, backend, state, user_message,
        storage_state=storage_state, on_progress=ctx.log,
    )
    ctx.db.save_onboarding(profile.name, state)
    last = state["messages"][-1] if state["messages"] else {}
    ctx.log("onboard", "reply ready")
    return {"reply": last.get("text", ""), "ready": bool(state["draft"].get("ready"))}


HANDLERS: dict[str, Handler] = {
    "generate": _handle_generate,
    "render": _handle_render,
    "session": _handle_session,
    "session_interactive": _handle_session_interactive,
    "test_beat": _handle_test_beat,
    "onboard": _handle_onboard,
}


class Worker(threading.Thread):
    """Claims queued jobs one at a time and runs them with log capture."""

    def __init__(self, db: Database, config: AppConfig, secrets: Secrets) -> None:
        super().__init__(name="job-worker", daemon=True)
        self._db = db
        self._config = config
        self._secrets = secrets
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        stale = self._db.fail_stale_running()
        if stale:
            logger.warning("marked %d stale running job(s) as failed", stale)
        logger.info("job worker started")
        while not self._stop.is_set():
            job = self._db.claim_next_job()
            if job is None:
                self._stop.wait(1.0)
                continue
            self._run_job(job)

    def _run_job(self, job: Job) -> None:
        log_path = self._config.resolve(self._config.output_dir) / "jobs" / f"{job.id}.log"
        self._db.set_job_log(job.id, str(log_path))
        logger.info("job %s (%s) started", job.id, job.kind)
        ctx = JobContext(job, self._db, self._config, self._secrets)
        try:
            handler = HANDLERS.get(job.kind)
            if handler is None:
                raise ValueError(f"unknown job kind {job.kind!r}")
            with capture_logs(log_path):
                result = handler(ctx)
            self._db.finish_job(job.id, result=result)
            logger.info("job %s succeeded", job.id)
        except Exception as exc:  # noqa: BLE001 - job boundary
            logger.error("job %s failed: %s", job.id, exc)
            try:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"\nERROR: {exc}\n{traceback.format_exc()}")
            except OSError:
                pass
            self._db.finish_job(job.id, error=str(exc))
