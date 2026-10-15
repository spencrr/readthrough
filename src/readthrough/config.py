"""Configuration: a global ``config.toml`` listing the repos you've onboarded, plus each repo's data directory.

Layout (``READTHROUGH_HOME``, default ``~/.readthrough``)::

    config.toml                    global settings and one [[repos]] entry per onboarded repo
    readthrough.log
    repos/<owner>/<name>/          everything for one repo
        profile.toml               local architecture profile (from onboarding), if any
        data.json  index.html  seen.json  ledger.json
        briefs/  cache/  study/  worktrees/

``Config`` is the per-repo view the rest of the code works with; ``Settings`` is the whole file.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from readthrough.files import write_atomic
from readthrough.registry import RegistryError
from readthrough.tuning import Tuning, TuningError, default_tuning, load_tuning

DEFAULT_HOME = Path(os.environ.get("READTHROUGH_HOME", Path.home() / ".readthrough"))
CONFIG_SCHEMA = 1
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
AUTO = "auto"


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    """One repo's settings, with global settings folded in."""

    home: Path
    repo: str
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
    copilot_args: list[str] = field(default_factory=list)
    port: int = 8765
    refresh_minutes: int = 10
    tuning: Tuning = field(default_factory=default_tuning)
    # Machine-wide choices from the top of config.toml; see terminals.py, assistants.py, profile.py.
    # The option dicts map a plugin name to its validated options object.
    terminal: str = AUTO
    assistant: str = AUTO
    terminal_options: dict[str, Any] = field(default_factory=dict)
    assistant_options: dict[str, Any] = field(default_factory=dict)
    profile_source_options: dict[str, Any] = field(default_factory=dict)

    @property
    def repo_dir(self) -> Path:
        owner, name = self.repo.split("/")
        return self.home / "repos" / owner / name

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


