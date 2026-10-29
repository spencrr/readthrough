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
  names, and URLs are third-party text. Descriptions and comments are rendered through DOMPurify; everything
  else is escaped where it lands, and links are only ever `http` or `https`. Every page carries a per-render
  nonce content security policy that runs no other script.
- **A repository's own profile is third-party configuration.** `readthrough.toml` in a repo you onboard is
  data, not code: its ids and colors are validated, its regular expressions are compiled when the profile
  loads, and nothing in it is executed.
- **The local server is for your browser only.** It binds `127.0.0.1`, checks the `Host` header (so a page on
  another site can't reach it by DNS rebinding), and requires a same-origin `Origin` and a per-start token for
  every state-changing request. Request bodies are capped (`limits.py`).
- **Credentials are never stored.** GitHub is read through the `gh` CLI with its own credential store; the
  other forges use a token from their CLI (`glab`, `az`) or an environment variable you name in `config.toml`.
  The config holds names of variables, never values. Tokens are redacted from logs, errors, and
  `doctor --bundle`.
- **Outside programs run by argument list**, never through a shell, and are resolved on `PATH` by name.
- **Model runtimes see what you point them at.** The study assistant sends PR text, diffs, and the files it
  reads from your clone to the provider you configured (GitHub Copilot, Anthropic, OpenAI or Azure OpenAI),
  and to nobody else. Usage is recorded locally in the repo's ledger.
- **`readthrough://` links** accept a strict grammar (a known forge, an onboarded repo, a PR number) and
  refuse anything else.

Problems in `gh`, `az`, `glab`, a review agent, or a model SDK belong with those projects; a report here that
turns out to be theirs will be forwarded if you ask.
