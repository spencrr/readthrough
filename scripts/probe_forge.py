"""Exercise one forge adapter against a real public repo and print what came back.

    uv run python scripts/probe_forge.py gitlab gitlab-org/gitlab
    uv run python scripts/probe_forge.py forgejo forgejo/forgejo
    uv run python scripts/probe_forge.py ado azure-sdk/public/azure-sdk-for-net

With ``--record DIR`` every HTTP response is also written under DIR as JSON, keyed by request, so the
same session can be replayed offline by the adapter tests (see tests/test_forges.py).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from readthrough import forges
from readthrough.config import Config
from readthrough.faults import ForgeError
from readthrough.forges import Rest
from readthrough.model import Capability
from readthrough.tuning import load_tuning


def _plain(value: Any) -> Any:
    if isinstance(value, Capability):
        return {"kind": value.kind, "value": _plain(value.value), "reason": value.reason}
    if hasattr(value, "__dataclass_fields__"):
        return {k: _plain(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v) for v in value]
    return value


# Only the response headers the adapters read are kept: the rest can carry identities (Azure DevOps puts the
# caller's account in `x-vss-userdata`), cookies, and request ids that have no place in a repository.
KEPT_HEADERS = {
    "content-type",
    "link",
    "x-total-count",
    "x-total-pages",
    "ratelimit-remaining",
    "retry-after",
    "x-vss-resourcetenant",
}
SKIPPED_PATHS = ("connectionData",)  # answers about the signed-in user, which the tests run without


def record_to(directory: Path) -> None:
    """Wrap ``Rest.call`` so every response lands in ``directory`` for the replaying tests."""
    directory.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    original = Rest.call

    def call(self: Rest, method: str, path: str, **kw: Any) -> Any:
        url = self.url(path, kw.get("params"))
        if any(skip in url for skip in SKIPPED_PATHS):
            return original(self, method, path, **kw)
        key = hashlib.sha1(f"{method} {url}".encode()).hexdigest()[:12]  # noqa: S324 - a file name, not security
        entry: dict[str, Any] = {"method": method, "url": url, "file": None}
        try:
            resp = original(self, method, path, **kw)
        except ForgeError as exc:
            # The refusal is part of the recording: the tests must see the same 401s and 404s the forge gave.
            # Azure DevOps names the refused caller by id in the message; that id is nobody's business.
            scrub = re.compile(r"The user '[0-9a-f-]{36}'")
            entry["error"] = {"type": type(exc).__name__, "message": scrub.sub("The user '<id>'", str(exc)),
                              "stderr": scrub.sub("The user '<id>'", exc.stderr),
                              "headers": {k: v for k, v in exc.headers.items() if k in KEPT_HEADERS}}  # fmt: skip
            raise
        else:
            if resp is not None:
                headers = {k: v for k, v in resp.headers.items() if k in KEPT_HEADERS}
                data = {"method": method, "url": url, "status": resp.status, "headers": headers, "text": resp.text}
                if resp.body and resp.body != resp.text.encode("utf-8"):  # a blob that isn't UTF-8 text
                    data["body_b64"] = base64.b64encode(resp.body).decode("ascii")
                (directory / f"{key}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
                entry["file"] = f"{key}.json"
            return resp
        finally:
            index.append(entry)
            (directory / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")

    Rest.call = call  # type: ignore[method-assign]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("forge", choices=forges.names())
    parser.add_argument("repo")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--record", type=Path, help="write every HTTP response under this directory")
    parser.add_argument("--skip-diffs", action="store_true")
    parser.add_argument("--tree-limit", type=int, default=1000, help="max_tree_files for the probe (default 1000)")
    args = parser.parse_args()
    if args.record:
        record_to(args.record)
    tuning = load_tuning({"forge": {"max_tree_files": args.tree_limit}})
    cfg = Config(home=Path.home() / ".readthrough", repo=args.repo, forge=args.forge, tuning=tuning)
    forge = forges.make(cfg)
    print(f"{forge.label} {args.repo}: auth {forge.describe_auth()}")
    started = time.monotonic()

    def show(label: str, value: Any) -> None:
        text = json.dumps(_plain(value), indent=1, default=str)
        print(f"\n== {label} ({time.monotonic() - started:.1f}s)\n{text[:3000]}{'…' if len(text) > 3000 else ''}")

    show("repo_info", forge.repo_info())
    show("rate_limit", forge.rate_limit())
    viewer, prs, total = forge.fetch_prs(args.limit)
    show("fetch_prs", {"viewer": viewer, "total": total, "prs": prs})
    for pr in prs:
        gaps = [(name, cap.kind, cap.reason) for name, cap in pr.gaps()]
        print(f"  #{pr.number} {pr.title[:50]!r}: {len(pr.files)} files, checks={pr.checks.kind} gaps={gaps}")
    branch, merged = forge.merged_prs(args.limit)
    show("merged_prs", {"branch": branch, "merged": merged})
    tree, complete = forge.tree(branch)
    show("tree", {"count": len(tree), "complete": complete, "first": tree[:10]})
    wanted = [p for p in ("README.md", "CODEOWNERS", ".github/CODEOWNERS", "docs/README.md") if p in tree][:2]
    files = forge.read_files(wanted)
    show("read_files", {p: t[:120] for p, t in files.items()})
    show("read_file(missing)", forge.read_file("definitely/not/here.md"))
    top = sorted({p.split("/")[0] for p in tree if "/" in p})[:1]
    show("list_dir", {d: forge.list_dir(d)[:10] for d in top})
    show("ci_context", forge.ci_context({pr.base for pr in prs}))
    if prs and not args.skip_diffs and "diffs" in forge.capabilities:
        diff = forge.fetch_diff(prs[0].number)
        show("fetch_diff", {"number": prs[0].number, "bytes": len(diff), "head": diff[:300]})
    print(f"\nall good in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
