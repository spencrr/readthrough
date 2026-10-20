from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from readthrough import agents, review
from readthrough import serve as serve_mod
from readthrough.config import Config, Settings
from readthrough.limits import MAX_REQUEST_BYTES
from readthrough.serve import Dashboard, DashboardServer

HEAD = "a" * 40
R = "/r/github/o/r"


@pytest.fixture
def live(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot"))
    cfg = Config(home=tmp_path / "home", repo="o/r", clone=tmp_path)
    cfg.repo_dir.mkdir(parents=True)
    payload = {
        "repo": "o/r",
        "generated": "2026-10-02T00:00:00+00:00",
        "prs": [{"number": 7, "head": HEAD}],
        "ledger": {},
    }
    cfg.data_path.write_text(json.dumps(payload), encoding="utf-8")
    dashboard = Dashboard(cfg, with_diffs=False)
    assert dashboard.load()
    httpd = DashboardServer([dashboard], 0)
    dashboard.token = httpd.token
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield dashboard, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _request(port: int, path: str, *, method: str = "GET", body: dict | None = None, headers: dict | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_health_and_page_carry_token(live):
    dashboard, port = live
    assert _request(port, "/api/health")[0] == 200
    status, html = _request(port, "/")
    assert status == 200
    assert dashboard.token.encode() in html


def test_rejects_foreign_host(live):
    _, port = live
    assert _request(port, R + "/api/data", headers={"Host": "evil.example"})[0] == 403


def test_post_requires_token_and_same_origin(live):
    dashboard, port = live
    assert _request(port, R + "/api/ledger/7", method="POST", body={"status": "reviewed"})[0] == 403
    token = {"X-Readthrough-Token": dashboard.token}
    bad_origin = {**token, "Origin": "https://evil.example"}
    assert _request(port, R + "/api/ledger/7", method="POST", body={"status": "reviewed"}, headers=bad_origin)[0] == 403
    assert _request(port, R + "/api/ledger/7", method="POST", body={"status": "lgtm"}, headers=token)[0] == 400


def test_ledger_round_trip_broadcasts(live):
    dashboard, port = live
    events = dashboard.subscribe()
    token = {"X-Readthrough-Token": dashboard.token, "Origin": f"http://127.0.0.1:{port}"}

    status, body = _request(port, R + "/api/ledger/7", method="POST", body={"status": "reviewed"}, headers=token)
    assert status == 200
    assert json.loads(body)["7"]["head"] == HEAD
    assert json.loads(dashboard.cfg.ledger_path.read_text())["7"]["status"] == "reviewed"
    assert events.get(timeout=2)[0] == "ledger"

    _request(port, R + "/api/ledger/7", method="POST", body={"status": None}, headers=token)
    assert "7" not in json.loads(dashboard.cfg.ledger_path.read_text())


def test_external_changes_are_picked_up(live):
    dashboard, _ = live
    events = dashboard.subscribe()
    folder = agents.copilot_home() / "session-state" / review.default_session_id(dashboard.cfg, 7)
    folder.mkdir(parents=True)
    (folder / f"inuse.{os.getpid()}.lock").write_text("x")
    dashboard._watch_once()
    event, data = events.get(timeout=2)
    assert event == "sessions"
    assert data[7]["session"]["exists"] and data[7]["session"]["active"]
    assert [x["id"] for x in data[7]["sessions"]] == [data[7]["session"]["id"]]


def test_second_server_cannot_share_the_port(live):
    dashboard, port = live
    with pytest.raises(OSError):
        DashboardServer([dashboard], port)


def test_page_scripts_carry_the_csp_nonce(live):
    _, port = live
    req = urllib.request.Request(f"http://127.0.0.1:{port}/")
    with urllib.request.urlopen(req, timeout=5) as resp:
        csp = resp.headers["Content-Security-Policy"]
        html = resp.read().decode()
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert html.count(f'<script nonce="{nonce}">') == 2
    assert "__NONCE__" not in html and "/*__READTHROUGH_" not in html
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp


def test_study_endpoints(live):
    dashboard, port = live
    from readthrough import study

    token = {"X-Readthrough-Token": dashboard.token}
    status, body = _request(
        port, R + "/api/study/7", method="POST", body={"kind": "prediction", "text": "Touches scorers"}, headers=token
    )
    assert status == 200 and json.loads(body)["kind"] == "prediction"
    assert (
        _request(port, R + "/api/study/7", method="POST", body={"kind": "question", "question": "q"}, headers=token)[0]
        == 400
    )
    assert (
        _request(port, R + "/api/study/999", method="POST", body={"kind": "note", "text": "x"}, headers=token)[0] == 400
    )
    # Azure DevOps numbers PRs per organization; an eight-digit id must reach the handler, not fall off the router.
    status, body = _request(port, R + "/api/study/17185872")
    assert status == 200 and json.loads(body)["entries"] == []
    assert json.loads(_request(port, R + "/api/brief/17185872")[1])["error"] == "No brief for #17185872"
    assert _request(port, R + "/api/brief/1234567890")[0] == 404, "ten digits is past any forge's numbering"

    study.append(dashboard.cfg, 7, "question", by="copilot", question="Why?", expected_points=["secret"])
    state = json.loads(_request(port, R + "/api/study/7")[1])
    assert [e["kind"] for e in state["entries"]] == ["prediction", "question"]
    assert "expected_points" not in state["entries"][1]
    assert state["summary"]["predictions"] == 1
    assert set(state["engine"]) == {"name", "available", "reason", "busy"}

    assert _request(port, R + "/api/study/7/ask", method="POST", body={"action": "dance"}, headers=token)[0] == 400
    reflection = {"kind": "reflection", "text": "Missed the retry path", "qid": "Q1"}
    status, body = _request(port, R + "/api/study/7", method="POST", body=reflection, headers=token)
    assert status == 200 and json.loads(body)["qid"] == "Q1"
    assert _request(port, R + "/api/study/7", method="POST", body={**reflection, "qid": 3}, headers=token)[0] == 400


def test_brief_endpoint(live):
    dashboard, port = live
    dashboard.cfg.briefs_dir.mkdir(parents=True, exist_ok=True)
    (dashboard.cfg.briefs_dir / "pr-7.md").write_text("# hello", encoding="utf-8")
    assert json.loads(_request(port, R + "/api/brief/7")[1])["markdown"] == "# hello"
    assert _request(port, R + "/api/brief/8")[0] == 404


def test_stale_lock_is_not_active(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path))
    folder = tmp_path / "session-state" / "abc"
    folder.mkdir(parents=True)
    (folder / "inuse.999999.lock").write_text("x")
    info = agents.Copilot().session_info("abc")
    assert info.exists and info.active is False


def test_routes_are_per_repo(live):
    _, port = live
    health = json.loads(_request(port, "/api/health")[1])
    assert health["repos"] == ["github/o/r"]
    assert _request(port, "/r/other/repo/api/data")[0] == 404
    assert _request(port, "/api/data")[0] == 404
    assert json.loads(_request(port, R + "/api/data")[1])["repo"] == "o/r"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.url.endswith("/r/github/o/r/")


def test_skip_and_pin_through_the_api(live):
    dashboard, port = live
    token = {"X-Readthrough-Token": dashboard.token}
    status, body = _request(
        port, R + "/api/study/7", method="POST", body={"kind": "prediction", "skipped": True}, headers=token
    )
    assert status == 200 and json.loads(body)["skipped"] is True
    status, body = _request(
        port, R + "/api/study/7", method="POST", body={"kind": "note", "text": "keep", "pinned": 1}, headers=token
    )
    assert status == 200 and json.loads(body)["pinned"] == 1
    bad = {"kind": "note", "text": "keep", "pinned": "1; drop"}
    assert _request(port, R + "/api/study/7", method="POST", body=bad, headers=token)[0] == 400


def test_setup_page_and_api_are_protected(live):
    dashboard, port = live
    req = urllib.request.Request(f"http://127.0.0.1:{port}/setup")
    with urllib.request.urlopen(req, timeout=5) as resp:
        csp, html = resp.headers["Content-Security-Policy"], resp.read().decode()
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert html.count(f'<script nonce="{nonce}">') == 2 and dashboard.token in html and "/*__READTHROUGH_" not in html
    body = {"global": {"refresh_minutes": 5}}
    assert _request(port, "/api/setup/settings", method="POST", body=body)[0] == 403
    assert _request(port, "/api/setup/settings", method="POST", body=body, headers={"Host": "evil.example"})[0] == 403
    assert (
        _request(port, "/api/setup/nope", method="POST", body={}, headers={"X-Readthrough-Token": dashboard.token})[0]
        == 404
    )
    assert _request(port, "/api/setup/state", headers={"Host": "evil.example"})[0] == 403


def test_failed_refreshes_are_recorded_and_retried_sooner(live, monkeypatch):
    from readthrough import serve as serve_mod
    from readthrough.build import BuildFailed
    from readthrough.faults import NetworkError

    dashboard, port = live
    cause = NetworkError("graphql PullRequests failed: connection reset", stderr="wsarecv")

    def fail(cfg, **kwargs):
        raise BuildFailed(cfg.repo, "fetching PRs", cause, "e123", 1.5)

    monkeypatch.setattr(serve_mod, "build", fail)
    events = dashboard.subscribe()
    assert dashboard.rebuild() is False and dashboard.rebuild() is False
    err = dashboard.status["error"]
    assert (err["id"], err["step"], dashboard.status["consecutive_failures"]) == ("e123", "fetching PRs", 2)
    assert "connection reset" in err["message"]
    assert events.get(timeout=2)[0] == "status"
    retry = dashboard.cfg.tuning.server.retry_after_failure_minutes
    assert dashboard.next_wait() == pytest.approx(retry[1] * 60)
    health = json.loads(_request(port, "/api/health")[1])
    assert health["status"]["github/o/r"]["error"]["id"] == "e123"

    dashboard.status["consecutive_failures"] = 0
    interval = dashboard.cfg.refresh_minutes * 60
    jitter = dashboard.cfg.tuning.server.refresh_jitter
    assert interval * (1 - jitter) <= dashboard.next_wait() <= interval * (1 + jitter)


def test_http_errors_refresh_and_review_endpoint(live, monkeypatch):
    dashboard, port = live
    token = {"X-Readthrough-Token": dashboard.token}

    assert _request(port, "/nope")[0] == 404
    assert _request(port, R + "/api/nope", method="POST", body={}, headers=token)[0] == 404

    status, body = _request(port, R + "/api/refresh", method="POST", body={}, headers=token)
    assert status == 200 and json.loads(body) == {"ok": True}
    assert dashboard._wake.is_set()

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{R}/api/refresh",
        method="POST",
        data=b"{",
        headers={"Content-Type": "application/json", **token},
    )
    with pytest.raises(urllib.error.HTTPError) as bad_json:
        urllib.request.urlopen(req, timeout=5)
    assert bad_json.value.code == 400

    huge = {"x": "x" * MAX_REQUEST_BYTES}
    assert _request(port, R + "/api/refresh", method="POST", body=huge, headers=token)[0] == 413

    launched = []
    plan = SimpleNamespace(session_id="11111111-1111-1111-1111-111111111111", resuming=True, agent="copilot")
    monkeypatch.setattr(serve_mod.review, "plan_review", lambda *args, **kwargs: plan)
    monkeypatch.setattr(serve_mod.review, "launch_in_terminal", lambda launch, cfg: launched.append(launch) or "wt")
    assert _request(port, R + "/api/review/7", method="POST", body={"session": "bad"}, headers=token)[0] == 400
    status, body = _request(port, R + "/api/review/7", method="POST", body={"session": plan.session_id}, headers=token)
    assert status == 200 and json.loads(body) == {"session": plan.session_id, "resuming": True, "agent": "copilot"}
    assert launched == [plan]


