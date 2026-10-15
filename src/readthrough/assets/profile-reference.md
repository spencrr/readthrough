# Profile reference (schema 1)

A profile is a TOML file describing a repo's architecture. Top-level keys must come before any `[table]` or `[[array]]` header.

```toml
schema = 1                      # required
repo = "owner/name"             # required
title = "Name"                  # shown in the dashboard header
drafted_by = "copilot"          # optional: "readthrough" or "copilot", whoever wrote the first draft (shown on the map)
read_order = ["api", "core"]    # optional: component ids in the order a reviewer should read them
```

## Layers (at least one)

Rows of the architecture map, top to bottom.

```toml
[[layers]]
id = "entry"
label = "Entry points"
flow = false        # true draws arrows between this layer's components, left to right
source = true       # false for tests/docs/infra: excluded from "cross-cutting" and sizing
color = "#2f6f9f"   # optional
```

## Components (at least one)

**First match wins**, so put narrow patterns (tests inside source trees, a sub-package) before broad ones. Anything unmatched falls into an implicit `other` component.

```toml
[[components]]
id = "scorers"                       # unique, kebab-case
label = "Scorers"
layer = "core"                       # a layer id
paths = ["src/app/score/**", "src/app/scoring.py"]   # globs: ** spans directories, * stays in one
contract = "Scorers"                 # optional: heading in the contracts doc (see [contracts])
responsibility = "Turns a response into a score."            # optional, used when there's no contract doc
does_not_own = "Deciding what to do with a score (the caller branches)."
questions = ["Does the score type match what callers expect?"]   # asked when a PR touches this component
```

Write `responsibility` and `does_not_own` in plain sentences. `does_not_own` is what makes "responsibility bleed" reviewable: name the neighbouring component that owns each thing.

## Contracts and rules (optional)

```toml
[contracts]
source = "docs/architecture.md"   # markdown file in the repo; each component's section is found by its `contract` heading
labels = { responsibility = "Responsibility", does_not_own = "Does not own" }   # the bold labels inside each section

[rules]
instructions_dir = ".github/instructions"     # *.instructions.md files with applyTo globs, matched to changed files
guidance_file = ".github/copilot-instructions.md"
```

## Review and study guidance (optional)

```toml
[review]
guidance = '''What reviewers here comment on, and what they don't.'''
test_cmd = "{clone}/.venv/bin/python -m pytest {tests} -q"   # {clone} and {tests} are filled in

[study]
guidance = '''Extra context for the study quiz.'''
```

## Signals, smells, paired files

Each has a `kind`: risk, blocker, convention, responsibility, correctness, info, author, or you.

```toml
[[signals]]            # raised when a PR changes matching paths
id = "migration"
kind = "risk"
message = "Database migration: check it's reversible"
paths = ["**/migrations/**"]
change_types = ["ADDED"]   # optional: ADDED, MODIFIED, DELETED, RENAMED

[[smells]]             # regex over added lines in the diff
id = "print"
kind = "convention"
message = "print() in library code"
path = '^src/'         # regex on the file path
line = '^\s*print\('   # regex on the added line (use TOML literal strings for regexes)

[[paired_files]]       # e.g. notebooks that must be regenerated with their .py source
id = "notebook-pair"
kind = "convention"
message = "Changed the .py but not the .ipynb"
under = "doc/"
extensions = [".py", ".ipynb"]
```

## Checks, weights, overlap

```toml
[checks]
tests_component = "tests"            # component id that holds tests (default "tests" if it exists)
test_paths = ["tests/**"]
no_tests_min_lines = 20              # flag source changes bigger than this with no test changes
cross_cutting_layers = 3             # flag PRs spanning this many source layers
large_lines = 800
stale_days = 21

[weights]
capped_paths = ["**/*.lock", "**/*.ipynb"]   # generated files: count at most capped_lines toward size
capped_lines = 20

[overlap]
ignore = ["**/__init__.py"]          # files every PR touches; don't count as overlap
non_source = ["tests/**", "docs/**"] # shown dimmer in the overlap graph
```

## Authors

Decides who's internal (a maintainer) and who's external. Rules apply in this order: bots, the `internal`/`external` login lists, team and org membership, then GitHub's author association.

```toml
[authors]
internal = ["alice"]                  # always internal
external = ["vendor-bot-account"]     # always external
bots = ["renovate"]                   # in addition to GitHub Bot accounts
internal_teams = ["owner/core-team"]  # members are internal (needs read:org)
internal_orgs = ["owner"]             # members are internal
association = { MEMBER = "internal", COLLABORATOR = "internal", CONTRIBUTOR = "external", NONE = "first-time" }
unknown = "external"

[authors.kinds.first-time]            # optional: tune a kind's label, badge, signal, review questions
label = "first-time contributor"
questions = ["Did a maintainer ask for this change?"]
```

An empty `[authors]` section is valid and means "use the defaults": OWNER, MEMBER, and COLLABORATOR are internal; CONTRIBUTOR is external; NONE (first PR) is first-time.
