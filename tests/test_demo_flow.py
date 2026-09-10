"""Flow-spec model validation and loading (demo mode)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from demo_narrator.demo.flow import load_flow
from demo_narrator.demo.models import FlowSpec, Locator, Step
from demo_narrator.errors import FlowError

REAL_FLOW = Path("profiles/lca-tool/flows/task-90396-copy-object.demoflow.yaml")


def test_real_flow_loads() -> None:
    flow = load_flow(REAL_FLOW)
    assert flow.project == "lca-tool"
    assert len(flow.beats) == 1
    assert flow.beats[0].task == 90396
    steps = flow.flat_steps()
    assert len(steps) >= 10
    assert steps[0][1].action == "goto"


def test_locator_requires_one_strategy() -> None:
    with pytest.raises(ValidationError):
        Locator()  # no strategy
    with pytest.raises(ValidationError):
        Locator(testid="x", text="y")  # two strategies
    with pytest.raises(ValidationError):
        Locator(name="Save")  # name without role
    assert Locator(role="button", name="Save").role == "button"
    assert Locator(testid="item", nth=2).nth == 2


def test_step_requires_action_fields() -> None:
    with pytest.raises(ValidationError):
        Step(action="goto")  # missing path
    with pytest.raises(ValidationError):
        Step(action="click")  # missing locator
    with pytest.raises(ValidationError):
        Step(action="fill", locator=Locator(testid="q"))  # missing value
    with pytest.raises(ValidationError):
        Step(action="press")  # missing keys
    ok = Step(action="fill", locator=Locator(label="Search"), value="concrete")
    assert ok.value == "concrete"
    assert Step(action="dwell", say="hello", min_duration_sec=2).min_duration_sec == 2


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        FlowSpec.model_validate(
            {"name": "f", "project": "p", "beats": [], "bogus": 1}
        )


def test_load_flow_errors(tmp_path: Path) -> None:
    with pytest.raises(FlowError):
        load_flow(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.demoflow.yaml"
    bad.write_text("name: x\nproject: p\nbeats:\n  - steps:\n    - action: click\n", encoding="utf-8")
    with pytest.raises(FlowError):
        load_flow(bad)  # click without locator
