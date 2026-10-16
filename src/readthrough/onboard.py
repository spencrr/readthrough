"""Onboarding: draft an architecture profile for a repo, measure how well it covers real PRs, and gate on it.

A repo can't be built until it's onboarded. Onboarding means having a profile (from a shared directory, the
repo itself, or a draft written here and edited by you) that:

* maps at least ``[tuning.onboarding] min_coverage`` of the lines changed by recent PRs to real components;
* has no path patterns that match nothing in the repo, and no component that earlier ones shadow;
* has an ``[authors]`` section, so you've decided who counts as internal.

The draft is deliberately plain: it reads CODEOWNERS, the directory tree, where recent PRs actually
changed code, and any architecture docs or instruction files. It gets you a working map in a minute;
the Copilot-assisted draft (``--copilot``) or your own edits make it good.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from readthrough.forges import Forge
from readthrough.model import MergedPullRequest
from readthrough.process import run
from readthrough.profile import FALLBACK, Profile, parse_profile
from readthrough.rules import glob_to_regex
from readthrough.tuning import default_tuning

log = logging.getLogger(__name__)

TEST_DIRS = {"tests", "test", "spec", "specs", "__tests__", "testing", "e2e", "integration_tests", "unit_tests"}
GENERIC_DIRS = {"src", "lib", "app", "core", "pkg", "internal", "common", "main"}  # mean nothing without their parent
DOC_DIRS = {"doc", "docs", "documentation", "website", "site", "examples", "example", "samples"}
INFRA_DIRS = {
    ".github",
    ".azuredevops",
    ".devcontainer",
    ".circleci",
    "build",
    "build_scripts",
    "ci",
    "scripts",
    "docker",
    "infra",
    "deploy",
    "tools",
    ".vscode",
}
INFRA_FILES = [
    "Dockerfile",
    "Makefile",
    "*.yml",
    "*.yaml",
    "*.toml",
    "*.cfg",
    "*.ini",
    "*.json",
    "*.lock",
    "LICENSE*",
    ".*",
]
MANIFESTS = ["pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt", "uv.lock", "poetry.lock", "**/package.json", "package.json",
             "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "**/*.csproj", "Gemfile*"]  # fmt: skip
ARCH_DOCS = ["ARCHITECTURE.md", "docs/ARCHITECTURE.md", "docs/architecture.md", "doc/architecture.md", "AGENTS.md"]
GUIDANCE = [".github/copilot-instructions.md", "AGENTS.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md"]
CODEOWNERS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]


@dataclass
class RepoFacts:
    """What onboarding knows about a repo, gathered once."""

    repo: str
    default_branch: str
    tree: list[str]
    churn: Counter[str]
    authors: list[dict[str, Any]]  # login, bot, association per recent PR
    codeowners: list[tuple[str, list[str]]] = field(default_factory=list)
    docs: dict[str, str] = field(default_factory=dict)
    has_instructions: bool = False
    tree_complete: bool = True  # False when the forge listed only part of a huge tree


# ---------------------------------------------------------------- gathering


def gather(
    forge: Forge,
    *,
    clone: Path | None = None,
    open_prs: list[MergedPullRequest] | None = None,
    sample: int | None = None,
) -> RepoFacts:
    branch, merged = forge.merged_prs(sample or default_tuning().onboarding.merged_sample)
    churn: Counter[str] = Counter()
    authors: list[dict[str, Any]] = []
    for pr in merged + (open_prs or []):
        for f in pr.files:
            churn[f.path] += f.additions + f.deletions
        authors.append({"login": pr.author.login, "bot": pr.author.bot, "association": pr.association.get("NONE")})
    tree, complete = _tree(forge, branch, clone)
    if not complete:
        log.warning("Listed only %d files of a bigger tree; onboarding with a local clone sees them all", len(tree))
    facts = RepoFacts(forge.repo, branch, tree, churn, authors, tree_complete=complete)
    wanted = [p for p in [*CODEOWNERS, *ARCH_DOCS, *GUIDANCE] if p in tree]
    texts = forge.read_files(wanted)
    for path in CODEOWNERS:
        if path in tree:
            facts.codeowners = parse_codeowners(texts.get(path) or "")
            break
    for path in [*ARCH_DOCS, *GUIDANCE]:
        if path in tree and path not in facts.docs:
            facts.docs[path] = texts.get(path) or ""
    facts.has_instructions = any(p.startswith(".github/instructions/") for p in tree)
    return facts


def _tree(forge: Forge, branch: str, clone: Path | None) -> tuple[list[str], bool]:
    if clone and (clone / ".git").exists():
        out = run(["git", "-C", str(clone), "ls-files"], check=False)
        if out.strip():
            return out.splitlines(), True
    return forge.tree(branch)


def parse_codeowners(text: str) -> list[tuple[str, list[str]]]:
    rules = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        rules.append((codeowners_glob(pattern), owners))
    return rules


def codeowners_glob(pattern: str) -> str:
    """Translate a CODEOWNERS (gitignore-style) pattern into the profile's glob syntax."""
    anchored = pattern.startswith("/")
    p = pattern.strip("/")
    if pattern.endswith("/") or ("." not in PurePosixPath(p).name and "*" not in p):
        p = f"{p}/**"
    return p if anchored or "/" in p.rstrip("/*") else f"**/{p}"


