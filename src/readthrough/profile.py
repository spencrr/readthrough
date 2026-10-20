"""Architecture profiles: everything repo-specific, as data.

A profile describes one repository's architecture (layers and components, mapped to paths), where its
architectural contracts live, which review rules apply, which signals and line heuristics to look for,
and how to classify PR authors. The dashboard's core reads only the profile, so onboarding a new repo is
writing (or generating) a profile, not changing code.

Profiles are TOML (stdlib ``tomllib``) and versioned with ``schema``.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from readthrough.config import portable_path
from readthrough.model import REPO_PATH_RE, ChangedFile
from readthrough.registry import Registry
from readthrough.rules import glob_to_regex

SCHEMA = 1
FALLBACK = "other"
DRAFTERS = ("readthrough", "copilot")  # who wrote the first draft; shown so readers know what to double-check
KINDS = ("risk", "blocker", "convention", "responsibility", "correctness", "info", "author", "you")
DEFAULT_ASSOCIATION = {
    "OWNER": "internal",
    "MEMBER": "internal",
    "COLLABORATOR": "internal",
    "CONTRIBUTOR": "external",
    "FIRST_TIME_CONTRIBUTOR": "first-time",
    "FIRST_TIMER": "first-time",
    "NONE": "first-time",
}


class ProfileError(ValueError):
    pass


@dataclass
class Layer:
    id: str
    label: str
    source: bool = True
    flow: bool = False
    color: str | None = None


@dataclass
class Component:
    id: str
    label: str
    layer: str
    paths: list[str]
    contract: str | None = None
    responsibility: str | None = None
    does_not_own: str | None = None
    questions: list[str] = field(default_factory=list)

    @cached_property
    def regexes(self) -> list[re.Pattern[str]]:
        return [glob_to_regex(p) for p in self.paths]

    def matches(self, path: str) -> bool:
        return any(r.match(path) for r in self.regexes)


@dataclass
class Signal:
    """Flag a PR when any changed file matches ``paths`` (optionally only for some change types)."""

    id: str
    kind: str
    message: str
    paths: list[str]
    change_types: list[str] = field(default_factory=list)

    @cached_property
    def regexes(self) -> list[re.Pattern[str]]:
        return [glob_to_regex(p) for p in self.paths]

    def fires(self, files: list[ChangedFile]) -> bool:
        return any(
            any(r.match(f.path) for r in self.regexes) and (not self.change_types or f.change in self.change_types)
            for f in files
        )


@dataclass
class PairedFiles:
    """Files that should change together, e.g. a notebook and the script it's generated from."""

    id: str
    kind: str
    message: str
    under: str
    extensions: list[str]


@dataclass
class Smell:
    """A regex over added lines. Prompts to look closer, never verdicts."""

    id: str
    kind: str
    message: str
    path: str
    line: str
    strip_brackets: bool = False

    @cached_property
    def path_re(self) -> re.Pattern[str]:
        return re.compile(self.path)

    @cached_property
    def line_re(self) -> re.Pattern[str]:
        return re.compile(self.line)


@dataclass
class AuthorKind:
    id: str
    label: str
    badge: str
    group: str
    signal: dict[str, str] | None = None
    questions: list[str] = field(default_factory=list)
    priority: float = 0.0


@dataclass
class Authors:
    internal: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)
    bots: list[str] = field(default_factory=list)
    internal_teams: list[str] = field(default_factory=list)
    internal_orgs: list[str] = field(default_factory=list)
    association: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ASSOCIATION))
    unknown: str = "external"
    kinds: dict[str, AuthorKind] = field(default_factory=dict)


