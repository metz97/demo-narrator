# demo-narrator — Setup & Run Guide

From zero to a narrated sprint-demo video produced entirely from your browser.
This covers the **web application** (`demo-narrator serve`); every step has a
CLI equivalent noted at the end.

---

## 1. Prerequisites

| Requirement | Why | Install |
|---|---|---|
| **Python 3.12+** | the engine | python.org / `winget install Python.Python.3.12` |
| **ffmpeg + ffprobe** (4.x+) | video assembly | Win: `winget install Gyan.FFmpeg` (new terminal after) · mac: `brew install ffmpeg` · deb: `apt install ffmpeg` |
| **espeak-ng** | Kokoro TTS phonemizer | Win: `winget install eSpeak-NG.eSpeak-NG` · mac: `brew install espeak-ng` · deb: `apt install espeak-ng` |
| **Claude access** (one of) | flow generation + narration polish | **(a)** Claude Code CLI installed & logged in (uses your subscription) — or **(b)** an `ANTHROPIC_API_KEY` |
| **Azure DevOps access** (one of) | sprint/task listing | **(a)** a PAT with *Work Items: Read* — or **(b)** a logged-in `az` CLI (`az login`; the tracker auto-discovers the org's Entra tenant) |
| **A running target app** | the thing being demoed | staging URL or local dev server, reachable from this machine |
| **A no-MFA demo account** for that app | automated login cannot pass 2FA | ask your QA team — it's the same kind of account e2e tests use |

## 2. Install

```powershell
git clone <repo> demo-narrator
cd demo-narrator
python -m venv .venv
.\.venv\Scripts\Activate.ps1              # macOS/Linux: source .venv/bin/activate

pip install -e ".[server,demo,kokoro]"    # web server + Playwright + free local TTS
python -m playwright install chromium     # one-time browser download
```

Kokoro downloads ~330 MB of voice weights into the Hugging Face cache on first
synthesis — the first video takes a few extra minutes.

## 3. Configure

```powershell
copy config\config.example.yaml config\config.yaml
copy .env.example .env
```

**`config/config.yaml`** — the keys that matter first:

```yaml
claude:
  mode: agent_sdk        # your Claude Code subscription login (dev machines)
  # mode: api_key        # ANTHROPIC_API_KEY — use this on servers

tts:
  provider: kokoro       # free, local; or "elevenlabs" (paid, needs API key)

# ffmpeg/ffprobe: leave defaults if they are on PATH, otherwise absolute paths:
# ffmpeg:
#   ffmpeg_path: "C:\\...\\ffmpeg.exe"
#   ffprobe_path: "C:\\...\\ffprobe.exe"
```

**`.env`** — secrets only, never in YAML (all optional depending on your choices above):

```dotenv
E2E_CORE_USERNAME=demo-account@company.com   # target-app login (no MFA!)
E2E_CORE_PASSWORD=...
AZURE_DEVOPS_EXT_PAT=                        # blank = use your `az login` session
ANTHROPIC_API_KEY=                           # only for claude.mode: api_key
ELEVENLABS_API_KEY=                          # only for tts.provider: elevenlabs
```

Verify everything before the first run:

```powershell
demo-narrator config-check     # must end "All required checks passed."
```

## 4. Describe your app: the project profile

Each target app is one YAML in `profiles/<name>.yaml`. You have three ways to
create and shape it — pick whichever fits:

**a) In the browser (easiest).** On the Projects page click **➕ New project**,
enter a name + the app URL (and optionally a sentence about what it is). This
scaffolds a valid profile and drops you on the project page.

**b) The setup assistant (recommended for a good profile).** On any project,
open **🤖 setup assistant**. It's a short chat: it *inspects your live app*
(read-only, using the stored session — following redirects to learn the sign-in
path, reading nav links to propose real routes), asks you clarifying questions,
and builds a **project context** + **feature_map** for you. Watch the draft fill
in on the right, then **Apply to profile**. The context it writes is fed into
every future demo generation, so the generator understands your product — not
just one task title. Your credentials never pass through the assistant.

