"""Logging: structured file log in the run directory + friendly console output."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console()

_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"


def setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    """Configure the root logger. Call once per CLI invocation.

    Console gets INFO (DEBUG with --verbose) via rich; the run.log file always
    captures DEBUG so failed runs can be diagnosed after the fact.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console_handler = RichHandler(
        console=console, show_path=False, rich_tracebacks=verbose, markup=False
    )
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        root.addHandler(file_handler)

    # Third-party noise stays at WARNING unless --verbose.
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)


def attach_run_log(log_file: Path) -> None:
    """Add the run.log file handler once the run directory exists."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    logging.getLogger().addHandler(file_handler)