@dataclass
class Profile:
    repo: str
    title: str
    layers: list[Layer]
    components: list[Component]
    read_order: list[str]
    contracts_source: str | None = None
    contracts_parser: str = "markdown-sections"
    contracts_labels: dict[str, str] = field(
        default_factory=lambda: {"responsibility": "Responsibility", "does_not_own": "Does not own"}
    )
    instructions_dir: str | None = ".github/instructions"
    review_guidance_file: str | None = None
    review_guidance: str = ""
    study_guidance: str = ""
    test_cmd: str | None = None
    questions: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    paired_files: list[PairedFiles] = field(default_factory=list)
    smells: list[Smell] = field(default_factory=list)
    test_paths: list[str] = field(default_factory=lambda: ["tests/**", "**/tests/**"])
    tests_component: str | None = "tests"
    no_tests_min_lines: int = 20
    cross_cutting_layers: int = 3
    large_lines: int = 800
    stale_days: int = 21
    capped_paths: list[str] = field(default_factory=list)
    capped_lines: int = 20
    overlap_ignore: list[str] = field(default_factory=list)
    overlap_non_source: list[str] = field(default_factory=list)
    authors: Authors = field(default_factory=Authors)
    drafted_by: str | None = None
    origin: str = ""
    notes: list[str] = field(default_factory=list)  # anything odd about how it was found, for the build's warnings

    # ---- lookups

    @cached_property
    def layer_by_id(self) -> dict[str, Layer]:
        return {layer.id: layer for layer in self.layers}

    @cached_property
    def component_by_id(self) -> dict[str, Component]:
        return {c.id: c for c in self.components}

    @cached_property
    def layer_order(self) -> dict[str, int]:
        return {layer.id: i for i, layer in enumerate(self.layers)}

    @cached_property
    def source_layers(self) -> set[str]:
        return {layer.id for layer in self.layers if layer.source}

    @cached_property
    def _test_res(self) -> list[re.Pattern[str]]:
        return [glob_to_regex(p) for p in self.test_paths]

    @cached_property
    def _capped_res(self) -> list[re.Pattern[str]]:
        return [glob_to_regex(p) for p in self.capped_paths]

    @cached_property
    def _overlap_ignore_res(self) -> list[re.Pattern[str]]:
        return [glob_to_regex(p) for p in self.overlap_ignore]

    def component_of(self, path: str) -> str:
        for comp in self.components:
            if comp.matches(path):
                return comp.id
        return FALLBACK

    def is_test(self, path: str) -> bool:
        return any(r.match(path) for r in self._test_res)

    def is_capped(self, path: str) -> bool:
        return any(r.match(path) for r in self._capped_res)

    def overlap_ignored(self, path: str) -> bool:
        return any(r.match(path) for r in self._overlap_ignore_res)

    def is_source(self, component: str) -> bool:
        return self.component_by_id[component].layer in self.source_layers

    def to_payload(self) -> dict[str, Any]:
        """What the dashboard needs to draw the map."""
        return {
            "repo": self.repo,
            "title": self.title,
            "layers": [vars(layer) for layer in self.layers],
            "overlap_non_source": [glob_to_regex(p).pattern for p in self.overlap_non_source],
            "author_kinds": {
                k: {"label": v.label, "badge": v.badge, "group": v.group} for k, v in self.authors.kinds.items()
            },
            "origin": self.origin,
            "drafted_by": self.drafted_by,
            "contracts_source": self.contracts_source,
        }


# ---------------------------------------------------------------- finding a repo's profile

PROFILE_FILE = "readthrough.toml"


def repo_profile_paths(forge: Any = None) -> list[str]:
    """Where a repo may carry its own profile, in the order they're tried.

    The forge's dot-directory first (``.github/readthrough.toml`` on GitHub: next to CODEOWNERS and the
    other review conventions, and what "Propose upstream" creates), then the repo root, dotted or plain.
    """
    config_dir = getattr(forge, "config_dir", "") or ".github"
    return [f"{config_dir}/{PROFILE_FILE}", f".{PROFILE_FILE}", PROFILE_FILE]


@dataclass
class ProfileSource:
    kind: str  # a finder's name: explicit | local | repo | shared
    label: str
    load: Any
    path: Path | None = None  # the file, for every kind except "repo"
    text: str | None = None  # the file's content when it was fetched rather than read from ``path``

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path is not None else (self.text or "")


# A finder returns the source it would supply for this repo, or None. ``cfg`` is the repo's Config and
# ``forge`` its forge adapter (None when the caller wants to stay offline). Options come from the
# ``[profile_sources.<name>]`` table in config.toml, validated against the finder's options dataclass.
Finder = Callable[[Any, Any, Any], "ProfileSource | None"]

PROFILE_SOURCES: Registry[Finder] = Registry("profile source")

