"""Supervised flow generation: Claude proposes one browser action at a time,
we execute it against the logged-in app and feed back the DOM, until the feature
is demonstrated. The result is a reviewable flow spec (Stage B, assisted mode).

Claude never drives the browser directly — the runner executes every action, so
navigation stays deterministic and destructive confirmations can be blocked.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..errors import ScriptGenerationError
from ..progress import ProgressFn, report
from ..trackers import tracker_from_profile
from .decision import DecisionBackend
from .models import Beat, FlowSpec, Step
from .profile import ProjectProfile
from .runner import _import_playwright, _run_step, settle

logger = logging.getLogger(__name__)

_DESTRUCTIVE = ("delete", "remove")  # never auto-click these during exploration

# One interactive element, as surfaced to Claude with a ready-to-use locator.
# Each element carries its nearest-row `context` text so repeated per-row controls
# (kebab menus, checkboxes) can be told apart.
_CAPTURE_JS = r"""
(testAttr) => {
  function ariaRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const t = el.tagName.toLowerCase();
    if (t === 'a' && el.hasAttribute('href')) return 'link';
    if (t === 'button') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'button' || ty === 'submit') return 'button';
      return 'textbox';
    }
    if (t === 'h1' || t === 'h2' || t === 'h3') return 'heading';
    return t;
  }
  function clean(t) { return (t || '').trim().replace(/\s+/g, ' ').slice(0, 50); }
  function rowContext(el) {
    const row = el.closest('tr,[role="row"],li');
    return row ? clean(row.innerText) : '';
  }
  function headerText(el) {
    return clean((el.innerText || '').split('\n')[0]);
  }
  const sel = 'button,a[href],input,textarea,select,[role="button"],[role="menuitem"],'
    + '[role="checkbox"],[role="tab"],[role="radio"],[role="link"],[role="option"],'
    + '[' + testAttr + '],h1,h2,h3';
  // Section markers: accordion/group headers and headings. A control's "section"
  // is the nearest such marker BEFORE it in document order — this is how a
  // checkbox learns it lives under "Manufacturer" vs "Program operator".
  const sectionSel = 'button[aria-expanded],[' + testAttr + '$="-header"],h1,h2,h3,h4';
  // Detect a REAL open dialog (strict: role=dialog / aria-modal only — MUI uses
  // .MuiModal-root for drawers and menus too, which must NOT hijack the digest).
  const dialogs = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],.MuiDialog-container')]
    .filter(d => { const r = d.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  const modal = dialogs.length ? dialogs[dialogs.length - 1] : null;
  const out = [];
  const seen = new Set();
  let section = '';
  // single pass over candidates AND section markers, in document order
  for (const el of document.querySelectorAll(sel + ',' + sectionSel)) {
    // update the running section (skip result-tile titles — those aren't sections)
    if (el.matches(sectionSel) && el.getAttribute(testAttr) !== 'item-title') {
      const t = headerText(el);
      if (t) section = t;
    }
    if (!el.matches(sel)) continue;
    const rect = el.getBoundingClientRect();
    const visible = !!(el.offsetParent || rect.width || rect.height) && rect.width > 0 && rect.height > 0;
    if (!visible) continue;
    const testid = el.getAttribute(testAttr);
    const role = ariaRole(el);
    const name = (el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || (el.innerText || '').trim().split('\n')[0] || el.value || '').slice(0, 60);
    const context = rowContext(el) || section;   // row wins (more specific), else section
    // include context in the dedupe key so identical per-row/section controls stay distinct
    const key = role + '|' + name + '|' + (testid || '') + '|' + context;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      tag: el.tagName.toLowerCase(), role, name, testid, context,
      enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
      chrome: !!el.closest('header,footer,nav,[role="navigation"]'),
      inModal: !!(modal && modal.contains(el)),
    });
  }
  // Priority order for the element budget: open-dialog content, then page
  // content, then header/footer/nav chrome (stable within each tier). Ordering
  // instead of hard scoping means a detection mistake can never blind the digest.
  return out.map((e, i) => [e, i])
    .sort((a, b) => (b[0].inModal - a[0].inModal) || (a[0].chrome - b[0].chrome) || (a[1] - b[1]))
    .map((p) => p[0]);
}
"""

_NAMED_ROLES = {
    "link", "button", "menuitem", "checkbox", "tab", "radio", "option", "combobox", "heading",
}


def _suggest_locator(e: dict[str, Any]) -> dict[str, Any]:
    """The exact locator Claude should copy for an element (testid > role+name > text)."""
    if e.get("testid"):
        return {"testid": e["testid"]}
    if e.get("name") and e.get("role") in _NAMED_ROLES:
        return {"role": e["role"], "name": e["name"]}
    if e.get("name"):
        return {"text": e["name"]}
    return {"css": e.get("tag", "*")}

_LOCATOR_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "testid": {"type": "string"},
        "role": {"type": "string"},
        "name": {"type": "string"},
        "label": {"type": "string"},
        "text": {"type": "string"},
        "placeholder": {"type": "string"},
        "css": {"type": "string"},
        "nth": {"type": "integer"},
    },
}

_STEP_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "goto", "click", "fill", "press", "select", "hover",
                "scroll_to", "highlight", "wait_for", "expect", "dwell",
            ],
        },
        "locator": _LOCATOR_SCHEMA,
        "path": {"type": ["string", "null"]},
        "value": {"type": ["string", "null"]},
        "keys": {"type": ["string", "null"]},
        "state": {"type": "string", "enum": ["visible", "hidden", "attached"]},
        "say": {"type": ["string", "null"]},
        "settle_ms": {"type": "integer"},
        "min_duration_sec": {"type": "number"},
        "destructive": {"type": "boolean"},
    },
    "required": ["action"],
}

_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "thought": {"type": "string"},
        "done": {"type": "boolean"},
        "step": _STEP_SCHEMA,
    },
    "required": ["thought", "done"],
}


def resolve_hint(
    project_name: str,
    task_id: int,
    explicit: Path | None,
    profiles_dir: Path = Path("profiles"),
) -> str | None:
    """Load operator guidance for a task: an explicit --hint file, else the
    convention <profiles_dir>/<project>/hints/task-<id>.md. Returns None if absent."""
    if explicit is not None:
        if not explicit.exists():
            raise ScriptGenerationError(f"--hint file not found: {explicit}")
        return explicit.read_text(encoding="utf-8").strip() or None
    default = profiles_dir / project_name / "hints" / f"task-{task_id}.md"
    if default.exists():
        logger.info("using hint file %s", default)
        return default.read_text(encoding="utf-8").strip() or None
    return None


def fetch_task(profile: ProjectProfile, task_id: int) -> str:
    """Read a work item's text over REST (PAT if set, else the az AAD session)."""
    return tracker_from_profile(profile).work_item_text(task_id)


