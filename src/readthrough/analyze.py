"""Turn raw GitHub PR data into lanes, signals, overlaps, and a burn-down plan.

Everything repo-specific (components, signals, heuristics, author kinds) comes from the profile.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Set as AbstractSet
from typing import Any

from readthrough.ci import classify
from readthrough.profile import Authors, Profile
from readthrough.rules import Instruction
from readthrough.tuning import PayloadTuning, PlanningTuning, default_tuning

PR = dict[str, Any]


def author_kind(
    login: str,
    association: str | None,
    typename: str | None,
    authors: Authors,
    *,
    members: AbstractSet[str] = frozenset(),
) -> str:
    """Classify a PR author into one of the profile's author kinds.

    Order: bots, the profile's explicit lists, membership of the profile's internal teams and orgs
    (looked up at build time and passed in as ``members``), then GitHub's author association, then
    the profile's fallback for anything else.
    """
    lower = login.lower()
    if typename == "Bot" or lower.endswith("[bot]") or lower in {b.lower() for b in authors.bots}:
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

    ``source`` is ``github`` (a fact from GitHub), ``profile`` (a rule in the repo's profile), or ``built-in``
    (readthrough's own check, with thresholds from the profile). None of these involve a language model.
    """
    out: dict[str, dict[str, str]] = {
        "no-tests": {
            "source": "built-in",
            "how": f"More than {profile.no_tests_min_lines} source lines changed and nothing in the `{profile.tests_component}` component",
        },
        "cross-cutting": {"source": "built-in", "how": f"Touches {profile.cross_cutting_layers} or more source layers"},
        "large": {"source": "built-in", "how": f"More than {profile.large_lines} lines changed"},
        "stale": {"source": "built-in", "how": f"No activity for more than {profile.stale_days} days"},
        "conflicts": {"source": "github", "how": "GitHub reports merge conflicts with the base branch"},
        "ci": {
            "source": "github",
            "how": "A required check fails on the PR's latest commit and not on the base branch",
        },
        "ci-optional": {"source": "github", "how": "Only checks that aren't required are failing"},
        "ci-not-run": {
            "source": "github",
            "how": "None of the base branch's required checks reported on the latest commit",
        },
        "threads": {"source": "github", "how": "Unresolved review threads on GitHub"},
        "requested": {"source": "github", "how": "You're a requested reviewer on GitHub"},
        "re-review": {"source": "github", "how": "The author pushed commits after a changes-requested review"},
        "ci-main": {"source": "github", "how": "The same checks fail on the base branch's latest commit"},
        "diff-too-large": {
            "source": "github",
            "how": "GitHub won't serve diffs of more than 300 files, so line heuristics can't run",
        },
        "diff-unavailable": {
            "source": "github",
            "how": "Fetching the diff failed this time; it's retried on the next refresh",
        },
        "partial": {
            "source": "github",
            "how": "More files, checks, or review threads than the [tuning.github] limits fetch",
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
    pr: PR,
    *,
    profile: Profile,
    viewer: str,
    instructions: list[Instruction],
    diff_text: str | None,
    now: dt.datetime,
    required_checks: dict[str, set[str] | None] | None = None,
    main_failing: AbstractSet[str] = frozenset(),
    members: AbstractSet[str] = frozenset(),
    planning: PlanningTuning | None = None,
    payload: PayloadTuning | None = None,
) -> PR:
    planning = planning or default_tuning().planning
    payload = payload or default_tuning().payload
    files = pr["files"]["nodes"]
    by_comp: dict[str, int] = {}
    file_rows = []
    for f in files:
        comp = profile.component_of(f["path"])
        churn = f["additions"] + f["deletions"]
        if profile.is_capped(f["path"]):
            churn = min(churn, profile.capped_lines)
        by_comp[comp] = by_comp.get(comp, 0) + churn
        file_rows.append({"path": f["path"], "add": f["additions"], "del": f["deletions"], "type": f["changeType"], "comp": comp})  # fmt: skip
    code_comps = {c for c in by_comp if profile.is_source(c)}
    layers = {profile.component_by_id[c].layer for c in code_comps}
    primary = max(code_comps or set(by_comp) or {"other"}, key=lambda c: by_comp.get(c, 0))
    paths = [f["path"] for f in files]

    commit = (pr["commits"]["nodes"] or [{}])[0].get("commit", {})
    ci = (commit.get("statusCheckRollup") or {}).get("state") or "NONE"
    reviews = [r for r in pr["latestReviews"]["nodes"] if r.get("author")]
    requested = [
        (r["requestedReviewer"] or {}).get("login") or (r["requestedReviewer"] or {}).get("name")
        for r in pr["reviewRequests"]["nodes"]
    ]
    requested = [r for r in requested if r]
    unresolved = _unresolved_threads(pr, payload)

    flags: list[dict[str, str]] = []

    def flag(fid: str, kind: str, msg: str) -> None:
        flags.append({"id": fid, "kind": kind, "msg": msg})

    for signal in profile.signals:
        if signal.fires(files):
            flag(signal.id, signal.kind, signal.message)
    src_churn = sum(by_comp[c] for c in code_comps)
    if profile.tests_component and src_churn > profile.no_tests_min_lines and profile.tests_component not in by_comp:
        flag("no-tests", "risk", f"{src_churn} lines of source changed with no test changes")
    for pair in profile.paired_files:
        if _paired_mismatch(paths, pair.under, pair.extensions):
            flag(pair.id, pair.kind, pair.message)
    if len(layers) >= profile.cross_cutting_layers:
        flag("cross-cutting", "risk", f"Touches {len(layers)} architecture layers: check for responsibility bleed")
    size = pr["additions"] + pr["deletions"]
    if size > profile.large_lines:
        flag("large", "risk", f"Large change ({size} lines): consider asking to split it")
    if pr["mergeable"] == "CONFLICTING":
        flag("conflicts", "blocker", "Merge conflicts with base")
    ci_info = classify(
        ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or [],
        required=(required_checks or {}).get(pr["baseRefName"]),
        main_failing=main_failing,
    )
    _ci_flags(ci_info, flag)
    idle = days_between(pr["updatedAt"], now)
    if idle > profile.stale_days:
        flag("stale", "info", f"No activity for {int(idle)} days")
    if unresolved:
        flag("threads", "info", f"{len(unresolved)} unresolved review thread(s)")
    if viewer in requested:
        flag("requested", "you", "Your review is requested")

    cr_times = [
        parse_ts(r["submittedAt"]) for r in reviews if r["state"] == "CHANGES_REQUESTED" and r.get("submittedAt")
    ]
    last_commit = parse_ts(commit.get("committedDate"))
    pushed_since_cr = bool(cr_times and last_commit and last_commit > max(t for t in cr_times if t))

    author = (pr["author"] or {}).get("login", "ghost")
    kind = author_kind(
        author, pr.get("authorAssociation"), (pr["author"] or {}).get("__typename"), profile.authors, members=members
    )
    kind_spec = profile.authors.kinds[kind]
    if kind_spec.signal:
        flag(kind_spec.signal["id"], kind_spec.signal["kind"], kind_spec.signal["message"])
    lane = _lane(pr, author=author, viewer=viewer, flags=flags, pushed_since_cr=pushed_since_cr)
    if pushed_since_cr and lane == "ready":
        flag("re-review", "you", "Author pushed after changes were requested: re-review")

    minutes = estimate_minutes(by_comp, len(layers), profile, planning)
    age = days_between(pr["createdAt"], now)
    priority = _priority(lane, age=age, minutes=minutes, ci_state=ci_info["state"], requested=viewer in requested,
                         pushed_since_cr=pushed_since_cr, closes_issue=bool(pr["closingIssuesReferences"]["nodes"]),
                         planning=planning)  # fmt: skip
    if lane == "ready" and kind_spec.priority:
        priority = round(priority + kind_spec.priority, 1)
    return {
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "author": author,
        "author_kind": kind,
        "author_group": kind_spec.group,
        "author_association": pr.get("authorAssociation") or "NONE",
        "body": (pr["body"] or "")[: payload.description_chars],
        "body_html": (pr.get("bodyHTML") or "")[: payload.description_html_chars],
        "draft": pr["isDraft"],
        "created": pr["createdAt"],
        "updated": pr["updatedAt"],
        "age_days": age,
        "idle_days": idle,
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "changed_files": pr["changedFiles"],
        "head": pr["headRefOid"],
        "base": pr["baseRefName"],
        "review_decision": pr["reviewDecision"] or "NONE",
        "mergeable": pr["mergeable"],
        "ci": ci,
        "ci_state": ci_info["state"],
        "ci_checks": ci_info,
        "labels": [label["name"] for label in pr["labels"]["nodes"]],
        "requested": requested,
        "reviews": [{"author": r["author"]["login"], "state": r["state"]} for r in reviews],
        "comments": pr["comments"]["totalCount"],
        "issues": pr["closingIssuesReferences"]["nodes"],
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


def _unresolved_threads(pr: PR, payload: PayloadTuning) -> list[dict[str, Any]]:
    threads = []
    for t in pr["reviewThreads"]["nodes"]:
        if t["isResolved"]:
            continue
        first = t["comments"]["nodes"][0] if t["comments"]["nodes"] else {}
        threads.append({
            "path": t["path"],
            "line": t["line"],
            "outdated": t["isOutdated"],
            "author": (first.get("author") or {}).get("login", "?"),
            "body": (first.get("body") or "")[: payload.thread_comment_chars],
            "body_html": (first.get("bodyHTML") or "")[: payload.thread_comment_html_chars],
        })  # fmt: skip
    return threads


def _lane(pr: PR, *, author: str, viewer: str, flags: list[dict[str, str]], pushed_since_cr: bool) -> str:
    if author == viewer:
        return "mine"
    if pr["isDraft"]:
        return "draft"
    if any(f["kind"] == "blocker" for f in flags):
        return "blocked"
    if pr["reviewDecision"] == "CHANGES_REQUESTED" and not pushed_since_cr:
        return "waiting"
    if pr["reviewDecision"] == "APPROVED":
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
