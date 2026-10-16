import asyncio
import sys
import types
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import FakeForge

from readthrough import onboard
from readthrough import onboard_copilot as copilot_mod
from readthrough.config import Config
from readthrough.onboard import RepoFacts
from readthrough.onboard_copilot import _clip, _Drafter, copilot_draft, mark_drafter

TREE = [
    "src/app/api/routes.py",
    "src/app/core/engine.py",
    "src/app/db/models.py",
    "tests/test_api.py",
    "docs/architecture.md",
    "README.md",
]
AUTHORS = [
    {"login": "alice", "typename": "User", "association": "MEMBER"},
    {"login": "external", "typename": "User", "association": "CONTRIBUTOR"},
]


@pytest.fixture
def facts() -> RepoFacts:
    return RepoFacts(
        "o/app",
        "main",
        TREE,
        Counter({"src/app/api/routes.py": 40, "src/app/core/engine.py": 30, "tests/test_api.py": 10}),
        AUTHORS,
        codeowners=[("src/app/api/**", ["@o/api"])],
        docs={"docs/architecture.md": "# Architecture\n\nAPI owns routes.\n"},
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(home=tmp_path / "home", repo="o/app")
    # A generous timeout: the fake sessions signal completion themselves. Only the timeout test shortens it; a
    # tiny default races the event loop on a slow machine.
    cfg.tuning = replace(
        cfg.tuning,
        study=replace(
            cfg.tuning.study,
            onboarding_tool_output_chars=24,
            onboarding_doc_excerpt_chars=20,
            onboarding_turn_timeout_seconds=5,
        ),
    )
    return cfg


class Params(SimpleNamespace):
    pass


def _event(kind: str, **data):
    return SimpleNamespace(type=SimpleNamespace(value=kind), data=SimpleNamespace(**data))


def _install_fake_copilot(monkeypatch: pytest.MonkeyPatch, client=None) -> None:
    def define_tool(**tool_options):
        def decorate(func):
            func.name = tool_options["name"]
            func.tool_options = tool_options
            return func

        return decorate

    class PermissionDecisionUserNotAvailable:
        pass

    monkeypatch.setitem(
        sys.modules,
        "copilot",
        types.SimpleNamespace(define_tool=define_tool, CopilotClient=(lambda: client) if client else object),
    )
    monkeypatch.setitem(sys.modules, "copilot.generated", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "copilot.generated.rpc",
        types.SimpleNamespace(PermissionDecisionUserNotAvailable=PermissionDecisionUserNotAvailable),
    )


def test_copilot_draft_reports_missing_sdk(monkeypatch: pytest.MonkeyPatch, cfg, facts):
    original_import = __import__

    def blocked_import(name, *args, **kwargs):
        if name == "copilot":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked_import)
    with pytest.raises(RuntimeError, match="Copilot SDK"):
        copilot_draft(cfg, forge=None, facts=facts)


def test_copilot_draft_returns_marked_best_proposal(monkeypatch: pytest.MonkeyPatch, cfg, facts):
    progress = []
    proposal = onboard.draft_profile(facts)

    class FakeDrafter:
        def __init__(self, cfg, forge, facts, on_progress):
            self.best = None
            self.summary = ""

        async def run(self):
            self.best = ([], 0.875, proposal)
            self.summary = "Check the API split."

    _install_fake_copilot(monkeypatch)
    monkeypatch.setattr(copilot_mod, "_Drafter", FakeDrafter)

    text = copilot_draft(cfg, forge=None, facts=facts, on_progress=progress.append)

    assert text.startswith("# Drafted with Copilot")
    assert onboard.check_profile_text(text, facts)[0].drafted_by == "copilot"
    assert progress == ["Best proposal: 88% coverage, 0 problem(s) left", "Copilot's notes:\nCheck the API split."]


def test_copilot_draft_raises_when_session_never_proposes(monkeypatch: pytest.MonkeyPatch, cfg, facts):
    class FakeDrafter:
        def __init__(self, cfg, forge, facts, on_progress):
            self.best = None
            self.summary = ""

        async def run(self):
            return None

    _install_fake_copilot(monkeypatch)
    monkeypatch.setattr(copilot_mod, "_Drafter", FakeDrafter)

    with pytest.raises(RuntimeError, match="didn't propose"):
        copilot_draft(cfg, forge=None, facts=facts)


def test_run_starts_client_creates_locked_down_session_and_disconnects(cfg, facts, monkeypatch: pytest.MonkeyPatch):
    class Session:
        def __init__(self):
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    class Client:
        def __init__(self):
            self.session = Session()
            self.started = False
            self.stopped = False
            self.created = []

        async def start(self):
            self.started = True

        async def create_session(self, *, session_id, **options):
            self.created.append((session_id, options))
            return self.session

        async def stop(self):
            self.stopped = True

    client = Client()
    _install_fake_copilot(monkeypatch, client)
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=lambda _m: None)
    monkeypatch.setattr(drafter, "_tools", lambda: [SimpleNamespace(name="read_file")])
    monkeypatch.setattr(drafter, "_first_prompt", lambda: "first prompt")

    async def fake_send(session, prompt):
        assert session is client.session and prompt == "first prompt"
        return "summary"

    monkeypatch.setattr(drafter, "_send", fake_send)

    asyncio.run(drafter.run())

    assert client.started and client.stopped and client.session.disconnected
    session_id, options = client.created[0]
    assert session_id
    assert options["available_tools"] == ["read_file"]
    assert options["client_name"] == "readthrough-onboard"
    assert options["enable_skills"] is False
    assert options["working_directory"] == str(cfg.home)
    assert options["on_permission_request"](None, None).__class__.__name__ == "PermissionDecisionUserNotAvailable"
    assert drafter.summary == "summary"


