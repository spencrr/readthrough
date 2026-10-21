import asyncio
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeForge

from readthrough import onboard, runtimes
from readthrough import onboard_model as model_mod
from readthrough.config import Config
from readthrough.onboard import RepoFacts
from readthrough.onboard_model import _clip, _Drafter, mark_drafter, model_draft

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


class FakeSession:
    """A runtime session scripted with the events one prompt produces; tool calls run the real handlers."""

    def __init__(self, script):
        self.script = script
        self.sent = []
        self.closed = False
        self.tools: dict[str, runtimes.ToolSpec] = {}

    @property
    def fresh(self) -> bool:
        return True

    async def send(self, prompt):
        self.sent.append(prompt)
        for item in self.script:
            if isinstance(item, tuple):
                _, name, args = item
                yield runtimes.Event("tool", name=name)
                await self.tools[name].handler(args)
            else:
                yield item

    async def close(self):
        self.closed = True


class FakeRuntime:
    name = "fake"

    def __init__(self, script=None):
        self.script = script or [runtimes.Event("done", text="")]
        self.opened = []
        self.session = None
        self.stopped = False

    def describe(self):
        return "a fake runtime"

    async def open(self, conversation):
        self.opened.append(conversation)
        self.session = FakeSession(list(self.script))
        self.session.tools = {t.name: t for t in conversation.tools}
        return self.session

    async def stop(self):
        self.stopped = True


def _use(monkeypatch: pytest.MonkeyPatch, runtime: FakeRuntime) -> None:
    monkeypatch.setattr(runtimes, "make", lambda name, options=None: runtime)
    monkeypatch.setattr(runtimes, "resolve", lambda name, options=None: ("fake", True, ""))


def test_model_draft_reports_a_missing_runtime(monkeypatch: pytest.MonkeyPatch, cfg, facts):
    monkeypatch.setattr(
        runtimes,
        "resolve",
        lambda name, options=None: ("copilot", False, "install the [study] extra, or set an API key"),
    )
    with pytest.raises(RuntimeError, match=r"needs a model runtime: install the \[study\] extra"):
        model_draft(cfg, forge=None, facts=facts)


def test_model_draft_returns_marked_best_proposal(monkeypatch: pytest.MonkeyPatch, cfg, facts):
    progress = []
    proposal = onboard.draft_profile(facts)

    class FakeDrafter:
        def __init__(self, cfg, forge, facts, on_progress):
            self.best = None
            self.summary = ""
            self.runtime_name = "anthropic"
            self.model = "claude-sonnet-5-5"

        async def run(self):
            self.best = ([], 0.875, proposal)
            self.summary = "Check the API split."

    _use(monkeypatch, FakeRuntime())
    monkeypatch.setattr(model_mod, "_Drafter", FakeDrafter)

    text = model_draft(cfg, forge=None, facts=facts, on_progress=progress.append)

    # The profile says a model drafted it and the header says which, so a reader knows what to double-check.
    assert text.startswith(
        "# Drafted with claude-sonnet-5-5 (anthropic runtime) by `readthrough onboard o/app --assisted`"
    )
    assert onboard.check_profile_text(text, facts)[0].drafted_by == "model"
    assert progress == ["Best proposal: 88% coverage, 0 problem(s) left", "The model's notes:\nCheck the API split."]


def test_model_draft_raises_when_session_never_proposes(monkeypatch: pytest.MonkeyPatch, cfg, facts):
    class FakeDrafter:
        def __init__(self, cfg, forge, facts, on_progress):
            self.best = None
            self.summary = ""

        async def run(self):
            return None

    _use(monkeypatch, FakeRuntime())
    monkeypatch.setattr(model_mod, "_Drafter", FakeDrafter)

    with pytest.raises(RuntimeError, match="didn't propose"):
        model_draft(cfg, forge=None, facts=facts)


def test_run_opens_a_fresh_locked_down_conversation_and_closes_it(cfg, facts, monkeypatch: pytest.MonkeyPatch):
    runtime = FakeRuntime([runtimes.Event("done", text="summary")])
    _use(monkeypatch, runtime)
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=lambda _m: None)
    monkeypatch.setattr(drafter, "_first_prompt", lambda: "first prompt")

    asyncio.run(drafter.run())

    conversation = runtime.opened[0]
    assert conversation.key.startswith("readthrough:onboarding:") and conversation.resume is False
    assert [t.name for t in conversation.tools] == [
        "list_directory",
        "read_file",
        "search_code",
        "match_glob",
        "propose_profile",
    ]
    assert (
        conversation.working_directory == str(cfg.home)
        and "drafting an architecture profile for o/app" in conversation.system
    )
    assert runtime.session is not None
    assert runtime.session is not None
    assert runtime.session.sent == ["first prompt"] and runtime.session.closed and runtime.stopped
    assert drafter.summary == "summary"