def _capture_dom(page: Any, test_id_attr: str) -> list[dict[str, Any]]:
    # Full list, uncapped: nth indices must reflect the real DOM, and the
    # formatter below collapses repetition instead of truncating blindly.
    elements: list[dict[str, Any]] = page.evaluate(_CAPTURE_JS, test_id_attr)
    return elements

_PER_SIGNATURE = 4     # examples shown per repeated locator signature
_MAX_LINES = 110       # hard stop for the whole listing


def _format_elements(elements: list[dict[str, Any]]) -> str:
    # Count each base-locator signature over the FULL list so nth values are
    # exact; long same-signature runs (category links, result tiles) show a few
    # examples plus a summary instead of flooding the element budget.
    sigs = [json.dumps(_suggest_locator(e), sort_keys=True) for e in elements]
    totals: dict[str, int] = {}
    for s in sigs:
        totals[s] = totals.get(s, 0) + 1

    lines: list[str] = []
    running: dict[str, int] = {}
    suppressed: dict[str, int] = {}
    truncated = 0
    for i, e in enumerate(elements):
        sig = sigs[i]
        idx = running.get(sig, 0)
        running[sig] = idx + 1
        if idx >= _PER_SIGNATURE and totals[sig] > _PER_SIGNATURE + 1:
            suppressed[sig] = suppressed.get(sig, 0) + 1
            continue
        if len(lines) >= _MAX_LINES:
            truncated += 1
            continue
        loc = _suggest_locator(e)
        if totals[sig] > 1:
            loc["nth"] = idx
        name = f' "{e["name"]}"' if e.get("name") else ""
        ctx = f' @ "{e["context"]}"' if e.get("context") and e["context"] != e.get("name") else ""
        disabled = " (disabled)" if not e.get("enabled", True) else ""
        lines.append(
            f'[{i}] {e["role"]}{name}{ctx}{disabled}  -> locator: {json.dumps(loc, separators=(",", ":"))}'
        )
    for sig, count in suppressed.items():
        base = json.dumps(json.loads(sig), separators=(",", ":"))
        lines.append(
            f'[+] {count} more elements share locator {base} — target one with "nth": '
            f'{_PER_SIGNATURE}..{totals[sig] - 1}'
        )
    if truncated:
        lines.append(f"[!] {truncated} further distinct elements omitted (listing budget)")
    return "\n".join(lines)