class FakeSession:
    def __init__(self, events):
        self.events = events
        self.sent = []
        self.unsubscribed = False
        self.callback: Callable[[Any], None] = lambda _event: None

    def on(self, callback):
        self.callback = callback

        def unsubscribe():
            self.unsubscribed = True

        return unsubscribe

    async def send(self, prompt):
        self.sent.append(prompt)
        for event in self.events:
            self.callback(event)


def test_send_records_progress_summary_and_unsubscribes(cfg, facts):
    progress = []
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=progress.append)
    session = FakeSession(
        [
            _event("tool.execution_start", tool_name="read_file"),
            _event("tool.execution_start", tool_name="propose_profile"),
            _event("assistant.message", content="Final notes"),
            _event("session.idle"),
        ]
    )

    result = asyncio.run(drafter._send(session, "prompt"))

    assert result == "Final notes"
    assert session.sent == ["prompt"] and session.unsubscribed
    assert progress == ["Copilot: read file"]


def test_send_raises_errors_only_when_no_best_proposal_exists(cfg, facts):
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=lambda _m: None)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(drafter._send(FakeSession([_event("session.error", message="boom")]), "prompt"))

    drafter.best = (["remaining"], 0.5, "text")
    assert asyncio.run(drafter._send(FakeSession([_event("session.error", message="ignored")]), "prompt")) == ""


def test_send_timeout_unsubscribes(cfg, facts):
    cfg.tuning = replace(cfg.tuning, study=replace(cfg.tuning.study, onboarding_turn_timeout_seconds=0.05))
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=lambda _m: None)
    session = FakeSession([])  # never signals
    with pytest.raises(TimeoutError):
        asyncio.run(drafter._send(session, "prompt"))
    assert session.unsubscribed


def test_first_prompt_includes_reference_docs_authors_and_no_clone_notice(cfg, facts):
    prompt = _Drafter(cfg, forge=None, facts=facts, on_progress=lambda _m: None)._first_prompt()

    assert "Draft the architecture profile for o/app" in prompt
    assert "<profile_reference>" in prompt
    assert "<doc path='docs/architecture.md'>" in prompt
    assert "# Architecture" in prompt
    assert "src/app/api/** @o/api" in prompt
    assert "alice association=MEMBER" in prompt
    assert "search_code isn't available" in prompt


def test_helpers_read_from_github_search_clone_and_track_best(
    cfg, facts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class Gh(FakeForge):
        def read_file(self, path):
            return "alpha\nbeta\ngamma\n" if path == "src/app/api/routes.py" else None

    drafter = _Drafter(cfg, forge=Gh("o/app"), facts=facts, on_progress=lambda _m: None)
    assert "    2  beta" in drafter._read("src/app/api/routes.py", 2, 99)
    assert "isn't a file in the repo" in drafter._read("../secret.txt", 1, 2)
    assert (
        drafter._search("alpha", None) == "search_code needs a local clone; use read_file and list_directory instead."
    )

    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    clone_drafter = _Drafter(
        Config(home=tmp_path / "home", repo="o/app", clone=clone), forge=None, facts=facts, on_progress=lambda _m: None
    )
    clone_drafter.cfg.tuning = replace(
        clone_drafter.cfg.tuning,
        study=replace(clone_drafter.cfg.tuning.study, onboarding_tool_output_chars=10),
    )
    monkeypatch.setattr(copilot_mod, "run", lambda cmd, check=True: "abcdefghijklmnopqrstuvwxyz")
    assert clone_drafter._search("a", "src/**").endswith("[truncated 16 characters]")

    wrong_repo = onboard.draft_profile(facts).replace('repo = "o/app"', 'repo = "other/app"')
    assert clone_drafter._propose(wrong_repo) == "repo must be 'o/app'."
    good = onboard.draft_profile(facts)
    assert "No problems" in clone_drafter._propose(good)
    assert clone_drafter.best is not None and clone_drafter.best[2] == good


def test_tools_delegate_to_helpers_and_validate_profile(cfg, facts, monkeypatch: pytest.MonkeyPatch):
    _install_fake_copilot(monkeypatch)
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=lambda _m: None)
    tools = {tool.name: tool for tool in drafter._tools()}
    monkeypatch.setattr(drafter, "_search", lambda pattern, path_glob: f"{pattern}:{path_glob}")

    assert "src/app/" in tools["list_directory"](Params(prefix="src", depth=1))
    assert "isn't a file" in asyncio.run(tools["read_file"](Params(path="missing.py", start_line=1, end_line=2)))
    assert asyncio.run(tools["search_code"](Params(pattern="Route", path_glob="src/**"))) == "Route:src/**"
    assert tools["match_glob"](Params(glob="src/app/**/*.py")).startswith("3 files, recent churn 70")
    assert "No problems" in tools["propose_profile"](Params(toml=onboard.draft_profile(facts)))


def test_mark_drafter_replaces_existing_value_and_clip():
    text = 'drafted_by = "readthrough"\nschema = 1\nrepo = "o/app"\n'
    marked = mark_drafter(text, "copilot")
    assert marked == 'schema = 1\ndrafted_by = "copilot"\nrepo = "o/app"\n'
    assert _clip("abc", limit=10) == "abc"
    assert _clip("abcdef", limit=3) == "abc\n... [truncated 3 characters]"
