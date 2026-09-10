"""Web server: SQLite job store lifecycle + API surface (no worker, no browser)."""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from demo_narrator.config import AppConfig, Secrets  # noqa: E402
from demo_narrator.server.app import create_app  # noqa: E402
from demo_narrator.server.db import Database  # noqa: E402


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "server.db")


def test_job_lifecycle(db: Database) -> None:
    a = db.create_job("generate", {"project": "x", "task_id": 1})
    b = db.create_job("render", {"project": "x", "flows": ["f"]})
    assert a.status == "queued"

    claimed = db.claim_next_job()
    assert claimed is not None and claimed.id == a.id and claimed.status == "running"

    db.finish_job(a.id, result={"ok": 1})
    done = db.get_job(a.id)
    assert done is not None and done.status == "succeeded" and done.result == {"ok": 1}

    claimed2 = db.claim_next_job()
    assert claimed2 is not None and claimed2.id == b.id
    db.finish_job(b.id, error="boom")
    failed = db.get_job(b.id)
    assert failed is not None and failed.status == "failed" and failed.error == "boom"
    assert db.claim_next_job() is None


def test_stale_running_recovery(db: Database) -> None:
    job = db.create_job("render", {})
    db.claim_next_job()
    assert db.fail_stale_running() == 1
    refreshed = db.get_job(job.id)
    assert refreshed is not None and refreshed.status == "failed"


@pytest.fixture()
def client(tmp_path: Path, db: Database) -> TestClient:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "test-app.yaml").write_text(
        "name: test-app\nbase_url: http://localhost:1\n", encoding="utf-8"
    )
    cfg = AppConfig(workspace_root=str(tmp_path))
    return TestClient(create_app(cfg, Secrets(), db))


def test_projects_listing(client: TestClient) -> None:
    projects = client.get("/api/projects").json()
    assert [p["name"] for p in projects] == ["test-app"]
    assert projects[0]["session_ok"] is False
    assert projects[0]["flows"] == 0


def test_generate_enqueues_jobs(client: TestClient, db: Database) -> None:
    resp = client.post(
        "/api/projects/test-app/generate",
        json={"tasks": [{"id": 90396, "guidance": "use REF02"}, {"id": 92163}]},
    )
    assert resp.status_code == 200
    ids = resp.json()["jobs"]
    assert len(ids) == 2
    job = db.get_job(ids[0])
    assert job is not None and job.kind == "generate" and job.params["guidance"] == "use REF02"


def test_render_enqueues_job(client: TestClient, db: Database) -> None:
    resp = client.post(
        "/api/renders",
        json={"project": "test-app", "flows": ["task-1-generated"], "sprint": 174},
    )
    job_id = resp.json()["job"]
    job = db.get_job(job_id)
    assert job is not None and job.kind == "render" and job.params["sprint"] == 174


def test_job_endpoints(client: TestClient, db: Database) -> None:
    job = db.create_job("session", {"project": "test-app"})
    assert client.get(f"/api/jobs/{job.id}").json()["status"] == "queued"
    assert client.get(f"/api/jobs/{job.id}/log").text == ""
    assert client.get("/api/jobs/nope").status_code == 404


def test_unknown_project_404(client: TestClient) -> None:
    assert client.get("/api/projects/ghost/flows").status_code == 404
    assert client.post("/api/renders", json={"project": "ghost", "flows": ["x"]}).status_code == 404


_FLOW = {
    "version": 1,
    "name": "task-1-generated",
    "title": "Feature one",
    "project": "test-app",
    "beats": [
        {"task": 1, "title": "Feature one",
         "steps": [{"action": "goto", "path": "/x", "say": "hello"},
                   {"action": "dwell", "say": "watch this", "min_duration_sec": 2.0}]}
    ],
}


@pytest.fixture()
def flow_client(client: TestClient, tmp_path: Path) -> TestClient:
    import yaml

    flows = tmp_path / "profiles" / "test-app" / "flows"
    flows.mkdir(parents=True)
    (flows / "task-1-generated.demoflow.yaml").write_text(yaml.safe_dump(_FLOW), encoding="utf-8")
    return client