def test_study_ask_accepts_and_reports_busy(live, monkeypatch):
    dashboard, port = live
    token = {"X-Readthrough-Token": dashboard.token}
    asked = []
    monkeypatch.setattr(dashboard, "ask", lambda number, body: asked.append((number, body["action"])))
    assert _request(port, R + "/api/study/7/ask", method="POST", body={"action": "quiz"}, headers=token)[0] == 202
    assert asked == [(7, "quiz")]

    monkeypatch.setattr(dashboard, "ask", lambda number, body: (_ for _ in ()).throw(serve_mod.Busy("already working")))
    status, body = _request(port, R + "/api/study/7/ask", method="POST", body={"action": "quiz"}, headers=token)
    assert status == 409 and json.loads(body)["error"] == "already working"


def test_server_alive_handles_good_bad_and_unreachable(monkeypatch):
    class Response:
        def __init__(self, payload: dict):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(self.payload).encode()

    monkeypatch.setattr(serve_mod.urllib.request, "urlopen", lambda *args, **kwargs: Response({"app": "readthrough"}))
    assert serve_mod.server_alive(8765) == {"app": "readthrough"}

    monkeypatch.setattr(serve_mod.urllib.request, "urlopen", lambda *args, **kwargs: Response({"app": "other"}))
    assert serve_mod.server_alive(8765) is None

    monkeypatch.setattr(serve_mod.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError))
    assert serve_mod.server_alive(8765) is None


