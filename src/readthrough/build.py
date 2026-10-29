"""The build pipeline: fetch, analyze, write briefs, data.json, and the dashboard."""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from readthrough import __version__, forges, ledger, logs, review, study
from readthrough.analyze import analyze, compute_overlaps, count_lines, plan_sessions, rule_descriptions, whats_new
from readthrough.brief import build_brief
from readthrough.config import Config
from readthrough.faults import ForgeError, TooLarge
from readthrough.files import write_atomic
from readthrough.forges import Forge
from readthrough.logs import pool_map
from readthrough.membership import internal_members
from readthrough.model import Capability, ChangedFile, PullRequest, known, unavailable
from readthrough.profile import Profile, resolve_profile
from readthrough.rules import Contract, Instruction, contracts_for, parse_instruction
from readthrough.system import registered_protocol_command
from readthrough.tuning import Tuning

# data.json's layout. Bump it when a field changes meaning or is removed (adding fields doesn't need it),
# so a newer data file is never misread by an older dashboard or server.
DATA_SCHEMA = 2
log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Everything fetched from the forge for one build. Analysis is a pure function of this and the profile."""

    viewer: str
    prs: list[PullRequest]
    contracts: dict[str, Contract]
    instructions: list[Instruction]
    required_checks: dict[str, Capability[set[str]]]
    main_failing: Capability[set[str]]
    diffs: dict[int, str]
    now: dt.datetime
    members: set[str] = field(default_factory=set)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    diff_problems: dict[int, str] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)


class BuildFailed(RuntimeError):
    """A build that couldn't finish. Says which step failed, and carries an error id shown in the dashboard."""

    def __init__(self, repo: str, step: str, cause: BaseException, error_id: str, seconds: float) -> None:
        reason = cause.args[0] if isinstance(cause, ForgeError) and cause.args else f"{type(cause).__name__}: {cause}"
        super().__init__(f"{step}: {reason}")
        self.repo, self.step, self.cause, self.error_id, self.seconds = repo, step, cause, error_id, seconds
        self.reason = reason
        self.expected = isinstance(cause, ForgeError)


class Warnings(list):
    """What a build had to do without. Each is logged once and shown in the dashboard."""

    def add(self, code: str, message: str, details: list[str] | None = None) -> None:
        details = details or []
        log.warning(
            "%s%s", message, f" ({'; '.join(details[:5])}{' …' if len(details) > 5 else ''})" if details else ""
        )
        self.append({"code": code, "message": message, "details": details[:20]})


