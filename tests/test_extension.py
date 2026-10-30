"""The Copilot canvas extension stays in step with the CLI it drives and the contract the page honours.

The extension is JavaScript the Copilot runtime runs, so its tests here are static: the constants it mirrors
from ``contract.py`` are the same, the CLI grammar it emits is the one ``cli.py`` parses, and both modules parse
as ES modules when a Node is around to say so (the runtime brings its own; none is a dependency of readthrough).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from readthrough import contract
from readthrough.cli import build_parser
from readthrough.system import CANVAS_FILES

EXTENSION = Path(__file__).resolve().parents[1] / "src" / "readthrough" / "assets" / "extension"


def _code(name: str) -> str:
    """The module without its comment lines, so prose about flags isn't read as flags."""
    lines = (EXTENSION / name).read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith(("//", "*")))


HELPER = _code("readthrough.mjs")
WIRING = _code("extension.mjs")


def _subcommands(parser) -> dict:
    for action in parser._actions:
        if isinstance(action.choices, dict):
            return action.choices
    raise AssertionError("the parser has subcommands")


def test_the_shipped_files_are_the_ones_install_copies():
    assert {p.name for p in EXTENSION.iterdir()} == set(CANVAS_FILES)


def test_the_panels_and_client_id_grammar_are_the_contracts():
    panels = re.search(r"export const PANELS = (\[[^\]]+\]);", HELPER)
    assert panels and json.loads(panels.group(1)) == list(contract.FOCUS_PANELS)
    client_re = re.search(r"const CLIENT_RE = /\^(.+)\$/;", HELPER)
    assert client_re and client_re.group(1) == contract.CLIENT_ID_RE
    wiring_re = re.search(r"pattern: /\^(.+?)\$/", WIRING)
    assert wiring_re and wiring_re.group(1) == contract.CLIENT_ID_RE


def test_every_cli_command_and_flag_the_extension_emits_exists():
    """The subcommands the helper runs are the CLI's, and every ``--flag=value`` it builds is an option one of
    them (or the main parser) takes, so a renamed flag fails here and not in a user's canvas."""
    parser = build_parser()
    subcommands = _subcommands(parser)
    used = set(re.findall(r'(?:repoArgs\(repo\), |run\(\[)"([a-z]+)"', HELPER))
    assert used == {"repos", "show", "brief", "study"}
    options = {o for a in parser._actions for o in a.option_strings}
    for command in used:
        options |= {o for a in subcommands[command]._actions for o in a.option_strings}
    emitted = {f"--{flag}" for flag in re.findall(r"--([a-z]+)=", HELPER)} | set(re.findall(r'"(--[a-z]+)"', HELPER))
    assert emitted and emitted <= options, sorted(emitted - options)


def test_the_extension_writes_nothing_to_the_log_or_the_ledger():
    """Actions read and steer; what the reviewer did is recorded through the chat's own CLI calls, which show."""
    assert not re.search(r'"(study|ledger)", .*--(prediction|teachback|note|finding|status)=', HELPER)
    assert "--by=" not in HELPER


@pytest.mark.skipif(shutil.which("node") is None, reason="no Node here; the Copilot runtime brings its own")
def test_both_modules_parse_as_es_modules():
    for name in CANVAS_FILES:
        subprocess.run(["node", "--check", str(EXTENSION / name)], check=True, capture_output=True, timeout=60)
