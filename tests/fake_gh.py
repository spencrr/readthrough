"""A stand-in for the GitHub CLI, for tests that run the real ``readthrough`` executable.

Point ``READTHROUGH_GH`` at it (``command()`` gives the value) and every ``gh`` call the tool makes is
answered from here, offline: a few open PRs for one repo, a diff, no optional files, and a default
branch whose CI is green. Each call is recorded under the ``$READTHROUGH_FAKE_GH_LOG`` directory so a
test can check what was asked (``calls()`` reads them back). Anything unexpected exits 1 with a message
on stderr, the way ``gh`` would.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

REPO = "octo/courier"
VIEWER = "me"

PRS = [
    {
        "number": 41,
        "title": "FIX: retry SMTP sends on transient errors",
        "url": f"https://github.com/{REPO}/pull/41",
        "body": "Retries the send three times with backoff.",
        "bodyHTML": "<p>Retries the send three times with backoff.</p>",
        "isDraft": False,
        "createdAt": "2026-10-01T12:00:00Z",
        "updatedAt": "2026-10-10T12:00:00Z",
        "additions": 48,
        "deletions": 6,
        "changedFiles": 2,
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "reviewDecision": "REVIEW_REQUIRED",
        "mergeable": "MERGEABLE",
        "authorAssociation": "CONTRIBUTOR",
        "author": {"__typename": "User", "login": "someone"},
        "labels": {"nodes": [{"name": "bug"}]},
        "files": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "totalCount": 2,
            "nodes": [
                {"path": "courier/transports/smtp.py", "additions": 38, "deletions": 6, "changeType": "MODIFIED"},
                {
                    "path": "tests/unit/transports/test_smtp.py",
                    "additions": 10,
                    "deletions": 0,
                    "changeType": "MODIFIED",
                },
            ],
        },
        "reviewRequests": {"nodes": [{"requestedReviewer": {"login": VIEWER}}]},
        "latestReviews": {"nodes": []},
        "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "totalCount": 0, "nodes": []},
        "comments": {"totalCount": 1},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "committedDate": "2026-10-10T12:00:00Z",
                        "statusCheckRollup": {
                            "state": "SUCCESS",
                            "contexts": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "totalCount": 1,
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "name": "tests",
                                        "conclusion": "SUCCESS",
                                        "status": "COMPLETED",
                                        "detailsUrl": "",
                                        "startedAt": "2026-10-10T12:00:00Z",
                                        "completedAt": "2026-10-10T12:05:00Z",
                                    }
                                ],
                            },
                        },
                    }
                }
            ]
        },
        "closingIssuesReferences": {"nodes": [{"number": 7, "title": "Sends fail on a blip", "url": ""}]},
    },
    {
        "number": 42,
        "title": "DOC: describe the pipeline stages",
        "url": f"https://github.com/{REPO}/pull/42",
        "body": "",
        "bodyHTML": "",
        "isDraft": True,
        "createdAt": "2026-10-05T12:00:00Z",
        "updatedAt": "2026-10-09T12:00:00Z",
        "additions": 120,
        "deletions": 0,
        "changedFiles": 1,
        "headRefOid": "b" * 40,
        "baseRefName": "main",
        "reviewDecision": None,
        "mergeable": "MERGEABLE",
        "authorAssociation": "MEMBER",
        "author": {"__typename": "User", "login": VIEWER},
        "labels": {"nodes": []},
        "files": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "totalCount": 1,
            "nodes": [{"path": "docs/pipeline.md", "additions": 120, "deletions": 0, "changeType": "ADDED"}],
        },
        "reviewRequests": {"nodes": []},
        "latestReviews": {"nodes": []},
        "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "totalCount": 0, "nodes": []},
        "comments": {"totalCount": 0},
        "commits": {"nodes": [{"commit": {"committedDate": "2026-10-09T12:00:00Z", "statusCheckRollup": None}}]},
        "closingIssuesReferences": {"nodes": []},
    },
]

DIFF = """\
diff --git a/courier/transports/smtp.py b/courier/transports/smtp.py
--- a/courier/transports/smtp.py
+++ b/courier/transports/smtp.py
@@ -10,3 +10,9 @@ class SmtpTransport:
     def send(self, envelope):
