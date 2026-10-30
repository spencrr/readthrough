"""The vendored JavaScript is what its README says it is.

Dependabot can't see a vendored file, so the README's table is the record: name, version, license, and the
SHA-256 of the file as shipped. A changed file without a changed row fails here, in the commit that changed it.
"""

import hashlib
import re
from pathlib import Path

from readthrough.build import VENDOR_SCRIPTS

VENDOR = Path(__file__).resolve().parents[1] / "src" / "readthrough" / "assets" / "vendor"
ROW = re.compile(r"^\| `(?P<file>[^`]+)` \| .+ \| `(?P<sha>[0-9a-f]{64})` \|$", re.MULTILINE)


def test_every_vendored_file_matches_the_hash_its_readme_records():
    recorded = {m["file"]: m["sha"] for m in ROW.finditer((VENDOR / "README.md").read_text(encoding="utf-8"))}
    assert set(recorded) == set(VENDOR_SCRIPTS), "the README lists exactly the scripts the page inlines"
    for name, sha in recorded.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == sha, f"{name} differs from its row"
    shipped = {p.name for p in VENDOR.glob("*.js")}
    assert shipped == set(VENDOR_SCRIPTS), "no script is vendored without being inlined, or inlined without a row"
