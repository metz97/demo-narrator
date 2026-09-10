"""Pydantic models for the flow spec (*.demoflow.yaml).

The flow spec is the app-agnostic contract between flow generation (Claude) and
the runner. It is validated strictly: unknown keys are rejected and each action
must carry the fields it needs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

# Actions the runner understands. Element-acting actions require a `locator`.
Action = Literal[
    "goto",
    "click",
    "fill",
    "press",
    "select",
    "hover",
    "scroll_to",
    "highlight",
    "wait_for",
    "expect",
    "dwell",
]

_NEEDS_LOCATOR = {"click", "fill", "select", "hover", "scroll_to", "highlight", "wait_for", "expect"}
_LOCATOR_STRATEGIES = ("testid", "role", "label", "text", "placeholder", "css")


class Locator(BaseModel):
    """A single Playwright locator, preferring semantic strategies over CSS."""

    model_config = ConfigDict(extra="forbid")

    testid: str | None = None
    role: str | None = None
    name: str | None = None  # accessible name, used with `role`
    label: str | None = None
    text: str | None = None
    placeholder: str | None = None
    css: str | None = None
    nth: int | None = None
    within: "Locator | None" = None

    @model_validator(mode="after")
    def _one_strategy(self) -> "Locator":
        chosen = [s for s in _LOCATOR_STRATEGIES if getattr(self, s) is not None]
        if not chosen:
            raise ValueError(
                f"locator must use one of {_LOCATOR_STRATEGIES}; got {self.model_dump(exclude_none=True)}"
            )
        if len(chosen) > 1:
            raise ValueError(f"locator must use exactly one strategy; got {chosen}")
        if self.name is not None and self.role is None:
            raise ValueError("locator 'name' is only valid together with 'role'")
        return self


class Step(BaseModel):
    """One action in a flow, optionally narrated."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    locator: Locator | None = None
    path: str | None = None                 # goto
    value: str | None = None                # fill / select
    keys: str | None = None                 # press
    state: Literal["visible", "hidden", "attached"] = "visible"  # wait_for
    say: str | None = None                  # narration for this beat
    pacing: Literal["audio", "fixed"] = "audio"
    min_duration_sec: float = 0.0
    settle_ms: int = 0

    @model_validator(mode="after")
    def _require_fields(self) -> "Step":
        if self.action == "goto" and not self.path:
            raise ValueError("action 'goto' requires 'path'")
        if self.action in _NEEDS_LOCATOR and self.locator is None:
            raise ValueError(f"action {self.action!r} requires a 'locator'")
        if self.action == "fill" and self.value is None:
            raise ValueError("action 'fill' requires 'value'")
        if self.action == "press" and not self.keys:
            raise ValueError("action 'press' requires 'keys'")
        return self


class Beat(BaseModel):
    """A group of steps demonstrating one thing (typically one work item)."""

    model_config = ConfigDict(extra="forbid")

    task: int | None = None
    title: str = ""
    steps: list[Step]


class Viewport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int = 1280
    height: int = 720


class FlowSpec(BaseModel):
    """A complete demo flow."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    name: str
    title: str = ""
    project: str
    locale: str | None = None
    viewport: Viewport | None = None
    beats: list[Beat]

    def flat_steps(self) -> list[tuple[int | None, Step]]:
        """All steps with their beat's task id, in order."""
        return [(beat.task, st) for beat in self.beats for st in beat.steps]


Locator.model_rebuild()  # resolve the self-referential `within` field
