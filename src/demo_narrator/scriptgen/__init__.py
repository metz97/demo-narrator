"""Stage 3 — script generation via Claude (agent_sdk or api_key backend)."""

from .generator import ScriptGenerator, ScriptRequest, SegmentPrompt

__all__ = ["ScriptGenerator", "ScriptRequest", "SegmentPrompt"]
