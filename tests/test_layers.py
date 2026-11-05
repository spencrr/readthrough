"""The package's layers, as a test: a module imports, at module level, only from the layers below its own.

The table is the architecture written down, and the one place to change when a module moves. A new module
has to be placed in a layer before the suite passes. Imports inside functions aren't checked: they are the
deliberate ones, deferred so the CLI starts fast (``cli`` loading ``build`` and ``serve``) or because a
registry is read by the config loader that its plugins import (``config`` looking up ``FORGES``).
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Iterator
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


def _module_scope(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Every statement that runs at import time: the module's own, and those inside its if/try/with blocks,
    but not the bodies of functions and classes."""
    for node in body:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        blocks = [getattr(node, name, []) for name in ("body", "orelse", "finalbody")]
        blocks += [handler.body for handler in getattr(node, "handlers", [])]
        for block in blocks:
            yield from _module_scope(block)


def _module_level_imports(source: str, package: tuple[str, ...]) -> set[str]:
    """The package units a file imports at module level, in either spelling, absolute or relative.
    ``package`` is the file's own, ``("forges",)`` for ``forges/github.py``, for resolving relative imports."""
    found: set[str] = set()
    for node in _module_scope(ast.parse(source).body):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[1] for a in node.names if a.name.startswith("readthrough.")}
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # from . import x / from .x import y, resolved against the file's own package
                base = list(package[: len(package) - (node.level - 1)]) + (node.module or "").split(".")
                base = [p for p in base if p]
            elif node.module and node.module.split(".")[0] == "readthrough":
                base = node.module.split(".")[1:]
            else:
                continue
            found |= {base[0]} if base else {a.name for a in node.names}
    return found - {"__version__"}


def test_the_collector_sees_every_spelling_of_an_import():
    source = textwrap.dedent("""
        from readthrough import serve
        import readthrough.build
        from readthrough.engine import StudyEngine
        from . import web_setup
        from .gitlab import Rest
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from readthrough import cli
        try:
            import readthrough.study
        except ImportError:
            from readthrough import usage
        def later():
            from readthrough import system  # deferred on purpose: not module level
        """)
    seen = _module_level_imports(source, ("forges",))
    assert seen == {"serve", "build", "engine", "forges", "cli", "study", "usage"}


def test_every_module_is_placed_and_imports_only_from_below():
    units = {_unit(p) for p in SRC.rglob("*.py")} - {"", "__main__"}
    assert units == set(LAYER_OF), f"place these in a layer: {sorted(units ^ set(LAYER_OF))}"
    for path in SRC.rglob("*.py"):
        unit = _unit(path)
        if not unit or unit == "__main__":
            continue
        imported = _module_level_imports(path.read_text(encoding="utf-8"), path.relative_to(SRC).parts[:-1])
        for target in imported - {unit}:
            assert LAYER_OF[target] < LAYER_OF[unit], (
                f"{path.relative_to(SRC)} imports {target}, which is not below it: move one, or defer the import"
            )
