"""The GitHub adapter under failure: classification, retries, timeouts, caching, pagination, and partial builds.

The stderr strings are the real ones from a server log (connection resets, unreachable API, oversized diffs).
"""

import json
import logging
import sys
from pathlib import Path

import pytest
from fakes import FakeForge

from readthrough import process
from readthrough.faults import (
    AuthError,
    ForgeError,
    NetworkError,
    NotFound,
    RateLimited,
    Timeout,
    TooLarge,
    classify_http,
    classify_stderr,
)
from readthrough.forges import github
from readthrough.forges.github import GitHub
from readthrough.model import known
from readthrough.process import Secret, describe
from readthrough.tuning import default_tuning, load_tuning


def test_tokens_are_secrets_and_never_shown(monkeypatch):
    token = Secret("gho_abc123")
    assert repr(token) == str(token) == "Secret(***)" and token.reveal() == "gho_abc123"
    assert token == Secret("gho_abc123") and token != Secret("other") and token != "gho_abc123"
    monkeypatch.setattr(process, "_seen_secrets", [])
    assert process.redact("token gho_abc123 rejected") == "token gho_abc123 rejected", "unseen values aren't guessed"
    process.remember(token)
    assert process.redact("HTTP 401: token gho_abc123 rejected") == "HTTP 401: token *** rejected"


def test_token_for_runs_gh_once_per_account(monkeypatch):
    calls = []

    class Proc:
        returncode = 0
        stdout = "gho_secret\n"

    monkeypatch.setattr(github, "_tokens", {})
    monkeypatch.setattr(process, "_seen_secrets", [])
    monkeypatch.setattr(process.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Proc())
    assert github.token_for("me").reveal() == "gho_secret"
    assert github.token_for("me").reveal() == "gho_secret"
    assert len(calls) == 1 and calls[0][-3:] == ["token", "--user", "me"]
    assert process.redact("echoed gho_secret") == "echoed ***", "a token read from gh is redacted from then on"
    Proc.returncode = 1
    with pytest.raises(ForgeError, match="isn't signed in as other"):
        github.token_for("other")


RESET = (
    'Post "https://api.github.com/graphql": read tcp 10.5.197.14:52827->20.29.134.17:443: wsarecv: '
    "An existing connection was forcibly closed by the remote host."
)
UNREACHABLE = "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com"
TOO_LARGE = (
    "could not find pull request diff: HTTP 406: Sorry, the diff exceeded the maximum number of files (300). "
    "Consider using 'List pull requests files' API or locally cloning the repository instead."
)


@pytest.mark.parametrize(
    ("stderr", "cls"),
    [
        (RESET, NetworkError),
        (UNREACHABLE, NetworkError),
        (
            (
                'Get "https://api.github.com/x": proxyconnect tcp: dial tcp 127.0.0.1:9: connectex: No connection could be '
                "made because the target machine actively refused it."
            ),
            NetworkError,
        ),
        ("dial tcp: lookup api.github.com: no such host", NetworkError),
        ("HTTP 502: Bad Gateway (https://api.github.com/graphql)", NetworkError),
        (TOO_LARGE, TooLarge),
        ("gh: Not Found (HTTP 404)", NotFound),
        ("GraphQL: Could not resolve to a Repository with the name 'o/nope'. (repository)", NotFound),
        ("HTTP 429: You have exceeded a secondary rate limit", RateLimited),
        ("HTTP 403: API rate limit exceeded for user ID 1", RateLimited),
        ("HTTP 401: Bad credentials (https://api.github.com/user)", AuthError),
        ("HTTP 403: Resource protected by organization SAML enforcement", AuthError),
        ("GraphQL: Field 'nope' doesn't exist on type 'Repository'", ForgeError),
        ("glab: HTTP 401 Unauthorized. Run 'glab auth login' to authenticate.", AuthError),
        ("ERROR: Please run 'az login' to setup account.", AuthError),
    ],
)
def test_classify_real_cli_errors(stderr, cls):
    assert classify_stderr(stderr) is cls


