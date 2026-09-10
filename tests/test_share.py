"""Share links: issuing, the public surface, and every way a link stops working.

The public routes are unauthenticated by design, so these tests care most about
the negative paths — revoked, expired, wrong password, deleted render.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from demo_narrator.config import AppConfig, Secrets  # noqa: E402
from demo_narrator.server.app import create_app  # noqa: E402
from demo_narrator.server.db import (  # noqa: E402
    Database,
    hash_share_password,
    verify_share_password,
)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "server.db")


@pytest.fixture()
def client(tmp_path: Path, db: Database) -> TestClient:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "test-app.yaml").write_text(
        "name: test-app\nbase_url: http://localhost:1\n", encoding="utf-8"
    )
    cfg = AppConfig(workspace_root=str(tmp_path))
    return TestClient(create_app(cfg, Secrets(), db))


@pytest.fixture()
def render(tmp_path: Path, db: Database):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00fake mp4")
    srt = tmp_path / "demo.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    return db.add_render(
        job_id=None,
        project="test-app",
        title="What's new in LCA",
        video_path=str(video),
        srt_path=str(srt),
        script_path=None,
        duration_sec=112.4,
    )


# -- password hashing ---------------------------------------------------------


def test_password_hash_roundtrip() -> None:
    stored = hash_share_password("hunter2")
    assert "hunter2" not in stored
    assert verify_share_password("hunter2", stored)
    assert not verify_share_password("hunter3", stored)


def test_password_hash_is_salted() -> None:
    assert hash_share_password("same") != hash_share_password("same")


def test_verify_rejects_garbage() -> None:
    assert not verify_share_password("x", "not-a-hash")
    assert not verify_share_password("x", "md5$1$aa$bb")


# -- issuing ------------------------------------------------------------------


def test_create_share_returns_a_usable_url(client: TestClient, render) -> None:
    res = client.post(f"/api/renders/{render.id}/share", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "active"
    assert body["views"] == 0
    assert body["allow_download"] is True
    assert body["has_password"] is False
    assert body["url"].endswith(f"/s/{body['token']}")
    assert len(body["token"]) >= 20


def test_share_of_unknown_render_is_404(client: TestClient) -> None:
    assert client.post("/api/renders/nope/share", json={}).status_code == 404


def test_cannot_share_a_render_whose_video_is_gone(
    client: TestClient, db: Database, tmp_path: Path
) -> None:
    r = db.add_render(
        job_id=None, project="test-app", title="ghost",
        video_path=str(tmp_path / "missing.mp4"), srt_path=None,
        script_path=None, duration_sec=None,
    )
    res = client.post(f"/api/renders/{r.id}/share", json={})
    assert res.status_code == 409


def test_expiry_days_must_be_positive(client: TestClient, render) -> None:
    res = client.post(f"/api/renders/{render.id}/share", json={"expires_in_days": 0})
    assert res.status_code == 422


# -- the public surface -------------------------------------------------------


def test_public_meta_and_video(client: TestClient, render) -> None:
    token = client.post(f"/api/renders/{render.id}/share", json={}).json()["token"]

    meta = client.get(f"/s/{token}/meta").json()
    assert meta["unlocked"] is True
    assert meta["needs_password"] is False
    assert meta["title"] == "What's new in LCA"
    assert meta["has_srt"] is True

    assert client.get(f"/s/{token}/video").status_code == 200
    assert client.get(f"/s/{token}/srt").status_code == 200

    dl = client.get(f"/s/{token}/download")
    assert dl.status_code == 200
    assert "attachment" in dl.headers["content-disposition"]


def test_unknown_token_is_404(client: TestClient) -> None:
    assert client.get("/s/nosuchtoken/meta").status_code == 404


def test_revoked_link_is_gone(client: TestClient, render) -> None:
    token = client.post(f"/api/renders/{render.id}/share", json={}).json()["token"]
    assert client.delete(f"/api/shares/{token}").status_code == 200
    for path in ("meta", "video", "srt", "download"):
        assert client.get(f"/s/{token}/{path}").status_code == 410


def test_expired_link_is_gone(client: TestClient, db: Database, render) -> None:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    share = db.create_share(render_id=render.id, expires_at=past)
    assert share.state == "expired"
    assert client.get(f"/s/{share.token}/meta").status_code == 410


def test_download_can_be_disabled(client: TestClient, render) -> None:
    token = client.post(
        f"/api/renders/{render.id}/share", json={"allow_download": False}
    ).json()["token"]
    assert client.get(f"/s/{token}/video").status_code == 200      # watching is fine
    assert client.get(f"/s/{token}/download").status_code == 403   # saving is not


def test_deleting_a_render_kills_its_links(client: TestClient, render) -> None:
    token = client.post(f"/api/renders/{render.id}/share", json={}).json()["token"]
    res = client.delete(f"/api/renders/{render.id}")
    assert res.status_code == 200 and res.json()["shares_removed"] == 1
    assert client.get(f"/s/{token}/meta").status_code == 404


# -- the password gate --------------------------------------------------------


def test_password_gate_blocks_until_unlocked(client: TestClient, render) -> None:
    token = client.post(
        f"/api/renders/{render.id}/share", json={"password": "sesame"}
    ).json()["token"]

    meta = client.get(f"/s/{token}/meta").json()
    assert meta["needs_password"] is True and meta["unlocked"] is False
    # Nothing about the video leaks before unlocking.
    assert "title" not in meta
    assert client.get(f"/s/{token}/video").status_code == 401

    assert client.post(f"/s/{token}/unlock", json={"password": "wrong"}).status_code == 401
    assert client.get(f"/s/{token}/video").status_code == 401

    assert client.post(f"/s/{token}/unlock", json={"password": "sesame"}).status_code == 200
    # The cookie the client kept now opens the whole surface.
    assert client.get(f"/s/{token}/meta").json()["unlocked"] is True
    assert client.get(f"/s/{token}/video").status_code == 200


def test_whitespace_only_password_means_no_password(client: TestClient, render) -> None:
    body = client.post(f"/api/renders/{render.id}/share", json={"password": "   "}).json()
    assert body["has_password"] is False


# -- view counting ------------------------------------------------------------


def test_a_view_is_recorded_on_play(client: TestClient, render) -> None:
    token = client.post(f"/api/renders/{render.id}/share", json={}).json()["token"]
    # Fetching the page or the video is not a view.
    client.get(f"/s/{token}/meta")
    client.get(f"/s/{token}/video")
    assert client.get(f"/api/renders/{render.id}/shares").json()[0]["views"] == 0

    client.post(f"/s/{token}/view", json={"seconds_watched": 0})
    assert client.get(f"/api/renders/{render.id}/shares").json()[0]["views"] == 1


def test_progress_updates_do_not_inflate_the_count(client: TestClient, render) -> None:
    token = client.post(f"/api/renders/{render.id}/share", json={}).json()["token"]
    client.post(f"/s/{token}/view", json={"seconds_watched": 0})
    client.post(f"/s/{token}/view", json={"seconds_watched": 42.5, "update_last": True})

    share = client.get(f"/api/renders/{render.id}/shares").json()[0]
    assert share["views"] == 1
    assert share["seconds_watched"] == pytest.approx(42.5)


def test_a_second_play_is_a_second_view(client: TestClient, render) -> None:
    token = client.post(f"/api/renders/{render.id}/share", json={}).json()["token"]
    client.post(f"/s/{token}/view", json={"seconds_watched": 0})
    client.post(f"/s/{token}/view", json={"seconds_watched": 0})
    assert client.get(f"/api/renders/{render.id}/shares").json()[0]["views"] == 2


def test_views_need_the_password_too(client: TestClient, render) -> None:
    token = client.post(
        f"/api/renders/{render.id}/share", json={"password": "sesame"}
    ).json()["token"]
    assert client.post(f"/s/{token}/view", json={"seconds_watched": 0}).status_code == 401


# -- library integration ------------------------------------------------------


def test_library_row_carries_share_state(client: TestClient, render) -> None:
    assert client.get("/api/renders").json()[0]["share"] is None
    token = client.post(f"/api/renders/{render.id}/share", json={}).json()["token"]

    row = client.get("/api/renders").json()[0]
    assert row["share"]["state"] == "active" and row["share"]["token"] == token

    client.delete(f"/api/shares/{token}")
    assert client.get("/api/renders").json()[0]["share"]["state"] == "revoked"


def test_a_live_link_wins_over_a_dead_one(client: TestClient, db: Database, render) -> None:
    dead = db.create_share(render_id=render.id)
    db.revoke_share(dead.token)
    live = db.create_share(render_id=render.id)

    summary = client.get("/api/renders").json()[0]["share"]
    assert summary["token"] == live.token and summary["state"] == "active"
