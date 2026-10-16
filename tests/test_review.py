import json
import uuid
from pathlib import Path

import pytest

from readthrough import review
from readthrough.config import Config

ALLOWED = ["github/Octo/Courier", "github/o/r", "gitlab/g/sub/p"]


def _cfg(tmp_path: Path, repo: str = "o/r", **kw) -> Config:
    return Config(home=tmp_path / "home", repo=repo, **kw)


def test_session_id_is_stable_per_pr(tmp_path: Path):
    cfg = _cfg(tmp_path, "Octo/Courier")
    a = review.default_session_id(cfg, 2897)
    assert a == review.default_session_id(cfg, 2897)
    assert a != review.default_session_id(cfg, 2898)
    # Derived from the PR's URL, so the same path on another forge is another session.
    assert a != review.default_session_id(_cfg(tmp_path, "Octo/Courier", forge="forgejo"), 2897)
    uuid.UUID(a)


def test_ledger_session_overrides_default(tmp_path: Path):
    cfg = _cfg(tmp_path)
    assert review.session_id_for(cfg, 1, {"session": "abc"}) == "abc"
    assert review.session_id_for(cfg, 1, None) == review.default_session_id(cfg, 1)


@pytest.mark.parametrize(
    ("url", "action", "repo", "number"),
    [
        ("readthrough://review/github/Octo/Courier/2897", "review", "github/Octo/Courier", 2897),
        ("READTHROUGH://Review/github/octo/courier/2897/", "review", "github/Octo/Courier", 2897),
        ("readthrough://review/Octo/Courier/2897", "review", "github/Octo/Courier", 2897),  # pre-forge form
        ("readthrough://refresh/github/Octo/Courier", "refresh", "github/Octo/Courier", None),
        ("readthrough://open/o/r", "open", "github/o/r", None),
        ("readthrough://review/gitlab/g/sub/p/5", "review", "gitlab/g/sub/p", 5),
        ("readthrough://open", "open", None, None),
    ],
)
def test_parse_readthrough_url_accepts(url, action, repo, number):
    parsed = review.parse_readthrough_url(url, allowed_repos=ALLOWED)
    assert (parsed.action, parsed.repo, parsed.number) == (action, repo, number)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/review/1",
        "readthrough://exec/o/r/1",
        "readthrough://review/evil/repo/1",
        "readthrough://review/g/sub/p/1",  # a non-GitHub repo must be named with its forge
        "readthrough://review/o/r/1?x=1",
        "readthrough://review/o/r/1;calc",
        "readthrough://review/../../x",
        "readthrough://review/o/r",
        "readthrough://review/o/r/1/2",
        "readthrough://refresh/o/r/1",
        "readthrough://review/o/r/12345678",
        "readthrough://review/o",
    ],
)
def test_parse_readthrough_url_rejects(url):
    with pytest.raises(ValueError):
        review.parse_readthrough_url(url, allowed_repos=ALLOWED)


def test_plan_review_new_then_resume(tmp_path: Path):
    cfg = Config(home=tmp_path / "home", repo="o/r", clone=tmp_path / "clone", agent_args=["--model", "m"])

    first = review.plan_review(cfg, 7, ledger_entry=None)
    assert not first.resuming and first.agent == "copilot"
    assert first.argv[0].endswith("copilot") and f"--session-id={first.session_id}" in first.argv
    assert "-i" in first.argv and "#7" in first.argv[first.argv.index("-i") + 1]
    assert first.argv[-2:] == ["--model", "m"]

    (tmp_path / "copilot" / "session-state" / first.session_id).mkdir(parents=True)
    second = review.plan_review(cfg, 7, ledger_entry=None)
    assert second.resuming
    assert second.argv[1] == f"--resume={first.session_id}"
    assert "-i" not in second.argv


