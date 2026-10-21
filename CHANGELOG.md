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
- Azure DevOps pull requests get a diff and line counts. The platform serves neither, so the adapter assembles
  a unified diff from both versions of each changed file (two blob requests per file, cached by head commit
  like every diff, capped by `[tuning.forge] max_assembled_diff_files`), and the build counts added and deleted
  lines from it for any forge that reports none. Line heuristics now run on Azure DevOps, and the "no line
  counts" and "no API for whole-change diffs" warnings are gone; a PR over the cap, or a build with `--no-diffs`,
  keeps the line-counts gap and says so.
- Azure DevOps authors get an association and membership checks. The platform has no association, so one is
  derived: an author with a completed PR in the repo is `COLLABORATOR` (everyone who can open a PR there holds
  contribute rights, GitHub's collaborator), one on their first PR here is `FIRST_TIME_CONTRIBUTOR`; nobody is
  `MEMBER`. The profile's `internal_orgs` (the organization's users) and `internal_teams`
  (`organization/team`, through any nesting of groups, via the identities API) now work with a sign-in, so
  the "can't tell us the association" warning is gone and authors are classified the way they are on GitHub.
- A token a CLI prints is used only if it looks like one (a line of printable characters). A stand-in that
  printed a banner used to end up in an `Authorization` header, which `urllib` rejects by crashing the call.
- The study assistant and the onboarding drafter run on a **model runtime**, chosen machine-wide with
  `runtime = "auto" | "copilot" | "anthropic" | "openai"` and configured in `[runtimes.NAME]`. `copilot` is the
  Copilot SDK: GitHub Copilot through its sign-in, or with `provider = "openai" | "azure" | "anthropic"` a
  provider of your own, where the runtime never touches the CLI's GitHub login. `anthropic` speaks Claude's
  Messages API and `openai` the Responses API (OpenAI, or Azure OpenAI and Foundry with a key or `entra = true`
  for an Entra token from `az`), both over the standard library with nothing to install but a key. The
  Socratic behaviour, the tools, and what reaches the study log are the same on every runtime (`runtimes/`
  holds the engines; `assistants.py` the behaviour), so the GitHub forge and the AI behind studying are no
  longer one credential: a repo on Azure DevOps can be studied with no GitHub account at all. `auto` takes
  the Copilot SDK when installed, else the first runtime whose key is set; `doctor` names the one in use.
  Checked live on the same PR with all of GitHub Copilot (Claude Sonnet 5 through the SDK) and Azure OpenAI
  `gpt-4.1` through both the SDK's provider mode and the Responses API with an Entra token.
- Every model call is recorded in the repo's usage ledger (`usage.jsonl`), one line per call keyed by the
  provider's call id: the model, tokens in the provider's own units, Copilot's premium-request multiplier and
  AI-credit consumption when it was Copilot, who was billed (`copilot:<login>` or `<provider>:<host>`), and
  what for (`study` on a PR, or the onboarding `draft`). Units are never converted into one another and an
  unreported quantity is absent, not zero. `doctor` sums the last thirty days per repo.
- Study-log entries an assistant writes carry the model that reached them, and the brief says "assessed by
  gpt-4.1 (through Copilot)" rather than just "by Copilot", since a verdict's weight depends on who gave it.
- A study turn that ends in an error is written to the study log as a `failure` entry (by `readthrough`, naming
  the assistant and the error), so a "Quiz me" with no reply is never read as one you abandoned. The Study tab
  shows it as a line in the transcript.
- The Study tab shows your "what I missed" reflections and offers a **What I missed** button after a verdict
  that wasn't *correct*; before, reflections could be written only through the API and appeared only in the
  brief. The transcript opens at its end, where the latest exchange and the composer are, and follows new
  entries while you're there.
- `scripts/screenshots.py` takes the documentation screenshots from a repo you've built, through the live
  server and a headless browser, as WebP. The ones in `docs/` are of microsoft/vscode.

### Changed

- The study assistant that reads the code is named for what it is, a **model**, not for GitHub Copilot, which
  is now one of three runtimes it can run on. `assistant = "model"` in `config.toml` (the old `"copilot"` is
  refused with the list of names); the log's `by` is `model`, and every entry the model writes also records
  the `runtime` that carried it next to the `model` the runtime reported, so the brief says "assessed by
  gpt-4.1 via openai" from the entry itself and switching runtimes never relabels an old grade. Logs written
  before this say `by: copilot`, the assistant's former name; they're read as `model` without touching the
  file, and with no runtime claimed, since they never recorded one. The Study tab labels each reply with the
  model that wrote it, says what will answer next (the configured model, or the runtime when GitHub Copilot
  picks per call),
  and names a failed turn by its runtime rather than a model that never answered; "Study with Copilot" is
  "Study". Model-assisted onboarding is `readthrough onboard --assisted` (was `--copilot`) and the setup page's
  **Draft with a model**, which names the runtime it would use or says why none can run; a draft's
  `drafted_by` is `model` (profiles saying `copilot` still load) and its first line names the model and
  runtime. `readthrough study --by model` records a review agent's finding. A registry name that resembles
  nothing (a renamed plugin, say) now gets the list of valid names in the error instead of no hint.
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
- On GitHub the whole tree is read in one call, so `max_tree_files` no longer applies there and a repo the size
  of vscode is judged against all of its files.
