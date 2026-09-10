"""demo-narrator command-line interface."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
from dotenv import load_dotenv
from rich.markup import escape
from rich.table import Table

from . import __version__
from .config import AppConfig, Secrets, load_config, load_secrets
from .errors import DemoNarratorError
from .logging_setup import console, setup_logging

app = typer.Typer(
    name="demo-narrator",
    help="Turn a raw screen recording of a web app into a narrated demo video.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

logger = logging.getLogger(__name__)

ConfigOpt = Annotated[
    Optional[Path],
    typer.Option("--config", help="Path to config YAML (default: config/config.yaml)."),
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug-level console output.")]


def _bootstrap(config_path: Path | None, verbose: bool) -> tuple[AppConfig, Secrets]:
    load_dotenv()  # secrets may live in .env; never in config.yaml
    setup_logging(verbose=verbose)
    return load_config(config_path), load_secrets()


def _fail(exc: BaseException, verbose: bool) -> None:
    if verbose:
        console.print_exception()
    console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
    raise typer.Exit(code=1)


@app.command()
def generate(
    video: Annotated[Path, typer.Argument(help="Input screen recording (.mp4 or .mkv).")],
    sprint: Annotated[
        Optional[int], typer.Option("--sprint", help="Azure DevOps sprint number for context.")
    ] = None,
    task: Annotated[
        Optional[int], typer.Option("--task", help="Azure DevOps work item id for context.")
    ] = None,
    context: Annotated[
        Optional[Path], typer.Option("--context", help="Free-form context markdown file.")
    ] = None,
    review: Annotated[
        bool,
        typer.Option(
            "--review",
            help="Pause after script generation for review before (expensive) TTS.",
        ),
    ] = False,
    output: Annotated[
        Optional[Path], typer.Option("--output", help="Output directory (default from config).")
    ] = None,
    resume: Annotated[
        Optional[Path],
        typer.Option("--resume", help="Existing run directory: skip already-completed stages."),
    ] = None,
    config: ConfigOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Generate a narrated demo video from a screen recording (fully automatic)."""
    try:
        cfg, secrets = _bootstrap(config, verbose)
        from .pipeline import run_generate

        video_out, script_md, srt = run_generate(
            video=video,
            config=cfg,
            secrets=secrets,
            sprint=sprint,
            task=task,
            context_file=context,
            review=review,
            output_dir=output,
            resume=resume,
        )
        console.print()
        console.print("[bold green]Done![/bold green]")
        console.print(f"  video:     {video_out}")
        console.print(f"  script:    {script_md}")
        console.print(f"  subtitles: {srt}")
    except DemoNarratorError as exc:
        _fail(exc, verbose)


@app.command("regen-audio")
def regen_audio(
    run_dir: Annotated[Path, typer.Argument(help="Run directory of a previous generate run.")],
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help="Override TTS provider: kokoro or elevenlabs."),
    ] = None,
    output: Annotated[
        Optional[Path], typer.Option("--output", help="Output directory (default from config).")
    ] = None,
    config: ConfigOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Re-run stages 4-5 only (after manual script edits, or with another provider)."""
    try:
        cfg, secrets = _bootstrap(config, verbose)
        from .pipeline import run_regen_audio

        video_out, script_md, srt = run_regen_audio(
            run_dir=run_dir,
            config=cfg,
            secrets=secrets,
            provider_name=provider,
            output_dir=output,
        )
        console.print()
        console.print("[bold green]Done![/bold green]")
        console.print(f"  video:     {video_out}")
        console.print(f"  script:    {script_md}")
        console.print(f"  subtitles: {srt}")
    except DemoNarratorError as exc:
        _fail(exc, verbose)


@app.command()
def demo(
    project: Annotated[
        str, typer.Option("--project", help="Project profile name (profiles/<name>.yaml) or path.")
    ],
    flow: Annotated[
        list[Path],
        typer.Option("--flow", help="Flow spec(s) to run. Repeat --flow to combine several into one video."),
    ],
    sprint: Annotated[
        Optional[int], typer.Option("--sprint", help="Sprint number, used to label the intro/outro cards.")
    ] = None,
    captions: Annotated[
        bool, typer.Option("--captions/--no-captions", help="Burn TikTok-style karaoke captions.")
    ] = True,
    bookends: Annotated[
        bool, typer.Option("--bookends/--no-bookends", help="Add intro + outro title cards.")
    ] = True,
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help="Override TTS provider: kokoro or elevenlabs."),
    ] = None,
    output: Annotated[
        Optional[Path], typer.Option("--output", help="Output directory (default from config).")
    ] = None,
    show_browser: Annotated[
        bool, typer.Option("--show-browser", help="Run the browser headed (visible) instead of headless.")
    ] = False,
    config: ConfigOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Generate a narrated demo by driving a running web app with Playwright.

    Pass --flow multiple times to stitch several features into ONE video (each
    flow's beats play in order, with one intro and one outro).
    """
    try:
        cfg, secrets = _bootstrap(config, verbose)
        from .demo.build import build_demo
        from .demo.flow import load_flow, merge_flows
        from .demo.models import FlowSpec
        from .demo.profile import load_profile, resolve_profile_path

        prof = load_profile(resolve_profile_path(project, cfg.resolve("profiles")))
        flows = [load_flow(f) for f in flow]
        if len(flows) == 1:
            flow_spec: FlowSpec = flows[0]
        else:
            name = f"sprint-{sprint}-demo" if sprint else "combined-demo"
            flow_spec = merge_flows(flows, name=name, title=name.replace("-", " ").title())
        for f in flows:
            if f.project and f.project != prof.name:
                console.print(
                    f"[yellow]Warning:[/yellow] flow project {f.project!r} != profile {prof.name!r}."
                )
        art = build_demo(
            prof, flow_spec, cfg, secrets,
            provider_name=provider, output_dir=output, headless=not show_browser,
            captions=captions, bookends=bookends, sprint=sprint,
        )
        console.print()
        console.print("[bold green]Done![/bold green]")
        console.print(f"  video:     {art.video}")
        console.print(f"  script:    {art.script_md}")
        console.print(f"  subtitles: {art.srt}")
        console.print(f"  run dir:   {art.run_dir}")
    except DemoNarratorError as exc:
        _fail(exc, verbose)


