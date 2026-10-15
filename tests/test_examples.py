"""The examples keep running: each one as its own process, with nothing but the installed package."""

import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = sorted((Path(__file__).parents[1] / "examples").glob("*.py"))


@pytest.mark.parametrize("script", EXAMPLES, ids=[p.stem for p in EXAMPLES])
def test_example_runs(script: Path):
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "an example should print what it found"


def test_offline_example_needs_only_the_standard_library():
    text = (Path(__file__).parents[1] / "examples" / "offline_analysis.py").read_text(encoding="utf-8")
    imports = {line.split()[1].split(".")[0] for line in text.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "datetime", "tempfile", "pathlib", "readthrough"}