def _build_prompt(
    task_text: str,
    profile: ProjectProfile,
    url: str,
    taken: list[Step],
    elements: str,
    max_steps: int,
    note: str | None,
    hint: str | None = None,
) -> str:
    routes = "\n".join(f"  {k}: {v}" for k, v in profile.feature_map.items()) or "  (none provided)"
    context = (
        f"\nABOUT THIS APP (background — orients you; the task below is what to demonstrate):\n"
        f"{profile.context.strip()}\n"
        if profile.context and profile.context.strip() else ""
    )
    history = "\n".join(
        f"  {i}. {s.action} {_step_summary(s)}" + (f'  // say: "{s.say}"' if s.say else "")
        for i, s in enumerate(taken)
    ) or "  (none yet)"
    correction = f"\nIMPORTANT — your previous decision was rejected: {note}\n" if note else ""
    guidance = (
        f"\nOPERATOR GUIDANCE — the person who built this feature wrote the steps below.\n"
        f"Follow them closely; they OVERRIDE your own guesses about what to click:\n{hint}\n"
        if hint else ""
    )
    return f"""You are authoring a short, NON-DESTRUCTIVE product demo of a web-app feature by
walking its UI one action at a time. I (the runner) execute each action you
return and reply with the new page state; you never control the browser directly.
{context}
FEATURE TO DEMONSTRATE (from the issue tracker):
{task_text}
{guidance}
APP: {profile.name}  —  CURRENT URL: {url}
KNOWN ROUTES (locale-free; usable with the `goto` action):
{routes}

STEPS TAKEN SO FAR ({len(taken)}/{max_steps} max):
{history}
{correction}
INTERACTIVE ELEMENTS ON THE CURRENT PAGE — pick one and COPY its `locator` object
EXACTLY (do not combine strategies or invent selectors). The ` @ "..."` suffix is
the element's SECTION/row — when guidance names a section (e.g. the *Manufacturer*
filter, not *Program operator*), pick an element whose ` @ "..."` matches it:
{elements}

Return ONE decision as JSON matching the provided schema:
- To act: set done=false and provide `step`. Copy the `locator` shown for the
  element you choose. Add a first-person `say` narration (<= ~30 words) for steps
  worth narrating; omit `say` for trivial navigation. Use `settle_ms` (600-1500)
  after actions that load data.
- To finish: set done=true (no step). Finish once the feature is clearly shown AND
  any dialog has been cancelled/closed.

RULES:
- You always start ALREADY SIGNED IN. If a sign-in/login page ever appears, do NOT
  attempt to log in (never fill credentials): set done=true immediately and say why
  in `thought` — authentication is handled outside this loop.
- `goto` paths are LOCALE-FREE (write /search?x=1, not /en/search?x=1). If guidance
  contains a full URL, you may pass it verbatim (http://...) — the runner accepts
  both — but never hand-build a path that repeats the locale prefix.
- After every goto, compare CURRENT URL with what you requested: a different path
  means the app redirected — your path was wrong; fix the path, don't wait for
  elements that will never come.
- Loading spinners: to wait for loading to FINISH, use wait_for with state="hidden"
  on the spinner element. Never wait for a spinner to become visible.
- If you are on the RIGHT page but the element the guidance describes is nowhere in
  the elements list after loading settles, do not keep hunting (no blind scrolling
  or key presses): set done=true and state exactly what is missing in `thought` —
  the feature may not be deployed in this environment.
- If OPERATOR GUIDANCE is present, follow its sequence step by step, and let it OVERRIDE
  the feature title/description when they differ — the guidance is the source of truth for
  what to click (e.g. if guidance says demonstrate the *Manufacturer* filter, do that even
  if the task title mentions *Program operator*). When it names a specific button/menu
  label, click that EXACT label when it appears in the elements list — do NOT substitute a
  similar-looking one. If the named element isn't visible yet, use `wait_for` or `dwell`
  rather than improvising a different path.
- A locator uses EXACTLY ONE strategy. For named links/buttons use {{"role":...,"name":...}} —
  never {{"role":...,"text":...}}.
- NEVER click destructive or data-mutating confirmations (Delete, Remove, Save,
  Confirm, or a final "Copy" that commits changes). Demonstrate the flow, then
  Cancel/close. Set step.destructive=true if a step could mutate data.
- Reach the feature by navigating (goto known routes, click into a project/record).
- Keep the whole demo to ~8-12 narrated steps. Be concise and concrete.
"""


