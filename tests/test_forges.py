"""The REST adapters (GitLab, Forgejo, Azure DevOps) against responses recorded from the real public forges.

``scripts/probe_forge.py --record`` wrote the fixtures under ``tests/fixtures/forges/<forge>/``: one JSON file
per request, keyed by a hash of ``METHOD url``, plus an index. ``replay`` serves them in place of ``Rest.call``,
so these tests exercise the adapters' URL building and normalization offline. A request the recording
never saw fails loudly rather than returning something made up.
"""

from __future__ import annotations

import base64
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
                raise cls(entry["error"]["message"], what=path, stderr=entry["error"]["stderr"],
                          headers=entry["error"].get("headers") or {})  # fmt: skip
            if entry["file"] is None:  # a 404 the adapter asked to have as None
                return None
            data = json.loads((directory / entry["file"]).read_text("utf-8"))
            assert hashlib.sha1(f"{method} {url}".encode()).hexdigest()[:12] == entry["file"][:-5]  # noqa: S324 - a key, not security
            body = base64.b64decode(data["body_b64"]) if data.get("body_b64") else b""
            return Response(data["status"], data["headers"], data["text"], body)

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
    tenants_asked: list[str | None] = []
    monkeypatch.setattr("readthrough.forges.azure_devops._az_token", lambda tenant: tenants_asked.append(tenant))
    monkeypatch.setattr("readthrough.forges.azure_devops._tenants", {})
    asked = replay("ado")
    forge = AzureDevOps("powershell/PowerShell/PSGallery-Cache-Repo", tuning=TUNING)
    assert forge.describe_auth() == "anonymous"
    # Even an organization with public projects refuses an anonymous listing of them, and that refusal names
    # its tenant, which is what az is asked for a token in.
    assert tenants_asked == ["72f988bf-86f1-41af-91ab-2d7cd011db47"] and forge.tenant() == tenants_asked[0]
    assert forge.sign_in_hint().startswith(
        "az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47 --allow-no-subscriptions"
    )
    assert "diffs" in forge.capabilities, "assembled from blobs"
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
    assert pr.association.known and pr.association.value == "FIRST_TIME_CONTRIBUTOR", "no completed PR by this author"
    assert pr.threads.known and pr.checks.known and pr.last_commit
    assert pr.updated >= pr.last_commit

    branch, merged = forge.merged_prs(2)
    assert branch == "main" and merged == [], "this repo has never completed a PR"
    tree, complete = forge.tree(branch)
    assert complete and tree == ["es-metadata.yml"]
    assert forge.read_file("definitely/not/here.md") is None
    context = forge.ci_context({"main"})
    # Branch policies need sign-in on Azure DevOps: unavailable, with the reason, never an empty set.
    assert not context.required["main"].known and "not authorized" in context.required["main"].reason
    assert context.main_failing.known
    # The diff is assembled from the blobs: this PR adds one file, so one blob fetch and a new-file header.
    diff = forge.fetch_diff(53)
    assert diff.startswith(
        "diff --git a/.pipeline/feed-sync.yml b/.pipeline/feed-sync.yml\nnew file mode 100644\n--- /dev/null\n+++ b/.pipeline/feed-sync.yml\n"
    )
    assert "\n+trigger:" in diff or "\n+" in diff
    assert sum(1 for a in asked if "/blobs/" in a) == 1