# Highest precedence first. A local draft always wins, so you can experiment; the repo's own file is the
# shared one; directories of profiles are for teams whose repos can't carry the file.
PRECEDENCE = ("explicit", "local", "repo", "shared")


@PROFILE_SOURCES.add("explicit", "the file named by `profile = ...` in the repo's config entry")
def _explicit(cfg: Any, forge: Any, options: Any) -> ProfileSource | None:
    if not getattr(cfg, "profile", None):
        return None
    path = Path(cfg.profile).expanduser()
    if not path.is_file():
        raise ProfileError(f"The profile configured for {cfg.repo}, {path}, doesn't exist")
    label = portable_path(path)
    return ProfileSource("explicit", label, lambda: load_profile(path, origin=label), path)


@PROFILE_SOURCES.add("local", "your draft from onboarding, in the repo's data directory")
def _local(cfg: Any, forge: Any, options: Any) -> ProfileSource | None:
    local = cfg.local_profile_path
    if not local.exists():
        return None
    label = portable_path(local)
    return ProfileSource("local", label, lambda: load_profile(local, origin=label), local)


@PROFILE_SOURCES.add(
    "repo", f"the repo's own {PROFILE_FILE} (in its forge's dot-directory or at the root), read from the forge"
)
def _repo(cfg: Any, forge: Any, options: Any) -> ProfileSource | None:
    if forge is None:
        return None
    candidates = repo_profile_paths(forge)
    found = forge.read_files(candidates)
    if not found:
        return None
    path = next(p for p in candidates if p in found)
    label = f"{cfg.repo}:{path}"
    notes = []
    if len(found) > 1:
        others = ", ".join(p for p in candidates if p in found and p != path)
        notes.append(f"{cfg.repo} carries more than one profile ({path} is used; also found {others})")

    def load() -> Profile:
        profile = parse_profile(_toml(found[path], label), origin=label)
        profile.notes.extend(notes)
        return profile

    return ProfileSource("repo", label, load, text=found[path])


@dataclass(frozen=True)
class SharedOptions:
    dirs: list[Path] = field(default_factory=list)  # each holds <owner>/<name>.toml files


@PROFILE_SOURCES.add(
    "shared", "directories of profiles, one file per repo (`dirs` in [profile_sources.shared])", options=SharedOptions
)
def _shared(cfg: Any, forge: Any, options: Any) -> ProfileSource | None:
    for directory in options.dirs:
        path = shared_profile_path(directory, getattr(cfg, "slug", f"github/{cfg.repo}"))
        if path is not None:
            return ProfileSource("shared", str(path), lambda p=path: load_profile(p), path)
    return None


def shared_profile_path(directory: Path, slug: str) -> Path | None:
    """``<directory>/<forge>/<owner>/<name>.toml``, matched case-insensitively the way forges treat repo names.

    ``<directory>/<owner>/<name>.toml`` is also accepted for GitHub repos, which is what the directory
    held before there were other forges.
    """
    *parents, name = slug.split("/")
    layouts = [parents] + ([parents[1:]] if parents[:1] == ["github"] else [])
    for layout in layouts:
        exact = directory.joinpath(*layout) / f"{name}.toml"
        if exact.is_file():
            return exact
        if not directory.is_dir():
            return None
        pattern = "/".join(["*"] * len(layout) + ["*.toml"])
        for candidate in directory.glob(pattern):
            found = [*candidate.relative_to(directory).parts[:-1], candidate.stem]
            if [p.lower() for p in found] == [p.lower() for p in [*layout, name]]:
                return candidate
    return None


def profile_sources(cfg: Any, forge: Any = None, *, first_only: bool = False) -> list[ProfileSource]:
    """Every place a profile for ``cfg.repo`` could come from, highest precedence first.

    With ``first_only``, stops at the first one found, so a local profile saves fetching the repo's file.
    """
    configured = getattr(cfg, "profile_source_options", None) or {}
    sources: list[ProfileSource] = []
    for name in PRECEDENCE:
        finder = PROFILE_SOURCES.get(name)
        options = configured.get(name) or PROFILE_SOURCES.defaults_for(name)
        source = finder.impl(cfg, forge, options)
        if source is not None:
            sources.append(source)
            if first_only:
                break
    return sources


