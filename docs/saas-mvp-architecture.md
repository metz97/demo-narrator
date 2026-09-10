# demo-narrator Web — Architecture & MVP Scope

**Status:** v1 · 2026-07-13 — **M0 + M1 + M2 + M3 built.**
M3: project **health strip** (app reachability + build-error detection + session
age), **share pages** per render (`#/r/<id>`: copy-link, downloads, remove from
library), **profile editor** with schema validation (comments preserved), job
**retry/cancel**, error toasts, and `deploy/` (Dockerfile, docker-compose,
systemd unit, deploy README — authored but not yet exercised on a Linux host).
M3's real acceptance — a team sprint review with zero CLI interventions —
requires actual team usage and remains open by definition.
M0: engine server-ready (DecisionBackend with api_key mode, REST Azure DevOps tracker,
progress/log capture, workspace_root; acceptance via `scripts/server_smoke.py`).
M1: `demo-narrator serve` — FastAPI + SQLite job worker + web UI (projects, live
sprint/task picker, generate jobs with log tail, render, library with player).
M2: the **flow editor** — step cards with inline narration/locator editing,
add/reorder/delete, per-beat guidance (persisted to hints), *Test this beat*
(preview clip inline via a `test_beat` job on the editor's unsaved state),
*Regenerate beat* with an accept/dismiss diff, version history with restore
(snapshots in SQLite on every save), read-only YAML view. Acceptance verified
live end-to-end through the UI (edit → save v1/v2 → test-beat preview).
Implementation deviations from this doc: stdlib `sqlite3` instead of SQLAlchemy;
static vanilla-JS UI throughout (the editor proved buildable without React —
revisit only if interaction complexity grows); reorder via ↑/↓ instead of drag.
Next: M3 (polish, sharing, deployment scripts, team adoption).
**Decision:** build the **single-tenant web version first** — a web UI + job server wrapped
around the existing engine, deployed inside the network where staging apps are reachable.
Multi-tenant SaaS concerns (billing, hybrid runners, tenant isolation, SOC 2) are explicitly
deferred; see *Non-goals* and *Path to multi-tenant*.

---

## 1. Purpose & positioning

Turn the proven CLI pipeline —

> pick tasks from the tracker → Claude walks the app → reviewable flow → narrated,
> captioned, chaptered demo video

— into a web product a whole team can use without touching a terminal. The single-tenant
MVP is both a useful internal tool immediately **and** the chassis for the future SaaS:
~80% of the product (UI, job model, flow editor, connectors) carries over unchanged, while
the two genuinely hard SaaS problems (reaching private customer apps, multi-tenant secrets)
are dodged by running inside the network.

**Core product bet:** raw generation is right ~60% of the time (validated: task 90396
generated cleanly; 92191 needed a hint + trim; 92163 needed hand-authoring). Therefore the
product is not "magic button" — it is **generate → review in a visual flow editor → record**.
The editor is the product; generation is the accelerant.

---

## 2. MVP user journey (screens)

```
 ┌──────────┐   ┌──────────────┐   ┌───────────────┐   ┌──────────────┐   ┌─────────┐
 │ Projects │ → │ Sprint & task │ → │  Generation    │ → │ Flow editor  │ → │ Render  │
 │ (profile)│   │    picker     │   │  progress      │   │ (review/fix) │   │ & share │
 └──────────┘   └──────────────┘   └───────────────┘   └──────────────┘   └─────────┘
```

1. **Projects** — list of configured target apps (one per profile YAML). Shows connection
   health: app reachable, session valid, tracker connected. Buttons: *Refresh session*,
   *Edit profile* (raw YAML editor in MVP).
2. **Sprint & task picker** — choose project → provider lists sprints → sprint lists work
   items (title, state, type). User multi-selects the demoable tasks. Per-task optional
   **Guidance** text box (this is the hints file, surfaced as a first-class feature).
3. **Generation progress** — one job per task; live log tail ("turn 4: clicked Actions…"),
   per-task status chips (generating / needs review / failed). Failures are normal and
   land in the editor, not a dead end.
4. **Flow editor** — the heart of the product. See §8.
5. **Render & share** — pick flows + order, sprint label, toggles (captions, bookends,
   voice), render job → video page with player, download, SRT/script.md, and per-beat
   timings. Library lists all rendered videos.

---

## 3. System architecture

```
┌─────────────────────────────── VM / container host (inside network) ───────────────────────────────┐
│                                                                                                     │
│  ┌──────────────┐     REST/JSON      ┌──────────────┐     job rows      ┌────────────────────────┐  │
│  │  Frontend    │ ◄───────────────► │   API server │ ◄───────────────► │   Worker (1 process)   │  │
│  │  React SPA   │    (poll jobs)     │   FastAPI    │    (Postgres/     │  - generate_flow        │  │
│  │  (Vite)      │                    │              │     SQLite WAL)   │  - record_demo          │  │
│  └──────────────┘                    └──────┬───────┘                   │  - test_beat            │  │
│                                             │                          │  - refresh_session      │  │
│                                             │ files                    └───────────┬────────────┘  │
│                                      ┌──────▼───────────────────────────────┐      │               │
│                                      │  Workspace (filesystem = source of   │      │ drives        │
│                                      │  truth, same layout as the CLI):     │      ▼               │
│                                      │   profiles/<app>.yaml                │  Playwright          │
│                                      │   profiles/<app>/flows/*.yaml        │  (headless Chromium) │
│                                      │   profiles/<app>/hints/*.md          │  ffmpeg / Kokoro     │
│                                      │   output/demo-runs/<run>/…           │  (CPU)               │
│                                      └──────────────────────────────────────┘                      │
│                                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────────────────────┘
        │ HTTPS (OAuth / PAT)                          │ HTTP(S) — must be network-reachable
        ▼                                              ▼
  Azure DevOps (REST API)                     Target app (staging URL)
        + Anthropic API (Claude, api_key mode)
```

**Key decisions**

| Decision | Choice | Why |
|---|---|---|
| Backend | **FastAPI** (Python) | Engine is Python; import it directly, no RPC layer. |
| Frontend | **React + Vite SPA** | Flow editor needs real interactivity (reorder, inline edit, preview). Next.js unnecessary for internal single-tenant. |
| Job queue | **DB-backed job table + 1 worker process** | Single-tenant volume is a few videos/day. No Redis/Celery ops burden. Interface designed so a real queue can replace it later. |
| DB | **SQLite (WAL)**, SQLAlchemy | One API + one worker at this scale. Swap to Postgres when concurrency demands (same ORM). |
| Artifact storage | **Filesystem workspace**, same layout as the CLI | Web and CLI stay interoperable — a power user can still hand-edit a flow YAML or run `demo-narrator demo`. DB stores jobs/runs/indexes only, never the artifacts. |
| Claude | **`api_key` mode** (Anthropic API) | Subscription-bound Agent SDK auth is per-developer, not per-server. The APIKeyBackend already exists in `scriptgen/`. |
| Video serving | Authenticated FastAPI file responses from `output/` | No S3 needed single-tenant. |
| Concurrency | **One recording job at a time** per worker | Playwright-sync + asyncio interplay (already handled with worker threads) and CPU-bound Kokoro/ffmpeg make serial execution the honest default. Scale = more worker processes later. |

---

## 4. Reuse map — existing engine → server role

| Existing module | Role in the web version | Change needed |
|---|---|---|
| `demo/models.py` (FlowSpec/Beat/Step/Locator) | The wire format between editor and runner. Serialize to JSON for the API (pydantic gives this for free). | None |
| `demo/profile.py` | Project config. | None |
| `demo/flow.py` (load/save/merge) | Flow persistence + combining tasks into one reel. | None |
| `demo/generate.py` (supervised loop, hints, DOM digest) | `generate_flow` job. | **Backend abstraction** — `_ask_claude` currently hardcodes the Agent SDK; route through the existing `ScriptModelBackend` protocol so the server uses `APIKeyBackend`. (~½ day, the biggest engine change.) |
| `demo/build.py` (per-beat record, cards, captions, assembly) | `record_demo` job. | Progress callback hook (currently logs only); split out a `render_beat`-only entry point for *test this beat*. |
| `demo/runner.py`, `demo/captions.py`, `demo/cards.py` | Unchanged workhorses. | None |
| `tts/` (Kokoro default, ElevenLabs premium, fake for tests) | Voice options in render settings. | None |
| `context.py` (Azure DevOps via az CLI) | Replace with **REST-based AzureDevOpsProvider** (az CLI needs interactive `az login`; a server wants a PAT + plain `httpx`). Same `WorkItemProvider` seam. | New ~150-line provider + `list_sprints()` method (new capability). |
| `config.py`, `errors.py`, `retry.py`, `glossary.py` | As-is. | Workspace-root setting instead of CWD-relative paths. |
| `cli.py` | Kept! CLI and web share the workspace. | None |
| Old screen-recording pipeline (`segmentation`, `pipeline`, …) | **Out of MVP scope** (still available via CLI). | None |

**Engine work items before UI work starts (M0):**
1. Route `demo/generate.py` through `ScriptModelBackend` (api_key support).
2. `AzureDevOpsProvider` on REST (PAT auth, `list_sprints`, `list_work_items(sprint)`, `get_work_item`).
3. Progress/log capture: per-job log handler + step-progress callback in `build_demo`/`generate_flow`.
4. `workspace_root` config so profiles/flows/output resolve from a configured directory.

---

## 5. Data model

Filesystem is the source of truth for **artifacts** (profiles, flows, hints, videos).
The DB holds **coordination and history**:

```
project        name (=profile file), tracker_kind, last_session_check, status
work_item      project, external_id, sprint, title, state, raw_json, fetched_at   (cache)
flow           id, project, name, file_path, task_ids[], updated_at
flow_version   flow_id, seq, yaml_snapshot, author, created_at                     (undo/history)
job            id, kind(generate|record|test_beat|refresh_session), status(queued|
               running|succeeded|failed|cancelled), params_json, log_path,
               created_at, started_at, finished_at, error
render         id, job_id, video_path, srt_path, script_path, duration_sec,
               flows[], sprint_label, options_json
secret         name → value (encrypted at rest: app creds, PAT, Anthropic key)     (or env-only in MVP)
```

Flow versioning = snapshot rows on every editor save (cheap, gives undo + audit).

---

## 6. API surface (REST)

```
# Projects / connections
GET    /api/projects                          list + health
GET    /api/projects/{p}                      profile (parsed + raw YAML)
PUT    /api/projects/{p}/profile              save YAML (validated by ProjectProfile)
POST   /api/projects/{p}/session/refresh      job: run login recipe headless
POST   /api/projects/{p}/session/upload       upload a storageState captured locally (MFA escape hatch)

# Tracker
GET    /api/projects/{p}/sprints
GET    /api/projects/{p}/sprints/{n}/work-items
GET    /api/projects/{p}/work-items/{id}

# Flows
POST   /api/projects/{p}/flows/generate       {task_ids[], guidance{task_id: text}} → job ids
GET    /api/flows/{id}                        JSON (FlowSpec) + metadata
PUT    /api/flows/{id}                        save (validates via pydantic; writes YAML + version row)
GET    /api/flows/{id}/versions               history
POST   /api/flows/{id}/test-beat              {beat_index} → job  (records beat only, no cards → preview clip)

# Renders
POST   /api/renders                           {project, flow_ids[], sprint, captions, bookends, provider} → job
GET    /api/renders                           library
GET    /api/renders/{id}                      metadata + timings
GET    /api/renders/{id}/video|srt|script     files (authenticated)

# Jobs
GET    /api/jobs/{id}                         status
GET    /api/jobs/{id}/log?offset=n            incremental log tail (UI polls at 1–2 s)
POST   /api/jobs/{id}/cancel
```

Polling (not SSE/websockets) for MVP — the worker appends to a log file per job; the UI
tails it. Boring and sufficient.

---

## 7. Job model

- Job kinds: `generate_flow` (per task, ~2–6 min), `record_demo` (per render, ~1–3 min per
  feature + cards), `test_beat` (~1 min), `refresh_session` (~30 s).
- Worker claims the oldest queued job (`UPDATE … WHERE status='queued' LIMIT 1` with WAL);
  crash recovery marks stale `running` jobs failed on worker start.
- Every job writes `output/jobs/<id>.log`; artifacts land in the normal run-dir layout.
- Serial execution is a **feature** at MVP scale (predictable resource use on one VM);
  the claim query is the only thing to change for parallel workers later.

---

## 8. The flow editor (product core)

The YAML review step, made visual. One screen per flow:

- **Step list** — each step a card: action icon, locator summary, narration text.
  Inline-edit narration (most common edit). Drag to reorder, delete, duplicate.
  Add-step form with the locator strategy picker (testid / role+name / label / text / css —
  css shows the same "brittle" warning the generator uses).
- **Beat header** — task id/title, link to the work item, the guidance/hint editor
  (persisted to `hints/task-<id>.md`, so regeneration uses it).
- **Test this beat** — records just this beat headless (no cards/captions) and shows the
  clip inline. This is the trust-building loop: fix a selector → test → see it work in
  ~1 min. (Wraps the existing per-beat renderer from `build.py`.)
- **Regenerate beat** — reruns `generate_flow` for that task with current guidance;
  presents old vs. new step list for accept/merge.
- **Validation** — live pydantic validation (the strict FlowSpec model) with errors pinned
  to the offending card, so a broken flow can't reach the renderer.

Explicit MVP simplifications: no live DOM element picker (paste selectors / rely on
generation), no timeline scrubber, raw YAML tab always available as the escape hatch.

---

## 9. Auth (four distinct concerns)

| Concern | MVP answer |
|---|---|
| **Into the web app** | Internal-network deployment + reverse-proxy SSO (or simple shared login). Roles/permissions deferred. |
| **Into the target app** | Profile `login_recipe` with a **dedicated no-MFA demo account** (creds in server env/secret store), session auto-refreshed by job when a run fails auth. MFA escape hatch: capture storageState locally with the CLI (`demo-auth --show-browser`) and upload via the API. |
| **Into Azure DevOps** | PAT (Work Items: Read) in server secrets; REST provider. OAuth app comes with multi-tenant. |
| **Anthropic** | `ANTHROPIC_API_KEY` in server secrets; `claude.mode: api_key`. Per-job token usage logged (this becomes metering later). |

Secrets in MVP: environment/`.env` on the VM (pattern already exists, never logged,
redacting `Secrets` object). A vault is a multi-tenant requirement, not an MVP one.

---

## 10. Deployment & infra

- **One VM / container** inside the network that can reach staging: 4–8 vCPU, 8–16 GB RAM
  (Kokoro + ffmpeg are CPU-bound; Chromium is RAM-hungry), ~50 GB disk for videos.
- Ubuntu image with: Python 3.12, `playwright install chromium --with-deps`, ffmpeg,
  espeak-ng. (Windows-specific paths in current config are already configurable.)
- Two processes (API, worker) under systemd or docker-compose; nightly backup of the
  workspace + SQLite file.
- No GPU. No Redis. No object store. That's the point of single-tenant first.

## 11. Cost model (order of magnitude)

| Item | Cost |
|---|---|
| Flow generation (Claude api_key, ~10–15 turns × ~8k input tokens, Opus-class) | **~$0.50–1.50 per task** |
| Narration polish / repair calls | ~$0.05–0.15 per task |
| TTS (Kokoro, CPU) | ~$0 (this is a real COGS advantage; ElevenLabs optional premium) |
| Recording + encoding | CPU minutes, negligible |
| VM | fixed ~$50–150/mo |

A 3-task sprint video ≈ **$2–5 marginal cost**. Pricing (later, SaaS) per-video or credit
packs comfortably clears this.

## 12. Non-goals for MVP (deferred to multi-tenant phase)

- Multi-tenancy, billing/Stripe, org/user management
- Hybrid runners for customer-private networks (the big SaaS engineering item)
- Jira / GitHub connectors (the `WorkItemProvider` seam is ready; Azure DevOps only in MVP)
- OAuth connector flows, secrets vault, SOC 2
- Visual DOM element picker, interactive-demo output formats, translations/multi-voice per beat
- The screen-recording (`generate`) pipeline in the UI — CLI-only remains

## 13. Milestones (one experienced dev, full-time)

| M | Deliverable | Acceptance | Est. |
|---|---|---|---|
| **M0** | Engine server-ready: api_key backend in generate loop, REST Azure provider (+list sprints), progress/log capture, workspace root. | `generate_flow` + `build_demo` run headless on a Linux VM against staging, driven by a Python script, no CLI. | ~1 wk |
| **M1** | API + worker + minimal UI: projects, sprint/task picker, generate jobs with live logs, render job, video page. *(No editor yet — flows render as-generated.)* | A teammate produces a sprint video from the browser with zero terminal use. | ~2 wk |
| **M2** | **Flow editor**: step cards, narration editing, reorder/delete/add, guidance box, test-this-beat, regenerate-beat, versioning. | The 92163-style failure (derailed generation) is fully recoverable in the UI: edit steps → test beat → render. | ~2–3 wk |
| **M3** | Polish: library & sharing links, session-refresh UX, profile editor with validation, error surfacing, deployment scripts, docs. | Team adopts it for a real sprint review; zero CLI interventions during that sprint. | ~1 wk |

**Total: ~6–7 weeks** to an internally adopted product. Multi-tenant work is scoped only
after M3 proves retention (teams keep using it sprint after sprint without prodding).

## 14. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Generation quality disappoints users on complex features (validated risk) | Editor-first framing; guidance box is prominent; "needs review" is a normal state, not an error. Track accept-without-edit rate as the KPI. |
| Staging data drift breaks saved flows (tes2 was emptied mid-project) | *Test this beat* before render; render report marks failed steps; encourage a stable, seeded demo dataset per project (document as a best practice). |
| Session expiry mid-run | Pre-flight session check before every job; auto `refresh_session` retry once. |
| Target app deploys/breaks (Vite-overlay incident) | Pre-flight "app health" probe (loads base URL, detects error overlays) with a clear, human-readable failure. |
| Anthropic API cost surprises | Per-job token logging from day one; per-project monthly budget alert. |
| Playwright/asyncio quirks server-side | Already solved in-engine (worker threads); serial worker avoids the rest. |
| IP/ownership (built around employer's app & tracker) | Resolve with Hubexo **before** external commercialization; the engine itself is app-agnostic. |

## 15. Open questions

1. Auth for the web app itself: reverse-proxy SSO available internally, or ship simple login?
2. Postgres from day one (if the team already runs it) vs SQLite?
3. Do we want per-beat voice/language options in MVP render settings, or keep one voice?
4. Where does the VM live (needs line-of-sight to staging + outbound HTTPS to Anthropic/Azure DevOps)?
