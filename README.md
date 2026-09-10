# demo-narrator

Turn a raw screen recording of a web application into a final narrated demo
video — fully automatically. One command: scene detection, optional sprint
context from Azure DevOps, an AI-written script (Claude, vision), local or
cloud text-to-speech, and frame-accurate assembly with subtitles.

```
demo-narrator generate ./recording.mp4 --sprint 174
```

Produces:

- `output/recording-narrated.mp4` — H.264 + AAC, original resolution, faststart
- `output/recording-script.md` — the full narration script with timestamps
- `output/recording-narration.srt` — subtitles (free accessibility win)

## How it works

| Stage | What happens |
|---|---|
| 1. Segmentation | ffmpeg scene detection splits the recording into logical segments; segments shorter than `min_segment_sec` merge into a neighbour; 1–3 keyframes per segment are extracted as JPEG (long edge ≤ 1568 px). |
| 2. Context | Optional: completed work items for `--sprint N` / a single `--task N` via the Azure DevOps CLI, and/or a free-form `--context notes.md`. Without context, narration is vision-only (with a warning). |
| 3. Script | Claude sees every keyframe with its timestamps plus the context and the style guide, and returns strictly-validated JSON. Each segment has a hard word budget of `floor(duration × 2.3)`; segments >15 % over budget get one automatic repair pass. |
| 4. Voice | The script is synthesized segment-by-segment behind a pluggable `TTSProvider`: **kokoro** (default — free, local, CPU) or **elevenlabs** (paid). |
| 5. Assembly | Narration shorter than its segment leaves natural silence; narration longer than its segment extends the video with a freeze-frame of the last frame (audio is never stretched or sped up). 50 ms fades prevent clicks. Everything concatenates into the final MP4. |

Every run creates `output/runs/<timestamp>-<name>/` holding all intermediates
(segments.json, keyframes, context.md, script.json, per-segment WAVs, run.log),
so any stage can be re-run without repeating earlier ones.

## Prerequisites

- **Python 3.11+**
- **ffmpeg + ffprobe** (any recent version; 4.x+ recommended)
  - Windows: `winget install Gyan.FFmpeg` (restart the terminal afterwards)
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu: `sudo apt install ffmpeg` — Fedora: `sudo dnf install ffmpeg`
  - If the binaries aren't on PATH, point `ffmpeg.ffmpeg_path` / `ffmpeg.ffprobe_path`
    in `config/config.yaml` at them.
