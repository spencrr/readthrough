"""The client-server contract (``contract.py``) holds on both sides.

The server side: a real build, with every optional part present, has exactly the declared shapes, so a field
added or dropped in the analysis is a deliberate edit of the contract. The page side: the scripts read nothing
the server doesn't send, listen only for events it emits, handle every study event it can send, and call only
routes it serves.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from readthrough import agents, contract, engine, review
from readthrough.build import Warnings, build
from readthrough.config import Config

REPO = "octo/courier"
PROFILE_PATH = Path(__file__).parent / "fixtures" / "courier.toml"
WEB = Path(__file__).resolve().parents[1] / "src" / "readthrough" / "assets" / "web"
SRC = Path(__file__).resolve().parents[1] / "src" / "readthrough"
DIFF = """\
diff --git a/courier/transports/smtp.py b/courier/transports/smtp.py
--- a/courier/transports/smtp.py
+++ b/courier/transports/smtp.py
@@ -1,3 +1,5 @@
+import time
 def send(envelope):
-    return deliver(envelope)
+    time.sleep(1)
+    return deliver(envelope)  # TODO: retry
"""


@pytest.fixture
def payload(tmp_path: Path, monkeypatch, make_pr, fake_forge) -> dict:
    """A build in which every list and optional record is populated, so each shape is really checked."""
    thread = {
        "isResolved": False, "isOutdated": True, "path": "courier/transports/smtp.py", "line": 3,
        "comments": {"nodes": [{"author": {"login": "rev"}, "body": "Why sleep?", "bodyHTML": "<p>Why sleep?</p>"}]},
    }  # fmt: skip
    failing = {
        "state": "FAILURE",
        "contexts": {"nodes": [{"__typename": "CheckRun", "name": "lint", "status": "COMPLETED",
                                "conclusion": "FAILURE", "detailsUrl": "https://ci/1"}]},
    }  # fmt: skip
    fake_forge.prs = [
        make_pr(number=1, body="Adds retries", labels={"nodes": [{"name": "bug"}]},
                reviewThreads={"nodes": [thread]}, reviewRequests={"nodes": [{"requestedReviewer": {"login": "me"}}]},
                commits={"nodes": [{"commit": {"committedDate": "2026-10-01T12:00:00Z", "statusCheckRollup": failing}}]}),
        make_pr(number=2, title="FEAT: other", author={"login": "someone"}),  # ready: a plan session needs one
        make_pr(number=3, title="DOC: draft", isDraft=True),
    ]  # fmt: skip
    fake_forge.total = 40
    fake_forge.diff_texts = {1: DIFF}
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot"))
    monkeypatch.setattr("readthrough.build.registered_protocol_command", lambda: None)
    cfg = Config(home=tmp_path / "home", repo=REPO, clone=tmp_path, onboarded="2026-10-01T00:00:00+00:00")
    cfg.local_profile_path.parent.mkdir(parents=True)
    cfg.local_profile_path.write_text(PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    # A review session for #1, so `session` and `sessions` carry real records.
    folder = agents.copilot_home() / "session-state" / review.default_session_id(cfg, 1)
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text("{}\n", encoding="utf-8")
    build(cfg, with_diffs=True)
    return json.loads(cfg.data_path.read_text(encoding="utf-8"))


def _exactly(record: type, value: dict, where: str) -> None:
    declared, actual = contract.fields(record), set(value)
    assert actual == declared, f"{where}: undeclared {sorted(actual - declared)}, missing {sorted(declared - actual)}"


def test_a_build_has_exactly_the_declared_shape(payload):
    _exactly(contract.Payload, payload, "payload")
    _exactly(contract.ForgeView, payload["forge"], "forge")
    _exactly(contract.ProfileView, payload["profile"], "profile")
    _exactly(contract.Freshness, payload["freshness"], "freshness")
    _exactly(contract.Links, payload["links"], "links")
    _exactly(contract.News, payload["news"], "news")
    assert payload["drift"] is None or set(payload["drift"]) == contract.fields(contract.Drift)
    assert all(set(rule) == {"source", "how"} for rule in payload["rules"].values())
    for component in payload["components"]:
        _exactly(contract.Component, component, f"component {component.get('id')}")
    assert payload["overlaps"] and payload["sessions"] and payload["warnings"], "the fixture populates each list"
    _exactly(contract.Overlap, payload["overlaps"][0], "overlap")
    _exactly(contract.PlanSession, payload["sessions"][0], "plan session")
    _exactly(contract.BuildWarning, payload["warnings"][0], "warning")
    warnings = Warnings()
    warnings.add("x", "a message", ["detail"])
    _exactly(contract.BuildWarning, warnings[0], "a warning as the build makes it")

    assert [p["number"] for p in payload["prs"]] == [1, 2, 3]
    for pr in payload["prs"]:
        _exactly(contract.PullRequestItem, pr, f"PR #{pr['number']}")
        _exactly(contract.StudySummary, pr["study"], "study summary")
        _exactly(contract.SessionInfo, pr["session"], "session")
        assert set(pr["ci_checks"]) <= contract.fields(contract.CiChecks)
        assert contract.required_fields(contract.CiChecks) - {"reason"} <= set(pr["ci_checks"])
        for row in pr["files"]:
            _exactly(contract.FileRow, row, "file row")
        for flag in pr["flags"]:
            _exactly(contract.Flag, flag, "flag")
    first = payload["prs"][0]
    assert first["smells"] and first["unresolved"] and first["ci_checks"]["failing"] and first["sessions"]
    _exactly(contract.Smell, first["smells"][0], "smell")
    _exactly(contract.Thread, first["unresolved"][0], "thread")
    _exactly(contract.FailingCheck, first["ci_checks"]["failing"][0], "failing check")
    _exactly(contract.AgentSession, first["sessions"][0], "agent session")
    assert first["reviews"] == [] or set(first["reviews"][0]) == contract.fields(contract.Review)
    assert first["labels"] == ["bug"] and first["requested"] == ["me"]
    assert not (contract.CLIENT_PR_FIELDS & contract.fields(contract.PullRequestItem)), (
        "what the page adds to a PR must not collide with what the server sends"
    )


def _reads(source: str, prefix: str) -> set[str]:
    """Field names read as ``prefix.name`` (not as part of a longer path)."""
    return set(re.findall(rf"(?<![\w.]){re.escape(prefix)}\.([A-Za-z_]\w*)", source))


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    return source[start : source.index("\n}\n", start)]


def test_the_page_reads_only_what_the_server_sends():
    js = (WEB / "dashboard.js").read_text(encoding="utf-8")
    assert _reads(js, "DATA") <= contract.fields(contract.Payload)
    assert _reads(js, "LIVE") <= contract.fields(contract.Live) | contract.CLIENT_LIVE_FIELDS
    assert _reads(js, "PROFILE") <= contract.fields(contract.ProfileView)
    pr_fields = contract.fields(contract.PullRequestItem) | contract.CLIENT_PR_FIELDS
    for prefix in ("p", "pr", "byNum[n]", "byNum[r]"):
        reads = _reads(js, prefix) - contract.fields(contract.ProfileView)  # `pr` is also PROFILE in one helper
        assert reads <= pr_fields, f"{prefix}: {sorted(reads - pr_fields)}"
    assert _reads(js, "st.engine") <= contract.fields(contract.EngineInfo)
    assert _reads(js, "FORGE") <= contract.fields(contract.ForgeView)
    assert _reads(js, "LINKS") <= contract.fields(contract.Links)
    study_handler = _function_body(js, "onStudyEvent")
    assert _reads(study_handler, "ev") <= contract.fields(contract.StudyEvent)

    listened = set(re.findall(r'es\.addEventListener\("(\w+)"', js))
    assert listened == contract.EVENTS, "the page listens for every event the server can send, and no others"
    handled = set(re.findall(r'ev\.type === "(\w+)"', study_handler))
    assert handled == contract.STUDY_EVENT_TYPES - contract.STUDY_EVENTS_THE_PAGE_MAY_IGNORE
    called = {m.rstrip("/") for m in re.findall(r"/api/[a-z_/]+", js)}
    served = {"/api/data", "/api/events", "/api/refresh", "/api/brief", "/api/ledger", "/api/review", "/api/study"}
    assert called <= served


def test_the_server_emits_only_declared_events():
    sources = "\n".join((SRC / name).read_text(encoding="utf-8") for name in ("serve.py", "engine.py", "assistants.py"))
    names = set(re.findall(r'broadcast\(\s*"(\w+)"', sources))
    assert names <= contract.EVENTS, sorted(names - contract.EVENTS)
    study_types = set(re.findall(r'"type": "(\w+)"', sources)) - {"object", "string", "integer", "array", "boolean"}
    assert study_types <= contract.STUDY_EVENT_TYPES, sorted(study_types - contract.STUDY_EVENT_TYPES)
    assert set(engine.ACTIONS) == contract.STUDY_ASK_ACTIONS
    assert "contract.STUDY_ADD_KINDS" in (SRC / "serve.py").read_text(encoding="utf-8"), (
        "the server checks what the page may add against the contract, not a list of its own"
    )
