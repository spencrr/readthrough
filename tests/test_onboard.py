import json
from collections import Counter
from pathlib import Path

import pytest

from readthrough import membership, onboard
from readthrough.faults import NetworkError
from readthrough.profile import ProfileError, parse_profile
from readthrough.tuning import default_tuning

TREE = [
    "src/app/api/routes.py",
    "src/app/api/auth.py",
    "src/app/core/engine.py",
    "src/app/core/tests/test_engine.py",
    "src/app/db/models.py",
    "src/app/__init__.py",
    "tests/test_api.py",
    "docs/index.md",
    ".github/workflows/ci.yml",
    "pyproject.toml",
    "README.md",
]
CHURN = Counter(
    {
        "src/app/api/routes.py": 300,
        "src/app/core/engine.py": 200,
        "src/app/core/tests/test_engine.py": 50,
        "src/app/db/models.py": 80,
        "tests/test_api.py": 60,
        "docs/index.md": 10,
        "pyproject.toml": 5,
    }
)
AUTHORS = [
    {"login": "alice", "bot": False, "association": "MEMBER"},
    {"login": "bob", "bot": False, "association": "CONTRIBUTOR"},
    {"login": "newbie", "bot": False, "association": "NONE"},
    {"login": "dependabot", "bot": True, "association": "NONE"},
]


@pytest.fixture
def facts() -> onboard.RepoFacts:
    return onboard.RepoFacts("o/app", "main", TREE, CHURN, AUTHORS, codeowners=[("src/app/db/**", ["@o/db"])])


def test_draft_is_a_valid_profile_that_covers_recent_prs(facts):
    profile, cov = onboard.check_profile_text(onboard.draft_profile(facts), facts)
    assert profile.repo == "o/app"
    assert cov.share >= cov.need == default_tuning().onboarding.min_coverage
    assert not cov.dead_patterns and not cov.shadowed
    assert profile.component_of("src/app/core/tests/test_engine.py") == "tests"
    assert profile.component_of("src/app/api/routes.py") != profile.component_of("src/app/core/engine.py")
    assert profile.component_of("src/app/db/models.py") == "db"
    assert cov.problems() == []


def test_coverage_flags_gaps_dead_patterns_shadowing_and_missing_authors(facts):
    raw = {
        "schema": 1,
        "repo": "o/app",
        "title": "App",
        "layers": [{"id": "src", "label": "Source"}],
        "components": [
            {"id": "core", "label": "Core", "layer": "src", "paths": ["src/app/core/**"]},
            {"id": "engine", "label": "Engine", "layer": "src", "paths": ["src/app/core/engine.py"]},
            {"id": "ghost", "label": "Ghost", "layer": "src", "paths": ["lib/**"]},
        ],
    }
    cov = onboard.measure(parse_profile(raw), facts, raw=raw)
    problems = "\n".join(cov.problems())
    assert cov.share < cov.need
    assert "'ghost'" in problems and "'lib/**'" in problems
    assert "'engine' never gets a file" in problems
    assert "[authors]" in problems
    assert ("tests/test_api.py", 60) in cov.unmapped_top


@pytest.mark.parametrize(
    ("pattern", "glob"),
    [
        ("/src/app/db/", "src/app/db/**"),
        ("docs/", "**/docs/**"),
        ("*.md", "**/*.md"),
        ("/README.md", "README.md"),
        ("src/app/api", "src/app/api/**"),
        ("/build", "build/**"),
    ],
)
def test_codeowners_glob(pattern, glob):
    assert onboard.codeowners_glob(pattern) == glob


def test_parse_codeowners_skips_comments():
    rules = onboard.parse_codeowners("# owners\n/src/ @team # core\n\n*.md @docs\n")
    assert rules == [("src/**", ["@team"]), ("**/*.md", ["@docs"])]


def test_source_roots_expand_src_layouts():
    tree = ["lib/x/a.py", "src/pkg/a.py", "src/pkg/b.py", "src/other/c.py", "tests/t.py"]
    assert onboard._source_roots(tree) == ["src/other", "src/pkg", "lib"]


def test_source_roots_rank_recent_churn_above_file_count():
    """A small new directory that recent PRs pour into must outrank a big quiet one (seen on microsoft/vscode)."""
    tree = [f"big/{i}.ts" for i in range(50)] + ["shim/lib.rs", "shim/main.rs", "tests/t.py"]
    assert onboard._source_roots(tree) == ["big", "shim"]
    churn = Counter({"shim/lib.rs": 9000, "shim/main.rs": 400, "big/1.ts": 30})
    assert onboard._source_roots(tree, churn) == ["shim", "big"]