**c) By hand.** Copy [`profiles/lca-tool.yaml`](../profiles/lca-tool.yaml) as a
template, or edit any profile in the browser (**project page → edit profile**)
with schema validation on save. The essentials:

```yaml
name: my-app
base_url: "${MY_APP_URL:-http://localhost:4200}"   # must be reachable from HERE
route_prefix: "/{locale}"        # only if routes live under /en, /de, …
default_locale: en

auth:
  strategy: storage_state
  storage_state: ".auth/my-app.json"      # where the captured session is kept
  login_recipe:                            # how to log in (runs headless)
    credentials_env: { username: E2E_CORE_USERNAME, password: E2E_CORE_PASSWORD }
    steps:
      - { action: goto, path: "/sign-in" }
      - { action: fill, locator: { label: "Email address" }, value: "${E2E_CORE_USERNAME}" }
      # … mirror your app's real login flow; see lca-tool.yaml for a 2-step IdP

selectors:
  test_id_attribute: data-testid           # your app's test-id convention

issue_tracker:
  provider: azure_devops
  organization: "https://dev.azure.com/yourorg"
  project: YourProject
  team: YourTeam

feature_map:                               # route hints for the generator
  search: "/search"
  projects: "/projects"

context: |                                 # what the app IS — fed into every generation
  A carbon-assessment tool for construction. Users create projects, add building
  elements, run assessments, and view LCA analytics. (Let the setup assistant write this.)
```

You can edit this later in the browser (**project page → edit profile**) with
schema validation on save.

## 5. Start the server

```powershell
demo-narrator serve                        # → http://127.0.0.1:8765
```

Open the URL. Your project card should show — check its **health strip**:

- 🟢 `app reachable` — the target app answers.
- 🟢/🟡 `session Nh old` — an authenticated browser session exists.
- 🔴 `no session` → two options:
  - **Refresh session** — runs the login recipe headless (needs the no-MFA
    account in `.env`). Jobs also do this automatically when a session expires.
  - **Log in manually** — OAuth-consent style: a browser window opens **on the
    machine running demo-narrator** showing your app's real login page. Sign in
    yourself — **MFA works**, since it's you at the keyboard. The window closes
    itself the moment you're in, and only the resulting session is stored;
    your credentials never pass through demo-narrator.
    (CLI equivalent: `demo-narrator demo-auth --project my-app --show-browser`.)

## 6. Produce a sprint demo (the whole loop, in the browser)

1. **Pick sprint & tasks** — the sprint dropdown loads from your tracker
   (current sprint pre-selected). Tick the tasks that are *demoable in the UI*.
2. **Add guidance** for complex features (the textarea under each ticked task):
   where the feature lives, exact button labels, which record to use — e.g.
   *"Open project X → Carbon assessment → edit REF02 → Build composite assembly
   → Next → the Override checkbox."* Guidance is saved and reused on regeneration.
3. **Generate flows** — one background job per task (~2–6 min each); watch live
   logs on the job cards. Claude walks your app supervised and writes a flow.
4. **Review in the editor** (flows table → *edit*). This step is not optional
   in spirit: generation is right ~60% of the time. Fix narration inline,
   adjust selectors, delete stray steps, then **▶ Test this beat** — a ~1-min
   preview clip renders inline. **⟳ Regenerate** re-runs Claude with your
   guidance and shows an accept/dismiss diff. Every **Save** creates a
   restorable version.
5. **Render** — tick the flows, set the sprint label, keep *captions* and
   *intro/outro* on, click **Render video**. You get: intro card → "Feature 1
   of N" section card → each demo → outro card, karaoke captions throughout.
6. **Share** — Library → open the render → **🔗 Copy share link** (plus mp4 /
   subtitles / script downloads).

