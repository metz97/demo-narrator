"""Playwright runner: execute a flow spec against a running app, record video,
and capture a per-step timeline for audio-driven narration placement.
"""

from __future__ import annotations

import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import PlaywrightError
from .models import FlowSpec, Locator, Step
from .profile import ProjectProfile, expand_env

logger = logging.getLogger(__name__)

BROWSER_ARGS_ENV = "DEMO_NARRATOR_BROWSER_ARGS"


def _launch_args() -> list[str]:
    """Extra Chromium flags for the login launches, from DEMO_NARRATOR_BROWSER_ARGS.

    Escape hatch for local host/port quirks — chiefly an IdP that pins a client's
    redirect_uri to an origin the app no longer serves, where the callback has to
    be pointed at scripts/oauth_callback_shim.py. Quote flags whose value contains
    spaces; parsing is shell-like."""
    raw = os.environ.get(BROWSER_ARGS_ENV, "").strip()
    return shlex.split(raw) if raw else []

_HIGHLIGHT_JS = (
    "(el) => { const o = el.style.outline; el.style.outline='3px solid #7b2ff7';"
    " el.style.outlineOffset='2px'; setTimeout(()=>{el.style.outline=o;}, 900); }"
)


@dataclass(frozen=True)
class StepTiming:
    index: int
    action: str
    task: int | None
    say: str | None
    start_sec: float   # video-time when this step's narration should start
    hold_sec: float    # dwell applied (narration duration or min_duration)


@dataclass
class RunResult:
    video_path: Path
    timings: list[StepTiming] = field(default_factory=list)
    total_sec: float = 0.0


def _import_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via install docs
        raise PlaywrightError(
            "Playwright is not installed. Install the demo extra:\n"
            '  pip install "demo-narrator[demo]"\n'
            "then download the browser:\n"
            "  python -m playwright install chromium"
        ) from exc
    return sync_playwright


def _resolve(root: Any, spec: Locator, test_id_attr: str) -> Any:
    scope = _resolve(root, spec.within, test_id_attr) if spec.within else root
    if spec.testid is not None:
        loc = scope.get_by_test_id(spec.testid)
    elif spec.role is not None:
        loc = scope.get_by_role(spec.role, name=spec.name) if spec.name else scope.get_by_role(spec.role)
    elif spec.label is not None:
        loc = scope.get_by_label(spec.label)
    elif spec.placeholder is not None:
        loc = scope.get_by_placeholder(spec.placeholder)
    elif spec.text is not None:
        loc = scope.get_by_text(spec.text)
    elif spec.css is not None:
        loc = scope.locator(spec.css)
    else:  # pragma: no cover - guarded by model validation
        raise PlaywrightError(f"locator has no strategy: {spec!r}")
    if spec.nth is not None:
        loc = loc.nth(spec.nth)
    return loc


def _flash(page: Any, loc: Any) -> None:
    try:
        handle = loc.first.element_handle(timeout=1500)
        if handle:
            page.evaluate(_HIGHLIGHT_JS, handle)
            page.wait_for_timeout(350)
    except Exception:  # noqa: BLE001 - highlight is best-effort cosmetic
        pass


