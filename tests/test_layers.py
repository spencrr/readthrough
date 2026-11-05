"""The package's layers, as a test: a module imports, at module level, only from the layers below its own.

The table is the architecture written down, and the one place to change when a module moves. A new module
has to be placed in a layer before the suite passes. Imports inside functions aren't checked: they are the
deliberate ones, deferred so the CLI starts fast (``cli`` loading ``build`` and ``serve``) or because a
registry is read by the config loader that its plugins import (``config`` looking up ``FORGES``).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "readthrough"

LAYERS = (
    # what everything else may use, and which uses nothing of ours
    frozenset({"contract", "faults", "files", "limits", "model", "registry", "rules", "tuning"}),
    # the machine: processes, logs, the config file, the ledger, CI classification, terminal launchers
    frozenset({"ci", "config", "ledger", "logs", "pages", "process", "terminals"}),
    # the plugins and the records they fill: forges, runtimes, agents, profiles, the study log, usage
    frozenset({"agents", "forges", "membership", "profile", "runtimes", "study", "usage"}),
    # reading a PR: analysis, the brief, onboarding, review sessions
    frozenset({"analyze", "brief", "onboard", "review"}),
    # the assistants that study a PR or draft a profile, and the OS pieces that launch them
    frozenset({"assistants", "onboard_model", "system"}),
    # putting a dashboard together
    frozenset({"build", "engine", "web_setup"}),
    # the entry points
    frozenset({"cli", "serve"}),
)
LAYER_OF = {module: level for level, layer in enumerate(LAYERS) for module in layer}


def _unit(path: Path) -> str:
    """The module, or the subpackage, a file belongs to: ``forges/github.py`` counts as ``forges``."""
    parts = path.relative_to(SRC).with_suffix("").parts
    return parts[0] if parts[0] != "__init__" else ""


def _module_level_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "readthrough":
            dotted = node.module.split(".")
            found |= {alias.name for alias in node.names} if len(dotted) == 1 else {dotted[1]}
    return found - {"__version__"}


def test_every_module_is_placed_and_imports_only_from_below():
    units = {_unit(p) for p in SRC.rglob("*.py")} - {"", "__main__"}
    assert units == set(LAYER_OF), f"place these in a layer: {sorted(units ^ set(LAYER_OF))}"
    for path in SRC.rglob("*.py"):
        unit = _unit(path)
        if not unit or unit == "__main__":
            continue
        for target in _module_level_imports(path) - {unit}:
            assert LAYER_OF[target] < LAYER_OF[unit], (
                f"{path.relative_to(SRC)} imports {target}, which is not below it: move one, or defer the import"
            )
