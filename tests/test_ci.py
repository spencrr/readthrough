from readthrough import ci
from readthrough.ci import STATE_RANK, failing_names, latest_checks
from readthrough.forges.github import _checks
from readthrough.model import Check

# Checks are described the way GitHub's GraphQL returns them and mapped through the GitHub adapter, so
# these tests cover both the mapping and the classification.


def run(name, conclusion, *, started="2026-10-01T10:00:00Z", status="COMPLETED", url=None) -> Check:
    node = {"__typename": "CheckRun", "name": name, "conclusion": conclusion, "status": status, "startedAt": started,
            "detailsUrl": url}  # fmt: skip
    return _checks([node])[0]


def status(context, state, *, created="2026-10-01T10:00:00Z") -> Check:
    return _checks([{"__typename": "StatusContext", "context": context, "state": state, "createdAt": created}])[0]


def classify(checks, *, required, main_failing):
    return ci.classify(checks, required=required, main_failing=main_failing)


REQUIRED = {"tests", "lint"}


def test_reruns_keep_the_latest_result():
    contexts = [
        run("tests", "FAILURE", started="2026-10-01T09:00:00Z"),
        run("tests", "SUCCESS", started="2026-10-01T11:00:00Z"),
    ]
    assert latest_checks(contexts)["tests"]["outcome"] == "SUCCESS"
    assert failing_names(contexts) == set()


def test_required_failure_is_failing():
    info = classify([run("tests", "FAILURE"), run("lint", "SUCCESS")], required=REQUIRED, main_failing=set())
    assert info["state"] == "failing"
    assert info["failing"] == [{"name": "tests", "url": None, "required": True, "on_main": False}]


def test_optional_failure_is_not_blocking():
    contexts = [run("tests", "SUCCESS"), run("lint", "SUCCESS"), run("docs-preview", "FAILURE")]
    assert classify(contexts, required=REQUIRED, main_failing=set())["state"] == "failing-optional"


def test_failures_shared_with_main_are_not_the_prs_fault():
    contexts = [run("tests", "SUCCESS"), run("lint", "FAILURE")]
    info = classify(contexts, required=REQUIRED, main_failing={"lint"})
    assert info["state"] == "failing-main"
    assert info["failing"][0]["on_main"]


def test_own_required_failure_wins_over_main_failure():
    contexts = [run("tests", "FAILURE"), run("lint", "FAILURE")]
    assert classify(contexts, required=REQUIRED, main_failing={"lint"})["state"] == "failing"


def test_required_checks_never_reported_is_not_run():
    info = classify([status("license/cla", "SUCCESS")], required=REQUIRED, main_failing=set())
    assert info["state"] == "not-run"
    assert info["required_missing"] == ["lint", "tests"]


def test_pending_and_partial_reports():
    assert (
        classify(
            [run("tests", None, status="IN_PROGRESS"), run("lint", "SUCCESS")], required=REQUIRED, main_failing=set()
        )["state"]
        == "pending"
    )
    assert classify([run("tests", "SUCCESS")], required=REQUIRED, main_failing=set())["state"] == "pending"
    assert (
        classify([run("tests", "CANCELLED"), run("lint", "SUCCESS")], required=REQUIRED, main_failing=set())["state"]
        == "pending"
    )


def test_passing_and_none():
    assert (
        classify([run("tests", "SUCCESS"), run("lint", "SKIPPED")], required=REQUIRED, main_failing=set())["state"]
        == "passing"
    )
    assert classify([], required=None, main_failing=set())["state"] == "none"


def test_without_known_required_checks_any_failure_counts():
    assert classify([run("tests", "FAILURE")], required=None, main_failing=set())["state"] == "failing"


def test_status_contexts_count():
    assert classify([status("ci/jenkins", "ERROR")], required={"ci/jenkins"}, main_failing=set())["state"] == "failing"


def test_rank_puts_required_failures_first():
    assert STATE_RANK["failing"] < STATE_RANK["failing-optional"] < STATE_RANK["passing"] < STATE_RANK["none"]
    assert STATE_RANK["unknown"] < STATE_RANK["passing"], "an unreadable CI isn't filed with the passing ones"


def test_unreadable_checks_are_unknown_not_none():
    info = ci.unknown("HTTP 403")
    assert info["state"] == "unknown" and info["reason"] == "HTTP 403" and not info["required_known"]
    assert ci.STATE_LABELS["unknown"] == "couldn't be read"


def test_pending_status_contexts_have_no_conclusion():
    check = status("ci/jenkins", "PENDING")
    assert (check.status, check.conclusion) == ("PENDING", None)
    assert classify([check], required=None, main_failing=set())["state"] == "pending"