def run_flow(
    profile: ProjectProfile,
    flow: FlowSpec,
    *,
    out_dir: Path,
    storage_state: str | None,
    durations: dict[int, float] | None = None,
    default_dwell: float = 2.5,
    default_timeout_ms: int = 20000,
    headless: bool = True,
    highlight: bool = True,
) -> RunResult:
    """Run ``flow`` and record it. ``durations`` maps a global step index to the
    seconds of narration synthesized for that step (audio-driven pacing)."""
    durations = durations or {}
    test_id_attr = profile.selectors.test_id_attribute
    locale = flow.locale or profile.default_locale
    vp = flow.viewport or profile.viewport
    viewport = {"width": vp.width, "height": vp.height}

    videos_dir = out_dir / "video"
    videos_dir.mkdir(parents=True, exist_ok=True)

    sync_playwright = _import_playwright()
    result = RunResult(video_path=Path())
    ref = {"t0": time.time()}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            viewport=viewport,
            storage_state=storage_state if storage_state and Path(storage_state).exists() else None,
            record_video_dir=str(videos_dir),
            record_video_size=viewport,
        )
        ctx.set_default_timeout(default_timeout_ms)
        page = ctx.new_page()
        ref["t0"] = time.time()  # video starts ~here; anchor the narration timeline

        def now() -> float:
            return time.time() - ref["t0"]

        try:
            for gi, (task, st) in enumerate(flow.flat_steps()):
                # Best-effort: a single flaky step must not abort a long multi-beat
                # recording. A failed step is skipped WITHOUT its narration hold, so a
                # broken beat stays short instead of lingering on the wrong screen.
                failed = False
                try:
                    _run_step(page, profile, st, locale, test_id_attr, highlight)
                    if st.settle_ms:
                        page.wait_for_timeout(st.settle_ms)
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    logger.warning(
                        "step %d (%s) failed; skipping: %s",
                        gi, st.action, str(exc).splitlines()[0],
                    )

                start = now()
                hold = 0.0
                if not failed:
                    if st.say:
                        hold = max(durations.get(gi, default_dwell), st.min_duration_sec)
                    elif st.min_duration_sec:
                        hold = st.min_duration_sec
                    if hold:
                        page.wait_for_timeout(int(hold * 1000))

                result.timings.append(
                    StepTiming(gi, st.action, task, st.say, round(start, 3), round(hold, 3))
                )
                logger.info(
                    "step %2d %-9s start=%6.2fs hold=%4.1fs %s",
                    gi, st.action, start, hold, (st.say or "")[:60],
                )
        except Exception as exc:  # noqa: BLE001 - unexpected fatal (page crash, etc.)
            ctx.close()
            browser.close()
            raise PlaywrightError(f"Recording failed: {exc}") from exc

        result.total_sec = now()
        page.wait_for_timeout(300)
        video = page.video
        ctx.close()  # video is flushed to disk on context close
        browser.close()
        if video:
            src = Path(video.path())
            dst = out_dir / "recording.webm"
            if src.exists():
                src.replace(dst)
                result.video_path = dst
    if not result.video_path.exists():
        raise PlaywrightError("Recording produced no video file.")
    return result


_QUIESCE_JS = (
    "() => document.querySelectorAll("
    "'button,a[href],input,textarea,select,[role],[data-testid],h1,h2,h3').length"
)
# Content can arrive in waves with multi-second gaps (this app's search page
# plateaus ~4s, then a second xhr triples the DOM and adds the real filter
# panel). Read the page only once the interactive-element count has held steady
# LONGER than that gap — spinner-hidden and networkidle both fire inside the gap.
_QUIESCE_STEP_MS = 750
_QUIESCE_STABLE = 6            # ~4.5s of no change before we call it settled
_QUIESCE_CAP_MS = 16000


def settle(page: Any) -> None:
    """Wait until the page stops rendering (adaptive DOM quiescence) before it is
    read or acted on, so neither the digest nor a click hits a half-loaded screen.
    Static pages settle in ~4.5s; progressively-rendered ones wait for their last
    wave. Bounded to ~16s."""
    prev, stable, waited = -1, 0, 0
    while waited < _QUIESCE_CAP_MS:
        page.wait_for_timeout(_QUIESCE_STEP_MS)
        waited += _QUIESCE_STEP_MS
        try:
            count = int(page.evaluate(_QUIESCE_JS))
        except Exception:  # noqa: BLE001 - navigating; stop settling
            break
        if count == prev and count > 0:
            stable += 1
            if stable >= _QUIESCE_STABLE:
                break
        else:
            stable = 0
        prev = count


