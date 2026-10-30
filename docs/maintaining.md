# Maintaining readthrough

How the code handles failure, where settings live, and how to check a change before it goes in. Read this before adding a forge call, a setting, or a page.

## Checking a change

The same gate runs on your machine and in CI. `.github/workflows/test.yml` is the gate as a reusable workflow (lint and types on Linux, tests on Linux, macOS, and Windows for every supported Python, then a build that installs the wheel into a clean environment and uploads it as the `dist` artifact); `ci.yml` calls it on every push and PR and adds `gate`, which is green only when everything in it was. `zizmor.yml` lints the workflows themselves and fails on any finding. `gate` and `zizmor` are the two checks the branch ruleset requires. The browser tests fail on CI if the runner has no Chrome or Edge, where locally they would skip:

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
| types | ty | Pinned to an exact version in the dev group, because it's pre-1.0 and its diagnostics change between releases. Bump it on purpose, with the fixes in the same commit. Covers `src`, `tests`, and `scripts`. Production code is written against `forges.Forge` and the `Protocol`s (`membership.Membership`, `assistants.Assistant`, `assistants.Harness`) rather than a concrete adapter, which is what lets the tests' fakes type-check. |
| tests | pytest with pytest-cov | Fails below the floor in `[tool.coverage.report] fail_under`. Raise the floor when coverage goes up; don't lower it to get a change in. |

The browser tests (`tests/test_browser.py`, marked `browser`) drive the real dashboard and setup page in headless Edge or Chrome through Playwright. They use whichever of those is installed and are skipped if neither is. They catch script errors and broken interactions that Python tests can't see. The first time they ran, they found a chart that drew negative-size shapes.

The end-to-end tests (`tests/test_e2e.py`, marked `e2e`) run the real `readthrough` executable as a subprocess, with `tests/fake_gh.py` standing in for `gh` through `READTHROUGH_GH`. That's the one place the whole program is exercised the way a user runs it: onboarding, a build, studying from the terminal, a second build that must hit the diff cache, `doctor`. The first time it ran, it found that a legitimately empty diff was never cached, so docs-only PRs were re-fetched on every refresh.

Tests never touch the network, `gh`, git, Copilot, the registry, or the scheduler. The fakes to reuse: `FakeForge` in `tests/fakes.py`, which the `fake_forge` fixture installs in place of `forges.make` (a whole build runs on it), `Script` in `test_github_faults.py` (scripted `gh` failures against the real GitHub adapter), the recorded forge responses under `tests/fixtures/forges/` that `test_forges.py` replays through `Rest.call`, `fake_gh.py` (the executable), `RecordingAssistant` in `test_engine.py`, the fake Copilot SDK and sessions in `test_assistants.py` and `test_onboard_model.py`, and the `live` server fixture in `test_serve.py`. The two stochastic steps, retry backoff and refresh jitter, have their own seeded generators and are tested as distributions in `test_jitter.py`.

The recordings come from `scripts/probe_forge.py --record`, which runs an adapter against a real public repo (codeberg.org/forgejo/forgejo, gitlab.com/gitlab-org/cli, dev.azure.com/powershell) and writes every response, including the refusals, keyed by request. Re-record when an adapter asks for something new; the tests then need their expectations refreshed, since the data is whatever the forge said that day. Only a few response headers are kept and known identity fields are scrubbed, so a recording holds nothing about whoever made it.

The screenshots in `docs/img/` come from `scripts/screenshots.py`, which serves a repo you've built through the live server, drives headless Edge or Chrome through Playwright at a fixed size in the light theme, and writes WebP (Pillow converts; Playwright writes only PNG and JPEG). They show microsoft/vscode, onboarded into a scratch home: `READTHROUGH_HOME=~/.readthrough-docs readthrough onboard microsoft/vscode --accept`, a build, a real study exchange on one PR, then `scripts/screenshots.py --repo microsoft/vscode --pr <n>`. Re-take them after a visible change to the pages.

## Failures

The rule underneath everything: **a failure is never mistaken for an answer.** "Couldn't check" must not become "no", "absent", or "nothing to see".

### Forge calls

