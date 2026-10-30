# Security

## Reporting a vulnerability

Report it privately through GitHub's vulnerability reporting for this repository:
<https://github.com/spencrr/readthrough/security/advisories/new>. Don't open a public issue for it. You'll hear
back from the maintainer in the advisory, and the fix is discussed and credited there before it is released.

Only the latest release is supported. If you can, check that the problem is still there on `main` before
reporting.

## What the tool trusts, and what it doesn't

readthrough runs on your machine and reads from code forges. The boundaries it defends, so you know what a
report is likely to be about:

- **The forge's data is written by strangers.** PR titles, descriptions, review comments, file paths, check
  names, and URLs are third-party text. Descriptions and comments are rendered through DOMPurify (which keeps
  ordinary links and images and drops scripts and handlers); everything else is escaped where it lands, and the
  links the page builds itself are only ever `http` or `https`. Every page carries a per-render nonce content
  security policy that runs no other script. Images in a description or a review thread may load from any
  `https` host: GitHub proxies them, but on another forge the image's host learns that someone opened the PR.
  Text a model wrote (the Study tab, the brief) is rendered without images, since an image URL is a way to
  send data out.
- **A repository's own profile is third-party configuration.** `readthrough.toml` in a repo you onboard is
  data: its ids and colors are validated, its regular expressions are compiled when the profile loads, and
  readthrough itself runs nothing from it. Two parts of it are shown to the models: the study guidance goes
  to the study assistant labelled as the maintainers' advice, and the review guidance and test command are
  printed for the review agent, whose instructions say to treat them as material and to show you any command
  before running it. The agent's own approval prompt is the gate there.
- **The local server is for your browser only.** It binds `127.0.0.1`, checks the `Host` header (so a page on
  another site can't reach it by DNS rebinding), requires a per-start token for every state-changing request,
  and refuses an `Origin` header that isn't its own when the browser sends one. Request bodies are capped
  (`limits.py`). Anyone else with an account on the same machine can reach the loopback port, as with any local
  development server.
- **Credentials are never stored.** GitHub is read through the `gh` CLI with its own credential store; the
  other forges use a token from their CLI (`glab`, `az`) or an environment variable you name in `config.toml`.
  The config holds names of variables, never values. Every secret the process has seen is blanked out of log
  lines, errors, and `doctor --bundle` on the way out.
- **Outside programs run by argument list**, never through a shell, and are resolved on `PATH` by name. The
  exceptions are the OS integrations that open a terminal window or register the scheduled task, which hand a
  fixed command with quoted values to the platform's own launcher.
- **Model runtimes see what you point them at.** The study assistant sends PR text, diffs, and the files it
  reads from your clone to the provider you configured (GitHub Copilot, Anthropic, OpenAI or Azure OpenAI),
  and to nobody else; what the model asks for is held to the clone and to each tool's schema. Usage is recorded
  locally in the repo's ledger.
- **`readthrough://` links** accept a strict grammar (a known forge, an onboarded repo, a PR number) and
  refuse anything else. Your browser asks before opening one; what it starts is a review session on that PR,
  in your review agent, under that agent's own permissions.

Problems in `gh`, `az`, `glab`, a review agent, or a model SDK belong with those projects; a report here that
turns out to be theirs will be forwarded if you ask.