"""Intro / outro title cards, rendered as styled HTML via Playwright and turned
into narrated video segments that bookend the demo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .runner import _import_playwright

_ACCENT = "#7b2ff7"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def card_html(kicker: str, title: str, items: list[str], *, closing: bool = False) -> str:
    """A dark, premium title card. `items` are the features being demonstrated."""
    if closing:
        list_html = "".join(f"<li>{_escape(i)}</li>" for i in items)
        body = f'<ul class="recap">{list_html}</ul>' if items else ""
    else:
        list_html = "".join(
            f'<li><span class="num">{n}</span>{_escape(i)}</li>' for n, i in enumerate(items, 1)
        )
        body = f'<ol class="features">{list_html}</ol>' if items else ""
    return f"""<div class="card">
  <div class="kicker">{_escape(kicker)}</div>
  <h1>{_escape(title)}</h1>
  {body}
</div>
<style>
  * {{ margin: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; }}
  .card {{
    width: 100vw; height: 100vh; padding: 96px 120px;
    display: flex; flex-direction: column; justify-content: center;
    background: radial-gradient(1200px 600px at 15% 10%, #2a1b4a 0%, #140d24 55%, #0b0715 100%);
    color: #fff;
  }}
  .kicker {{ color: {_ACCENT}; font-weight: 700; letter-spacing: .28em;
             text-transform: uppercase; font-size: 26px; margin-bottom: 28px; filter: brightness(1.5); }}
  h1 {{ font-size: 76px; font-weight: 800; line-height: 1.05; max-width: 16ch; }}
  ol.features, ul.recap {{ margin-top: 56px; list-style: none; display: flex;
                           flex-direction: column; gap: 22px; }}
  ol.features li {{ font-size: 40px; font-weight: 600; display: flex; align-items: center; gap: 24px; }}
  .num {{ display: inline-flex; align-items: center; justify-content: center;
          width: 56px; height: 56px; border-radius: 50%; background: {_ACCENT};
          font-size: 30px; font-weight: 800; flex: none; }}
  ul.recap li {{ font-size: 38px; font-weight: 600; opacity: .92; }}
  ul.recap li::before {{ content: "✓"; color: {_ACCENT}; font-weight: 800; margin-right: 20px; filter: brightness(1.6); }}
</style>"""


def render_card(html: str, out_png: Path, *, width: int, height: int) -> Path:
    """Render a card's HTML to a PNG with Playwright (Chromium)."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    sync_playwright = _import_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": width, "height": height})
        page = ctx.new_page()
        page.set_content(html, wait_until="networkidle")
        page.wait_for_timeout(200)
        page.screenshot(path=str(out_png))
        browser.close()
    return out_png


def intro_narration(sprint: int | None, features: list[str]) -> str:
    n = len(features)
    where = f"Sprint {sprint}" if sprint else "this release"
    return f"Here's the {where} demo, walking through {n} new feature{'s' if n != 1 else ''} in the LCA tool."


def outro_narration(sprint: int | None, features: list[str]) -> str:
    n = len(features)
    where = f"Sprint {sprint}" if sprint else "this release"
    return f"That's {n} feature{'s' if n != 1 else ''} from {where}. Thanks for watching."


def intro_card_html(sprint: int | None, features: list[str]) -> Any:
    kicker = (f"Sprint {sprint} · " if sprint else "") + "Product demo"
    return card_html(kicker, "What's new in NBS LCA", features)


def outro_card_html(sprint: int | None, features: list[str]) -> Any:
    kicker = (f"Sprint {sprint} · " if sprint else "") + "That's a wrap"
    return card_html(kicker, "Thanks for watching", features, closing=True)


def section_card_html(index: int, total: int, title: str) -> Any:
    return card_html(f"Feature {index + 1} of {total}", title, [])


def section_narration(index: int, total: int, title: str) -> str:
    if index == 0:
        lead = "First up"
    elif index == total - 1:
        lead = "Finally"
    else:
        lead = "Next"
    return f"{lead}: {title}."
