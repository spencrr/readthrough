"""The contract between the server and the page: what crosses the boundary, and the rules it crosses under.

Three things cross it, all declared here so that changing one is a deliberate edit that tests check from both
sides (``tests/test_contract.py``: a real build has exactly these shapes, and the page reads nothing but them):

1. **The payload** (``data.json``, ``build.DATA_SCHEMA``). ``Payload`` is the whole file; the page gets it
   inlined at render and again from ``GET /api/data`` after a ``data`` event. ``PullRequestItem`` is one PR.
2. **Server-Sent Events** on ``GET /api/events`` (``EVENTS``). They are notifications, not state: each carries
   enough to patch the page in place, but the page's truth is always what a GET returns, and after any gap it
   re-fetches rather than reasoning about what it missed (see "Gaps" below).
3. **The study API** (``StudyState``, ``StudyEvent``): the per-PR study log and the assistant's turn.

Rules:

* Adding a field is compatible within a schema; the page ignores what it doesn't know. Removing or renaming
  one, or changing what it means, bumps ``DATA_SCHEMA``: the server refuses to serve a newer file and
  rebuilds (``serve.Dashboard.load``), and a page from an older server run reloads itself on ``hello``.
* The page may add fields of its own to a PR item (``CLIENT_PR_FIELDS``: its view of the ledger); the server
  never writes those names.
* Study log entries have ids that are unique per PR and strictly increase in the file, so the page
  de-duplicates by id and an event delivered twice is harmless. Only one assistant turn runs per PR at a
  time; a second request is refused with 409 and the page shows the reason.
* Gaps. An ``EventSource`` reconnects by itself, but events sent meanwhile are gone; a client that can't keep
  up overflows its queue. Either way the page is told, or notices, and re-fetches everything it holds:
  ``hello`` with a different ``instance`` means the server restarted (new CSRF token: reload the page);
  ``resync`` means the server dropped events for this client; a reconnect after an error reloads too.
* Errors are JSON ``{"error": message}`` with the HTTP status that says why (400 bad request, 403 wrong host,
  origin, or token, 404 unknown PR or route, 409 busy, 413 too large, 500 with an error id). The page shows
  the message as it is.
"""

from __future__ import annotations

from typing import Any, TypedDict

# ---------------------------------------------------------------- the payload


class FileRow(TypedDict):
    path: str
    add: int
    del_: int  # JSON key ``del``; see FIELD_NAMES
    type: str  # ADDED | MODIFIED | DELETED | RENAMED | COPIED | CHANGED
    comp: str  # the component the path belongs to (``other`` when unmapped)


class Flag(TypedDict):
    id: str  # a rule id (see ``Payload.rules``)
    kind: str  # risk | blocker | convention | responsibility | correctness | info | author | you
    msg: str


class Smell(TypedDict):
    id: str
    kind: str
    path: str
    line: int
    msg: str
    code: str  # the offending line, clipped


class FailingCheck(TypedDict):
    name: str
    url: str | None
    required: bool
    on_main: bool  # also failing on the base branch: not this PR's fault


class CiChecks(TypedDict, total=False):
    state: str  # ci.STATES: passing | failing | failing-optional | failing-main | not-run | pending | none | unknown
    failing: list[FailingCheck]
    pending: int
    cancelled: int
    passed: int
    total: int
    required_missing: list[str]
    required_known: bool
    reason: str  # only when state is unknown: why the forge couldn't say


class Thread(TypedDict):
    path: str | None
    line: int | None
    outdated: bool
    author: str
    body: str
    body_html: str  # third-party HTML: the page runs it through DOMPurify


class Review(TypedDict):
    author: str
    state: str


class Issue(TypedDict):
    number: int
    title: str
    url: str


class SessionInfo(TypedDict):
    """``agents.SessionInfo.payload()``: the PR's primary review session."""

    id: str
    exists: bool
    updated: int | None
    active: bool | None  # None: the agent leaves no trace of whether the session is open


class AgentSession(SessionInfo):
    """Every session linked to the PR, the primary first (``review.pr_sessions``)."""

    where: str  # "VS Code" | "terminal" | the client name | "other"
    name: str
    agent: str  # the agent's registry name
    agent_label: str
    editor_url: str | None


