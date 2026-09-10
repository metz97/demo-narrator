"""SQLite store for jobs and the render library.

Deliberately stdlib ``sqlite3`` rather than an ORM (deviation from the plan doc,
noted there): two tables and a claim query don't justify the dependency. Every
call opens a short-lived connection, so the API thread pool and the worker
thread never share one — WAL mode keeps readers and the writer happy.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'queued',
  params      TEXT NOT NULL DEFAULT '{}',
  result      TEXT,
  error       TEXT,
  log_path    TEXT,
  created_at  TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS renders (
  id           TEXT PRIMARY KEY,
  job_id       TEXT,
  project      TEXT NOT NULL,
  title        TEXT NOT NULL,
  video_path   TEXT NOT NULL,
  srt_path     TEXT,
  script_path  TEXT,
  duration_sec REAL,
  chapters     TEXT,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flow_versions (
  project    TEXT NOT NULL,
  flow       TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  yaml       TEXT NOT NULL,
  note       TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (project, flow, seq)
);
CREATE TABLE IF NOT EXISTS onboarding (
  project    TEXT PRIMARY KEY,
  state      TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS render_shares (
  token          TEXT PRIMARY KEY,
  render_id      TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  expires_at     TEXT,
  password_hash  TEXT,
  allow_download INTEGER NOT NULL DEFAULT 1,
  revoked_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_render_shares_render ON render_shares (render_id);
CREATE TABLE IF NOT EXISTS share_views (
  token           TEXT NOT NULL,
  ts              TEXT NOT NULL,
  seconds_watched REAL NOT NULL DEFAULT 0,
  ip_hash         TEXT
);
CREATE INDEX IF NOT EXISTS idx_share_views_token ON share_views (token);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    status: str
    params: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    log_path: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Job":
        return Job(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            params=json.loads(row["params"] or "{}"),
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            log_path=row["log_path"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )


@dataclass(frozen=True)
class Render:
    id: str
    job_id: str | None
    project: str
    title: str
    video_path: str
    srt_path: str | None
    script_path: str | None
    duration_sec: float | None
    created_at: str
    chapters: list[dict[str, Any]]

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Render":
        return Render(
            id=row["id"],
            job_id=row["job_id"],
            project=row["project"],
            title=row["title"],
            video_path=row["video_path"],
            srt_path=row["srt_path"],
            script_path=row["script_path"],
            duration_sec=row["duration_sec"],
            created_at=row["created_at"],
            chapters=json.loads(row["chapters"]) if row["chapters"] else [],
        )


_PBKDF2_ROUNDS = 240_000


def hash_share_password(password: str) -> str:
    """Salted PBKDF2 — a share password gates a link, so it never round-trips."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_share_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
    )
    return secrets.compare_digest(candidate.hex(), digest_hex)


