"""Configuration: a global ``config.toml`` listing the repos you've onboarded, plus each repo's data directory.

Layout (``READTHROUGH_HOME``, default ``~/.readthrough``)::

    config.toml                    global settings and one [[repos]] entry per onboarded repo
    readthrough.log
    repos/<forge>/<owner>/<name>/  everything for one repo (GitLab groups and ADO projects add segments)
        profile.toml               local architecture profile (from onboarding), if any
        data.json  index.html  seen.json  ledger.json
        briefs/  cache/  study/  worktrees/

``Config`` is the per-repo view the rest of the code works with; ``Settings`` is the whole file.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from readthrough.files import write_atomic
from readthrough.registry import RegistryError
from readthrough.tuning import Tuning, TuningError, default_tuning, load_tuning

DEFAULT_HOME = Path(os.environ.get("READTHROUGH_HOME", Path.home() / ".readthrough"))
CONFIG_SCHEMA = 2  # 2: repos name their forge, and data lives under repos/<forge>/...
AUTO = "auto"


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    """One repo's settings, with global settings folded in."""

    home: Path
    repo: str
    forge: str = "github"
    clone: Path | None = None
    remote: str | None = None
    account: str | None = None
    profile: Path | None = None
    onboarded: str | None = None
    session_minutes: int = 60
    limit: int = 100
    internal_authors: list[str] = field(default_factory=list)
    external_authors: list[str] = field(default_factory=list)
    study_model: str | None = None
    agent_args: list[str] = field(default_factory=list)  # extra flags for the review agent, for this repo
    port: int = 8765
    refresh_minutes: int = 10
    tuning: Tuning = field(default_factory=default_tuning)
    # Machine-wide choices from the top of config.toml; see terminals.py, assistants.py, agents.py, profile.py.
    # The option dicts map a plugin name to its validated options object.
    terminal: str = AUTO
    assistant: str = AUTO
    agent: str = AUTO
    terminal_options: dict[str, Any] = field(default_factory=dict)
    assistant_options: dict[str, Any] = field(default_factory=dict)
    agent_options: dict[str, Any] = field(default_factory=dict)
    profile_source_options: dict[str, Any] = field(default_factory=dict)
    forge_options: dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        """``<forge>/<repo path>``: the key for URLs and the data directory."""
        return f"{self.forge}/{self.repo}"

    @property
    def repo_dir(self) -> Path:
        return self.home.joinpath("repos", *self.slug.split("/"))

    @property
    def briefs_dir(self) -> Path:
        return self.repo_dir / "briefs"

    @property
    def cache_dir(self) -> Path:
        return self.repo_dir / "cache"

    @property
    def study_dir(self) -> Path:
        return self.repo_dir / "study"

    @property
    def worktrees_dir(self) -> Path:
        return self.repo_dir / "worktrees"

    @property
    def data_path(self) -> Path:
        return self.repo_dir / "data.json"

    @property
    def dashboard_path(self) -> Path:
        return self.repo_dir / "index.html"

    @property
    def ledger_path(self) -> Path:
        return self.repo_dir / "ledger.json"

    @property
    def seen_path(self) -> Path:
        return self.repo_dir / "seen.json"

    @property
    def local_profile_path(self) -> Path:
        return self.repo_dir / "profile.toml"

    @property
    def log_path(self) -> Path:
        return self.home / "readthrough.log"

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"


