"""The setup-assistant conversation: one turn of the onboarding chat.

A turn is single-shot from the model's point of view — the whole transcript,
the current working draft, and (optionally) a fresh read-only inspection of the
app are packed into one prompt, and the model returns a structured reply plus
proposed profile changes. This keeps it compatible with both decision backends
(agent_sdk / api_key) — no streaming or multi-turn tool loop needed.

State (persisted per project) is a plain dict:
    {
      "messages": [{"role": "user"|"assistant"|"system", "text": str,
                    "questions": [str]?}],
      "draft": {"context": str, "feature_map": {name: path},
                "sign_in_path": str|None, "ready": bool},
      "pending_look_at": [str],   # paths the assistant asked to see next turn
    }
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .decision import DecisionBackend
from .inspect import format_observations, inspect_app
from .profile import ProjectProfile

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    """A safe feature_map key: lowercase letters/digits/underscores only."""
    return _SLUG_RE.sub("_", name.strip().lower()).strip("_") or "route"

_ONBOARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "context": {"type": "string"},
        "feature_map": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
                "required": ["name", "path"],
            },
        },
        "sign_in_path": {"type": ["string", "null"]},
        "look_at": {"type": "array", "items": {"type": "string"}},
        "ready": {"type": "boolean"},
    },
    "required": ["reply"],
}


def empty_state() -> dict[str, Any]:
    return {
        "messages": [],
        "draft": {"context": "", "feature_map": {}, "sign_in_path": None, "ready": False},
        "pending_look_at": [],
    }


def _draft_summary(draft: dict[str, Any]) -> str:
    fm = draft.get("feature_map") or {}
    routes = "\n".join(f"    {k}: {v}" for k, v in fm.items()) or "    (none yet)"
    ctx = (draft.get("context") or "").strip() or "(empty)"
    return (
        f'  context:\n    {ctx}\n'
        f'  sign_in_path: {draft.get("sign_in_path") or "(unset)"}\n'
        f'  feature_map (routes):\n{routes}\n'
        f'  ready: {bool(draft.get("ready"))}'
    )


def _transcript(messages: list[dict[str, Any]], limit: int = 20) -> str:
    recent = messages[-limit:]
    lines: list[str] = []
    for m in recent:
        role = m.get("role", "user")
        who = {"user": "USER", "assistant": "YOU", "system": "SYSTEM"}.get(role, role.upper())
        lines.append(f"{who}: {m.get('text', '')}")
    return "\n".join(lines) or "(no messages yet)"


def build_onboard_prompt(
    profile: ProjectProfile,
    state: dict[str, Any],
    user_message: str,
    observations_text: str,
) -> str:
    return f"""You are the SETUP ASSISTANT for demo-narrator, a tool that auto-generates narrated
demo videos of a web app. You are helping a user configure a PROJECT PROFILE for their
app through a short, friendly conversation. Your goals, in order:

1. Build a clear PROJECT CONTEXT: a few sentences describing what the app is, who uses
   it, its main areas, key terminology, and any navigation gotchas. This is fed into
   every future demo generation, so make it genuinely useful — not marketing fluff.
2. Propose a FEATURE_MAP: short route hints (name -> locale-free path) for the app's
   main areas, so the generator can jump straight to a feature. Derive these from the
   pages you inspect (the links you actually see), not guesses.
3. Confirm the SIGN_IN_PATH: the URL fragment that means "not signed in" (e.g. /sign-in,
   /login). If inspecting the app root redirected to a sign-in page, use that path.
4. Ask CLARIFYING QUESTIONS whenever something is ambiguous or missing — do not invent
   facts about the app. Prefer 1–3 focused questions over a wall of them.

APP: {profile.name}
BASE URL: {profile.base_url}

CURRENT DRAFT (what you've proposed so far — refine it, don't restate it verbatim):
{_draft_summary(state.get("draft", {}))}

WHAT I SAW WHEN I INSPECTED THE APP THIS TURN (read-only; final path shows redirects):
{observations_text}

CONVERSATION SO FAR:
{_transcript(state.get("messages", []))}

THE USER JUST SAID:
{user_message or "(the user opened the assistant without typing anything — greet them, say what you can see about the app so far, and ask what it is.)"}

Return JSON matching the schema:
- reply: your conversational message to the user (warm, concise, concrete). Reference
  what you actually saw ("I can see a Projects and a Search area…"). NEVER ask for
  passwords or secrets — logging in is handled elsewhere.