def test_drift_only_when_open_prs_leave_the_map():
    fits = [{"by_comp": {"a": 90, "other": 10}, "files": [{"path": "z/q.py", "comp": "other", "add": 10, "del": 0}]}]
    assert onboard.drift(fits) is None
    drifting = [
        {
            "by_comp": {"a": 10, "other": 90},
            "files": [{"path": "new/thing/x.py", "comp": "other", "add": 80, "del": 10}],
        }
    ]
    assert onboard.drift(drifting) == {"coverage": 0.1, "unmapped": [("new/thing", 90)]}


def test_author_table_groups_kinds(facts):
    profile, _ = onboard.check_profile_text(onboard.draft_profile(facts), facts)
    kinds = {login: kind for login, _, kind, _ in onboard.author_table(profile, facts)}
    assert kinds["alice"] == "internal"
    assert kinds["bob"] == "external"
    assert kinds["newbie"] == "first-time"
    assert kinds["dependabot"] == "bot"
    members = {login: kind for login, _, kind, _ in onboard.author_table(profile, facts, members={"bob"})}
    assert members["bob"] == "internal"


class FakeGh:
    """The membership half of a forge: one lookup per (group, login), some of which fail."""

    def __init__(self, members: set[str], down: frozenset[str] = frozenset()):
        self.members, self.down = members, down
        self.calls: list[str] = []
        self.tuning = default_tuning().forge

    def is_member(self, group: str, login: str) -> bool:
        self.calls.append(f"{group}/{login}")
        if login.lower() in self.down:
            raise NetworkError("connection reset", stderr="wsarecv: connection reset")
        return login.lower() in self.members  # logins are case-insensitive


def test_membership_is_checked_per_author_and_cached(tmp_path: Path):
    gh = FakeGh({"alice", "carol"})
    cache = tmp_path / "members.json"
    found = membership.internal_members(
        gh, teams=["o/core"], orgs=["o"], logins={"alice", "bob", "Carol"}, cache_path=cache
    )
    assert found == ({"alice", "carol"}, [])
    assert len(gh.calls) == 6
    again = membership.internal_members(
        gh, teams=["o/core"], orgs=["o"], logins={"alice", "bob", "Carol"}, cache_path=cache
    )
    assert again == found and len(gh.calls) == 6
    assert membership.internal_members(gh, teams=[], orgs=[], logins={"x"}, cache_path=cache) == (set(), [])


def test_membership_failures_are_reported_not_cached(tmp_path: Path):
    cache = tmp_path / "members.json"
    flaky = FakeGh({"alice", "dave"}, down=frozenset({"dave"}))
    found, failed = membership.internal_members(flaky, teams=[], orgs=["o"], logins={"alice", "dave"}, cache_path=cache)
    assert found == {"alice"} and len(failed) == 1 and "dave" in failed[0]
    assert "org:o:dave" not in json.loads(cache.read_text())
    healthy = FakeGh({"alice", "dave"})
    found, failed = membership.internal_members(
        healthy, teams=[], orgs=["o"], logins={"alice", "dave"}, cache_path=cache
    )
    assert found == {"alice", "dave"} and failed == [] and healthy.calls == ["o/dave"]


def test_model_drafter_keeps_the_best_proposal(facts, tmp_path: Path):
    from readthrough.config import Config
    from readthrough.onboard_model import _Drafter

    said: list[str] = []
    drafter = _Drafter(Config(home=tmp_path, repo="o/app"), forge=None, facts=facts, on_progress=said.append)
    assert drafter._propose("schema = 1\nrepo = ").startswith("Invalid profile")
    good = onboard.draft_profile(facts)
    assert "No problems" in drafter._propose(good)
    weak = good.replace("[authors]", "[not_authors]")
    assert drafter._propose(weak).startswith("Invalid profile")  # unknown top-level key
    no_authors = "\n".join(line for line in good.splitlines() if not line.startswith("[authors]"))
    drafter._propose(no_authors)
    assert drafter.best is not None and drafter.best[2] == good
    assert drafter.proposals == 4 and len(said) == 4


