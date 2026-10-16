"""Drive the real pages in a headless browser: they load without script errors and the main interactions work.

Marked ``browser``; skipped when Playwright or a Chromium-family browser isn't available.
"""

import threading
from pathlib import Path

import pytest
from conftest import PROFILE_PATH, REPO, SLUG

from readthrough.build import build
from readthrough.config import Config
from readthrough.serve import Dashboard, DashboardServer

sync_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser

BODY = 'Adds **retries** to the transport.\n\n- one\n- two\n\n<img src="data:," onerror="window.pwned=1">'


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        launched = None
        for options in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                launched = p.chromium.launch(headless=True, **options)
                break
            except sync_api.Error:
                continue
        if launched is None:
            pytest.skip("no Chromium-family browser available")
        yield launched
        launched.close()


@pytest.fixture
def server(tmp_path: Path, monkeypatch, make_pr, fake_forge):
    fake_forge.prs = [
        make_pr(number=1, body=BODY),
        make_pr(number=2, title="FEAT: other", author={"login": "me"}),
        make_pr(number=3, title="DOC: draft", isDraft=True),
    ]
    fake_forge.total = 40  # more open than fetched, so the "limit" banner shows
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot"))
    monkeypatch.setattr("readthrough.build.registered_protocol_command", lambda: None)
    cfg = Config(home=tmp_path / "home", repo=REPO, onboarded="2026-10-01T00:00:00+00:00")
    cfg.home.mkdir(parents=True)
    (cfg.home / "config.toml").write_text(
        f'[[repos]]\nname = "{REPO}"\nonboarded = "{cfg.onboarded}"\n', encoding="utf-8"
    )
    cfg.local_profile_path.parent.mkdir(parents=True)
    cfg.local_profile_path.write_text(PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    build(cfg, with_diffs=False)
    dashboard = Dashboard(cfg, with_diffs=False)
    assert dashboard.load()
    httpd = DashboardServer([dashboard], 0)
    dashboard.token = httpd.token
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def page(browser, server):
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: errors.append(f"console: {msg.text}") if msg.type == "error" else None)
    page.errors = errors  # type: ignore[attr-defined]
    page.base = server  # type: ignore[attr-defined]
    yield page
    context.close()


def open_dashboard(page):
    page.goto(f"{page.base}/r/{SLUG}/")
    page.wait_for_selector("#v-map.on")


def rows(page) -> list[int]:
    return [
        int(n) for n in page.eval_on_selector_all("#v-table tbody [data-open]", "els => els.map(e => e.dataset.open)")
    ]


def test_every_view_renders_without_script_errors(page):
    open_dashboard(page)
    for tab in ("plan", "overlap", "table", "map"):
        page.click(f'[data-tab="{tab}"]')
        page.wait_for_selector(f"#v-{tab}.on")
    assert page.errors == []


def test_warnings_from_the_build_are_shown(page):
    open_dashboard(page)
    assert "most recently active of 40" in page.inner_text("#banners")


def test_lane_pills_select_only_then_restore(page):
    open_dashboard(page)
    page.click('[data-tab="table"]')
    assert sorted(rows(page)) == [1, 2, 3]
    page.click('#lanes [data-lane="draft"]')
    assert rows(page) == [3]
    page.click('#lanes [data-lane="draft"]')
    assert sorted(rows(page)) == [1, 2, 3]
    # Ctrl-click is a right-click on macOS; the page accepts Ctrl, Cmd, or Shift as "add or remove one".
    page.click('#lanes [data-lane="draft"]', modifiers=["ControlOrMeta"])
    assert sorted(rows(page)) == [1, 2]
    assert page.errors == []


def test_the_drawer_renders_the_description_safely(page):
    open_dashboard(page)
    page.click('[data-tab="table"]')
    page.click('#v-table [data-open="1"]')
    page.wait_for_selector("body.drawer-open")
    html = page.inner_html("#drawer")
    assert "<strong>retries</strong>" in html and "<li>one</li>" in html
    assert "onerror" not in html
    assert page.evaluate("window.pwned") is None
    page.click("#close")
    page.wait_for_selector("body:not(.drawer-open)")
    assert page.errors == []


def test_theme_button_cycles_and_remembers(page):
    open_dashboard(page)
    seen = set()
    for _ in range(3):
        page.click("#theme")
        seen.add(page.evaluate("document.documentElement.dataset.theme"))
    assert {"light", "dark"} <= seen
    stored = page.evaluate("localStorage.getItem('readthrough:theme')")
    page.reload()
    page.wait_for_selector("#v-map.on")
    assert page.evaluate("localStorage.getItem('readthrough:theme')") == stored


def test_setup_page_loads_and_lists_the_repo(page):
    page.goto(f"{page.base}/setup")
    page.wait_for_selector("#nav .ritem")
    assert REPO in page.inner_text("#nav")
    assert page.errors == []


def test_pages_have_no_unfilled_placeholders(page):
    for path in (f"/r/{SLUG}/", "/setup"):
        page.goto(page.base + path)
        assert "/*__READTHROUGH_" not in page.content()