- questions: optional short clarifying questions (also weave them into `reply`).
- context: your CURRENT BEST project context (full text; refine each turn).
- feature_map: your CURRENT BEST route hints as {{name, path}} objects. `name` MUST be a
  short lowercase slug (letters/digits/underscores only, e.g. `projects_overview`,
  `object_search`) — never a display label and never a status word like "(unverified)".
  `path` is locale-free. Return the COMPLETE list each turn; it REPLACES the previous one,
  so include everything you want to keep.
- sign_in_path: the app's sign-in path if you can tell, else omit/null.
- look_at: up to 4 app paths you'd like me to inspect next turn to learn more
  (e.g. ["/projects", "/search"]). Only paths you have reason to believe exist.
- ready: true only when the context + feature_map are solid enough to start generating
  demos and you have no blocking questions.
"""


def _merge_decision(state: dict[str, Any], decision: dict[str, Any]) -> None:
    draft = state["draft"]
    ctx = decision.get("context")
    if isinstance(ctx, str) and ctx.strip():
        draft["context"] = ctx.strip()
    fm = decision.get("feature_map")
    if isinstance(fm, list) and fm:
        # The model returns its COMPLETE current-best map each turn, so replace
        # (not merge) — this prevents renamed/duplicate keys accumulating.
        new_map: dict[str, str] = {}
        for entry in fm:
            name, path = entry.get("name"), entry.get("path")
            if isinstance(name, str) and isinstance(path, str) and name.strip() and path.strip():
                new_map[_slug(name)] = path.strip()
        if new_map:
            draft["feature_map"] = new_map
    sip = decision.get("sign_in_path")
    if isinstance(sip, str) and sip.strip():
        draft["sign_in_path"] = sip.strip()
    draft["ready"] = bool(decision.get("ready"))
    look_at = decision.get("look_at")
    state["pending_look_at"] = [p for p in look_at if isinstance(p, str)] if isinstance(look_at, list) else []


def run_onboard_turn(
    profile: ProjectProfile,
    backend: DecisionBackend,
    state: dict[str, Any],
    user_message: str,
    *,
    storage_state: str | None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """Advance the conversation by one turn, mutating and returning `state`."""
    state.setdefault("messages", [])
    state.setdefault("draft", empty_state()["draft"])
    if user_message.strip():
        state["messages"].append({"role": "user", "text": user_message.strip()})

    # Inspect the app on the first turn, or whenever the assistant asked to see
    # specific pages. Inspection is read-only and best-effort.
    first_turn = not any(m.get("role") == "assistant" for m in state["messages"])
    look_at = state.get("pending_look_at") or []
    observations_text = "(the app was not inspected this turn)"
    if first_turn or look_at:
        try:
            observations = inspect_app(
                profile, look_at, storage_state=storage_state, on_progress=on_progress
            )
            observations_text = format_observations(observations)
            seen = ", ".join(
                f'{o["path"]}{" ✓" if o.get("signed_in") else ""}'
                for o in observations if not o.get("error")
            )
            if seen:
                state["messages"].append({"role": "system", "text": f"🔎 Looked at: {seen}"})
        except Exception as exc:  # noqa: BLE001 - inspection is optional; keep chatting
            logger.warning("app inspection failed: %s", exc)
            observations_text = f"(app inspection failed: {str(exc).splitlines()[0]})"

    prompt = build_onboard_prompt(profile, state, user_message, observations_text)
    decision = backend.ask(prompt, _ONBOARD_SCHEMA)
    _merge_decision(state, decision)

    reply = str(decision.get("reply") or "").strip() or "(no reply)"
    questions = [q for q in decision.get("questions", []) if isinstance(q, str)]
    state["messages"].append({"role": "assistant", "text": reply, "questions": questions})
    return state


def merged_profile_yaml(current_yaml: str, draft: dict[str, Any]) -> str:
    """Apply the draft (context, feature_map, sign_in_path) onto a profile's YAML.

    Re-dumps the mapping (comments are not preserved), then validates. Raises
    ValueError if the result is not a valid profile.
    """
    import yaml

    from .profile import ProjectProfile, expand_env

    raw = yaml.safe_load(current_yaml) or {}
    if not isinstance(raw, dict):
        raise ValueError("profile must be a YAML mapping")
    ctx = (draft.get("context") or "").strip()
    if ctx:
        raw["context"] = ctx
    fm = draft.get("feature_map") or {}
    if fm:
        existing = raw.get("feature_map") or {}
        if not isinstance(existing, dict):
            existing = {}
        existing.update(fm)
        raw["feature_map"] = existing
    sip = draft.get("sign_in_path")
    if isinstance(sip, str) and sip.strip():
        auth = raw.get("auth")
        if not isinstance(auth, dict):
            auth = {}
        auth["sign_in_path"] = sip.strip()
        raw["auth"] = auth
    ProjectProfile.model_validate(expand_env(raw))  # fail before writing garbage
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
