"""The live dashboard: a localhost server that keeps every onboarded repo fresh and pushes changes to the page.

Each repo lives under ``/r/<forge>/<owner>/<name>/`` (its slug: ``/r/github/microsoft/vscode/``) and gets the
same single-file dashboard as a static build, plus a small API (paths relative to the repo prefix):

    GET  /                    the dashboard
    GET  /api/data            the current payload
    GET  /api/events          Server-Sent Events: data, ledger, sessions, status, study
    GET  /api/study/<n>       the PR's study log;  GET /api/brief/<n>  its brief
    POST /api/refresh         rebuild now
    POST /api/review/<n>      start or resume the Copilot review session for a PR
    POST /api/ledger/<n>      {"status": "understood" | "reviewed" | ... | null}
    POST /api/study/<n>[/ask] add to the study log, or ask the study assistant

``GET /api/health`` (no prefix) is the liveness probe; ``/`` redirects to the default repo, or to ``/setup``
when nothing is onboarded yet. ``/setup`` is browser onboarding and repo settings; its API lives under
``/api/setup/`` (see web_setup.py).

Only 127.0.0.1 is bound. Requests must carry a matching Host header (blocks DNS rebinding), and
POSTs must carry the per-run token embedded in the page plus a same-origin Origin, so other sites
in your browser can't drive it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import queue
import random
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from readthrough import __version__, ledger, logs, review, study
from readthrough.build import DATA_SCHEMA, BuildFailed, build, mark_seen, refresh_sessions, render_html, render_setup
from readthrough.config import DEFAULT_HOME, Config, load_settings
from readthrough.engine import Busy, StudyEngine, public_entries
from readthrough.files import mtime
from readthrough.limits import MAX_DISCARD_BYTES, MAX_REQUEST_BYTES
from readthrough.web_setup import Conflict, SetupError, SetupService

log = logging.getLogger(__name__)


class BodyTooLarge(ValueError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def page_csp(nonce: str) -> str:
    """Only our own nonce'd scripts run. PR descriptions are third-party HTML, so this matters."""
    return (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src data: https:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def repo_url(port: int, slug: str) -> str:
    return f"{base_url(port)}r/{slug}/"


def server_url(cfg: Config) -> str:
    """The live page for this repo."""
    return repo_url(cfg.port, cfg.slug)


def server_alive(port: int, *, timeout: float = 0.5) -> dict[str, Any] | None:
    """The running server's health info, or None."""
    try:
        with urllib.request.urlopen(f"{base_url(port)}api/health", timeout=timeout) as resp:  # noqa: S310 - a fixed http://127.0.0.1 URL
            info = json.loads(resp.read())
            return info if info.get("app") == "readthrough" else None
    except (OSError, ValueError):
        return None


class Dashboard:
    """Owns the payload and the background threads; the HTTP handler only reads and delegates."""

    def __init__(self, cfg: Config, *, with_diffs: bool = True, seed: int | None = None) -> None:
        self.cfg = cfg
        self.with_diffs = with_diffs
        # Refresh jitter spreads repos' refreshes apart. Its own generator so a seed makes it reproducible.
        self.jitter = random.Random(seed)  # noqa: S311 - timing jitter, not security
        self.token = ""  # set by the server, shared by every repo it serves
        self.instance = ""
        self.payload: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._clients: set[queue.Queue[tuple[str, Any]]] = set()
        # error is None or {"id", "message", "step", "at"}; the dashboard shows it, and so does /api/health.
        self.status: dict[str, Any] = {
            "building": False, "error": None, "last_build": None, "next_build": None,
            "last_success": None, "consecutive_failures": 0, "warnings": 0,
        }  # fmt: skip
        self._seen = {"data": 0.0, "ledger": 0.0}
        self._study_seen: dict[str, float] = {}
        self._wake_reason = "scheduled"
        self._start_delay = 0.0
        self.engine = StudyEngine(cfg, self._engine_event, self.pr_info, lambda: self.snapshot().get("components", []))

    def _engine_event(self, event: str, data: Any) -> None:
        # Keep the payload's per-PR summary current so a page reload shows the latest progress.
        if event == "study" and data.get("type") == "entry":
            info = self.pr_info(data["pr"])
            if info is not None:
                info["study"] = data["summary"]
            self._study_seen[f"pr-{data['pr']}.jsonl"] = mtime(study.log_path(self.cfg, data["pr"]))
        self.broadcast(event, data)

    # ---- state

    def load(self) -> bool:
        if not self.cfg.data_path.exists():
            return False
        try:
            payload = json.loads(self.cfg.data_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            log.warning("Ignoring unreadable %s (%s); it will be rebuilt", self.cfg.data_path, exc)
            return False
        if payload.get("schema", DATA_SCHEMA) != DATA_SCHEMA:
            log.warning("Ignoring %s: written for data schema %s, this version reads %s; it will be rebuilt",
                        self.cfg.data_path, payload.get("schema"), DATA_SCHEMA)  # fmt: skip
            return False
        payload["ledger"] = ledger.load(self.cfg.ledger_path)
        refresh_sessions(payload, self.cfg)
        with self._lock:
            self.payload = payload
        self._seen = {"data": mtime(self.cfg.data_path), "ledger": mtime(self.cfg.ledger_path)}
        self.status["last_build"] = payload.get("generated")
        return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.payload

    # ---- events

    def subscribe(self) -> queue.Queue[tuple[str, Any]]:
        q: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=100)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[tuple[str, Any]]) -> None:
        with self._lock:
            self._clients.discard(q)

    def broadcast(self, event: str, data: Any) -> None:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                self.unsubscribe(q)

    def _set_status(self, **changes: Any) -> None:
        self.status.update(changes)
        self.broadcast("status", self.status)

    # ---- background work

    def rebuild(self) -> bool:
        """Build once. The build logs its own failure; here we only record it for the dashboard and /api/health."""
        if not self._build_lock.acquire(blocking=False):
            return False
        try:
            self._set_status(building=True)
            try:
                build(self.cfg, with_diffs=self.with_diffs)
                self.load()
            except BuildFailed as exc:
                self._failed(exc.error_id, exc.reason, exc.step)
                return False
            except Exception as exc:  # loading the result failed: not GitHub's fault, so keep the traceback
                error_id = logs.new_id()
                with logs.context(self.cfg.repo):
                    log.exception("Loading the new build failed [error %s]", error_id, extra={"error_id": error_id})
                self._failed(error_id, f"{type(exc).__name__}: {exc}", "loading the build")
                return False
            self._set_status(
                building=False, error=None, consecutive_failures=0, last_success=_now(),
                last_build=self.payload.get("generated"), warnings=len(self.payload.get("warnings") or []),
            )  # fmt: skip
            self.broadcast("data", {"generated": self.payload.get("generated")})
            return True
        finally:
            self._build_lock.release()

    def _failed(self, error_id: str, message: str, step: str) -> None:
        self._set_status(
            building=False, consecutive_failures=self.status["consecutive_failures"] + 1,
            error={"id": error_id, "message": message[:400], "step": step, "at": _now()},
        )  # fmt: skip

    def request_refresh(self, reason: str) -> None:
        self._wake_reason = reason
        self._wake.set()

    def next_wait(self) -> float:
        """Seconds until the next refresh: the interval (with jitter), or sooner after a failure."""
        t = self.cfg.tuning.server
        interval = (
            max(1, self.cfg.refresh_minutes) * 60 * (1 + self.jitter.uniform(-t.refresh_jitter, t.refresh_jitter))
        )
        failures = self.status["consecutive_failures"]
        if failures and failures <= len(t.retry_after_failure_minutes):
            return min(interval, t.retry_after_failure_minutes[failures - 1] * 60)
        return interval

    def refresh_loop(self) -> None:
        with logs.context(self.cfg.repo):
            if self._start_delay and not self._stop.wait(self._start_delay):
                log.debug("first refresh after a %.0fs stagger", self._start_delay)
            while not self._stop.is_set():
                wait = self.next_wait()
                self.status["next_build"] = time.time() + wait
                if self.status["consecutive_failures"]:
                    log.info("next attempt in %.0fs (after %d failed refresh%s)", wait, self.status["consecutive_failures"],
                             "es" if self.status["consecutive_failures"] > 1 else "")  # fmt: skip
                woken = self._wake.wait(timeout=wait)
                self._wake.clear()
                reason, self._wake_reason = (self._wake_reason if woken else "scheduled"), "scheduled"
                if not self._stop.is_set():
                    log.info("refreshing (%s)", reason)
                    self.rebuild()

    def watch_loop(self) -> None:
        """Pick up changes made outside the server: `readthrough build`, `readthrough ledger`, Copilot sessions."""
        while not self._stop.wait(self.cfg.tuning.server.watch_seconds):
            try:
                self._watch_once()
            except Exception:
                log.exception("Watcher failed")

    def _watch_once(self) -> None:
        data_m, ledger_m = mtime(self.cfg.data_path), mtime(self.cfg.ledger_path)
        if data_m != self._seen["data"] and not self._build_lock.locked():
            log.info("data.json changed outside the server; reloading")
            self.load()
            self.broadcast("data", {"generated": self.payload.get("generated")})
            return
        if ledger_m != self._seen["ledger"]:
            self._seen["ledger"] = ledger_m
            entries = ledger.load(self.cfg.ledger_path)
            with self._lock:
                self.payload["ledger"] = entries
            self.broadcast("ledger", entries)
        with self._lock:
            changed = refresh_sessions(self.payload, self.cfg) if self.payload else {}
        if changed:
            self.broadcast("sessions", changed)
        self._watch_study()

    def _watch_study(self) -> None:
        """Pick up study-log appends made outside the dashboard, e.g. `readthrough study N --note` from a terminal."""
        folder = self.cfg.study_dir
        if not folder.is_dir():
            return
        for path in folder.glob("pr-*.jsonl"):
            stamp = mtime(path)
            if self._study_seen.get(path.name) == stamp:
                continue
            first_look = path.name not in self._study_seen
            self._study_seen[path.name] = stamp
            if not first_look:
                self._study_changed(int(path.stem.split("-")[1]), reload=True)

    def _study_changed(self, number: int, *, reload: bool = False) -> dict[str, Any]:
        summary = study.summarize(study.load(self.cfg, number))
        info = self.pr_info(number)
        if info is not None:
            info["study"] = summary
        if reload:
            self.broadcast("study", {"pr": number, "type": "reload", "summary": summary})
        self._study_seen[f"pr-{number}.jsonl"] = mtime(study.log_path(self.cfg, number))
        return summary

    def start(self, *, build_now: bool = False, delay: float = 0.0) -> None:
        """Start the refresh and watch threads. ``delay`` staggers repos so they don't all hit GitHub at once."""
        self._start_delay = delay
        for target in (self.refresh_loop, self.watch_loop):
            threading.Thread(target=target, daemon=True, name=f"{target.__name__}:{self.cfg.repo}").start()
        if build_now:
            self.request_refresh("first build")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.engine.stop()

    # ---- actions

    def pr_info(self, number: int) -> dict[str, Any] | None:
        return next((p for p in self.snapshot().get("prs", []) if p["number"] == number), None)

    def study_state(self, number: int) -> dict[str, Any]:
        entries = study.load(self.cfg, number)
        resolved = self.engine.resolved
        engine = {"name": resolved.name, "available": resolved.available, "reason": resolved.reason}
        return {
            "entries": public_entries(entries),
            "summary": study.summarize(entries),
            "engine": {**engine, "busy": self.engine.busy(number)},
        }

    def add_study(self, number: int, body: dict[str, Any]) -> dict[str, Any]:
        """Entries the learner writes directly, without asking the assistant anything."""
        kind = body.get("kind")
        if kind not in ("prediction", "teachback", "reflection", "note", "finding"):
            raise ValueError("you can add a prediction, teachback, reflection, note, or finding")
        if self.pr_info(number) is None:
            raise ValueError(f"#{number} isn't an open PR in the current data")
        fields = {
            k: body[k]
            for k in ("text", "section", "severity", "summary", "path", "line")
            if body.get(k) not in (None, "")
        }
        if kind == "reflection" and body.get("qid"):
            if not isinstance(body["qid"], str):
                raise ValueError("qid must be a question id")
            fields["qid"] = body["qid"]
        if kind == "prediction" and body.get("skipped") is True:
            fields = {"text": "Skipped the prediction.", "skipped": True}
        if kind == "note" and body.get("pinned") is not None:
            if not isinstance(body["pinned"], int):
                raise ValueError("pinned must be an entry id")
            fields["pinned"] = body["pinned"]
        entry = study.append(self.cfg, number, kind, by="you", **fields)
        study.refresh_brief(self.cfg, number)
        summary = self._study_changed(number)
        self.broadcast("study", {"pr": number, "type": "entry", "entry": entry, "summary": summary})
        return entry

    def ask(self, number: int, body: dict[str, Any]) -> None:
        self.engine.submit(number, str(body.get("action", "chat")), str(body.get("text") or ""), body.get("qid"))

    def brief(self, number: int) -> str:
        path = self.cfg.briefs_dir / f"pr-{number}.md"
        if not path.exists():
            raise ValueError(f"No brief for #{number}")
        return path.read_text(encoding="utf-8")

    def head_of(self, number: int) -> str | None:
        return next((p["head"] for p in self.snapshot().get("prs", []) if p["number"] == number), None)

    def set_status(self, number: int, status: str | None) -> dict[str, Any]:
        if status is None:
            ledger.clear_status(self.cfg.ledger_path, number)
        else:
            ledger.record(self.cfg.ledger_path, number, status=status, head=self.head_of(number) or "")
        self._watch_once()
        return self.snapshot()["ledger"]

    def launch_review(self, number: int, session_id: str | None = None) -> dict[str, Any]:
        entry = ledger.load(self.cfg.ledger_path).get(str(number))
        launch = review.plan_review(self.cfg, number, ledger_entry=entry, session_id=session_id)
        terminal = review.launch_in_terminal(launch, self.cfg)
        log.info(
            "%s Copilot session %s for #%d in %s",
            "Resumed" if launch.resuming else "Started", launch.session_id, number, terminal,
        )  # fmt: skip
        return {"session": launch.session_id, "resuming": launch.resuming}