Every platform is an adapter in `forges/` (`github.py`, `gitlab.py`, `forgejo.py`, `azure_devops.py`), registered in `forges.FORGES` and chosen per repo by `forge = "..."`. An adapter turns its API into the records in `model.py`: `PullRequest`, with `ChangedFile`s, `Review`s, `Thread`s, and `Check`s. Nothing outside `forges/` reads a platform's API, and the analysis reads nothing but those records.

A value an adapter may not be able to supply is a `Capability`: `known` (including a known empty value), `unsupported` (this forge has no such concept or endpoint), or `unavailable` (it has one, but the call failed this time), each with a reason. `PullRequest.gaps()` lists the ones that aren't known; the build turns them into a warning and a per-PR `gap` signal, and the analysis uses a neutral default (`ci.classify` has an `unknown` state for unreadable checks; a forge without line counts weighs files by count). What an adapter can do beyond the essentials is its `capabilities` set (`diffs`, `required_checks`, `default_branch_ci`, `rate_limit`, `memberships`, `accounts`); the build checks it before trying, and never guesses.

Two transports. GitHub goes through the `gh` CLI (`process.run`), which carries the user's sign-in, SSO, and proxies. The others speak REST through `forges.Rest`, a thin layer over `urllib`: UTF-8, a timeout on every call, errors classified by status, `Authorization` sent only to its own host, and a `Retry-After` honored when a rate limit names one. Credentials come from the platform's CLI (`glab auth status --show-token`, `az account get-access-token`) or an environment variable, else calls are anonymous; `Credential.source` says which, for `doctor`, and the token itself is a `process.Secret`. An `az` identity that the organization refuses is set aside for anonymous access once, with a log line, because public Azure DevOps projects answer anonymous calls they refuse to an outsider.

`faults.py` holds one error hierarchy for both transports, classified from a CLI's stderr (`classify_stderr`) or an HTTP status (`classify_http`):

| Error | Meaning | Retried? |
| --- | --- | --- |
| `NetworkError` | Connection reset or refused, DNS failure, proxy failure, a 5xx | yes, with backoff and jitter |
| `Timeout` | Hit `[tuning.forge] timeout_seconds` | yes |
| `RateLimited` | 429, or 403 with a rate-limit message | yes, after `Retry-After` or `rate_limit_wait_seconds`, whichever is shorter |
| `AuthError` | 401, 403, SSO not authorized, or Azure DevOps's sign-in page (a 203) | no |
| `NotFound` | 404, or GraphQL can't resolve the name | no |
| `TooLarge` | 406, a diff over the forge's size limit | no |
| `ForgeError` | anything else | no |

`Forge.retrying` does the retrying, and one process-wide semaphore caps how many calls run at once across every repo (`[tuning.forge] concurrency`). Each error has a one-line `.reason` for logs and banners, and `process.describe` names a CLI call readably (`graphql PullRequests #12`, `GET repos/o/r/contents/a.md`, `pr diff #42`).

When something may legitimately not exist, use `read_file`, `api_or_none`, or `Rest.call(absent=(NotFound,))`. They return `None` only for a 404 and raise everything else. Membership checks (`is_member`, a GitHub capability) follow the same rule: only a 404 means "not a member", and a failed check isn't cached.

Paginated data (a PR's files, checks, and review threads; a repo's tree) is fetched page by page up to the caps in `[tuning.forge]`. Anything cut short is recorded (`PullRequest.truncated`, the second value `tree()` returns), never dropped silently: onboarding stops judging path patterns "dead" against a partial tree and says why.

### Builds

Only the PR list is essential. `fetch_snapshot` lets everything else degrade: CI context, rule files, diffs, and membership checks each add a `Warnings` entry (logged once, shown as a banner) when they fail or when the forge can't do them, and `_partial_flags` puts a signal on each affected PR. Diffs are cached only when they were fetched successfully; a diff too large to serve gets a marker file, so it isn't asked for again until the PR's head changes.

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

`config.toml` has `schema = 2`, and `data.json` has `"schema": 2` (`build.DATA_SCHEMA`; its shape is declared in `contract.py`). Bump a schema only when a field changes meaning or goes away; adding a field doesn't need it. A config with an unknown schema is refused with a message to upgrade. A `data.json` with a different schema, or one that doesn't parse, is ignored by the server and rebuilt. The ledger and study logs are append-friendly JSON files without a schema field, so change them only by adding fields.

