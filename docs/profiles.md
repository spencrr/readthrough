# Writing a profile

A profile tells readthrough what a repo's architecture is. Onboarding drafts one for you, and a model can draft a better one, but the ones that pay off are edited by someone who knows the code. This page is about making those edits well. The [schema reference](../src/readthrough/assets/profile-reference.md) lists every key; the [test fixture profile](../tests/fixtures/courier.toml), for a made-up message-delivery library, is a complete worked example that uses every part of the schema.

## What it's for

Everything readthrough shows you is organized by the profile:

- **Layers** are the rows of the map, top to bottom. A PR that touches many layers is flagged as cross-cutting.
- **Components** are the boxes. Each changed file belongs to exactly one, and the burn-down plan groups sessions by component, so you build context on one part of the code at a time.
- **Contracts** (`responsibility` and `does_not_own`) appear in the PR panel and the brief for every component a PR touches. They're what turns "this looks fine" into "this target is now deciding when to retry, and retries belong to the attack." Both the quiz and the deep review ask questions about them.
- **Signals and smells** are the hints in the PR panel: paths and code patterns that deserve a second look in this repo.
- **Authors** decides who is internal.
- **Guidance** goes to the model, in the deep review and the Study tab: what reviewers here comment on, how to run the tests, what to emphasise when quizzing.

## Layers

Order layers the way a request flows, or the way dependencies point: entry points at the top (CLI, API, UI), then orchestration, then core building blocks, then shared foundations. Three to six layers is plenty. Put tests, docs, and infra in a last layer with `source = false`, so they don't make every PR look cross-cutting.

`flow = true` draws arrows between a layer's components from left to right. Use it when a layer is a pipeline (say, parse, then validate, then execute), and list the components in that order.

`color` tints a layer's label on the map; it takes a CSS color (`#1b7c83`, `rebeccapurple`, `rgb(...)`), nothing else. Every `id` in a profile (layers, components, signals, smells, author kinds) is a slug of letters, digits, `_`, `.`, and `-`, because ids end up in URLs, CSS classes, and data attributes.

## Components

Aim for the components a new maintainer would name if you asked them to sketch the system: ten to twenty for a large repo, five or six for a small one. A component is a responsibility, not a folder. Often they're the same, but when they aren't, follow the responsibility. Click keeps everything in one folder, and its components are still clearly the decorator API, the command model, argument parsing, and so on, one or two modules each.

Things to get right:

- **First match wins.** A file belongs to the first component whose `paths` match it. Put narrow patterns before broad ones: tests that live next to source, a sub-package that's its own component, generated files.
- **Globs.** `**` crosses directories and `*` doesn't. `src/app/score/**` is everything under `score/`; `src/app/*.py` is only the top-level modules.
- **Watch the report.** Onboarding prints how much recently changed code lands in each component. A component that never gets anything is a typo or is shadowed by an earlier one. A huge one usually wants splitting. Paths that fall into "other" are the next components to write.

## Contracts

`responsibility` is one or two sentences on what the component is for. `does_not_own` is the more useful one: the things people are tempted to do in this component that belong somewhere else, each naming where it belongs.

> **Responsibility:** Sends a prompt to a model and returns its response.
> **Does not own:** Deciding whether to retry or branch on the response (the attack), scoring it (scorers), or persisting it (memory).

Write them from the code and from what reviewers actually push back on. A contract that's wrong teaches the wrong thing to everyone who studies with it, so leave it out rather than guess.

If the repo already describes its components in a markdown document, with a section per component, point `[contracts]` at it and give each component a `contract` naming its heading. The document is fetched from the default branch on every build, so it stays in step with the maintainers. `labels` says which bold labels inside a section mean "responsibility" and "does not own".

Each component can also have `questions`: things a reviewer should ask whenever a PR touches it. They're added to the brief.

## Signals and smells

A **signal** fires when a PR changes certain paths. Good ones point at changes that are cheap to make and expensive to get wrong: database migrations, public exports, dependency manifests, CI config, auth code. `change_types = ["ADDED"]` limits one to new files, which is how you catch "added a new provider without registering it."