## 7. CLI equivalents (everything also works headless)

```powershell
demo-narrator demo-auth     --project my-app                  # capture session
demo-narrator demo-generate --project my-app --task 12345     # Claude authors a flow
demo-narrator demo          --project my-app --sprint 174 `
  --flow profiles\my-app\flows\task-12345-generated.demoflow.yaml   # record (repeat --flow to combine)
```

Flows/profiles/videos are plain files — the web UI and CLI are interchangeable.

## 8. Troubleshooting (field-tested)

| Symptom | Fix |
|---|---|
| Health: `app unreachable` | Start the app / fix the URL in the profile. All jobs will fail until green. |
| Health: `build error on page` | Your app's dev server shows an error overlay (e.g. a failed import) — the recording would film the error screen. Fix the app first. |
| `Refresh session` job fails | Wrong credentials in `.env`, an MFA-protected account (use the `--show-browser` escape hatch), or the login recipe no longer matches the login page. |
| Login recipe times out waiting for a post-login element | The OAuth round trip is landing somewhere else. Check the app's own redirect URI (lca-tool: `apps/lca-tool/web/public/oauth-config.json`, served at `/oauth-config.json` — it is **not** taken from the repo's `.env`) against the port the app serves on, and against what the IdP has registered. The IdP renders *Not a valid request* for an unregistered `redirect_uri`, so the recipe then fails one step earlier, on the email field. When the registered origin is a port the app no longer serves, run `make callback-shim` and set `DEMO_NARRATOR_BROWSER_ARGS` (see `.env.example`) — the shim 302s the code to the real origin, which keeps the PKCE verifier (per-origin `sessionStorage`) usable. |
| Session was fine, then broke after a port change | `storageState` holds the OAuth token in `localStorage`, which is per-origin, so a session captured on `:4200` is dead on `:4201` — the job re-runs the login recipe. Expected; the capture just has to succeed on the new origin. |
| Generation/recording lands on the sign-in page | Shouldn't happen anymore: every job **validates the session first and auto-runs the login recipe** when it's expired. If it still fails, the recipe itself is failing — see the row above. The generator is instructed never to type credentials. |
| Tracker 401 / `TF400813` | PAT missing/expired, or (az-login path) multi-tenant account — the tracker auto-resolves the org tenant, but the org's tenant must be in `az account list`. |
| `Claude Code CLI not found` | Install & log in to Claude Code, or switch `claude.mode: api_key` and set the key. |
| Generation loops on a list page / picks wrong records | Write **guidance** (step 6.2) — name exact labels and records. SVG charts and deep modals often need a hand-edited flow (the editor exists for exactly this). |
| Generation/recording picks a control from the wrong *section* (e.g. Program-operator instead of Manufacturer filter) | The engine now settles on progressively-rendered pages and tags each control with its section (`@ "Manufacturer"`). If it still errs, make the guidance name the section AND an exact label; the task *title* can bias it, so guidance wins only when explicit. |
| A step clicks before slow content appears | Handled: navigation now waits for the page to stop rendering (DOM quiescence) before the next step. Very slow apps: add an explicit `wait_for` on the app's `loading-spinner` (`state: hidden`) in the flow. |
| Recorded steps skipped, video short | The app's data changed (records deleted/renamed) or selectors rotted. Open the editor → *Test this beat* → fix locators. |
| Kokoro errors about espeak/phonemes | Install the `espeak-ng` system package. |
| First render extremely slow | One-time Kokoro weight download (~330 MB) + torch warm-up. |
| Job stuck `running` after a crash | Restart the server — stale jobs are auto-marked failed; click **retry**. |

## 9. Deploying for the team

For a shared VM (Docker or systemd) inside your network, see
[`deploy/README.md`](../deploy/README.md). Key rules: the VM must reach the
staging app, use `claude.mode: api_key` (not a personal subscription), and keep
the server off the public internet (no built-in auth — reverse proxy + SSO).