def _step_summary(s: Step) -> str:
    if s.action == "goto":
        return s.path or ""
    if s.locator is not None:
        loc = s.locator.model_dump(exclude_none=True)
        return json.dumps(loc, separators=(",", ":"))
    return ""


def _prepare_step(raw_step: dict[str, Any]) -> tuple[Step | None, str | None]:
    """Validate one proposed step, coercing the common role+text -> role+name slip."""
    raw = {k: v for k, v in raw_step.items() if v is not None and k != "destructive"}
    loc = raw.get("locator")
    if isinstance(loc, dict):
        if "role" in loc and "text" in loc and "name" not in loc:
            loc["name"] = loc.pop("text")
        elif "text" in loc and "name" in loc:
            loc.pop("text")
    try:
        return Step.model_validate(raw), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc).splitlines()[0]


def _is_destructive(decision_step: dict[str, Any], step: Step) -> bool:
    if decision_step.get("destructive"):
        return True
    name = (step.locator.name or step.locator.text or "").lower() if step.locator else ""
    return any(d in name for d in _DESTRUCTIVE)


def _execute(page: Any, profile: ProjectProfile, step: Step, locale: str, test_id_attr: str) -> str | None:
    """Execute a step; auto-disambiguate an ambiguous locator with nth=0. Returns
    an error note on failure, else None."""
    try:
        _run_step(page, profile, step, locale, test_id_attr, highlight=False)
        return None
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).splitlines()[0]
        if "strict mode violation" in msg and step.locator is not None and step.locator.nth is None:
            step.locator.nth = 0
            try:
                _run_step(page, profile, step, locale, test_id_attr, highlight=False)
                return None
            except Exception as exc2:  # noqa: BLE001
                msg = str(exc2).splitlines()[0]
        return f"that step failed to run: {msg}. Pick a different element."