def fetch_snapshot(
    cfg: Config,
    profile: Profile,
    *,
    with_diffs: bool = True,
    forge: Forge | None = None,
    steps: logs.Steps | None = None,
) -> Snapshot:
    """Fetch everything for a build. Only the PR list is essential; other inputs degrade to a warning."""
    forge = forge or forges.make(cfg)
    steps = steps or logs.Steps(log)
    warnings = Warnings()
    for note in profile.notes:
        warnings.add("profile", note)
    now = dt.datetime.now(dt.UTC)
    with steps.step("fetching PRs"):
        log.info("Fetching open %ss from %s%s", forge.pr_noun, forge.label, f" as {cfg.account}" if cfg.account else "")
        viewer, prs, total = forge.fetch_prs(cfg.limit)
    if total > len(prs):
        warnings.add(
            "limit",
            f"Showing the {len(prs)} most recently active of {total} open {forge.pr_noun}s. Raise `limit` for this repo to see more.",
        )
    capped = [
        f"#{p.number} {', '.join(f'{k} {have} of {of}' for k, (have, of) in p.truncated.items())}"
        for p in prs
        if p.truncated
    ]
    if capped:
        warnings.add(
            "truncated",
            f"{len(capped)} {forge.pr_noun}s are too big to fetch completely, so their map placement and checks are partial",
            capped,
        )

    with steps.step("fetching CI context"):
        try:
            context = forge.ci_context({p.base for p in prs})
            problems = sorted({c.reason for c in (*context.required.values(), context.main_failing) if not c.known})
        except ForgeError as exc:
            context = forges.CiContext({}, unavailable(exc.reason), "main")
            problems = [exc.reason]
        if problems:
            warnings.add(
                "ci",
                "Couldn't read required checks or the default branch's CI, so CI states are less precise",
                problems,
            )
    with steps.step("fetching rules"):
        log.info("Fetching architecture contracts and instruction files from %s", context.default_branch)
        contracts, instructions, problems = fetch_rules(forge, profile, branch=context.default_branch)
        if problems:
            warnings.add(
                "rules",
                f"Couldn't fetch {len(problems)} rule or contract files; the rules shown may be incomplete",
                problems,
            )
    diffs: dict[int, str] = {}
    diff_problems: dict[int, str] = {}
    fetched = 0
    if with_diffs and "diffs" not in forge.capabilities:
        warnings.add("diffs", f"{forge.label} has no API for whole-change diffs, so line heuristics never run here")
        diff_problems = dict.fromkeys((p.number for p in prs), "unsupported")
    elif with_diffs:
        with steps.step("fetching diffs"):
            diffs, failures, fetched = forge.diffs(prs, cfg.cache_dir)
            log.info(
                "Diffs: %d downloaded, %d from cache, %d unavailable", fetched, len(diffs) - fetched, len(failures)
            )
            diff_problems = {n: ("too-large" if isinstance(e, TooLarge) else e.reason) for n, e in failures.items()}
            errors = [f"#{n}: {e.reason}" for n, e in failures.items() if not isinstance(e, TooLarge)]
            if errors:
                warnings.add(
                    "diffs",
                    f"Couldn't fetch {len(errors)} diffs; their line heuristics are missing until the next refresh",
                    errors,
                )
            counted = count_from_diffs(prs, diffs)
            if counted:
                log.info(
                    "Counted changed lines from the diffs of %d %ss the forge gave no counts for",
                    counted,
                    forge.pr_noun,
                )
    _gap_warnings(warnings, prs, forge)  # after the diffs: they may have filled the line counts
    with steps.step("checking memberships"):
        members, failed = fetch_members(
            forge, profile, prs, cfg.cache_dir / "members.json", cfg.tuning.authors.membership_cache_days
        )
        if failed:
            warnings.add(
                "members",
                f"Couldn't check {len(failed)} team or org memberships; those authors use the profile's other rules this time",
                failed,
            )
    stats = {"prs": len(prs), "open": total, "diffs_downloaded": fetched, "diffs_cached": len(diffs) - fetched}
    return Snapshot(
        viewer,
        prs,
        contracts,
        instructions,
        context.required,
        context.main_failing,
        diffs,
        now,
        members,
        warnings,
        diff_problems,
        stats,
    )


def count_from_diffs(prs: list[PullRequest], diffs: dict[int, str]) -> int:
    """Fill in per-file and per-PR line counts from the fetched diffs for PRs whose forge reported none.

    Azure DevOps has no count anywhere in its API, but its diff is assembled from the files themselves, so the
    counts fall out of it. Returns how many PRs were filled; the rest keep their ``line_counts`` gap.
    """
    filled = 0
    for pr in prs:
        text = diffs.get(pr.number)
        if pr.line_counts.known or text is None:
            continue
        counts = count_lines(text)
        pr.files = [ChangedFile(f.path, *counts.get(f.path, (0, 0)), f.change) for f in pr.files]
        pr.additions = sum(f.additions for f in pr.files)
        pr.deletions = sum(f.deletions for f in pr.files)
        pr.line_counts = known(True)
        filled += 1
    return filled


def _gap_warnings(warnings: Warnings, prs: list[PullRequest], forge: Forge) -> None:
    """One warning per kind of thing the forge couldn't tell us, so the gap is visible beyond the PR signals."""
    gaps: dict[tuple[str, str], list[str]] = {}
    for pr in prs:
        for name, cap in pr.gaps():
            gaps.setdefault((name, cap.kind), []).append(f"#{pr.number}: {cap.reason}")
    for (name, kind), details in sorted(gaps.items()):
        if kind == "unsupported":
            warnings.add(
                f"gap-{name}",
                f"{forge.label} can't tell us the {name.replace('_', ' ')}; related signals and lanes use a neutral default",
            )
        else:
            warnings.add(
                f"gap-{name}",
                f"Couldn't read the {name.replace('_', ' ')} for {len(details)} {forge.pr_noun}s this time",
                details,
            )