@pytest.mark.parametrize(
    ("status", "body", "cls"),
    [
        (404, '{"message":"404 Project Not Found"}', NotFound),
        (401, "", AuthError),
        (403, '{"message":"403 Forbidden"}', AuthError),
        (403, "API rate limit exceeded", RateLimited),
        (429, "", RateLimited),
        (406, "diff too large", TooLarge),
        (502, "Bad Gateway", NetworkError),
        (503, "", NetworkError),
        (422, '{"message":"Unprocessable"}', ForgeError),
    ],
)
def test_classify_http_statuses(status, body, cls):
    assert classify_http(status, body) is cls


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        (
            ["gh", "api", "graphql", "-f", "query=query PullRequests($o:String!){x}", "-F", "number=12"],
            "graphql PullRequests #12",
        ),
        (
            ["gh", "api", "-H", "Accept: application/vnd.github.raw", "repos/o/r/contents/a.md"],
            "GET repos/o/r/contents/a.md",
        ),
        (["gh", "api", "-X", "POST", "repos/o/r/issues"], "POST repos/o/r/issues"),
        (["gh", "pr", "diff", "42", "-R", "o/r"], "pr diff #42"),
    ],
)
def test_describe_names_calls_readably(cmd, expected):
    assert describe(cmd) == expected


