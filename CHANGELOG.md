# Changelog

## Unreleased

### Added

- Repos on GitLab, Forgejo (Codeberg and other Forgejo or Gitea servers), and Azure DevOps, next to GitHub. A
  repo's config entry says where it lives (`forge = "gitlab"`; GitHub is the default), and `readthrough onboard
  --forge gitlab group/project` writes it. GitHub keeps going through `gh`; the others are read through their
  REST API with a token from the platform's CLI (`glab`, `az`) or an environment variable (`GITLAB_TOKEN`,
  `FORGEJO_TOKEN`, `AZURE_DEVOPS_EXT_PAT`), else anonymously, which is enough for public repos. Hosts and
  variable names go under `[forges.<name>]`; tokens never go in the config. Each adapter was checked against a
  real public repo, and the tests replay those recorded responses.
- What a forge can't tell us is said, not assumed: a warning on the dashboard and a *gap* signal on each affected
  PR when a platform has no review decision, no merge state, no diffs, no per-file line counts (Azure DevOps),
  no named required checks (GitLab), or when author associations need a token (Forgejo). CI that couldn't be read
  is its own state, "couldn't be read", never "passing" or "no CI".
- A repo can carry its profile as `readthrough.toml` in its forge's dot-directory (`.github/`, `.gitlab/`,
  `.forgejo/`, `.azuredevops/`) or at the root as `.readthrough.toml` or `readthrough.toml`. The dot-directory's
  wins when a repo has several, with a warning. Shared profile directories take `forge/owner/name.toml`.
- Rate-limit replies that name a `Retry-After` are waited for that long (capped by `rate_limit_wait_seconds`)
  instead of a fixed minute. gitlab.com throttles anonymous callers per IP address; the guide says so.
- `[tuning.forge] max_tree_files` caps how much of a repo's tree onboarding lists through an API that pages a
  hundred files at a time. A partial tree is reported as partial, and path patterns aren't judged against it.
- `scripts/probe_forge.py` exercises an adapter against a real repo and can record its responses for the tests.
- Deep reviews can run in Claude Code as well as the Copilot CLI. `agent = "auto" | "copilot" | "claude"` in
  `config.toml` chooses (`auto` takes the first one installed), with per-agent options under `[agents.<name>]`
  and per-repo flags in `agent_args` (which replaces `copilot_args`). Sessions started in either agent are
  linked to their PR by the marker in their first message and resumed in the agent that owns them; the PR
  panel says which agent each one ran in. `readthrough install skill` installs the skill for every agent
  found (`--agent` for one). Claude Code leaves no trace of an open session, so for its sessions the panel
  shows when they were last written rather than claiming they're closed.
- A study turn that ends in an error is written to the study log as a `failure` entry (by `readthrough`, naming
  the assistant and the error), so a "Quiz me" with no reply is never read as one you abandoned. The Study tab
  shows it as a line in the transcript.
- The Study tab shows your "what I missed" reflections and offers a **What I missed** button after a verdict
  that wasn't *correct*; before, reflections could be written only through the API and appeared only in the
  brief. The transcript opens at its end, where the latest exchange and the composer are, and follows new
  entries while you're there.

### Changed

- The config file is schema 2. Each repo's data lives under `repos/<forge>/<owner>/<name>/` (so
  `repos/github/microsoft/vscode/`), dashboard pages under `/r/<forge>/<owner>/<name>/`, and
  `readthrough://` links and the marker in Copilot prompts name repos the same way. There is no migration: move
  `repos/owner/name` to `repos/github/owner/name`, rename `[tuning.github]` to `[tuning.forge]`, and set
  `schema = 2`; the error message for an old file says the same. Sessions linked by the old `owner/name` marker
  are still found.
- `data.json` is schema 2: the raw GitHub rollup state (`ci`) is gone, `ci_state` has the new `unknown` value,
  and the payload carries the repo's `slug` and `forge`.
- Saving `config.toml` from the browser keeps `terminal`, `assistant`, and the plugin option tables; before, a
  save from the settings page dropped them.