def test_azure_devops_assembles_diffs_from_both_sides_of_each_file(monkeypatch):
    """Edits, adds, deletes, renames, and binaries, through a scripted transport; and the cap on files."""
    forge = AzureDevOps("o/p/r", tuning=TUNING)
    blobs = {"old1": b"a\nb\nc\n", "new1": b"a\nB\nc\n", "new2": b"fresh\n", "old3": b"gone\n", "same": b"moved\n",
             "bin0": b"\x89PNG\0\0", "bin1": b"\x89PNG\0\1"}  # fmt: skip
    changes = [
        {"changeType": "edit", "item": {"path": "/src/a.py", "objectId": "new1", "originalObjectId": "old1", "gitObjectType": "blob"}},
        {"changeType": "add", "item": {"path": "/docs/new.md", "objectId": "new2", "gitObjectType": "blob"}},
        {"changeType": "delete", "originalPath": "/old.txt", "item": {"path": None, "originalObjectId": "old3", "gitObjectType": "blob"}},
        {"changeType": "rename", "originalPath": "/was.md", "item": {"path": "/is.md", "objectId": "same", "originalObjectId": "same", "gitObjectType": "blob"}},
        {"changeType": "edit", "item": {"path": "/img.png", "objectId": "bin1", "originalObjectId": "bin0", "gitObjectType": "blob"}},
        {"changeType": "edit", "item": {"path": "/src", "isFolder": True, "gitObjectType": "tree"}},
    ]  # fmt: skip
    pr = {
        "pullRequestId": 9,
        "lastMergeTargetCommit": {"commitId": "b" * 40},
        "lastMergeSourceCommit": {"commitId": "h" * 40},
    }

    def call(self, method, path, *, params=None, headers=None, absent=()):
        if path.endswith("/pullrequests/9"):
            return Response(200, {}, json.dumps(pr))
        if path.endswith("/diffs/commits"):
            return Response(200, {}, json.dumps({"changes": changes, "allChangesIncluded": True}))
        if "/blobs/" in path:
            return Response(200, {}, "", blobs[path.rsplit("/", 1)[1]])
        raise AssertionError(path)

    monkeypatch.setattr(Rest, "call", call)
    diff = forge.fetch_diff(9)
    assert "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n" in diff
    assert (
        "diff --git a/docs/new.md b/docs/new.md\nnew file mode 100644\n--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1 @@\n+fresh\n"
        in diff
    )
    assert (
        "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\n--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
        in diff
    )
    assert "diff --git a/was.md b/is.md\nrename from was.md\nrename to is.md\ndiff --git" in diff, (
        "a pure rename shows no hunk"
    )
    assert "Binary files a/img.png and b/img.png differ\n" in diff
    assert "a/src b/src" not in diff, "folders aren't files"
    assert diff.endswith("\n")
    # What the analysis reads out of it: added lines per file, by the +++ b/ header.
    from readthrough.analyze import parse_diff

    assert parse_diff(diff) == {"src/a.py": [(2, "B")], "docs/new.md": [(1, "fresh")]}

    small = AzureDevOps("o/p/r", tuning=load_tuning({"forge": {"max_assembled_diff_files": 3, "retries": 0}}).forge)
    with pytest.raises(faults.TooLarge, match="more than 3 files"):
        small.fetch_diff(9)


