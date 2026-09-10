"""Locate the Claude Code CLI so the Agent SDK can spawn it.

The SDK finds ``claude`` on PATH. On fresh installs (or when the terminal that
launched this process predates the install) it may not be there yet, so we also
probe the well-known install locations and prepend the winning directory.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _candidates() -> list[Path]:
    home = Path.home()
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    names = ("claude.exe", "claude.cmd", "claude")
    dirs = [
        home / ".local" / "bin",
        home / ".claude" / "local",
        Path(appdata) / "npm" if appdata else None,
        Path(local) / "Programs" / "claude" if local else None,
    ]
    return [d / n for d in dirs if d is not None for n in names]


def ensure_claude_on_path() -> str | None:
    """Return the path to the ``claude`` CLI, prepending its dir to PATH if needed."""
    found = shutil.which("claude")
    if found:
        return found
    for cand in _candidates():
        if cand.exists():
            os.environ["PATH"] = str(cand.parent) + os.pathsep + os.environ.get("PATH", "")
            logger.debug("Added %s to PATH for the Claude CLI.", cand.parent)
            return str(cand)
    return None