def goto_url(profile: ProjectProfile, path: str, locale: str) -> str:
    """Resolve a goto target forgivingly.

    Accepts a full URL (used verbatim), a locale-free path (the documented
    convention), or a path that already carries the locale prefix — hints often
    contain full URLs like http://host/en/search, and models copy them; naive
    prefixing would produce /en/en/search."""
    if path.startswith(("http://", "https://")):
        return path
    prefix = profile.route_prefix.replace("{locale}", locale)
    if prefix and (path == prefix or path.startswith((prefix + "/", prefix + "?"))):
        path = path[len(prefix):] or "/"
    return profile.route(path, locale)


def _run_step(  # noqa: PLR0912
    page: Any, profile: ProjectProfile, st: Step, locale: str, test_id_attr: str, highlight: bool
) -> None:
    loc = _resolve(page, st.locator, test_id_attr) if st.locator else None
    action = st.action

    if action == "goto":
        assert st.path is not None
        page.goto(goto_url(profile, st.path, locale), wait_until="domcontentloaded")
        settle(page)  # wait for progressively-rendered content before the next step
    elif action == "click":
        assert loc is not None
        loc.scroll_into_view_if_needed()
        if highlight:
            _flash(page, loc)
        loc.click()
    elif action == "fill":
        assert loc is not None and st.value is not None
        loc.scroll_into_view_if_needed()
        loc.fill(str(expand_env(st.value)))
    elif action == "press":
        assert st.keys is not None
        (loc or page.keyboard).press(st.keys) if loc else page.keyboard.press(st.keys)
    elif action == "select":
        assert loc is not None and st.value is not None
        loc.select_option(str(expand_env(st.value)))
    elif action == "hover":
        assert loc is not None
        loc.hover()
    elif action in ("scroll_to", "highlight"):
        assert loc is not None
        loc.first.scroll_into_view_if_needed()
        if highlight:
            _flash(page, loc)
    elif action in ("wait_for", "expect"):
        assert loc is not None
        loc.first.wait_for(state=st.state)
    elif action == "dwell":
        pass
    else:  # pragma: no cover - guarded by model validation
        raise PlaywrightError(f"unknown action: {action}")


def _looks_signed_in(profile: ProjectProfile, url: str) -> bool:
    """True when a URL is inside the app and past the auth gate: on the app's own
    host (not an external IdP), not the sign-in path, not an OAuth callback."""
    from urllib.parse import urlparse  # noqa: PLC0415

    base_host = urlparse(profile.base_url).netloc
    parsed = urlparse(url)
    if parsed.netloc != base_host:
        return False
    if profile.auth.sign_in_path and profile.auth.sign_in_path in url:
        return False
    return "/callback" not in parsed.path


def capture_session_interactive(
    profile: ProjectProfile, *, out_state: Path, timeout_sec: int = 300
) -> Path:
    """OAuth-consent-style manual login: open a VISIBLE browser window on this
    machine, let the user complete the app's real login (MFA included), then
    capture the session and close the window. Credentials never pass through
    demo-narrator — only the resulting browser session is stored."""
    sync_playwright = _import_playwright()
    out_state.parent.mkdir(parents=True, exist_ok=True)
    start_path = profile.auth.sign_in_path or "/"
    captured = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=_launch_args())
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto(profile.route(start_path), wait_until="domcontentloaded", timeout=30000)
            logger.info(
                "manual-login window open for %s — waiting up to %ds for the user to sign in",
                profile.name, timeout_sec,
            )
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                try:
                    if _looks_signed_in(profile, page.url):
                        page.wait_for_timeout(1500)  # let post-login redirects/cookies settle
                        if _looks_signed_in(profile, page.url):
                            ctx.storage_state(path=str(out_state))
                            captured = True
                            break
                    page.wait_for_timeout(1000)
                except Exception:  # noqa: BLE001 - user closed the window / page navigating
                    break
        finally:
            browser.close()

    if not captured:
        raise PlaywrightError(
            f"Manual login was not completed within {timeout_sec}s (or the window was "
            "closed early). Start it again when you're ready."
        )
    logger.info("manual login captured for %s -> %s", profile.name, out_state)
    return out_state