def test_azure_devops_tells_returning_authors_from_first_timers_and_checks_memberships(monkeypatch):
    """Association from the completed-PR history (one query per author, cached), and memberships from the
    organization's users and the identities API's recursive "member of" for a project team."""
    forge = AzureDevOps("contoso/Delivery Team/Courier-ops", tuning=TUNING)
    asked: list[str] = []
    users_pages = [
        (
            {"value": [{"principalName": "Ann@contoso.com", "mailAddress": "ann@contoso.com"}]},
            {"x-ms-continuationtoken": "p2"},
        ),
        ({"value": [{"principalName": "bob@contoso.com", "mailAddress": None}]}, {}),
    ]
    team_descriptor = "Microsoft.TeamFoundation.Identity;S-1-9-team"
    member_of = {
        "id-ann": [team_descriptor, "Microsoft.TeamFoundation.Identity;S-1-9-valid-users"],
        "id-bob": ["Microsoft.TeamFoundation.Identity;S-1-9-valid-users"],
    }

    def call(self, method, path, *, params=None, headers=None, absent=()):
        params = params or {}
        asked.append(
            path.split("_apis/")[-1]
            + " "
            + " ".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "api-version")
        )
        if path.endswith("/pullrequests"):
            return Response(
                200,
                {},
                json.dumps(
                    {"value": [{"pullRequestId": 1}] if params.get("searchCriteria.creatorId") == "old" else []}
                ),
            )
        if path.endswith("/graph/users"):
            page, hdrs = users_pages.pop(0)
            return Response(200, hdrs, json.dumps(page))
        if path.endswith("/teams"):
            return Response(200, {}, json.dumps({"value": [{"id": "team-guid", "name": "Delivery Team"}]}))
        if path.endswith("/identities"):
            if params.get("identityIds") == "team-guid":
                return Response(200, {}, json.dumps({"value": [{"id": "team-guid", "descriptor": team_descriptor}]}))
            if params.get("queryMembership") == "ExpandedUp":
                return Response(
                    200,
                    {},
                    json.dumps(
                        {"value": [{"id": params["identityIds"], "memberOf": member_of[params["identityIds"]]}]}
                    ),
                )
            if params.get("searchFilter") == "General":  # a login not seen on any PR this build
                found = (
                    [{"id": "id-bob", "properties": {"Account": {"$value": "Bob@contoso.com"}}}]
                    if params["filterValue"] == "bob@contoso.com"
                    else []
                )
                return Response(200, {}, json.dumps({"value": found}))
        raise AssertionError(path)

    monkeypatch.setattr(Rest, "call", call)
    forge._look_up_history({"old", "new"})
    assert forge._association({"createdBy": {"id": "old"}}).value == "COLLABORATOR"
    assert forge._association({"createdBy": {"id": "new"}}).value == "FIRST_TIME_CONTRIBUTOR"
    assert not forge._association({"createdBy": {}}).known
    assert sum("creatorId" in a for a in asked) == 2, "one history query per author"
    forge._association({"createdBy": {"id": "old"}})
    assert sum("creatorId" in a for a in asked) == 2, "cached"

    assert forge.is_member("contoso", "ANN@contoso.com") and forge.is_member("CONTOSO", "bob@contoso.com")
    assert not forge.is_member("contoso", "zed@contoso.com")
    assert sum(a.startswith("graph/users") for a in asked) == 2, "both pages, once"

    forge._login({"createdBy": {"id": "id-ann", "uniqueName": "Ann@contoso.com"}})  # as fetch_prs would
    assert forge.is_member("contoso/Delivery Team", "ann@contoso.com") and forge.is_member(
        "Delivery Team/delivery team", "ann@contoso.com"
    )
    assert not forge.is_member("contoso/Delivery Team", "bob@contoso.com"), "resolved by search, not in the team"
    assert (
        sum("identityIds=team-guid" in a for a in asked) == 1
        and sum(a.endswith("/teams ") or "teams " in a for a in asked) == 1
    ), "the team is resolved once"
    assert sum("searchFilter=General" in a for a in asked) == 1
    with pytest.raises(ForgeError, match="no identity found"):
        forge.is_member("contoso/Delivery Team", "nobody@contoso.com")
    with pytest.raises(ForgeError, match="no team named"):
        forge.is_member("contoso/Nobody", "ann@contoso.com")
    with pytest.raises(ForgeError, match="isn't this organization"):
        forge.is_member("other-org", "ann@contoso.com")


def test_assemble_diff_keeps_line_endings_honest():
    """CRLF files and a missing final newline come out the way git prints them, and content starting with --- isn't a header."""
    pairs = [
        forges.FilePair("a.sql", "a.sql", b"-- one\r\n-- two\r\n", b"-- one\r\n-- three\r\n"),
        forges.FilePair("b.txt", "b.txt", b"x\ny", b"x\nz"),
    ]
    out = forges.assemble_diff(pairs)
    assert "@@ -1,2 +1,2 @@\n -- one\n--- two\n+-- three\n" in out
    assert "@@ -1,2 +1,2 @@\n x\n-y\n\\ No newline at end of file\n+z\n\\ No newline at end of file\n" in out
    assert forges.assemble_diff([]) == ""


def test_azure_devops_repo_can_be_onboarded(replay, monkeypatch):
    """The whole onboarding path on a repo whose locator has three segments: gather from the recorded forge, draft,
    then parse and measure the draft the way ``onboard --accept`` does. The profile's ``repo`` is the forge's
    path, not an ``owner/name`` assumption, and its title is the repository's own name."""
    from readthrough import onboard

    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.setattr("readthrough.forges.azure_devops._az_token", lambda: None)
    replay("ado")
    forge = AzureDevOps("powershell/PowerShell/PSGallery-Cache-Repo", tuning=TUNING)

    facts = onboard.gather(forge, sample=2)
    assert facts.repo == "powershell/PowerShell/PSGallery-Cache-Repo" and facts.tree_complete
    text = onboard.draft_profile(facts)
    profile, cov = onboard.check_profile_text(text, facts)
    assert profile.repo == facts.repo and profile.title == "PSGallery-Cache-Repo"
    assert cov.problems() == [] and cov.basis.startswith("files in the repo"), (
        "no merged PRs, so the tree is the yardstick"
    )
    assert profile.component_of("es-metadata.yml") is not None, "the one file the repo has is on the map"