def generate_flow(
    profile: ProjectProfile,
    task_id: int,
    task_text: str,
    backend: DecisionBackend,
    *,
    storage_state: str | None,
    max_steps: int = 14,
    headless: bool = True,
    hint: str | None = None,
    on_progress: ProgressFn | None = None,
) -> FlowSpec:
    """Run the supervised loop and return a validated FlowSpec."""
    sync_playwright = _import_playwright()
    locale = profile.default_locale
    test_id_attr = profile.selectors.test_id_attribute
    vp = profile.viewport
    taken: list[Step] = []
    note: str | None = None
    invalid_streak = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            viewport={"width": vp.width, "height": vp.height},
            storage_state=storage_state if storage_state and Path(storage_state).exists() else None,
        )
        # Fail fast during exploration: a wrong locator should cost seconds, not
        # a 20s stall per turn. (Recording uses its own timeout.)
        ctx.set_default_timeout(10000)
        page = ctx.new_page()
        page.goto(profile.base_url.rstrip("/") + profile.route_prefix.replace("{locale}", locale) + "/",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        if profile.auth.sign_in_path and profile.auth.sign_in_path in page.url:
            ctx.close()
            browser.close()
            raise ScriptGenerationError(
                "The app presented its sign-in page — the stored session is invalid. "
                "Callers should run ensure_session() first (jobs/CLI do this automatically); "
                f"or refresh manually: demo-narrator demo-auth --project {profile.name}"
            )

        try:
            for turn in range(max_steps):
                settle(page)  # let progressively-rendered content finish before reading
                elements = _format_elements(_capture_dom(page, test_id_attr))
                decision = backend.ask(
                    _build_prompt(task_text, profile, page.url, taken, elements, max_steps, note, hint),
                    _DECISION_SCHEMA,
                )
                thought = str(decision.get("thought", ""))[:100]
                logger.info("gen turn %d: %s", turn, thought)
                report(on_progress, "generate", f"task {task_id} turn {turn + 1}/{max_steps}: {thought}")
                if decision.get("done") or not decision.get("step"):
                    break

                step, err = _prepare_step(decision["step"])
                if err or step is None:
                    invalid_streak += 1
                    note = f"invalid step ({err}). Copy a locator EXACTLY as shown."
                    logger.warning("invalid step #%d: %s", invalid_streak, err)
                    if invalid_streak >= 3:
                        break
                    continue
                invalid_streak, note = 0, None

                if _is_destructive(decision["step"], step):
                    logger.info("recorded but did NOT execute destructive step")
                    taken.append(step)
                    break

                exec_err = _execute(page, profile, step, locale, test_id_attr)
                if exec_err:
                    note = exec_err
                    logger.warning(exec_err)
                    continue
                if step.settle_ms:
                    page.wait_for_timeout(step.settle_ms)
                taken.append(step)
                logger.info("  executed %s %s", step.action, _step_summary(step))
        finally:
            ctx.close()
            browser.close()

    if not taken:
        raise ScriptGenerationError("Claude produced no usable steps for this task.")

    title = _title_from_task(task_text, task_id)
    # viewport is left unset (None) so it inherits the profile's — avoids a
    # noisy `viewport: {}` in the serialized flow.
    return FlowSpec(
        version=1,
        name=f"task-{task_id}-generated",
        title=title,
        project=profile.name,
        locale=locale,
        beats=[Beat(task=task_id, title=title, steps=taken)],
    )


DEMO_NARRATION_STYLE = """You are writing the spoken voiceover for a product demo video.
Rewrite each narration line so it is:
- Concise: ONE sentence, <= 24 words, natural spoken English.
- Descriptive of what the viewer sees happen on screen — NOT an instruction to yourself.
    Good: "Selecting an object activates the Actions button for that sub-element."
    Bad:  "Let's click the checkbox to demonstrate the bulk-selection feature."
- Free of meta-commentary about requirements ("as specified", "matching the acceptance
  criteria", "just as expected", "to demonstrate").
- Cohesive as a sequence: the first line introduces the feature; later lines flow on.
- Present tense, active voice; avoid "Let's" filler unless it genuinely reads well."""


def dedupe_steps(steps: list[Step]) -> list[Step]:
    """Drop consecutive steps that repeat the same action/locator/value."""
    out: list[Step] = []
    last_key: tuple[Any, ...] | None = None
    for s in steps:
        loc = s.locator.model_dump(exclude_none=True) if s.locator else None
        key = (s.action, json.dumps(loc, sort_keys=True), s.value, s.path, s.keys)
        if key == last_key:
            logger.info("deduped repeated step: %s %s", s.action, _step_summary(s))
            continue
        out.append(s)
        last_key = key
    return out


def _narrated(steps: list[Step]) -> list[Step]:
    return [s for s in steps if s.say]


def polish_narration(
    flow: FlowSpec, task_text: str, backend: DecisionBackend, style: str = DEMO_NARRATION_STYLE
) -> None:
    """Rewrite all `say:` lines together (one Claude call) for concision + cohesion.

    Mutates the flow in place. On any mismatch/failure the originals are kept.
    """
    targets = [s for beat in flow.beats for s in _narrated(beat.steps)]
    if not targets:
        return
    listing = "\n".join(
        f'{i + 1}. [{s.action} {_step_summary(s)}] "{s.say}"' for i, s in enumerate(targets)
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"narrations": {"type": "array", "items": {"type": "string"}}},
        "required": ["narrations"],
    }
    prompt = (
        f"{style}\n\nFEATURE (context):\n{task_text[:1200]}\n\n"
        f"CURRENT NARRATION LINES — rewrite each, keep exactly {len(targets)} lines in order:\n"
        f"{listing}\n\nReturn JSON {{\"narrations\": [...]}} with exactly {len(targets)} rewritten lines."
    )
    try:
        result = backend.ask(prompt, schema)
        rewritten = result.get("narrations", [])
    except ScriptGenerationError as exc:
        logger.warning("narration polish skipped: %s", exc)
        return
    if not isinstance(rewritten, list) or len(rewritten) != len(targets):
        logger.warning(
            "narration polish returned %s lines (expected %d); keeping originals.",
            len(rewritten) if isinstance(rewritten, list) else "?", len(targets),
        )
        return
    for step, new_say in zip(targets, rewritten):
        if isinstance(new_say, str) and new_say.strip():
            step.say = new_say.strip()
    logger.info("polished %d narration lines.", len(targets))