class StudySummary(TypedDict):
    """``study.summarize``: the log's counts, shown next to the PR and at the top of the Study tab."""

    questions: int
    answered: int
    unaided: int
    after_hint: int
    after_brief: int
    assessed: int
    correct: int
    partial: int
    incorrect: int
    score: float | None
    predictions: int
    prediction_skipped: bool
    briefed: bool
    teachback: str | None
    teachbacks: int
    reflections: int
    findings: int
    notes: int


class PullRequestItem(TypedDict):
    """One analysed PR (``analyze.analyze``, plus what the build adds: sessions and the study summary)."""

    number: int
    title: str
    url: str
    author: str
    author_kind: str
    author_group: str  # internal | external | bot | you
    author_association: str
    body: str
    body_html: str
    draft: bool
    created: str
    updated: str
    age_days: float
    idle_days: float
    additions: int
    deletions: int
    changed_files: int
    head: str
    base: str
    review_decision: str
    mergeable: str
    ci_state: str
    ci_checks: CiChecks
    labels: list[str]
    requested: list[str]
    reviews: list[Review]
    comments: int
    issues: list[Issue]
    files: list[FileRow]
    by_comp: dict[str, int]
    primary: str
    layers: list[str]
    rules: list[str]  # names of the instruction files that apply
    flags: list[Flag]
    smells: list[Smell]
    unresolved: list[Thread]
    lane: str  # mine | draft | blocked | waiting | approved | ready
    minutes: int
    priority: float
    related: list[int]
    session: SessionInfo
    sessions: list[AgentSession]
    study: StudySummary


CLIENT_PR_FIELDS = frozenset({"status", "note", "understood", "reviewed"})
"""What the page adds to a PR item from the ledger (``loadProgress``). Never written by the server."""


class Component(TypedDict):
    id: str
    label: str
    layer: str
    contract: dict[str, Any]  # ``profile.Contract`` as a dict, or {} when the component has none


class ProfileView(TypedDict):
    repo: str
    title: str
    layers: list[Any]
    author_kinds: dict[str, Any]
    contracts_source: str | None
    overlap_non_source: list[str]
    drafted_by: str | None
    origin: str
    onboarded: str | None


class Instruction(TypedDict):
    name: str
    patterns: list[str]
    url: str


class Overlap(TypedDict):
    a: int
    b: int
    files: list[str]


class PlanSession(TypedDict):
    component: str
    label: str
    prs: list[int]
    minutes: int


class News(TypedDict):
    since: str | None
    opened: list[int]
    closed: list[dict[str, Any]]  # {number, title}
    needs_you: list[int]
    changed_after_review: list[int]


class Drift(TypedDict):
    coverage: float
    unmapped: list[Any]  # (path, count) pairs


class BuildWarning(TypedDict):
    code: str
    message: str
    details: list[str]


class Freshness(TypedDict):
    stale_live_hours: float
    stale_snapshot_hours: float


class Links(TypedDict):
    protocol: bool
    briefs_dir: str
    clone_uri: str | None


class ForgeView(TypedDict):
    name: str
    label: str
    pr_noun: str
    url: str
    files_tab: str


class Payload(TypedDict):
    """``data.json``: everything the page shows. ``prs`` is the only part a build can't do without."""

    schema: int
    version: str
    repo: str
    slug: str
    forge: ForgeView
    viewer: str
    generated: str
    profile: ProfileView
    rules: dict[str, dict[str, str]]  # rule id -> {source, how}
    components: list[Component]
    instructions: list[Instruction]
    prs: list[PullRequestItem]
    overlaps: list[Overlap]
    sessions: list[PlanSession]
    ledger: dict[str, Any]  # PR number (as a string) -> ledger entry
    news: News
    drift: Drift | None
    draft: bool
    warnings: list[BuildWarning]
    stats: dict[str, Any]
    freshness: Freshness
    links: Links


# ---------------------------------------------------------------- the page's own settings