def test_flow_get_and_save_with_versioning(flow_client: TestClient) -> None:
    got = flow_client.get("/api/projects/test-app/flows/task-1-generated").json()
    assert got["flow"]["title"] == "Feature one"
    assert "yaml" in got

    edited = dict(got["flow"])
    edited["beats"][0]["steps"][1]["say"] = "watch this closely"
    resp = flow_client.put(
        "/api/projects/test-app/flows/task-1-generated",
        json={"flow": edited, "note": "tighten narration"},
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 1

    versions = flow_client.get("/api/projects/test-app/flows/task-1-generated/versions").json()
    assert versions[0]["seq"] == 1 and versions[0]["note"] == "tighten narration"
    v1 = flow_client.get("/api/projects/test-app/flows/task-1-generated/versions/1").json()
    assert "watch this closely" in v1["yaml"]

    # invalid flow is rejected with detail, file untouched
    bad = {"flow": {"name": "x", "project": "p", "beats": [{"steps": [{"action": "click"}]}]}}
    assert flow_client.put("/api/projects/test-app/flows/task-1-generated", json=bad).status_code == 422


def test_test_beat_enqueues_and_validates(flow_client: TestClient, db: Database) -> None:
    ok = flow_client.post(
        "/api/projects/test-app/flows/task-1-generated/test-beat",
        json={"flow": _FLOW, "beat_index": 0},
    )
    job = db.get_job(ok.json()["job"])
    assert job is not None and job.kind == "test_beat" and job.params["beat_index"] == 0

    bad = flow_client.post(
        "/api/projects/test-app/flows/task-1-generated/test-beat",
        json={"flow": {"name": "x"}, "beat_index": 0},
    )
    assert bad.status_code == 422


def test_hints_roundtrip(client: TestClient) -> None:
    assert client.get("/api/projects/test-app/hints/42").json()["text"] == ""
    client.put("/api/projects/test-app/hints/42", json={"text": "use REF02"})
    assert client.get("/api/projects/test-app/hints/42").json()["text"] == "use REF02"


def test_interactive_session_enqueues(client: TestClient, db: Database) -> None:
    resp = client.post("/api/projects/test-app/session/interactive?timeout_sec=120")
    job = db.get_job(resp.json()["job"])
    assert job is not None
    assert job.kind == "session_interactive"
    assert job.params["timeout_sec"] == 120


def test_job_video_404_when_absent(client: TestClient, db: Database) -> None:
    job = db.create_job("test_beat", {})
    assert client.get(f"/api/jobs/{job.id}/video").status_code == 404


def test_health_reports_unreachable_app(client: TestClient) -> None:
    h = client.get("/api/projects/test-app/health").json()
    assert h["app_reachable"] is False   # profile points at localhost:1
    assert h["session"] is False


def test_profile_roundtrip_and_validation(client: TestClient) -> None:
    got = client.get("/api/projects/test-app/profile").json()
    assert "base_url" in got["yaml"]

    ok = client.put(
        "/api/projects/test-app/profile",
        json={"yaml": "name: test-app\nbase_url: http://localhost:2\n# a comment\n"},
    )
    assert ok.status_code == 200
    assert "# a comment" in client.get("/api/projects/test-app/profile").json()["yaml"]

    bad = client.put(
        "/api/projects/test-app/profile",
        json={"yaml": "name: test-app\nbase_url: http://x\nbogus_key: 1\n"},
    )
    assert bad.status_code == 422


def test_create_project_scaffolds_valid_profile(client: TestClient) -> None:
    resp = client.post(
        "/api/projects",
        json={"name": "New-App", "base_url": "http://localhost:9",
              "context": "A tool for X.\nUsers do Y."},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "new-app"  # normalised to lowercase
    # it appears in the listing and its scaffold loads + keeps the context
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "new-app" in names
    got = client.get("/api/projects/new-app/profile").json()["yaml"]
    assert "http://localhost:9" in got
    assert "A tool for X." in got


def test_create_project_rejects_bad_input(client: TestClient) -> None:
    # duplicate
    assert client.post(
        "/api/projects", json={"name": "test-app", "base_url": "http://x"}
    ).status_code in (409, 422)
    # bad name
    assert client.post(
        "/api/projects", json={"name": "Bad Name!", "base_url": "http://x"}
    ).status_code == 422
    # non-http url
    assert client.post(
        "/api/projects", json={"name": "okname", "base_url": "ftp://x"}
    ).status_code == 422


def test_onboard_message_enqueues_job(client: TestClient, db: Database) -> None:
    jid = client.post("/api/projects/test-app/onboard/message", json={"text": "hi"}).json()["job"]
    job = db.get_job(jid)
    assert job is not None and job.kind == "onboard" and job.params["message"] == "hi"


def test_onboard_get_apply_reset(client: TestClient, db: Database) -> None:
    got = client.get("/api/projects/test-app/onboard").json()
    assert got["base_url"].startswith("http") and got["messages"] == []
    # seed a draft, then apply it onto the profile
    db.save_onboarding("test-app", {
        "messages": [{"role": "assistant", "text": "hi"}],
        "draft": {"context": "An app.", "feature_map": {"search": "/search"},
                  "sign_in_path": "/login", "ready": True},
        "pending_look_at": [],
    })
    assert client.post("/api/projects/test-app/onboard/apply").status_code == 200
    prof_yaml = client.get("/api/projects/test-app/profile").json()["yaml"]
    assert "An app." in prof_yaml and "/search" in prof_yaml and "/login" in prof_yaml
    # reset clears the conversation
    client.post("/api/projects/test-app/onboard/reset")
    assert client.get("/api/projects/test-app/onboard").json()["messages"] == []


def test_retry_and_cancel(client: TestClient, db: Database) -> None:
    job = db.create_job("render", {"project": "test-app", "flows": ["f"]})
    # queued -> cancellable, not retriable
    assert client.post(f"/api/jobs/{job.id}/retry").status_code == 409
    assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 200
    refreshed = db.get_job(job.id)
    assert refreshed is not None and refreshed.status == "cancelled"
    # cancelled -> retriable with identical params
    new_id = client.post(f"/api/jobs/{job.id}/retry").json()["job"]
    new = db.get_job(new_id)
    assert new is not None and new.params == job.params and new.status == "queued"
    # running jobs cannot be cancelled
    db.claim_next_job()
    assert client.post(f"/api/jobs/{new_id}/cancel").status_code == 409


def test_render_detail_and_delete(client: TestClient, db: Database, tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 2048)
    r = db.add_render(
        job_id=None, project="test-app", title="T", video_path=str(video),
        srt_path=None, script_path=None, duration_sec=12.0,
    )
    detail = client.get(f"/api/renders/{r.id}").json()
    assert detail["video_exists"] is True and detail["duration_sec"] == 12.0
    assert client.delete(f"/api/renders/{r.id}").status_code == 200
    assert client.get(f"/api/renders/{r.id}").status_code == 404
    assert video.exists()  # library removal never deletes files