def test_azure_devops_steps_aside_when_the_org_refuses_the_az_identity(monkeypatch):
    """A public project answers anonymously what it refuses to an outsider's az token: the adapter steps aside.
    A private one refuses both, and then the error says how to sign in to this organization's tenant."""
    from readthrough.faults import AuthError

    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.setattr("readthrough.forges.azure_devops._az_token", lambda tenant: "eyJ-not-a-real-token")
    monkeypatch.setattr("readthrough.forges.azure_devops._tenants", {"o": "t-1"})
    forge = AzureDevOps("o/p/r", tuning=TUNING)
    assert forge.describe_auth() == "az (tenant t-1)"
    attempts: list[str | None] = []
    public = True

    def call(self: Rest, method: str, path: str, **kw: Any) -> Response:
        attempts.append(self.cred.source)
        if self.cred.token or not public:
            raise AuthError("GET x failed: HTTP 401", what=path, stderr="HTTP 401 TF400813: not authorized")
        return Response(200, {}, json.dumps({"id": "1", "name": "r", "defaultBranch": "refs/heads/main"}))

    monkeypatch.setattr(Rest, "call", call)
    assert forge.repo_info()["default_branch"] == "main"
    assert attempts == ["az", "anonymous"] and forge.describe_auth() == "anonymous"
    assert forge.vssps.cred.source == "anonymous", "the identities host follows"

    public = False
    private = AzureDevOps("o/p/r", tuning=TUNING)
    with pytest.raises(AuthError, match="refuses both the az identity and anonymous access") as refused:
        private.repo_info()
    assert "az login --tenant t-1 --allow-no-subscriptions" in str(refused.value)
    assert refused.value.__cause__ is not None, "the first refusal is kept as the cause"
    assert private.describe_auth() == "az (tenant t-1)", "the identity is kept; stepping aside didn't help"


def test_azure_devops_remembers_why_az_had_no_token(monkeypatch):
    """When az has nothing for the tenant, doctor repeats the line of az's complaint that explains it."""
    from readthrough.forges import azure_devops as ado

    monkeypatch.setattr("readthrough.forges._cli_tokens", {})
    monkeypatch.setattr(ado, "_az_notes", {})
    monkeypatch.setattr(ado, "_tenants", {"o": "t-1"})
    said = {
        "out": None,
        "err": "ERROR: AADSTS50076: Due to a configuration change made by your administrator, you must use multi-factor authentication.\nInteractive authentication is needed. Please run:\naz login --scope x",
    }
    monkeypatch.setattr(ado, "quiet_output", lambda cmd, with_failure=False: (said["out"], said["err"]))
    forge = AzureDevOps("o/p/r", tuning=TUNING)
    assert forge.describe_auth().startswith("anonymous; az has no token for o: AADSTS50076")
    assert (
        ado._az_complaint(
            "ERROR: The command failed with an unexpected error. Here is the traceback:\n  File x\nValueError: Unable to get authority configuration for https://login.microsoftonline.com/t-1\nTo check existing issues, visit"
        )
        == "ValueError: Unable to get authority configuration for https://login.microsoftonline.com/t-1"
    )
    assert (
        ado._az_complaint("ERROR: Please run 'az login' to setup account.") == "Please run 'az login' to setup account."
    )
    assert ado._az_complaint("") == "az isn't installed or isn't signed in"


def test_azure_devops_urls():
    forge = AzureDevOps("org/proj/repo", tuning=TUNING)
    assert forge.pr_ref(5) == "pull/5/merge"
    assert (
        forge.file_url("docs/a.md", "main") == "https://dev.azure.com/org/proj/_git/repo?path=/docs/a.md&version=GBmain"
    )
    assert AzureDevOps.validate_repo("o/r") and AzureDevOps.validate_repo("o/p/r") is None