MACHINE_KEYS = {"terminal", "assistant", "terminal_options", "assistant_options", "profile_source_options"}
REPO_KEYS = ({f.name for f in fields(Config)} - {"home", "repo", "port", "refresh_minutes"} - MACHINE_KEYS) | {"name"}
GLOBAL_KEYS = {
    "schema", "default_repo", "port", "refresh_minutes", "defaults", "tuning", "repos",
    "terminal", "assistant", "terminals", "assistants", "profile_sources",
}  # fmt: skip
PATH_KEYS = {"clone", "profile"}


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
    terminal_options: dict[str, Any] = field(default_factory=dict)
    assistant_options: dict[str, Any] = field(default_factory=dict)
    profile_source_options: dict[str, Any] = field(default_factory=dict)

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
        return [r["name"] for r in self.repos]

    def entry(self, repo: str) -> dict[str, Any] | None:
        return next((r for r in self.repos if r["name"].lower() == repo.lower()), None)

    def config_for(self, repo: str | None = None) -> Config:
        """The per-repo view. With no repo given, the default repo, or the only one."""
        if repo is None:
            repo = self.default_repo or (self.repos[0]["name"] if len(self.repos) == 1 else None)
            if repo is None:
                if not self.repos:
                    raise ConfigError("No repos yet. Onboard one with `readthrough onboard owner/name`.")
                raise ConfigError(f"Several repos are configured ({', '.join(self.names())}); pick one with --repo.")
        entry = self.entry(repo)
        if entry is None:
            if not REPO_RE.match(repo):
                raise ConfigError(f"{repo!r} isn't owner/name")
            entry = {"name": repo}
        merged = {**self.defaults, **{k: v for k, v in entry.items() if k not in ("name", "tuning")}}
        try:
            tuning = load_tuning(
                self.tuning_raw, repo_overrides=entry.get("tuning"), where=f"tuning for {entry['name']}"
            )
        except TuningError as exc:
            raise ConfigError(f"{self.path}: {exc}") from exc
        cfg = Config(
            home=self.home, repo=entry["name"], port=self.port, refresh_minutes=self.refresh_minutes, tuning=tuning,
            terminal=self.terminal, assistant=self.assistant, terminal_options=self.terminal_options,
            assistant_options=self.assistant_options, profile_source_options=self.profile_source_options,
        )  # fmt: skip
        for key, value in merged.items():
            setattr(cfg, key, Path(value).expanduser() if key in PATH_KEYS and value else value)
        return cfg

    def all(self) -> list[Config]:
        return [self.config_for(name) for name in self.names()]

    def upsert(self, repo: str, **values: Any) -> dict[str, Any]:
        entry = self.entry(repo)
        if entry is None:
            entry = {"name": repo}
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
        if self.default_repo and self.default_repo.lower() == repo.lower():
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
    if raw.get("schema", CONFIG_SCHEMA) != CONFIG_SCHEMA:
        raise ConfigError(
            f"{settings.path} is schema {raw['schema']}; this readthrough reads schema {CONFIG_SCHEMA}. Upgrade readthrough."
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
        if not REPO_RE.match(str(entry.get("name", ""))):
            raise ConfigError(f"repos[{i}] in {settings.path} needs name = 'owner/name'")
        settings.repos.append(dict(entry))
    bad = set(settings.defaults) - (REPO_KEYS - {"tuning"})
    if bad:
        raise ConfigError(f"Unknown key(s) {', '.join(sorted(bad))} in [defaults] of {settings.path}")
    for entry in settings.repos:
        settings.config_for(entry["name"])  # validates the repo's own tuning
    return settings


def _load_plugins(settings: Settings, raw: dict[str, Any]) -> None:
    """``terminal``, ``assistant``, and the ``[terminals.*]``, ``[assistants.*]``, ``[profile_sources.*]`` tables.

    Each name is checked against its registry and each table against the plugin's options, so a typo
    fails here, where the message can say what was meant, rather than when a review is launched.
    """
    from readthrough.assistants import ASSISTANTS
    from readthrough.profile import PROFILE_SOURCES
    from readthrough.terminals import TERMINALS

    base = settings.path.parent
    for key, registry in (("terminal", TERMINALS), ("assistant", ASSISTANTS)):
        name = raw.get(key, AUTO)
        if not isinstance(name, str):
            raise ConfigError(f"{key} must be a name, not {name!r}")
        if name != AUTO:
            registry.get(name)
        setattr(settings, key, name)
    for key, registry, attr in (
        ("terminals", TERMINALS, "terminal_options"),
        ("assistants", ASSISTANTS, "assistant_options"),
        ("profile_sources", PROFILE_SOURCES, "profile_source_options"),
    ):
        tables = raw.get(key) or {}
        if not isinstance(tables, dict) or not all(isinstance(t, dict) for t in tables.values()):
            raise ConfigError(f"[{key}] must hold one [{key}.<name>] table per plugin")
        validated = {
            name: registry.options_for(name, table, where=f"[{key}.{name}]", base=base)
            for name, table in tables.items()
        }
        setattr(settings, attr, validated)


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
    out += [f"port = {settings.port}", f"refresh_minutes = {settings.refresh_minutes}", ""]
    if settings.defaults:
        out.append("[defaults]   # apply to every repo unless the repo sets its own")
        out += [f"{k} = {_value(portable_path(v) if k in PATH_KEYS else v)}" for k, v in settings.defaults.items()]
        out.append("")
    out += _tables("tuning", settings.tuning_raw)
    for entry in settings.repos:
        out.append("[[repos]]")
        out.append(f"name = {_value(entry['name'])}")
        out += [
            f"{k} = {_value(portable_path(v) if k in PATH_KEYS else v)}"
            for k, v in entry.items()
            if k not in ("name", "tuning")
        ]
        out.append("")
        out += _tables("repos.tuning", entry.get("tuning") or {})
    return "\n".join(out)


# ---------------------------------------------------------------- local clones


def _git(args: list[str]) -> str:
    from readthrough.github import GhError, run

    try:
        return run(["git", *args])
    except GhError:
        return ""


def remote_for(clone: Path, repo: str) -> str | None:
    """The name of the remote in ``clone`` that points at ``repo`` (https or ssh, any host alias)."""
    want = repo.lower()
    for line in _git(["-C", str(clone), "remote", "-v"]).splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.search(rf"[:/]{re.escape(want)}(?:\.git)?/?$", parts[1].lower()):
            return parts[0]
    return None


def guess_clone(repo: str) -> Path | None:
    """Look for an existing clone of ``repo`` in the usual places."""
    name = repo.split("/")[1]
    for base in (Path.home() / "src", Path.home() / "source" / "repos", Path.home() / "code", Path.home()):
        for candidate in (base / name, base / name.lower()):
            if (candidate / ".git").exists() and remote_for(candidate, repo):
                return candidate
    return None