def fetch_rules(
    forge: Forge, profile: Profile, *, branch: str = "main"
) -> tuple[dict[str, Contract], list[Instruction], list[str]]:
    """The profile's architecture contracts and the repo's instruction files, from the default branch.

    Also returns what couldn't be fetched, so the build can carry on with the rest and say what's missing.
    """
    problems: list[str] = []
    doc = None
    if profile.contracts_source:
        try:
            doc = forge.read_file(profile.contracts_source)
        except ForgeError as exc:
            problems.append(f"{profile.contracts_source}: {exc.reason}")
    contracts = contracts_for(profile, doc)
    if not profile.instructions_dir:
        return contracts, [], problems
    try:
        names = [n for n in forge.list_dir(profile.instructions_dir) if n.endswith(".md")]
    except ForgeError as exc:
        problems.append(f"{profile.instructions_dir}/: {exc.reason}")
        return contracts, [], problems

    def fetch(name: str) -> str | None:
        try:
            return forge.read_file(f"{profile.instructions_dir}/{name}")
        except ForgeError as exc:
            problems.append(f"{name}: {exc.reason}")
            return None

    texts = pool_map(fetch, names, forge.tuning.concurrency)
    instructions = [
        parse_instruction(n, t, forge.file_url(f"{profile.instructions_dir}/{n}", branch))
        for n, t in zip(names, texts, strict=True)
        if t is not None
    ]
    return contracts, [i for i in instructions if i.patterns], problems


def fetch_members(
    forge: Forge, profile: Profile, prs: list[PullRequest], cache_path: Path, ttl_days: float = 7.0
) -> tuple[set[str], list[str]]:
    a = profile.authors
    if not (a.internal_teams or a.internal_orgs):
        return set(), []
    if "memberships" not in forge.capabilities:
        return set(), [f"{forge.label} can't answer team or org membership questions"]
    logins = {p.author.login for p in prs if not p.author.bot}
    return internal_members(
        forge,
        teams=a.internal_teams,
        orgs=a.internal_orgs,
        logins=logins - {""},
        cache_path=cache_path,
        ttl_days=ttl_days,
    )


