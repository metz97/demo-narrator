# Playwright demo generation — design (v1, focus: lca-tool web)

This document defines the two contracts that make the demo generator work for
lca-tool today and extend to other web apps later **by adding config, not code**:

1. **Project profile** — everything app-specific (URL, auth, selectors, issue
   tracker, source hints). One file per app: `profiles/<app>.yaml`.
2. **Flow spec** — an app-agnostic, human-reviewable list of browser steps with
   per-step narration. Claude *generates* it, a human *reviews* it, the runner
   *replays* it. File: `profiles/<app>/flows/<name>.demoflow.yaml`.

Everything between the two contracts is the generic engine.

```
 input: --project lca-tool --sprint 179 --task 90396 90397 90398
   │
   ├─ A. Fetch tasks         (WorkItemProvider ← profile.issue_tracker)   [generic + azure_devops impl]
   ├─ B. Generate flow       (Claude: task text + source hints + live DOM) → *.demoflow.yaml   [generic]
   │        └─ human review / edit  (the whole point of the flow spec being readable)
   ├─ C. Narration script    (per-step `say:` → validated, glossary applied)   [reuse Stage 3/4 engine]
   ├─ D. Run + record         (Playwright replays flow, records video,          [NEW runner]
   │                           audio-driven pacing)
   └─ E. Assemble             (mux one narration track over the recording + SRT) [reuse Stage 5]
```

Why this scales to N apps: Playwright drives the **browser**, not the
framework. React/Angular/Vue all become DOM, so the runner is inherently
app-agnostic. All per-app knowledge lives in the profile; all per-feature
knowledge lives in the flow spec.

---

## 1. Project profile

One YAML per app. Secrets are **never** in this file — only env-var *names* are
referenced; values come from the environment / `.env` (consistent with the rest
of demo-narrator). See `profiles/lca-tool.yaml` for the filled-in reference.

### Fields

| Field | Req | Meaning |
|---|---|---|
| `name` | ✅ | Profile id, matches `--project`. |
| `base_url` | ✅ | Running app URL. Supports `${ENV:-default}` expansion. |
| `route_prefix` | | Template prepended to every flow `goto` path. lca-tool routes are under `/:lang`, so `"/{locale}"`. |
| `default_locale` | | Fills `{locale}` (lca-tool: `en`). |
| `viewport` | | `{width, height}` for the recording (default 1280×720). |
| `dev_server` | | `{command, url, timeout_sec}` — auto-start when `base_url` is local and nothing is listening. |
| `auth` | ✅ | How to obtain an authenticated session. See **Auth strategies**. |
| `selectors.test_id_attribute` | | Attribute for `testid` locators (lca-tool: `data-testid`). |
| `selectors.fallback` | | Ordered locator strategies when no testid exists (`[role, label, text, placeholder]`). |
| `issue_tracker` | ✅ | `{provider, ...}` — how `--sprint`/`--task` resolve to work-item text. |
| `source` | | Optional accelerator: `{repo_path, e2e_path, framework, router_file}` mined during flow generation. |
| `glossary_path` / `style_path` | | Per-app overrides for pronunciation & narration style. |

### Auth strategies

Auth is the main per-app integration cost. The engine never learns "how to log
in to app X"; it only needs **an authenticated session**. Three ways to give it
one, in order of preference:

1. **`storage_state`** — point at a Playwright `storageState` JSON produced by a
   prior login. For lca-tool this is *free*: the QA `playwright-e2e` project
   already writes `.auth/core.json` / `.auth/lite.json`. The runner loads it via
   `browser.new_context(storage_state=...)`. No credentials touch our tool.
2. **`login_recipe`** — a short declarative flow (same step vocabulary as a demo
   flow) run once to establish the session, then cached as storage state. Used
   for apps without an existing e2e setup.
3. **`script`** — escape hatch: a path to a custom auth function, for exotic
   flows the recipe can't express.

lca-tool uses **`storage_state`** with a documented `login_recipe` fallback (the
recipe mirrors the real OAuth-PKCE flow from `auth.setup.ts`).

### Issue tracker (`WorkItemProvider`)

`--sprint N --task 90396 90397` must resolve to `{id, title, description,
acceptance_criteria, state}`. Modeled as a pluggable `WorkItemProvider`;
`azure_devops` is the only implementation for now (reuses the existing
`context.py` az-CLI logic). Adding Jira/GitHub later = one new provider class +
`provider:` value in a profile — the engine is unchanged.