- A profile found in the repo's data directory, or named by `profile = ...`, is labelled with a `~`-relative path
  on the map and in `doctor`, not an absolute one.
- A PR's worktree whose clone or data directory has moved since it was made (its `.git` file points at a gitdir
  that is gone) is set aside as `pr-<n>.detached-<time>` and made afresh, instead of every deep review and
  study turn failing with "not a git repository".
- The documentation images are WebP at 1.5× instead of PNG, a third of the size, and the logo is an SVG.

### Fixed

- The live dashboard took two to four seconds to answer a page request on a repo with a hundred PRs. The
  watcher thread recomputed every PR's agent sessions every three seconds while holding the payload lock, and
  for each PR it searched PATH for the agents again (several milliseconds per lookup on Windows), so every page
  load waited for a pass to finish. The agents are now resolved once per pass, and the scan runs without the
  lock and applies its findings under it: 5.5 s to 0.1 s per pass on microsoft/vscode, and the page in about
  100 ms (the browser's own parse and render of the 2.6 MB page is about 200 ms).
- A clone (or a model, or a runtime) set in config.toml or on the setup page now reaches a running study
  session. The server reloaded the repo's settings but the study engine and its assistant kept the Config they
  were built with, so a repo onboarded without a clone kept failing "No local clone configured" after one was
  added, until a restart; and a PR's checkout was made once per process, so a push to the PR between turns was
  invisible to the assistant's tools. The engine now takes the new Config in place, the assistant reads it on
  every turn, and the checkout follows the PR's head. Two further races closed on the way: two browser tabs (or
  a double-click) could start two turns on one PR, since the busy check wasn't atomic across handler threads;
  and the study assistant and a deep review launched from the page could `git worktree add` the same path at
  once, now serialized per worktree.
- The `anthropic` and `openai` runtimes retry what the provider calls temporary (429, 529 overloaded, 5xx, a
  dropped connection), waiting what `Retry-After` asks for or an exponential backoff with jitter, four attempts
  at most; a bad key or a bad request still fails at once with the provider's message.
- A profile's `repo` is the repo's path on its forge, however many segments that has. The parser demanded
  exactly `owner/name`, so onboarding an Azure DevOps repo (`organization/project/repository`) or a GitLab
  project in a subgroup drafted a profile and then refused to load it. The default title, and the directory
  `onboard` looks in for an existing clone, are the repo's own name (the last segment), not the project's.
- Azure DevOps projects called things like `Delivery Team` can be onboarded: a repo path's segments may have
  spaces between words. The slug keeps the literal name (so does the data directory); dashboard and
  `readthrough://` URLs carry it percent-encoded, and the marker in agent prompts carries it as written.
- A clone's remote is matched the way its forge writes URLs, so an Azure DevOps clone
  (`https://…/org/Delivery%20Team/_git/repo`, or `v3/org/project/repo` over SSH) is recognised by `onboard`,
  the setup page, and `doctor`; before, only the GitHub-shaped `…/owner/name(.git)` ending was.
- Login lists edited in the browser accept the email addresses Azure DevOps and GitLab use as logins.
- Pull requests with eight-digit numbers work in the dashboard. Azure DevOps numbers PRs per organization, and
  a big one is past 17 million, but every PR route in the live server, the `readthrough://` URL, and the marker
  that links agent sessions to PRs stopped at seven digits, so the Study and Brief tabs answered "not found".
  The bound is now nine digits, in one place.
- A PR's brief says "Open on Azure DevOps" (or GitLab, or Forgejo) rather than "Open on GitHub" for every
  forge, and its Files link, like the ones under each signal, opens the files tab the way that forge spells it.
- Azure DevOps sign-in no longer depends on which tenant `az` happens to default to. readthrough asks the
  organization for its tenant (an anonymous call it refuses, naming it; the request carries
  `X-TFS-FedAuthRedirect: Suppress`, so refusals are 401s rather than a sign-in page) and asks `az` for a
  token in that tenant, so an account signed in to several tenants is no longer refused by an organization
  outside its default one. When `az` has no sign-in there, `doctor` repeats what `az` said and its fix is a
  single-tenant login (`az login --tenant … --allow-no-subscriptions --scope …`), which prompts once; a plain
  `az login` with many accounts in Windows opens a window per tenant and most fail. A failed token lookup is
  retried after ten minutes, so a sign-in made after `doctor`'s advice is picked up by a running server.
- The setup page says how a repo is read before anything runs: for the forge picked, which CLI or API
  (the `gh` CLI for GitHub; the REST API, with the identities host, for Azure DevOps; the REST API for
  GitLab and Forgejo) and where the credential comes from, in the order tried, as each adapter declares
  (`Forge.access`, `Forge.credentials`). After **Check access**, and on an onboarded repo's settings, it
  says which credential is in use right now (`az (tenant …)`, `gh as <account>`, `anonymous`).
- An organization that refuses both the `az` identity and anonymous access now fails with an error that
  says how to sign in, instead of a bare refusal after a log line claiming anonymous access was fine. The
  live server answers a forge refusal during setup with that message (HTTP 502) rather than a traceback on
  stderr and a dropped connection, and anything else unexpected in a request with a 500 that names its
  error id.

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
