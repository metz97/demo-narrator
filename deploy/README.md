# Deploying the demo-narrator web server

Single-tenant deployment on a VM **inside the network** that can reach the
target app's staging URL, with outbound HTTPS to Azure DevOps and the
Anthropic API. See `docs/saas-mvp-architecture.md` for the full picture.

> **Honesty note:** these artifacts were authored on a Windows dev machine and
> have not been exercised against a real Linux host/Docker daemon yet. Expect a
> shakedown run (they are deliberately boring, but budget an hour).

## Checklist (either path)

1. `config/config.yaml` — set `claude.mode: api_key` (servers should not use a
   personal Claude subscription), ffmpeg paths can be left default on Linux
   (`ffmpeg`/`ffprobe` on PATH).
2. `.env` — `ANTHROPIC_API_KEY`, `AZURE_DEVOPS_EXT_PAT` (Work Items: Read),
   `E2E_CORE_USERNAME`/`E2E_CORE_PASSWORD` (no-MFA demo account).
3. Profiles under `profiles/` — `base_url` must be reachable **from the VM**
   (a developer's `localhost:4200` is not; use staging).
4. First session: `POST /api/projects/<name>/session/refresh` from the UI
   (runs the login recipe headless), or capture locally with
   `demo-narrator demo-auth --show-browser` and copy the storageState file up.

## Option A — Docker

```bash
docker compose -f deploy/docker-compose.yml up -d --build
# UI on http://<vm>:8765
```

The whole repo directory is mounted as the workspace, so profiles/flows/videos
are plain files on the host and the CLI remains usable alongside the server.

## Option B — bare VM + systemd

```bash
sudo apt install ffmpeg espeak-ng python3.12-venv
git clone <repo> /opt/demo-narrator && cd /opt/demo-narrator
python3 -m venv .venv
.venv/bin/pip install ".[server,demo,kokoro]"
.venv/bin/playwright install --with-deps chromium
cp deploy/demo-narrator.service /etc/systemd/system/
sudo systemctl enable --now demo-narrator
```

## Sizing & ops

- 4–8 vCPU, 8–16 GB RAM, ~50 GB disk (videos + ~330 MB Kokoro weights +
  Chromium). No GPU.
- Jobs run serially by design; one render ≈ 1–3 min per feature.
- Back up nightly: the workspace directory + `output/server/server.db`.
- Job logs: `output/jobs/<id>.log`. Server DB: `output/server/server.db`.
- No built-in auth: keep it on the internal network / behind a reverse proxy
  with SSO. Do not expose it to the internet as-is.