def hash_viewer_ip(ip: str | None) -> str | None:
    """View counts are per-visitor, but we never store the address itself."""
    if not ip:
        return None
    return hashlib.sha256(ip.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Share:
    token: str
    render_id: str
    created_at: str
    expires_at: str | None
    password_hash: str | None
    allow_download: bool
    revoked_at: str | None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Share":
        return Share(
            token=row["token"],
            render_id=row["render_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            password_hash=row["password_hash"],
            allow_download=bool(row["allow_download"]),
            revoked_at=row["revoked_at"],
        )

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            deadline = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline <= datetime.now(timezone.utc)

    @property
    def state(self) -> str:
        if self.revoked_at:
            return "revoked"
        return "expired" if self.expired else "active"

    @property
    def live(self) -> bool:
        return self.state == "active"


class Database:
    # Columns added after the first release. SQLite can only add one at a time,
    # and only if it is missing, so each is applied idempotently on open.
    _ADDED_COLUMNS = (("renders", "chapters", "TEXT"),)

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table, column, decl in self._ADDED_COLUMNS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # -- jobs -----------------------------------------------------------------

    def create_job(self, kind: str, params: dict[str, Any], log_path: str | None = None) -> Job:
        job_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, kind, params, log_path, created_at) VALUES (?,?,?,?,?)",
                (job_id, kind, json.dumps(params), log_path, _now()),
            )
        job = self.get_job(job_id)
        assert job is not None
        return job

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def claim_next_job(self) -> Job | None:
        """Atomically move the oldest queued job to running (single worker)."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?",
                (_now(), row["id"]),
            )
        return self.get_job(row["id"])

    def finish_job(
        self, job_id: str, *, result: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        status = "failed" if error else "succeeded"
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, result=?, error=?, finished_at=? WHERE id=?",
                (status, json.dumps(result) if result else None, error, _now(), job_id),
            )

    def set_job_log(self, job_id: str, log_path: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET log_path=? WHERE id=?", (log_path, job_id))

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a job that has not started yet (queued only)."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                (_now(), job_id),
            )
        return cur.rowcount > 0

    def fail_stale_running(self) -> int:
        """Crash recovery on worker start: running jobs can't have survived."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='failed', error='worker restarted mid-job', finished_at=? "
                "WHERE status='running'",
                (_now(),),
            )
        return cur.rowcount

    # -- flow versions -----------------------------------------------------------

    def add_flow_version(self, project: str, flow: str, yaml_text: str, note: str | None = None) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM flow_versions WHERE project=? AND flow=?",
                (project, flow),
            ).fetchone()
            seq = int(row["m"]) + 1
            conn.execute(
                "INSERT INTO flow_versions (project, flow, seq, yaml, note, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (project, flow, seq, yaml_text, note, _now()),
            )
        return seq

    def list_flow_versions(self, project: str, flow: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, note, created_at, LENGTH(yaml) AS size FROM flow_versions "
                "WHERE project=? AND flow=? ORDER BY seq DESC",
                (project, flow),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_flow_version(self, project: str, flow: str, seq: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT yaml FROM flow_versions WHERE project=? AND flow=? AND seq=?",
                (project, flow, seq),
            ).fetchone()
        return str(row["yaml"]) if row else None

    # -- onboarding (setup-assistant conversation) ------------------------------

    def get_onboarding(self, project: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM onboarding WHERE project=?", (project,)
            ).fetchone()
        if row is None:
            return None
        parsed: dict[str, Any] = json.loads(row["state"] or "{}")
        return parsed

    def save_onboarding(self, project: str, state: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO onboarding (project, state, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(project) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                (project, json.dumps(state), _now()),
            )

    def delete_onboarding(self, project: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM onboarding WHERE project=?", (project,))
        return cur.rowcount > 0

    # -- renders ----------------------------------------------------------------

    def add_render(
        self,
        *,
        job_id: str | None,
        project: str,
        title: str,
        video_path: str,
        srt_path: str | None,
        script_path: str | None,
        duration_sec: float | None,
        chapters: list[dict[str, Any]] | None = None,
    ) -> Render:
        render_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO renders (id, job_id, project, title, video_path, srt_path, "
                "script_path, duration_sec, chapters, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (render_id, job_id, project, title, video_path, srt_path, script_path,
                 duration_sec, json.dumps(chapters) if chapters else None, _now()),
            )
        render = self.get_render(render_id)
        assert render is not None
        return render

    def get_render(self, render_id: str) -> Render | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM renders WHERE id=?", (render_id,)).fetchone()
        return Render.from_row(row) if row else None

    def delete_render(self, render_id: str) -> bool:
        """Remove a render from the library (files stay in their run directory)."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM renders WHERE id=?", (render_id,))
        return cur.rowcount > 0

    def list_renders(self, limit: int = 100) -> list[Render]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM renders ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Render.from_row(r) for r in rows]

    # -- share links --------------------------------------------------------------

    def create_share(
        self,
        *,
        render_id: str,
        expires_at: str | None = None,
        password: str | None = None,
        allow_download: bool = True,
    ) -> Share:
        """Issue a fresh capability token. 22 url-safe chars ≈ 128 bits."""
        token = secrets.token_urlsafe(16)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO render_shares (token, render_id, created_at, expires_at, "
                "password_hash, allow_download) VALUES (?,?,?,?,?,?)",
                (
                    token,
                    render_id,
                    _now(),
                    expires_at,
                    hash_share_password(password) if password else None,
                    1 if allow_download else 0,
                ),
            )
        share = self.get_share(token)
        assert share is not None
        return share

    def get_share(self, token: str) -> Share | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM render_shares WHERE token=?", (token,)
            ).fetchone()
        return Share.from_row(row) if row else None

    def list_shares(self, render_id: str) -> list[Share]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM render_shares WHERE render_id=? ORDER BY rowid DESC",
                (render_id,),
            ).fetchall()
        return [Share.from_row(r) for r in rows]

    def revoke_share(self, token: str) -> bool:
        """Revoking is permanent and keeps the row, so a dead link stays explainable."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE render_shares SET revoked_at=? WHERE token=? AND revoked_at IS NULL",
                (_now(), token),
            )
        return cur.rowcount > 0

    def delete_shares_for_render(self, render_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM render_shares WHERE render_id=?", (render_id,))
            conn.execute(
                "DELETE FROM share_views WHERE token NOT IN (SELECT token FROM render_shares)"
            )
        return cur.rowcount

    def record_share_view(
        self,
        token: str,
        *,
        seconds_watched: float = 0.0,
        ip_hash: str | None = None,
        update_last: bool = False,
    ) -> None:
        """Record a view, or extend the watched time of the one already in flight.

        A view is one playback. `update_last` lets the page report how far the
        viewer actually got without counting them twice.
        """
        with self._connect() as conn:
            if update_last:
                cur = conn.execute(
                    "UPDATE share_views SET seconds_watched=? WHERE rowid = ("
                    "  SELECT rowid FROM share_views WHERE token=? AND ip_hash IS ?"
                    "  ORDER BY rowid DESC LIMIT 1) AND seconds_watched < ?",
                    (seconds_watched, token, ip_hash, seconds_watched),
                )
                if cur.rowcount:
                    return
            conn.execute(
                "INSERT INTO share_views (token, ts, seconds_watched, ip_hash) VALUES (?,?,?,?)",
                (token, _now(), seconds_watched, ip_hash),
            )

    def share_stats(self, token: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS views, COUNT(DISTINCT ip_hash) AS viewers, "
                "COALESCE(SUM(seconds_watched), 0) AS seconds, MAX(ts) AS last_viewed_at "
                "FROM share_views WHERE token=?",
                (token,),
            ).fetchone()
        return {
            "views": int(row["views"]),
            "viewers": int(row["viewers"]),
            "seconds_watched": float(row["seconds"]),
            "last_viewed_at": row["last_viewed_at"],
        }

    def share_summaries(self) -> dict[str, dict[str, Any]]:
        """One row per render for the library's share column — newest live link wins."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM share_views v WHERE v.token = s.token) "
                "AS views FROM render_shares s ORDER BY s.rowid ASC"
            ).fetchall()
        summaries: dict[str, dict[str, Any]] = {}
        for row in rows:
            share = Share.from_row(row)
            existing = summaries.get(share.render_id)
            # A live link always displaces a dead one; otherwise the newest wins.
            if existing and existing["state"] == "active" and not share.live:
                continue
            summaries[share.render_id] = {
                "token": share.token,
                "state": share.state,
                "expires_at": share.expires_at,
                "has_password": share.has_password,
                "allow_download": share.allow_download,
                "views": int(row["views"]),
            }
        return summaries
