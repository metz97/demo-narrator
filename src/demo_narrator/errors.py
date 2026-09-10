"""Application error types.

Every error raised deliberately by demo-narrator is a DemoNarratorError with
an actionable, user-facing message. The CLI catches these and prints them
without a traceback (use --verbose for tracebacks).
"""

from __future__ import annotations


class DemoNarratorError(Exception):
    """Base class for all expected application errors."""


class ConfigError(DemoNarratorError):
    """Configuration file or environment problem."""


class FFmpegNotFoundError(DemoNarratorError):
    """ffmpeg/ffprobe binary missing."""


class FFmpegError(DemoNarratorError):
    """An ffmpeg/ffprobe invocation failed."""


class InputVideoError(DemoNarratorError):
    """The input video failed validation."""


class ContextError(DemoNarratorError):
    """Stage 2 context gathering failed."""


class ScriptGenerationError(DemoNarratorError):
    """Stage 3 script generation failed."""


class TTSError(DemoNarratorError):
    """Stage 4 voice synthesis failed."""


class CostGuardError(DemoNarratorError):
    """A configured cost cap would be exceeded; run was aborted."""


class RunDirError(DemoNarratorError):
    """Run directory missing or inconsistent (resume / regen-audio)."""


class ProfileError(DemoNarratorError):
    """A project profile is missing, unreadable, or invalid."""


class FlowError(DemoNarratorError):
    """A demo flow spec is missing, unreadable, or invalid."""


class PlaywrightError(DemoNarratorError):
    """Browser automation failed (missing install, auth, or a flow step)."""
