"""Turn pull requests into lanes, signals, overlaps, and a burn-down plan.

The input is the neutral :mod:`readthrough.model` record every forge adapter produces. Everything
repo-specific (components, signals, heuristics, author kinds) comes from the profile.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Set as AbstractSet
from typing import Any

from readthrough import ci
from readthrough.model import Capability, Check, PullRequest
from readthrough.profile import Authors, Profile
from readthrough.rules import Instruction
from readthrough.tuning import PayloadTuning, PlanningTuning, default_tuning

PR = dict[str, Any]

# What a gap in the forge's answer means for the PR, by field. Shown as a signal so nobody reads the
# resulting default as a fact.
GAP_MESSAGES = {
    "association": "The author's relationship to the repo",
    "decision": "The review decision",
    "mergeable": "Whether the change merges cleanly",
    "threads": "Review threads",
    "checks": "CI checks",
    "line_counts": "Lines changed per file (so size, effort, and the no-tests check go by file counts)",
}


def author_kind(
    login: str,
    association: str | None,
    authors: Authors,
    *,
    bot: bool = False,
    members: AbstractSet[str] = frozenset(),
) -> str:
    """Classify a PR author into one of the profile's author kinds.

    Order: bots, the profile's explicit lists, membership of the profile's internal teams and orgs
    (looked up at build time and passed in as ``members``), then the forge's author association, then
    the profile's fallback for anything else.
    """
    lower = login.lower()
    if bot or lower.endswith("[bot]") or lower in {b.lower() for b in authors.bots}:
        return "bot"
    if lower in {x.lower() for x in authors.internal}:
        return "internal"
    if lower in {x.lower() for x in authors.external}:
        return "external"
    if lower in members:
        return "internal"
    return authors.association.get(association or "NONE", authors.unknown)


def parse_ts(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


def days_between(value: str | None, now: dt.datetime) -> float:
    ts = parse_ts(value)
    return round((now - ts).total_seconds() / 86400, 1) if ts else 0.0


def parse_diff(text: str) -> dict[str, list[tuple[int, str]]]:
    """Map each file in a unified diff to its added lines as ``(new_line_number, text)``."""
    files: dict[str, list[tuple[int, str]]] = {}
    current: list[tuple[int, str]] | None = None
    line_no = 0
    for raw in text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current = files.setdefault(path[2:], []) if path.startswith("b/") else None
        elif raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            line_no = int(match.group(1)) if match else 0
        elif current is not None:
            if raw.startswith("+"):
                current.append((line_no, raw[1:]))
                line_no += 1
            elif raw.startswith(" "):
                line_no += 1
    return files


def strip_brackets(text: str) -> str:
    """Drop nested ``[...]`` so commas inside hints like ``dict[str, Any]`` don't look like parameters."""
    previous = None
    while previous != text:
        previous, text = text, re.sub(r"\[[^\[\]]*\]", "", text)
    return text