MACHINE_CHOICES = ("terminal", "assistant", "agent")  # `name = "..."` keys at the top of config.toml
MACHINE_KEYS = {
    *MACHINE_CHOICES,
    "terminal_options",
    "assistant_options",
    "agent_options",
    "profile_source_options",
    "forge_options",
}
REPO_KEYS = ({f.name for f in fields(Config)} - {"home", "repo", "port", "refresh_minutes"} - MACHINE_KEYS) | {"name"}
GLOBAL_KEYS = {
    "schema", "default_repo", "port", "refresh_minutes", "defaults", "tuning", "repos",
    *MACHINE_CHOICES, "terminals", "assistants", "agents", "profile_sources", "forges",
}  # fmt: skip
PATH_KEYS = {"clone", "profile"}
# config.toml table -> the Settings attribute holding its validated per-plugin options.
PLUGIN_TABLES = {
    "terminals": "terminal_options",
    "assistants": "assistant_options",
    "agents": "agent_options",
    "profile_sources": "profile_source_options",
    "forges": "forge_options",
}


@dataclass
class Settings:
    home: Path = DEFAULT_HOME
    default_repo: str | None = None
    port: int = 8765
    refresh_minutes: int = 10
    defaults: dict[str, Any] = field(default_factory=dict)
    repos: list[dict[str, Any]] = field(default_factory=list)
    tuning_raw: dict[str, Any] = field(default_factory=dict)
    config_file: Path | None = None
    terminal: str = AUTO
    assistant: str = AUTO
    agent: str = AUTO
    terminal_options: dict[str, Any] = field(default_factory=dict)
    assistant_options: dict[str, Any] = field(default_factory=dict)
    agent_options: dict[str, Any] = field(default_factory=dict)
    profile_source_options: dict[str, Any] = field(default_factory=dict)
    forge_options: dict[str, Any] = field(default_factory=dict)
    plugin_raw: dict[str, dict[str, Any]] = field(default_factory=dict)  # the option tables as written, for saving

    @property
    def path(self) -> Path:
        """config.toml: in the data directory, unless READTHROUGH_CONFIG points somewhere else (a dotfiles repo, say)."""
        return self.config_file or self.home / "config.toml"

    @property
    def tuning(self) -> Tuning:
        return load_tuning(self.tuning_raw)

    @property
    def log_path(self) -> Path:
        return self.home / "readthrough.log"

    def names(self) -> list[str]:
        """Each repo as the user refers to it: the plain name when that's unambiguous, else ``forge/name``."""
        counts: dict[str, int] = {}
        for r in self.repos:
            counts[r["name"].lower()] = counts.get(r["name"].lower(), 0) + 1
        return [r["name"] if counts[r["name"].lower()] == 1 else _slug(r) for r in self.repos]

    def display_name(self, repo: str) -> str:
        """How to refer to a configured repo in messages and ``default_repo``: see ``names()``."""
        entry = self.entry(repo)
        if entry is None:
            raise ConfigError(f"{repo} isn't configured")
        return self.names()[self.repos.index(entry)]

    def entry(self, repo: str, forge: str | None = None) -> dict[str, Any] | None:
        """The entry ``repo`` names: a ``forge/owner/name`` slug, or a plain name (with ``forge`` to disambiguate)."""
        want = repo.lower()
        by_slug = [r for r in self.repos if _slug(r).lower() == want]
        if by_slug:
            return by_slug[0]
        by_name = [r for r in self.repos if r["name"].lower() == want and (forge is None or _forge_of(r) == forge)]
        if len(by_name) > 1:
            raise ConfigError(
                f"{repo} is onboarded on several forges ({', '.join(_slug(r) for r in by_name)}); name one"
            )
        return by_name[0] if by_name else None

    def config_for(self, repo: str | None = None, *, forge: str | None = None) -> Config:
        """The per-repo view. With no repo given, the default repo, or the only one.

        A repo that isn't configured yet gets a config too (onboarding starts that way); ``forge`` says
        which platform it's on, and the name is checked against that platform's shape.
        """
        if repo is None:
            repo = self.default_repo or (self.repos[0]["name"] if len(self.repos) == 1 else None)
            if repo is None:
                if not self.repos:
                    raise ConfigError("No repos yet. Onboard one with `readthrough onboard owner/name`.")
                raise ConfigError(f"Several repos are configured ({', '.join(self.names())}); pick one with --repo.")
        entry = self.entry(repo, forge)
        if entry is None:
            forge_name, name = _split_slug(repo, forge)
            problem = _validate_repo(forge_name, name)
            if problem:
                raise ConfigError(problem)
            entry = {"name": name, "forge": forge_name}
        merged = {**self.defaults, **{k: v for k, v in entry.items() if k not in ("name", "tuning")}}
        try:
            tuning = load_tuning(
                self.tuning_raw, repo_overrides=entry.get("tuning"), where=f"tuning for {entry['name']}"
            )
        except TuningError as exc:
            raise ConfigError(f"{self.path}: {exc}") from exc
        cfg = Config(
            home=self.home,
            repo=entry["name"],
            port=self.port,
            refresh_minutes=self.refresh_minutes,
            tuning=tuning,
            **{key: getattr(self, key) for key in MACHINE_KEYS},
        )
        for key, value in merged.items():
            setattr(cfg, key, Path(value).expanduser() if key in PATH_KEYS and value else value)
        return cfg

    def all(self) -> list[Config]:
        return [self.config_for(_slug(r)) for r in self.repos]

    def upsert(self, repo: str, *, forge: str | None = None, **values: Any) -> dict[str, Any]:
        entry = self.entry(repo, forge)
        if entry is None:
            forge_name, name = _split_slug(repo, forge)
            entry = {"name": name, "forge": forge_name}
            self.repos.append(entry)
        for key, value in values.items():
            if key not in REPO_KEYS:
                raise ConfigError(f"unknown repo setting {key!r}")
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = str(value) if isinstance(value, Path) else value
        return entry

    def remove(self, repo: str) -> bool:
        entry = self.entry(repo)
        if entry is None:
            return False
        self.repos.remove(entry)
        if self.default_repo and self.default_repo.lower() in (
            repo.lower(),
            entry["name"].lower(),
            _slug(entry).lower(),
        ):
            self.default_repo = None
        return True

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        write_atomic(self.path, dump_settings(self))


