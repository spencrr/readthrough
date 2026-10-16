"""The REST adapters (GitLab, Forgejo, Azure DevOps) against responses recorded from the real public forges.

``scripts/probe_forge.py --record`` wrote the fixtures under ``tests/fixtures/forges/<forge>/``: one JSON file
per request, keyed by a hash of ``METHOD url``, plus an index. ``replay`` serves them in place of ``Rest.call``,
so these tests exercise the adapters' URL building and normalization offline. A request the recording
never saw fails loudly rather than returning something made up.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from readthrough import faults, forges
from readthrough.faults import ForgeError
from readthrough.forges import Response, Rest, retry_after
from readthrough.forges.azure_devops import AzureDevOps
from readthrough.forges.forgejo import Forgejo
from readthrough.forges.gitlab import GitLab
from readthrough.model import PullRequest
from readthrough.tuning import load_tuning

FIXTURES = Path(__file__).parent / "fixtures" / "forges"
TUNING = load_tuning({"forge": {"max_tree_files": 1000, "retries": 0}}).forge


@pytest.fixture
def replay(monkeypatch: pytest.MonkeyPatch):
    """Point ``Rest.call`` at a fixture directory. Returns the list of URLs the adapter asked for."""
    asked: list[str] = []

    def install(forge: str) -> list[str]:
        directory = FIXTURES / forge
        index = {(e["method"], e["url"]): e for e in json.loads((directory / "index.json").read_text("utf-8"))}

        def call(self: Rest, method: str, path: str, **kw: Any) -> Response | None:
            url = self.url(path, kw.get("params"))
            asked.append(url)
            if (method, url) not in index:
                raise AssertionError(f"no recorded response for {method} {url}")
            entry = index[(method, url)]
            if entry.get("error"):  # the forge refused; raise what the transport raised then
                cls = getattr(faults, entry["error"]["type"])
                if kw.get("absent") and issubclass(cls, kw["absent"]):
                    return None
                raise cls(entry["error"]["message"], what=path, stderr=entry["error"]["stderr"])
            if entry["file"] is None:  # a 404 the adapter asked to have as None
                return None
            data = json.loads((directory / entry["file"]).read_text("utf-8"))
            assert hashlib.sha1(f"{method} {url}".encode()).hexdigest()[:12] == entry["file"][:-5]  # noqa: S324 - a key, not security
            return Response(data["status"], data["headers"], data["text"])

        monkeypatch.setattr(Rest, "call", call)
        return asked

    return install


def _check_common(prs: list[PullRequest]) -> None:
    for pr in prs:
        assert pr.number > 0 and pr.title and pr.url.startswith("https://") and pr.head
        assert pr.created <= pr.updated
        assert all(f.path and not f.path.startswith("/") for f in pr.files)
        if pr.decision.known:
            assert pr.decision.value in ("APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", "NONE")
        assert pr.mergeable.known and pr.mergeable.value in ("MERGEABLE", "CONFLICTING", "UNKNOWN")
        for name, cap in pr.gaps():
            assert cap.reason, f"{name} is {cap.kind} without saying why"


# ---------------------------------------------------------------- Forgejo, recorded from codeberg.org/forgejo/forgejo


def test_forgejo_reads_a_public_repo_anonymously(replay, monkeypatch):
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    asked = replay("forgejo")
    forge = Forgejo("forgejo/forgejo", tuning=TUNING)
    assert forge.describe_auth() == "anonymous"
    info = forge.repo_info()
    assert info["full_name"] == "forgejo/forgejo" and info["default_branch"] == "forgejo" and not info["private"]

    viewer, prs, total = forge.fetch_prs(2)
    assert viewer == "" and total > len(prs) == 2
    _check_common(prs)
    first = prs[0]
    assert first.number == 14544 and first.base == "forgejo" and prs[1].base == "v15.0/forgejo"
    assert [f.path for f in first.files][:1] == ["routers/api/v1/repo/file.go"]
    assert first.additions == 147 and first.deletions == 190 and first.line_counts.known
    assert first.labels == ["code/api", "test/present"] and first.requested == [
        "Gusted",
        "Cyborus",
        "mfenniak",
        "famfo-cb",
    ]
    assert first.last_commit is not None
    assert first.checks.known and first.checks.value
    assert all(c.url is None or c.url.startswith("https://codeberg.org/") for c in first.checks.value)
    # Without a token the collaborator list is closed, so the association is reported as unavailable, not guessed.
    assert first.association.kind == "unavailable" and "token" in first.association.reason
    assert first.threads.known, first.threads.reason

    branch, merged = forge.merged_prs(2)
    assert branch == "forgejo" and all(m.files for m in merged)
    tree, complete = forge.tree(branch)
    assert complete is False and len(tree) == 1000, "the probe capped the tree, and the adapter says so"
    files = forge.read_files(["README.md", "CODEOWNERS"])
    assert set(files) == {"README.md", "CODEOWNERS"} and "Forgejo" in files["README.md"]
    assert forge.read_file("definitely/not/here.md") is None
    assert forge.list_dir(".devcontainer") == ["devcontainer.json"]

    context = forge.ci_context({pr.base for pr in prs})
    assert context.default_branch == "forgejo"
    assert all(not cap.known and "token" in cap.reason for cap in context.required.values())
    assert context.main_failing.known
    diff = forge.fetch_diff(first.number)
    assert diff.startswith("diff --git a/routers/api/v1/repo/file.go")
    assert all(u.startswith("https://codeberg.org/api/v1/") for u in asked)


def test_forgejo_urls_and_refs():
    forge = Forgejo("octo/courier", tuning=TUNING)
    assert forge.web_url() == "https://codeberg.org/octo/courier"
    assert forge.pr_url(12) == "https://codeberg.org/octo/courier/pulls/12"
    assert forge.pr_ref(12) == "pull/12/head"
    assert forge.file_url("docs/a.md", "main") == "https://codeberg.org/octo/courier/src/branch/main/docs/a.md"
    assert forge.new_file_url(".forgejo/readthrough.toml", "main") is not None
    assert Forgejo.validate_repo("nope") and Forgejo.validate_repo("o/r") is None


# ---------------------------------------------------------------- Azure DevOps, recorded from dev.azure.com/powershell


def test_azure_devops_reads_a_public_project(replay, monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.setattr("readthrough.forges.azure_devops._az_token", lambda: None)
    replay("ado")
    forge = AzureDevOps("powershell/PowerShell/PSGallery-Cache-Repo", tuning=TUNING)
    assert forge.describe_auth() == "anonymous"
    assert "diffs" not in forge.capabilities
    info = forge.repo_info()
    assert info["full_name"] == "powershell/PowerShell/PSGallery-Cache-Repo" and info["default_branch"] == "main"

    viewer, prs, total = forge.fetch_prs(2)
    assert viewer == "" and total == 1 and len(prs) == 1
    _check_common(prs)
    pr = prs[0]
    assert (
        pr.number == 53
        and pr.url == "https://dev.azure.com/powershell/PowerShell/_git/PSGallery-Cache-Repo/pullrequest/53"
    )
    assert [f.path for f in pr.files] == [".pipeline/feed-sync.yml"] and pr.files[0].change == "ADDED"
    assert pr.line_counts.kind == "unsupported" and pr.additions == pr.deletions == 0
    assert pr.association.kind == "unsupported"
    assert pr.threads.known and pr.checks.known and pr.last_commit
    assert pr.updated >= pr.last_commit

    branch, merged = forge.merged_prs(2)
    assert branch == "main" and merged == [], "this repo has never completed a PR"
    tree, complete = forge.tree(branch)
    assert complete and tree == ["es-metadata.yml"]
    assert forge.read_file("definitely/not/here.md") is None
    context = forge.ci_context({"main"})
    # Branch policies need sign-in on Azure DevOps: unavailable, with the reason, never an empty set.
    assert not context.required["main"].known and "sign-in" in context.required["main"].reason
    assert context.main_failing.known
    with pytest.raises(ForgeError, match="no unified diff"):
        forge.fetch_diff(53)


def test_azure_devops_steps_aside_when_the_org_refuses_the_az_identity(monkeypatch):
    from readthrough.faults import AuthError

    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.setattr("readthrough.forges.azure_devops._az_token", lambda: "eyJ-not-a-real-token")
    forge = AzureDevOps("o/p/r", tuning=TUNING)
    assert forge.describe_auth() == "az"
    attempts: list[str | None] = []

    def call(self: Rest, method: str, path: str, **kw: Any) -> Response:
        attempts.append(self.cred.source)
        if self.cred.token:
            raise AuthError("GET x failed: HTTP 401", what=path, stderr="HTTP 401")
        return Response(200, {}, json.dumps({"id": "1", "name": "r", "defaultBranch": "refs/heads/main"}))

    monkeypatch.setattr(Rest, "call", call)
    assert forge.repo_info()["default_branch"] == "main"
    assert attempts == ["az", "anonymous"] and forge.describe_auth() == "anonymous"


def test_azure_devops_urls():
    forge = AzureDevOps("org/proj/repo", tuning=TUNING)
    assert forge.pr_ref(5) == "pull/5/merge"
    assert (
        forge.file_url("docs/a.md", "main") == "https://dev.azure.com/org/proj/_git/repo?path=/docs/a.md&version=GBmain"
    )
    assert AzureDevOps.validate_repo("o/r") and AzureDevOps.validate_repo("o/p/r") is None


# ---------------------------------------------------------------- GitLab, recorded from gitlab.com/gitlab-org/cli


def test_gitlab_reads_a_public_project_anonymously(replay, monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr("readthrough.forges.gitlab._glab_token", lambda host: None)
    replay("gitlab")
    forge = GitLab("gitlab-org/cli", tuning=TUNING)
    assert forge.describe_auth() == "anonymous" and forge.pr_noun == "merge request"
    info = forge.repo_info()
    assert info["full_name"] == "gitlab-org/cli" and info["default_branch"] == "main" and not info["private"]

    viewer, prs, total = forge.fetch_prs(2)
    assert viewer == "" and total >= len(prs) == 2
    _check_common(prs)
    for pr in prs:
        assert pr.url.startswith("https://gitlab.com/gitlab-org/cli/-/merge_requests/")
        assert pr.files and pr.line_counts.known
        assert pr.checks.kind in ("known", "unavailable")
        # gitlab.com serves discussions only to signed-in users, and the review decision rests on them: both are
        # reported as unavailable rather than the MR being called approved or ready.
        assert pr.threads.kind == "unavailable" and "401" in pr.threads.reason
        assert pr.decision.kind == "unavailable" and "discussions" in pr.decision.reason
        assert pr.association.kind == "unavailable"
    branch, merged = forge.merged_prs(2)
    assert branch == "main" and len(merged) == 2
    tree, complete = forge.tree(branch)
    assert len(tree) <= 1000 and complete is False, "gitlab-org/cli has more than a thousand entries"
    assert forge.read_file("definitely/not/here.md") is None
    context = forge.ci_context({pr.base for pr in prs})
    assert context.default_branch == "main"
    assert all(cap.kind == "unsupported" for cap in context.required.values()), "GitLab has no required checks"


def test_gitlab_urls_and_nested_groups():
    forge = GitLab("group/sub/project", tuning=TUNING)
    assert forge.web_url() == "https://gitlab.com/group/sub/project"
    assert forge.pr_url(7) == "https://gitlab.com/group/sub/project/-/merge_requests/7"
    assert forge.pr_ref(7) == "merge-requests/7/head"
    assert GitLab.validate_repo("single") and GitLab.validate_repo("a/b/c") is None
    assert forges.slug("gitlab", "group/sub/project") == "gitlab/group/sub/project"


# ---------------------------------------------------------------- the shared transport


def test_retry_after_prefers_the_forges_own_number():
    assert retry_after("30") == 30.0
    assert retry_after(None, str(1_700_000_060), now=1_700_000_000.0) == 60.0
    assert retry_after("soon", None) is None
    assert retry_after(None, None) is None


def test_rate_limited_waits_use_retry_after_up_to_the_configured_cap():
    from readthrough.faults import RateLimited
    from readthrough.forges import backoff

    tuning = load_tuning({"forge": {"rate_limit_wait_seconds": 60.0}}).forge
    exc = RateLimited("429", stderr="HTTP 429")
    assert backoff(1, tuning, exc) == 60.0
    exc.retry_after = 12.0
    assert backoff(1, tuning, exc) == 12.0
    exc.retry_after = 751.0  # GitLab's second limiter asks for twelve minutes; we don't wait that long
    assert backoff(1, tuning, exc) == 60.0


def test_glab_token_is_read_from_its_status_report():
    """glab writes `auth status` to stderr, one indented `Token:` line per host, masked unless --show-token."""
    from readthrough.forges.gitlab import parse_glab_token

    report = "gitlab.com\n  ✓ Logged in to gitlab.com as someone (keyring)\n  ✓ Token: glpat-abc123\n"
    assert parse_glab_token(report) == "glpat-abc123"
    assert parse_glab_token(report.replace("glpat-abc123", "**************************")) is None
    assert parse_glab_token("No GitLab instance has been authenticated with glab.") is None


def test_quiet_output_can_include_stderr(monkeypatch):
    from readthrough import process

    class Proc:
        returncode = 0
        stdout = ""
        stderr = "gitlab.com\n  Token: glpat-x\n"

    monkeypatch.setattr(process.subprocess, "run", lambda cmd, **kw: Proc())
    assert process.quiet_output(["glab", "auth", "status"]) == ""
    assert "Token: glpat-x" in (process.quiet_output(["glab", "auth", "status"], with_stderr=True) or "")
    Proc.returncode = 1
    assert process.quiet_output(["glab", "auth", "status"], with_stderr=True) is None


def test_building_an_adapter_spawns_no_cli(monkeypatch):
    """URLs, refs, and session ids are computed from adapters built per PR; none of that may ask glab or az."""
    calls: list[list[str]] = []
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.setattr(
        "readthrough.process.subprocess.run",
        lambda cmd, **kw: calls.append(cmd) or (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr("readthrough.forges._cli_tokens", {})
    gitlab = GitLab("g/p", tuning=TUNING)
    ado = AzureDevOps("o/p/r", tuning=TUNING)
    assert gitlab.pr_url(1) and gitlab.pr_ref(1) and ado.pr_url(2) and ado.pr_ref(2)
    assert calls == []
    assert gitlab.describe_auth() == "anonymous" and ado.describe_auth() == "anonymous"
    assert [c[0].rsplit("-", 1)[1] for c in calls] == ["glab", "az"], (
        "asked once each, when the credential was first needed"
    )
    GitLab("g/p", tuning=TUNING).describe_auth()
    assert len(calls) == 2, "and never again in this process"
