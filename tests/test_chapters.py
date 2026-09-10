"""Chapters: recorded during assembly, stored on the render, served to viewers.

The timeline maths is the part worth pinning down — a chapter opens on its
section card so a viewer jumping to it lands on the title, not mid-action.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo_narrator.demo.build import _beat_summary
from demo_narrator.demo.models import Beat, Step
from demo_narrator.server.db import Database

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from demo_narrator.config import AppConfig, Secrets  # noqa: E402
from demo_narrator.server.app import create_app  # noqa: E402


# -- the blurb ----------------------------------------------------------------


def test_summary_is_the_first_narrated_line() -> None:
    beat = Beat(
        task=1, title="Analytics",
        steps=[
            Step(action="goto", path="/x"),
            Step(action="dwell", say="  A pie   chart breaks the total down.  "),
            Step(action="dwell", say="Ignored, the first one wins."),
        ],
    )
    assert _beat_summary(beat) == "A pie chart breaks the total down."


def test_summary_is_empty_when_nothing_is_narrated() -> None:
    beat = Beat(task=1, title="Silent", steps=[Step(action="dwell"), Step(action="goto", path="/x")])
    assert _beat_summary(beat) == ""


def test_summary_is_truncated_on_a_word_boundary() -> None:
    beat = Beat(task=1, title="Long", steps=[Step(action="dwell", say="word " * 60)])
    out = _beat_summary(beat)
    assert len(out) <= 180 and out.endswith("…")


# -- storage ------------------------------------------------------------------


CHAPTERS = [
    {"index": 0, "title": "Copy & paste", "task": 92133,
     "start_sec": 6.0, "end_sec": 64.2, "summary": "Copy a whole sub-tree."},
    {"index": 1, "title": "Analytics", "task": 92163,
     "start_sec": 64.2, "end_sec": 110.0, "summary": "Pie and bar breakdowns."},
]


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "server.db")


def _add(db: Database, tmp_path: Path, chapters=None):
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00mp4")
    return db.add_render(
        job_id=None, project="test-app", title="Sprint 178 demo",
        video_path=str(video), srt_path=None, script_path=None,
        duration_sec=110.0, chapters=chapters,
    )


def test_chapters_round_trip(db: Database, tmp_path: Path) -> None:
    render = _add(db, tmp_path, CHAPTERS)
    assert db.get_render(render.id).chapters == CHAPTERS


def test_a_render_without_chapters_reads_back_empty(db: Database, tmp_path: Path) -> None:
    render = _add(db, tmp_path, None)
    assert db.get_render(render.id).chapters == []


def test_the_column_is_added_to_an_existing_database(tmp_path: Path) -> None:
    """Databases created before chapters existed must keep working."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE renders (id TEXT PRIMARY KEY, job_id TEXT, project TEXT NOT NULL,"
        " title TEXT NOT NULL, video_path TEXT NOT NULL, srt_path TEXT, script_path TEXT,"
        " duration_sec REAL, created_at TEXT NOT NULL);"
        "INSERT INTO renders VALUES ('old1', NULL, 'p', 'Old render', '/v.mp4',"
        " NULL, NULL, 12.0, '2026-01-01T00:00:00+00:00');"
    )
    conn.commit()
    conn.close()

    db = Database(path)                       # opening applies the migration
    old = db.get_render("old1")
    assert old is not None and old.chapters == []
    # and the migration is idempotent
    assert Database(path).get_render("old1") is not None


# -- the API ------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, db: Database) -> TestClient:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "test-app.yaml").write_text(
        "name: test-app\nbase_url: http://localhost:1\n", encoding="utf-8"
    )
    return TestClient(create_app(AppConfig(workspace_root=str(tmp_path)), Secrets(), db))


def test_render_detail_serves_chapters(client: TestClient, db: Database, tmp_path: Path) -> None:
    render = _add(db, tmp_path, CHAPTERS)
    body = client.get(f"/api/renders/{render.id}").json()
    assert body["chapters"] == CHAPTERS


def test_library_row_counts_chapters(client: TestClient, db: Database, tmp_path: Path) -> None:
    _add(db, tmp_path, CHAPTERS)
    assert client.get("/api/renders").json()[0]["chapter_count"] == 2


def test_the_public_page_gets_chapters(client: TestClient, db: Database, tmp_path: Path) -> None:
    render = _add(db, tmp_path, CHAPTERS)
    token = client.post(f"/api/renders/{render.id}/share", json={}).json()["token"]
    meta = client.get(f"/s/{token}/meta").json()
    assert [c["title"] for c in meta["chapters"]] == ["Copy & paste", "Analytics"]
    assert meta["chapters"][0]["start_sec"] == 6.0


def test_a_locked_link_does_not_leak_chapters(
    client: TestClient, db: Database, tmp_path: Path
) -> None:
    render = _add(db, tmp_path, CHAPTERS)
    token = client.post(
        f"/api/renders/{render.id}/share", json={"password": "sesame"}
    ).json()["token"]
    meta = client.get(f"/s/{token}/meta").json()
    assert "chapters" not in meta          # chapter titles are content too

    client.post(f"/s/{token}/unlock", json={"password": "sesame"})
    assert len(client.get(f"/s/{token}/meta").json()["chapters"]) == 2


# -- assembly wiring ----------------------------------------------------------