- The onboarding drafter ranks source roots by recent churn rather than file count, and names a generic
  directory (`src`, `lib`, `core`) after its parent. Onboarding microsoft/vscode went from 76% coverage, refused,
  to 100%.
- A PR's worktree whose clone or data directory has moved since it was made (its `.git` file points at a gitdir
  that is gone) is set aside as `pr-<n>.detached-<time>` and made afresh, instead of every deep review and
  study turn failing with "not a git repository".

### Removed

- `github.py`. Its pieces are `forges/github.py` (the adapter), `process.py` (running programs, secrets), and
  `faults.py` (the error hierarchy, now shared with the REST adapters).

## 0.3.0 (2026-10-14)

The first release under the name readthrough, and the first with a public baseline. Earlier versions were a
private tool for one repository; this one is for any repository, and the history starts here.

### Added

- A rules-based study assistant that needs nothing installed. It asks the questions the repo's profile already
  carries (the shared ones, each touched component's, the author kind's, and one per signal), hints by pointing
  at files, and assembles a briefing from the PR's metadata. It never grades: after the last question about a
  component it shows that component's contract once, so you can check yourself, and the Study tab offers a
  "what I missed" note. `assistant = "auto" | "copilot" | "rules"` in `config.toml` chooses; `auto` takes Copilot
  when the `[study]` extra is installed.
- Every answer in the study log is tagged with the aids that came before it (a hint for that question, a
  briefing), computed from the log itself. The brief's summary says how many answers were unaided, and never
  shows a score unless Copilot graded.
- Terminal launchers for deep-review sessions are chosen by name: `terminal = "auto" | "wt" | "console" |
  "terminal-app" | "x-terminal-emulator" | "gnome-terminal"`, with per-launcher options under `[terminals.<name>]`
  (Windows Terminal takes a `profile`, a `window`, and `extra_args`).
- Shared profile directories: `[profile_sources.shared] dirs = [...]` lists directories holding
  `owner/name.toml` files, so a team can keep maps for repos that can't carry `.github/readthrough.toml`.
- Plugin names and options in `config.toml` are validated when the file is read, with a suggestion for a typo.
  Relative paths in options are taken from the config file's directory.
- `READTHROUGH_GH` and `READTHROUGH_GIT` name the program to run instead of the one on PATH (a path, or a JSON
  list for a command with arguments). The end-to-end tests use it; so can a wrapper script.
- CI on Linux, macOS, and Windows for Python 3.11 through 3.14, with a build job that installs the wheel into a
  clean environment and uploads the artifacts.
- Runnable examples under `examples/`, including one that uses only the standard library.

### Changed

- One package, `readthrough`, installed from the repository root. The PyRIT add-on package is gone, and with it
  the `readthrough.profiles` entry point: a repo's profile comes from an explicit path, your local draft, the
  repo's own `.github/readthrough.toml`, or a shared directory, in that order. An explicit `profile` path that
  doesn't exist is an error rather than a silent fall-through.
- Tokens from `gh auth token` are held as `Secret` values whose repr is `Secret(***)`, and any token this process
  has seen is blanked out of captured `gh` error output.
- Programs are resolved with `shutil.which` before they're run, so `.cmd` shims and wrappers are found on Windows.
- `gh` and `git` output is decoded as UTF-8 everywhere, including the PowerShell calls behind the Windows
  installers.
- Retry backoff and refresh jitter use their own seeded random generators, so neither can perturb the other and
  tests can pin them.
- The type checker is `ty`, pinned exactly; lint is ruff with every rule enabled and a reasoned ignore list.
- The logo is a page being read rather than a radar sweep. The URL scheme is `readthrough://`, the data directory
  `~/.readthrough`, the environment variables `READTHROUGH_HOME` and `READTHROUGH_CONFIG`, and the in-repo profile
  `.github/readthrough.toml`.

### Fixed

- A legitimately empty diff (a PR GitHub serves no diff text for) was never cached, so it was fetched again on
  every refresh. It's now remembered until the PR's head changes, like the "too large" answer.
- `drift()` took a profile it never read.
