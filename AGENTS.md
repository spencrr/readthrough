# Working on readthrough

One package, `src/readthrough`, standard library only at runtime (plus the `gh` CLI); the `[study]` extra adds the
GitHub Copilot SDK. Python 3.11 is the floor and what `.python-version` pins for development.

## Commands

```sh
uv sync                                  # environment with the dev group (tests, ruff, ty, playwright) and [study]
uv run python scripts/check.py           # the gate: ruff check, ruff format --check, ty check, pytest with coverage
uv run python scripts/check.py --fix     # same, letting ruff fix and format first
uv run python scripts/check.py --quick   # skip the browser tests (what the pre-commit hook runs)
uv run pytest tests/test_engine.py -q    # one file; -k NAME for one test; -m e2e / -m browser for those groups
uv run readthrough --help                # the CLI from the checkout
uv build                                 # sdist and wheel into dist/
git config core.hooksPath .githooks      # once per clone: run --quick before every commit
uv run python scripts/screenshots.py --repo microsoft/vscode --pr N   # redraw docs/img/*.webp from a built repo
```

CI runs the same gate on Linux, macOS, and Windows for every supported Python. Nothing passes locally that
would fail there except for the browser tests, which skip when no Chromium-family browser is installed.

## Conventions that aren't obvious

- **A failure is never mistaken for an answer.** "Couldn't check" must not become "no", "absent", or "nothing
  to see". A value a forge may not supply is a `model.Capability` (`known` / `unsupported` / `unavailable`);
  the build turns the last two into a `Warnings` entry and a per-PR `gap` signal, and `ci.classify` has an
  `unknown` state. Only `read_file` / `api_or_none` / `Rest.call(absent=...)` return `None`, and only for a 404.
- **Every forge is an adapter in `forges/`** producing `model.PullRequest` records; nothing outside reads a
  platform's API. GitHub goes through the `gh` CLI; the others speak REST over `urllib` (`forges.Rest`) with a
  token from the platform's CLI or an environment variable, else anonymously. A repo's identity is its slug,
  `<forge>/<path>` (data under `repos/<forge>/...`, pages under `/r/<forge>/...`); `forge` defaults to `github`.
  Verify an adapter against a real public repo with `scripts/probe_forge.py`, which also records the responses
  the replay tests in `test_forges.py` run on (identities and all but a few headers are scrubbed).
- **Every outside program runs through `process.run`** with an argument list, never a shell string. It resolves
  the program with `shutil.which` (so `.cmd` shims and wrappers work on Windows), decodes as UTF-8, applies a
  timeout, and classifies failures (`faults.classify_stderr`). `READTHROUGH_GH` / `READTHROUGH_GIT` /
  `READTHROUGH_GLAB` / `READTHROUGH_AZ` substitute a program: the tests use that to stand in for `gh` at the
  process boundary.
- **UTF-8 everywhere.** Every `open`, `read_text`, `write_text`, and `subprocess` call names `encoding="utf-8"`.
  Files the tool writes go through `files.write_atomic`.
- **Secrets never reach a log, an error, or a bundle.** Tokens are `process.Secret`s whose repr is
  `Secret(***)`, registered with `process.remember` so `process.redact` strips every seen one from captured
  output. `Rest` sends `Authorization` only to its own host. `config.toml` holds no credentials (the
  `[forges.*]` tables name hosts and environment variables), and `doctor --bundle` says so.
- **Stochastic steps take a seed.** Retry backoff (`forges.jitter`) and refresh jitter (`Dashboard(seed=...)`)
  each have their own `random.Random`; tests assert their distributions with a fixed seed and a stated margin.
- **Cache keys cover everything that changes the result.** Diffs are cached by PR number and head SHA, with
  separate markers for "too large" and "legitimately empty" so neither is re-asked until the head changes.
  Failures are never cached.
- **Pluggable things go through a registry** (`registry.Registry`): forges, profile sources, terminal
  launchers, study assistants. A plugin has a name, a one-line summary, an implementation, and a dataclass of
  options that the config loader validates with did-you-mean errors. Never add a name-based `if/elif` for one
  of these; what a forge can't do is a missing entry in its `capabilities`, which the build reads.
- **Settings have one home each.** Per-machine or per-repo choices live in `config.toml` (`config.py`);
  operational numbers are `[tuning.*]` with defaults and documentation in `assets/defaults.toml` (a test keeps
  the two in step); safety limits are constants in `limits.py` and change only by a deliberate commit.
- **The study log is append-only and truthful about authorship.** Every entry's `by` is who wrote it (`you`,
  `copilot`, `rules`; `readthrough` only for a `failure`, the record that a turn ended in an error). Only
  Copilot writes verdicts; the rules assistant never grades. Answers are tagged with the aids that preceded
  them (`hint`, `brief`) from the log itself, never asserted.
- **The pages are one served file.** `assets/web/` holds separate `.html`, `.css`, `.js` for editing;
  `build.page_source` inlines them, every script carries a per-render nonce, and the CSP runs nothing else.
  No inline `onclick=`; third-party HTML goes through DOMPurify. Vendored JS lives in `assets/vendor/` with
  its license and is listed in `build.VENDOR_SCRIPTS`.
- **Lint is `ruff` with `select = ["ALL"]`** and an ignore list that states each reason in `pyproject.toml`.
  Suppress per file (with a reason) rather than per line where a whole file legitimately differs. Types are
  checked by `ty`, pinned exactly; bump it deliberately.
- **Tests never touch the network, `gh`, git, Copilot, the registry, or the scheduler.** Reuse the fakes:
  `FakeForge` (`tests/fakes.py`, installed by the `fake_forge` fixture in place of `forges.make`), `Script`
  (`test_github_faults.py`), `tests/fake_gh.py` for the real executable, the recorded forge responses under
  `tests/fixtures/forges/` (`test_forges.py`), `RecordingAssistant` (`test_engine.py`), the fake Copilot SDK in
  `test_assistants.py`. PRs are described GraphQL-shaped by `make_pr` and normalized by `make_pull`.
- **Coverage is a ratchet.** `fail_under` in `pyproject.toml` only goes up.
- **Commits** are one logical change each, with a message a person would write, and no trailers.
