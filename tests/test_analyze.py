from conftest import NOW, PROFILE

from readthrough.analyze import (
    analyze,
    compute_overlaps,
    find_smells,
    is_newsworthy,
    parse_diff,
    plan_sessions,
    whats_new,
)

DIFF = """\
diff --git a/courier/transports/smtp.py b/courier/transports/smtp.py
--- a/courier/transports/smtp.py
+++ b/courier/transports/smtp.py
@@ -10,3 +10,7 @@ class X:
     def keep(self):
-        old()
+        Store.open().save(m)
+    async def send(self, *, m):
+    async def route(self) -> None:  # async-suffix-exempt
+    def helper(self, a: dict[str, int]) -> None:
+    def two(self, a, b):
diff --git a/tests/unit/test_x.py b/tests/unit/test_x.py
--- a/tests/unit/test_x.py
+++ b/tests/unit/test_x.py
@@ -1,1 +1,2 @@
+    Store.open()
"""


def test_component_of_prefers_specific_prefixes():
    assert PROFILE.component_of("courier/pipeline/steps/dedupe.py") == "steps"
    assert PROFILE.component_of("courier/pipeline/runner.py") == "pipeline"
    assert PROFILE.component_of("courier/util/x.py") == "common"
    assert PROFILE.component_of("README.md") == "other"


def test_parse_diff_tracks_new_line_numbers():
    added = parse_diff(DIFF)
    assert [n for n, _ in added["courier/transports/smtp.py"]] == [11, 12, 13, 14, 15]
    assert added["tests/unit/test_x.py"][0][0] == 1


def test_find_smells():
    ids = {(s["id"], s["line"]) for s in find_smells(parse_diff(DIFF), PROFILE)}
    assert ("transport-store", 11) in ids
    assert ("async-suffix", 12) in ids
    # Exempt marker, and commas inside type hints, are not findings.
    assert not any(i == "async-suffix" and line == 13 for i, line in ids)
    assert ("kw-only", 14) not in ids
    assert ("kw-only", 15) in ids
    # Responsibility smells don't fire in tests.
    assert all(s["path"].startswith("courier/") for s in find_smells(parse_diff(DIFF), PROFILE))


def test_analyze_ready_lane_and_rules(make_pr):
    from readthrough.rules import parse_instruction

    ins = parse_instruction("transports.md", '---\napplyTo: "courier/transports/**"\n---\n', "u")
    it = analyze(make_pr(), profile=PROFILE, viewer="me", instructions=[ins], diff_text=None, now=NOW)
    assert it["lane"] == "ready"
    assert it["primary"] == "transports"
    assert it["rules"] == ["transports.md"]
    assert it["priority"] > 0


def test_lanes(make_pr):
    def lane(**kw):
        return analyze(make_pr(**kw), profile=PROFILE, viewer="me", instructions=[], diff_text=None, now=NOW)["lane"]

    assert lane(author={"login": "me"}) == "mine"
    assert lane(isDraft=True) == "draft"
    assert lane(mergeable="CONFLICTING") == "blocked"
    assert lane(reviewDecision="APPROVED") == "approved"
    review = {
        "nodes": [{"author": {"login": "r"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-10-01T18:00:00Z"}]
    }
    assert lane(reviewDecision="CHANGES_REQUESTED", latestReviews=review) == "waiting"


def test_push_after_changes_requested_is_a_re_review(make_pr):
    review = {
        "nodes": [{"author": {"login": "r"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-10-01T06:00:00Z"}]
    }
    it = analyze(
        make_pr(reviewDecision="CHANGES_REQUESTED", latestReviews=review),
        profile=PROFILE,
        viewer="me",
        instructions=[],
        diff_text=None,
        now=NOW,
    )
    assert it["lane"] == "ready"
    assert any(f["id"] == "re-review" for f in it["flags"])


def test_no_tests_flag(make_pr):
    files = {
        "nodes": [{"path": "courier/transports/smtp.py", "additions": 50, "deletions": 0, "changeType": "MODIFIED"}]
    }
    it = analyze(make_pr(files=files), profile=PROFILE, viewer="me", instructions=[], diff_text=None, now=NOW)
    assert any(f["id"] == "no-tests" for f in it["flags"])


def _items(make_pr):
    a = analyze(make_pr(number=1), profile=PROFILE, viewer="me", instructions=[], diff_text=None, now=NOW)
    b = analyze(make_pr(number=2), profile=PROFILE, viewer="me", instructions=[], diff_text=None, now=NOW)
    return a, b


def test_overlaps_and_sessions(make_pr):
    a, b = _items(make_pr)
    overlaps = compute_overlaps([a, b], PROFILE)
    assert overlaps == [{"a": 1, "b": 2, "files": ["courier/transports/smtp.py", "tests/unit/transports/test_smtp.py"]}]
    sessions = plan_sessions([a, b], PROFILE, budget=a["minutes"])
    assert [s["prs"] for s in sessions] == [[1], [2]]


def test_whats_new(make_pr):
    a, b = _items(make_pr)
    first = whats_new(None, [a], {}, "me")
    assert first["since"] is None and is_newsworthy(first)

    previous = {
        "generated": "2026-10-01T00:00:00Z",
        "prs": [a, {"number": 9, "title": "gone", "flags": [], "head": "x"}],
    }
    b_moved = dict(b, head="b" * 40)
    a_moved = dict(a, head="c" * 40)
    ledger = {"1": {"head": "a" * 40}}
    news = whats_new(previous, [a_moved, b_moved], ledger, "me")
    assert news["opened"] == [2]
    assert news["closed"] == [{"number": 9, "title": "gone"}]
    assert news["changed_after_review"] == [1]
    assert is_newsworthy(news)

    quiet = whats_new({"generated": "x", "prs": [a]}, [a], {}, "me")
    assert not is_newsworthy(quiet)


def _with_checks(make_pr, *checks):
    nodes = [
        {
            "__typename": "CheckRun",
            "name": n,
            "conclusion": c,
            "status": "COMPLETED",
            "startedAt": "2026-10-01T00:00:00Z",
        }
        for n, c in checks
    ]
    rollup = {"state": "FAILURE", "contexts": {"nodes": nodes}}
    return make_pr(
        commits={"nodes": [{"commit": {"committedDate": "2026-10-01T12:00:00Z", "statusCheckRollup": rollup}}]}
    )


def test_only_own_required_failures_block(make_pr):
    def go(pr, main=frozenset()):
        return analyze(
            pr, profile=PROFILE, viewer="me", instructions=[], diff_text=None, now=NOW,
            required_checks={"main": {"tests"}}, main_failing=set(main),
        )  # fmt: skip

    blocked = go(_with_checks(make_pr, ("tests", "FAILURE")))
    assert (blocked["lane"], blocked["ci_state"]) == ("blocked", "failing")
    optional = go(_with_checks(make_pr, ("tests", "SUCCESS"), ("docs", "FAILURE")))
    assert (optional["lane"], optional["ci_state"]) == ("ready", "failing-optional")
    assert any(f["id"] == "ci-optional" for f in optional["flags"])
    on_main = go(_with_checks(make_pr, ("tests", "FAILURE")), main={"tests"})
    assert (on_main["lane"], on_main["ci_state"]) == ("ready", "failing-main")
    passing = go(_with_checks(make_pr, ("tests", "SUCCESS")))
    assert passing["priority"] > optional["priority"]