def find_smells(
    added: dict[str, list[tuple[int, str]]], profile: Profile, *, per_file_cap: int = 3, code_chars: int = 160
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    counts: dict[tuple[str, str], int] = {}
    for path, lines in added.items():
        is_test = profile.is_test(path)
        for smell in profile.smells:
            if (is_test and smell.kind != "info") or not smell.path_re.search(path):
                continue
            for line_no, text in lines:
                probe = strip_brackets(text) if smell.strip_brackets else text
                if not smell.line_re.search(probe):
                    continue
                key = (smell.id, path)
                counts[key] = counts.get(key, 0) + 1
                if counts[key] <= per_file_cap:
                    hits.append({"id": smell.id, "kind": smell.kind, "path": path, "line": line_no, "msg": smell.message, "code": text.strip()[:code_chars]})  # fmt: skip
    return hits


def rule_descriptions(profile: Profile) -> dict[str, dict[str, str]]:
    """How each signal and line heuristic is worked out, keyed by id, for the dashboard's "how was this decided" notes.

    ``source`` is ``forge`` (a fact from the forge the PRs live on), ``profile`` (a rule in the repo's profile),
    or ``built-in`` (readthrough's own check, with thresholds from the profile). None of these involve a
    language model.
    """
    out: dict[str, dict[str, str]] = {
        "no-tests": {
            "source": "built-in",
            "how": f"More than {profile.no_tests_min_lines} source lines changed and nothing in the `{profile.tests_component}` component",
        },
        "cross-cutting": {"source": "built-in", "how": f"Touches {profile.cross_cutting_layers} or more source layers"},
        "large": {"source": "built-in", "how": f"More than {profile.large_lines} lines changed"},
        "stale": {"source": "built-in", "how": f"No activity for more than {profile.stale_days} days"},
        "conflicts": {"source": "forge", "how": "The forge reports merge conflicts with the base branch"},
        "ci": {
            "source": "forge",
            "how": "A required check fails on the PR's latest commit and not on the base branch",
        },
        "ci-optional": {"source": "forge", "how": "Only checks that aren't required are failing"},
        "ci-not-run": {
            "source": "forge",
            "how": "None of the base branch's required checks reported on the latest commit",
        },
        "threads": {"source": "forge", "how": "Unresolved review threads"},
        "requested": {"source": "forge", "how": "You're a requested reviewer"},
        "re-review": {"source": "forge", "how": "The author pushed commits after a changes-requested review"},
        "ci-main": {"source": "forge", "how": "The same checks fail on the base branch's latest commit"},
        "diff-too-large": {
            "source": "forge",
            "how": "The forge won't serve a diff this big, so line heuristics can't run",
        },
        "diff-unavailable": {
            "source": "forge",
            "how": "Fetching the diff failed this time; it's retried on the next refresh",
        },
        "diff-unsupported": {
            "source": "forge",
            "how": "This forge has no API for a whole-change diff, so line heuristics never run",
        },
        "partial": {
            "source": "forge",
            "how": "More files, checks, or review threads than the [tuning.forge] limits fetch",
        },
        "gap": {
            "source": "forge",
            "how": "The forge couldn't supply this, so the dependent signals and lane use a neutral default",
        },
    }
    for s in profile.signals:
        when = f" ({', '.join(s.change_types).lower()} only)" if s.change_types else ""
        out[s.id] = {"source": "profile", "how": f"Profile signal: a changed file matches {', '.join(s.paths)}{when}"}
    for pair in profile.paired_files:
        out[pair.id] = {
            "source": "profile",
            "how": f"Profile paired files: {' and '.join(pair.extensions)} under {pair.under or 'the repo'} didn't change together",
        }
    for smell in profile.smells:
        out[smell.id] = {
            "source": "profile",
            "how": f"Profile line heuristic: an added line matches /{smell.line}/ in files matching /{smell.path}/",
        }
    for kind in profile.authors.kinds.values():
        if kind.signal:
            out[kind.signal["id"]] = {
                "source": "profile",
                "how": f"Profile [authors]: the author classifies as {kind.label}",
            }
    return out


def estimate_minutes(
    by_comp: dict[str, int], layers: int, profile: Profile, planning: PlanningTuning | None = None
) -> int:
    """Rough reading time. Source lines cost most, tests less, docs and infra almost nothing."""
    t = planning or default_tuning().planning
    src = sum(v for k, v in by_comp.items() if profile.is_source(k))
    tests = by_comp.get(profile.tests_component, 0) if profile.tests_component else 0
    rest = sum(v for k, v in by_comp.items() if not profile.is_source(k) and k != profile.tests_component)
    minutes = (t.estimate_base_minutes + t.estimate_minutes_per_source_line * src + t.estimate_minutes_per_test_line * tests
               + t.estimate_minutes_per_other_line * rest + t.estimate_minutes_per_extra_layer * max(0, layers - 1))  # fmt: skip
    return int(min(t.estimate_max_minutes, minutes))


def _paired_mismatch(paths: list[str], under: str, exts: list[str]) -> bool:
    a, b = exts
    left = {p[: -len(a)] for p in paths if p.startswith(under) and p.endswith(a)}
    right = {p[: -len(b)] for p in paths if p.startswith(under) and p.endswith(b)}
    return bool(left ^ right)


def analyze(
    pr: PullRequest,
    *,
    profile: Profile,
    viewer: str,
    instructions: list[Instruction],
    diff_text: str | None,
    now: dt.datetime,
    required_checks: dict[str, Capability[set[str]]] | None = None,
    main_failing: AbstractSet[str] = frozenset(),
    members: AbstractSet[str] = frozenset(),
    planning: PlanningTuning | None = None,
    payload: PayloadTuning | None = None,
) -> PR:
    planning = planning or default_tuning().planning
    payload = payload or default_tuning().payload
    by_comp: dict[str, int] = {}
    file_rows = []
    counted = pr.line_counts.known  # else every file weighs one line: the forge gave no counts
    for f in pr.files:
        comp = profile.component_of(f.path)
        churn = f.additions + f.deletions if counted else 1
        if profile.is_capped(f.path):
            churn = min(churn, profile.capped_lines)
        by_comp[comp] = by_comp.get(comp, 0) + churn
        file_rows.append({"path": f.path, "add": f.additions, "del": f.deletions, "type": f.change, "comp": comp})
    code_comps = {c for c in by_comp if profile.is_source(c)}
    layers = {profile.component_by_id[c].layer for c in code_comps}
    primary = max(code_comps or set(by_comp) or {"other"}, key=lambda c: by_comp.get(c, 0))
    paths = [f.path for f in pr.files]

    flags: list[dict[str, str]] = []

    def flag(fid: str, kind: str, msg: str) -> None:
        flags.append({"id": fid, "kind": kind, "msg": msg})

    for name, cap in pr.gaps():
        how = "not something this forge can tell us" if cap.kind == "unsupported" else f"couldn't be read: {cap.reason}"
        flag("gap", "info", f"{GAP_MESSAGES[name]}: {how}")

    unresolved = _unresolved_threads(pr, payload)
    for signal in profile.signals:
        if signal.fires(pr.files):
            flag(signal.id, signal.kind, signal.message)
    src_churn = sum(by_comp[c] for c in code_comps)
    if (
        counted
        and profile.tests_component
        and src_churn > profile.no_tests_min_lines
        and profile.tests_component not in by_comp
    ):
        flag("no-tests", "risk", f"{src_churn} lines of source changed with no test changes")
    for pair in profile.paired_files:
        if _paired_mismatch(paths, pair.under, pair.extensions):
            flag(pair.id, pair.kind, pair.message)
    if len(layers) >= profile.cross_cutting_layers:
        flag("cross-cutting", "risk", f"Touches {len(layers)} architecture layers: check for responsibility bleed")
    size = pr.additions + pr.deletions
    if counted and size > profile.large_lines:
        flag("large", "risk", f"Large change ({size} lines): consider asking to split it")
    if pr.mergeable.get("UNKNOWN") == "CONFLICTING":
        flag("conflicts", "blocker", "Merge conflicts with base")
    ci_info = _ci_summary(pr, required_checks or {}, main_failing)
    _ci_flags(ci_info, flag)
    idle = days_between(pr.updated, now)
    if idle > profile.stale_days:
        flag("stale", "info", f"No activity for {int(idle)} days")
    if unresolved:
        flag("threads", "info", f"{len(unresolved)} unresolved review thread(s)")
    if viewer in pr.requested:
        flag("requested", "you", "Your review is requested")

    cr_times = [parse_ts(r.submitted) for r in pr.reviews if r.state == "CHANGES_REQUESTED" and r.submitted]
    last_commit = parse_ts(pr.last_commit)
    pushed_since_cr = bool(cr_times and last_commit and last_commit > max(t for t in cr_times if t))

    author = pr.author.login
    kind = author_kind(author, pr.association.get("NONE"), profile.authors, bot=pr.author.bot, members=members)
    kind_spec = profile.authors.kinds[kind]
    if kind_spec.signal:
        flag(kind_spec.signal["id"], kind_spec.signal["kind"], kind_spec.signal["message"])
    decision = pr.decision.get("NONE")
    lane = _lane(pr, decision=decision, viewer=viewer, flags=flags, pushed_since_cr=pushed_since_cr)
    if pushed_since_cr and lane == "ready":
        flag("re-review", "you", "Author pushed after changes were requested: re-review")

    minutes = estimate_minutes(by_comp, len(layers), profile, planning)
    age = days_between(pr.created, now)
    priority = _priority(lane, age=age, minutes=minutes, ci_state=ci_info["state"], requested=viewer in pr.requested,
                         pushed_since_cr=pushed_since_cr, closes_issue=bool(pr.issues), planning=planning)  # fmt: skip
    if lane == "ready" and kind_spec.priority:
        priority = round(priority + kind_spec.priority, 1)
    return {
        "number": pr.number,
        "title": pr.title,
        "url": pr.url,
        "author": author,
        "author_kind": kind,
        "author_group": kind_spec.group,
        "author_association": pr.association.get("NONE"),
        "body": pr.body[: payload.description_chars],
        "body_html": pr.body_html[: payload.description_html_chars],
        "draft": pr.draft,
        "created": pr.created,
        "updated": pr.updated,
        "age_days": age,
        "idle_days": idle,
        "additions": pr.additions,
        "deletions": pr.deletions,
        "changed_files": pr.changed_files,
        "head": pr.head,
        "base": pr.base,
        "review_decision": decision,
        "mergeable": pr.mergeable.get("UNKNOWN"),
        "ci_state": ci_info["state"],
        "ci_checks": ci_info,
        "labels": list(pr.labels),
        "requested": list(pr.requested),
        "reviews": [{"author": r.author, "state": r.state} for r in pr.reviews],
        "comments": pr.comments,
        "issues": [{"number": i.number, "title": i.title, "url": i.url} for i in pr.issues],
        "files": file_rows,
        "by_comp": by_comp,
        "primary": primary,
        "layers": sorted(layers, key=profile.layer_order.__getitem__),
        "rules": [i.name for i in instructions if any(i.applies_to(p) for p in paths)],
        "flags": flags,
        "smells": find_smells(parse_diff(diff_text), profile, code_chars=payload.line_heuristic_code_chars)
        if diff_text
        else [],
        "unresolved": unresolved,
        "lane": lane,
        "minutes": minutes,
        "priority": priority,
    }


def _ci_summary(
    pr: PullRequest, required_checks: dict[str, Capability[set[str]]], main_failing: AbstractSet[str]
) -> dict[str, Any]:
    if not pr.checks.known:
        return ci.unknown(pr.checks.reason)
    checks: list[Check] = pr.checks.get([])
    required = required_checks.get(pr.base)
    # Required checks the forge couldn't name are treated as "none known": the state then says "failing"
    # for any own failure rather than "optional", which errs towards attention.
    return ci.classify(checks, required=required.get(set()) if required else None, main_failing=main_failing)


def _check_names(rows: list[dict[str, Any]]) -> str:
    return ", ".join(r["name"] for r in rows[:3]) + (f" and {len(rows) - 3} more" if len(rows) > 3 else "")


def _ci_flags(ci_info: dict[str, Any], flag: Any) -> None:
    state, failing = ci_info["state"], ci_info["failing"]
    names = _check_names
    own = [r for r in failing if not r["on_main"]]
    if state == "failing":
        flag("ci", "blocker", f"CI failing: {names(own)}")
    elif state == "failing-optional":
        flag("ci-optional", "risk", f"Optional check failing: {names(own)}")
    elif state == "not-run":
        flag(
            "ci-not-run",
            "info",
            "Required checks haven't run on the latest commit (waiting for workflow approval, a label, or a re-run?)",
        )
    if any(r["on_main"] for r in failing):
        flag(
            "ci-main",
            "info",
            f"Also failing on main, not this PR's fault: {names([r for r in failing if r['on_main']])}",
        )


def _unresolved_threads(pr: PullRequest, payload: PayloadTuning) -> list[dict[str, Any]]:
    return [
        {
            "path": t.path,
            "line": t.line,
            "outdated": t.outdated,
            "author": t.author,
            "body": t.body[: payload.thread_comment_chars],
            "body_html": t.body_html[: payload.thread_comment_html_chars],
        }
        for t in pr.threads.get([])
        if not t.resolved
    ]


def _lane(pr: PullRequest, *, decision: str, viewer: str, flags: list[dict[str, str]], pushed_since_cr: bool) -> str:
    if pr.author.login == viewer:
        return "mine"
    if pr.draft:
        return "draft"
    if any(f["kind"] == "blocker" for f in flags):
        return "blocked"
    if decision == "CHANGES_REQUESTED" and not pushed_since_cr:
        return "waiting"
    if decision == "APPROVED":
        return "approved"
    return "ready"


def _priority(
    lane: str, *, age: float, minutes: int, ci_state: str, requested: bool, pushed_since_cr: bool, closes_issue: bool,
    planning: PlanningTuning,
) -> float:  # fmt: skip
    t = planning
    if lane == "approved":
        return t.priority_approved
    if lane != "ready":
        return 0.0
    score = t.priority_base + min(age, t.priority_age_cap_days)
    score += (t.priority_review_requested if requested else 0) + (t.priority_re_review if pushed_since_cr else 0)
    score += (
        t.priority_quick_bonus
        if minutes <= t.priority_quick_minutes
        else t.priority_short_bonus
        if minutes <= t.priority_short_minutes
        else 0
    )
    # A green PR can be reviewed with confidence; one with its own optional failures will likely change.
    score += {"passing": t.priority_ci_passing, "failing-main": t.priority_ci_failing_on_main,
              "failing-optional": t.priority_ci_failing_optional, "not-run": t.priority_ci_not_run}.get(ci_state, 0)  # fmt: skip
    score += t.priority_closes_issue if closes_issue else 0
    return round(score, 1)


def compute_overlaps(items: list[PR], profile: Profile) -> list[dict[str, Any]]:
    by_file: dict[str, list[int]] = {}
    for it in items:
        for f in it["files"]:
            if not profile.overlap_ignored(f["path"]):
                by_file.setdefault(f["path"], []).append(it["number"])
    pairs: dict[tuple[int, int], list[str]] = {}
    for path, nums in by_file.items():
        for i, a in enumerate(nums):
            for b in nums[i + 1 :]:
                pairs.setdefault((min(a, b), max(a, b)), []).append(path)
    return [{"a": a, "b": b, "files": files} for (a, b), files in sorted(pairs.items())]


def plan_sessions(items: list[PR], profile: Profile, *, budget: int = 60) -> list[dict[str, Any]]:
    """Pack ready PRs into ~budget-minute sessions, one component per session to limit context switching."""
    groups: dict[str, list[PR]] = {}
    for it in items:
        if it["lane"] == "ready":
            groups.setdefault(it["primary"], []).append(it)
    sessions: list[tuple[str, list[PR], int]] = []
    for comp, members in groups.items():
        members.sort(key=lambda m: (-m["priority"], m["number"]))
        current: list[PR] = []
        total = 0
        for m in members:
            if current and total + m["minutes"] > budget:
                sessions.append((comp, current, total))
                current, total = [], 0
            current.append(m)
            total += m["minutes"]
        if current:
            sessions.append((comp, current, total))
    sessions.sort(key=lambda s: -max(m["priority"] for m in s[1]))
    return [
        {
            "component": comp,
            "label": profile.component_by_id[comp].label,
            "prs": [m["number"] for m in ms],
            "minutes": total,
        }
        for comp, ms, total in sessions
    ]


def whats_new(previous: dict[str, Any] | None, items: list[PR], ledger: dict[str, Any], viewer: str) -> dict[str, Any]:
    """Summarize what changed since the previous build. Drives the dashboard banner and ``--open auto``."""
    if not previous:
        return {"since": None, "opened": [], "closed": [], "needs_you": [], "changed_after_review": []}
    before = {p["number"]: p for p in previous.get("prs", [])}
    now = {p["number"]: p for p in items}
    needs_you = [
        n for n, p in now.items()
        if any(f["kind"] == "you" for f in p["flags"])
        and not any(f["kind"] == "you" for f in before.get(n, {}).get("flags", []))
    ]  # fmt: skip
    changed_after_review = [
        n for n, p in now.items()
        if (entry := ledger.get(str(n))) and entry.get("head") and entry["head"] != p["head"]
        and before.get(n, {}).get("head") != p["head"]
    ]  # fmt: skip
    return {
        "since": previous.get("generated"),
        "opened": sorted(n for n in now if n not in before and now[n]["author"] != viewer),
        "closed": [{"number": n, "title": before[n]["title"]} for n in sorted(before) if n not in now],
        "needs_you": sorted(needs_you),
        "changed_after_review": sorted(changed_after_review),
    }


def is_newsworthy(news: dict[str, Any]) -> bool:
    return news["since"] is None or bool(news["opened"] or news["needs_you"] or news["changed_after_review"])
