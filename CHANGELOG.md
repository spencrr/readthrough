# Changelog

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