---

## 2. Flow spec

A demo flow is metadata + **beats** (a beat groups steps under one demonstrated
task) + ordered **steps**. Claude emits it against a strict JSON schema; a human
reviews the readable YAML; the runner executes it deterministically.

### Top level

```yaml
version: 1
name: search-and-compare
title: "Search products and compare sustainability data"
project: lca-tool
locale: en                       # fills route_prefix {locale}
viewport: { width: 1280, height: 720 }
beats:
  - task: 90396                  # Azure DevOps work-item id this beat demonstrates
    title: "Product search"
    steps: [ ... ]
```

### Step model

Every step is a discriminated union on `action` (validated strictly — no free
regex). Element-acting steps carry a **locator** (see below). Any step may carry
narration and pacing:

| Common field | Meaning |
|---|---|
| `say` | Narration for this beat of the video. Omit → silent, fast mechanical step. |
| `pacing` | `audio` (default): hold the step until its narration finishes + pad. `fixed`: use `min_duration_sec`. |
| `min_duration_sec` | Floor on how long the step is shown (for `fixed`, or to linger). |
| `settle_ms` | Extra wait after the action (animations, data load) before the next step. |

**Actions:**

| `action` | Payload | Notes |
|---|---|---|
| `goto` | `path` | Prefixed by profile `route_prefix`. |
| `click` | `locator` | |
| `fill` | `locator`, `value` | |
| `press` | `keys` (e.g. `"Enter"`) | Optional `locator` to focus first. |
| `select` | `locator`, `value` | Native/ARIA select. |
| `hover` | `locator` | |
| `scroll_to` | `locator` | Smooth-scrolls into view (demo polish). |
| `highlight` | `locator` | Visual emphasis only, no state change (injected outline). |
| `wait_for` | `locator`, `state` (`visible`/`hidden`/`attached`) | Readiness gate before narrating about it. |
| `expect` | `locator`, `assert` (`visible`/`text`/`count`), `value?` | Sanity check; failure aborts the flow with a clear error. |
| `dwell` | — | Hold on the current screen (usually with `say`) — e.g. narrate over a static result. |

### Locator (prefer semantic over CSS)

```yaml
locator:
  testid: "item"          # → getByTestId (uses profile test_id_attribute)
  # OR role + name:
  role: "button"
  name: "Compare"         # accessible name
  # OR:
  label: "Search NBS LCA" # getByLabel (aria-label / <label>)
  text: "Compare"         # getByText
  placeholder: "..."      # getByPlaceholder
  css: "[data-x] > .y"    # escape hatch — discouraged, flags a review warning
  nth: 0                  # disambiguate when several match
  within:                 # optional scoping parent locator (same shape)
    testid: "search-header"
```

Generation preference order: `testid` → `role`+`name` → `label`/`placeholder` →
`text` → `css`. A `css` locator is allowed but surfaces a warning at review time,
because it is the most brittle.

### Audio-driven pacing (why sync is exact, not reconciled)

The recording bends to the audio, not the other way around:

1. For each step with `say`, synthesize the narration first and measure its WAV
   duration (reusing the existing TTS providers).
2. The runner performs the step's action, then **holds** for the narration
   duration (`pacing: audio`) before advancing.
3. The Playwright video therefore spans exactly the narrated timeline. Assembly
   is then just muxing one continuous narration track over the recording + SRT —
   **no freeze-frames, no stretching, no manual timestamp editing.**

Silent steps (`say` omitted) run at natural speed, so mechanical setup clicks
don't waste video time.

---

## 3. What this reuses vs. what is new

| Piece | Status |
|---|---|
| Work-item fetch (`context.py`, az CLI) | **Reuse** (wrapped as `azure_devops` WorkItemProvider). |
| Narration script gen + word budget + glossary + TTS | **Reuse** — input is now per-step `say:` (higher accuracy, cheaper: structured steps beat keyframe guessing). |
| Assembly / SRT / script.md | **Reuse** — simpler (mux one track, no freeze-frame logic). |
| Flow generation (Stage B) | **New** — Claude + source hints + live-DOM exploration → flow spec. |
| Flow runner (Stage D) | **New** — replays a flow spec in Playwright, records video, audio-driven pacing. Runner language TBD (TS runner reusing QA auth/POM vs Playwright-Python). |

Scene detection (old Stage 1) is retired for this mode; the flow spec replaces it.