def test_model_drafter_reads_only_repo_files(facts, tmp_path: Path):
    from readthrough.config import Config
    from readthrough.onboard_model import _Drafter

    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "src/app/api").mkdir(parents=True)
    (clone / "src/app/api/routes.py").write_text("a\nb\nc\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    drafter = _Drafter(
        Config(home=tmp_path, repo="o/app", clone=clone), forge=None, facts=facts, on_progress=lambda _: None
    )
    assert "    2  b" in drafter._read("src/app/api/routes.py", 1, 10)
    assert "isn't a file in the repo" in drafter._read("../secret.txt", 1, 10)
    assert "src/  files=6  recent_churn=630" in drafter._dirs("", 1)
    assert "src/app/core/  files=2" in drafter._dirs("src/app", 1)


def test_a_repo_without_merged_prs_is_measured_by_its_files():
    tree = [
        "packages/a/src/a/x.py",
        "packages/a/src/a/y.py",
        "packages/b/src/b/z.py",
        "packages/a/tests/test_x.py",
        "README.md",
    ]
    facts = onboard.RepoFacts("o/mono", "main", tree, Counter(), [])
    profile, cov = onboard.check_profile_text(onboard.draft_profile(facts), facts)
    assert cov.total == len(tree) and "files in the repo" in cov.basis
    assert cov.problems() == []
    assert profile.component_of("packages/a/src/a/x.py") != profile.component_of("packages/b/src/b/z.py")
    assert profile.component_of("packages/a/tests/test_x.py") == "tests"


def test_draft_drops_components_that_never_get_a_file():
    comps = [
        {"id": "all", "label": "All", "layer": "l", "paths": ["src/**"]},
        {"id": "shadowed", "label": "S", "layer": "l", "paths": ["src/app/**"]},
        {"id": "empty", "label": "E", "layer": "l", "paths": ["nowhere/**"]},
    ]
    assert [c["id"] for c in onboard._drop_unclaimed(comps, ["src/app/x.py"])] == ["all"]


def test_drafts_record_who_drafted_them(facts):
    from readthrough.onboard_model import mark_drafter

    profile, _ = onboard.check_profile_text(onboard.draft_profile(facts), facts)
    assert profile.drafted_by == "readthrough"
    marked = mark_drafter(onboard.draft_profile(facts), "model")
    assert marked.count("drafted_by") == 1
    assert onboard.check_profile_text(marked, facts)[0].drafted_by == "model"
    # Profiles drafted before runtimes existed say "copilot": a model on the only runtime there was.
    legacy = marked.replace('"model"', '"copilot"')
    assert onboard.check_profile_text(legacy, facts)[0].drafted_by == "model"
    with pytest.raises(ProfileError, match="drafted_by"):
        onboard.check_profile_text(marked.replace('"model"', '"someone"'), facts)


def test_deleted_files_dont_count_toward_coverage():
    churn = Counter({"src/app/api/routes.py": 100, "requirements/dev.txt": 900})
    facts = onboard.RepoFacts("o/app", "main", TREE, churn, AUTHORS)
    _, cov = onboard.check_profile_text(onboard.draft_profile(facts), facts)
    assert cov.total == 100 and cov.share == 1.0
    assert not any(p.startswith("requirements") for p, _ in cov.unmapped_top)
    assert cov.basis == "changed lines in recent PRs"


@pytest.mark.parametrize(
    ("repo", "title"),
    [
        ("o/app", "app"),
        ("powershell/PowerShell/PSGallery-Cache-Repo", "PSGallery-Cache-Repo"),  # Azure DevOps: org/project/repo
        ("gitlab-org/frontend/playground", "playground"),  # a GitLab project in a subgroup
    ],
)
def test_drafts_are_valid_for_repos_whose_path_has_more_than_two_segments(repo, title):
    """A repo's path is whatever its forge says it is; the drafter and the parser must agree on it."""
    facts = onboard.RepoFacts(repo, "main", TREE, CHURN, AUTHORS)
    profile, cov = onboard.check_profile_text(onboard.draft_profile(facts), facts)
    assert profile.repo == repo and profile.title == title, "the title is the repo's own name, not the project's"
    assert cov.problems() == []
    with pytest.raises(ProfileError, match="repo must be"):
        parse_profile({"schema": 1, "repo": "no-slash", "layers": [], "components": []})
    with pytest.raises(ProfileError, match="repo must be"):
        parse_profile({"schema": 1, "repo": "o//app", "layers": [], "components": []})