def test_send_records_progress_summary_and_usage(cfg, facts):
    from readthrough import usage

    progress = []
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=progress.append)
    used = runtimes.Usage(
        call_id="c1",
        model="claude-sonnet-5-5",
        input_tokens=10,
        output_tokens=5,
        billed_to="anthropic:api.anthropic.com",
    )
    session = FakeSession([
        runtimes.Event("tool", name="read_file"),
        runtimes.Event("tool", name="propose_profile"),
        runtimes.Event("usage", usage=used),
        runtimes.Event("done", text="Final notes"),
    ])  # fmt: skip

    result = asyncio.run(drafter._send(session, "prompt"))

    assert result == "Final notes" and session.sent == ["prompt"]
    assert progress == ["model: read file"], "proposals are reported by the tool itself, with their coverage"
    line = usage.load(cfg)[0]
    assert (line["purpose"], line["ref"], line["billed_to"]) == ("draft", "onboarding", "anthropic:api.anthropic.com")


def test_send_raises_errors_only_when_no_best_proposal_exists(cfg, facts):
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=lambda _m: None)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(drafter._send(FakeSession([runtimes.Event("error", text="boom")]), "prompt"))

    drafter.best = (["remaining"], 0.5, "text")
    assert asyncio.run(drafter._send(FakeSession([runtimes.Event("error", text="ignored")]), "prompt")) == ""


def test_send_times_out(cfg, facts):
    cfg.tuning = replace(cfg.tuning, study=replace(cfg.tuning.study, onboarding_turn_timeout_seconds=0.05))
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=lambda _m: None)

    class Hanging(FakeSession):
        async def send(self, prompt):
            await asyncio.sleep(10)
            yield runtimes.Event("done")

    with pytest.raises(TimeoutError):
        asyncio.run(drafter._send(Hanging([]), "prompt"))


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
    monkeypatch.setattr(model_mod, "run", lambda cmd, check=True: "abcdefghijklmnopqrstuvwxyz")
    assert clone_drafter._search("a", "src/**").endswith("[truncated 16 characters]")

    wrong_repo = onboard.draft_profile(facts).replace('repo = "o/app"', 'repo = "other/app"')
    assert clone_drafter._propose(wrong_repo) == "repo must be 'o/app'."
    good = onboard.draft_profile(facts)
    assert "No problems" in clone_drafter._propose(good)
    assert clone_drafter.best is not None and clone_drafter.best[2] == good


def test_tools_delegate_to_helpers_and_validate_profile(cfg, facts, monkeypatch: pytest.MonkeyPatch):
    drafter = _Drafter(cfg, forge=None, facts=facts, on_progress=lambda _m: None)
    tools = {tool.name: tool for tool in drafter._tools()}
    monkeypatch.setattr(drafter, "_search", lambda pattern, path_glob: f"{pattern}:{path_glob}")

    call = lambda name, **args: asyncio.run(tools[name].handler(args))
    assert "src/app/" in call("list_directory", prefix="src", depth=1)
    assert "src/" in call("list_directory"), "defaults apply when the model sends nothing"
    assert "isn't a file" in call("read_file", path="missing.py", start_line=1, end_line=2)
    assert call("search_code", pattern="Route", path_glob="src/**") == "Route:src/**"
    assert call("match_glob", glob="src/app/**/*.py").startswith("3 files, recent churn 70")
    assert "No problems" in call("propose_profile", toml=onboard.draft_profile(facts))
    for spec in tools.values():
        assert runtimes.tool_schema(spec)["type"] == "object"


def test_mark_drafter_replaces_existing_value_and_clip():
    text = 'drafted_by = "readthrough"\nschema = 1\nrepo = "o/app"\n'
    marked = mark_drafter(text, "model")
    assert marked == 'schema = 1\ndrafted_by = "model"\nrepo = "o/app"\n'
    assert _clip("abc", limit=10) == "abc"
    assert _clip("abcdef", limit=3) == "abc\n... [truncated 3 characters]"