def test_plan_review_in_claude_code(tmp_path: Path):
    """With agent = "claude", a new session starts in Claude Code and an existing one is found in its project store."""
    cfg = Config(
        home=tmp_path / "home", repo="o/r", clone=tmp_path / "clone", agent="claude", agent_args=["--model", "m"]
    )
    first = review.plan_review(cfg, 7, ledger_entry=None)
    assert first.agent == "claude" and first.argv[0].endswith("claude")
    assert first.argv[1:3] == ["--session-id", first.session_id] and first.argv[-3:-1] == ["--model", "m"]
    assert first.argv[-1].startswith("[readthrough:github/o/r#7]"), "Claude Code takes the prompt as its last argument"
    project = tmp_path / "claude" / "projects" / "C--Users-me-src-r"
    project.mkdir(parents=True)
    (project / f"{first.session_id}.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"x"}}\n', encoding="utf-8"
    )
    second = review.plan_review(cfg, 7, ledger_entry=None)
    assert second.resuming and second.argv[1:3] == ["--resume", first.session_id]
    info = review.session_info(cfg, first.session_id)
    assert info["exists"] and info["active"] is None, (
        "Claude Code leaves no mark of an open session, so none is claimed"
    )


def test_an_uninstalled_agent_is_named_with_its_install_hint(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("READTHROUGH_CLAUDE")
    monkeypatch.setattr("readthrough.process.shutil.which", lambda name: None)
    cfg = Config(home=tmp_path / "home", repo="o/r", clone=tmp_path / "clone", agent="claude")
    with pytest.raises(RuntimeError, match=r"Claude Code \(`claude`\) is not on PATH.*npm install"):
        review.plan_review(cfg, 7, ledger_entry=None)


def test_vscode_urls():
    from readthrough.agents import Claude, Copilot

    assert Copilot().editor_url("abc") == "vscode://agents/agent-host-session/copilotcli/abc"
    assert Claude().editor_url("abc") is None
    url = review.vscode_new_session_url("review #1 & more", None)
    assert url.startswith("vscode://agents/new?prompt=review%20%231%20%26%20more")


def _session(root: Path, sid: str, first: str | None, client: str = "github/cli") -> None:
    folder = root / "session-state" / sid
    folder.mkdir(parents=True)
    events = [{"type": "session.start", "data": {}}]
    if first is not None:
        events.append({"type": "user.message", "data": {"content": first}})
    (folder / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    (folder / "workspace.yaml").write_text(
        f"id: {sid}\nclient_name: {client}\nname: {(first or '')[:40]}\n", encoding="utf-8"
    )


def _claude_session(root: Path, sid: str, first: str) -> None:
    project = root / "projects" / "c--Users-me-src-r"
    project.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "queue-operation", "sessionId": sid},
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "a subagent's prompt"}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": first}]}, "sessionId": sid},
    ]
    (project / f"{sid}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_sessions_are_linked_to_prs_by_the_prompt_marker(tmp_path: Path):
    root = tmp_path / "copilot"
    cfg = Config(home=tmp_path / "home", repo="o/r", clone=tmp_path)
    primary = review.default_session_id(cfg, 7)
    vs = "11111111-1111-1111-1111-111111111111"
    _session(root, primary, review.review_prompt(cfg, 7, Path("b.md")))
    # A marker written before there were forges names a GitHub repo by its path alone; it still links.
    _session(root, vs, review.pr_marker("O/R", 7) + " please review", client="vscode-agent-host")
    _session(
        root,
        "22222222-2222-2222-2222-222222222222",
        "talking about [readthrough:github/o/r#8] later",
        client="github/cli",
    )
    _session(root, "33333333-3333-3333-3333-333333333333", None)
    _session(root, "44444444-4444-4444-4444-444444444444", "unrelated")
    _session(root, "55555555-5555-5555-5555-555555555555", "[readthrough:gitlab/o/r#7] same path, other forge")
    # A Claude Code session started from a pasted prompt is linked the same way, by its first (non-subagent) message.
    claude = "66666666-6666-6666-6666-666666666666"
    _claude_session(tmp_path / "claude", claude, review.pr_marker("github/o/r", 7) + " in Claude Code")

    index = review.session_index(cfg)
    assert index.scan(force=True)
    found = index.sessions_for(cfg.slug, 7)
    assert set(found) == {primary, vs, claude}
    assert found[claude]["agent"] == "claude" and found[vs]["agent"] == "copilot"
    assert "33333333-3333-3333-3333-333333333333" not in json.loads((cfg.home / "session-index.json").read_text())
    assert set(review.session_index(cfg).sessions_for(cfg.slug, 8)) == {"22222222-2222-2222-2222-222222222222"}
    assert set(index.sessions_for("gitlab/o/r", 7)) == {"55555555-5555-5555-5555-555555555555"}

    sessions = review.pr_sessions(cfg, 7, primary)
    assert sessions[0]["id"] == primary and {s["id"] for s in sessions[1:]} == {vs, claude}
    by_id = {s["id"]: s for s in sessions}
    assert (by_id[primary]["where"], by_id[vs]["where"], by_id[claude]["where"]) == ("terminal", "VS Code", "terminal")
    assert (by_id[vs]["agent_label"], by_id[claude]["agent_label"]) == ("Copilot", "Claude Code")
    assert by_id[primary]["editor_url"] and by_id[claude]["editor_url"] is None

    launch = review.plan_review(cfg, 7, ledger_entry=None, session_id=vs)
    assert launch.resuming and launch.argv[1] == f"--resume={vs}"
    # Resuming the Claude Code session resumes it in Claude Code, although Copilot is this machine's default.
    launch = review.plan_review(cfg, 7, ledger_entry=None, session_id=claude)
    assert launch.agent == "claude" and launch.argv[1:3] == ["--resume", claude]
    with pytest.raises(ValueError, match="isn't linked"):
        review.plan_review(cfg, 7, ledger_entry=None, session_id="44444444-4444-4444-4444-444444444444")


def test_review_prompt_starts_with_the_marker(tmp_path: Path):
    prompt = review.review_prompt(_cfg(tmp_path), 7, Path("b.md"))
    assert prompt.startswith("[readthrough:github/o/r#7] ")
    assert "for o/r#7" in prompt
    match = review.MARKER_RE.search(prompt)
    assert match is not None and match.groups() == ("github/o/r", "7")
    ado = review.MARKER_RE.search(review.pr_marker("ado/org/proj/repo", 12))
    assert ado is not None and ado.groups() == ("ado/org/proj/repo", "12")


def test_ensure_worktree_replaces_one_whose_clone_moved(tmp_path: Path, monkeypatch, fake_forge):
    """A worktree made before the clone or the data directory moved points at a gitdir that is gone. It is
    moved aside, never deleted, and a fresh one is added; a healthy one is just checked out."""
    clone = tmp_path / "clone"
    cfg = _cfg(tmp_path, clone=clone)
    path = review.worktree_path(cfg, 7)
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: /old/place/.git/worktrees/pr-7", encoding="utf-8")
    (path / "notes.txt").write_text("edits in the stale checkout", encoding="utf-8")
    calls: list[list[str]] = []
    attached = {"ok": False}

    def fake_run(cmd: list[str], *, check: bool = True, **_kw) -> str:
        calls.append(cmd)
        if cmd[3:5] == ["worktree", "repair"]:
            return ""
        if cmd[3:] == ["rev-parse", "--git-common-dir"]:
            if not attached["ok"]:
                raise review.ForgeError("fatal: not a git repository", what="git")
            return str(clone / ".git")
        if cmd[3:5] == ["worktree", "add"]:
            attached["ok"] = True
            Path(cmd[-2]).mkdir()
            (Path(cmd[-2]) / ".git").write_text("gitdir: new", encoding="utf-8")
        return "deadbeef\n"

    monkeypatch.setattr(review, "run", fake_run)
    where, merge_base, head = review.ensure_worktree(cfg, 7)

    assert (where, merge_base, head) == (path, "deadbeef", "deadbeef")
    assert [c[3] for c in calls] == ["fetch", "worktree", "rev-parse", "worktree", "rev-parse", "merge-base"]
    assert calls[1][4] == "repair" and calls[3][4] == "add"
    aside = [p for p in path.parent.iterdir() if p.name.startswith("pr-7.detached-")]
    assert len(aside) == 1 and (aside[0] / "notes.txt").exists(), "what was in it is kept, not deleted"

    calls.clear()
    review.ensure_worktree(cfg, 7)
    assert [c[3] for c in calls] == ["fetch", "worktree", "rev-parse", "checkout", "rev-parse", "merge-base"]