# ---------------------------------------------------------------- drafting


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "root"


def _label(path: str) -> str:
    name = PurePosixPath(path).name.replace("_", " ").replace("-", " ")
    return name[:1].upper() + name[1:]


def _source_roots(tree: list[str], churn: Counter[str] | None = None) -> list[str]:
    """Top-level directories holding the code, excluding tests, docs, and infra.

    Ordered by where recent PRs changed lines, then by file count. Churn first, because a new directory with
    few files and a lot of change (a port in progress, say) matters more to a reviewer than an old one with
    many files and none; ranking by size alone dropped exactly such a directory on a large repo.
    """
    counts: Counter[str] = Counter(p.split("/", 1)[0] for p in tree if "/" in p)
    churn_by_root: Counter[str] = Counter()
    for path, lines in (churn or {}).items():
        if "/" in path:
            churn_by_root[path.split("/", 1)[0]] += lines
    roots = sorted(
        (d for d in counts if d.lower() not in TEST_DIRS | DOC_DIRS | INFRA_DIRS and not d.startswith(".")),
        key=lambda d: (-churn_by_root[d], -counts[d], d),
    )
    # "src/<pkg>" layouts: look one level deeper.
    expanded = []
    for root in roots:
        if root == "src":
            expanded += [
                f"src/{d}"
                for d in sorted({p.split("/")[1] for p in tree if p.startswith("src/") and p.count("/") >= 2})
            ]
        else:
            expanded.append(root)
    return expanded


