"""FastAPI application: the REST surface the web UI (and anything else) talks to.

The worker runs as a thread started by `demo-narrator serve`; the app itself is
worker-agnostic (tests exercise routes with no worker attached).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import AppConfig, Secrets
from ..errors import DemoNarratorError
from .db import Database, Share, hash_viewer_ip, verify_share_password

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_FLOW_SUFFIX = ".demoflow.yaml"


class GenerateTask(BaseModel):
    id: int
    guidance: str | None = None


class GenerateRequest(BaseModel):
    tasks: list[GenerateTask] = Field(min_length=1)
    max_steps: int = 14
    polish: bool = True


class RenderRequest(BaseModel):
    project: str
    flows: list[str] = Field(min_length=1)
    sprint: int | None = None
    captions: bool = True
    bookends: bool = True
    provider: str | None = None


class SaveFlowRequest(BaseModel):
    flow: dict[str, Any]          # FlowSpec JSON (validated server-side)
    note: str | None = None       # version label shown in history


class TestBeatRequest(BaseModel):
    flow: dict[str, Any]          # current editor state (may be unsaved)
    beat_index: int = 0
    captions: bool = False


class HintRequest(BaseModel):
    text: str


class ProfileSaveRequest(BaseModel):
    yaml: str


class CreateProjectRequest(BaseModel):
    name: str
    base_url: str
    context: str | None = None


class OnboardMessageRequest(BaseModel):
    text: str = ""


class CreateShareRequest(BaseModel):
    expires_in_days: int | None = None      # None = never expires
    password: str | None = None             # None = no gate
    allow_download: bool = True


class UnlockShareRequest(BaseModel):
    password: str = ""


class ShareViewRequest(BaseModel):
    seconds_watched: float = 0.0
    update_last: bool = False        # report progress without counting a second view


class SayPreviewRequest(BaseModel):
    text: str
    provider: str | None = None       # None = the configured default


class RegenerateSayRequest(BaseModel):
    flow: dict[str, Any]              # current editor state (may be unsaved)
    beat_index: int = 0
    step_index: int = 0
    task_id: int | None = None        # pulls tracker context when task_text is absent
    task_text: str | None = None


_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


def _scaffold_profile_yaml(name: str, base_url: str, context: str | None) -> str:
    """A valid, minimal profile ready to refine (in the editor or the assistant).

    Defaults mirror profiles/lca-tool.yaml; auth/tracker are left as commented
    stubs so the profile validates immediately and the user fills in the rest."""
    import json

    ctx = context.strip() if context and context.strip() else ""
    ctx_block = ""
    if ctx:
        # block scalar keeps the multi-line context readable in the file
        indented = "\n".join("  " + line for line in ctx.splitlines())
        ctx_block = f"context: |\n{indented}\n\n"
    # name is regex-validated to a safe plain scalar; a JSON string is a valid
    # YAML double-quoted scalar, so it safely quotes the URL.
    return (
        f"# Profile for {name}. Edit here or refine it with the setup assistant.\n"
        f"name: {name}\n"
        f"base_url: {json.dumps(base_url)}\n"
        f"default_locale: en\n\n"
        f"{ctx_block}"
        f"auth:\n"
        f"  strategy: storage_state\n"
        f"  storage_state: \".auth/{name}.json\"   # captured session (Log in manually / Refresh session)\n"
        f"  sign_in_path: \"/sign-in\"              # URL fragment that means 'not signed in'\n"
        f"  # login_recipe:                        # optional: enables headless Refresh session\n"
        f"  #   credentials_env: {{ username: E2E_CORE_USERNAME, password: E2E_CORE_PASSWORD }}\n"
        f"  #   steps:\n"
        f"  #     - {{ action: goto, path: \"/sign-in\" }}\n\n"
        f"selectors:\n"
        f"  test_id_attribute: data-testid\n\n"
        f"# issue_tracker:                         # optional: sprint/task listing\n"
        f"#   provider: azure_devops\n"
        f"#   organization: \"https://dev.azure.com/yourorg\"\n"
        f"#   project: YourProject\n"
        f"#   team: YourTeam\n\n"
        f"feature_map: {{}}                         # route hints, e.g. search: /search\n"
    )


def create_app(config: AppConfig, secrets: Secrets, db: Database) -> FastAPI:
    app = FastAPI(title="demo-narrator", docs_url="/api/docs", openapi_url="/api/openapi.json")
    profiles_dir = config.resolve("profiles")

    def _profile(name: str) -> Any:
        from ..demo.profile import load_profile, resolve_profile_path

        try:
            return load_profile(resolve_profile_path(name, profiles_dir))
        except DemoNarratorError as exc:
            raise HTTPException(404, str(exc)) from exc

    def _tracker(profile: Any) -> Any:
        from ..trackers import tracker_from_profile

        try:
            return tracker_from_profile(profile)
        except DemoNarratorError as exc:
            raise HTTPException(502, str(exc)) from exc

    # -- projects -------------------------------------------------------------

    @app.get("/api/meta")
    def server_meta() -> dict[str, Any]:
        """What the header chips report: which engines this server is wired to."""
        return {
            "claude_mode": config.claude.mode,
            "tts_provider": config.tts.provider,
            "captions_default": True,
        }

    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if profiles_dir.exists():
            for path in sorted(profiles_dir.glob("*.yaml")):
                name = path.stem
                try:
                    prof = _profile(name)
                except HTTPException:
                    out.append({"name": name, "error": "profile failed to load"})
                    continue
                storage = prof.auth.storage_state
                flows = profiles_dir / name / "flows"
                tracker = prof.issue_tracker
                tracker_label = None
                if tracker is not None:
                    extra = tracker.model_dump(exclude={"provider"})
                    org = str(extra.get("organization", "")).rstrip("/").rsplit("/", 1)[-1]
                    parts = [p for p in (org, extra.get("project")) if p]
                    tracker_label = " · ".join(str(p) for p in parts) or tracker.provider
                out.append(
                    {
                        "name": prof.name,
                        "base_url": prof.base_url,
                        "route_prefix": prof.route_prefix,
                        "locale": prof.default_locale,
                        "tracker": tracker is not None,
                        "tracker_label": tracker_label,
                        "session_ok": bool(storage and Path(storage).exists()),
                        "storage_state": Path(storage).name if storage else None,
                        "flows": len(list(flows.glob("*.demoflow.yaml"))) if flows.exists() else 0,
                    }
                )
        return out

    @app.post("/api/projects")
    def create_project(req: CreateProjectRequest) -> dict[str, Any]:
        name = req.name.strip().lower()
        if not _NAME_RE.match(name):
            raise HTTPException(
                422,
                "Project name must be 2–49 chars: lowercase letters, digits and hyphens, "
                "starting with a letter or digit.",
            )
        base_url = req.base_url.strip()
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(422, "base_url must start with http:// or https://")
        profiles_dir.mkdir(parents=True, exist_ok=True)
        path = profiles_dir / f"{name}.yaml"
        if path.exists():
            raise HTTPException(409, f"A project named {name!r} already exists.")
        text = _scaffold_profile_yaml(name, base_url, req.context)
        # Validate the scaffold before writing so we never leave a broken profile.
        from ..demo.profile import ProjectProfile, expand_env
        import yaml as _yaml

        try:
            ProjectProfile.model_validate(expand_env(_yaml.safe_load(text)))
        except Exception as exc:  # noqa: BLE001 - should never happen; surfaces a bug in the scaffold
            raise HTTPException(500, f"generated profile failed validation: {exc}") from exc
        path.write_text(text, encoding="utf-8")
        return {"name": name, "created": path.name}

    @app.get("/api/projects/{project}/sprints")
    def list_sprints(project: str) -> list[dict[str, Any]]:
        tracker = _tracker(_profile(project))
        try:
            sprints = tracker.list_sprints()
        except DemoNarratorError as exc:
            raise HTTPException(502, str(exc)) from exc
        return [{"name": s.name, "path": s.path, "timeframe": s.timeframe} for s in sprints]

    @app.get("/api/projects/{project}/sprints/{sprint}/work-items")
    def list_work_items(project: str, sprint: str) -> list[dict[str, Any]]:
        tracker = _tracker(_profile(project))
        try:
            items = tracker.list_work_items(sprint)
        except DemoNarratorError as exc:
            raise HTTPException(502, str(exc)) from exc
        return [
            {"id": i.id, "title": i.title, "state": i.state, "type": i.work_item_type}
            for i in items
        ]

    @app.get("/api/projects/{project}/flows")
    def list_flows(project: str) -> list[dict[str, Any]]:
        from ..demo.flow import load_flow

        prof = _profile(project)
        flows_dir = profiles_dir / prof.name / "flows"
        out: list[dict[str, Any]] = []
        for path in sorted(flows_dir.glob("*.demoflow.yaml")) if flows_dir.exists() else []:
            entry: dict[str, Any] = {"file": path.name, "name": path.name.replace(_FLOW_SUFFIX, "")}
            try:
                flow = load_flow(path)
                steps = [st for _task, st in flow.flat_steps()]
                narrated = [s for s in steps if s.say]
                entry.update(
                    title=flow.title,
                    steps=len(steps),
                    beats=len(flow.beats),
                    narrated=len(narrated),
                    words=sum(len((s.say or "").split()) for s in narrated),
                    tasks=[b.task for b in flow.beats if b.task],
                    version=flow.version,
                    viewport=(
                        f"{flow.viewport.width}x{flow.viewport.height}" if flow.viewport else None
                    ),
                    # Enough of the beat to recognise the flow without opening the editor.
                    step_preview=[
                        {"action": s.action, "summary": _step_summary(s)} for s in steps[:8]
                    ],
                )
            except DemoNarratorError as exc:
                entry["error"] = str(exc)[:200] or "invalid flow spec"
            out.append(entry)
        return out

    @app.get("/api/projects/{project}/health")
    def project_health(project: str) -> dict[str, Any]:
        """Light preflight: is the app reachable, does a session exist, how old is it."""
        import httpx

        prof = _profile(project)
        out: dict[str, Any] = {"base_url": prof.base_url}
        try:
            resp = httpx.get(prof.base_url, timeout=5.0, follow_redirects=True)
            out["app_reachable"] = True
            out["app_status"] = resp.status_code
            body = resp.text[:4000].lower()
            out["build_error"] = "failed to resolve import" in body or "plugin:vite" in body
        except httpx.HTTPError as exc:
            out["app_reachable"] = False
            out["app_error"] = str(exc)[:200]
        storage = prof.auth.storage_state
        if storage and Path(storage).exists():
            import time as _time

            out["session"] = True
            out["session_age_hours"] = round(
                (_time.time() - Path(storage).stat().st_mtime) / 3600, 1
            )
        else:
            out["session"] = False
        return out

    @app.get("/api/projects/{project}/profile")
    def get_profile_yaml(project: str) -> dict[str, Any]:
        prof = _profile(project)
        path = profiles_dir / f"{prof.name}.yaml"
        return {"name": prof.name, "yaml": path.read_text(encoding="utf-8")}

    @app.put("/api/projects/{project}/profile")
    def put_profile_yaml(project: str, req: ProfileSaveRequest) -> dict[str, Any]:
        import yaml as _yaml

        from ..demo.profile import ProjectProfile, expand_env

        prof = _profile(project)
        try:
            raw = _yaml.safe_load(req.yaml)
            if not isinstance(raw, dict):
                raise ValueError("profile must be a YAML mapping")
            ProjectProfile.model_validate(expand_env(raw))
        except Exception as exc:  # noqa: BLE001 - validation detail goes to the editor
            raise HTTPException(422, str(exc)) from exc
        # The submitted text is written verbatim so comments/formatting survive.
        (profiles_dir / f"{prof.name}.yaml").write_text(req.yaml, encoding="utf-8")
        return {"saved": f"{prof.name}.yaml"}

    # -- flow editor -------------------------------------------------------------

    def _flow_path(project_name: str, flow_name: str) -> Path:
        stem = flow_name if flow_name.endswith(_FLOW_SUFFIX) else f"{flow_name}{_FLOW_SUFFIX}"
        path = profiles_dir / project_name / "flows" / stem
        if not path.exists():
            raise HTTPException(404, f"flow not found: {flow_name}")
        return path

    @app.get("/api/projects/{project}/flows/{flow_name}")
    def get_flow(project: str, flow_name: str) -> dict[str, Any]:
        from ..demo.flow import load_flow

        prof = _profile(project)
        path = _flow_path(prof.name, flow_name)
        try:
            flow = load_flow(path)
        except DemoNarratorError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "name": flow_name.replace(_FLOW_SUFFIX, ""),
            "file": path.name,
            "flow": flow.model_dump(exclude_none=True),
            "yaml": path.read_text(encoding="utf-8"),
        }

    @app.put("/api/projects/{project}/flows/{flow_name}")
    def put_flow(project: str, flow_name: str, req: SaveFlowRequest) -> dict[str, Any]:
        from ..demo.flow import save_flow
        from ..demo.models import FlowSpec

        prof = _profile(project)
        path = _flow_path(prof.name, flow_name)
        try:
            flow = FlowSpec.model_validate(req.flow)
        except Exception as exc:  # noqa: BLE001 - pydantic detail goes to the editor
            raise HTTPException(422, str(exc)) from exc
        save_flow(flow, path)
        seq = db.add_flow_version(
            prof.name, flow_name, path.read_text(encoding="utf-8"), req.note
        )
        return {"saved": path.name, "version": seq, "steps": len(flow.flat_steps())}

    @app.get("/api/projects/{project}/flows/{flow_name}/versions")
    def flow_versions(project: str, flow_name: str) -> list[dict[str, Any]]:
        prof = _profile(project)
        return db.list_flow_versions(prof.name, flow_name)

    @app.get("/api/projects/{project}/flows/{flow_name}/versions/{seq}")
    def flow_version(project: str, flow_name: str, seq: int) -> dict[str, Any]:
        from ..demo.models import FlowSpec
        import yaml as _yaml

        prof = _profile(project)
        text = db.get_flow_version(prof.name, flow_name, seq)
        if text is None:
            raise HTTPException(404, "version not found")
        flow = FlowSpec.model_validate(_yaml.safe_load(text))
        return {"seq": seq, "yaml": text, "flow": flow.model_dump(exclude_none=True)}

    @app.post("/api/projects/{project}/flows/{flow_name}/test-beat")
    def test_beat(project: str, flow_name: str, req: TestBeatRequest) -> dict[str, Any]:
        from ..demo.models import FlowSpec

        prof = _profile(project)
        try:
            FlowSpec.model_validate(req.flow)  # fail fast before queuing
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, str(exc)) from exc
        job = db.create_job(
            "test_beat",
            {
                "project": prof.name,
                "flow": req.flow,
                "beat_index": req.beat_index,
                "captions": req.captions,
                "flow_name": flow_name,
            },
        )
        return {"job": job.id}

    @app.get("/api/jobs/{job_id}/video")
    def job_video(job_id: str) -> FileResponse:
        job = db.get_job(job_id)
        if job is None or not job.result:
            raise HTTPException(404, "no video for this job")
        video = job.result.get("video")
        if not video or not Path(video).exists():
            raise HTTPException(404, "video missing on disk")
        return FileResponse(video)

    # -- hints ---------------------------------------------------------------------

    def _hint_path(project_name: str, task_id: int) -> Path:
        return profiles_dir / project_name / "hints" / f"task-{task_id}.md"

    @app.get("/api/projects/{project}/hints/{task_id}")
    def get_hint(project: str, task_id: int) -> dict[str, Any]:
        prof = _profile(project)
        path = _hint_path(prof.name, task_id)
        return {"task_id": task_id, "text": path.read_text(encoding="utf-8") if path.exists() else ""}

    @app.put("/api/projects/{project}/hints/{task_id}")
    def put_hint(project: str, task_id: int, req: HintRequest) -> dict[str, Any]:
        prof = _profile(project)
        path = _hint_path(prof.name, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(req.text, encoding="utf-8")
        return {"saved": str(path.name)}

    @app.post("/api/projects/{project}/generate")
    def start_generate(project: str, req: GenerateRequest) -> dict[str, Any]:
        prof = _profile(project)  # validates the project exists
        jobs = [
            db.create_job(
                "generate",
                {
                    "project": prof.name,
                    "task_id": t.id,
                    "guidance": t.guidance,
                    "max_steps": req.max_steps,
                    "polish": req.polish,
                },
            )
            for t in req.tasks
        ]
        return {"jobs": [j.id for j in jobs]}

    @app.post("/api/projects/{project}/session/refresh")
    def refresh_session(project: str) -> dict[str, Any]:
        prof = _profile(project)
        job = db.create_job("session", {"project": prof.name})
        return {"job": job.id}

    @app.post("/api/projects/{project}/session/interactive")
    def interactive_session(project: str, timeout_sec: int = 300) -> dict[str, Any]:
        """Manual login: opens a VISIBLE browser on the server machine; the user
        completes the app's real login there (MFA supported), and only the
        resulting session is stored — credentials never touch demo-narrator."""
        prof = _profile(project)
        job = db.create_job(
            "session_interactive", {"project": prof.name, "timeout_sec": timeout_sec}
        )
        return {"job": job.id}

    # -- setup assistant (onboarding) ------------------------------------------

    @app.get("/api/projects/{project}/onboard")
    def get_onboard(project: str) -> dict[str, Any]:
        from ..demo.onboard import empty_state

        prof = _profile(project)
        state = db.get_onboarding(prof.name) or empty_state()
        return {"project": prof.name, "base_url": prof.base_url, **state}

    @app.post("/api/projects/{project}/onboard/message")
    def onboard_message(project: str, req: OnboardMessageRequest) -> dict[str, Any]:
        prof = _profile(project)
        job = db.create_job("onboard", {"project": prof.name, "message": req.text})
        return {"job": job.id}

    @app.post("/api/projects/{project}/onboard/apply")
    def onboard_apply(project: str) -> dict[str, Any]:
        from ..demo.onboard import empty_state, merged_profile_yaml

        prof = _profile(project)
        state = db.get_onboarding(prof.name) or empty_state()
        path = profiles_dir / f"{prof.name}.yaml"
        try:
            new_yaml = merged_profile_yaml(path.read_text(encoding="utf-8"), state["draft"])
        except Exception as exc:  # noqa: BLE001 - surfaces to the UI
            raise HTTPException(422, f"could not apply draft: {exc}") from exc
        path.write_text(new_yaml, encoding="utf-8")
        return {"saved": path.name}

    @app.post("/api/projects/{project}/onboard/reset")
    def onboard_reset(project: str) -> dict[str, Any]:
        prof = _profile(project)
        db.delete_onboarding(prof.name)
        return {"reset": prof.name}

    # -- renders ---------------------------------------------------------------

    @app.post("/api/renders")
    def start_render(req: RenderRequest) -> dict[str, Any]:
        prof = _profile(req.project)
        job = db.create_job(
            "render",
            {
                "project": prof.name,
                "flows": req.flows,
                "sprint": req.sprint,
                "captions": req.captions,
                "bookends": req.bookends,
                "provider": req.provider,
            },
        )
        return {"job": job.id}

    @app.get("/api/renders")
    def list_renders() -> list[dict[str, Any]]:
        shares = db.share_summaries()
        return [
            {
                "id": r.id,
                "project": r.project,
                "title": r.title,
                "duration_sec": r.duration_sec,
                "created_at": r.created_at,
                "video_exists": Path(r.video_path).exists(),
                "chapter_count": len(r.chapters),
                "share": shares.get(r.id),
            }
            for r in db.list_renders()
        ]

    @app.get("/api/renders/{render_id}")
    def get_render(render_id: str) -> dict[str, Any]:
        r = db.get_render(render_id)
        if r is None:
            raise HTTPException(404, "render not found")
        video = Path(r.video_path)
        return {
            "id": r.id,
            "project": r.project,
            "title": r.title,
            "duration_sec": r.duration_sec,
            "created_at": r.created_at,
            "video_exists": video.exists(),
            "size_mb": round(video.stat().st_size / 1_048_576, 1) if video.exists() else None,
            "has_srt": bool(r.srt_path and Path(r.srt_path).exists()),
            "has_script": bool(r.script_path and Path(r.script_path).exists()),
            "chapters": r.chapters,
            "shares": [
                {
                    "token": sh.token,
                    "state": sh.state,
                    "created_at": sh.created_at,
                    "expires_at": sh.expires_at,
                    "has_password": sh.has_password,
                    "allow_download": sh.allow_download,
                    **db.share_stats(sh.token),
                }
                for sh in db.list_shares(r.id)
            ],
        }

    @app.delete("/api/renders/{render_id}")
    def delete_render(render_id: str) -> dict[str, Any]:
        # Links die with the render: a live token must never outlive its video.
        revoked = db.delete_shares_for_render(render_id)
        if not db.delete_render(render_id):
            raise HTTPException(404, "render not found")
        return {"deleted": render_id, "shares_removed": revoked}

    def _render_file(render_id: str, attr: str) -> FileResponse:
        render = db.get_render(render_id)
        if render is None:
            raise HTTPException(404, "render not found")
        path_value = getattr(render, attr)
        if not path_value or not Path(path_value).exists():
            raise HTTPException(404, f"{attr} missing on disk")
        return FileResponse(path_value)

    @app.get("/api/renders/{render_id}/video")
    def render_video(render_id: str) -> FileResponse:
        return _render_file(render_id, "video_path")

    @app.get("/api/renders/{render_id}/srt")
    def render_srt(render_id: str) -> FileResponse:
        return _render_file(render_id, "srt_path")

    @app.get("/api/renders/{render_id}/script")
    def render_script(render_id: str) -> FileResponse:
        return _render_file(render_id, "script_path")

    # -- narration (step 3) ----------------------------------------------------------

    # The editor's word budget: assembly gives a segment floor(duration * 2.3) words,
    # so the same rate turns a `say:` back into the seconds it will occupy.
    _WORDS_PER_SEC = 2.3

    def _say_seconds(step: Any) -> float:
        """How long one step will run once its narration is spoken."""
        words = len((step.say or "").split())
        spoken = words / _WORDS_PER_SEC if words else 0.0
        base = spoken if step.pacing == "audio" else 0.0
        return float(max(base, float(step.min_duration_sec)) + (float(step.settle_ms) / 1000.0))

    @app.post("/api/projects/{project}/say/preview")
    def preview_say(project: str, req: SayPreviewRequest) -> FileResponse:
        """Synthesize one narration line so it can be auditioned before a render."""
        from ..ffmpeg import FFmpeg
        from ..tts.base import create_provider

        _profile(project)
        text = req.text.strip()
        if not text:
            raise HTTPException(422, "nothing to synthesize")
        if len(text) > 800:
            raise HTTPException(422, "narration line is too long to preview")
        provider_name = req.provider or config.tts.provider
        ffmpeg = FFmpeg(config.ffmpeg.ffmpeg_path, config.ffmpeg.ffprobe_path)
        try:
            provider = create_provider(provider_name, config.tts, secrets, ffmpeg)
        except DemoNarratorError as exc:
            raise HTTPException(400, str(exc)) from exc
        problems = provider.check()
        if problems:
            raise HTTPException(503, f"{provider_name} is not usable: " + "; ".join(problems))

        cache_dir = config.resolve(config.output_dir) / "previews"
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(f"{provider_name}\x00{text}".encode()).hexdigest()[:20]
        out = cache_dir / f"{digest}.wav"
        if not out.exists():
            try:
                provider.synthesize(text, out)
            except DemoNarratorError as exc:
                raise HTTPException(502, str(exc)) from exc
        return FileResponse(out, media_type="audio/wav")

    @app.post("/api/projects/{project}/flows/{flow_name}/regenerate-say")
    def regenerate_say_line(
        project: str, flow_name: str, req: RegenerateSayRequest
    ) -> dict[str, Any]:
        """Rewrite one step's narration. Synchronous: it is a single short model call."""
        from ..demo.decision import create_decision_backend
        from ..demo.generate import regenerate_say
        from ..demo.models import FlowSpec

        prof = _profile(project)
        try:
            flow = FlowSpec.model_validate(req.flow)
        except Exception as exc:  # noqa: BLE001 - validation detail goes to the editor
            raise HTTPException(422, str(exc)) from exc

        task_text = req.task_text or ""
        if not task_text and req.task_id:
            from ..demo.generate import fetch_task

            try:
                task_text = fetch_task(prof, req.task_id)
            except DemoNarratorError as exc:
                logger.warning("regenerate-say: task %s unavailable: %s", req.task_id, exc)

        backend = create_decision_backend(config.claude, secrets)
        try:
            say = regenerate_say(
                flow, req.beat_index, req.step_index, task_text, backend
            )
        except DemoNarratorError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"say": say, "words": len(say.split())}

    @app.get("/api/projects/{project}/estimate")
    def estimate_render(
        project: str, flows: list[str] = Query(default=[]), bookends: bool = True
    ) -> dict[str, Any]:
        """Predict the finished runtime from the flows selected for a render."""
        from ..demo.flow import load_flow

        prof = _profile(project)
        if not flows:
            raise HTTPException(422, "no flows selected")
        per_flow: list[dict[str, Any]] = []
        for name in flows:
            path = _flow_path(prof.name, name)
            try:
                flow = load_flow(path)
            except DemoNarratorError as exc:
                raise HTTPException(422, str(exc)) from exc
            steps = [s for beat in flow.beats for s in beat.steps]
            narrated = [s for s in steps if s.say]
            per_flow.append(
                {
                    "name": name.replace(_FLOW_SUFFIX, ""),
                    "title": flow.title or flow.name,
                    "beats": len(flow.beats),
                    "steps": len(steps),
                    "narrated_steps": len(narrated),
                    "words": sum(len((s.say or "").split()) for s in steps),
                    "seconds": round(sum(_say_seconds(s) for s in steps), 1),
                }
            )
        # Bookends are a generated title and outro card, ~4s each.
        bookend_sec = 8.0 if bookends else 0.0
        total = sum(f["seconds"] for f in per_flow) + bookend_sec
        return {
            "flows": per_flow,
            "bookend_sec": bookend_sec,
            "steps": sum(f["steps"] for f in per_flow),
            "words": sum(f["words"] for f in per_flow),
            "narrated_steps": sum(f["narrated_steps"] for f in per_flow),
            "seconds": round(total, 1),
            "provider": config.tts.provider,
            "provider_is_paid": config.tts.provider == "elevenlabs",
        }

    # -- share links ---------------------------------------------------------------
    #
    # A token is a bearer capability: anyone holding the URL can watch. The public
    # routes below are deliberately unauthenticated, so every one of them re-checks
    # revocation, expiry and the password gate rather than trusting the caller.

    def _share_json(share: Share, *, base: str) -> dict[str, Any]:
        stats = db.share_stats(share.token)
        return {
            "token": share.token,
            "render_id": share.render_id,
            "url": f"{base}/s/{share.token}",
            "state": share.state,
            "created_at": share.created_at,
            "expires_at": share.expires_at,
            "revoked_at": share.revoked_at,
            "has_password": share.has_password,
            "allow_download": share.allow_download,
            **stats,
        }

    def _base_url(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    @app.get("/api/renders/{render_id}/shares")
    def list_render_shares(render_id: str, request: Request) -> list[dict[str, Any]]:
        if db.get_render(render_id) is None:
            raise HTTPException(404, "render not found")
        base = _base_url(request)
        return [_share_json(sh, base=base) for sh in db.list_shares(render_id)]

    @app.post("/api/renders/{render_id}/share")
    def create_render_share(
        render_id: str, req: CreateShareRequest, request: Request
    ) -> dict[str, Any]:
        render = db.get_render(render_id)
        if render is None:
            raise HTTPException(404, "render not found")
        if not Path(render.video_path).exists():
            raise HTTPException(409, "render video is missing on disk; nothing to share")
        expires_at = None
        if req.expires_in_days is not None:
            if req.expires_in_days < 1:
                raise HTTPException(422, "expires_in_days must be at least 1")
            expiry = datetime.now(timezone.utc) + timedelta(days=req.expires_in_days)
            expires_at = expiry.isoformat(timespec="seconds")
        password = (req.password or "").strip() or None
        share = db.create_share(
            render_id=render_id,
            expires_at=expires_at,
            password=password,
            allow_download=req.allow_download,
        )
        logger.info("share issued for render %s (expires=%s)", render_id, expires_at or "never")
        return _share_json(share, base=_base_url(request))

    @app.delete("/api/shares/{token}")
    def revoke_share(token: str) -> dict[str, Any]:
        if db.get_share(token) is None:
            raise HTTPException(404, "share not found")
        db.revoke_share(token)
        return {"revoked": token}

    # -- public share surface (no auth) ----------------------------------------------

    def _share_cookie_name(token: str) -> str:
        return f"dnshare_{token}"

    def _share_cookie_value(share: Share) -> str:
        """Proof-of-unlock derived from the stored hash: survives restarts, leaks nothing."""
        return hmac.new(
            (share.password_hash or "").encode(), share.token.encode(), "sha256"
        ).hexdigest()

    def _live_share(token: str) -> Share:
        share = db.get_share(token)
        if share is None:
            raise HTTPException(404, "link not found")
        if share.state == "revoked":
            raise HTTPException(410, "this link has been revoked")
        if share.state == "expired":
            raise HTTPException(410, "this link has expired")
        return share

    def _unlocked(share: Share, request: Request) -> bool:
        if not share.has_password:
            return True
        supplied = request.cookies.get(_share_cookie_name(share.token), "")
        return hmac.compare_digest(supplied, _share_cookie_value(share))

    def _gate(share: Share, request: Request) -> None:
        if not _unlocked(share, request):
            raise HTTPException(401, "this link is password protected")

    @app.get("/s/{token}/meta")
    def share_meta(token: str, request: Request) -> dict[str, Any]:
        share = _live_share(token)
        render = db.get_render(share.render_id)
        if render is None:
            raise HTTPException(404, "the shared video is no longer available")
        unlocked = _unlocked(share, request)
        meta: dict[str, Any] = {
            "token": share.token,
            "needs_password": share.has_password,
            "unlocked": unlocked,
            "allow_download": share.allow_download,
            "expires_at": share.expires_at,
        }
        if unlocked:
            meta.update(
                {
                    "title": render.title,
                    "project": render.project,
                    "duration_sec": render.duration_sec,
                    "created_at": render.created_at,
                    "has_srt": bool(render.srt_path and Path(render.srt_path).exists()),
                    "chapters": render.chapters,
                }
            )
        return meta

    @app.post("/s/{token}/unlock")
    def unlock_share(token: str, req: UnlockShareRequest) -> Response:
        share = _live_share(token)
        if not share.has_password:
            return JSONResponse({"unlocked": True})
        if not verify_share_password(req.password, share.password_hash or ""):
            raise HTTPException(401, "wrong password")
        response = JSONResponse({"unlocked": True})
        response.set_cookie(
            _share_cookie_name(token),
            _share_cookie_value(share),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 12,
        )
        return response

    @app.post("/s/{token}/view")
    def record_share_view(
        token: str, req: ShareViewRequest, request: Request
    ) -> dict[str, Any]:
        """Called once playback actually starts - a view is a watch, not a page load."""
        share = _live_share(token)
        _gate(share, request)
        db.record_share_view(
            token,
            seconds_watched=max(0.0, req.seconds_watched),
            ip_hash=hash_viewer_ip(request.client.host if request.client else None),
            update_last=req.update_last,
        )
        return {"recorded": token}

    def _shared_file(share: Share, attr: str) -> FileResponse:
        render = db.get_render(share.render_id)
        if render is None:
            raise HTTPException(404, "the shared video is no longer available")
        path_value = getattr(render, attr)
        if not path_value or not Path(path_value).exists():
            raise HTTPException(404, "file missing on disk")
        return FileResponse(path_value)

    @app.get("/s/{token}/video")
    def share_video(token: str, request: Request) -> FileResponse:
        share = _live_share(token)
        _gate(share, request)
        return _shared_file(share, "video_path")

    @app.get("/s/{token}/srt")
    def share_srt(token: str, request: Request) -> FileResponse:
        share = _live_share(token)
        _gate(share, request)
        return _shared_file(share, "srt_path")

    @app.get("/s/{token}/download")
    def share_download(token: str, request: Request) -> FileResponse:
        share = _live_share(token)
        _gate(share, request)
        if not share.allow_download:
            raise HTTPException(403, "downloads are disabled for this link")
        render = db.get_render(share.render_id)
        response = _shared_file(share, "video_path")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", render.title if render else "demo").strip("-")
        response.headers["Content-Disposition"] = f'attachment; filename="{safe or "demo"}.mp4"'
        return response

    # -- jobs --------------------------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs(limit: int = 30) -> list[dict[str, Any]]:
        return [_job_json(j) for j in db.list_jobs(limit)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return _job_json(job)

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str) -> dict[str, Any]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.status not in ("failed", "cancelled"):
            raise HTTPException(409, f"only failed/cancelled jobs can be retried (is {job.status})")
        new = db.create_job(job.kind, job.params)
        return {"job": new.id}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if not db.cancel_job(job_id):
            raise HTTPException(409, f"job is {job.status}; only queued jobs can be cancelled")
        return {"cancelled": job_id}

    @app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
    def job_log(job_id: str, offset: int = 0) -> str:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if not job.log_path or not Path(job.log_path).exists():
            return ""
        data = Path(job.log_path).read_text(encoding="utf-8", errors="replace")
        return data[offset:]

    # -- UI ------------------------------------------------------------------------

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(_STATIC_DIR / "index.html")

        @app.get("/s/{token}", include_in_schema=False)
        def share_page(token: str) -> HTMLResponse:
            """Standalone viewer: no app chrome, no login, no bundled app.js."""
            page = _STATIC_DIR / "share.html"
            if not page.exists():
                raise HTTPException(404, "share page missing")
            return HTMLResponse(page.read_text(encoding="utf-8"))

    return app


def _step_summary(step: Any) -> str:
    """One-line description of a step for list views (mirrors the flow editor)."""
    if step.path:
        return str(step.path)
    loc = step.locator
    if loc is not None:
        for key in ("testid", "role", "label", "text", "placeholder", "css"):
            value = getattr(loc, key, None)
            if value:
                out = f"{key}={value}"
                if key == "role" and loc.name:
                    out += f' "{loc.name}"'
                if loc.nth is not None:
                    out += f" nth:{loc.nth}"
                return out
    return str(step.value or "")


def _job_json(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "params": job.params,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
