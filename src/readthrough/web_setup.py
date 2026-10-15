"""Onboarding and repo settings from the browser.

Everything here mirrors a CLI command (``readthrough onboard``, ``remove``, ``default``, editing config.toml), and
the page shows the CLI equivalent for each action. The server only calls this for requests that carry its
token and come from its own pages; on top of that, every input is validated here.

Kept out of the browser on purpose: ``copilot_args`` (flags passed straight to a program the server
launches), the ``profile`` path override (makes the server read an arbitrary file), and ``port`` (changing
it from the page you're on breaks that page). The page shows them read-only with the CLI command.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from readthrough import onboard, review
from readthrough.config import REPO_RE, Config, ConfigError, Settings, guess_clone, remote_for
from readthrough.github import FileReader, GhError, GitHub, Reader, run, signed_in_accounts
from readthrough.limits import BROWSER_EDITABLE_INTS, LOGIN_RE, MODEL_RE
from readthrough.profile import REPO_PROFILE_PATH, ProfileError, parse_profile, profile_sources

log = logging.getLogger(__name__)

EDITABLE_LISTS = ("internal_authors", "external_authors")


class SetupError(ValueError):
    """A problem to show the user, as a 400."""


class Conflict(SetupError):
    """config.toml changed since the page loaded it."""


class SetupService:
    def __init__(
        self, settings_loader: Callable[[], Settings], *, on_change: Callable[[], None] = lambda: None
    ) -> None:
        self.load = settings_loader
        self.on_change = on_change
        self._facts: dict[tuple[str, str | None, str | None], tuple[float, onboard.RepoFacts]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._previews: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._config_lock = threading.Lock()

    # ---------------------------------------------------------------- routing

    def get(self, path: str, query: dict[str, str]) -> Any:
        if path == "/api/setup/state":
            return self.state(query.get("repo"))
        if m := re.fullmatch(r"/api/setup/jobs/([0-9a-f]{32})", path):
            return self.job(m.group(1))
        raise LookupError("not found")

    def post(self, path: str, body: dict[str, Any]) -> Any:
        actions: dict[str, Callable[[dict[str, Any]], Any]] = {
            "/api/setup/inspect": self.inspect,
            "/api/setup/clone": self.check_clone,
            "/api/setup/draft": self.start_draft,
            "/api/setup/check": self.check,
            "/api/setup/preview": self.start_preview,
            "/api/setup/save": self.save_profile,
            "/api/setup/accept": self.accept,
            "/api/setup/settings": self.save_settings,
            "/api/setup/remove": self.remove,
        }
        if path not in actions:
            raise LookupError("not found")
        return actions[path](body)

    def preview_payload(self, preview_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._previews.get(preview_id)

    # ---------------------------------------------------------------- reading

    def state(self, repo: str | None) -> dict[str, Any]:
        settings = self.load()
        out: dict[str, Any] = {
            "settings": {
                "default_repo": settings.default_repo,
                "port": settings.port,
                "refresh_minutes": settings.refresh_minutes,
                "version": _version(settings),
                "path": str(settings.path),
            },
            "repos": [_repo_view(c) for c in settings.all()],
            "accounts": signed_in_accounts(),
        }
        if repo:
            repo = _repo_name(repo)
            entry = settings.entry(repo)
            cfg = settings.config_for(repo)
            out["repo"] = {**_repo_view(cfg), "configured": entry is not None, **self._profile_view(cfg)}
        return out

    def _profile_view(self, cfg: Config, gh: Reader | None = None) -> dict[str, Any]:
        try:
            sources = profile_sources(cfg, gh)
        except GhError:
            sources = profile_sources(cfg)
        if not sources:
            return {"source": None, "sources": [], "profile_text": "", "editable": True}
        first = sources[0]
        return {
            "source": {"kind": first.kind, "label": first.label},
            "sources": [{"kind": s.kind, "label": s.label} for s in sources],
            "profile_text": _source_text(first, gh),
            "editable": first.kind == "local",
        }

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise LookupError("no such job")
            return {k: v for k, v in job.items() if k != "thread"}

    # ---------------------------------------------------------------- repo, account, clone

    def inspect(self, body: dict[str, Any]) -> dict[str, Any]:
        repo, account = _repo_name(body.get("repo")), _account(body.get("account"))
        gh = GitHub(repo, account)
        try:
            info = json.loads(gh.api(f"repos/{repo}"))
        except GhError as exc:
            accounts = signed_in_accounts()
            hint = f" Signed-in gh accounts: {', '.join(accounts)}; pick the one that can see it." if accounts else ""
            raise SetupError(f"Can't read {repo}{' as ' + account if account else ''}.{hint}") from exc
        name = info["full_name"]
        settings = self.load()
        entry = settings.entry(name) or {}
        clone = entry.get("clone") or guess_clone(name)
        cfg = settings.config_for(name)
        if account and not entry.get("account"):
            cfg.account = account
        return {
            "full_name": name,
            "private": info.get("private", False),
            "default_branch": info.get("default_branch"),
            "description": info.get("description") or "",
            "configured": bool(entry),
            "onboarded": cfg.onboarded,
            "clone": str(clone) if clone else "",
            **self._profile_view(cfg, gh),
        }

    def check_clone(self, body: dict[str, Any]) -> dict[str, Any]:
        repo = _repo_name(body.get("repo"))
        clone = _clone_path(body.get("clone"))
        if clone is None:
            return {"ok": False, "message": "No clone: reviews and worktrees need one, but the map works without it."}
        remotes = []
        for line in run(["git", "-C", str(clone), "remote", "-v"], check=False).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in (r["name"] for r in remotes):
                remotes.append({"name": parts[0], "url": parts[1]})
        detected = remote_for(clone, repo)
        points = any(r["name"] == detected and repo.lower() in r["url"].lower() for r in remotes)
        return {
            "ok": True,
            "remotes": remotes,
            "detected": detected,
            "message": f"Git repo; remote '{detected}' points at {repo}."
            if points
            else f"Git repo, but no remote points at {repo}; reviews fetch PRs through '{detected}'.",
        }

    # ---------------------------------------------------------------- drafting and checking

    def start_draft(self, body: dict[str, Any]) -> dict[str, Any]:
        repo, account = _repo_name(body.get("repo")), _account(body.get("account"))
        mode = body.get("mode", "plain")
        if mode not in ("plain", "copilot", "source"):
            raise SetupError("mode must be plain, copilot, or source")
        clone = _clone_path(body.get("clone"))
        remote = _remote(body.get("remote"))
        version = body.get("version")

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            cfg = self._remember(repo, account=account, clone=clone, remote=remote, version=version)
            gh = GitHub(cfg.repo, cfg.account)
            facts = self._gather(cfg, gh, say)
            if mode == "source":
                view = self._profile_view(cfg, gh)
                text = view["profile_text"]
                if not text:
                    raise SetupError("There's no existing profile for this repo; draft one instead.")
            elif mode == "copilot":
                from readthrough.onboard_copilot import copilot_draft

                say("Copilot is reading the repo. This takes a few minutes.")
                text = copilot_draft(cfg, gh, facts, on_progress=say)
            else:
                say("Drafting from the file tree, CODEOWNERS, docs, and recent PRs")
                text = onboard.draft_profile(facts, title=cfg.repo.split("/")[1])
            return {"toml": text, "report": self._report(text, cfg, facts)}

        return {"job": self._run_job(f"draft:{repo.lower()}", work)}

    def check(self, body: dict[str, Any]) -> dict[str, Any]:
        repo, text = _repo_name(body.get("repo")), _toml_text(body.get("toml"))
        cfg = self.load().config_for(repo)
        facts = self._gather(cfg, GitHub(cfg.repo, cfg.account), log.info)
        return self._report(text, cfg, facts)

    def _report(self, text: str, cfg: Config, facts: onboard.RepoFacts) -> dict[str, Any]:
        try:
            raw = tomllib.loads(text)
            profile = parse_profile(raw, origin="editor")
        except (tomllib.TOMLDecodeError, ProfileError) as exc:
            return {"valid": False, "error": str(exc).removeprefix("editor: ")}
        if profile.repo.lower() != cfg.repo.lower():
            return {"valid": False, "error": f"repo is {profile.repo!r}; this page is for {cfg.repo}"}
        cov = onboard.measure(profile, facts, raw=raw, min_coverage=cfg.tuning.onboarding.min_coverage)
        profile.authors.internal = [*profile.authors.internal, *cfg.internal_authors]
        profile.authors.external = [*profile.authors.external, *cfg.external_authors]
        labels = {c.id: c.label for c in profile.components}
        return {
            "valid": True,
            "share": round(cov.share, 3),
            "need": cov.need,
            "basis": cov.basis,
            "total": cov.total,
            "busiest": [[labels.get(c, c), n] for c, n in cov.by_component.most_common(12) if c != "other"],
            "unmapped": cov.unmapped_top,
            "problems": cov.problems(),
            "authors": onboard.author_table(profile, facts),
            "layers": [{"id": layer.id, "label": layer.label, "source": layer.source} for layer in profile.layers],
            "components": len(profile.components),
        }

    def start_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        repo, text = _repo_name(body.get("repo")), _toml_text(body.get("toml"))

        def work(say: Callable[[str], None]) -> dict[str, Any]:
            from readthrough.build import build

            cfg = self.load().config_for(repo)
            profile = _parse(text)
            say("Fetching open PRs and laying them out with this profile (no diffs, so it's quick)")
            payload = build(cfg, with_diffs=False, profile=profile, draft=True, write=False)
            preview_id = uuid.uuid4().hex
            with self._lock:
                self._previews[preview_id] = payload
                while len(self._previews) > 5:
                    self._previews.pop(next(iter(self._previews)))
            return {"url": f"/setup/preview/{preview_id}"}

        return {"job": self._run_job(f"preview:{repo.lower()}", work)}

    # ---------------------------------------------------------------- writing

    def save_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        repo, text = _repo_name(body.get("repo")), _toml_text(body.get("toml"))
        profile = _parse(text)
        if profile.repo.lower() != repo.lower():
            raise SetupError(f"The profile's repo is {profile.repo!r}, not {repo}")
        cfg = self._remember(repo, version=body.get("version"))
        cfg.repo_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.local_profile_path
        if path.exists():
            if path.read_text(encoding="utf-8") == text:
                return {"saved": str(path), "unchanged": True}
            path.replace(path.with_name(f"profile.{dt.datetime.now().astimezone():%Y%m%d-%H%M%S}.toml"))
        path.write_text(text, encoding="utf-8")
        return {"saved": str(path)}

    def accept(self, body: dict[str, Any]) -> dict[str, Any]:
        repo = _repo_name(body.get("repo"))
        if body.get("toml"):
            self.save_profile(body)
        settings = self.load()
        cfg = settings.config_for(repo)
        gh = GitHub(cfg.repo, cfg.account)
        sources = profile_sources(cfg, gh, first_only=True)  # a local draft saves fetching the repo's file
        if not sources:
            raise SetupError("Draft or choose a profile first.")
        text = _source_text(sources[0], gh)
        report = self._report(text, cfg, self._gather(cfg, gh, log.info))
        if not report.get("valid"):
            raise SetupError(report.get("error", "The profile doesn't load."))
        if report["problems"] and not body.get("force"):
            raise SetupError("Not accepted yet: " + " ".join(report["problems"]))
        with self._config_lock:
            settings = self.load()
            settings.upsert(cfg.repo, onboarded=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
            if not settings.default_repo and len(settings.repos) == 1:
                settings.default_repo = cfg.repo
            settings.save()
        self.on_change()
        return {"onboarded": True, "url": f"/r/{cfg.repo}/"}

    def save_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        values = body.get("values") or {}
        glob = body.get("global") or {}
        if not isinstance(values, dict) or not isinstance(glob, dict):
            raise SetupError("values and global must be objects")
        with self._config_lock:
            settings = self.load()
            _check_version(settings, body.get("version"))
            if body.get("repo"):
                repo = _repo_name(body["repo"])
                if settings.entry(repo) is None:
                    raise SetupError(f"{repo} isn't configured; onboard it first")
                settings.upsert(repo, **_clean_repo_values(values, repo))
            if "default_repo" in glob:
                default = glob["default_repo"] or None
                if default is not None and settings.entry(_repo_name(default)) is None:
                    raise SetupError(f"{default} isn't configured")
                entry = settings.entry(_repo_name(default)) if default else None
                settings.default_repo = entry["name"] if entry else None
            if "refresh_minutes" in glob:
                settings.refresh_minutes = _int(glob["refresh_minutes"], "refresh_minutes", 1, 1440)
            settings.save()
        self.on_change()
        return {"version": _version(self.load())}

    def remove(self, body: dict[str, Any]) -> dict[str, Any]:
        repo = _repo_name(body.get("repo"))
        with self._config_lock:
            settings = self.load()
            _check_version(settings, body.get("version"))
            cfg = settings.config_for(repo)
            if not settings.remove(cfg.repo):
                raise SetupError(f"{repo} isn't configured")
            settings.save()
        self.on_change()
        purged = False
        if body.get("purge") is True and cfg.repo_dir.exists():
            if cfg.worktrees_dir.exists():
                for wt in cfg.worktrees_dir.glob("pr-*"):
                    if wt.name[3:].isdigit():
                        review.remove_worktree(cfg, int(wt.name[3:]))
            shutil.rmtree(cfg.repo_dir, ignore_errors=True)
            purged = True
        return {"removed": cfg.repo, "purged": purged}

    # ---------------------------------------------------------------- helpers

    def _remember(self, repo: str, *, account: str | None = None, clone: Path | None = None,
                  remote: str | None = None, version: Any = None) -> Config:  # fmt: skip
        """Add the repo to config.toml (or update it), the way `readthrough onboard` does before drafting."""
        with self._config_lock:
            settings = self.load()
            if version is not None:
                _check_version(settings, version)
            values: dict[str, Any] = {}
            if account:
                values["account"] = account
            if clone:
                values["clone"] = clone
                values["remote"] = remote or remote_for(clone, repo)
            if settings.entry(repo) is None or values:
                settings.upsert(repo, **values)
                settings.save()
            return settings.config_for(repo)

    def _gather(self, cfg: Config, gh: Reader, say: Callable[[str], None]) -> onboard.RepoFacts:
        key = (cfg.repo.lower(), cfg.account, str(cfg.clone) if cfg.clone else None)
        with self._lock:
            cached = self._facts.get(key)
        if cached and time.time() - cached[0] < cfg.tuning.onboarding.facts_cache_minutes * 60:
            return cached[1]
        say(f"Reading {cfg.repo}: recent merged PRs, the file tree, CODEOWNERS, and docs")
        facts = onboard.gather(gh, clone=cfg.clone, sample=cfg.tuning.onboarding.merged_sample)
        with self._lock:
            self._facts[key] = (time.time(), facts)
        return facts

    def _run_job(self, key: str, work: Callable[[Callable[[str], None]], dict[str, Any]]) -> str:
        with self._lock:
            for job_id, job in self._jobs.items():
                if job["key"] == key and job["status"] == "running":
                    return job_id
            job_id = uuid.uuid4().hex
            job: dict[str, Any] = {"key": key, "status": "running", "log": [], "result": None, "error": None}
            self._jobs[job_id] = job
            while len(self._jobs) > 20:
                self._jobs.pop(next(iter(self._jobs)))

        def say(message: str) -> None:
            with self._lock:
                job["log"].append(message)

        def target() -> None:
            try:
                result = work(say)
                with self._lock:
                    job.update(status="done", result=result)
            except (SetupError, ConfigError, ProfileError, GhError, RuntimeError, ValueError) as exc:
                with self._lock:
                    job.update(status="error", error=str(exc))
            except Exception as exc:
                log.exception("Setup job %s failed", key)
                with self._lock:
                    job.update(status="error", error=f"Unexpected error: {exc}")

        threading.Thread(target=target, daemon=True, name=f"setup:{key}").start()
        return job_id


# ---------------------------------------------------------------- validation


def _repo_name(value: Any) -> str:
    if not isinstance(value, str) or not REPO_RE.match(value.strip()):
        raise SetupError("Repo must look like owner/name")
    return value.strip()


def _account(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not LOGIN_RE.match(value):
        raise SetupError("That isn't a GitHub login")
    if value not in signed_in_accounts():
        raise SetupError(f"gh isn't signed in as {value}. Run `gh auth login` for it first.")
    return value


def _clone_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or "\0" in value or len(value) > 400:
        raise SetupError("That isn't a usable path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SetupError("Give the clone's full path")
    if not (path / ".git").exists():
        raise SetupError(f"{path} isn't a git clone (no .git)")
    return path.resolve()


def _remote(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[\w.-]{1,60}", value):
        raise SetupError("That isn't a git remote name")
    return value


def _toml_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SetupError("The profile is empty")
    if len(value) > 200_000:
        raise SetupError("The profile is too big")
    return value


def _parse(text: str) -> Any:
    try:
        return parse_profile(tomllib.loads(text), origin="editor")
    except (tomllib.TOMLDecodeError, ProfileError) as exc:
        raise SetupError(str(exc)) from exc


def _int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str) or not str(value).strip().isdigit():
        raise SetupError(f"{name} must be a whole number")
    number = int(value)
    if not low <= number <= high:
        raise SetupError(f"{name} must be between {low} and {high}")
    return number


def _clean_repo_values(values: dict[str, Any], repo: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key in BROWSER_EDITABLE_INTS:
            out[key] = None if value in (None, "") else _int(value, key, *BROWSER_EDITABLE_INTS[key])
        elif key in EDITABLE_LISTS:
            if not isinstance(value, list) or not all(isinstance(v, str) and LOGIN_RE.match(v) for v in value):
                raise SetupError(f"{key} must be a list of GitHub logins")
            out[key] = sorted(set(value), key=str.lower) or None
        elif key == "study_model":
            if value not in (None, "") and not (isinstance(value, str) and MODEL_RE.match(value)):
                raise SetupError("That isn't a model name")
            out[key] = value or None
        elif key == "account":
            out[key] = _account(value)
        elif key == "clone":
            out[key] = _clone_path(value)
        elif key == "remote":
            out[key] = _remote(value)
        else:
            raise SetupError(f"{key} can't be changed from the browser")
    if out.get("clone") and "remote" not in out:
        out["remote"] = remote_for(out["clone"], repo)
    return out


def _version(settings: Settings) -> str:
    try:
        return str(settings.path.stat().st_mtime_ns)
    except FileNotFoundError:
        return "0"


def _check_version(settings: Settings, version: Any) -> None:
    if version is not None and str(version) != _version(settings):
        raise Conflict(
            "config.toml changed since this page loaded it (maybe from the command line). Reload and try again."
        )


def _repo_view(cfg: Config) -> dict[str, Any]:
    built = cfg.data_path.stat().st_mtime if cfg.data_path.exists() else None
    return {
        "name": cfg.repo,
        "onboarded": cfg.onboarded,
        "account": cfg.account,
        "clone": str(cfg.clone) if cfg.clone else "",
        "remote": cfg.remote,
        "session_minutes": cfg.session_minutes,
        "limit": cfg.limit,
        "study_model": cfg.study_model,
        "internal_authors": cfg.internal_authors,
        "external_authors": cfg.external_authors,
        "copilot_args": cfg.copilot_args,
        "profile_override": str(cfg.profile) if cfg.profile else None,
        "built": built,
        "data_dir": str(cfg.repo_dir),
    }


def _source_text(source: Any, gh: FileReader | None) -> str:
    if source.path is not None:
        return source.path.read_text(encoding="utf-8")
    return (gh.raw_or_none(REPO_PROFILE_PATH) if gh else None) or ""
