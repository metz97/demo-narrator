"""Endpoints the run wizard depends on: server meta, the enriched flow list,
and the render estimate that step 4 shows before you spend a render."""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from demo_narrator.config import AppConfig, Secrets  # noqa: E402
from demo_narrator.server.app import create_app  # noqa: E402
from demo_narrator.server.db import Database  # noqa: E402

_FLOW = {
    "version": 3,
    "name": "task-1-generated",
    "title": "Copy & paste carbon assessments",
    "project": "test-app",
    "viewport": {"width": 1280, "height": 720},
    "beats": [
        {
            "task": 1,
            "title": "Copy & paste",
            "steps": [
                {"action": "goto", "path": "/projects/overview", "say": "Here is the overview.",
                 "settle_ms": 500},
                {"action": "wait_for", "locator": {"text": "Total A1-A3"}},
                {"action": "click", "locator": {"role": "button", "name": "Copy"},
                 "say": "Copy the whole sub-tree.", "min_duration_sec": 3.0},
                {"action": "dwell", "min_duration_sec": 2.0, "pacing": "fixed"},
            ],
        }
    ],
}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "test-app.yaml").write_text(
        "name: test-app\nbase_url: http://localhost:1\ndefault_locale: en\n", encoding="utf-8"
    )
    import yaml

    flows = profiles / "test-app" / "flows"
    flows.mkdir(parents=True)
    (flows / "task-1-generated.demoflow.yaml").write_text(yaml.safe_dump(_FLOW), encoding="utf-8")
    (flows / "broken.demoflow.yaml").write_text("name: broken\nbeats: nope\n", encoding="utf-8")

    cfg = AppConfig(workspace_root=str(tmp_path))
    return TestClient(create_app(cfg, Secrets(), Database(tmp_path / "server.db")))


def test_meta_reports_the_configured_engines(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    assert meta["claude_mode"] in ("agent_sdk", "api_key")
    assert meta["tts_provider"] in ("kokoro", "elevenlabs")


def test_projects_carry_what_the_cards_render(client: TestClient) -> None:
    p = client.get("/api/projects").json()[0]
    assert p["name"] == "test-app"
    assert p["locale"] == "en"
    assert p["tracker"] is False and p["tracker_label"] is None
    assert p["flows"] == 2


def test_flow_list_is_rich_enough_for_the_cards(client: TestClient) -> None:
    flows = {f["name"]: f for f in client.get("/api/projects/test-app/flows").json()}

    good = flows["task-1-generated"]
    assert good["beats"] == 1
    assert good["steps"] == 4
    assert good["narrated"] == 2
    assert good["words"] == 8
    assert good["version"] == 3
    assert good["viewport"] == "1280x720"
    assert good["tasks"] == [1]
    # The preview is what the beat chips show, in order.
    assert [s["action"] for s in good["step_preview"]][:2] == ["goto", "wait_for"]
    assert good["step_preview"][0]["summary"] == "/projects/overview"
    assert good["step_preview"][2]["summary"] == 'role=button "Copy"'


def test_a_broken_flow_reports_why_instead_of_vanishing(client: TestClient) -> None:
    flows = {f["name"]: f for f in client.get("/api/projects/test-app/flows").json()}
    assert "error" in flows["broken"]
    assert "steps" not in flows["broken"]


def test_estimate_sums_narration_and_bookends(client: TestClient) -> None:
    est = client.get(
        "/api/projects/test-app/estimate", params={"flows": ["task-1-generated"]}
    ).json()
    assert est["steps"] == 4
    assert est["narrated_steps"] == 2
    assert est["words"] == 8
    assert est["bookend_sec"] == 8.0
    assert est["flows"][0]["beats"] == 1
    # 4 words at 2.3 w/s = 1.74s, +0.5s settle; the next 4 words -> 1.74s but the
    # 3.0s floor wins; the fixed-pacing dwell contributes its 2.0s. Plus bookends.
    assert est["seconds"] == pytest.approx(2.24 + 3.0 + 2.0 + 8.0, abs=0.05)


def test_estimate_can_drop_the_bookends(client: TestClient) -> None:
    est = client.get(
        "/api/projects/test-app/estimate",
        params={"flows": ["task-1-generated"], "bookends": False},
    ).json()
    assert est["bookend_sec"] == 0.0
    assert est["seconds"] == pytest.approx(7.24, abs=0.05)


def test_estimate_needs_at_least_one_flow(client: TestClient) -> None:
    assert client.get("/api/projects/test-app/estimate").status_code == 422


def test_estimate_rejects_an_unknown_flow(client: TestClient) -> None:
    res = client.get("/api/projects/test-app/estimate", params={"flows": ["ghost"]})
    assert res.status_code == 404


def test_estimate_surfaces_an_invalid_flow(client: TestClient) -> None:
    res = client.get("/api/projects/test-app/estimate", params={"flows": ["broken"]})
    assert res.status_code == 422


def test_say_preview_rejects_empty_and_overlong_text(client: TestClient) -> None:
    base = "/api/projects/test-app/say/preview"
    assert client.post(base, json={"text": "   "}).status_code == 422
    assert client.post(base, json={"text": "x" * 801}).status_code == 422


def test_regenerate_say_validates_the_submitted_flow(client: TestClient) -> None:
    res = client.post(
        "/api/projects/test-app/flows/task-1-generated/regenerate-say",
        json={"flow": {"name": "x", "project": "p", "beats": "nope"}},
    )
    assert res.status_code == 422
