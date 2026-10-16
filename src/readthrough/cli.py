"""Command-line entry point: ``readthrough [--repo owner/name] <command>``."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import shutil
import subprocess
import sys
import time
import webbrowser
from importlib.resources import files
from pathlib import Path
from typing import Any

from readthrough import __version__, agents, forges, ledger, logs, review, system
from readthrough.analyze import is_newsworthy
from readthrough.config import Config, ConfigError, Settings, guess_clone, load_settings, remote_for
from readthrough.faults import ForgeError
from readthrough.forges.github import signed_in_accounts
from readthrough.process import run
from readthrough.profile import ProfileError

log = logging.getLogger("readthrough")
SKILL_NAME = agents.SKILL_NAME


def _setup_logging(settings: Settings, *, verbose: bool, command: str) -> None:
    """readthrough.log always gets timestamped lines. The console does too for the server; commands print plainly."""
    settings.home.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    logs.setup(
        settings.log_path, settings.tuning.logging, verbose=verbose, console="full" if command == "serve" else "plain"
    )


def _say(text: str = "") -> None:
    if sys.stdout is not None:
        print(text)


def _open(target: str | Path) -> None:
    webbrowser.open(Path(target).resolve().as_uri() if isinstance(target, Path) else target)


def _open_dashboard(cfg: Config) -> None:
    """Prefer the live server when it's running and serving this repo; fall back to the static file."""
    from readthrough.build import mark_seen
    from readthrough.serve import server_alive, server_url

    info = server_alive(cfg.port)
    if info and cfg.repo.lower() in (r.lower() for r in info.get("repos", [])):
        _open(server_url(cfg))  # the server records the visit itself
    else:
        _open(cfg.dashboard_path)
        mark_seen(cfg)


def _head_for(cfg: Config, number: int) -> str:
    if not cfg.data_path.exists():
        return ""
    data = json.loads(cfg.data_path.read_text(encoding="utf-8"))
    return next((p["head"] for p in data["prs"] if p["number"] == number), "")


def _require_onboarded(cfg: Config) -> None:
    if not cfg.onboarded:
        raise ConfigError(f"{cfg.repo} isn't onboarded yet. Run `readthrough onboard {cfg.repo}`.")


