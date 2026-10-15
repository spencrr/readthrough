from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from readthrough import onboard, web_setup
from readthrough.config import load_settings
from readthrough.github import GhError
from readthrough.limits import BROWSER_EDITABLE_INTS
from readthrough.web_setup import Conflict, SetupError, SetupService

TREE = ["src/app/core.py", "src/app/api.py", "tests/test_core.py", "README.md"]
FACTS = onboard.RepoFacts("o/app", "main", TREE, Counter({"src/app/core.py": 50, "tests/test_core.py": 10}),
                          [{"login": "alice", "typename": "User", "association": "MEMBER"}])  # fmt: skip


CHANGES: list[int] = []


@pytest.fixture
def service(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(web_setup, "signed_in_accounts", lambda: ["me", "me_corp"])
    monkeypatch.setattr(web_setup, "GitHub", FakeGitHub)  # nothing here may reach the real gh
    FakeGitHub.calls = []
    CHANGES.clear()
    svc = SetupService(lambda: load_settings(home), on_change=lambda: CHANGES.append(1))
    monkeypatch.setattr(svc, "_gather", lambda cfg, gh, say: FACTS)
    return svc, home


def test_accept_needs_a_profile_that_passes(service):
    svc, home = service
    draft = onboard.draft_profile(FACTS)
    svc.save_profile({"repo": "o/app", "toml": draft})
    report = svc.check({"repo": "o/app", "toml": draft})
    assert report["valid"] and report["problems"] == [] and report["authors"][0][0] == "alice"

    broken = draft.replace("[authors]", "[not_a_section]")
    assert not svc.check({"repo": "o/app", "toml": broken})["valid"]
    with pytest.raises(SetupError):
        svc.accept({"repo": "o/app", "toml": broken})

    result = svc.accept({"repo": "o/app"})
    assert result == {"onboarded": True, "url": "/r/o/app/"}
    settings = load_settings(home)
    assert settings.config_for("o/app").onboarded and settings.default_repo == "o/app"
    assert CHANGES


def test_settings_are_validated_and_version_checked(service):
    svc, home = service
    svc.save_profile({"repo": "o/app", "toml": onboard.draft_profile(FACTS)})
    version = svc.state(None)["settings"]["version"]
    out = svc.save_settings({"repo": "o/app", "version": version, "values": {
        "session_minutes": "45", "internal_authors": ["bob", "bob"], "account": "me_corp", "study_model": "claude-opus-4.8"}})  # fmt: skip
    cfg = load_settings(home).config_for("o/app")
    assert (cfg.session_minutes, cfg.internal_authors, cfg.account, cfg.study_model) == (
        45,
        ["bob"],
        "me_corp",
        "claude-opus-4.8",
    )

    with pytest.raises(Conflict):
        svc.save_settings({"repo": "o/app", "version": version, "values": {"limit": 5}})
    for bad in ({"copilot_args": ["--allow-all-tools"]}, {"profile": "C:/Windows/win.ini"}, {"session_minutes": 0},
                {"internal_authors": ["not a login!"]}, {"account": "stranger"}, {"study_model": "x; rm -rf"}):  # fmt: skip
        with pytest.raises(SetupError):
            svc.save_settings({"repo": "o/app", "version": out["version"], "values": bad})
    with pytest.raises(SetupError, match="isn't configured"):
        svc.save_settings({"repo": "x/y", "values": {"limit": 5}})


def test_inputs_are_checked_before_use(service, tmp_path: Path):
    svc, _ = service
    for repo in ("", "no-slash", "o/app; calc", None):
        with pytest.raises(SetupError):
            svc.check({"repo": repo, "toml": "schema = 1"})
    with pytest.raises(SetupError, match="isn't a git clone"):
        svc.check_clone({"repo": "o/app", "clone": str(tmp_path)})
    with pytest.raises(SetupError, match="full path"):
        svc.check_clone({"repo": "o/app", "clone": "relative/path"})
    with pytest.raises(SetupError, match="empty"):
        svc.save_profile({"repo": "o/app", "toml": "  "})
    with pytest.raises(SetupError, match="not o/app"):
        svc.save_profile(
            {"repo": "o/app", "toml": onboard.draft_profile(FACTS).replace('repo = "o/app"', 'repo = "o/other"')}
        )
    with pytest.raises(LookupError):
        svc.post("/api/setup/run-anything", {})


def test_remove_and_purge(service):
    svc, home = service
    svc.save_profile({"repo": "o/app", "toml": onboard.draft_profile(FACTS)})
    data_dir = load_settings(home).config_for("o/app").repo_dir
    assert data_dir.exists()
    svc.remove({"repo": "o/app", "purge": True, "version": svc.state(None)["settings"]["version"]})
    assert load_settings(home).entry("o/app") is None and not data_dir.exists()


class ImmediateThread:
    def __init__(self, *, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class FakeGitHub:
    calls: ClassVar[list[tuple[str, str | None, str]]] = []
    remote_files: ClassVar[dict[str, str]] = {}  # path -> contents the fake repo "has"

    def __init__(self, repo: str, account: str | None = None):
        self.repo = repo
        self.account = account

    def api(self, path: str) -> str:
        self.calls.append((self.repo, self.account, path))
        if self.repo == "o/missing":
            raise GhError("not found", stderr="HTTP 404")
        return '{"full_name":"o/app","private":true,"default_branch":"main","description":null}'

    def raw_or_none(self, path: str) -> str | None:
        return self.remote_files.get(path)


def test_setup_get_inspect_clone_and_source_text(service, tmp_path: Path, monkeypatch):
    svc, _home = service
    monkeypatch.setattr(FakeGitHub, "remote_files", {".github/readthrough.toml": "remote .github/readthrough.toml"})
    guessed = tmp_path / "guessed"
    monkeypatch.setattr(web_setup, "guess_clone", lambda repo: guessed)
    assert svc.get("/api/setup/state", {})["accounts"] == ["me", "me_corp"]
    with pytest.raises(LookupError):
        svc.get("/api/setup/jobs/" + "0" * 32, {})

    local = tmp_path / "profile.toml"
    local.write_text("local profile", encoding="utf-8")
    source = SimpleNamespace(kind="local", label="Local profile", path=local)
    assert web_setup._source_text(source, None) == "local profile"
    remote_source = SimpleNamespace(kind="repo", label="Repo profile", path=None)
    assert web_setup._source_text(remote_source, FakeGitHub("o/app")) == "remote .github/readthrough.toml"

    result = svc.inspect({"repo": "o/app", "account": "me_corp"})
    assert result["full_name"] == "o/app"
    assert result["private"] is True
    assert result["description"] == ""
    assert result["clone"] == str(guessed)
    assert FakeGitHub.calls[-1] == ("o/app", "me_corp", "repos/o/app")

    with pytest.raises(SetupError, match="Can't read o/missing as me"):
        svc.inspect({"repo": "o/missing", "account": "me"})

    assert svc.check_clone({"repo": "o/app", "clone": ""})["ok"] is False
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setattr(web_setup, "run", lambda *args, **kwargs: "origin https://github.com/o/app (fetch)\n")
    monkeypatch.setattr(web_setup, "remote_for", lambda clone, repo: "origin")
    ok = svc.check_clone({"repo": "o/app", "clone": str(clone)})
    assert ok["ok"] is True and ok["detected"] == "origin" and "points at o/app" in ok["message"]


def test_setup_jobs_cover_draft_preview_and_errors(service, monkeypatch):
    svc, _ = service
    monkeypatch.setattr(web_setup.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(web_setup, "GitHub", FakeGitHub)

    job_id = svc.start_draft({"repo": "o/app", "mode": "plain"})["job"]
    job = svc.job(job_id)
    assert job["status"] == "done"
    assert "schema = 1" in job["result"]["toml"]
    assert job["log"] == ["Drafting from the file tree, CODEOWNERS, docs, and recent PRs"]

    saved = svc.save_profile({"repo": "o/app", "toml": job["result"]["toml"]})
    assert Path(saved["saved"]).exists()
    source_job = svc.post("/api/setup/draft", {"repo": "o/app", "mode": "source"})["job"]
    assert svc.job(source_job)["result"]["toml"] == job["result"]["toml"]

    from readthrough import onboard_copilot

    monkeypatch.setattr(onboard_copilot, "copilot_draft", lambda cfg, gh, facts, on_progress: job["result"]["toml"])
    copilot_job = svc.start_draft({"repo": "o/app", "mode": "copilot"})["job"]
    assert svc.job(copilot_job)["log"][0] == "Copilot is reading the repo. This takes a few minutes."

    with pytest.raises(SetupError, match="mode must be"):
        svc.start_draft({"repo": "o/app", "mode": "unknown"})

    from readthrough import build as build_mod

    monkeypatch.setattr(build_mod, "build", lambda *args, **kwargs: {"repo": "o/app", "prs": []})
    preview_job = svc.start_preview({"repo": "o/app", "toml": job["result"]["toml"]})["job"]
    preview = svc.job(preview_job)["result"]["url"].rsplit("/", 1)[1]
    assert svc.preview_payload(preview) == {"repo": "o/app", "prs": []}

    error_job = svc._run_job("bad", lambda say: (_ for _ in ()).throw(SetupError("boom")))
    assert svc.job(error_job)["status"] == "error"
    assert svc.job(error_job)["error"] == "boom"


def test_save_profile_backs_up_changed_profiles(service):
    svc, home = service
    original = onboard.draft_profile(FACTS)
    saved = Path(svc.save_profile({"repo": "o/app", "toml": original})["saved"])
    assert svc.save_profile({"repo": "o/app", "toml": original})["unchanged"] is True

    updated = original + "\n# changed\n"
    svc.save_profile({"repo": "o/app", "toml": updated})
    cfg = load_settings(home).config_for("o/app")
    assert saved.read_text(encoding="utf-8") == updated
    assert len(list(cfg.repo_dir.glob("profile.*.toml"))) == 1


def test_browser_setting_validation_edges(service):
    svc, _ = service
    svc.save_profile({"repo": "o/app", "toml": onboard.draft_profile(FACTS)})
    version = svc.state(None)["settings"]["version"]

    for key, (low, high) in BROWSER_EDITABLE_INTS.items():
        assert svc.save_settings({"repo": "o/app", "version": version, "values": {key: str(low)}})["version"]
        version = svc.state(None)["settings"]["version"]
        assert svc.save_settings({"repo": "o/app", "version": version, "values": {key: high}})["version"]
        version = svc.state(None)["settings"]["version"]
        with pytest.raises(SetupError, match=key):
            svc.save_settings({"repo": "o/app", "version": version, "values": {key: high + 1}})
        with pytest.raises(SetupError, match=key):
            svc.save_settings({"repo": "o/app", "version": version, "values": {key: True}})

    with pytest.raises(SetupError, match="values and global"):
        svc.save_settings({"values": ["x"], "global": {}})
    with pytest.raises(SetupError, match="Repo must"):
        svc.save_settings({"global": {"default_repo": "not-a-repo"}})
    with pytest.raises(SetupError, match="isn't configured"):
        svc.save_settings({"global": {"default_repo": "o/other"}})
    with pytest.raises(SetupError, match="profile is too big"):
        svc.save_profile({"repo": "o/app", "toml": "x" * 200_001})
    with pytest.raises(SetupError, match="usable path"):
        svc.save_settings({"repo": "o/app", "version": version, "values": {"clone": "x" * 401}})
    with pytest.raises(SetupError, match="remote name"):
        svc.save_settings({"repo": "o/app", "version": version, "values": {"remote": "origin;bad"}})