A **smell** is a regular expression over added lines, optionally limited to certain files. Use them for conventions reviewers keep repeating: a naming rule, a forbidden import, a call that belongs in another component. Keep them quiet. A smell that fires on every PR gets ignored, so make each one specific enough that a hit is worth reading. Write the regexes as TOML literal strings (single quotes) so backslashes survive.

**Paired files** catch two files that should change together and didn't: a notebook and the script it's generated from, a schema and its migration.

Every signal has a `kind`, which decides how loudly it's shown. `blocker`, `risk`, and `responsibility` signals appear on the burn-down plan's rows; `convention` and `author` ones also appear in the All PRs table; everything, `info` and `correctness` included, appears in the PR panel.

## Authors

The defaults follow GitHub's author association: owners, org members, and collaborators are internal; contributors are external; someone opening their first PR is a first-time contributor. That's right for most open-source repos. Change it when:

- your maintainers aren't all org members, or not every org member is a maintainer: list `internal_teams` (as `org/team-slug`) and they're checked per author;
- the repo lives in a huge org where membership says nothing: map `MEMBER` to `external` under `association` and rely on teams;
- a few people are exceptions: list their logins under `internal` or `external`. For exceptions that only matter to you, use `internal_authors` in your own config instead.

Each kind can have its own `signal` and `questions` under `[authors.kinds.<kind>]`, and you can add kinds of your own (a `partner` kind for a vendor's engineers, say) and map associations or teams to them.

## Guidance for the model

`[review] guidance` is what the deep review follows when it drafts comments: what this repo's reviewers comment on and what they don't. Distil it from the contributing guide or the repo's Copilot instructions, and keep it short. `test_cmd` is how to run tests, with `{clone}` and `{tests}` filled in. `[study] guidance` is extra context for the quiz.

`[rules] instructions_dir` points at a folder of `*.instructions.md` files with `applyTo` globs, the format VS Code and Copilot use. Each PR's brief lists the ones that apply to its files.

## Sharing a profile

A profile in your `~/.readthrough` only helps you. Two ways to share one:

**In the repo.** Commit it as `readthrough.toml` in the forge's dot-directory (`.github/readthrough.toml` on GitHub; `.gitlab/`, `.forgejo/`, or `.azuredevops/` elsewhere), next to CODEOWNERS and the other review conventions, or at the root as `.readthrough.toml` or `readthrough.toml`. Anyone onboarding the repo gets it automatically, and it changes through PRs like everything else. This is the right choice when the maintainers want it. If a repo ends up with more than one, the dot-directory's wins and the dashboard points out the others.

**In a shared directory.** When you can't or don't want to put it in the repo (it isn't your project, or the maintainers haven't taken it yet), keep it in a directory of profiles your team shares, one file per repo at `FORGE/OWNER/NAME.toml` (`OWNER/NAME.toml` also works for GitHub repos), and list that directory in each person's config:

```toml
# ~/.readthrough/config.toml
[profile_sources.shared]
dirs = ["~/src/team-profiles"]
```

A git repo of profiles is the natural shape: clone it anywhere, point `dirs` at the clone, and `git pull` is how maps get updated. Onboarding finds the file, `readthrough profile sources` shows it as `shared`, and your own local draft still wins while you're experimenting. The settings page's **Customize** copies a shared profile into a local draft; when the maintainers are ready to own it, **Propose upstream** turns it into the in-repo file.

## Who drafted it

Drafts record where they came from in a top-level `drafted_by` key: `"readthrough"` for the file-tree draft, `"model"` for a model's draft (the file's first line names the model and runtime; older profiles say `"copilot"`, the drafter's former name, which reads the same). The dashboard's map legend shows it, so anyone reading the map knows whether a person has been over it. Leave it when you edit a draft; delete it once the profile is really yours.

## Checking your work

The browser's settings page (the gear button in the dashboard) does all of this live: edit the TOML and the coverage report updates as you type, with a **+ component** button for each unmapped path and a map preview.

```powershell
readthrough profile check --file my-profile.toml   # is it valid?
readthrough onboard OWNER/NAME                     # coverage and problems against recent PRs
readthrough onboard OWNER/NAME --preview           # see the map
readthrough profile sources                        # which profile is actually in use
```