def test_build_demo_records_a_chapter_per_beat(monkeypatch, tmp_path: Path) -> None:
    """Drive the real assembly loop with fake clips: cards 4s, beat clips 10s.

    This is the test that pins *where* a chapter opens — on its section card, so
    a viewer jumping to it lands on the title rather than mid-action.
    """
    from demo_narrator.demo import build as build_mod

    class _FakeProvider:
        name = "fake"
        is_paid = False

        def check(self):
            return []

        def synthesize(self, text, out_path):
            out_path.write_bytes(b"fake")
            return out_path

    class _FakeFFmpeg:
        def __init__(self, *a, **k):
            pass

        def media_duration(self, path):
            # cards are 4s, recorded beats are 10s
            return 4.0 if "card" in Path(path).name else 10.0

        def concat_reencode(self, segments, out, **kw):
            out.write_bytes(b"fake-mp4")
            return out

    def _fake_card(name, html, say, cards_dir, *a, **k):
        clip = Path(cards_dir) / f"{name}-card.mp4"
        clip.write_bytes(b"fake")
        return clip, 4.0

    def _fake_beat(profile, beat_flow, config, provider, ffmpeg, glossary, out_dir, *a, **k):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        clip = Path(out_dir) / "beat.mp4"
        clip.write_bytes(b"fake")
        return clip, [(0.0, "narration", 3.0)]

    monkeypatch.setattr(build_mod, "create_provider", lambda *a, **k: _FakeProvider())
    monkeypatch.setattr(build_mod, "FFmpeg", _FakeFFmpeg)
    monkeypatch.setattr(build_mod, "_resolve_storage_state", lambda profile: None)
    monkeypatch.setattr(build_mod, "load_glossary", lambda path: {})
    monkeypatch.setattr(build_mod, "_cost_guard", lambda *a, **k: None)
    monkeypatch.setattr(build_mod, "_card_clip", _fake_card)
    monkeypatch.setattr(build_mod, "_render_beat", _fake_beat)

    from demo_narrator.demo.models import FlowSpec
    from demo_narrator.demo.profile import ProjectProfile

    flow = FlowSpec(
        version=1, name="sprint-demo", project="test-app", title="Sprint demo",
        beats=[
            Beat(task=1, title="One", steps=[Step(action="dwell", say="First feature.")]),
            Beat(task=2, title="Two", steps=[Step(action="dwell", say="Second feature.")]),
        ],
    )
    profile = ProjectProfile(name="test-app", base_url="http://localhost:1")
    art = build_mod.build_demo(
        profile, flow, AppConfig(workspace_root=str(tmp_path)), Secrets(),
        output_dir=tmp_path, bookends=True, captions=False,
    )

    # intro 4s | section 4s + beat 10s | section 4s + beat 10s | outro
    assert [c["title"] for c in art.chapters] == ["One", "Two"]
    assert art.chapters[0]["start_sec"] == 4.0     # opens on its section card
    assert art.chapters[0]["end_sec"] == 18.0
    assert art.chapters[1]["start_sec"] == 18.0
    assert art.chapters[1]["end_sec"] == 32.0
    assert art.chapters[0]["task"] == 1
    assert art.chapters[1]["summary"] == "Second feature."
    # chapters tile the timeline with no gaps
    assert all(
        art.chapters[i]["end_sec"] == art.chapters[i + 1]["start_sec"]
        for i in range(len(art.chapters) - 1)
    )
    # and they are written next to the other run artifacts
    written = json.loads((art.run_dir / "chapters.json").read_text(encoding="utf-8"))
    assert written == art.chapters


def test_build_demo_without_bookends_opens_on_the_beat(monkeypatch, tmp_path: Path) -> None:
    from demo_narrator.demo import build as build_mod

    class _FakeFFmpeg:
        def __init__(self, *a, **k):
            pass

        def media_duration(self, path):
            return 10.0

        def concat_reencode(self, segments, out, **kw):
            out.write_bytes(b"fake-mp4")
            return out

    def _fake_beat(profile, beat_flow, config, provider, ffmpeg, glossary, out_dir, *a, **k):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        clip = Path(out_dir) / "beat.mp4"
        clip.write_bytes(b"fake")
        return clip, []

    class _FakeProvider:
        name = "fake"
        is_paid = False

        def check(self):
            return []

    monkeypatch.setattr(build_mod, "create_provider", lambda *a, **k: _FakeProvider())
    monkeypatch.setattr(build_mod, "FFmpeg", _FakeFFmpeg)
    monkeypatch.setattr(build_mod, "_resolve_storage_state", lambda profile: None)
    monkeypatch.setattr(build_mod, "load_glossary", lambda path: {})
    monkeypatch.setattr(build_mod, "_cost_guard", lambda *a, **k: None)
    monkeypatch.setattr(build_mod, "_render_beat", _fake_beat)

    from demo_narrator.demo.models import FlowSpec
    from demo_narrator.demo.profile import ProjectProfile

    flow = FlowSpec(
        version=1, name="d", project="test-app",
        beats=[Beat(task=1, title="Only", steps=[Step(action="dwell", say="Hi.")])],
    )
    art = build_mod.build_demo(
        ProjectProfile(name="test-app", base_url="http://localhost:1"), flow,
        AppConfig(workspace_root=str(tmp_path)), Secrets(),
        output_dir=tmp_path, bookends=False, captions=False,
    )
    assert art.chapters[0]["start_sec"] == 0.0
    assert art.chapters[0]["end_sec"] == 10.0


def test_artifacts_default_to_no_chapters() -> None:
    from demo_narrator.demo.build import DemoArtifacts

    art = DemoArtifacts(
        video=Path("v.mp4"), srt=Path("v.srt"), script_md=Path("v.md"), run_dir=Path(".")
    )
    assert art.chapters == []


def test_chapters_json_is_serialisable() -> None:
    assert json.loads(json.dumps(CHAPTERS)) == CHAPTERS
