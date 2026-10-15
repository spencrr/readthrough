# Maintaining readthrough

How the code handles failure, where settings live, and how to check a change before it goes in. Read this before adding a GitHub call, a setting, or a page.

## Checking a change

The same gate runs on your machine and in CI (`.github/workflows/ci.yml`: lint and types on Linux, tests on Linux, macOS, and Windows for every supported Python, then a build that installs the wheel into a clean environment):

```sh
uv run python scripts/check.py          # lint, format, types, tests with the coverage floor
uv run python scripts/check.py --fix    # let ruff fix and format first
uv run python scripts/check.py --quick  # skip the browser tests
```

Every gate runs even if an earlier one fails, so one pass lists everything to fix. Turn on the pre-commit hook once per clone and it runs `--quick` before each commit:

```sh
git config core.hooksPath .githooks
```

`git commit --no-verify` skips it when you really need to.

The gates:

| Gate | Tool | Notes |
| --- | --- | --- |
| lint | ruff | `select = ["ALL"]` in `pyproject.toml`, with an ignore list that gives each rule's reason. Prefer fixing a finding; suppress per file (with a reason) when a whole file legitimately differs, and per line only for a single call that can't be written another way. |
| format | ruff format | |
| types | ty | Pinned to an exact version in the dev group, because it's pre-1.0 and its diagnostics change between releases. Bump it on purpose, with the fixes in the same commit. Covers `src`, `tests`, and `scripts`. Production code accepts the `Protocol`s in `github.py` (`FileReader`, `Reader`, `Fetcher`) and `membership.Runner` rather than the concrete `GitHub`, which is what lets the tests' fakes type-check. |
| tests | pytest with pytest-cov | Fails below the floor in `[tool.coverage.report] fail_under`. Raise the floor when coverage goes up; don't lower it to get a change in. |

The browser tests (`tests/test_browser.py`, marked `browser`) drive the real dashboard and setup page in headless Edge or Chrome through Playwright. They use whichever of those is installed and are skipped if neither is. They catch script errors and broken interactions that Python tests can't see. The first time they ran, they found a chart that drew negative-size shapes.

The end-to-end tests (`tests/test_e2e.py`, marked `e2e`) run the real `readthrough` executable as a subprocess, with `tests/fake_gh.py` standing in for `gh` through `READTHROUGH_GH`. That's the one place the whole program is exercised the way a user runs it: onboarding, a build, studying from the terminal, a second build that must hit the diff cache, `doctor`. The first time it ran, it found that a legitimately empty diff was never cached, so docs-only PRs were re-fetched on every refresh.

Tests never touch the network, `gh`, git, Copilot, the registry, or the scheduler. The fakes to reuse: `FakeGitHub` in `test_cli.py` (a whole build), `Script` and `StubGh` in `test_github_faults.py` (scripted `gh` failures), `fake_gh.py` (the executable), `RecordingAssistant` in `test_engine.py`, the fake Copilot SDK and sessions in `test_assistants.py` and `test_onboard_copilot.py`, and the `live` server fixture in `test_serve.py`. The two stochastic steps, retry backoff and refresh jitter, have their own seeded generators and are tested as distributions in `test_jitter.py`.

## Failures

The rule underneath everything: **a failure is never mistaken for an answer.** "Couldn't check" must not become "no", "absent", or "nothing to see".

### GitHub calls

All `gh` access goes through `github.py`. `run()` puts a timeout on every call, so a hung call can't stall a repo. `classify()` turns `gh`'s stderr into one of these errors:

| Error | Meaning | Retried? |
| --- | --- | --- |
| `GhNetworkError` | Connection reset or refused, DNS failure, proxy failure, GitHub 5xx | yes, with backoff and jitter |
| `GhTimeout` | Hit `[tuning.github] timeout_seconds` | yes |
| `GhRateLimited` | 429, or 403 with a rate-limit message | yes, after `rate_limit_wait_seconds` |
| `GhAuthError` | 401, 403, SSO not authorized | no |
| `GhNotFound` | 404, or GraphQL can't resolve the name | no |
| `GhTooLarge` | 406, a diff over GitHub's size limit | no |
| `GhError` | anything else | no |

`GitHub.run` does the retrying, and `_gh_slots` caps how many calls run at once across every repo. Each error has a one-line `.reason` for logs and banners, and `describe()` names the call readably (`graphql PullRequests #12`, `GET repos/o/r/contents/a.md`, `pr diff #42`).

When something may legitimately not exist, use `api_or_none` or `raw_or_none`. They return `None` only for a 404 and raise everything else. Membership checks follow the same rule: only a 404 means "not a member", and a failed check isn't cached.