class Handler(BaseHTTPRequestHandler):
    server_version = f"readthrough/{__version__}"

    @property
    def app(self) -> DashboardServer:
        """``self.server`` with its real type: a Handler only ever runs inside a DashboardServer."""
        server = self.server
        if not isinstance(server, DashboardServer):  # pragma: no cover - a programming error, not a request error
            raise TypeError("Handler is bound to the wrong server type")
        return server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler's signature
        log.debug("%s %s", self.address_string(), format % args)

    def _host_ok(self) -> bool:
        port = self.app.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _route(self) -> tuple[Dashboard | None, str]:
        return self.app.route(self.path.split("?", 1)[0])

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "bad host")
        dashboard, path = self._route()
        if dashboard is None:
            if path == "/api/health":
                return self._json({"app": "readthrough", "version": __version__,
                                   "repos": [r["slug"] for r in self.app.repo_names()],
                                   "status": self.app.repo_status()})  # fmt: skip
            if path in ("/", "/index.html"):
                return self._front_page()
            if path == "/setup":
                return self._setup_page()
            if m := re.fullmatch(r"/setup/preview/([0-9a-f]{32})", path):
                payload = self.app.setup.preview_payload(m.group(1))
                if payload is None:
                    return self._error(HTTPStatus.NOT_FOUND, "preview expired; make a new one")
                nonce = secrets.token_urlsafe(16)
                html = render_html(payload, nonce=nonce).encode()
                return self._send(
                    HTTPStatus.OK, html, "text/html; charset=utf-8", {"Content-Security-Policy": page_csp(nonce)}
                )
            if path.startswith("/api/setup/"):
                query = dict(urllib.parse.parse_qsl(self.path.partition("?")[2]))
                return self._setup_call(lambda: self.app.setup.get(path, query))
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if path in ("/", "/index.html"):
            return self._page(dashboard)
        if path == "/api/data":
            return self._json(dashboard.snapshot())
        if path == "/api/events":
            return self._events(dashboard)
        try:
            if m := re.fullmatch(r"/api/study/(\d{1,7})", path):
                return self._json(dashboard.study_state(int(m.group(1))))
            if m := re.fullmatch(r"/api/brief/(\d{1,7})", path):
                return self._json({"markdown": dashboard.brief(int(m.group(1)))})
        except ValueError as exc:
            return self._error(HTTPStatus.NOT_FOUND, str(exc))
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    def _front_page(self) -> None:
        names = self.app.repo_names()
        if names:
            target = self.app.default_repo() or names[0]["slug"]
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"/r/{target}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/setup")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _setup_page(self) -> None:
        nonce = secrets.token_urlsafe(16)
        live = {"token": self.app.token, "instance": self.app.instance, "repos": self.app.repo_names()}
        html = render_setup(live, nonce).encode()
        self._send(HTTPStatus.OK, html, "text/html; charset=utf-8", {"Content-Security-Policy": page_csp(nonce)})

    def _setup_call(self, call: Any) -> None:
        try:
            return self._json(call())
        except Conflict as exc:
            return self._error(HTTPStatus.CONFLICT, str(exc))
        except SetupError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except LookupError as exc:
            return self._error(HTTPStatus.NOT_FOUND, str(exc))

    def _page(self, dashboard: Dashboard) -> None:
        snapshot = dashboard.snapshot()
        if not snapshot:
            body = f"<!doctype html><title>readthrough</title><meta http-equiv=refresh content=5><p>Building {dashboard.cfg.repo} for the first time…".encode()
            return self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
        live = {
            "token": self.app.token,
            "instance": self.app.instance,
            "base": f"/r/{dashboard.cfg.slug}",
            "repos": self.app.repo_names(),
            "refresh_minutes": dashboard.cfg.refresh_minutes,
            "status": dashboard.status,
        }
        nonce = secrets.token_urlsafe(16)
        html = render_html(snapshot, live=live, nonce=nonce).encode()
        mark_seen(dashboard.cfg, snapshot)
        self._send(HTTPStatus.OK, html, "text/html; charset=utf-8", {"Content-Security-Policy": page_csp(nonce)})
        return None

    def do_POST(self) -> None:
        # Read the body before any early rejection: on Windows, closing a socket with unread request
        # data sends a reset, and the client sees a connection error instead of our 403.
        try:
            raw = self._read_body()
        except ValueError as exc:
            self.close_connection = True
            too_large = isinstance(exc, BodyTooLarge)
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE if too_large else HTTPStatus.BAD_REQUEST, str(exc))
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "bad host")
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{self.headers['Host']}":
            return self._error(HTTPStatus.FORBIDDEN, "bad origin")
        if not secrets.compare_digest(self.headers.get("X-Readthrough-Token", ""), self.app.token):
            return self._error(HTTPStatus.FORBIDDEN, "bad token (reload the page)")
        dashboard, path = self._route()
        if dashboard is None:
            if path.startswith("/api/setup/"):
                try:
                    setup_body = self._parse_json(raw)
                except ValueError as exc:
                    return self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return self._setup_call(lambda: self.app.setup.post(path, setup_body))
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        try:
            body = self._parse_json(raw)
            if path == "/api/refresh":
                dashboard.request_refresh(f"requested from {self.headers.get('User-Agent', '?')[:60]}")
                return self._json({"ok": True})
            if m := re.fullmatch(r"/api/review/(\d{1,7})", path):
                sid = body.get("session")
                if sid is not None and not (isinstance(sid, str) and re.fullmatch(r"[0-9a-f-]{36}", sid)):
                    return self._error(HTTPStatus.BAD_REQUEST, "bad session id")
                return self._json(dashboard.launch_review(int(m.group(1)), sid))
            if m := re.fullmatch(r"/api/ledger/(\d{1,7})", path):
                status = body.get("status")
                if status is not None and status not in ledger.STATUSES:
                    return self._error(HTTPStatus.BAD_REQUEST, "unknown status")
                return self._json(dashboard.set_status(int(m.group(1)), status))
            if m := re.fullmatch(r"/api/study/(\d{1,7})", path):
                return self._json(dashboard.add_study(int(m.group(1)), body))
            if m := re.fullmatch(r"/api/study/(\d{1,7})/ask", path):
                dashboard.ask(int(m.group(1)), body)
                return self._json({"ok": True}, HTTPStatus.ACCEPTED)
        except Busy as exc:
            return self._error(HTTPStatus.CONFLICT, str(exc))
        except (RuntimeError, ValueError) as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length > MAX_REQUEST_BYTES:
            if length <= MAX_DISCARD_BYTES:
                while length > 0:
                    chunk = self.rfile.read(min(length, 65536))
                    if not chunk:
                        break
                    length -= len(chunk)
            raise BodyTooLarge(f"body too large (the limit is {MAX_REQUEST_BYTES} bytes)")
        return self.rfile.read(length) if length > 0 else b""

    @staticmethod
    def _parse_json(raw: bytes) -> dict[str, Any]:
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")  # noqa: TRY004 - bad input, reported as a 400
        return data

    def _events(self, dashboard: Dashboard) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = dashboard.subscribe()
        try:
            self._event("hello", {"instance": self.app.instance})
            self._event("status", dashboard.status)
            while True:
                try:
                    event, data = q.get(timeout=dashboard.cfg.tuning.server.keepalive_seconds)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._event(event, data)
        except OSError:
            pass
        finally:
            dashboard.unsubscribe(q)

    def _event(self, event: str, data: Any) -> None:
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()


