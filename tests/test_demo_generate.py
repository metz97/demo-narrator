"""Pure-function tests for supervised flow generation (no browser/network)."""

from __future__ import annotations

from demo_narrator.demo.generate import (
    _build_prompt,
    _format_elements,
    _prepare_step,
    _suggest_locator,
    _title_from_task,
    dedupe_steps,
)
from demo_narrator.demo.profile import ProjectProfile


def test_build_prompt_includes_project_context_when_set() -> None:
    prof = ProjectProfile(
        name="app", base_url="http://x", context="This app manages carbon assessments."
    )
    with_ctx = _build_prompt("TASK", prof, "http://x/p", [], "els", 12, None)
    assert "ABOUT THIS APP" in with_ctx
    assert "carbon assessments" in with_ctx
    # absent context -> no stray header
    prof2 = ProjectProfile(name="app", base_url="http://x")
    assert "ABOUT THIS APP" not in _build_prompt("TASK", prof2, "http://x/p", [], "els", 12, None)


def test_format_elements_shows_section_context() -> None:
    """Section context ( @ "...") disambiguates identical controls across groups
    so the model can honour guidance like 'the Manufacturer filter'."""
    els = [
        {"role": "checkbox", "name": "Acme (5)", "testid": None, "context": "Manufacturer", "enabled": True},
        {"role": "checkbox", "name": "Globex (3)", "testid": None, "context": "Program operator", "enabled": True},
    ]
    listing = _format_elements(els)
    assert '@ "Manufacturer"' in listing
    assert '@ "Program operator"' in listing
    # a control whose context equals its own name shouldn't get a noisy suffix
    same = _format_elements([{"role": "heading", "name": "Manufacturer", "context": "Manufacturer", "enabled": True}])
    assert "@ " not in same


def test_format_elements_collapses_long_runs() -> None:
    els = [
        {"role": "link", "name": f"Category {i}", "testid": "filter-category-link", "context": "", "enabled": True}
        for i in range(30)
    ]
    listing = _format_elements(els)
    # a few examples + a summary line, not 30 rows
    assert listing.count("filter-category-link") <= 6
    assert "more elements share locator" in listing
from demo_narrator.demo.models import Locator, Step


def test_dedupe_drops_consecutive_identical_steps() -> None:
    fill = Step(action="fill", locator=Locator(role="combobox", name="Search"), value="tes2")
    fill_again = Step(action="fill", locator=Locator(role="combobox", name="Search"), value="tes2")
    cancel = Step(action="click", locator=Locator(role="button", name="Cancel"))
    fill_later = Step(action="fill", locator=Locator(role="combobox", name="Search"), value="tes2")
    out = dedupe_steps([fill, fill_again, cancel, fill_later])
    # consecutive dup collapsed; the non-consecutive repeat after cancel is kept
    assert [s.action for s in out] == ["fill", "click", "fill"]


def test_suggest_locator_prefers_testid_then_role_name_then_text() -> None:
    assert _suggest_locator({"testid": "item", "role": "button", "name": "X"}) == {"testid": "item"}
    assert _suggest_locator({"role": "link", "name": "tes2", "testid": None}) == {
        "role": "link", "name": "tes2",
    }
    # a role that doesn't take an accessible name falls back to text
    assert _suggest_locator({"role": "cell", "name": "42", "testid": None}) == {"text": "42"}
    assert _suggest_locator({"role": "generic", "name": "", "tag": "div"}) == {"css": "div"}


def test_prepare_step_coerces_role_plus_text_to_name() -> None:
    step, err = _prepare_step(
        {"action": "click", "locator": {"role": "link", "text": "tes2"}}
    )
    assert err is None
    assert step is not None
    assert step.locator is not None
    assert step.locator.role == "link"
    assert step.locator.name == "tes2"
    assert step.locator.text is None


def test_prepare_step_drops_redundant_text_when_name_present() -> None:
    step, err = _prepare_step(
        {"action": "click", "locator": {"role": "button", "name": "Save", "text": "Save"}}
    )
    assert err is None and step is not None and step.locator is not None
    assert step.locator.text is None and step.locator.name == "Save"


def test_prepare_step_reports_invalid() -> None:
    step, err = _prepare_step({"action": "click"})  # click without locator
    assert step is None
    assert err is not None


def test_title_from_task_extracts_heading() -> None:
    text = (
        "# Work item 90396\n\n"
        "## [90396] Product Backlog Item: LCA - Project - Copy and paste an object (In Progress)\n\n"
        "### Description\nblah"
    )
    assert _title_from_task(text, 90396) == "LCA - Project - Copy and paste an object"
    assert _title_from_task("no headings here", 5) == "Task 5"
