"""Lightweight app inspection for the setup assistant.

Opens the app (headless, with the stored session if any), visits a few paths,
and reports what it sees — final URL after redirects, whether it looks signed in,
navigation links, page sections, and a sample of controls. The assistant uses
this to propose a real `feature_map` and to detect the app's sign-in path.

This is deliberately read-only and shallow: it never clicks or mutates anything.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .profile import ProjectProfile
from .runner import _import_playwright, _looks_signed_in, goto_url, settle

logger = logging.getLogger(__name__)

# One read-only sweep of the current page: nav/menu links (text + same-origin
# path), section headings, and a small sample of named controls. Kept compact —
# it is fed to the model verbatim.
_INSPECT_JS = r"""
() => {
  function clean(t) { return (t || '').trim().replace(/\s+/g, ' ').slice(0, 60); }
  const origin = location.origin;
  const links = [];
  const seenHref = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const r = a.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;
    let href = a.href;
    if (!href.startsWith(origin)) continue;           // same-app links only
    const path = href.slice(origin.length) || '/';
    if (path.startsWith('#')) continue;
    const text = clean(a.innerText || a.getAttribute('aria-label'));
    if (!text) continue;
    const key = text + '|' + path;
    if (seenHref.has(key)) continue;
    seenHref.add(key);
    links.push({ text, path });
  }
  const sections = [];
  const seenSec = new Set();
  for (const h of document.querySelectorAll('h1,h2,h3,[role="heading"]')) {
    const r = h.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;
    const t = clean((h.innerText || '').split('\n')[0]);
    if (t && !seenSec.has(t)) { seenSec.add(t); sections.push(t); }
  }
  const controls = [];
  const seenCtl = new Set();
  for (const el of document.querySelectorAll('button,[role="button"],[role="tab"],input,select,textarea')) {
    const r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;
    const name = clean(el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || (el.innerText || '').split('\n')[0] || el.value);
    if (!name || seenCtl.has(name)) continue;
    seenCtl.add(name);
    controls.push(name);
  }
  return {
    title: clean(document.title),
    links: links.slice(0, 40),
    sections: sections.slice(0, 25),
    controls: controls.slice(0, 25),
  };
}
"""


def inspect_app(
    profile: ProjectProfile,
    paths: list[str],
    *,
    storage_state: str | None,
    on_progress: Any | None = None,
) -> list[dict[str, Any]]:
    """Visit each path (deduped, capped) and return a read-only observation each.

    `paths` are locale-free app paths (or full URLs); an empty list defaults to
    the app root. Never raises for a single bad page — records an error instead.
    """
    sync_playwright = _import_playwright()
    locale = profile.default_locale
    vp = profile.viewport
    # Always start from the root; dedupe while preserving order; keep it cheap.
    ordered: list[str] = []
    for p in ["/", *paths]:
        if p not in ordered:
            ordered.append(p)
    ordered = ordered[:6]

    use_state = storage_state if storage_state and Path(storage_state).exists() else None
    observations: list[dict[str, Any]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": vp.width, "height": vp.height}, storage_state=use_state
        )
        ctx.set_default_timeout(15000)
        page = ctx.new_page()
        try:
            for path in ordered:
                obs: dict[str, Any] = {"path": path}
                try:
                    if on_progress:
                        on_progress("onboard", f"inspecting {path}")
                    page.goto(goto_url(profile, path, locale), wait_until="domcontentloaded")
                    settle(page)
                    data = page.evaluate(_INSPECT_JS)
                    obs.update(data)
                    obs["final_url"] = page.url
                    obs["final_path"] = urlparse(page.url).path or "/"
                    obs["signed_in"] = _looks_signed_in(profile, page.url)
                except Exception as exc:  # noqa: BLE001 - one bad page shouldn't abort the sweep
                    obs["error"] = str(exc).splitlines()[0]
                observations.append(obs)
        finally:
            ctx.close()
            browser.close()
    return observations


def format_observations(observations: list[dict[str, Any]]) -> str:
    """Render observations as compact text for the model prompt."""
    if not observations:
        return "(the app was not inspected this turn)"
    out: list[str] = []
    for obs in observations:
        if obs.get("error"):
            out.append(f'PAGE {obs["path"]} — could not load: {obs["error"]}')
            continue
        redirected = obs.get("final_path") and obs["final_path"] not in (obs["path"], "/")
        gate = "SIGNED IN" if obs.get("signed_in") else "NOT signed in (sign-in gate)"
        head = f'PAGE {obs["path"]} → {obs.get("final_path", "?")} [{gate}]'
        if redirected and obs["path"] == "/":
            head += "  (root redirected here)"
        out.append(head)
        if obs.get("title"):
            out.append(f'  title: {obs["title"]}')
        links = obs.get("links") or []
        if links:
            out.append("  links:")
            out += [f'    - "{lk["text"]}" -> {lk["path"]}' for lk in links[:30]]
        if obs.get("sections"):
            out.append("  sections: " + ", ".join(obs["sections"][:20]))
        if obs.get("controls"):
            out.append("  controls: " + ", ".join(obs["controls"][:20]))
    return "\n".join(out)
