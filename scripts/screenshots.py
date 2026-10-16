"""Take the documentation screenshots from a repo you've built, as WebP.

    uv run python scripts/screenshots.py --repo microsoft/vscode            # from the repo's last build
    uv run python scripts/screenshots.py --repo microsoft/vscode --build    # build first
    uv run python scripts/screenshots.py --repo microsoft/vscode --pr 123   # which PR the detail and study shots show

Serves the repo's existing data through the live server on a free port (no network unless --build), drives
a headless Edge or Chrome through Playwright at a fixed size in the light theme, and writes docs/img/*.webp:
the map, the plan, the overlap graph, a PR's detail panel, and its Study tab. WebP keeps the pages crisp at
a third of the PNG size. Pillow (dev group) does the conversion; Playwright can only write PNG and JPEG.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import threading
from pathlib import Path

from PIL import Image
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import ViewportSize, sync_playwright

from readthrough.build import build
from readthrough.config import load_settings
from readthrough.serve import Dashboard, DashboardServer

OUT = Path(__file__).resolve().parents[1] / "docs" / "img"
SIZE: ViewportSize = {"width": 1360, "height": 860}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", help="which repo (default: the default repo)")
    parser.add_argument("--build", action="store_true", help="build the repo first (needs the forge)")
    parser.add_argument("--pr", type=int, help="the PR for the detail and study shots (default: the first ready one)")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--quality", type=int, default=80, help="WebP quality (default 80)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = load_settings()
    cfg = settings.config_for(args.repo)
    if args.build:
        build(cfg)
    dashboard = Dashboard(cfg, with_diffs=False)
    if not dashboard.load():
        print(f"No build for {cfg.repo} yet; run with --build or `readthrough build` first.", file=sys.stderr)
        return 1
    httpd = DashboardServer([dashboard], 0)
    dashboard.token = httpd.token
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/r/{cfg.slug}"
    payload = dashboard.snapshot()
    number = args.pr or next((p["number"] for p in payload["prs"] if p["lane"] == "ready"), payload["prs"][0]["number"])
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = p.chromium.launch(headless=True, channel=channel)
                break
            except PlaywrightError:
                continue
        if browser is None:
            print("No Chromium-family browser found.", file=sys.stderr)
            return 1
        context = browser.new_context(viewport=SIZE, color_scheme="light", device_scale_factor=1.5)
        page = context.new_page()
        page.goto(f"{base}/#map")
        page.wait_for_selector("#v-map.on")
        page.evaluate("localStorage.setItem('readthrough:theme', 'light')")

        def shot(name: str) -> None:
            page.wait_for_timeout(400)  # let the layout and the glass settle
            png = page.screenshot(type="png")
            image = Image.open(io.BytesIO(png)).convert("RGB")
            target = args.out / f"{name}.webp"
            image.save(target, "WEBP", quality=args.quality, method=6)
            print(f"{target.name}: {target.stat().st_size // 1024} KB")

        for tab in ("map", "plan", "overlap"):
            page.click(f'[data-tab="{tab}"]')
            page.wait_for_selector(f"#v-{tab}.on")
            shot(tab)
        page.click('[data-tab="table"]')
        page.wait_for_selector("#v-table.on")
        page.click(f'#v-table [data-open="{number}"]')
        page.wait_for_selector("body.drawer-open")
        shot("review")
        page.click('[data-dtab="study"]')
        page.wait_for_selector(".transcript")
        shot("study")
        browser.close()
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