def regenerate_say(
    flow: FlowSpec,
    beat_index: int,
    step_index: int,
    task_text: str,
    backend: DecisionBackend,
    style: str = DEMO_NARRATION_STYLE,
) -> str:
    """Rewrite the `say:` of one step, with its neighbours as context.

    The single-step sibling of `polish_narration`: the narration editor rewrites
    one line at a time, and a whole-flow call would churn lines the user has
    already approved. Returns the new line; the caller decides whether to keep it.
    """
    try:
        beat = flow.beats[beat_index]
        step = beat.steps[step_index]
    except IndexError as exc:
        raise ScriptGenerationError(
            f"step {beat_index}/{step_index} is out of range for this flow"
        ) from exc

    def _line(i: int, s: Step) -> str:
        marker = " <- REWRITE THIS ONE" if i == step_index else ""
        said = f' "{s.say}"' if s.say else " (no narration)"
        return f"{i + 1}. [{s.action} {_step_summary(s)}]{said}{marker}"

    context = "\n".join(_line(i, s) for i, s in enumerate(beat.steps))
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"narration": {"type": "string"}},
        "required": ["narration"],
    }
    prompt = (
        f"{style}\n\nFEATURE (context):\n{task_text[:1200]}\n\n"
        f"BEAT STEPS (narration in sequence):\n{context}\n\n"
        f"Rewrite ONLY the narration for step {step_index + 1} "
        f"({step.action} {_step_summary(step)}). It must follow on naturally from the "
        f"line before it and not repeat what the surrounding lines already say.\n"
        f'Return JSON {{"narration": "..."}} with a single sentence.'
    )
    result = backend.ask(prompt, schema)
    new_say = result.get("narration")
    if not isinstance(new_say, str) or not new_say.strip():
        raise ScriptGenerationError("the model returned no narration for this step")
    return new_say.strip()

def refine_flow(
    flow: FlowSpec, task_text: str, backend: DecisionBackend, *, polish: bool = True
) -> FlowSpec:
    """Deterministic dedupe + optional Claude narration polish."""
    for beat in flow.beats:
        beat.steps = dedupe_steps(beat.steps)
    if polish:
        polish_narration(flow, task_text, backend)
    return flow


def _title_from_task(task_text: str, task_id: int) -> str:
    for line in task_text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            # "## [90396] Product Backlog Item: LCA - Project - Copy ... (In Progress)"
            body = line[3:]
            if ":" in body:
                body = body.split(":", 1)[1]
            return body.rsplit("(", 1)[0].strip() or f"Task {task_id}"
    return f"Task {task_id}"