@app.command("demo-generate")
def demo_generate(
    project: Annotated[
        str, typer.Option("--project", help="Project profile name (profiles/<name>.yaml) or path.")
    ],
    task: Annotated[
        list[int],
        typer.Option("--task", help="Work item id to demo. Repeat --task for one multi-feature video."),
    ],
    sprint: Annotated[
        Optional[int], typer.Option("--sprint", help="Sprint number (labels the flow + intro/outro).")
    ] = None,
    out: Annotated[
        Optional[Path],
        typer.Option("--out", help="Where to write the flow (default: profiles/<project>/flows/)."),
    ] = None,
    run: Annotated[
        bool, typer.Option("--run", help="Immediately record the generated flow after writing it.")
    ] = False,
    hint: Annotated[
        Optional[Path],
        typer.Option("--hint", help="Guidance file (single task). Else profiles/<project>/hints/task-<id>.md."),
    ] = None,
    max_steps: Annotated[
        int, typer.Option("--max-steps", help="Cap on generated flow steps (per task).")
    ] = 14,
    polish: Annotated[
        bool,
        typer.Option("--polish/--no-polish", help="Refine narration + dedupe steps with Claude."),
    ] = True,
    config: ConfigOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Generate a demo flow by having Claude walk the app (supervised).

    Pass --task multiple times to author one flow with a beat per task (a whole
    sprint demo in a single video).
    """
    try:
        cfg, secrets = _bootstrap(config, verbose)
        from .demo.build import _resolve_storage_state, build_demo
        from .demo.decision import create_decision_backend
        from .demo.flow import merge_flows, save_flow
        from .demo.generate import fetch_task, generate_flow, refine_flow, resolve_hint
        from .demo.profile import load_profile, resolve_profile_path

        profiles_dir = cfg.resolve("profiles")
        prof = load_profile(resolve_profile_path(project, profiles_dir))
        storage = _resolve_storage_state(prof)
        backend = create_decision_backend(cfg.claude, secrets)

        flows = []
        for tid in task:
            console.print(f"Fetching task [bold]{tid}[/bold] from Azure DevOps ...")
            task_text = fetch_task(prof, tid)
            guidance = resolve_hint(
                prof.name, tid, hint if len(task) == 1 else None, profiles_dir
            )
            console.print(
                f"Generating flow for {tid} — Claude walks the app"
                + (" (using hint)" if guidance else "")
                + " ..."
            )
            f = generate_flow(
                prof, tid, task_text, backend,
                storage_state=storage, max_steps=max_steps, hint=guidance,
            )
            if polish:
                f = refine_flow(f, task_text, backend, polish=True)
            flows.append(f)

        if len(flows) == 1:
            flow = flows[0]
            default_name = f"{flow.name}.demoflow.yaml"
        else:
            stem = f"sprint-{sprint}-demo" if sprint else "combined-demo"
            flow = merge_flows(flows, name=stem, title=stem.replace("-", " ").title())
            default_name = f"{stem}.demoflow.yaml"

        target = out or profiles_dir / prof.name / "flows" / default_name
        save_flow(flow, target)
        console.print(f"[bold green]Flow written:[/bold green] {target}")
        console.print(f"  {len(flow.beats)} beat(s), {len(flow.flat_steps())} steps. Review, then run:")
        console.print(f"  demo-narrator demo --project {prof.name} --flow {target.as_posix()}")

        if run:
            console.print("\nRecording the generated flow ...")
            art = build_demo(prof, flow, cfg, secrets, sprint=sprint)
            console.print("[bold green]Done![/bold green]")
            console.print(f"  video:     {art.video}")
            console.print(f"  run dir:   {art.run_dir}")
    except DemoNarratorError as exc:
        _fail(exc, verbose)


@app.command("demo-auth")
def demo_auth(
    project: Annotated[
        str, typer.Option("--project", help="Project profile name (profiles/<name>.yaml) or path.")
    ],
    show_browser: Annotated[
        bool,
        typer.Option("--show-browser", help="Log in manually in a visible browser instead of using credentials."),
    ] = False,
    config: ConfigOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Capture an authenticated browser session for a project (writes storageState)."""
    try:
        cfg, _secrets = _bootstrap(config, verbose)
        from .demo.profile import load_profile, resolve_profile_path
        from .demo.runner import capture_session

        prof = load_profile(resolve_profile_path(project, cfg.resolve("profiles")))
        target = prof.auth.storage_state
        if not target:
            raise DemoNarratorError(
                f"Profile {prof.name!r} has no auth.storage_state path to write the session to."
            )
        if show_browser:
            from .demo.runner import capture_session_interactive

            console.print(
                f"Capturing session for [bold]{prof.name}[/bold] — a browser window will "
                "open; sign in there yourself (MFA is fine). It closes once you're in."
            )
            out = capture_session_interactive(prof, out_state=Path(target))
        else:
            console.print(
                f"Capturing session for [bold]{prof.name}[/bold] via the login recipe ..."
            )
            out = capture_session(prof, out_state=Path(target), headless=True)
        console.print(f"[bold green]Session saved:[/bold green] {out}")
    except DemoNarratorError as exc:
        _fail(exc, verbose)


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port.")] = 8765,
    worker: Annotated[
        bool, typer.Option("--worker/--no-worker", help="Run the job worker in-process.")
    ] = True,
    config: ConfigOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Run the web UI + API (single-tenant server mode)."""
    try:
        cfg, secrets = _bootstrap(config, verbose)
        try:
            import uvicorn
        except ImportError as exc:
            raise DemoNarratorError(
                'The server extra is not installed: pip install "demo-narrator[server]"'
            ) from exc
        from .server.app import create_app
        from .server.db import Database
        from .server.jobs import Worker

        db = Database(cfg.resolve(cfg.output_dir) / "server" / "server.db")
        web = create_app(cfg, secrets, db)
        if worker:
            Worker(db, cfg, secrets).start()
        console.print(f"[bold green]demo-narrator web[/bold green] on http://{host}:{port}")
        uvicorn.run(web, host=host, port=port, log_level="warning")
    except DemoNarratorError as exc:
        _fail(exc, verbose)


@app.command("config-check")
def config_check(
    config: ConfigOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Validate configuration, environment variables, ffmpeg, and API connectivity."""
    try:
        cfg, secrets = _bootstrap(config, verbose)
        from .checks import run_all_checks

        results = run_all_checks(cfg, secrets)
        table = Table(title=f"demo-narrator {__version__} — configuration check")
        table.add_column("check")
        table.add_column("status")
        table.add_column("detail", overflow="fold")
        failed = False
        for r in results:
            if r.ok:
                status = "[green]ok[/green]"
            elif r.fatal:
                status = "[red]FAIL[/red]"
                failed = True
            else:
                status = "[yellow]warn[/yellow]"
            table.add_row(escape(r.name), status, escape(r.detail))
        console.print(table)
        if failed:
            console.print("[bold red]Configuration check failed.[/bold red]")
            raise typer.Exit(code=1)
        console.print("[bold green]All required checks passed.[/bold green]")
    except DemoNarratorError as exc:
        _fail(exc, verbose)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