def analyze_snapshot(
    snap: Snapshot, profile: Profile, *, budget: int = 60, ledger_entries: dict[str, Any] | None = None,
    tuning: Tuning | None = None, forge: Forge | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Lanes, signals, overlaps, the burn-down plan, and a brief per PR (which names the forge, GitHub by default)."""
    ledger_entries = ledger_entries or {}
    items = [
        analyze(
            p,
            profile=profile,
            viewer=snap.viewer,
            instructions=snap.instructions,
            diff_text=snap.diffs.get(p.number),
            now=snap.now,
            required_checks=snap.required_checks,
            main_failing=snap.main_failing.get(set()),
            members=snap.members,
            planning=tuning.planning if tuning else None,
            payload=tuning.payload if tuning else None,
        )
        for p in snap.prs
    ]
    by_number = {p.number: p for p in snap.prs}
    for it in items:
        _partial_flags(it, by_number[it["number"]], snap.diff_problems.get(it["number"]))
    overlaps = compute_overlaps(items, profile)
    titles = {it["number"]: it["title"] for it in items}
    by_name = {i.name: i for i in snap.instructions}
    briefs: dict[int, str] = {}
    for it in items:
        related = sorted(
            (
                (o["b"] if o["a"] == it["number"] else o["a"], o["files"])
                for o in overlaps
                if it["number"] in (o["a"], o["b"])
            ),
            key=lambda r: -len(r[1]),
        )
        it["related"] = [n for n, _ in related]
        briefs[it["number"]] = build_brief(
            it, profile=profile, contracts=snap.contracts, instructions=by_name, related=related, titles=titles,
            ledger_entry=ledger_entries.get(str(it["number"])), forge_label=forge.label if forge else "GitHub",
            files_tab=forge.files_tab if forge else "/files",
        )  # fmt: skip
    return {
        "items": items,
        "overlaps": overlaps,
        "sessions": plan_sessions(items, profile, budget=budget),
        "briefs": briefs,
    }


def _partial_flags(item: dict[str, Any], pr: PullRequest, diff_problem: str | None) -> None:
    """Say on the PR itself when its data is incomplete, so nobody mistakes "no signals" for "nothing to see"."""
    if diff_problem == "too-large":
        item["flags"].append({"id": "diff-too-large", "kind": "info",
                              "msg": f"Diff too large to serve ({pr.changed_files} files): line heuristics skipped"})  # fmt: skip
    elif diff_problem == "unsupported":
        item["flags"].append({"id": "diff-unsupported", "kind": "info",
                              "msg": "This forge serves no whole-change diff: line heuristics can't run"})  # fmt: skip
    elif diff_problem:
        item["flags"].append(
            {"id": "diff-unavailable", "kind": "info", "msg": f"Diff couldn't be fetched this time: {diff_problem}"}
        )
    if pr.truncated:
        parts = ", ".join(f"{have} of {of} {name}" for name, (have, of) in pr.truncated.items())
        item["flags"].append(
            {
                "id": "partial",
                "kind": "info",
                "msg": f"Only partly fetched ({parts}): placement and checks may be incomplete",
            }
        )
        item["truncated"] = {k: list(v) for k, v in pr.truncated.items()}


def build(
    cfg: Config, *, with_diffs: bool = True, profile: Profile | None = None, draft: bool = False, write: bool = True
) -> dict[str, Any]:
    """Fetch, analyze, and write data.json, the dashboard, and briefs.

    ``draft`` marks an onboarding preview. ``write=False`` only returns the payload, leaving the repo's
    files alone, so a draft profile can be previewed without disturbing the live dashboard.

    Every log line inside is tagged with the repo and a build id. A failure is logged once (one line for
    expected GitHub errors, a traceback otherwise) and raised as ``BuildFailed``.
    """
    build_id = logs.new_id()
    with logs.context(cfg.repo, build_id):
        steps = logs.Steps(log)
        started = time.monotonic()
        log.info("build started%s", "" if write else " (preview)")
        try:
            payload = _build(cfg, steps, with_diffs=with_diffs, profile=profile, draft=draft, write=write)
        except Exception as exc:
            failed = BuildFailed(cfg.repo, steps.current, exc, logs.new_id(), time.monotonic() - started)
            extra = {"error_id": failed.error_id, "step": failed.step, "duration_s": round(failed.seconds, 1)}
            message = "build failed after %.1fs at step '%s' [error %s]: %s"
            args = (failed.seconds, failed.step, failed.error_id, failed.reason)
            if failed.expected:
                log.warning(message, *args, extra=extra)
            else:
                log.exception(message, *args, extra=extra)
            raise failed from exc
        stats = payload["stats"]
        left = ", ".join(f"{v} {k}" for k, v in (stats.get("rate_limit") or {}).items())
        log.info(
            "built in %.1fs: %d PRs, %d diffs downloaded / %d cached, %d warnings%s (%s)",
            time.monotonic() - started, stats["prs"], stats["diffs_downloaded"], stats["diffs_cached"], len(payload["warnings"]),
            f", rate limit left: {left}" if left else "",
            steps.summary(),
        )  # fmt: skip
        return payload


def _build(
    cfg: Config, steps: logs.Steps, *, with_diffs: bool, profile: Profile | None, draft: bool, write: bool
) -> dict[str, Any]:
    from readthrough.onboard import drift

    forge = forges.make(cfg)
    with steps.step("resolving profile"):
        profile = profile or resolve_profile(cfg, forge)
    snap = fetch_snapshot(cfg, profile, with_diffs=with_diffs, forge=forge, steps=steps)
    with steps.step("analyzing"):
        entries = ledger.load(cfg.ledger_path)
        result = analyze_snapshot(
            snap, profile, budget=cfg.session_minutes, ledger_entries=entries, tuning=cfg.tuning, forge=forge
        )
    items = result["items"]
    previous = _load_json(cfg.seen_path) or _load_json(cfg.data_path)

    steps.current = "writing briefs"
    if write:
        cfg.briefs_dir.mkdir(parents=True, exist_ok=True)
        for old in cfg.briefs_dir.glob("pr-*.md"):
            old.unlink()
    review.session_index(cfg).scan(force=True)
    known = review.agent_set(cfg)
    for it in items:
        entry = entries.get(str(it["number"]))
        primary = review.session_id_for(cfg, it["number"], entry)
        it["session"] = review.session_info(cfg, primary, known=known)
        it["sessions"] = review.pr_sessions(cfg, it["number"], primary, known=known)
        log_entries = study.load(cfg, it["number"])
        it["study"] = study.summarize(log_entries)
        appendix = study.render_appendix(log_entries)
        brief = result["briefs"][it["number"]]
        if write:
            (cfg.briefs_dir / f"pr-{it['number']}.md").write_text(
                brief + ("\n" + appendix if appendix else ""), encoding="utf-8"
            )

    payload = {
        "schema": DATA_SCHEMA,
        "version": __version__,
        "repo": cfg.repo,
        "slug": cfg.slug,
        "forge": {
            "name": forge.name,
            "label": forge.label,
            "pr_noun": forge.pr_noun,
            "url": forge.web_url(),
            "files_tab": forge.files_tab,
        },
        "viewer": snap.viewer,
        "generated": snap.now.isoformat(),
        "profile": {**profile.to_payload(), "onboarded": cfg.onboarded},
        "rules": rule_descriptions(profile),
        "components": [
            {
                "id": c.id,
                "label": c.label,
                "layer": c.layer,
                "contract": vars(snap.contracts[c.id]) if c.id in snap.contracts else {},
            }
            for c in profile.components
        ],
        "instructions": [{"name": i.name, "patterns": i.patterns, "url": i.url} for i in snap.instructions],
        "prs": items,
        "overlaps": result["overlaps"],
        "sessions": result["sessions"],
        "ledger": entries,
        "news": whats_new(previous, items, entries, snap.viewer),
        "drift": drift(items, cfg.tuning.onboarding.drift_coverage),
        "draft": draft,
        "warnings": snap.warnings,
        "stats": {**snap.stats, "rate_limit": forge.rate_limit()},
        "freshness": {
            "stale_live_hours": cfg.tuning.freshness.stale_live_hours,
            "stale_snapshot_hours": cfg.tuning.freshness.stale_snapshot_hours,
        },
        "links": {
            "protocol": registered_protocol_command() is not None,
            "briefs_dir": str(cfg.briefs_dir.resolve()),
            "clone_uri": cfg.clone.resolve().as_uri() if cfg.clone else None,
        },
    }
    steps.current = "writing the dashboard"
    if write:
        cfg.data_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(cfg.data_path, json.dumps(payload, indent=1))
        write_atomic(cfg.dashboard_path, render_html(payload))
    return payload


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def mark_seen(cfg: Config, payload: dict[str, Any] | None = None) -> None:
    """Remember what the user last looked at, so "what's new" means new since then, not since the last build.

    Matters once the live server rebuilds every few minutes: otherwise the banner would only ever
    cover ten minutes, and the morning `--open auto` would never find anything to open for.
    """
    payload = payload or _load_json(cfg.data_path)
    if not payload:
        return
    keep = ("number", "title", "author", "head", "flags")
    seen = {"generated": payload.get("generated"), "prs": [{k: p.get(k) for k in keep} for p in payload.get("prs", [])]}
    write_atomic(cfg.seen_path, json.dumps(seen))


VENDOR_SCRIPTS = (
    "cytoscape.min.js",
    "layout-base.js",
    "cose-base.js",
    "cytoscape-fcose.js",
    "marked.umd.js",
    "purify.min.js",
    "lucide-icons.js",
)


def _vendor_js() -> str:
    vendor = files("readthrough").joinpath("assets/vendor")
    parts = [vendor.joinpath(name).read_text(encoding="utf-8") for name in VENDOR_SCRIPTS]
    return "\n;\n".join(parts).replace("</script", "<\\/script")


def page_source(name: str) -> str:
    """Assemble a page from its sources in assets/web: markup, shared theme plus page CSS, and page script.

    The result is still one self-contained file; the split only exists so each part can be edited as what it is.
    """
    web = files("readthrough").joinpath("assets/web")

    def part(file: str) -> str:
        return web.joinpath(file).read_text(encoding="utf-8").removesuffix("\n")

    css = "\n".join(p for p in (part("theme.css"), part(f"{name}.css")) if p)
    return (
        part(f"{name}.html")
        .replace("/*__READTHROUGH_CSS__*/", css)
        .replace("/*__READTHROUGH_APP__*/", part(f"{name}.js"))
    )


def render_setup(live: dict[str, Any], nonce: str) -> str:
    """The onboarding and settings page. Only ever served live: it's all API calls."""
    page = page_source("setup")
    icons = files("readthrough").joinpath("assets/vendor/lucide-icons.js").read_text(encoding="utf-8")
    return (
        page.replace("__NONCE__", nonce)
        .replace("/*__READTHROUGH_ICONS__*/", icons.replace("</script", "<\\/script"))
        .replace("/*__READTHROUGH_LIVE__*/null", inline_json(live))
    )


def inline_json(value: Any) -> str:
    r"""JSON that can sit inside a <script> element whatever strings it carries.

    ``<`` becomes ``\u003c``, which JSON reads as the same character, so neither ``</script>`` nor ``<!--`` (which
    puts the HTML tokenizer into a state where the real ``</script>`` no longer counts) can appear in the page.
    """
    return json.dumps(value).replace("<", "\\u003c")


def render_html(payload: dict[str, Any], *, live: dict[str, Any] | None = None, nonce: str | None = None) -> str:
    """Inline the libraries, the data, and (when served) the live-mode settings into the single-file dashboard.

    Every script carries a per-render nonce, and the page's CSP only runs scripts with that nonce. PR
    descriptions are third-party HTML, so even if something slipped past sanitizing it could not run.
    """
    nonce = nonce or secrets.token_urlsafe(16)
    return (
        page_source("dashboard")
        .replace("__NONCE__", nonce)
        .replace("/*__READTHROUGH_VENDOR__*/", _vendor_js())
        .replace("/*__READTHROUGH_LIVE__*/null", inline_json(live))
        .replace("/*__READTHROUGH_DATA__*/null", inline_json(payload))  # last: nothing in the data is a placeholder
    )


def session_changes(payload: dict[str, Any], cfg: Config) -> dict[int, dict[str, Any]]:
    """The PRs whose agent sessions differ from what the payload says, and their new values.

    Only reads: the server runs this on its watcher thread every few seconds and must not hold the payload lock
    while it looks, or every page request waits on it. The agents are resolved once per pass; the lookups
    behind them cost milliseconds each and there is one PR after another.
    """
    review.session_index(cfg).scan()
    known = review.agent_set(cfg)
    changed = {}
    for it in payload["prs"]:
        entry = payload["ledger"].get(str(it["number"]))
        primary = review.session_id_for(cfg, it["number"], entry)
        info = review.session_info(cfg, primary, known=known)
        sessions = review.pr_sessions(cfg, it["number"], primary, known=known)
        if info != it.get("session") or sessions != it.get("sessions"):
            changed[it["number"]] = {"session": info, "sessions": sessions}
    return changed


def apply_session_changes(payload: dict[str, Any], changed: dict[int, dict[str, Any]]) -> None:
    """Write what ``session_changes`` found into the payload (under the server's lock, when there is one)."""
    by_number = {it["number"]: it for it in payload["prs"]}
    for number, fields in changed.items():
        if (it := by_number.get(number)) is not None:
            it.update(fields)


def refresh_sessions(payload: dict[str, Any], cfg: Config) -> dict[int, dict[str, Any]]:
    """Recompute agent session status for every PR in place. Returns only the PRs whose sessions changed."""
    changed = session_changes(payload, cfg)
    apply_session_changes(payload, changed)
    return changed