class Script:
    """Stands in for the adapter's ``run``: replays a list of outcomes (exceptions or outputs) and records calls."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingGitHub(GitHub):
    """A real client whose waits are recorded instead of slept."""

    def __init__(self, repo: str, tuning):
        super().__init__(repo, None, tuning)
        self.slept: list[float] = []
        self.sleep = self.slept.append


def _gh(monkeypatch, script, **tuning) -> RecordingGitHub:
    monkeypatch.setattr(github, "run", script)
    return RecordingGitHub("o/r", load_tuning({"forge": tuning}).forge if tuning else default_tuning().forge)


def _err(cls, stderr):
    return cls(f"x failed: {stderr.splitlines()[0]}", what="graphql PullRequests", stderr=stderr)


def test_temporary_failures_are_retried_with_growing_waits(monkeypatch, caplog):
    script = Script(_err(NetworkError, RESET), _err(NetworkError, UNREACHABLE), "ok")
    gh = _gh(monkeypatch, script, retry_backoff_seconds=2.0, retry_max_backoff_seconds=30.0)
    with caplog.at_level(logging.WARNING, logger="readthrough"):
        assert gh.api("user") == "ok"
    assert len(script.calls) == 3
    assert 2.0 <= gh.slept[0] <= 2.5 and 4.0 <= gh.slept[1] <= 5.0
    assert "attempt 1 of 4" in caplog.text and "forcibly closed" in caplog.text


def test_retries_give_up_with_a_clear_message(monkeypatch):
    gh = _gh(monkeypatch, Script(_err(NetworkError, RESET)), retries=2)
    with pytest.raises(NetworkError, match="gave up after 3 attempts"):
        gh.api("user")


def test_permanent_failures_are_not_retried(monkeypatch):
    for cls in (NotFound, AuthError, TooLarge, ForgeError):
        script = Script(_err(cls, "nope"))
        gh = _gh(monkeypatch, script)
        with pytest.raises(cls):
            gh.api("user")
        assert len(script.calls) == 1 and gh.slept == []


def test_rate_limits_wait_the_configured_time(monkeypatch):
    gh = _gh(monkeypatch, Script(_err(RateLimited, "HTTP 429"), "ok"), rate_limit_wait_seconds=42.0)
    assert gh.api("user") == "ok" and gh.slept == [42.0]


def test_absent_means_404_only(monkeypatch):
    gh = _gh(monkeypatch, Script(_err(NotFound, "HTTP 404")))
    assert gh.read_file("x.md") is None and gh.api_or_none("repos/o/r/x") is None
    gh = _gh(monkeypatch, Script(_err(NetworkError, RESET)), retries=0)
    with pytest.raises(NetworkError):
        gh.read_file("x.md")


def test_hung_commands_time_out():
    with pytest.raises(Timeout, match="timed out"):
        process.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)


def test_missing_tool_is_reported_plainly():
    with pytest.raises(ForgeError, match="isn't installed"):
        process.run(["definitely-not-a-real-tool-xyz"])


def test_graphql_sends_only_integers_as_typed_values(monkeypatch):
    script = Script(json.dumps({"data": {"ok": 1}}))
    gh = _gh(monkeypatch, script)
    gh.graphql("query Q{x}", owner="123", name="true", number=7)
    cmd = script.calls[0]
    assert cmd[cmd.index("owner=123") - 1 : cmd.index("owner=123") + 1] == ["-f", "owner=123"]
    assert cmd[cmd.index("number=7") - 1 : cmd.index("number=7") + 1] == ["-F", "number=7"]


# ---------------------------------------------------------------- the diff cache


def test_failed_diffs_are_never_cached(monkeypatch, tmp_path: Path):
    gh = _gh(monkeypatch, Script(_err(NetworkError, RESET)), retries=0)
    with pytest.raises(NetworkError):
        gh.diff(5, "a" * 40, tmp_path)
    assert list(tmp_path.iterdir()) == []
    gh = _gh(monkeypatch, Script("diff --git a/x b/x\n"))
    assert gh.diff(5, "a" * 40, tmp_path) == ("diff --git a/x b/x\n", True)
    assert gh.diff(5, "a" * 40, tmp_path) == ("diff --git a/x b/x\n", False)


def test_oversized_diffs_are_remembered_per_commit(monkeypatch, tmp_path: Path):
    script = Script(_err(TooLarge, TOO_LARGE))
    gh = _gh(monkeypatch, script)
    for _ in range(2):
        with pytest.raises(TooLarge):
            gh.diff(9, "b" * 40, tmp_path)
    assert len(script.calls) == 1 and (tmp_path / f"pr-9-{'b' * 12}.toolarge").exists()
    gh = _gh(monkeypatch, Script("new diff"))
    assert gh.diff(9, "c" * 40, tmp_path) == ("new diff", True)
    assert not list(tmp_path.glob("*.toolarge"))


def test_empty_cached_diffs_from_old_versions_are_refetched(monkeypatch, tmp_path: Path):
    (tmp_path / f"pr-3-{'d' * 12}.diff").write_text("", encoding="utf-8")
    gh = _gh(monkeypatch, Script("real"))
    assert gh.diff(3, "d" * 40, tmp_path) == ("real", True)


def test_a_legitimately_empty_diff_is_cached_too(monkeypatch, tmp_path: Path):
    """GitHub serves no text for some PRs (binary-only). That answer is remembered, so a refresh doesn't re-ask."""
    script = Script("", "should not be asked again")
    gh = _gh(monkeypatch, script)
    assert gh.diff(4, "e" * 40, tmp_path) == ("", True)
    assert gh.diff(4, "e" * 40, tmp_path) == ("", False)
    assert len(script.calls) == 1 and (tmp_path / f"pr-4-{'e' * 12}.empty").exists()
    gh = _gh(monkeypatch, Script("now has text"))
    assert gh.diff(4, "f" * 40, tmp_path) == ("now has text", True)
    assert not list(tmp_path.glob("*.empty")), "a new head forgets the old answer"


def test_diffs_report_failures_per_pr(monkeypatch, tmp_path: Path, make_pull):
    def fake(cmd, **kwargs):
        n = cmd[3]
        if n == "1":
            return "ok"
        raise (_err(TooLarge, TOO_LARGE) if n == "2" else _err(NetworkError, RESET))

    gh = _gh(monkeypatch, fake, retries=0)
    prs = [make_pull(number=n, headRefOid=str(n) * 40) for n in (1, 2, 3)]
    texts, failures, fetched = gh.diffs(prs, tmp_path)
    assert texts == {1: "ok"} and fetched == 1
    assert isinstance(failures[2], TooLarge) and isinstance(failures[3], NetworkError)


