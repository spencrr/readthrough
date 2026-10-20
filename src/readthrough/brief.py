"""Per-PR study briefs: the Markdown a reviewer (or the deep-review agent) reads before opening the diff."""

from __future__ import annotations

from typing import Any

from readthrough.ci import STATE_LABELS
from readthrough.profile import Profile
from readthrough.rules import Contract, Instruction


def reading_order(files: list[dict[str, Any]], profile: Profile) -> list[dict[str, Any]]:
    return sorted(files, key=lambda f: (profile.read_order.index(f["comp"]), -(f["add"] + f["del"])))


def _author_label(profile: Profile, it: dict[str, Any]) -> str:
    kind = profile.authors.kinds.get(it.get("author_kind", ""))
    return kind.label if kind else "unknown"


def build_brief(
    it: dict[str, Any],
    *,
    profile: Profile,
    contracts: dict[str, Contract],
    instructions: dict[str, Instruction],
    related: list[tuple[int, list[str]]],
    titles: dict[int, str],
    ledger_entry: dict[str, Any] | None,
    forge_label: str = "GitHub",
    files_tab: str = "/files",
) -> str:
    out: list[str] = []
    w = out.append
    w(f"# #{it['number']} {it['title']}\n")
    w(
        f"[Open on {forge_label}]({it['url']}) · [Files]({it['url']}{files_tab}) · author **@{it['author']}** ({_author_label(profile, it)}) · lane **{it['lane']}** · "
        f"+{it['additions']}/-{it['deletions']} in {it['changed_files']} files · opened {int(it['age_days'])}d ago · "
        f"idle {int(it['idle_days'])}d · CI **{STATE_LABELS.get(it.get('ci_state', ''), 'unknown')}** · review **{it['review_decision']}** · est. **{it['minutes']} min**\n"
    )
    w(f"Head SHA: `{it['head']}`\n")
    if ledger_entry:
        stale = " (new commits since then)" if ledger_entry.get("head") and ledger_entry["head"] != it["head"] else ""
        w(
            f"**Your ledger:** {ledger_entry.get('status')} on {ledger_entry.get('date', '')[:10]}{stale}. {ledger_entry.get('note', '')}\n"
        )
    if it["issues"]:
        w("Closes: " + ", ".join(f"[#{i['number']} {i['title']}]({i['url']})" for i in it["issues"]) + "\n")

    w("## 0. Predict before you read\n")
    w("Write these down *before* opening the diff, then compare with what's actually there:\n")
    w("- Which files and components do you expect it to touch, and which one is riskiest?")
    w("- What test would *you* write to prove it works?")
    w("- What is the simplest implementation you can imagine? Is the PR more complex than that, and why?\n")

    w("## 1. Author's description\n")
    w((it["body"] or "_No description._").strip() + "\n")

    w("## 2. Where it lands in the architecture\n")
    for comp, churn in sorted(it["by_comp"].items(), key=lambda kv: -kv[1]):
        c = profile.component_by_id[comp]
        w(f"- **{c.label}** ({c.layer}), {churn} lines")
        contract = contracts.get(comp)
        if contract and contract.responsibility:
            w(f"  - Responsibility: {contract.responsibility}")
        if contract and contract.does_not_own:
            w("  " + contract.does_not_own.replace("\n", "\n    "))
    w("")

    w("## 3. Suggested reading order\n")
    w("Types first, then the code that uses them, then the tests. The tests are where you check the author's claims.\n")
    for f in reading_order(it["files"], profile):
        tag = "" if f["type"] == "MODIFIED" else f" _{f['type'].lower()}_"
        w(f"1. `{f['path']}` +{f['add']}/-{f['del']} [{profile.component_by_id[f['comp']].label}]{tag}")
    w("")

    w("## 4. Signals to verify\n")
    if not it["flags"] and not it["smells"]:
        w("_No automated signals. That only means the cheap checks found nothing, not that the PR is safe._\n")
    for fl in it["flags"]:
        w(f"- [{fl['kind']}] {fl['msg']}")
    failing = (it.get("ci_checks") or {}).get("failing") or []
    if failing:
        w("\nFailing checks:\n")
        for r in failing:
            tags = ", ".join(t for t, on in (("required", r["required"]), ("also failing on main", r["on_main"])) if on)
            name = f"[{r['name']}]({r['url']})" if r.get("url") else r["name"]
            w(f"- {name}" + (f" ({tags})" if tags else ""))
    if it["smells"]:
        w("\nLine-level heuristics on added lines (false positives happen):\n")
        for s in it["smells"]:
            w(f"- [{s['kind']}] `{s['path']}:{s['line']}`: {s['msg']}  \n  `{s['code']}`")
    w("")

    w("## 5. Rules that apply\n")
    if not it["rules"]:
        w("_No instruction files match._\n")
    for name in it["rules"]:
        ins = instructions[name]
        w(f"- [{name}]({ins.url}) (`{', '.join(ins.patterns)}`): " + "; ".join(ins.headings))
    w("")

    w("## 6. Questions to answer before approving\n")
    seen: set[str] = set()
    kind = profile.authors.kinds.get(it.get("author_kind", ""))
    by_size = sorted(it["by_comp"], key=lambda c: -it["by_comp"][c])
    groups = [
        profile.questions,
        kind.questions if kind else [],
        *(profile.component_by_id[c].questions for c in by_size),
    ]
    for questions in groups:
        for q in questions:
            if q not in seen:
                seen.add(q)
                w(f"- [ ] {q}")
    w("")

    w("## 7. Existing discussion\n")
    reviews = ", ".join(f"@{r['author']}: {r['state']}" for r in it["reviews"]) or "none"
    requested = ", ".join("@" + r for r in it["requested"]) or "none"
    w(f"Reviews: {reviews}. Requested: {requested}. Conversation comments: {it['comments']}.\n")
    for t in it["unresolved"]:
        outdated = " (outdated)" if t["outdated"] else ""
        body = t["body"].replace("\n", " ")
        w(f"- `{t['path']}:{t['line']}`{outdated} @{t['author']}: {body}")
    w("")

    w("## 8. Related open PRs (shared files)\n")
    if not related:
        w("_None. This PR can be reviewed on its own._\n")
    for num, shared in related:
        w(f"- #{num} {titles.get(num, '')}: shares {len(shared)} file(s): " + ", ".join(f"`{s}`" for s in shared[:5]))
    w("")
    return "\n".join(out)
