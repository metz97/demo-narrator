"""REST Azure DevOps tracker (mocked httpx)."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from demo_narrator.errors import ContextError
from demo_narrator.trackers.azure_devops import AzureDevOpsTracker, Sprint


class _Resp:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload else "")

    def json(self) -> Any:
        return self._payload


class _FakeHttp:
    """Records requests; pops canned responses in order."""

    def __init__(self, responses: list[_Resp]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, headers: Any = None, json: Any = None) -> _Resp:
        self.calls.append((method, url, json))
        self.last_headers = headers
        return self.responses.pop(0)


def _tracker(responses: list[_Resp], team: str = "LCA") -> tuple[AzureDevOpsTracker, _FakeHttp]:
    http = _FakeHttp(responses)
    t = AzureDevOpsTracker("thenbs", "Cirrus", team, pat="secret-pat", http=http)  # type: ignore[arg-type]
    return t, http


def _team_fields(*values: tuple[str, bool], field: str = "System.AreaPath") -> _Resp:
    """A teamfieldvalues response — fetched once before the first work-item WIQL."""
    return _Resp(200, {
        "field": {"referenceName": field},
        "values": [{"value": v, "includeChildren": kids} for v, kids in values],
    })


def test_org_name_normalized_and_pat_basic_auth() -> None:
    t, http = _tracker([_Resp(200, {"value": []})])
    t.list_sprints()
    method, url, _ = http.calls[0]
    assert url.startswith("https://dev.azure.com/thenbs/Cirrus/LCA/_apis/work/teamsettings/iterations")
    expected = base64.b64encode(b":secret-pat").decode()
    assert http.last_headers["Authorization"] == f"Basic {expected}"


def test_list_sprints_parses_iterations() -> None:
    payload = {
        "value": [
            {"id": "abc", "name": "Sprint 174", "path": "Cirrus\\Sprint 174",
             "attributes": {"timeFrame": "current"}},
            {"id": "def", "name": "Sprint 175", "path": "Cirrus\\Sprint 175", "attributes": {}},
        ]
    }
    t, _ = _tracker([_Resp(200, payload)])
    sprints = t.list_sprints()
    assert [s.name for s in sprints] == ["Sprint 174", "Sprint 175"]
    assert sprints[0].timeframe == "current"
    assert sprints[1].timeframe is None


def test_list_work_items_wiql_then_fields() -> None:
    wiql = {"workItems": [{"id": 90396}, {"id": 92191}]}
    fields = {
        "value": [
            {"id": 90396, "fields": {"System.Title": "Copy object", "System.State": "Done",
                                     "System.WorkItemType": "PBI", "System.IterationPath": "Cirrus\\Sprint 174"}},
            {"id": 92191, "fields": {"System.Title": "Overrides", "System.State": "Done",
                                     "System.WorkItemType": "PBI", "System.IterationPath": "Cirrus\\Sprint 174"}},
        ]
    }
    t, http = _tracker([_team_fields(("Cirrus\\Delivery\\LCA", False)), _Resp(200, wiql), _Resp(200, fields)])
    items = t.list_work_items(Sprint(id="x", name="Sprint 174", path="Cirrus\\Sprint 174", timeframe=None))
    assert [i.id for i in items] == [90396, 92191]
    # tree fields only allow =/UNDER in WIQL
    assert "[System.IterationPath] UNDER 'Cirrus\\Sprint 174'" in http.calls[1][2]["query"]
    assert items[0].title == "Copy object"


def test_list_work_items_empty_sprint() -> None:
    t, _ = _tracker([_team_fields(("Cirrus\\Delivery\\LCA", False)), _Resp(200, {"workItems": []})])
    sprint = Sprint(id="x", name="Sprint 999", path="Cirrus\\Sprint 999", timeframe=None)
    assert t.list_work_items(sprint) == []


def test_string_sprint_resolved_via_iterations() -> None:
    iterations = {"value": [
        {"id": "a", "name": "Sprint 174", "path": "Cirrus\\Sprint 174", "attributes": {}},
    ]}
    t, http = _tracker([
        _Resp(200, iterations),
        _team_fields(("Cirrus\\Delivery\\LCA", False)),
        _Resp(200, {"workItems": []}),
    ])
    assert t.list_work_items("sprint 174") == []
    assert "UNDER 'Cirrus\\Sprint 174'" in http.calls[2][2]["query"]


def test_work_items_scoped_to_team_area_path() -> None:
    """An iteration is shared by every team in the project, so the WIQL must also
    filter on the team's area paths — otherwise a sprint lists the whole project."""
    t, http = _tracker([_team_fields(("Cirrus\\Delivery\\LCA", False)), _Resp(200, {"workItems": []})])
    t.list_work_items(Sprint(id="x", name="Sprint 174", path="Cirrus\\Sprint 174", timeframe=None))
    assert http.calls[0][1].endswith(
        "/Cirrus/LCA/_apis/work/teamsettings/teamfieldvalues?api-version=7.1"
    )
    assert "([System.AreaPath] = 'Cirrus\\Delivery\\LCA')" in http.calls[1][2]["query"]