- **For the default script backend** (`claude.mode: agent_sdk`): the
  [Claude Code CLI](https://claude.com/claude-code) installed and logged in.
  Usage draws on your Claude subscription — no API key needed.
  *Alternative*: set `claude.mode: api_key` and export `ANTHROPIC_API_KEY`.
- **For the default voice** (`tts.provider: kokoro`):
  - `pip install "demo-narrator[kokoro]"` (pulls PyTorch — a large download)
  - the `espeak-ng` system package:
    `winget install eSpeak-NG.eSpeak-NG` / `brew install espeak-ng` / `apt install espeak-ng`
- **For `--sprint` / `--task`**: the Azure CLI with the DevOps extension
  (`az extension add --name azure-devops`) and `AZURE_DEVOPS_EXT_PAT` set.

## Setup

```bash
git clone <this repo> && cd demo-narrator
python -m venv .venv
.venv/Scripts/activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -e ".[kokoro]"        # or plain `pip install -e .` for ElevenLabs-only
cp config/config.example.yaml config/config.yaml
cp .env.example .env              # fill in only the secrets you need
demo-narrator config-check        # verifies everything before the first run
```

`config-check` validates the YAML, finds ffmpeg/ffprobe, verifies the selected
Claude backend and TTS provider are usable, and pings the relevant APIs. Fix
anything it flags before your first real run.

## Usage

```bash
# Fully automatic (context from sprint 174):
demo-narrator generate ./recording.mp4 --sprint 174

# Single work item + your own notes:
demo-narrator generate ./recording.mp4 --task 90396 --context notes.md

# Review (and optionally edit) the script before spending on TTS:
demo-narrator generate ./recording.mp4 --sprint 174 --review

# Resume a failed/interrupted run without repeating completed stages:
demo-narrator generate ./recording.mp4 --resume output/runs/20260707-183935-recording

# Re-run only voice + assembly, e.g. after editing script.json by hand,
# or to upgrade a Kokoro run to ElevenLabs without re-running vision analysis:
demo-narrator regen-audio output/runs/20260707-183935-recording --provider elevenlabs
```

Every command accepts `--config <file>` (default `config/config.yaml`) and
`--verbose` for debug output. Full option reference: `demo-narrator generate --help`.

## Demo mode (Playwright — generate the recording too)

Instead of narrating a screen recording you already made, demo mode **drives a
running instance of your web app** with Playwright, records it, and narrates the
result — so the on-screen actions and the voiceover are synchronised by
construction, with no manual editing. This is the recommended path for
repeatable per-sprint feature demos.

It is driven by two files (see [docs/playwright-demo-design.md](docs/playwright-demo-design.md)):

- a **project profile** (`profiles/<app>.yaml`) — the app's URL, auth, selector
  convention, and issue-tracker config;
- a **flow spec** (`profiles/<app>/flows/*.demoflow.yaml`) — the ordered browser
  steps, each with its narration line (`say:`).

```bash
pip install -e ".[demo,kokoro]"        # adds Playwright
python -m playwright install chromium  # one-time browser download
```

### Workflow

```bash
# 1. Capture an authenticated session once (uses E2E_* creds from .env, or
#    --show-browser to log in manually). Writes the storageState the profile points at.
demo-narrator demo-auth --project lca-tool

# 2. Author a flow for a work item — Claude walks the app (supervised) and writes a
#    reviewable *.demoflow.yaml. Optionally guide it with a hint file (see below).
demo-narrator demo-generate --project lca-tool --task 90396

# 3. Record the narrated video from that flow (review the YAML first):
demo-narrator demo --project lca-tool \
  --flow profiles/lca-tool/flows/task-90396-generated.demoflow.yaml
```

### One video for several tasks (e.g. a sprint demo)

`--sprint` does **not** auto-pick tasks (a sprint has many non-demoable items) —
you list the tasks you want. Author each task's flow, then pass **multiple
`--flow`** to stitch them into a single video with one intro and one outro:

```bash
demo-narrator demo-generate --project lca-tool --task 92191   # writes task-92191-generated.demoflow.yaml
demo-narrator demo-generate --project lca-tool --task 90396
demo-narrator demo-generate --project lca-tool --task 92163
# review/trim each flow, then combine into one narrated video:
demo-narrator demo --project lca-tool --sprint 174 \
  --flow profiles/lca-tool/flows/task-92191-generated.demoflow.yaml \
  --flow profiles/lca-tool/flows/task-90396-generated.demoflow.yaml \
  --flow profiles/lca-tool/flows/task-92163-generated.demoflow.yaml
```

The result opens with an **intro card** ("What's new in Sprint 174" + the feature
list); then, before each feature, a **section card** announces it ("Feature 1 of 3:
…"); each feature is recorded as its own segment and plays in order; and it closes
with an **outro card**. **TikTok-style karaoke captions** (a rolling 3-word window
with the spoken word highlighted) are burned in throughout — cards included.
Toggle with `--no-captions` / `--no-bookends`.

### Hints — teach the generator

Complex features are hard to generate blind. Drop guidance (prose or step-by-step)
in `profiles/<project>/hints/task-<id>.md` and `demo-generate` follows it as
authoritative instructions (or pass `--hint <file>`). Example:
[profiles/lca-tool/hints/task-92191.md](profiles/lca-tool/hints/task-92191.md).

### Web UI (single-tenant server)

> **New here? Follow the full step-by-step guide: [docs/SETUP.md](docs/SETUP.md)**
> (prerequisites → install → profile → first session → first video → troubleshooting).

```bash
pip install -e ".[server,demo,kokoro]"
demo-narrator serve                 # http://127.0.0.1:8765
```

The web UI wraps the same engine as a **four-step run** — `Work items →
Flows & beats → Narration → Render` — with **Jobs** and **Library** as
top-level destinations:

1. **Work items** — pick a sprint, tick the demoable tasks, set `max_steps` and
   `polish`. Per-task guidance is saved to `hints/task-<id>.md`, so it survives
   the run and any later regeneration.
2. **Flows & beats** — every generated flow shown as its beats and steps, with
   the semantic locator for each step, a validity check against the `FlowSpec`
   contract, and *Test first beat* for a ~20s preview clip.
3. **Narration** — one `say:` per step, with its `pacing`, `min_duration_sec`
   and `settle_ms`. Audition a line with **▶ Preview** (synthesised by the
   configured TTS provider), rewrite one with **Regenerate**, and watch the word
   budget against the style rule. Edits are saved as a new flow version.
4. **Render** — drag to order the flows, choose sprint label, provider, captions
   and bookends, and see a **runtime estimate** derived from the actual `say:`
   lines before you spend a render.

Assembly records a **chapter per beat** on the way through — the same cumulative
clock that positions subtitles — so every render knows where each feature starts
and ends. A chapter opens on its section card, so jumping to it lands on the
title rather than mid-action, and its blurb is the beat's first narrated line.
They are written to `chapters.json` in the run directory and stored with the
render; the render page and the share page both list them, click-to-seek.

**Review each flow in the visual editor** (edit steps and narration inline,
*Test this beat*, *Regenerate* with an accept/dismiss diff, version history with
restore) at any point from step 2.

Jobs, versions, share links, and the render index live in SQLite under
`output/server/`; profiles/flows/videos stay on the filesystem, so the CLI and
the web UI are interchangeable.
Claude auth follows `claude.mode` in config: `agent_sdk` (your Claude Code
subscription login) or `api_key` (`ANTHROPIC_API_KEY`) — see
[docs/saas-mvp-architecture.md](docs/saas-mvp-architecture.md).

Each project card shows a **health strip** (app reachable? build error? session
age? tracker configured?) so failures are diagnosed before a job runs. Failed
jobs have one-click **retry**. To deploy the server on a VM (Docker or systemd),
see [deploy/README.md](deploy/README.md).

#### Sharing a render

Every rendered video can be published as a **share link** — `/s/<token>`, a
standalone page with no app chrome and no sign-in, so a colleague just watches:

```
POST   /api/renders/{id}/share    {expires_in_days, password, allow_download}
GET    /api/renders/{id}/shares   every link issued for this render, with counts
DELETE /api/shares/{token}        revoke immediately and permanently
```

The page shows an **In this demo** contents list built from the render's
chapters, so a viewer can jump straight to the feature they care about.

A token is a **bearer capability**: anyone holding the URL can watch, so treat
it like a password. Each link can carry an expiry, a password (PBKDF2, salted,
never stored in the clear) and a download toggle. A **view is a playback**, not
a page load — nothing is counted until the video actually plays, and viewer IPs
are hashed rather than stored. Revoking a link, or deleting the render, kills
every route for that token straight away.

### Command reference

| Command | What it does |
|---|---|
| `serve` | Web UI + API + job worker (`--host`, `--port`, `--no-worker`). |
| `demo-auth --project P` | Capture/refresh the authenticated browser session (`--show-browser` for manual login). |
| `demo-generate --project P --task N` | Claude walks the app and writes a reviewable flow. `--hint`, `--max-steps`, `--no-polish`, `--run`. |
| `demo --project P --flow F [--flow G …]` | Record the narrated video. `--sprint N`, `--captions/--no-captions`, `--bookends/--no-bookends`, `--provider`, `--show-browser`, `--output`. |
| `generate <video>` | The other pipeline: narrate an existing screen recording. |
| `regen-audio <run-dir>` | Re-run voice + assembly only. |
| `config-check` | Validate config, ffmpeg, backends, API connectivity. |

Output (video + `-script.md` + `-narration.srt`) lands in
`output/demo-runs/<timestamp>-<flow>/`.

> **Auth**: the login step never stores your password — credentials are read from
> `.env` (`E2E_CORE_USERNAME`/`E2E_CORE_PASSWORD`, a no-MFA test account) or you
> log in yourself with `--show-browser`. The captured session is reused until it
> expires. See the profile's `auth:` block.

## Configuration reference

See [config/config.example.yaml](config/config.example.yaml) — every key is
documented inline with its default. Highlights:

| Key | Default | Meaning |
|---|---|---|
| `segmentation.scene_threshold` | `0.25` | Scene-cut sensitivity; lower catches subtler transitions. |
| `segmentation.min_segment_sec` | `4.0` | Shorter scenes merge into a neighbour. |
| `claude.mode` | `agent_sdk` | `agent_sdk` (subscription) or `api_key` (Anthropic API). |
| `tts.provider` | `kokoro` | `kokoro` (free/local) or `elevenlabs` (paid). |
| `tts.max_tts_characters` | `10000` | Hard cap for **paid** providers; run aborts above it. |
| `tts.kokoro.voice` | `af_heart` | Any voice from the Kokoro-82M voice list. |
| `assembly.fade_ms` | `50` | Audio fade at segment boundaries. |

Secrets go in the environment or `.env`, never in the YAML — see
[.env.example](.env.example). Secrets are never logged.

- **Style guide** (`config/style.md`): the narration rules sent to Claude —
  edit freely; deleting the file falls back to the embedded default.
- **Pronunciation glossary** (`config/glossary.json`): `term → phonetic`
  replacements applied to the text sent to TTS (whole-word, case-sensitive).
  Subtitles and script.md keep the original spelling.

### Kokoro model cache

On first use Kokoro downloads ~330 MB of weights into the standard Hugging Face
hub cache: `~/.cache/huggingface/hub` (Windows:
`%USERPROFILE%\.cache\huggingface\hub`); override with `HF_HOME`. CPU synthesis
runs at roughly real-time speed — per-segment progress is shown.

## Cost expectations

- **Stage 3 (Claude)** — the only stage that costs tokens. Before the call the
  CLI prints keyframe count and an input-token estimate. Rule of thumb: each
  keyframe is ~1,800 input tokens; a 5-minute recording with ~20 segments ≈
  40–60 keyframes ≈ 80–110 K input tokens plus a few K output. In `agent_sdk`
  mode this draws on your Claude subscription; in `api_key` mode it is billed
  to your Anthropic account.
- **Stage 4 (TTS)** — Kokoro is free (local CPU). ElevenLabs bills per
  character; the CLI prints the exact character count first and aborts above
  `tts.max_tts_characters` (default 10,000 ≈ US$1–3 depending on plan).
- Everything else (ffmpeg, Azure DevOps reads) is free.

Tip: `--review` exists precisely because TTS is the most expensive stage to
redo — inspect the script before synthesis, edit `script.json` if needed, and
continue with `regen-audio`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ffmpeg not found` | Install ffmpeg (see Prerequisites) or set explicit paths in `config/config.yaml`. `winget` installs need a new terminal to refresh PATH. |
| **Narration mismatches the screen** | Use `--review`: inspect/edit the script in `<run-dir>/script.json`, then `demo-narrator regen-audio <run-dir>`. Also try providing better context (`--sprint`/`--context`) and lowering `scene_threshold` (e.g. 0.15) so subtle page changes get their own segments. |
| Too few / too many segments | Tune `segmentation.scene_threshold` (lower = more segments) and `min_segment_sec`. |
| `Claude Code CLI ('claude') not found` | Install Claude Code and log in, or switch to `claude.mode: api_key` + `ANTHROPIC_API_KEY`. |
| Kokoro errors mentioning espeak / phonemes | Install the `espeak-ng` system package (see Prerequisites). |
| `TTS character guard` abort | Raise `tts.max_tts_characters`, shorten the script, or use `--provider kokoro` (the cap only applies to paid providers). |
| ElevenLabs 429s | Handled automatically (sequential requests, exponential backoff). Persistent 429s mean your plan's concurrency/quota is exhausted. |
| Run crashed halfway | `demo-narrator generate <video> --resume <run-dir>` skips completed stages; `<run-dir>/run.log` has full debug logs. |
| Voice sounds wrong on jargon | Add entries to `config/glossary.json` (e.g. `"K8s": "kubernetes"`), then `regen-audio`. |

## Development

```bash
make install      # venv + editable install with dev deps
make check        # mypy (strict) + full test suite
make test-unit    # skip the ffmpeg integration tests
```

Tests mock all network calls; the ffmpeg integration test generates its own
tiny test video and is skipped automatically when ffmpeg is unavailable.

## Design decisions

- **Claude Agent SDK as the default Stage-3 backend.** It authenticates through
  the local Claude Code CLI, so usage draws on the user's subscription. The SDK
  has no direct image parameter, so Claude reads keyframes itself via its
  `Read` tool (the only tool allowed, with the run directory as cwd); JSON is
  enforced with `output_format: json_schema` and read from
  `ResultMessage.structured_output`. `api_key` mode sends base64 image blocks
  and uses `output_config.format` structured outputs (default model
  `claude-opus-4-8`).
- **Freeze-frame via still-image concat, not `tpad`.** `tpad` requires
  ffmpeg ≥ 4.2; generating a still clip from the last frame and concatenating
  works on every ffmpeg build (some machines carry ancient ffmpeg 3.x from
  ImageMagick installs).
- **Uniform re-encode per segment, stream-copy concat.** Each segment is
  encoded once with identical codec parameters (CFR, same timescale), so the
  final concatenation is a lossless `-c copy` with `+faststart`.
- **Timings always come from Stage 1 and ffprobe, never from the model.**
  Claude's echoed `start_sec`/`end_sec` are ignored; narration WAV durations
  are measured with ffprobe before reconciliation.
- **Glossary applies to TTS text only.** `script.json` keeps both `narration`
  (display, used in subtitles/script.md) and `tts_text` (glossary-applied), so
  phonetic spellings never leak into subtitles.
- **Word budget enforcement is tolerant by design.** After the single repair
  pass, still-over-budget segments proceed with a warning — the assembly stage
  guarantees correctness via freeze-frames, so a long narration degrades
  gracefully instead of failing the run.
- **ElevenLabs via plain REST (httpx)** rather than the vendor SDK: trivial to
  mock, full control over retry/backoff, one less dependency.
- **Secrets only via env/.env** (python-dotenv). The config object that holds
  them redacts itself in `repr`, and log statements never interpolate secrets.

## QUICKSTART

Exact commands for a first real run (Windows shown; adapt the venv activation
for macOS/Linux):

```powershell
cd demo-narrator
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[kokoro]"
winget install Gyan.FFmpeg           # if ffmpeg is missing; then open a NEW terminal
winget install eSpeak-NG.eSpeak-NG   # needed by Kokoro

copy config\config.example.yaml config\config.yaml
copy .env.example .env               # only needed for api_key mode / ElevenLabs / Azure DevOps

demo-narrator config-check           # must end with "All required checks passed."

# First run — review the script before synthesis:
demo-narrator generate .\recording.mp4 --sprint 174 --review

# Happy with the flow? Fully automatic from now on:
demo-narrator generate .\recording.mp4 --sprint 174
```

The final video lands in `output\recording-narrated.mp4`, with the script and
subtitles beside it.