def draft_components(facts: RepoFacts, *, max_components: int = 16) -> list[dict[str, Any]]:
    """Choose components from where code lives and where PRs change it."""
    churn_by_dir: Counter[str] = Counter()
    for path, lines in live_churn(facts).items():
        parts = path.split("/")
        for depth in (1, 2, 3):
            if len(parts) > depth:
                churn_by_dir["/".join(parts[:depth])] += lines
    components: list[dict[str, Any]] = []
    roots = _source_roots(facts.tree, live_churn(facts))
    for root in roots[:6]:
        subdirs = sorted(
            {
                p[len(root) + 1 :].split("/", 1)[0]
                for p in facts.tree
                if p.startswith(root + "/") and p[len(root) + 1 :].count("/") >= 1
            }
        )
        busy = [d for d in subdirs if churn_by_dir[f"{root}/{d}"] > 0 and d.lower() not in TEST_DIRS]
        busy.sort(key=lambda d: -churn_by_dir[f"{root}/{d}"])
        layer = _slug(root.split("/")[-1])
        root_name = root.split("/")[-1]
        for d in busy[: max(2, max_components // max(1, len(roots)))]:
            # "src", "lib", or "core" says nothing on its own; name it after the directory it's in.
            generic = d.lower() in GENERIC_DIRS
            components.append(
                {
                    "id": _slug(f"{root_name}-{d}") if generic else _slug(d),
                    "label": f"{_label(root_name)} {d}" if generic else _label(d),
                    "layer": layer,
                    "paths": [f"{root}/{d}/**"],
                }
            )
        components.append(
            {
                "id": _slug(root.split("/")[-1]) if busy else _slug(root),
                "label": _label(root) + (" (rest)" if busy else ""),
                "layer": layer,
                "paths": [f"{root}/**"],
            }
        )
    # CODEOWNERS entries for directories the tree heuristic didn't name.
    for glob, _owners in facts.codeowners:
        if glob.endswith("/**") and not any(glob in c["paths"] for c in components):
            base = glob[:-3].lstrip("*/")
            if base and any(p.startswith(base + "/") for p in facts.tree) and base.split("/")[0] in roots:
                components.insert(
                    0,
                    {
                        "id": _slug(base.split("/")[-1]),
                        "label": _label(base),
                        "layer": _slug(base.split("/")[0]),
                        "paths": [glob],
                    },
                )
    test_globs = sorted({f"{p.split('/')[0]}/**" for p in facts.tree if p.split("/")[0].lower() in TEST_DIRS})
    test_globs += [
        g
        for g in (
            "**/test_*.py",
            "**/*_test.py",
            "**/*_test.go",
            "**/*.test.ts",
            "**/*.test.tsx",
            "**/*.spec.ts",
            "**/__tests__/**",
        )
        if any(glob_to_regex(g).match(p) for p in facts.tree)
    ]
    exists = lambda g: any(glob_to_regex(g).match(p) for p in facts.tree)  # noqa: E731
    doc_globs = sorted({f"{p.split('/')[0]}/**" for p in facts.tree if p.split("/")[0].lower() in DOC_DIRS})
    doc_globs += [g for g in ["**/*.md"] if exists(g)]
    infra_globs = sorted({f"{p.split('/')[0]}/**" for p in facts.tree if p.split("/")[0] in INFRA_DIRS})
    infra_globs += [g for g in INFRA_FILES if exists(g)]
    support = [
        ("tests", "Tests", test_globs),
        ("docs", "Docs", doc_globs),
        ("infra", "Build / CI / Infra", infra_globs),
    ]
    # Tests and docs inside source roots must win over the source component, so they go first.
    head: list[dict[str, Any]] = [
        {"id": cid, "label": label, "layer": "support", "paths": globs} for cid, label, globs in support if globs
    ]
    seen: set[str] = set()
    out = []
    for comp in [*head, *components]:
        cid = str(comp["id"])
        if cid in seen:
            cid = f"{cid}-{len(seen)}"
            comp["id"] = cid
        seen.add(cid)
        out.append(comp)
    return _drop_unclaimed(out, facts.tree)


def _drop_unclaimed(components: list[dict[str, Any]], tree: list[str]) -> list[dict[str, Any]]:
    """Remove components that end up with no files once first-match-wins is applied (shadowed or empty)."""
    regexes = [[glob_to_regex(g) for g in c["paths"]] for c in components]
    claimed: set[int] = set()
    for path in tree:
        for i, rxs in enumerate(regexes):
            if any(rx.match(path) for rx in rxs):
                claimed.add(i)
                break
    return [c for i, c in enumerate(components) if i in claimed]


def draft_profile(facts: RepoFacts, *, title: str | None = None) -> str:
    """A complete, valid profile as TOML text, with comments pointing at what to edit."""
    components = draft_components(facts)
    layer_ids = list(dict.fromkeys(c["layer"] for c in components if c["layer"] != "support"))
    owner = facts.repo.split("/")[0]
    arch = next((p for p in ARCH_DOCS if p in facts.docs), None)
    guidance = next((p for p in GUIDANCE if p in facts.docs), None)
    out = [
        f"# Architecture profile for {facts.repo}, drafted by `readthrough onboard`.",
        "# Edit freely: rename and regroup components, write each one's responsibility, add signals.",
        "# `readthrough onboard " + facts.repo + "` re-measures coverage; add `--accept` to finish onboarding.",
        "schema = 1",
        f"repo = {json.dumps(facts.repo)}",
        'drafted_by = "readthrough"',
        f"title = {json.dumps(title or facts.repo.split('/')[1])}",
        "",
    ]
    for lid in layer_ids:
        out += ["[[layers]]", f"id = {json.dumps(lid)}", f"label = {json.dumps(_label(lid))}", ""]
    out += ["[[layers]]", 'id = "support"', 'label = "Tests / docs / infra"', "source = false", ""]
    out.append("# First match wins: tests and docs come first so they aren't counted as source.")
    for c in components:
        out += [
            "[[components]]",
            f"id = {json.dumps(c['id'])}",
            f"label = {json.dumps(c['label'])}",
            f"layer = {json.dumps(c['layer'])}",
            "paths = [" + ", ".join(json.dumps(p) for p in c["paths"]) + "]",
        ]
        if c["layer"] != "support":
            out.append('# responsibility = "What this component is for."')
            out.append('# does_not_own = "What belongs somewhere else, so reviewers can spot responsibility bleed."')
        out.append("")
    if arch:
        out += [
            "[contracts]",
            f"# {arch} could hold each component's contract as '## <section>' headings with",
            "# '**Responsibility**:' lines and '- **Does not own**' bullets. Then set a component's contract = '<section>'.",
            f"source = {json.dumps(arch)}",
            "",
        ]
    out += ["[rules]", f"instructions_dir = {json.dumps('.github/instructions' if facts.has_instructions else '')}"]
    if guidance:
        out.append(f"guidance_file = {json.dumps(guidance)}")
    out.append("")
    manifests = [m for m in MANIFESTS if any(glob_to_regex(m).match(p) for p in facts.tree)]
    if manifests:
        out += [
            "[[signals]]",
            'id = "deps"',
            'kind = "risk"',
            'message = "Dependency manifest changed"',
            "paths = [" + ", ".join(json.dumps(m) for m in manifests) + "]",
            "",
        ]
    migrations = sorted(
        {p.rsplit("/", 1)[0] + "/**" for p in facts.tree if re.search(r"(^|/)(migrations|alembic)/", p)}
    )[:3]
    if migrations:
        out += [
            "[[signals]]",
            'id = "migration"',
            'kind = "risk"',
            'message = "Database migration: check upgrade and downgrade"',
            "paths = [" + ", ".join(json.dumps(m) for m in migrations) + "]",
            "",
        ]
    out += [
        "[[smells]]",
        'id = "todo"',
        'kind = "info"',
        'message = "TODO/FIXME added"',
        'path = "."',
        "line = '\\b(?:TODO|FIXME|XXX|HACK)\\b'",
        "",
        "[checks]",
        "test_paths = ["
        + ", ".join(
            json.dumps(g) for g in ["tests/**", "**/tests/**", "**/test_*.py", "**/*.test.ts", "**/__tests__/**"]
        )
        + "]",
        "",
        "[weights]",
        "capped_paths = ["
        + ", ".join(json.dumps(g) for g in ["**/*.lock", "**/*-lock.json", "**/*.ipynb", "**/*.snap", "**/*.min.js"])
        + "]",
        "",
        "[overlap]",
        "ignore = "
        + json.dumps(
            [m for m in ("uv.lock", "package-lock.json", "yarn.lock", "go.sum", "Cargo.lock") if m in facts.tree]
        ),
        'non_source = ["tests/**", "**/tests/**", "docs/**", "doc/**", "**/*.lock", "**/*.md"]',
        "",
        "[authors]",
        "# GitHub's author association does most of the work: org members, owners, and collaborators are",
        "# internal; contributors are external. Fill these in where that's wrong for your team.",
        "internal = []        # logins, e.g. contractors who aren't org members",
        "external = []        # logins, e.g. people who left but kept access",
        "bots = []",
        'internal_teams = []  # e.g. ["' + owner + '/maintainers"]: members count as internal',
        'internal_orgs = []   # e.g. ["' + owner + "\"]: checks each author's org membership",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------- coverage


@dataclass
class Coverage:
    total: int
    mapped: int
    by_component: Counter[str]
    unmapped_top: list[tuple[str, int]]
    dead_patterns: list[tuple[str, str]]
    shadowed: list[str]
    has_authors: bool
    basis: str = "changed lines in recent PRs"
    need: float = field(default_factory=lambda: default_tuning().onboarding.min_coverage)

    @property
    def share(self) -> float:
        return self.mapped / self.total if self.total else 1.0

    def problems(self) -> list[str]:
        out = []
        if self.share < self.need:
            out.append(
                f"Only {self.share:.0%} of {self.basis} map to a component (need {self.need:.0%}). Add components for the unmapped paths below."
            )
        out += [f"Component {cid!r}: pattern {pat!r} matches no file in the repo" for cid, pat in self.dead_patterns]
        out += [
            f"Component {cid!r} never gets a file: earlier components claim everything it matches. Move it up."
            for cid in self.shadowed
        ]
        if not self.has_authors:
            out.append(
                "The profile has no [authors] section. Add one (even empty) once you've checked how authors classify."
            )
        return out


def live_churn(facts: RepoFacts) -> Counter[str]:
    """Lines changed by recent PRs in files that still exist.

    Files deleted since can't be mapped without a pattern that matches nothing, so they don't count.
    With no merged PRs to learn from (or none touching current files), every file counts once instead.
    """
    tree = set(facts.tree)
    live = Counter({p: n for p, n in facts.churn.items() if p in tree}) if tree else facts.churn
    return live or Counter(dict.fromkeys(facts.tree, 1))


def measure(
    profile: Profile, facts: RepoFacts, *, raw: dict[str, Any] | None = None, min_coverage: float | None = None
) -> Coverage:
    by_component: Counter[str] = Counter()
    unmapped: Counter[str] = Counter()
    total = mapped = 0
    # With no merged PRs to learn from, every file in the repo counts once instead.
    sample = live_churn(facts)
    for path, churn in sample.items():
        lines = max(churn, 1)
        comp = profile.component_of(path)
        total += lines
        by_component[comp] += lines
        if comp == FALLBACK:
            unmapped["/".join(path.split("/")[:2]) if "/" in path else path] += lines
        else:
            mapped += lines
    dead, shadowed = [], []
    claimed = {p: profile.component_of(p) for p in facts.tree}
    for comp in profile.components:
        if comp.id == FALLBACK:
            continue
        # A pattern that matches nothing is only a finding when the whole tree was seen.
        for pat, rx in zip(comp.paths, comp.regexes, strict=True):
            if facts.tree_complete and not any(rx.match(p) for p in facts.tree):
                dead.append((comp.id, pat))
        if any(comp.matches(p) for p in facts.tree) and comp.id not in claimed.values():
            shadowed.append(comp.id)
    has_authors = raw is None or "authors" in raw
    basis = (
        "changed lines in recent PRs"
        if any(p in claimed for p in facts.churn)
        else "files in the repo (no merged PRs to learn from yet)"
    )
    if not facts.tree_complete:
        basis += f" (only {len(facts.tree)} files of the tree were listed; onboard with --clone to see them all)"
    need = default_tuning().onboarding.min_coverage if min_coverage is None else min_coverage
    return Coverage(total, mapped, by_component, unmapped.most_common(8), dead, shadowed, has_authors, basis, need)


def author_table(
    profile: Profile, facts: RepoFacts, members: AbstractSet[str] = frozenset()
) -> list[tuple[str, str, str, int]]:
    """How recent PR authors classify under this profile: (login, association, kind, PR count)."""
    from readthrough.analyze import author_kind

    counts: Counter[tuple[str, str, str]] = Counter()
    for a in facts.authors:
        kind = author_kind(a["login"], a["association"], profile.authors, bot=a.get("bot", False), members=members)
        counts[(a["login"], a["association"], kind)] += 1
    return sorted(
        ((login, assoc, kind, n) for (login, assoc, kind), n in counts.items()), key=lambda r: (r[2], -r[3], r[0])
    )


def check_profile_text(
    text: str, facts: RepoFacts, *, origin: str = "draft", min_coverage: float | None = None
) -> tuple[Profile, Coverage]:
    import tomllib

    raw = tomllib.loads(text)
    profile = parse_profile(raw, origin=origin)
    return profile, measure(profile, facts, raw=raw, min_coverage=min_coverage)


def drift(items: list[dict[str, Any]], threshold: float | None = None) -> dict[str, Any] | None:
    """Warn when the map stops fitting: too much of the open PRs' churn falls into "other"."""
    total = sum(sum(it["by_comp"].values()) for it in items)
    other = sum(it["by_comp"].get(FALLBACK, 0) for it in items)
    if not total:
        return None
    share = 1 - other / total
    if share >= (default_tuning().onboarding.drift_coverage if threshold is None else threshold):
        return None
    paths: Counter[str] = Counter()
    for it in items:
        for f in it["files"]:
            if f["comp"] == FALLBACK:
                paths["/".join(f["path"].split("/")[:2])] += f["add"] + f["del"]
    return {"coverage": round(share, 2), "unmapped": paths.most_common(5)}


def report(cov: Coverage, profile: Profile, authors: list[tuple[str, str, str, int]]) -> str:
    lines = [f"Coverage: {cov.share:.0%} of {cov.total} {cov.basis} map to a component."]
    top = [(profile.component_by_id[c].label, n) for c, n in cov.by_component.most_common(12) if c != FALLBACK]
    if top:
        lines.append("  Busiest: " + ", ".join(f"{label} {n}" for label, n in top))
    if cov.unmapped_top:
        lines.append("  Unmapped (falls into 'other'): " + ", ".join(f"{p} {n}" for p, n in cov.unmapped_top))
    lines.append("Authors in recent PRs:")
    for kind in dict.fromkeys(k for _, _, k, _ in authors):
        people = [f"{login} ({assoc.lower()}, {n})" for login, assoc, k, n in authors if k == kind]
        lines.append(
            f"  {kind}: " + ", ".join(people[:12]) + (f" and {len(people) - 12} more" if len(people) > 12 else "")
        )
    problems = cov.problems()
    lines.append("Ready to accept." if not problems else "Before accepting:")
    lines += [f"  - {p}" for p in problems]
    return "\n".join(lines)