class Live(TypedDict):
    """Inlined as ``LIVE`` when the page is served (``null`` in a static snapshot)."""

    token: str  # the CSRF token every POST carries as X-Readthrough-Token
    instance: str  # changes when the server restarts; a page that sees another one on ``hello`` reloads
    base: str  # the repo's URL prefix, /r/<slug>
    repos: list[dict[str, str]]  # {slug, name} for the switcher
    refresh_minutes: int
    status: dict[str, Any]  # the dashboard's status, as the ``status`` event carries it


CLIENT_LIVE_FIELDS = frozenset({"disconnected"})
"""What the page adds to ``LIVE`` itself: whether its event stream is down right now."""


# ---------------------------------------------------------------- events


EVENTS = frozenset({"hello", "status", "data", "ledger", "sessions", "study", "resync"})
"""Server-Sent Event names on /api/events.

hello     {instance}                 first, on every (re)connection
status    the dashboard's status      building, error, last_build, next_build, warnings, ...
data      {generated}                 a build finished: fetch /api/data
ledger    the whole ledger            a status or note changed
sessions  {number: {session, sessions}}  only the PRs whose agent sessions changed
study     a StudyEvent                the Study tab's stream
resync    {reason}                    this client missed events (its queue overflowed): fetch everything again
"""

STUDY_EVENT_TYPES = frozenset({"start", "delta", "tool", "entry", "usage", "done", "error", "reload"})
"""``StudyEvent.type``. The engine sends start, then any number of delta and tool, entries as they are written,
usage per model call, and done or error; reload says the log changed outside the dashboard (a terminal appended)."""

STUDY_EVENTS_THE_PAGE_MAY_IGNORE = frozenset({"usage"})
"""Informational: the page needn't react (usage goes to the ledger, which doctor sums)."""


class StudyEvent(TypedDict, total=False):
    pr: int
    type: str  # one of STUDY_EVENT_TYPES
    action: str  # start: what the learner asked for
    assistant: str  # start: which assistant answers
    text: str  # delta: the next piece of the reply
    name: str  # tool: the tool the model is calling
    entry: dict[str, Any]  # entry: the log entry (a question's expected points withheld until answered)
    summary: dict[str, Any]  # entry, reload: the log's StudySummary afterwards
    message: str  # error: why the turn failed (also logged as a failure entry)


# ---------------------------------------------------------------- the study API


class EngineInfo(TypedDict):
    """``engine.StudyEngine.engine_info`` plus ``busy``: what answers in the Study tab."""

    name: str  # the assistant: model | rules
    available: bool
    reason: str
    runtime: str | None  # the model runtime, when the assistant runs on one
    model: str | None  # the model configured for it, when the runtime names one up front
    describe: str  # the runtime's own description
    busy: bool  # a turn is running for this PR


class StudyState(TypedDict):
    """``GET /api/study/<n>``."""

    entries: list[dict[str, Any]]  # study log entries, public form (``engine.public_entries``)
    summary: dict[str, Any]  # StudySummary
    engine: EngineInfo


STUDY_ADD_KINDS = frozenset({"prediction", "teachback", "note", "reflection", "finding"})
"""``POST /api/study/<n>`` body ``kind``: what the learner writes without asking the assistant anything.
``note`` with ``pinned`` keeps a message in the brief; ``prediction`` with ``skipped`` records skipping it."""

STUDY_ASK_ACTIONS = frozenset({"chat", "quiz", "brief", "answer", "teachback", "hint"})
"""``POST /api/study/<n>/ask`` body ``action`` (``engine.ACTIONS``); ``text`` and ``qid`` as the action needs."""


# ---------------------------------------------------------------- helpers for the tests

FIELD_NAMES = {"del_": "del"}  # TypedDict keys that can't be spelled as the JSON key


def fields(record: type) -> frozenset[str]:
    """The JSON keys a TypedDict declares."""
    return frozenset(FIELD_NAMES.get(k, k) for k in record.__annotations__)


def required_fields(record: type) -> frozenset[str]:
    required: frozenset[str] = getattr(record, "__required_keys__", frozenset())
    return frozenset(FIELD_NAMES.get(k, k) for k in required)
