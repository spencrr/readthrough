"""Analyze pull requests against an architecture profile, with no network, no gh, and no extras.

readthrough's analysis is a pure function of a profile and the PR data: which component each file belongs
to, which lane the PR is in, what signals fire, and how long it would take to review. This script feeds it
two made-up PRs for a made-up repo and prints what it concludes, then writes the brief for one of them.

The PRs are built as ``readthrough.model.PullRequest`` records, the forge-neutral shape every adapter
(GitHub, GitLab, Forgejo, Azure DevOps) produces, so this is also what a new adapter has to fill in.

    uv run python examples/offline_analysis.py
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

from readthrough.build import Snapshot, analyze_snapshot
from readthrough.config import Config
from readthrough.model import Author, ChangedFile, PullRequest, known, unsupported
from readthrough.profile import parse_profile

PROFILE = parse_profile(
    {
        "schema": 1,
        "repo": "octo/courier",
        "title": "Courier",
        "layers": [
            {"id": "core", "label": "Core"},
            {"id": "support", "label": "Tests and docs", "source": False},
        ],
        "components": [
            {
                "id": "transports",
                "label": "Transports",
                "layer": "core",
                "paths": ["courier/transports/**"],
                "responsibility": "Deliver one envelope over one protocol.",
                "does_not_own": "Persistence: the pipeline saves to the store.",
                "questions": ["Does the transport avoid writing to the store?"],
            },
            {"id": "pipeline", "label": "Pipeline", "layer": "core", "paths": ["courier/pipeline/**"]},
            {"id": "tests", "label": "Tests", "layer": "support", "paths": ["tests/**"]},
            {"id": "docs", "label": "Docs", "layer": "support", "paths": ["docs/**"]},
        ],
        "smells": [
            {
                "id": "transport-store",
                "kind": "responsibility",
                "message": "transport touching the store",
                "path": "^courier/transports/",
                "line": r"\bStore\b",
            }
        ],
    }
)

NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.UTC)


def pr(
    number: int, title: str, files: list[tuple[str, int]], *, author: str, association: str, draft: bool = False
) -> PullRequest:
    """One pull request as the analysis sees it. Anything a forge can't supply is marked, not defaulted."""
    return PullRequest(
        number=number,
        title=title,
        url=f"https://github.com/octo/courier/pull/{number}",
        body="",
        draft=draft,
        created="2026-10-01T12:00:00Z",
        updated="2026-10-10T12:00:00Z",
        additions=sum(add for _, add in files),
        deletions=0,
        changed_files=len(files),
        head=f"{number:040x}",
        base="main",
        author=Author(author),
        files=[ChangedFile(path, add, 0) for path, add in files],
        last_commit="2026-10-10T12:00:00Z",
        association=known(association),
        decision=known("REVIEW_REQUIRED"),
        mergeable=known("MERGEABLE"),
        threads=known([]),
        checks=unsupported("this made-up forge has no CI"),  # shows up as a signal, never as "passing"
    )


DIFF_41 = """\
diff --git a/courier/transports/smtp.py b/courier/transports/smtp.py
--- a/courier/transports/smtp.py
+++ b/courier/transports/smtp.py
@@ -10,1 +10,3 @@ class SmtpTransport:
     def send(self, envelope):
+        for attempt in range(3):
+            Store.open().save(envelope)
"""


def main() -> None:
    snapshot = Snapshot(
        viewer="me",
        prs=[
            pr(
                41,
                "Retry SMTP sends",
                [("courier/transports/smtp.py", 40), ("tests/test_smtp.py", 12)],
                author="someone",
                association="CONTRIBUTOR",
            ),
            pr(42, "Describe the pipeline", [("docs/pipeline.md", 120)], author="me", association="MEMBER", draft=True),
        ],
        contracts={},
        instructions=[],
        required_checks={},
        main_failing=known(set()),
        diffs={41: DIFF_41},
        now=NOW,
    )
    result = analyze_snapshot(snapshot, PROFILE)
    for item in result["items"]:
        print(f"#{item['number']} {item['title']}")
        print(f"   lane {item['lane']}, primary component {item['primary']}, about {item['minutes']} min to review")
        for flag in item["flags"]:
            print(f"   signal [{flag['kind']}] {flag['msg']}")
        for smell in item["smells"]:
            print(f"   heuristic [{smell['kind']}] {smell['path']}:{smell['line']}: {smell['msg']}")
    print(f"\nsessions: {[s['prs'] for s in result['sessions']]}")

    # The brief is Markdown; writing it needs only a place to put it.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(home=Path(tmp), repo="octo/courier")
        cfg.briefs_dir.mkdir(parents=True)
        path = cfg.briefs_dir / "pr-41.md"
        path.write_text(result["briefs"][41], encoding="utf-8")
        print(f"\nbrief for #41 ({path.stat().st_size} bytes); its questions:")
        for line in result["briefs"][41].splitlines():
            if line.startswith("- [ ]"):
                print(f"   {line[6:]}")


if __name__ == "__main__":
    main()