Paginated data (a PR's files, checks, and review threads) is fetched page by page up to the caps in `[tuning.github]`. Anything cut short is recorded in `pr["truncated"]`, never dropped silently.

### Builds

Only the PR list is essential. `fetch_snapshot` lets everything else degrade: CI context, rule files, diffs, and membership checks each add a `Warnings` entry (logged once, shown as a banner) when they fail, and `_partial_flags` puts a signal on each affected PR. Diffs are cached only when they were fetched successfully; a diff too large to serve gets a marker file, so it isn't asked for again until the PR's head changes.

`build()` is the boundary. It tags every log line inside with the repo and a build id, times each step, and turns any failure into `BuildFailed`, which carries the step, the cause, an error id, and how long the build ran. The failure is logged there exactly once: one line for an expected GitHub error, a traceback for anything else. Callers show the error id and don't log it again.

The live server (`serve.py`) records each repo's last error, its last success, and how many refreshes in a row have failed. It retries a failed repo sooner (`retry_after_failure_minutes`), staggers the repos' first refreshes, and jitters intervals so they drift apart. The dashboard banner, `/api/health`, and `readthrough doctor` all read that status.

`cli.main` catches anything that escapes a command. Expected errors print one line. Anything else gets an error id, a traceback in the log, and a pointer to `readthrough doctor --bundle`.

## Logging

`logs.py` sets up the `readthrough` logger: `readthrough.log` (rotated) always gets timestamps, and the console gets them only for `serve`. Rules:

- Inside a build, `logs.context(repo, build_id)` tags every line. Use `logs.pool_map` instead of a bare `ThreadPoolExecutor`, so worker threads keep the tags.
- Log a failure once, where it's handled, not at every level it passes through.
- Expected failures are one `warning` line with the reason. Use `log.exception` only for things that are bugs.
- Put structured fields in `extra=` (`error_id`, `step`, `duration_s`). The JSON format (`[tuning.logging] format = "json"`) emits them as keys.

## Settings

There are three kinds of value, and each has one home:

| Kind | Where | Changed by |
| --- | --- | --- |
| What you'd configure per machine or repo (accounts, clones, limit, models) | `config.toml`, read by `config.py` | the user, the settings page |
| Operational tunables (timeouts, retries, thresholds, weights, sizes) | `tuning.py` (types and ranges) and `assets/defaults.toml` (defaults and documentation) | `[tuning.*]` tables in `config.toml` |
| Safety limits | `limits.py` | a code change, deliberately |

`defaults.toml` is the source of truth for defaults and is what `readthrough config --defaults` prints. A test fails if it and the dataclasses disagree, or if a setting has no comment.

To add a tunable:

1. Add a field to the section's dataclass in `tuning.py`, with `_r(low, high)` for a number or `_choice(...)` for a string. It has no default in Python.
2. Add it to the section in `assets/defaults.toml`, with a comment that says what it does in terms a user would recognize.
3. Read it as `cfg.tuning.SECTION.NAME` (per repo) or `settings.tuning.SECTION.NAME` (global). Pass it down as an argument rather than reading config deep inside a helper, so tests can set it directly.
4. If it should be settable per repo, the section must be in `REPO_SECTIONS`.

Make a value tunable when someone could reasonably want a different one: network conditions, repo size, team habits. If changing it would loosen a safeguard (request size, accepted input shapes, what the browser may edit), it belongs in `limits.py` with a comment saying what it protects.

### File formats

`config.toml` has `schema = 1`, and `data.json` has `"schema": 1` (`build.DATA_SCHEMA`). Bump a schema only when a field changes meaning or goes away; adding a field doesn't need it. A config with an unknown schema is refused with a message to upgrade. A `data.json` with a different schema, or one that doesn't parse, is ignored by the server and rebuilt. The ledger and study logs are append-friendly JSON files without a schema field, so change them only by adding fields.

## The pages

The UI lives in `assets/web/`. Each page has its own `.html`, `.css`, and `.js`, and `theme.css` holds the color tokens and base styles they share. `build.page_source(name)` inlines them, and `render_html` and `render_setup` fill in the nonce, the vendored scripts, and the data, so what's served is still one self-contained file that works offline as a snapshot.

- Every script carries a per-render nonce, and the content security policy runs nothing else. Don't add inline event handlers (`onclick=`); use the delegated listeners.
- PR descriptions and comments are third-party HTML. They go through DOMPurify, and `test_browser.py` checks that an `onerror` attribute doesn't survive.
- Escape everything else you interpolate with `esc()`.
- Third-party JavaScript is vendored under `assets/vendor/` with its license, and listed in `build.VENDOR_SCRIPTS`. The page makes no requests to other sites.
- Check both themes. New colors go in `theme.css` as tokens if both pages use them, otherwise at the top of the page's CSS.

## Plugins

Three things are chosen by name in `config.toml` and go through `registry.Registry`: where profiles come from (`profile.PROFILE_SOURCES`), how a review session gets a window (`terminals.TERMINALS`), and who answers in the Study tab (`assistants.ASSISTANTS`). To add one:

1. Write a dataclass of options with defaults, or use `registry.NoOptions`. Field types can be `str`, `int`, `float`, `bool`, `Path`, `list[...]` of those, or `X | None`; the loader checks them and resolves relative paths against the config file.
2. Register the implementation with `REGISTRY.add("name", "one-line summary", options=Options)`. The name is what users write; the summary is what `doctor` and errors show.
3. Keep the precedence or `auto` policy where it is (`profile.PRECEDENCE`, `terminals.AUTO_ORDER`, `assistants.AUTO_ORDER`); a plugin never decides its own priority.
4. Add a test in `test_registry.py` that builds its command or result without running anything, and one in `test_e2e.py` or the module's tests if it changes what a user sees.

A terminal launcher is a pure function from a `Command` to a `Spawn`; only the Windows ones run on the author's machine, so the others carry the invocation their terminal documents and a note to that effect. A study assistant implements `assistants.Assistant` (`turn` and `stop`) and writes to the log only through the `Harness` it's given, with its own name in `by`; it must never write a verdict unless it read the code to reach it.

## Releasing

1. Bump `version` in `pyproject.toml`. `readthrough.__version__` reads it from the installed package.
2. Add a section to `CHANGELOG.md`.
3. Run `uv run python scripts/check.py`, then commit and tag `vX.Y.Z`. CI's build job uploads the sdist and wheel for the tag.