def test_azure_devops_project_names_may_have_spaces():
    """Projects are often called things like "Delivery Team"; the locator keeps the literal name and URLs encode it."""
    assert AzureDevOps.validate_repo("contoso/Delivery Team/Courier-Ops") is None
    assert AzureDevOps.validate_repo("my org/p/r"), "organizations can't"
    assert AzureDevOps.validate_repo("o/p /r") and AzureDevOps.validate_repo("o/a  b/r"), (
        "only single spaces between words"
    )
    forge = AzureDevOps("contoso/Delivery Team/Courier-Ops", tuning=TUNING)
    assert forge.pr_url(7) == "https://dev.azure.com/contoso/Delivery%20Team/_git/Courier-Ops/pullrequest/7"
    assert forge.slug("contoso/Delivery Team/Courier-Ops") == "ado/contoso/Delivery Team/Courier-Ops"


def test_remote_urls_are_recognised_per_forge():
    repo = "contoso/Delivery Team/Courier-Ops"
    assert AzureDevOps.remote_matches("https://contoso@dev.azure.com/contoso/Delivery%20Team/_git/Courier-ops", repo)
    assert AzureDevOps.remote_matches("git@ssh.dev.azure.com:v3/contoso/Delivery%20Team/Courier-Ops", repo)
    assert not AzureDevOps.remote_matches("https://dev.azure.com/contoso/Other/_git/Courier-Ops", repo)
    assert not AzureDevOps.remote_matches("https://github.com/contoso/Courier-Ops.git", repo)
    # The default rule: the repo's path ends the URL, with or without .git, over https or ssh.
    assert Forgejo.remote_matches("git@codeberg.org:forgejo/forgejo.git", "forgejo/forgejo")
    assert GitLab.remote_matches("https://gitlab.com/gitlab-org/frontend/playground", "gitlab-org/frontend/playground")
    assert not GitLab.remote_matches("https://gitlab.com/other/playground", "gitlab-org/frontend/playground")


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


def test_error_summary_prefers_the_apis_message():
    body = '{"$id":"1","innerException":null,"message":"TF400813: The user is not authorized to access this resource.","typeName":"X"}'
    assert forges.error_summary(body) == "TF400813: The user is not authorized to access this resource."
    assert forges.error_summary('{"error": "invalid_token", "error_description": "expired"}') == "expired"
    assert forges.error_summary('{"error": "invalid_token"}') == "invalid_token"
    assert forges.error_summary("\n<html>sign in</html>\n") == "<html>sign in</html>"
    assert forges.error_summary('{"message": ""}') == '{"message": ""}' and forges.error_summary("") == ""


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


def test_only_things_that_look_like_tokens_become_credentials(monkeypatch, caplog):
    """A CLI stand-in that prints a banner, or a variable set to prose, must not end up in an Authorization header."""
    monkeypatch.setenv("T", "glpat-abcdefghijklmnop")
    assert forges.credential(env="T", cli=None, cli_name="x").source == "env T"
    monkeypatch.setenv("T", "Microsoft Windows [Version 10]\n(c) Microsoft Corporation.")
    with caplog.at_level("WARNING"):
        assert (
            forges.credential(env="T", cli=lambda: "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.token", cli_name="az").source
            == "az"
        )
    assert "doesn't look like a token" in caplog.text
    assert forges.credential(env="", cli=lambda: "please run az login", cli_name="az").source == "anonymous"
    assert forges.credential(env="", cli=lambda: "short", cli_name="az").source == "anonymous"
    assert forges.credential(env="", cli=lambda: None, cli_name="az").source == "anonymous"


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
    monkeypatch.setattr("readthrough.forges.azure_devops._tenants", {"o": None})  # the tenant probe is a network call
    monkeypatch.setattr("readthrough.forges.azure_devops._az_notes", {})
    gitlab = GitLab("g/p", tuning=TUNING)
    ado = AzureDevOps("o/p/r", tuning=TUNING)
    assert gitlab.pr_url(1) and gitlab.pr_ref(1) and ado.pr_url(2) and ado.pr_ref(2)
    assert calls == []
    assert gitlab.describe_auth() == "anonymous" and ado.describe_auth().startswith("anonymous; az has no token")
    assert [c[0].rsplit("-", 1)[1] for c in calls] == ["glab", "az"], (
        "asked once each, when the credential was first needed"
    )
    GitLab("g/p", tuning=TUNING).describe_auth()
    assert len(calls) == 2, "and never again in this process"