class DashboardServer(ThreadingHTTPServer):
    """Serves every onboarded repo. Follows config.toml, so newly onboarded repos appear without a restart."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind the same port and silently share traffic.
    allow_reuse_address = sys.platform != "win32"

    def __init__(
        self, dashboards: list[Dashboard], port: int, *, settings_loader: Any = None, with_diffs: bool = True
    ) -> None:
        self.token = secrets.token_urlsafe(24)
        self.instance = secrets.token_hex(6)
        self.dashboards: dict[str, Dashboard] = {}
        self._lock = threading.Lock()
        home = dashboards[0].cfg.home if dashboards else DEFAULT_HOME
        self._settings_loader = settings_loader
        self._with_diffs = with_diffs
        self._watch_seconds = dashboards[0].cfg.tuning.server.watch_seconds if dashboards else 3.0
        self._default: str | None = None
        for dashboard in dashboards:
            self._add(dashboard)
        loader = settings_loader or (lambda: load_settings(home))
        self.setup = SetupService(loader, on_change=lambda: self.sync(loader()))
        super().__init__(("127.0.0.1", port), Handler)

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def _add(self, dashboard: Dashboard) -> None:
        dashboard.token = self.token
        dashboard.instance = self.instance
        with self._lock:
            self.dashboards[dashboard.cfg.slug.lower()] = dashboard

    def dashboard_for(self, slug: str) -> Dashboard | None:
        with self._lock:
            return self.dashboards.get(slug.lower())

    def route(self, path: str) -> tuple[Dashboard | None, str]:
        """Split ``/r/<forge>/<owner>/<name>/<rest>`` into the repo's Dashboard and ``/<rest>``.

        Slugs have as many segments as the forge needs, so the longest known slug that prefixes the path wins.
        """
        if not path.startswith("/r/"):
            return None, path
        lower = path.lower()
        with self._lock:
            slugs = sorted(self.dashboards, key=len, reverse=True)
            for slug in slugs:
                prefix = f"/r/{slug}"
                if lower == prefix or lower.startswith(prefix + "/"):
                    return self.dashboards[slug], path[len(prefix) :] or "/"
        return None, path

    def repo_names(self) -> list[dict[str, str]]:
        """Each served repo's slug (for URLs) and the name the user knows it by."""
        with self._lock:
            dashboards = list(self.dashboards.values())
        counts: dict[str, int] = {}
        for d in dashboards:
            counts[d.cfg.repo.lower()] = counts.get(d.cfg.repo.lower(), 0) + 1
        return [
            {"slug": d.cfg.slug, "name": d.cfg.repo if counts[d.cfg.repo.lower()] == 1 else d.cfg.slug}
            for d in dashboards
        ]

    def default_repo(self) -> str | None:
        """The default repo's slug, when it's served."""
        if not self._default:
            return None
        with self._lock:
            dashboards = list(self.dashboards.values())
        want = self._default.lower()
        match = next((d for d in dashboards if want in (d.cfg.slug.lower(), d.cfg.repo.lower())), None)
        return match.cfg.slug if match else None

    def repo_status(self) -> dict[str, dict[str, Any]]:
        keys = ("building", "error", "last_success", "consecutive_failures", "warnings", "next_build")
        with self._lock:
            return {r.cfg.slug: {k: r.status.get(k) for k in keys} for r in self.dashboards.values()}

    def sync(self, settings: Any) -> None:
        """Add dashboards for newly onboarded repos and stop those that were removed."""
        self._default = settings.default_repo
        wanted = {c.slug.lower(): c for c in settings.all() if c.onboarded}
        with self._lock:
            current = dict(self.dashboards)
        for key, dashboard in current.items():
            if key not in wanted:
                log.info("No longer serving %s", dashboard.cfg.repo)
                dashboard.stop()
                with self._lock:
                    self.dashboards.pop(key, None)
        for key, cfg in wanted.items():
            if key not in current:
                log.info("Now serving %s", cfg.repo)
                dashboard = Dashboard(cfg, with_diffs=self._with_diffs)
                self._add(dashboard)
                dashboard.start(build_now=not dashboard.load())
            else:
                current[key].cfg = cfg

    def watch_config(self, stop: threading.Event) -> None:
        if self._settings_loader is None:
            return
        seen = 0.0
        while not stop.wait(self._watch_seconds):
            try:
                settings = self._settings_loader()
                stamp = mtime(settings.path)
                if stamp != seen:
                    seen = stamp
                    self.sync(settings)
            except Exception:
                log.exception("Reloading config.toml failed")

    def stop_all(self) -> None:
        with self._lock:
            dashboards = list(self.dashboards.values())
        for dashboard in dashboards:
            dashboard.stop()


def serve(settings: Any, *, with_diffs: bool = True, on_ready: Any = None, settings_loader: Any = None) -> None:
    configs = [c for c in settings.all() if c.onboarded]
    dashboards = [Dashboard(c, with_diffs=with_diffs) for c in configs]
    try:
        httpd = DashboardServer(dashboards, settings.port, settings_loader=settings_loader, with_diffs=with_diffs)
    except OSError as exc:
        raise RuntimeError(
            f"Can't listen on port {settings.port} ({exc}). Is another program using it? Set `port` in config.toml."
        ) from exc
    httpd.sync(settings)
    stop = threading.Event()
    threading.Thread(target=httpd.watch_config, args=(stop,), daemon=True, name="config-watch").start()
    stagger = settings.tuning.server.start_stagger_seconds
    for i, dashboard in enumerate(dashboards):
        dashboard.start(build_now=not dashboard.load(), delay=i * stagger)
    log.info("Live dashboard at %s for %s (refreshing every %d min; Ctrl+C to stop)",
             base_url(settings.port), ", ".join(c.repo for c in configs) or "no repos yet", settings.refresh_minutes)  # fmt: skip
    if on_ready:
        on_ready()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.stop_all()
        httpd.server_close()