def load_settings(home: Path | None = None) -> Settings:
    """Read config.toml. ``READTHROUGH_CONFIG`` names another file, unless a home directory is given explicitly."""
    env_file = os.environ.get("READTHROUGH_CONFIG")
    settings = Settings(
        home=home or DEFAULT_HOME, config_file=Path(env_file).expanduser() if env_file and home is None else None
    )
    if not settings.path.exists():
        return settings
    try:
        raw = tomllib.loads(settings.path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{settings.path}: {exc}") from exc
    unknown = set(raw) - GLOBAL_KEYS
    if unknown:
        hints = "".join(_did_you_mean(k, GLOBAL_KEYS) for k in sorted(unknown))
        raise ConfigError(f"Unknown key(s) {', '.join(sorted(unknown))} in {settings.path}{hints}")
    found = raw.get("schema", CONFIG_SCHEMA)
    if found > CONFIG_SCHEMA:
        raise ConfigError(
            f"{settings.path} is schema {found}; this readthrough reads schema {CONFIG_SCHEMA}. Upgrade readthrough."
        )
    if found < CONFIG_SCHEMA:
        raise ConfigError(
            f"{settings.path} is schema {found}; this readthrough reads schema {CONFIG_SCHEMA}. "
            f"Schema 2 files each repo's data under repos/<forge>/..., so move repos/<owner>/<name> to "
            f"repos/github/<owner>/<name>, rename [tuning.github] to [tuning.forge], and set schema = {CONFIG_SCHEMA}."
        )
    settings.default_repo = raw.get("default_repo")
    settings.port = int(raw.get("port", settings.port))
    settings.refresh_minutes = int(raw.get("refresh_minutes", settings.refresh_minutes))
    settings.defaults = dict(raw.get("defaults") or {})
    settings.tuning_raw = dict(raw.get("tuning") or {})
    try:
        load_tuning(settings.tuning_raw)
        _load_plugins(settings, raw)
    except (TuningError, RegistryError) as exc:
        raise ConfigError(f"{settings.path}: {exc}") from exc
    for i, entry in enumerate(raw.get("repos") or []):
        bad = set(entry) - REPO_KEYS
        if bad:
            hints = "".join(_did_you_mean(k, REPO_KEYS) for k in sorted(bad))
            raise ConfigError(f"Unknown key(s) {', '.join(sorted(bad))} in repos[{i}] of {settings.path}{hints}")
        forge_name = str(entry.get("forge", "github"))
        from readthrough.forges import FORGES

        try:
            FORGES.get(forge_name)
        except RegistryError as exc:
            raise ConfigError(f"repos[{i}] in {settings.path}: {exc}") from exc
        problem = _validate_repo(forge_name, str(entry.get("name", "")))
        if problem:
            raise ConfigError(f"repos[{i}] in {settings.path}: {problem}")
        settings.repos.append(dict(entry))
    bad = set(settings.defaults) - (REPO_KEYS - {"tuning", "forge"})
    if bad:
        raise ConfigError(f"Unknown key(s) {', '.join(sorted(bad))} in [defaults] of {settings.path}")
    for entry in settings.repos:
        settings.config_for(_slug(entry))  # validates the repo's own tuning
    return settings


def _forge_of(entry: dict[str, Any]) -> str:
    return str(entry.get("forge") or "github")


def _slug(entry: dict[str, Any]) -> str:
    return f"{_forge_of(entry)}/{entry['name']}"


def _split_slug(repo: str, forge: str | None) -> tuple[str, str]:
    """``forge/owner/name`` -> (forge, owner/name); a plain name takes ``forge`` or the default.

    The first segment only counts as a forge when what follows is a valid locator there, so a GitHub owner
    that happens to be called ``gitlab`` still reads as ``gitlab/<name>`` on GitHub.
    """
    from readthrough.forges import FORGES, validate_repo

    head, _, rest = repo.partition("/")
    if forge is None and head in FORGES and rest and validate_repo(head, rest) is None:
        return head, rest
    return forge or "github", repo


def _validate_repo(forge: str, repo: str) -> str | None:
    from readthrough.forges import FORGES, validate_repo

    try:
        FORGES.get(forge)
    except RegistryError as exc:
        return str(exc)
    return validate_repo(forge, repo)


def _load_plugins(settings: Settings, raw: dict[str, Any]) -> None:
    """``terminal``, ``assistant``, ``agent``, and the ``[terminals.*]``, ``[assistants.*]``, ``[agents.*]``,
    ``[profile_sources.*]``, ``[forges.*]`` tables.

    Each name is checked against its registry and each table against the plugin's options, so a typo
    fails here, where the message can say what was meant, rather than when a review is launched.
    """
    from readthrough.agents import AGENTS
    from readthrough.assistants import ASSISTANTS
    from readthrough.forges import FORGES
    from readthrough.profile import PROFILE_SOURCES
    from readthrough.terminals import TERMINALS

    base = settings.path.parent
    for key, registry in (("terminal", TERMINALS), ("assistant", ASSISTANTS), ("agent", AGENTS)):
        name = raw.get(key, AUTO)
        if not isinstance(name, str):
            raise ConfigError(f"{key} must be a name, not {name!r}")
        if name != AUTO:
            registry.get(name)
        setattr(settings, key, name)
    registries = {
        "terminals": TERMINALS,
        "assistants": ASSISTANTS,
        "agents": AGENTS,
        "profile_sources": PROFILE_SOURCES,
        "forges": FORGES,
    }
    for key, attr in PLUGIN_TABLES.items():
        tables = raw.get(key) or {}
        if not isinstance(tables, dict) or not all(isinstance(t, dict) for t in tables.values()):
            raise ConfigError(f"[{key}] must hold one [{key}.<name>] table per plugin")
        validated = {
            name: registries[key].options_for(name, table, where=f"[{key}.{name}]", base=base)
            for name, table in tables.items()
        }
        setattr(settings, attr, validated)
        settings.plugin_raw[key] = {name: dict(table) for name, table in tables.items()}


def _did_you_mean(key: str, options: Any) -> str:
    import difflib

    close = difflib.get_close_matches(key, sorted(options), n=1)
    return f" ({key}: did you mean {close[0]!r}?)" if close else ""


def portable_path(value: Any) -> str:
    """Write paths under your home directory as ``~/...`` so the file works on another machine or account."""
    path = Path(value).expanduser()
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(value)


# ---------------------------------------------------------------- writing (tomllib only reads)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return json.dumps(str(value), ensure_ascii=False)


def _tables(prefix: str, raw: dict[str, Any]) -> list[str]:
    out = []
    for section, values in raw.items():
        out.append(f"[{prefix}.{section}]")
        out += [f"{k} = {_value(v)}" for k, v in values.items()]
        out.append("")
    return out


def dump_settings(settings: Settings) -> str:
    out = [
        '# readthrough configuration. See docs/guide.md, "Configuration". Tunable settings and their',
        "# defaults: `readthrough config --defaults`.",
        f"schema = {CONFIG_SCHEMA}",
        "",
    ]
    if settings.default_repo:
        out.append(f"default_repo = {_value(settings.default_repo)}")
    out += [f"port = {settings.port}", f"refresh_minutes = {settings.refresh_minutes}"]
    for key in MACHINE_CHOICES:
        if getattr(settings, key) != AUTO:
            out.append(f"{key} = {_value(getattr(settings, key))}")
    out.append("")
    if settings.defaults:
        out.append("[defaults]   # apply to every repo unless the repo sets its own")
        out += [f"{k} = {_value(portable_path(v) if k in PATH_KEYS else v)}" for k, v in settings.defaults.items()]
        out.append("")
    for key in PLUGIN_TABLES:
        out += _tables(key, settings.plugin_raw.get(key) or {})
    out += _tables("tuning", settings.tuning_raw)
    for entry in settings.repos:
        out.append("[[repos]]")
        out.append(f"name = {_value(entry['name'])}")
        out += [
            f"{k} = {_value(portable_path(v) if k in PATH_KEYS else v)}"
            for k, v in entry.items()
            if k not in ("name", "tuning") and not (k == "forge" and v == "github")
        ]
        out.append("")
        out += _tables("repos.tuning", entry.get("tuning") or {})
    return "\n".join(out)


# ---------------------------------------------------------------- local clones


def _git(args: list[str]) -> str:
    from readthrough.faults import ForgeError
    from readthrough.process import run

    try:
        return run(["git", *args])
    except ForgeError:
        return ""


def remote_for(clone: Path, repo: str, forge: str = "github") -> str | None:
    """The name of the remote in ``clone`` that points at ``repo`` (https or ssh, any host alias).

    What a remote URL for ``repo`` looks like is the forge's to say (``Forge.remote_matches``).
    """
    from readthrough.forges import adapter

    matches = adapter(forge).remote_matches
    for line in _git(["-C", str(clone), "remote", "-v"]).splitlines():
        parts = line.split()
        if len(parts) >= 2 and matches(parts[1], repo):
            return parts[0]
    return None


def guess_clone(repo: str, forge: str = "github") -> Path | None:
    """Look for an existing clone of ``repo`` in the usual places."""
    name = repo.rsplit("/", 1)[1]  # the repo's own name: the last segment on every forge
    for base in (Path.home() / "src", Path.home() / "source" / "repos", Path.home() / "code", Path.home()):
        for candidate in (base / name, base / name.lower()):
            if (candidate / ".git").exists() and remote_for(candidate, repo, forge):
                return candidate
    return None