## The pages

The UI lives in `assets/web/`. Each page has its own `.html`, `.css`, and `.js`, and `theme.css` holds the color tokens and base styles they share. `build.page_source(name)` inlines them, and `render_html` and `render_setup` fill in the nonce, the vendored scripts, and the data, so what's served is still one self-contained file that works offline as a snapshot.

- Every script carries a per-render nonce, and the content security policy runs nothing else. Don't add inline event handlers (`onclick=`); use the delegated listeners.
- PR descriptions and comments are third-party HTML. They go through DOMPurify, and `test_browser.py` checks that an `onerror` attribute doesn't survive.
- Escape everything else you interpolate with `esc()`.
- Third-party JavaScript is vendored under `assets/vendor/` with its license, and listed in `build.VENDOR_SCRIPTS`. The page itself makes no requests to other sites; the images in a PR's description are the one exception, and `SECURITY.md` says what that means.
- Check both themes. New colors go in `theme.css` as tokens if both pages use them, otherwise at the top of the page's CSS.

### The contract with the server

`contract.py` declares everything that crosses between the page and the server, and `test_contract.py` holds both sides to it: a build with every optional part populated must have exactly the declared shapes (an added or dropped field fails the test until the contract says so), and the page's scripts must read only declared fields, listen only for declared events, handle every study event the server can send, and call only routes it serves. So a new field is three edits, in `analyze.py` or `build.py`, in `contract.py`, and in the page, and the test tells you which one you forgot.

The rules the contract states, in short: events are notifications and a GET is the truth, so after any gap (a reconnect, a client that fell behind and was sent `resync`, a server restart seen on `hello`) the page fetches everything it holds again rather than reasoning about what it missed; study entries have per-PR ids that only ever grow, so the page de-duplicates by id and a repeated event is harmless; one assistant turn runs per PR, and a second request gets 409. The shared files behind the API (the study logs, the ledger) are read and written under `files.locked`, a file lock that also covers another process, since a review agent's terminal writes to the same log as the server.

## Plugins

Six things are chosen by name in `config.toml` and go through `registry.Registry`: where PRs come from (`forges.FORGES`), where profiles come from (`profile.PROFILE_SOURCES`), which agent runs a deep review (`agents.AGENTS`), how a review session gets a window (`terminals.TERMINALS`), who answers in the Study tab (`assistants.ASSISTANTS`), and which model runtime carries it (`runtimes.RUNTIMES`). To add one:

1. Write a dataclass of options with defaults, or use `registry.NoOptions`. Field types can be `str`, `int`, `float`, `bool`, `Path`, `list[...]` of those, or `X | None`; the loader checks them and resolves relative paths against the config file.
2. Register the implementation with `REGISTRY.add("name", "one-line summary", options=Options)`. The name is what users write; the summary is what `doctor` and errors show.
3. Keep the precedence or `auto` policy where it is (`profile.PRECEDENCE`, `agents.AUTO_ORDER`, `terminals.AUTO_ORDER`, `assistants.AUTO_ORDER`); a plugin never decides its own priority.
4. Add a test in `test_registry.py` that builds its command or result without running anything, and one in `test_e2e.py` or the module's tests if it changes what a user sees.

A forge subclasses `forges.Forge`: class attributes for its name, label, dot-directory, repo shape, `capabilities`, and `access` and `credentials` (one line on what it reads through and the credential sources in the order tried, which the setup page shows before anything runs), then the fetching methods (`repo_info`, `fetch_prs`, `merged_prs`, `tree`, `read_file`, `list_dir`, `ci_context`, `fetch_diff`, and `is_member` when `memberships` is claimed), each producing the records in `model.py`. Use `forges.Rest` for HTTP and `credential()` for the token so timeouts, redaction, and retries come for free; mark what the platform can't say `unsupported` and what failed `unavailable`, with the reason. Before it ships, run `scripts/probe_forge.py` against a real public repo and record the responses for `test_forges.py`.

A review agent subclasses `agents.Agent`: how to start a session with a given id, name, and prompt and how to resume one by id; where its sessions live and how to read a session's first user message (the PR marker in it is what links the session to a PR); whether a session is open right now, when the agent leaves a trace of that (Copilot does, Claude Code doesn't, and the adapter says `None` rather than `False`); and where its skills go. What ships is what could be run here: Codex or Gemini CLI would be the same shape, verified against their session formats first.