def test_dashboard_rebuild_success_generic_failure_and_full_client_queue(live, monkeypatch):
    dashboard, _ = live
    full = dashboard.subscribe()
    for i in range(100):
        full.put_nowait(("old", i))
    dashboard.broadcast("data", {"generated": "new"})
    assert full not in dashboard._clients

    def build_success(cfg, **kwargs):
        cfg.data_path.write_text(
            json.dumps({"repo": "o/r", "generated": "new", "prs": [{"number": 7, "head": HEAD}]}),
            encoding="utf-8",
        )

    monkeypatch.setattr(serve_mod, "build", build_success)
    events = dashboard.subscribe()
    assert dashboard.rebuild() is True
    assert dashboard.status["last_success"] is not None
    assert events.get(timeout=2)[0] == "status"

    monkeypatch.setattr(serve_mod, "build", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(serve_mod.logs, "new_id", lambda: "e999")
    assert dashboard.rebuild() is False
    assert dashboard.status["error"]["id"] == "e999"
    assert dashboard.status["error"]["step"] == "loading the build"


def test_refresh_loop_uses_wake_reason_without_sleep(live, monkeypatch, caplog):
    dashboard, _ = live
    monkeypatch.setattr(dashboard, "next_wait", lambda: 60)
    monkeypatch.setattr(dashboard, "rebuild", lambda: dashboard.stop() or True)

    dashboard.request_refresh("manual")
    with caplog.at_level("INFO", logger="readthrough.serve"):
        dashboard.refresh_loop()
    assert "refreshing (manual)" in caplog.text


def test_config_sync_adds_removes_and_reload_watch(monkeypatch, tmp_path: Path):
    started = []
    stopped = []
    monkeypatch.setattr(Dashboard, "start", lambda self, **kwargs: started.append((self.cfg.repo, kwargs)))
    monkeypatch.setattr(Dashboard, "stop", lambda self: stopped.append(self.cfg.repo))

    first = Settings(home=tmp_path, default_repo="o/a", repos=[{"name": "o/a", "onboarded": "now"}])
    current_settings = [first]
    httpd = DashboardServer([], 0, settings_loader=lambda: current_settings[0], with_diffs=False)
    try:
        httpd.sync(first)
        assert httpd.repo_names() == [{"slug": "github/o/a", "name": "o/a"}]
        assert httpd.default_repo() == "github/o/a"
        assert "github/o/a" in httpd.repo_status()
        assert started == [("o/a", {"build_now": True})]
        assert httpd.route("/r/github/o/a/api/data") == (httpd.dashboard_for("github/o/a"), "/api/data")
        assert httpd.route("/r/GitHub/o/a") == (httpd.dashboard_for("github/o/a"), "/")
        assert httpd.route("/r/o/a/api/data") == (None, "/r/o/a/api/data"), "routes need the full slug"

        second = Settings(home=tmp_path, default_repo="o/b", repos=[{"name": "o/b", "onboarded": "now"}])
        httpd.sync(second)
        assert httpd.repo_names() == [{"slug": "github/o/b", "name": "o/b"}]
        assert stopped == ["o/a"]
        assert started[-1] == ("o/b", {"build_now": True})

        # An Azure DevOps project with spaces in its name: the browser sends them percent-encoded.
        ado = Settings(
            home=tmp_path, repos=[{"name": "contoso/Delivery Team/Courier-Ops", "forge": "ado", "onboarded": "now"}]
        )
        httpd.sync(ado)
        dashboard = httpd.dashboard_for("ado/contoso/Delivery Team/Courier-Ops")
        assert (
            dashboard is not None
            and dashboard.cfg.repo_dir == tmp_path / "repos" / "ado" / "contoso" / "Delivery Team" / "Courier-Ops"
        )
        assert httpd.route("/r/ado/contoso/Delivery%20Team/Courier-Ops/api/data") == (dashboard, "/api/data")
        assert httpd.route("/r/ado/contoso/Delivery Team/Courier-Ops/") == (dashboard, "/")

        second.save()
        current_settings[0] = second
        reloaded = []
        stop = threading.Event()
        monkeypatch.setattr(httpd, "sync", lambda settings: (reloaded.append(settings), stop.set()))
        monkeypatch.setattr(httpd, "_watch_seconds", 0.01)
        httpd.watch_config(stop)
        assert reloaded == [second]
    finally:
        httpd.server_close()


@pytest.mark.parametrize("content", ['{"schema": 99, "prs": []}', "{not json"])
def test_unreadable_or_newer_data_is_rebuilt_not_misread(tmp_path: Path, monkeypatch, caplog, content):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot"))
    cfg = Config(home=tmp_path, repo="o/r")
    cfg.repo_dir.mkdir(parents=True)
    cfg.data_path.write_text(content, encoding="utf-8")
    dashboard = Dashboard(cfg, with_diffs=False)
    assert dashboard.load() is False
    assert "it will be rebuilt" in caplog.text


def test_a_malformed_length_is_a_bad_request(live):
    import http.client

    _, port = live
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", R + "/api/refresh")
    conn.putheader("Content-Length", "lots")
    conn.endheaders()
    assert conn.getresponse().status == 400
    conn.close()
