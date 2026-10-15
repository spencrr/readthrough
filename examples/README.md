# Examples

Each one runs from a checkout with `uv run python examples/<name>.py`, or from an install with plain `python`.
None needs the network, `gh`, Copilot, or the `[study]` extra.

| Script | What it shows |
| --- | --- |
| `offline_analysis.py` | The analysis as a pure function: a profile plus PR data in, lanes, signals, line heuristics, a plan, and a brief out. Standard library only. |
| `custom_profile_source.py` | Extending readthrough through a registry: a new place profiles can come from, with options the config loader validates and did-you-mean errors for typos. |

For the whole tool against a real repository, follow the quick start in the [README](../README.md); for the
whole tool against a fake `gh`, see `tests/test_e2e.py`.
