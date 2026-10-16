---
name: readthrough
description: Lay out a repo's open pull requests (GitHub, GitLab, Forgejo, or Azure DevOps) on its architecture (interactive dashboard, burn-down plan, overlap graph) and run guided deep-review sessions where the reviewer predicts, explains back, and critiques a PR before any review is drafted. Use when the user asks to triage, lay out, prioritize, burn down, get up to speed on, study, or deeply review PRs, or says "run a deep review session for owner/name#N".
---

# Readthrough

Everything goes through the `readthrough` CLI. Every command takes `--repo owner/name` (or `--repo forge/owner/name` when the same name is onboarded on two forges); without it, the user's default repo is used. `readthrough repos` shows each repo and, in the dashboard data, `forge.label` says which platform it's on. Run `readthrough repos` to see which repos are onboarded. If the command is missing, tell the user to install it (see the readthrough guide).

Two modes:

1. **Dashboard**: refresh the dashboard and summarize it.
2. **Deep review**: a guided session on one PR. Success means the user can explain the PR and critique it, not that it gets approved quickly.

If the repo isn't onboarded, `readthrough` says so. Onboarding means checking an architecture map with the user, so don't do it silently: suggest `readthrough onboard owner/name` and stop.

## Mode 1: Dashboard

```powershell
readthrough --repo OWNER/NAME build --open
```

Read `data.json` from the repo's data directory (`readthrough config --repo OWNER/NAME` prints it) and summarize: lane counts, the `news` block (what changed since the last build), the top 3 entries in `sessions`, PRs flagged `requested` or `re-review`, and the hottest shared files. If `drift` is set, mention that the architecture map needs updating. Keep it to a short paragraph and a list.

## Mode 2: Deep review session

You may have been launched by the dashboard's review button. If so, this Copilot session is tied to the PR and the user will come back to it. If the conversation already has history, pick up where it left off. Don't start over.

**Ground rules.** These are what make this different from a rubber stamp:
- **Don't reveal your own findings until the user commits to theirs.** Each phase asks first and shows second.
- Use `ask_user` for every question, one at a time, and keep each turn short.
- Every claim you make cites `path:line` in the PR's head commit.
- The brief's heuristic signals are prompts to check, not conclusions. Confirm or dismiss each one against the code.
- Never approve, request changes, or post comments without explicit confirmation of the exact text.

In the commands below, `R` stands for `--repo OWNER/NAME`.

### Phase 0: Prepare (silent)

1. `readthrough R profile show`. This is the repo's architecture profile: its layers and components, where its component contracts live, its review guidance, and its test command. Follow the review guidance throughout.
2. `readthrough R brief N`. If the PR isn't found or the brief looks stale, run `readthrough R build --no-diffs` first. The brief's last section, **Study log**, holds evidence the user has already built, possibly from studying in the dashboard: predictions, quiz answers with verdicts, teach-backs, findings. Use it, starting with its **Handoff** line. Don't re-ask what they've already shown they understand; spend the time on what they got partial or wrong, and on unanswered questions.
3. `readthrough R worktree N`. This prints the worktree path, head SHA, merge-base, and the diff command. The user's own checkout is never touched. If the head SHA differs from the brief's, say so: the PR moved.
4. Read the full diff, the rule files listed under "Rules that apply", and the contract sections the brief quotes. For anything non-trivial, also read the callers of changed public functions (`git grep` in the worktree).
5. Silently form your own review: correctness bugs, responsibility bleed against the components' "does not own" lists, rule violations, and test adequacy (would the tests fail on the base branch?). Rank your top 1-3. Write them to the session workspace and **don't show them yet**.

### Phase 1: Predict

Show only the title, a 2-3 line summary of the author's description, and the components touched. Ask: *"Before we look at code, what do you think the riskiest part of this change is, and what test would you want to see?"* Record the answer in the study log, in their words: `readthrough R study N --prediction "<their answer>"`.

### Phase 2: Guided walk

Follow the brief's reading order, in chunks of at most about 5 files. For each chunk:
- Explain the **intent** in 2-3 sentences: why it changed, not a narration of the diff.
- Show the 1-2 most important hunks with `path:line`.
- Ask **one** check question tied to a component contract or an applicable rule. For example: "This module now writes to the database. Which component should own persistence?" Or: "What happens in this branch if the list is empty?"
- If the answer is wrong or vague, give a hint, not the answer. After a second miss, explain briefly and move on.

Tie the change to code the user already knows (base classes, sibling implementations) using `git grep` in the worktree.

### Phase 3: Teach-back

Ask the user to explain the whole PR in 3-4 sentences as if they were briefing a teammate. Record it with `readthrough R study N --teachback "<their words>"`. Name the specific gaps (a missed component, misunderstood control flow). If a gap matters, walk that part again.

### Phase 4: Critique duel

1. Ask: *"What are your concerns, if any? List them before I share mine."*
2. Show your Phase 0 findings side by side with theirs, grouped as **both found**, **only you**, **only me**. For each "only me" item, give the evidence (`path:line` and the rule or contract) so the user can disagree.
3. Settle disputed points empirically when that's cheap. Use the profile's test command (from `profile show`) in the worktree; the profile's review guidance says how to make sure the tests run the PR's code, not the user's checkout. To check whether the PR's tests actually exercise the change, run them against the merge-base source too.
4. Record each finding the user agreed with: `readthrough R study N --finding "major: <one line>" --at path:line --by copilot` (severities: blocker, major, minor, question). Use `--by you` for ones the user raised.

### Phase 5: Draft and decide

1. Draft review comments **only** for points the user agreed with, in their voice, each anchored to `path:line`. Aim for 2-3 high-signal comments, not 15.
2. Ask for the verdict: comment, approve, request changes, or not yet.
3. On explicit confirmation, post the review. On GitHub: `gh pr review N -R OWNER/NAME --comment|--approve|--request-changes --body-file <file>`; for inline comments, `gh api repos/OWNER/NAME/pulls/N/reviews` with a `comments` array; if `readthrough config` shows an `account` for the repo, run `gh` as that account (`$env:GH_TOKEN = gh auth token --user <account>`). On GitLab use `glab mr note` / `glab mr approve`; on Azure DevOps `az repos pr set-vote` and `az repos pr update`; on Forgejo, hand the user the drafted comments to post, since there is no signed-in CLI to post through.
4. Record the outcome. The ledger pins it to the head SHA, so new commits flag the PR again. If the live dashboard is open, the PR ticks off there within a few seconds:
   ```powershell
   readthrough R ledger N --status reviewed --note "<one line the user learned>"
   ```
   Statuses: `understood`, `reviewed`, `commented`, `approved`, `changes-requested`, `skipped`.
5. Offer to clean up with `readthrough R worktree N --remove`.

### Batch mode ("burn down session K")

Read `sessions[K-1]` from `data.json`. Open with a 3-line overview of how the session's PRs relate (shared files, shared contracts), so context carries from one PR to the next. Run Phases 1-5 for each PR, keeping Phase 2 light for PRs under about 50 lines. Finish with what the user learned and what's still open.