def test_team_area_path_include_children_uses_under() -> None:
    t, http = _tracker([
        _team_fields(("Cirrus\\Delivery\\LCA", True), ("Cirrus\\Design UX", False)),
        _Resp(200, {"workItems": []}),
    ])
    t.list_work_items(Sprint(id="x", name="Sprint 174", path="Cirrus\\Sprint 174", timeframe=None))
    assert (
        "([System.AreaPath] UNDER 'Cirrus\\Delivery\\LCA' "
        "OR [System.AreaPath] = 'Cirrus\\Design UX')"
    ) in http.calls[1][2]["query"]


def test_team_scope_fetched_once_and_reused() -> None:
    t, http = _tracker([
        _team_fields(("Cirrus\\Delivery\\LCA", False)),
        _Resp(200, {"workItems": []}),
        _Resp(200, {"workItems": []}),
    ])
    t.list_work_items(Sprint(id="x", name="Sprint 174", path="Cirrus\\Sprint 174", timeframe=None))
    t.list_work_items(Sprint(id="y", name="Sprint 175", path="Cirrus\\Sprint 175", timeframe=None))
    assert sum("teamfieldvalues" in url for _m, url, _b in http.calls) == 1


def test_no_team_configured_skips_area_filter() -> None:
    t, http = _tracker([_Resp(200, {"workItems": []})], team="")
    t.list_work_items(Sprint(id="x", name="Sprint 174", path="Cirrus\\Sprint 174", timeframe=None))
    assert "AreaPath" not in http.calls[0][2]["query"]


def test_team_field_lookup_failure_falls_back_to_unscoped() -> None:
    """A missing/forbidden team-field endpoint should over-report, not break listing."""
    t, http = _tracker([_Resp(404, {}, text="not found"), _Resp(200, {"workItems": []})])
    assert t.list_work_items(Sprint(id="x", name="S", path="Cirrus\\S", timeframe=None)) == []
    assert "AreaPath" not in http.calls[1][2]["query"]


def test_unknown_string_sprint_raises() -> None:
    t, _ = _tracker([_Resp(200, {"value": []})])
    with pytest.raises(ContextError, match="No sprint matching"):
        t.list_work_items("Sprint 999")


def test_auth_failure_raises_context_error() -> None:
    t, _ = _tracker([_Resp(401)])
    with pytest.raises(ContextError, match="rejected"):
        t.list_sprints()


def test_work_item_text_formats_markdown() -> None:
    item = {
        "id": 90396,
        "fields": {
            "System.Title": "Copy and paste an object",
            "System.WorkItemType": "Product Backlog Item",
            "System.State": "Done",
            "System.Description": "<p>Copy objects <b>between</b> assessments</p>",
        },
    }
    t, _ = _tracker([_Resp(200, item)])
    text = t.work_item_text(90396)
    assert "Copy and paste an object" in text
    assert "Copy objects between assessments" in text  # HTML stripped
    assert "<p>" not in text


def test_missing_org_rejected() -> None:
    with pytest.raises(ContextError):
        AzureDevOpsTracker("", "Cirrus")
