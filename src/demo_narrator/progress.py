"""Job-friendly progress reporting and log capture.

Servers run generation/recording as background jobs; they need (a) a structured
progress callback for status displays and (b) the full engine log persisted per
job. Both are no-ops unless requested, so CLI behaviour is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

# (phase, detail) — e.g. ("record", "feature 2/3: Calculation overrides")
ProgressFn = Callable[[str, str], None]


def report(on_progress: ProgressFn | None, phase: str, detail: str) -> None:
    """Invoke the callback if provided; never let a callback error kill a job step."""
    if on_progress is None:
        return
    try:
        on_progress(phase, detail)
    except Exception:  # noqa: BLE001 - observer must not break the pipeline
        logging.getLogger(__name__).warning("progress callback failed", exc_info=True)


@contextmanager
def capture_logs(path: Path, *, level: int = logging.INFO) -> Iterator[Path]:
    """Attach a file handler for everything under the demo_narrator logger tree.

    Usage (in a job worker):
        with capture_logs(job_log_path):
            build_demo(...)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger("demo_narrator")
    previous_level = root.level
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)
    root.addHandler(handler)
    try:
        yield path
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        handler.close()
