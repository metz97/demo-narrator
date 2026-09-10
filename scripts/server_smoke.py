"""M0 acceptance check: drive the demo engine headless with NO CLI involvement,
the way a server job worker will.

Exercises: REST tracker (sprints + work-item text), decision backend (one
narration-polish call), per-job log capture, progress callbacks, and a full
per-beat record + assembly.

Usage (from the repo root, venv python):
    python scripts/server_smoke.py --project lca-tool --task 92163 \
        --flow profiles/lca-tool/flows/task-92163-generated.demoflow.yaml
Add --generate to also run supervised flow generation (slower, several minutes).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from demo_narrator.config import load_config, load_secrets
from demo_narrator.demo.build import build_demo
from demo_narrator.demo.decision import create_decision_backend
from demo_narrator.demo.flow import load_flow, save_flow
from demo_narrator.demo.generate import generate_flow, refine_flow, resolve_hint
from demo_narrator.demo.profile import load_profile, resolve_profile_path
from demo_narrator.logging_setup import setup_logging
from demo_narrator.progress import capture_logs
from demo_narrator.trackers import tracker_from_profile


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--flow", type=Path, help="Existing flow to record (skips generation).")
    ap.add_argument("--generate", action="store_true", help="Run supervised generation too.")
    ap.add_argument("--no-record", action="store_true", help="Skip the record/assemble stage.")
    args = ap.parse_args()

    load_dotenv()
    setup_logging(verbose=False)
    config = load_config(None)
    secrets = load_secrets()

    profiles_dir = config.resolve("profiles")
    profile = load_profile(resolve_profile_path(args.project, profiles_dir))
    backend = create_decision_backend(config.claude, secrets)
    print(f"[1] profile={profile.name}  claude-backend={backend.name}")

    # -- tracker over REST (no az CLI in the request path) --------------------
    tracker = tracker_from_profile(profile)
    sprints = tracker.list_sprints()
    current = next((s for s in sprints if s.timeframe == "current"), None)
    print(f"[2] tracker: {len(sprints)} sprints; current={current.name if current else '?'}")
    task_text = tracker.work_item_text(args.task)
    print(f"[3] work item {args.task}: {task_text.splitlines()[2][:80]}")

    # -- flow: load or generate ------------------------------------------------
    if args.generate:
        hint = resolve_hint(profile.name, args.task, None, profiles_dir)
        from demo_narrator.demo.runner import ensure_session

        flow = generate_flow(
            profile, args.task, task_text, backend,
            storage_state=ensure_session(profile), hint=hint,
            on_progress=lambda phase, detail: print(f"    [{phase}] {detail}"),
        )
        flow = refine_flow(flow, task_text, backend, polish=True)
        target = profiles_dir / profile.name / "flows" / f"{flow.name}.demoflow.yaml"
        save_flow(flow, target)
        print(f"[4] generated flow: {target} ({len(flow.flat_steps())} steps)")
    elif args.flow:
        flow = load_flow(args.flow)
        # one live decision-backend call, the cheap way (rewrites narration lines)
        refine_flow(flow, task_text, backend, polish=True)
        print(f"[4] loaded flow {flow.name} ({len(flow.flat_steps())} steps); narration polished via {backend.name}")
    else:
        print("[4] no --flow/--generate given; nothing to record")
        return 0

    if args.no_record:
        return 0

    # -- record + assemble with job-style logging/progress ---------------------
    log_path = config.resolve(config.output_dir) / "jobs" / "smoke.log"
    with capture_logs(log_path):
        art = build_demo(
            profile, flow, config, secrets,
            bookends=False,  # keep the smoke run short
            on_progress=lambda phase, detail: print(f"    [{phase}] {detail}"),
        )
    print(f"[5] video: {art.video}")
    print(f"[6] job log captured: {log_path} ({log_path.stat().st_size} bytes)")
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