def _toml(text: str, label: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{label}: {exc}") from exc


def resolve_profile(cfg: Any, forge: Any = None) -> Profile:
    """The profile for ``cfg.repo`` from the highest-precedence source that has one."""
    sources = profile_sources(cfg, forge, first_only=True)
    if not sources:
        raise ProfileError(f"No architecture profile for {cfg.repo}. Onboard it: readthrough onboard {cfg.repo}")
    profile = sources[0].load()
    if profile.repo.lower() != cfg.repo.lower():
        raise ProfileError(f"{profile.origin} is for {profile.repo}, not {cfg.repo}")
    # Local login overrides from config.toml extend the profile's lists.
    profile.authors.internal = [*profile.authors.internal, *getattr(cfg, "internal_authors", [])]
    profile.authors.external = [*profile.authors.external, *getattr(cfg, "external_authors", [])]
    return profile


# ---------------------------------------------------------------- loading


_TOP_KEYS = {
    "schema", "repo", "title", "drafted_by", "layers", "components", "read_order", "contracts", "rules", "review", "study",
    "questions", "signals", "paired_files", "smells", "checks", "weights", "overlap", "authors",
}  # fmt: skip


def load_profile(path: Path | str, *, origin: str | None = None) -> Profile:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{path}: {exc}") from exc
    return parse_profile(raw, origin=origin or str(path))


def parse_profile(raw: dict[str, Any], *, origin: str = "") -> Profile:
    where = origin or "profile"

    def fail(msg: str) -> ProfileError:
        return ProfileError(f"{where}: {msg}")

    def table(data: Any, name: str, allowed: set[str]) -> dict[str, Any]:
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise fail(f"[{name}] must be a table")
        unknown = set(data) - allowed
        if unknown:
            raise fail(f"unknown key(s) in [{name}]: {', '.join(sorted(unknown))}")
        return data

    def strs(value: Any, name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise fail(f"{name} must be a list of strings")
        return value

    def kind_of(value: Any, name: str) -> str:
        if value not in KINDS:
            raise fail(f"{name}: kind must be one of {', '.join(KINDS)}")
        return value

    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise fail(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    if raw.get("schema") != SCHEMA:
        raise fail(f"schema must be {SCHEMA} (got {raw.get('schema')!r})")
    if raw.get("drafted_by") not in (None, *DRAFTERS):
        raise fail(f"drafted_by must be one of {', '.join(DRAFTERS)}")
    repo = raw.get("repo")
    # The repo's path on its forge: owner/name on most, group/subgroup/project on GitLab, org/project/repo on
    # Azure DevOps. Which forge is implied by where the profile was found, so it isn't repeated here.
    if not isinstance(repo, str) or not REPO_PATH_RE.fullmatch(repo):
        raise fail("repo must be the repo's path on its forge, like 'owner/name'")

    layers = []
    for i, item in enumerate(raw.get("layers") or []):
        t = table(item, f"layers[{i}]", {"id", "label", "source", "flow", "color"})
        if not t.get("id") or not t.get("label"):
            raise fail(f"layers[{i}] needs id and label")
        layers.append(
            Layer(t["id"], t["label"], bool(t.get("source", True)), bool(t.get("flow", False)), t.get("color"))
        )
    if not layers:
        raise fail("at least one [[layers]] entry is required")
    layer_ids = [layer.id for layer in layers]
    if len(set(layer_ids)) != len(layer_ids):
        raise fail("layer ids must be unique")

    components = []
    for i, item in enumerate(raw.get("components") or []):
        name = f"components[{i}]"
        t = table(
            item, name, {"id", "label", "layer", "paths", "contract", "responsibility", "does_not_own", "questions"}
        )
        for key in ("id", "label", "layer"):
            if not t.get(key):
                raise fail(f"{name} needs {key}")
        if t["layer"] not in layer_ids:
            raise fail(f"{name} ({t['id']}): unknown layer {t['layer']!r}")
        paths = strs(t.get("paths"), f"{name}.paths")
        if not paths:
            raise fail(f"{name} ({t['id']}) needs at least one path pattern")
        components.append(
            Component(
                t["id"], t["label"], t["layer"], paths, t.get("contract"), t.get("responsibility"), t.get("does_not_own"),
                strs(t.get("questions"), f"{name}.questions"),
            )
        )  # fmt: skip
    if not components:
        raise fail("at least one [[components]] entry is required")
    ids = [c.id for c in components]
    if len(set(ids)) != len(ids):
        raise fail("component ids must be unique")
    if FALLBACK not in ids:
        components.append(Component(FALLBACK, "Other", layers[-1].id, ["**"]))
        ids.append(FALLBACK)

    read_order = strs(raw.get("read_order"), "read_order")
    bad = set(read_order) - set(ids)
    if bad:
        raise fail(f"read_order mentions unknown component(s): {', '.join(sorted(bad))}")
    by_layer = sorted(components, key=lambda c: layer_ids.index(c.layer))
    read_order += [c.id for c in by_layer if c.id not in read_order]

    contracts = table(raw.get("contracts"), "contracts", {"source", "parser", "labels"})
    if contracts.get("parser", "markdown-sections") != "markdown-sections":
        raise fail("contracts.parser: only 'markdown-sections' is supported")
    labels = {"responsibility": "Responsibility", "does_not_own": "Does not own", **(contracts.get("labels") or {})}

    rules = table(raw.get("rules"), "rules", {"instructions_dir", "guidance_file"})
    review = table(raw.get("review"), "review", {"guidance", "test_cmd"})
    study = table(raw.get("study"), "study", {"guidance"})
    checks = table(
        raw.get("checks"),
        "checks",
        {"test_paths", "tests_component", "no_tests_min_lines", "cross_cutting_layers", "large_lines", "stale_days"},
    )
    weights = table(raw.get("weights"), "weights", {"capped_paths", "capped_lines"})
    overlap = table(raw.get("overlap"), "overlap", {"ignore", "non_source"})
    questions = table(raw.get("questions"), "questions", {"always"})

    tests_component = checks.get("tests_component", "tests" if "tests" in ids else None)
    if tests_component is not None and tests_component not in ids:
        raise fail(f"checks.tests_component: unknown component {tests_component!r}")

    signals = []
    for i, item in enumerate(raw.get("signals") or []):
        t = table(item, f"signals[{i}]", {"id", "kind", "message", "paths", "change_types"})
        signals.append(
            Signal(
                t["id"],
                kind_of(t.get("kind"), f"signals[{i}]"),
                t["message"],
                strs(t.get("paths"), f"signals[{i}].paths"),
                strs(t.get("change_types"), f"signals[{i}].change_types"),
            )
        )
    paired = []
    for i, item in enumerate(raw.get("paired_files") or []):
        t = table(item, f"paired_files[{i}]", {"id", "kind", "message", "under", "extensions"})
        exts = strs(t.get("extensions"), f"paired_files[{i}].extensions")
        if len(exts) != 2:
            raise fail(f"paired_files[{i}].extensions needs exactly two extensions")
        paired.append(
            PairedFiles(t["id"], kind_of(t.get("kind"), f"paired_files[{i}]"), t["message"], t.get("under", ""), exts)
        )
    smells = []
    for i, item in enumerate(raw.get("smells") or []):
        t = table(item, f"smells[{i}]", {"id", "kind", "message", "path", "line", "strip_brackets"})
        smell = Smell(
            t["id"],
            kind_of(t.get("kind"), f"smells[{i}]"),
            t["message"],
            t.get("path", "."),
            t["line"],
            bool(t.get("strip_brackets", False)),
        )
        try:
            _ = (smell.path_re, smell.line_re)  # compiles both, raising on a bad regex
        except re.error as exc:
            raise fail(f"smells[{i}] ({smell.id}): bad regex: {exc}") from exc
        smells.append(smell)

    return Profile(
        repo=repo,
        title=raw.get("title") or repo.rsplit("/", 1)[1],
        layers=layers,
        components=components,
        read_order=read_order,
        contracts_source=contracts.get("source"),
        contracts_labels=labels,
        instructions_dir=rules.get("instructions_dir", ".github/instructions"),
        review_guidance_file=rules.get("guidance_file"),
        review_guidance=review.get("guidance", ""),
        study_guidance=study.get("guidance", ""),
        test_cmd=review.get("test_cmd"),
        questions=strs(questions.get("always"), "questions.always"),
        signals=signals,
        paired_files=paired,
        smells=smells,
        test_paths=strs(checks.get("test_paths"), "checks.test_paths") or ["tests/**", "**/tests/**"],
        tests_component=tests_component,
        no_tests_min_lines=int(checks.get("no_tests_min_lines", 20)),
        cross_cutting_layers=int(checks.get("cross_cutting_layers", 3)),
        large_lines=int(checks.get("large_lines", 800)),
        stale_days=int(checks.get("stale_days", 21)),
        capped_paths=strs(weights.get("capped_paths"), "weights.capped_paths"),
        capped_lines=int(weights.get("capped_lines", 20)),
        overlap_ignore=strs(overlap.get("ignore"), "overlap.ignore"),
        overlap_non_source=strs(overlap.get("non_source"), "overlap.non_source"),
        authors=_parse_authors(raw.get("authors"), fail, table, strs, kind_of),
        drafted_by=raw.get("drafted_by"),
        origin=origin,
    )


OUTSIDE_QUESTIONS = [
    "Is there a linked issue or earlier discussion showing the maintainers want this change?",
    "Does it add dependencies, network calls, file writes, or subprocesses? Read each of those lines.",
]
DEFAULT_KINDS = {
    "internal": AuthorKind("internal", "internal", "internal", "internal"),
    "external": AuthorKind(
        "external", "external", "external", "external",
        {"id": "external", "kind": "author", "message": "External contributor: look closely at new dependencies, network, file, and subprocess access"},
        OUTSIDE_QUESTIONS,
    ),
    "first-time": AuthorKind(
        "first-time", "first-time contributor", "first-time", "external",
        {"id": "first-time", "kind": "author", "message": "First-time contributor: check that maintainers asked for this change"},
        OUTSIDE_QUESTIONS,
    ),
    "bot": AuthorKind("bot", "bot", "bot", "bot"),
    "unknown": AuthorKind("unknown", "unknown", "unknown", "external"),
}  # fmt: skip
GROUPS = ("internal", "external", "bot")


def _parse_authors(data: Any, fail: Any, table: Any, strs: Any, kind_of: Any) -> Authors:
    t = table(
        data,
        "authors",
        {"internal", "external", "bots", "internal_teams", "internal_orgs", "association", "unknown", "kinds"},
    )
    kinds = {k: AuthorKind(**vars(v)) for k, v in DEFAULT_KINDS.items()}
    for kid, spec in (t.get("kinds") or {}).items():
        s = table(spec, f"authors.kinds.{kid}", {"label", "badge", "group", "signal", "questions", "priority"})
        base = kinds.get(kid) or AuthorKind(kid, kid, kid, "external")
        group = s.get("group", base.group)
        if group not in GROUPS:
            raise fail(f"authors.kinds.{kid}.group must be one of {', '.join(GROUPS)}")
        signal = s.get("signal", base.signal)
        if signal is not None:
            signal = table(signal, f"authors.kinds.{kid}.signal", {"id", "kind", "message"})
            kind_of(signal.get("kind"), f"authors.kinds.{kid}.signal")
        kinds[kid] = AuthorKind(
            kid, s.get("label", base.label), s.get("badge", base.badge), group, signal,
            strs(s.get("questions"), f"authors.kinds.{kid}.questions") or base.questions, float(s.get("priority", base.priority)),
        )  # fmt: skip
    association = {**DEFAULT_ASSOCIATION, **(t.get("association") or {})}
    unknown_kind = t.get("unknown", "external")
    for name, kid in [*association.items(), ("unknown", unknown_kind)]:
        if kid not in kinds:
            raise fail(f"authors: {name} maps to unknown kind {kid!r}")
    return Authors(
        internal=strs(t.get("internal"), "authors.internal"),
        external=strs(t.get("external"), "authors.external"),
        bots=strs(t.get("bots"), "authors.bots"),
        internal_teams=strs(t.get("internal_teams"), "authors.internal_teams"),
        internal_orgs=strs(t.get("internal_orgs"), "authors.internal_orgs"),
        association=association,
        unknown=unknown_kind,
        kinds=kinds,
    )
