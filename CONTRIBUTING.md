# Contributing

Thanks for looking. The short version: work on a `dev/<your login>/<topic>` branch, open a pull request against
`main`, and give the pull request a Conventional Commits title. [AGENTS.md](AGENTS.md) has the exact commands,
including the gate to run before you push, and the conventions that aren't obvious; the maintainers' guide
(`docs/maintaining.md`) has the rest, and [SECURITY.md](SECURITY.md) says how to report a vulnerability.

## Branches

`main` is what ships; `release/X.Y` carries a patch to an older line; `dev/<login>/<topic>` is where work in
progress lives, under your own GitHub login. Those are the only branch names the repository accepts (a ruleset
refuses others), and `dev/*` is yours to rebase or rewrite as you like. A change reaches `main` through a pull
request the maintainer has approved, rebased or squashed so the history stays linear.

## Commit messages

Subjects follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/): `type(scope)!: description`.
A check on every pull request reads the title (a squash makes it the commit) and each commit's subject (a rebase
lands them as they are); `uv run python scripts/commit_messages.py --range origin/main..HEAD` runs the same check
locally.

- The **type** says what kind of change it is: `feat` (something new for users), `fix` (a bug), `docs`,
  `refactor` (no change in behaviour), `test`, `build` (packaging and dependencies), `ci` (workflows and
  checks), `chore` (nothing else fits; a formatting-only change is one, since ruff owns formatting), `perf`,
  `revert`.
- The **scope** is optional, lowercase, and names the part touched: `canvas`, `serve`, `page`, `forges`,
  `study`, `cli`, `release`, `deps`, or whatever reads best. Don't agonize over it.
- A **breaking change** carries `!` after the type or scope, and says what breaks in the body.
- The **description** says what the change does. Lowercase reads best after the colon, but it isn't checked:
  Dependabot capitalizes, and the specification doesn't mind.
- The **body** is prose a person would write: what changed and why, the reasoning behind a choice, what was
  considered and rejected. Wrap it at about 110 columns. No trailers.

```text
fix(serve): refuse a focus for a PR that isn't open

The page would have shown a toast and moved on, but the caller had no way to tell the request was pointless.
The server now answers 404 for a number that isn't in the current payload, and `readthrough show` says so.
```