def session_is_valid(profile: ProjectProfile, storage_state: str, *, headless: bool = True) -> bool:
    """Open the app with the stored session; valid iff we are not bounced to the
    sign-in gate (profile.auth.sign_in_path)."""
    sync_playwright = _import_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(storage_state=storage_state)
        page = ctx.new_page()
        try:
            page.goto(profile.route("/"), wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            gated = bool(profile.auth.sign_in_path) and profile.auth.sign_in_path in page.url
        except Exception as exc:  # noqa: BLE001 - unreachable app is not a session problem
            raise PlaywrightError(
                f"Could not reach {profile.base_url} to validate the session: "
                f"{str(exc).splitlines()[0]}"
            ) from exc
        finally:
            ctx.close()
            browser.close()
    return not gated


def ensure_session(profile: ProjectProfile, *, headless: bool = True) -> str | None:
    """Return a storage-state path holding a VALID session, refreshing it via the
    profile's login recipe when missing or expired.

    This is the preflight every generate/record job runs, so the generator and
    the recorder never face a login form (the model must never type credentials).
    """
    if profile.auth.strategy != "storage_state":
        return profile.auth.storage_state
    ss = profile.auth.storage_state
    if not ss:
        raise PlaywrightError(
            f"Profile {profile.name!r} has no auth.storage_state path configured."
        )
    if Path(ss).exists() and session_is_valid(profile, ss, headless=headless):
        return ss

    if profile.auth.login_recipe is None:
        raise PlaywrightError(
            f"The session for {profile.name!r} is missing or expired and the profile has "
            "no auth.login_recipe to refresh it automatically.\n"
            f"Capture one manually:  demo-narrator demo-auth --project {profile.name} --show-browser"
        )
    logger.info("session for %s missing/expired — running the login recipe", profile.name)
    capture_session(profile, out_state=Path(ss), headless=headless)
    if not session_is_valid(profile, ss, headless=headless):
        raise PlaywrightError(
            "The login recipe completed but the app still shows its sign-in gate. "
            "Check E2E credentials in .env and that the recipe matches the current login page."
        )
    logger.info("session refreshed for %s", profile.name)
    return ss


def capture_session(
    profile: ProjectProfile, *, out_state: Path, headless: bool = True, timeout_ms: int = 45000
) -> Path:
    """Run the profile's login recipe and save an authenticated storageState."""
    recipe = profile.auth.login_recipe
    if recipe is None:
        raise PlaywrightError(
            f"Profile {profile.name!r} has no auth.login_recipe; cannot capture a session "
            "with credentials. Provide auth.storage_state instead."
        )
    creds = recipe.credentials_env
    if creds:
        for var in (creds.username, creds.password):
            if not os.environ.get(var):
                raise PlaywrightError(
                    f"Environment variable {var} is not set (needed for {profile.name!r} login). "
                    "Add it to your .env."
                )

    sync_playwright = _import_playwright()
    locale = profile.default_locale
    test_id_attr = profile.selectors.test_id_attribute
    out_state.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_launch_args())
        ctx = browser.new_context(viewport={"width": 1280, "height": 720})
        ctx.set_default_timeout(timeout_ms)
        page = ctx.new_page()
        try:
            for st in recipe.steps:
                _run_step(page, profile, st, locale, test_id_attr, highlight=False)
                if st.settle_ms:
                    page.wait_for_timeout(st.settle_ms)
            page.wait_for_timeout(1500)
            ctx.storage_state(path=str(out_state))
        except Exception as exc:  # noqa: BLE001
            raise PlaywrightError(f"Login recipe failed: {exc}") from exc
        finally:
            ctx.close()
            browser.close()
    return out_state