def _bundled_skill() -> str:
    return files("readthrough").joinpath("assets/SKILL.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- per-repo commands


def cmd_build(settings: Settings, args: argparse.Namespace) -> int:
    from readthrough.build import BuildFailed, build

    configs = [c for c in settings.all() if c.onboarded] if args.all else [settings.config_for(args.repo)]
    if not configs:
        log.error("No onboarded repos. Onboard one with `readthrough onboard owner/name`.")
        return 1
    failures = 0
    for cfg in configs:
        try:
            _require_onboarded(cfg)
            payload = build(cfg, with_diffs=not args.no_diffs)
        except BuildFailed as exc:  # already logged, with its error id
            _say(f"{cfg.repo}: the build failed [error {exc.error_id}]. Details are in {settings.log_path}.")
            failures += 1
            continue
        except (ProfileError, ConfigError) as exc:
            log.error("%s: %s", cfg.repo, exc)
            failures += 1
            continue
        _summarize(cfg, payload)
        if args.open == "always" or (args.open == "auto" and is_newsworthy(payload["news"])):
            _open_dashboard(cfg)
    return 1 if failures else 0


def _summarize(cfg: Config, payload: dict[str, Any]) -> None:
    lanes: dict[str, int] = {}
    for p in payload["prs"]:
        lanes[p["lane"]] = lanes.get(p["lane"], 0) + 1
    news = payload["news"]
    log.info(
        "%s: %d open PRs | %s", cfg.repo, len(payload["prs"]), " | ".join(f"{k}: {v}" for k, v in sorted(lanes.items()))
    )
    log.info(
        "%d review sessions planned, %d min for the ready lane",
        len(payload["sessions"]),
        sum(s["minutes"] for s in payload["sessions"]),
    )
    if news["since"]:
        log.info(
            "Since you last looked: %d new, %d closed, %d need you, %d changed after your review",
            len(news["opened"]), len(news["closed"]), len(news["needs_you"]), len(news["changed_after_review"]),
        )  # fmt: skip
    if payload.get("drift"):
        d = payload["drift"]
        log.warning("Map drift: only %d%% of open-PR churn maps to a component (unmapped: %s). Run `readthrough onboard %s --update`.",
                    d["coverage"] * 100, ", ".join(p for p, _ in d["unmapped"]), cfg.repo)  # fmt: skip
    log.info("Dashboard: %s", cfg.dashboard_path)


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    from readthrough.serve import base_url, serve, server_alive

    def with_overrides(loaded: Settings) -> Settings:
        """Command-line options win over config.toml, including when the server reloads it."""
        if args.port:
            loaded.port = args.port
        if args.interval:
            loaded.refresh_minutes = args.interval
        return loaded

    settings = with_overrides(settings)
    if server_alive(settings.port):
        log.info("Already running at %s", base_url(settings.port))
        if args.open:
            _open(base_url(settings.port))
        return 0
    if not any(c.onboarded for c in settings.all()):
        log.warning("No onboarded repos yet; the server will pick them up as you onboard them.")
    serve(
        settings, with_diffs=not args.no_diffs, settings_loader=lambda: with_overrides(load_settings(args.home)),
        on_ready=(lambda: _open(base_url(settings.port))) if args.open else None,
    )  # fmt: skip
    return 0


def cmd_open(settings: Settings, args: argparse.Namespace) -> int:
    cfg = settings.config_for(args.repo)
    from readthrough.serve import server_alive

    if not cfg.dashboard_path.exists() and not server_alive(cfg.port):
        log.error("No dashboard for %s yet. Run `readthrough build` or `readthrough serve` first.", cfg.repo)
        return 1
    _open_dashboard(cfg)
    return 0


def cmd_brief(settings: Settings, args: argparse.Namespace) -> int:
    cfg = settings.config_for(args.repo)
    path = cfg.briefs_dir / f"pr-{args.pr}.md"
    if not path.exists():
        log.error("No brief for %s#%d. Run `readthrough build` (is the PR still open?).", cfg.repo, args.pr)
        return 1
    _say(str(path) if args.path else path.read_text(encoding="utf-8"))
    return 0


def cmd_ledger(settings: Settings, args: argparse.Namespace) -> int:
    cfg = settings.config_for(args.repo)
    if args.clear:
        ledger.clear_status(cfg.ledger_path, args.pr)
        _say(f"#{args.pr}: status cleared")
        return 0
    if args.status is None and args.note is None:
        entry = ledger.load(cfg.ledger_path).get(str(args.pr))
        _say(str(entry) if entry else f"No ledger entry for #{args.pr}")
        return 0
    entry = ledger.record(cfg.ledger_path, args.pr, status=args.status, note=args.note, head=_head_for(cfg, args.pr))
    _say(f"#{args.pr}: {entry.get('status', '(no status)')}")
    return 0


def cmd_study(settings: Settings, args: argparse.Namespace) -> int:
    from readthrough import study

    cfg = settings.config_for(args.repo)
    adds: list[tuple[str, Any]] = [
        (k, getattr(args, k)) for k in ("prediction", "teachback", "note") if getattr(args, k)
    ]
    if args.finding:
        severity, _, summary = args.finding.partition(":")
        path, _, line = (args.at or "").partition(":")
        fields: dict[str, Any] = {"severity": severity.strip(), "summary": summary.strip()}
        if path:
            fields["path"] = path
        if line.isdigit():
            fields["line"] = int(line)
        adds.append(("finding", fields))
    for kind, value in adds:
        entry_fields = value if isinstance(value, dict) else {"text": value}
        by = "you" if kind in ("prediction", "teachback") else args.by
        entry = study.append(cfg, args.pr, kind, by=by, **entry_fields)
        _say(f"#{args.pr}: added {kind} #{entry['id']}")
    if adds:
        study.refresh_brief(cfg, args.pr)
        return 0
    appendix = study.render_appendix(study.load(cfg, args.pr))
    _say(appendix.replace(study.MARKER, "").strip() if appendix else f"No study log for #{args.pr} yet.")
    return 0


def cmd_review(settings: Settings, args: argparse.Namespace) -> int:
    cfg = settings.config_for(args.repo)
    entry = ledger.load(cfg.ledger_path).get(str(args.pr))
    launch = review.plan_review(cfg, args.pr, ledger_entry=entry, fresh=args.fresh)
    if args.fresh:
        ledger.record(cfg.ledger_path, args.pr, session=launch.session_id)
    if args.vscode:
        prompt = review.review_prompt(cfg, args.pr, cfg.briefs_dir / f"pr-{args.pr}.md")
        existing = review.agent_for(cfg, launch.session_id).editor_url(launch.session_id) if launch.resuming else None
        _open(existing or review.vscode_new_session_url(prompt, cfg.clone))
        return 0
    verb = "Resuming" if launch.resuming else "Starting"
    log.info("%s %s session %s for %s#%d in %s", verb, launch.agent, launch.session_id, cfg.repo, args.pr, launch.cwd)
    if args.print:
        _say(subprocess.list2cmdline(launch.argv) if sys.platform == "win32" else " ".join(launch.argv))
        return 0
    if args.here:
        return subprocess.call(launch.argv, cwd=launch.cwd)
    terminal = review.launch_in_terminal(launch, cfg)
    log.info("Opened it in %s", terminal)
    return 0


def cmd_worktree(settings: Settings, args: argparse.Namespace) -> int:
    cfg = settings.config_for(args.repo)
    if args.remove:
        _say("removed" if review.remove_worktree(cfg, args.pr) else "no worktree")
        return 0
    path, merge_base, head = review.ensure_worktree(cfg, args.pr, base=args.base)
    _say(
        f'worktree   {path}\nhead       {head}\nmerge-base {merge_base}\ndiff       git -C "{path}" diff {merge_base}..HEAD'
    )
    return 0


def cmd_handle_url(settings: Settings, args: argparse.Namespace) -> int:
    """Entry point for the readthrough:// protocol. Runs windowless, so errors go to a message box."""
    log.info("URL: %s", args.url)
    try:
        target = review.parse_readthrough_url(args.url, allowed_repos=[c.slug for c in settings.all() if c.onboarded])
        if target.repo is None:
            from readthrough.serve import base_url, server_alive

            if server_alive(settings.port):
                _open(base_url(settings.port))
                return 0
            return cmd_open(settings, argparse.Namespace(repo=None))
        ns = argparse.Namespace(repo=target.repo, pr=target.number, fresh=False, vscode=False, print=False, here=False,
                                all=False, no_diffs=False, open="always")  # fmt: skip
        if target.action == "review":
            return cmd_review(settings, ns)
        if target.action == "refresh":
            return cmd_build(settings, ns)
        return cmd_open(settings, ns)
    except Exception as exc:
        log.exception("URL handler failed")
        system.message_box("readthrough", f"{exc}\n\nDetails: {settings.log_path}")
        return 1


# ---------------------------------------------------------------- onboarding and repos


def cmd_onboard(settings: Settings, args: argparse.Namespace) -> int:
    from readthrough import onboard
    from readthrough.profile import profile_sources

    repo = args.target or args.repo or settings.default_repo
    if not repo:
        log.error("Which repo? readthrough onboard owner/name (or --forge gitlab group/project, and so on)")
        return 1
    existing = settings.entry(repo, args.forge) or {}
    forge_name = existing.get("forge") or args.forge
    repo = existing.get("name", repo)
    clone = (
        Path(args.clone).expanduser()
        if args.clone
        else (Path(existing["clone"]) if existing.get("clone") else guess_clone(repo))
    )
    account = args.account or existing.get("account")
    cfg = settings.config_for(repo, forge=forge_name)
    cfg.account = account
    forge = forges.make(cfg)
    try:
        info = forge.repo_info()
    except ForgeError as exc:
        log.error("Can't read %s on %s%s: %s", repo, forge.label, f" as {account}" if account else "", exc.reason)
        accounts = signed_in_accounts() if "accounts" in forge.capabilities else []
        if accounts:
            log.error("Signed-in gh accounts: %s. Pick one with --account.", ", ".join(accounts))
        return 1
    full_name = info.get("full_name") or repo
    if full_name.lower() != repo.lower() and existing:
        log.error("%s now lives at %s. Remove it and onboard the new name.", repo, full_name)
        return 1
    repo = full_name
    remote = args.remote or existing.get("remote") or (remote_for(clone, repo) if clone else None)
    settings.upsert(repo, forge=forge.name, clone=clone, account=account, remote=remote)
    cfg = settings.config_for(repo, forge=forge.name)
    forge = forges.make(cfg)
    if clone is None:
        log.info("No local clone found (looked in ~/src and ~/repos). Reviews need one; set it with --clone.")

    log.info("Looking at %s: recent merged %ss, the file tree, CODEOWNERS, and docs...", repo, forge.pr_noun)
    facts = onboard.gather(forge, clone=clone, sample=cfg.tuning.onboarding.merged_sample)
    sources = profile_sources(cfg, forge)

    if args.copilot:
        from readthrough.onboard_copilot import copilot_draft

        _write_profile(cfg, copilot_draft(cfg, forge, facts, on_progress=lambda m: log.info("  %s", m)))
        sources = profile_sources(cfg, forge)
    elif args.update or args.draft or not sources:
        if cfg.local_profile_path.exists() and not (args.update or args.force):
            log.info("Keeping your draft at %s (pass --update to redraft it).", cfg.local_profile_path)
        else:
            _write_profile(cfg, onboard.draft_profile(facts, title=repo.rsplit("/", 1)[-1]))
            sources = profile_sources(cfg, forge)
    source = sources[0]
    log.info("Profile: %s", source.label)

    try:
        profile = source.load()
    except ProfileError as exc:
        log.error("The profile doesn't load: %s", exc)
        settings.save()
        return 1
    cov = onboard.measure(profile, facts, raw=_raw_profile(source), min_coverage=cfg.tuning.onboarding.min_coverage)
    _say(onboard.report(cov, profile, onboard.author_table(profile, facts)))

    if args.preview:
        from readthrough.build import build

        payload = build(cfg, with_diffs=not args.no_diffs, profile=profile, draft=not cfg.onboarded)
        _summarize(cfg, payload)
        _open(cfg.dashboard_path)

    if args.accept:
        if cov.problems() and not args.force:
            log.error("Not accepted. Fix the items above, or pass --force to accept anyway.")
            settings.save()
            return 1
        settings.upsert(cfg.slug, onboarded=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
        if not settings.default_repo and len(settings.repos) == 1:
            settings.default_repo = repo
        settings.save()
        name = settings.display_name(cfg.slug)
        _say(
            f"\n{repo} is onboarded. Build it with `readthrough --repo {name} build --open`; a running server picks it up on its own."
        )
        return 0
    settings.save()
    if not cfg.onboarded:
        _say(f"\nNext: edit {source.label}, re-check with `readthrough onboard {repo}`,"
             f" look at it with `--preview`, and finish with `--accept`.")  # fmt: skip
    return 0


def _write_profile(cfg: Config, text: str) -> None:
    cfg.repo_dir.mkdir(parents=True, exist_ok=True)
    if cfg.local_profile_path.exists():
        backup = cfg.local_profile_path.with_name(f"profile.{dt.datetime.now().astimezone():%Y%m%d-%H%M%S}.toml")
        cfg.local_profile_path.replace(backup)
        log.info("Saved the previous profile as %s", backup.name)
    cfg.local_profile_path.write_text(text, encoding="utf-8")
    log.info("Wrote a draft profile to %s", cfg.local_profile_path)


def _raw_profile(source: Any) -> dict[str, Any] | None:
    """The raw TOML of a profile you're editing, so onboarding can see what you haven't filled in yet."""
    import tomllib

    if source.kind in ("local", "explicit") and source.path is not None:
        return tomllib.loads(source.path.read_text(encoding="utf-8"))
    return None  # shared and in-repo profiles are someone's considered map


def cmd_repos(settings: Settings, args: argparse.Namespace) -> int:
    from readthrough.profile import profile_sources

    if not settings.repos:
        _say("No repos yet. Onboard one with `readthrough onboard owner/name`.")
        return 0
    default = settings.entry(settings.default_repo) if settings.default_repo else None
    for name, cfg in zip(settings.names(), settings.all(), strict=True):
        sources = profile_sources(cfg)
        built = (
            dt.datetime.fromtimestamp(cfg.data_path.stat().st_mtime, tz=dt.UTC).astimezone().strftime("%Y-%m-%d %H:%M")
            if cfg.data_path.exists()
            else "never"
        )
        star = "*" if default is not None and default is settings.entry(cfg.slug) else " "
        _say(f"{star} {name:32} {'onboarded' if cfg.onboarded else 'NOT ONBOARDED':13}  built {built:16}  "
             f"profile: {sources[0].label if sources else 'in-repo file or none'}"
             + (f"  (as {cfg.account})" if cfg.account else ""))  # fmt: skip
    return 0


def cmd_default(settings: Settings, args: argparse.Namespace) -> int:
    cfg = settings.config_for(args.target)
    if settings.entry(cfg.slug) is None:
        log.error("%s isn't configured", args.target)
        return 1
    settings.default_repo = settings.display_name(cfg.slug)
    settings.save()
    _say(f"Default repo: {settings.default_repo}")
    return 0


def cmd_remove(settings: Settings, args: argparse.Namespace) -> int:
    cfg = settings.config_for(args.target)
    if not settings.remove(cfg.slug):
        log.error("%s isn't configured", args.target)
        return 1
    settings.save()
    if args.purge and cfg.repo_dir.exists():
        if cfg.worktrees_dir.exists():
            for wt in cfg.worktrees_dir.glob("pr-*"):
                if wt.name[3:].isdigit():
                    review.remove_worktree(cfg, int(wt.name[3:]))
        shutil.rmtree(cfg.repo_dir, ignore_errors=True)
        _say(f"Removed {cfg.repo} and deleted {cfg.repo_dir}")
    else:
        _say(f"Removed {cfg.repo} from the config. Its data is still in {cfg.repo_dir} (--purge deletes it).")
    return 0


def cmd_profile(settings: Settings, args: argparse.Namespace) -> int:
    from readthrough.profile import load_profile, profile_sources

    if args.action == "check" and args.file:
        try:
            p = load_profile(Path(args.file))
        except ProfileError as exc:
            log.error("%s", exc)
            return 1
        _say(f"{args.file}: a valid profile for {p.repo} ({len(p.layers)} layers, {len(p.components)} components)")
        return 0
    cfg = settings.config_for(args.repo)
    forge = forges.make(cfg)
    sources = profile_sources(cfg, forge)
    if not sources:
        log.error("No profile for %s. Run `readthrough onboard %s`.", cfg.repo, cfg.repo)
        return 1
    if args.action == "sources":
        for i, s in enumerate(sources):
            _say(f"{'->' if i == 0 else '  '} {s.kind:9} {s.label}")
        return 0
    source = sources[0]
    if args.action == "path":
        _say(source.label)
        return 0
    if args.action == "export":
        _say(source.read())
        return 0
    profile = source.load()
    if args.action == "check":
        from readthrough import onboard

        facts = onboard.gather(forge, clone=cfg.clone, sample=cfg.tuning.onboarding.merged_sample)
        cov = onboard.measure(profile, facts, raw=_raw_profile(source), min_coverage=cfg.tuning.onboarding.min_coverage)
        _say(onboard.report(cov, profile, onboard.author_table(profile, facts)))
        return 1 if cov.problems() else 0
    _say(f"{profile.title} ({profile.repo}), from {profile.origin}")
    _say(f"Layers: {', '.join(layer.label for layer in profile.layers)}")
    _say(f"Components: {', '.join(c.label for c in profile.components)}")
    if profile.contracts_source:
        _say(f"Contracts: {profile.contracts_source}")
    if profile.review_guidance_file:
        _say(f"Review guidance file: {profile.review_guidance_file}")
    if profile.review_guidance:
        _say("Review guidance:\n" + profile.review_guidance.strip())
    if profile.test_cmd:
        _say("Test command: " + profile.test_cmd.replace("{clone}", str(cfg.clone or "<clone>")))
    return 0


# ---------------------------------------------------------------- machine setup


def cmd_install(settings: Settings, args: argparse.Namespace) -> int:
    home = settings.home if args.home_set else None
    if args.what == "skill":
        for dest in _skill_dirs(settings, args):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text(_bundled_skill(), encoding="utf-8")
            _say(f"Installed the {SKILL_NAME} skill to {dest}")
    elif args.what == "protocol":
        _say(f"Registered {review.URL_SCHEME}:// -> {system.install_protocol()}")
        _say("Rebuild the dashboards (`readthrough build --all`) so their buttons switch to one-click launch.")
    elif args.what == "schedule":
        command = system.install_schedule(at=args.at, days=args.days, open_mode=args.open, home=home)
        _say(f"Scheduled '{system.TASK_NAME}' ({args.days} at {args.at}): {command}")
        _say(f"Next run: {system.schedule_status().next_run or 'unknown'}")
    elif args.what == "server":
        from readthrough.serve import base_url

        command = system.install_server_task(home=home)
        _say(f"'{system.SERVER_TASK_NAME}' starts at logon and is starting now: {command}")
        _say(f"Dashboard: {base_url(settings.port)}")
    return 0


def _skill_dirs(settings: Settings, args: argparse.Namespace) -> list[Path]:
    """Where to put the skill: an explicit directory, one agent's, or every installed agent's (else the default's)."""
    if args.dest:
        return [Path(args.dest)]
    if args.agent:
        return [agents.make(args.agent, settings.agent_options).skill_dir]
    available = [a for a in agents.all_agents(settings.agent_options) if a.available()]
    return [a.skill_dir for a in available or [agents.resolve(settings.agent, settings.agent_options)]]


def cmd_uninstall(settings: Settings, args: argparse.Namespace) -> int:
    if args.what == "skill":
        removed = False
        for dest in _skill_dirs(settings, args):
            removed = dest.exists() or removed
            shutil.rmtree(dest, ignore_errors=True)
    elif args.what == "protocol":
        removed = system.uninstall_protocol()
    elif args.what == "server":
        removed = system.uninstall_schedule(system.SERVER_TASK_NAME)
    else:
        removed = system.uninstall_schedule()
    _say("removed" if removed else "nothing to remove")
    return 0


def cmd_config(settings: Settings, args: argparse.Namespace) -> int:
    from readthrough.tuning import changed_from_defaults, defaults_text

    if args.defaults:
        _say(defaults_text())
        return 0
    if args.check:
        # Loading already validated everything; reaching here means the file is fine.
        _say(f"{settings.path}: valid ({len(settings.repos)} repos)")
        return 0
    if args.edit:
        if not settings.path.exists():
            settings.save()
        _open(settings.path)
        return 0
    _say(f"{'config file':16} {settings.path} ({'exists' if settings.path.exists() else 'not created yet'})")
    _say(f"{'default repo':16} {settings.default_repo or '-'}")
    _say(f"{'port':16} {settings.port}")
    _say(f"{'refresh minutes':16} {settings.refresh_minutes}")
    changed = changed_from_defaults(settings.tuning)
    _say(f"{'tuning':16} " + ("; ".join(f"{sec}: {', '.join(f'{k}={v}' for k, v in vals.items())}" for sec, vals in changed.items())
                              if changed else "all defaults (see `readthrough config --defaults`)"))  # fmt: skip
    configs = [settings.config_for(args.repo)] if args.repo else settings.all()
    for cfg in configs:
        _say(f"\n[{cfg.repo}]")
        for key in ("clone", "remote", "account", "profile", "onboarded", "session_minutes", "limit",
                    "internal_authors", "external_authors", "study_model", "agent_args"):  # fmt: skip
            _say(f"  {key:16} {getattr(cfg, key)}")
        repo_changed = {k: v for k, v in changed_from_defaults(cfg.tuning).items() if changed.get(k) != v}
        if repo_changed:
            _say(
                f"  {'tuning':16} "
                + "; ".join(
                    f"{sec}: {', '.join(f'{k}={v}' for k, v in vals.items())}" for sec, vals in repo_changed.items()
                )
            )
        _say(f"  {'data':16} {cfg.repo_dir}")
    return 0


def cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    if args.bundle:
        return _doctor_bundle(settings, args)
    problems = 0

    def check(ok: bool | None, label: str, detail: str, fix: str = "") -> None:
        """ok=None marks something optional."""
        nonlocal problems
        problems += ok is False
        mark = {True: "ok ", False: "XX ", None: "-- "}[ok]
        hint = ("\n    fix: " if ok is False else "\n    optional: ") + fix if fix and not ok else ""
        _say(f"{mark} {label:14} {detail}{hint}")

    _say("This machine")
    accounts = signed_in_accounts()
    check(bool(accounts), "gh", f"signed in as {', '.join(accounts)}" if accounts else "not signed in", "gh auth login")
    agent = agents.resolve(settings.agent, settings.agent_options)
    check(agent.available(), "agent", agents.describe(settings.agent, settings.agent_options), agent.install_hint)
    for other in agents.all_agents(settings.agent_options):
        if other.available() or other.name == agent.name:
            skill = other.skill_dir / "SKILL.md"
            current = skill.exists() and skill.read_text(encoding="utf-8") == _bundled_skill()
            check(current if other.available() else None, f"skill ({other.name})",
                  str(skill) if skill.exists() else "not installed", f"readthrough install skill --agent {other.name}")  # fmt: skip
    registered = system.registered_protocol_command()
    check(
        registered == system.handler_command(),
        "protocol",
        registered or "not registered",
        "readthrough install protocol",
    )
    sched = system.schedule_status()
    check(sched.installed, "schedule",
          f"{sched.state}, next {sched.next_run}, last result {sched.last_result}" if sched.installed else "not installed",
          "readthrough install schedule")  # fmt: skip
    from readthrough import assistants, terminals
    from readthrough.serve import base_url, server_alive

    live = server_alive(settings.port)
    health = (live or {}).get("status") or {}
    server_task = system.schedule_status(system.SERVER_TASK_NAME)
    detail = f"running at {base_url(settings.port)}" if live else "not running"
    check(
        True if live else None,
        "server",
        detail + (" (starts at logon)" if server_task.installed else ""),
        "readthrough install server",
    )
    assistant = assistants.resolve(settings.assistant)
    chosen = ", chosen automatically" if settings.assistant == assistants.AUTO else ""
    if not assistant.available:
        check(
            False, "assistant", f"{assistant.name}: {assistant.reason}", 'set assistant = "rules", or install [study]'
        )
    elif assistant.name == "rules" and settings.assistant == assistants.AUTO:
        check(None, "assistant", f"rules{chosen}", "install the [study] extra for Copilot, which reads the code")
    else:
        check(True, "assistant", f"{assistant.name}{chosen}")
    terminal = terminals.describe(settings.terminal)
    check(not terminal.startswith("No terminal"), "terminal", terminal, "set terminal = ... in config.toml")

    configs = [settings.config_for(args.repo)] if args.repo else settings.all()
    if not configs:
        _say("\nNo repos yet. Onboard one with `readthrough onboard owner/name`.")
    for cfg in configs:
        _doctor_repo(cfg, check, accounts, health.get(cfg.slug))
    return 1 if problems else 0


def _doctor_repo(cfg: Config, check: Any, accounts: list[str], live: dict[str, Any] | None = None) -> None:
    from readthrough.profile import profile_sources

    forge = forges.make(cfg)
    _say(f"\n{cfg.repo}" + (f" ({forge.label})" if forge.name != forges.DEFAULT else ""))
    if cfg.account:
        check(cfg.account in accounts, "account", cfg.account, f"gh auth login (as {cfg.account})")
    check(True, "auth", forge.describe_auth())
    try:
        forge.repo_info()
        check(True, "access", "can read the repo")
    except ForgeError as exc:
        fix = (
            "pick a gh account that can read it: readthrough onboard REPO --account LOGIN"
            if "accounts" in forge.capabilities
            else f"give readthrough a token: {forge.describe_auth()}"
        )
        check(False, "access", exc.reason[:120], fix)
    clone_ok = bool(cfg.clone and (cfg.clone / ".git").exists())
    check(
        clone_ok if cfg.clone else None,
        "clone",
        str(cfg.clone or "not set"),
        f"readthrough onboard {cfg.repo} --clone PATH",
    )
    if clone_ok and cfg.clone is not None:
        remotes = run(["git", "-C", str(cfg.clone), "remote"], check=False).split()
        remote = cfg.remote or remote_for(cfg.clone, cfg.repo)
        check(
            remote in remotes,
            "remote",
            f"{remote} in {remotes}",
            f"git -C {cfg.clone} remote add {remote} {forge.web_url()}",
        )
    sources = profile_sources(cfg)
    check(
        bool(sources) or None,
        "profile",
        sources[0].label if sources else f"none local (an in-repo {forge.config_dir}/readthrough.toml may apply)",
    )
    check(bool(cfg.onboarded), "onboarded", cfg.onboarded or "no", f"readthrough onboard {cfg.repo}")
    if cfg.data_path.exists():
        age = time.time() - cfg.data_path.stat().st_mtime
        limit_h = cfg.tuning.freshness.doctor_build_hours
        check(age < limit_h * 3600, "last build", f"{age / 3600:.1f} h ago", f"readthrough --repo {cfg.repo} build")
        data = json.loads(cfg.data_path.read_text(encoding="utf-8"))
        for w in data.get("warnings") or []:
            check(None, "warning", w["message"])
        drift = data.get("drift")
        if drift:
            check(
                False,
                "map",
                f"only {drift['coverage']:.0%} of open-PR churn maps to a component",
                f"readthrough onboard {cfg.repo} --update",
            )
    elif cfg.onboarded:
        check(False, "last build", "never", f"readthrough --repo {cfg.repo} build --open")
    if live and live.get("error"):
        err = live["error"]
        check(False, "server", f"{live['consecutive_failures']} failed refresh(es) in a row; last at {err['at']} "
              f"during '{err['step']}' [error {err['id']}]: {err['message']}", f"look for error {err['id']} in readthrough.log")  # fmt: skip


def _doctor_bundle(settings: Settings, args: argparse.Namespace) -> int:
    """Everything needed to debug a problem, in one zip: config, recent logs, versions, and live health."""
    import platform
    import zipfile

    from readthrough.serve import server_alive

    out = settings.home / "bundles" / f"readthrough-debug-{dt.datetime.now().astimezone():%Y%m%d-%H%M%S}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    def version(cmd: list[str]) -> str:
        try:
            return run(cmd, timeout=20).strip().splitlines()[0]
        except (ForgeError, IndexError) as exc:
            return f"unavailable ({exc})"

    # The checkout this runs from, when it's an editable install; a wheel install just reports "unavailable".
    sha = version(["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "--short", "HEAD"])
    info = {
        "readthrough": __version__, "git_commit": sha, "python": sys.version, "platform": platform.platform(),
        "gh": version(["gh", "--version"]), "copilot": version(["copilot", "--version"]), "claude": version(["claude", "--version"]),
        "gh_accounts": signed_in_accounts(), "home": str(settings.home), "config": str(settings.path),
        "repos": [{"name": c.slug, "onboarded": c.onboarded, "built": cfg_built(c)} for c in settings.all()],
    }  # fmt: skip
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("versions.json", json.dumps(info, indent=2))
        if settings.path.exists():
            z.write(settings.path, "config.toml")  # holds no secrets: tokens stay in gh's own store
        for log_file in sorted(settings.home.glob("readthrough.log*")):
            z.write(log_file, log_file.name)
        z.writestr("health.json", json.dumps(server_alive(settings.port) or {"server": "not running"}, indent=2))
        for cfg in settings.all():
            if cfg.data_path.exists():
                data = json.loads(cfg.data_path.read_text(encoding="utf-8"))
                summary = {k: data.get(k) for k in ("schema", "version", "generated", "warnings", "stats", "drift")}
                z.writestr(f"repos/{cfg.slug.replace('/', '__')}.json", json.dumps(summary, indent=2))
    _say(
        f"Wrote {out}\nIt has your config, recent logs, versions, and server health; no tokens. Attach it to a bug report."
    )
    return 0


def cfg_built(cfg: Config) -> str | None:
    if not cfg.data_path.exists():
        return None
    return (
        dt.datetime.fromtimestamp(cfg.data_path.stat().st_mtime, tz=dt.UTC).astimezone().isoformat(timespec="seconds")
    )


# ---------------------------------------------------------------- parser


def build_parser(prog: str = "readthrough") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Lay out a repo's open PRs and study them in depth.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--home", type=Path, help="data directory (default ~/.readthrough)")
    parser.add_argument("--repo", metavar="OWNER/NAME",
                        help="which repo (default: the default repo, or the only one); FORGE/OWNER/NAME when a name is on several forges")  # fmt: skip
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("onboard", help="map a repo's architecture so readthrough can lay out its PRs")
    p.add_argument("target", nargs="?", metavar="OWNER/NAME")
    p.add_argument("--forge", choices=forges.names(), default=None,
                   help=f"where the repo lives (default: {forges.DEFAULT}; a FORGE/OWNER/NAME target says it too)")  # fmt: skip
    p.add_argument("--clone", help="your local clone (default: looks in ~/src and ~/repos)")
    p.add_argument("--remote", help="git remote that points at the repo (default: detected)")
    p.add_argument("--account", help="gh account to read the repo as (for repos only one of your accounts can see)")
    p.add_argument("--draft", action="store_true", help="draft a profile even if a shared or in-repo one exists")
    p.add_argument("--update", action="store_true", help="redraft your local profile (keeps a backup)")
    p.add_argument("--copilot", action="store_true", help="have Copilot draft the profile by reading the repo")
    p.add_argument("--preview", action="store_true", help="build the dashboard with this profile and open it")
    p.add_argument("--accept", action="store_true", help="finish onboarding once the checks pass")
    p.add_argument("--force", action="store_true", help="accept despite problems; or replace an existing draft")
    p.add_argument("--no-diffs", action="store_true", help="--preview without diffs (faster)")
    p.set_defaults(func=cmd_onboard)

    sub.add_parser("repos", help="list configured repos").set_defaults(func=cmd_repos)
    p = sub.add_parser("default", help="set the repo commands use when you leave out --repo")
    p.add_argument("target", metavar="OWNER/NAME")
    p.set_defaults(func=cmd_default)
    p = sub.add_parser("remove", help="stop tracking a repo")
    p.add_argument("target", metavar="OWNER/NAME")
    p.add_argument("--purge", action="store_true", help="also delete its data, worktrees, and study logs")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("profile", help="show, check, or export the repo's architecture profile")
    p.add_argument("action", nargs="?", default="show", choices=("show", "check", "sources", "path", "export"))
    p.add_argument("--file", help="check: validate this TOML file instead of the repo's profile")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("build", help="fetch PRs and regenerate the dashboard and briefs")
    p.add_argument("--all", action="store_true", help="every onboarded repo")
    p.add_argument("--no-diffs", action="store_true", help="skip diffs (faster, no line heuristics)")
    p.add_argument("--open", nargs="?", const="always", default="never", choices=("always", "auto", "never"),
                   help="open the dashboard afterwards; 'auto' opens only when something changed that needs you")  # fmt: skip
    p.set_defaults(func=cmd_build)

    sub.add_parser("open", help="open the dashboard (the live one if the server is running)").set_defaults(
        func=cmd_open
    )

    p = sub.add_parser("serve", help="run the live dashboard for every onboarded repo on localhost")
    p.add_argument("--port", type=int, help="port (default from config, 8765)")
    p.add_argument("--interval", type=int, help="minutes between refreshes (default from config, 10)")
    p.add_argument("--no-diffs", action="store_true", help="skip diffs (faster, no line heuristics)")
    p.add_argument("--open", action="store_true", help="open the dashboard once it's up")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("brief", help="print a PR's study brief")
    p.add_argument("pr", type=int)
    p.add_argument("--path", action="store_true", help="print only the file path")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("ledger", help="record (or show) what you did with a PR")
    p.add_argument("pr", type=int)
    p.add_argument("--status", choices=ledger.STATUSES)
    p.add_argument("--note")
    p.add_argument("--clear", action="store_true", help="forget the status (keeps the note and session)")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("study", help="show or add to a PR's study log (evidence of understanding)")
    p.add_argument("pr", type=int)
    p.add_argument("--prediction", help="what you expect the PR to do, written before reading it")
    p.add_argument("--teachback", help="your summary of the whole PR, as if briefing a teammate")
    p.add_argument("--note", help="a short insight to keep with the brief")
    p.add_argument("--finding", metavar="SEVERITY:SUMMARY", help="e.g. 'major: retries swallow the original error'")
    p.add_argument("--at", metavar="PATH:LINE", help="location for --finding")
    p.add_argument("--by", choices=("you", "copilot"), default="you", help="who wrote a --note or --finding")
    p.set_defaults(func=cmd_study)

    p = sub.add_parser("review", help="start or resume the Copilot deep-review session for a PR")
    p.add_argument("pr", type=int)
    p.add_argument("--fresh", action="store_true", help="start a new session instead of resuming")
    p.add_argument("--here", action="store_true", help="run in this terminal instead of opening a new one")
    p.add_argument("--vscode", action="store_true", help="open in VS Code's Agents window instead of the CLI")
    p.add_argument("--print", action="store_true", help="print the command instead of running it")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("worktree", help="check out a PR in its own git worktree")
    p.add_argument("pr", type=int)
    p.add_argument("--base", default="main")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_worktree)

    p = sub.add_parser("handle-url", help=argparse.SUPPRESS)
    p.add_argument("url")
    p.set_defaults(func=cmd_handle_url)

    for name, func in (("install", cmd_install), ("uninstall", cmd_uninstall)):
        p = sub.add_parser(
            name,
            help=f"{name} the review skill, the readthrough:// link handler, the daily refresh, or the live server",
        )
        p.add_argument("what", choices=("skill", "protocol", "schedule", "server"))
        p.add_argument("--agent", choices=agents.AGENTS.names(),
                       help="skill: only this agent's skill directory (default: every installed agent's)")  # fmt: skip
        p.add_argument("--dest", help="skill: an explicit directory instead")
        if name == "install":
            p.add_argument("--at", default="08:30", help="schedule: time of day, HH:MM (default 08:30)")
            p.add_argument("--days", choices=("daily", "weekdays"), default="weekdays")
            p.add_argument("--open", choices=("always", "auto", "never"), default="auto",
                           help="schedule: when to open the dashboard (default auto)")  # fmt: skip
        p.set_defaults(func=func)

    p = sub.add_parser("config", help="show the effective configuration")
    p.add_argument("--edit", action="store_true", help="open config.toml")
    p.add_argument("--defaults", action="store_true", help="print every tunable setting with its default and meaning")
    p.add_argument("--check", action="store_true", help="validate config.toml and exit")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", help="check that everything is set up")
    p.add_argument("--bundle", action="store_true", help="write a zip of config, logs, and versions for a bug report")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None, *, prog: str = "readthrough") -> None:
    args = build_parser(prog).parse_args(argv)
    args.home_set = args.home is not None
    try:
        settings = load_settings(args.home)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    _setup_logging(settings, verbose=args.verbose, command=args.command)
    try:
        code = args.func(settings, args)
    except (ForgeError, ProfileError, RuntimeError, ValueError, NotImplementedError) as exc:
        log.error("%s", exc)
        code = 1
    except Exception as exc:
        error_id = logs.new_id()
        log.exception("Unexpected error [error %s]", error_id, extra={"error_id": error_id})
        print(f"readthrough hit an unexpected error ({type(exc).__name__}) [error {error_id}]. "
              f"`readthrough doctor --bundle` makes a zip to attach to a bug report.", file=sys.stderr)  # fmt: skip
        code = 2
    sys.exit(code)