def test_cache_drops_empty_and_long_gone_diffs(monkeypatch, tmp_path: Path, make_pull):
    import os
    import time

    old = time.time() - 30 * 86400
    keep, gone, empty, recent = (tmp_path / f"pr-{n}-{'a' * 12}.diff" for n in (1, 2, 3, 4))
    for path, text in ((keep, "x"), (gone, "x"), (empty, ""), (recent, "x")):
        path.write_text(text, encoding="utf-8")
    for path in (keep, gone, empty):
        os.utime(path, (old, old))
    gh = _gh(monkeypatch, Script("x"))
    gh.diffs([make_pull(number=1, headRefOid="a" * 40)], tmp_path)
    assert keep.exists() and recent.exists() and not gone.exists() and not empty.exists()


# ---------------------------------------------------------------- pagination


def _conn(nodes, total, cursor=None):
    return {"nodes": nodes, "totalCount": total, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def test_big_prs_are_fetched_in_pages_up_to_the_cap(monkeypatch):
    files = [{"path": f"f{i}", "additions": 1, "deletions": 0, "changeType": "MODIFIED"} for i in range(250)]
    pr = {
        "number": 7, "headRefOid": "e" * 40,
        "files": _conn(files[:100], 250, "c1"),
        "reviewThreads": _conn([], 0),
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": _conn([], 0)}}}]},
    }  # fmt: skip
    pages = {"c1": _conn(files[100:200], 250, "c2"), "c2": _conn(files[200:], 250)}

    def fake(cmd, **kwargs):
        cursor = next(a.split("=", 1)[1] for a in cmd if a.startswith("cursor="))
        return json.dumps({"data": {"repository": {"pullRequest": {"files": pages[cursor]}}}})

    gh = _gh(monkeypatch, fake)
    gh.complete_pr(pr)
    assert len(pr["files"]["nodes"]) == 250 and "truncated" not in pr

    pr["files"] = _conn(files[:100], 250, "c1")
    gh = _gh(monkeypatch, fake, max_files_per_pr=150)
    gh.complete_pr(pr)
    assert len(pr["files"]["nodes"]) == 150 and pr["truncated"] == {"files": [150, 250]}


# ---------------------------------------------------------------- partial builds


class FlakyForge(FakeForge):
    """A forge whose optional calls fail, to check the build carries on and says what's missing."""

    def ci_context(self, bases):
        raise NetworkError("graphql DefaultBranch failed: reset", stderr=RESET)

    def read_file(self, path):
        if path.endswith(".md"):
            raise NetworkError("GET contents failed: reset", stderr=RESET)

    def list_dir(self, path):
        return ["kusto.instructions.md"]

    def fetch_diff(self, number):
        raise TooLarge("too large", stderr=TOO_LARGE)


def test_a_build_degrades_instead_of_failing(make_pr, tmp_path: Path, fake_forge):
    from conftest import PROFILE

    from readthrough.build import analyze_snapshot, fetch_snapshot
    from readthrough.config import Config

    pr = make_pr(number=11, changedFiles=888)
    pr["truncated"] = {"files": [100, 888]}
    fake_forge.prs, fake_forge.total = [pr], 2753
    cfg = Config(home=tmp_path, repo="o/r", limit=1)
    snap = fetch_snapshot(cfg, PROFILE, forge=FlakyForge("o/r"))
    codes = [w["code"] for w in snap.warnings]
    assert codes == ["limit", "truncated", "ci", "rules"]
    assert "1 most recently active of 2753" in snap.warnings[0]["message"]
    assert "Couldn't fetch 2 rule or contract files" in snap.warnings[3]["message"]
    assert "kusto.instructions.md" in " ".join(snap.warnings[3]["details"])
    assert snap.diff_problems == {11: "too-large"}
    assert not snap.main_failing.known, "an unreadable default branch is not 'nothing failing'"
    item = analyze_snapshot(snap, PROFILE)["items"][0]
    flags = {f["id"]: f["msg"] for f in item["flags"]}
    assert "888 files" in flags["diff-too-large"] and "100 of 888 files" in flags["partial"]
    assert item["lane"] != "blocked"


class NoDiffForge(FakeForge):
    capabilities = FakeForge.capabilities - {"diffs", "memberships"}
    label = "Azure DevOps"


def test_unsupported_capabilities_are_said_not_assumed(make_pr, tmp_path: Path, fake_forge):
    from conftest import PROFILE

    from readthrough.build import analyze_snapshot, fetch_snapshot
    from readthrough.config import Config

    pr = make_pr(number=3)
    pr["authorAssociation"] = None
    fake_forge.prs = [pr]
    profile = PROFILE
    profile.authors.internal_orgs = ["octo"]
    try:
        snap = fetch_snapshot(Config(home=tmp_path, repo="o/r"), profile, forge=NoDiffForge("o/r"))
    finally:
        profile.authors.internal_orgs = []
    codes = {w["code"] for w in snap.warnings}
    assert {"diffs", "members"} <= codes
    assert snap.diff_problems == {3: "unsupported"} and snap.diffs == {}
    assert any("Azure DevOps can't answer" in d for w in snap.warnings for d in w["details"])
    item = analyze_snapshot(snap, profile)["items"][0]
    assert any(f["id"] == "diff-unsupported" for f in item["flags"])
    assert item["smells"] == []


def test_a_local_profile_means_the_repo_file_is_not_fetched(tmp_path: Path):
    from readthrough.config import Config
    from readthrough.profile import resolve_profile

    class Unreachable(FakeForge):
        def read_files(self, paths):
            raise AssertionError("fetched the repo's profile despite a local one")

    cfg = Config(home=tmp_path, repo="o/app")
    cfg.repo_dir.mkdir(parents=True)
    cfg.local_profile_path.write_text(
        'schema = 1\nrepo = "o/app"\n[[layers]]\nid = "l"\nlabel = "L"\n[[components]]\nid = "a"\nlabel = "A"\n'
        'layer = "l"\npaths = ["**"]\n',
        encoding="utf-8",
    )
    assert resolve_profile(cfg, Unreachable("o/app")).repo == "o/app"


def test_the_repo_profile_is_found_at_any_of_its_paths(tmp_path: Path, fake_forge):
    from readthrough.config import Config
    from readthrough.profile import profile_sources, repo_profile_paths

    text = 'schema = 1\nrepo = "o/app"\n[[layers]]\nid = "l"\nlabel = "L"\n[[components]]\nid = "a"\nlabel = "A"\nlayer = "l"\npaths = ["**"]\n'
    cfg = Config(home=tmp_path, repo="o/app")
    forge = FakeForge("o/app")
    assert repo_profile_paths(forge) == [".github/readthrough.toml", ".readthrough.toml", "readthrough.toml"]
    assert profile_sources(cfg, forge) == []

    fake_forge.files = {"readthrough.toml": text}
    (source,) = profile_sources(cfg, forge)
    assert source.kind == "repo" and source.label == "o/app:readthrough.toml" and source.read() == text
    assert source.load().notes == []

    fake_forge.files = {"readthrough.toml": text, ".github/readthrough.toml": text}
    (source,) = profile_sources(cfg, forge)
    assert source.label == "o/app:.github/readthrough.toml", "the forge's dot-directory wins"
    (note,) = source.load().notes
    assert "more than one profile" in note and "readthrough.toml" in note


def test_required_checks_from_protection_and_rulesets(monkeypatch):
    protection = json.dumps(
        {"protection": {"required_status_checks": {"contexts": ["tests"], "checks": [{"context": "lint"}]}}}
    )
    rules = json.dumps(
        [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "e2e"}]}}]
    )
    gh = _gh(monkeypatch, Script(protection, rules))
    assert gh.required_checks("main") == known({"tests", "lint", "e2e"})
    gh = _gh(monkeypatch, Script(_err(AuthError, "HTTP 403")))
    unseen = gh.required_checks("main")
    assert not unseen.known and "isn't visible" in unseen.reason