-        self._client.sendmail(envelope)
+        for attempt in range(3):
+            try:
+                return self._client.sendmail(envelope)
+            except Exception:
+                time.sleep(2 ** attempt)
+        Store.open().save(envelope)
"""


TREE = [
    "README.md",
    "pyproject.toml",
    "courier/__init__.py",
    "courier/api/routes.py",
    "courier/api/schemas.py",
    "courier/cli/main.py",
    "courier/pipeline/runner.py",
    "courier/pipeline/steps/dedupe.py",
    "courier/transports/smtp.py",
    "courier/transports/http.py",
    "courier/store/sqlite.py",
    "courier/models/envelope.py",
    "tests/unit/transports/test_smtp.py",
    "tests/unit/pipeline/test_runner.py",
    "docs/architecture.md",
    "docs/pipeline.md",
]

# Recently merged PRs: the churn onboarding measures a drafted profile against.
MERGED = [
    {"number": n, "author": {"login": login, "__typename": "User"}, "authorAssociation": assoc,
     "files": {"nodes": [{"path": path, "additions": add, "deletions": 2} for path, add in files]}}
    for n, login, assoc, files in (
        (30, "alice", "MEMBER", [("courier/transports/smtp.py", 40), ("tests/unit/transports/test_smtp.py", 12)]),
        (31, "bob", "MEMBER", [("courier/pipeline/runner.py", 60), ("courier/pipeline/steps/dedupe.py", 20)]),
        (32, "someone", "CONTRIBUTOR", [("courier/api/routes.py", 25), ("courier/api/schemas.py", 15)]),
        (33, "alice", "MEMBER", [("courier/store/sqlite.py", 30), ("tests/unit/pipeline/test_runner.py", 8)]),
        (34, "carol", "FIRST_TIME_CONTRIBUTOR", [("docs/architecture.md", 50)]),
        (35, "bob", "MEMBER", [("courier/models/envelope.py", 18), ("courier/cli/main.py", 9)]),
    )
]  # fmt: skip


def _log(argv: list[str]) -> None:
    """Record the call. One file per call, because readthrough runs several ``gh`` at once and concurrent appends to
    one file can clobber each other on Windows."""
    directory = os.environ.get("READTHROUGH_FAKE_GH_LOG")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{os.getpid()}.json"
        (Path(directory) / name).write_text(json.dumps(argv), encoding="utf-8")


def calls(directory: Path) -> list[list[str]]:
    """Every recorded call, in the order they were made."""
    if not directory.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.iterdir())]


def _graphql(argv: list[str]) -> str:
    fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("query="))
    query = next(a for a in argv if a.startswith("query="))
    if "query PullRequests" in query:
        size = int(fields.get("size", "100"))
        return json.dumps(
            {
                "data": {
                    "viewer": {"login": VIEWER},
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "totalCount": len(PRS),
                            "nodes": PRS[:size],
                        }
                    },
                }
            }
        )
    if "query MergedPullRequests" in query:
        return json.dumps(
            {"data": {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequests": {"nodes": MERGED}}}}
        )
    if "query DefaultBranch" in query:
        return json.dumps(
            {
                "data": {
                    "repository": {
                        "defaultBranchRef": {
                            "name": "main",
                            "target": {
                                "oid": "c" * 40,
                                "statusCheckRollup": {
                                    "state": "SUCCESS",
                                    "contexts": {
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                        "totalCount": 0,
                                        "nodes": [],
                                    },
                                },
                            },
                        }
                    }
                }
            }
        )
    if "query Files" in query:
        # Several files in one call: each alias is `fN: object(expression:"HEAD:<path>")`; only the docs exist.
        aliases = re.findall(r'(f\d+): object\(expression:"HEAD:([^"]+)"\)', query)
        text = "# Architecture\n\n## Transports\n\n**Responsibility**: deliver one envelope.\n\n**Does not own**: persistence.\n"
        repo = {alias: ({"text": text} if path == "docs/architecture.md" else None) for alias, path in aliases}
        return json.dumps({"data": {"repository": repo}})
    raise SystemExit(f"fake gh: unexpected graphql query {query[:60]!r}")


def main(argv: list[str]) -> int:
    _log(argv)
    if argv[:2] == ["auth", "status"]:
        print(f"github.com\n  Logged in to github.com account {VIEWER} (keyring)", file=sys.stderr)
        return 0
    if argv[:2] == ["api", "graphql"]:
        print(_graphql(argv[2:]))
        return 0
    if argv[:1] == ["api"]:
        path = next(a for a in reversed(argv[1:]) if not a.startswith("-") and ":" not in a)
        if path == "rate_limit":
            print(json.dumps({"resources": {"core": {"remaining": 4990}, "graphql": {"remaining": 4980}}}))
            return 0
        if path == f"repos/{REPO}":
            print(json.dumps({"full_name": REPO, "name": "courier", "default_branch": "main", "private": False}))
            return 0
        if path.startswith(f"repos/{REPO}/git/trees/"):
            print(json.dumps({"truncated": False, "tree": [{"path": p, "type": "blob"} for p in TREE]}))
            return 0
        if path == f"repos/{REPO}/contents/docs/architecture.md":
            print(
                "# Architecture\n\n## Transports\n\n**Responsibility**: deliver one envelope.\n\n**Does not own**: persistence.\n"
            )
            return 0
        print("gh: Not Found (HTTP 404)", file=sys.stderr)  # no profile file, no CODEOWNERS, no protection rules
        return 1
    if argv[:2] == ["pr", "diff"]:
        print(DIFF if argv[2] == "41" else "", end="")
        return 0
    print(f"fake gh: unexpected command {' '.join(argv)}", file=sys.stderr)
    return 1


def command() -> str:
    """The ``READTHROUGH_GH`` value that makes readthrough run this file instead of ``gh``."""
    return json.dumps([sys.executable, str(Path(__file__).resolve())])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