A terminal launcher is a pure function from a `Command` to a `Spawn`; only the Windows ones run on the author's machine, so the others carry the invocation their terminal documents and a note to that effect. A study assistant implements `assistants.Assistant` (`turn` and `stop`) and writes to the log only through the `Harness` it's given, with its own name in `by`; it must never write a verdict unless it read the code to reach it. The model assistant borrows its engine from a `runtimes.Runtime` (`open` a `Conversation`, then `send` prompts and consume `Event`s: text, tool, usage, done, error); a runtime translates the assistant's `ToolSpec`s to its wire format, runs the tool loop, and reports usage in its provider's own units (`runtimes.Usage`), and never touches the log itself. Adding a provider means a new runtime module under `runtimes/` with a dataclass of options, registered in `RUNTIMES`, with a `name` (what the study log records as `runtime`), a `model` property (the configured model, or `None` when the service picks), and `describe()`; the assistant and the drafter need no change, and nothing in the UI or the log names a provider except through those.

## Releasing

A release is a `vX.Y.Z` tag on `main` (or on a `release/*` branch, for a patch to an older line). The tag starts `.github/workflows/release.yml`, which checks that the tagged commit is on one of those branches, that the tag is the version in `pyproject.toml`, and that `CHANGELOG.md` has a section for it; runs the whole gate again through `test.yml`; and then waits. Approving the `pypi` environment (Actions → the run → *Review deployments*) lets the last job run: it takes the wheel and sdist the gate built, attests their provenance, uploads them to PyPI through trusted publishing (no token anywhere), and creates the GitHub release with the changelog section as its notes. Nothing in that last job builds anything, and nothing starts it but a tag plus a click. Only an admin can create a `v*` tag or a `release/*` branch, and anyone else needs a reviewed pull request to land a commit on `main`, so a leaked push credential can burn runner minutes but can't publish to PyPI; a GitHub release someone else created for the tag ahead of the pipeline makes the publish job fail rather than pass, since it only leaves an existing release alone when it carries exactly the files it built.

1. Bump `version` in `pyproject.toml` and run `uv lock`, so the lockfile names the new version (`UV_LOCKED` in CI refuses a stale one). `readthrough.__version__` reads it from the installed package.
2. Add a `## X.Y.Z (YYYY-MM-DD)` section to `CHANGELOG.md`; the release notes are that section, up to the next heading.
3. Run `uv run python scripts/check.py`, commit, and push `main`. Wait for CI.
4. `git tag vX.Y.Z && git push origin vX.Y.Z`. Only a repository admin can create a `v*` tag, and once it exists it can't be moved or deleted. The same goes for `release/*` branches, and a non-admin lands a commit on `main` only through a pull request the admin has approved.
5. Approve the deployment when the run reaches `publish`. Then check it from the outside, on a downloaded file rather than a local build:

```sh
gh release verify vX.Y.Z                                    # the release is immutable and attested
gh release verify-asset vX.Y.Z readthrough-X.Y.Z-py3-none-any.whl   # this file is the published asset
gh attestation verify readthrough-X.Y.Z-py3-none-any.whl -R spencrr/readthrough --signer-workflow spencrr/readthrough/.github/workflows/release.yml   # built by release.yml in this repo
uv tool install readthrough==X.Y.Z && readthrough --version
```

If the run fails after PyPI accepted the files, fix what failed and re-run the failed job: the upload skips files PyPI already has, and the release step leaves an existing release alone. The tag can't be moved, so a mistake in the release itself means a new patch version.

For a patch to an older line when `main` has unfinished work: `git switch -c release/X.Y vX.Y.0`, cherry-pick with `-x`, release from there, and add the changelog section to `main` by hand.

The toolchain that builds a release is pinned, in two places: `uv` is whatever version the pinned `setup-uv` action ships a checksum for (`version: latest-known`, so Dependabot's bump of the action, after its cooldown, is what moves `uv`), and the build backend is an exact `build-constraint-dependencies` entry in `pyproject.toml`, bumped by hand like `ty`.