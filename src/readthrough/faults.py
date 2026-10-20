"""How calls to the outside fail, classified so the caller knows what the failure means.

One hierarchy for every forge and for ``git``: the build retries what's temporary, treats only ``NotFound``
as "absent", and turns everything else into a warning with the reason. Classification works from a
CLI's stderr (``gh``, ``glab``, ``az``) or from an HTTP status, so adapters on either transport raise the
same errors.
"""

from __future__ import annotations

import re


class ForgeError(RuntimeError):
    """A failed call to a forge or to ``git``. Subclasses say why, and whether trying again could help."""

    retryable = False
    kind = "error"

    def __init__(
        self, message: str, *, what: str = "", stderr: str = "", returncode: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:  # fmt: skip
        super().__init__(message)
        self.what, self.stderr, self.returncode = what, stderr, returncode
        self.headers = headers or {}  # an HTTP refusal's response headers (lower-cased): some say how to sign in

    @property
    def reason(self) -> str:
        """The first meaningful line of the error output, for one-line logs."""
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        return (lines[0] if lines else str(self))[:300]


class NetworkError(ForgeError):
    retryable, kind = True, "network"


class Timeout(NetworkError):
    kind = "timeout"


class RateLimited(ForgeError):
    retryable, kind = True, "rate-limit"
    retry_after: float | None = None  # seconds the forge asked us to wait, when it said


class AuthError(ForgeError):
    kind = "auth"


class NotFound(ForgeError):
    kind = "not-found"


class TooLarge(ForgeError):
    kind = "too-large"


_STDERR_PATTERNS: list[tuple[type[ForgeError], re.Pattern[str]]] = [
    (RateLimited, re.compile(r"HTTP 429|rate limit|abuse detection", re.IGNORECASE)),
    (TooLarge, re.compile(r"HTTP 406|exceeded the maximum number of|diff is taking too long|too large to", re.IGNORECASE)),
    (NotFound, re.compile(r"HTTP 404|Not Found|Could not resolve to a|no pull requests? found", re.IGNORECASE)),
    (AuthError, re.compile(r"HTTP 401|HTTP 403|Bad credentials|gh auth login|glab auth login|az login|SAML|single sign-on|must authenticate", re.IGNORECASE)),
    (NetworkError, re.compile(
        r"error connecting to|connection (?:reset|refused)|forcibly closed|actively refused|proxyconnect|dial tcp|"
        r"connectex|network is unreachable|no route to host|wsarecv|wsasend|broken pipe|i/o timeout|"
        r"timeout awaiting|TLS handshake|unexpected EOF|\bEOF\b|no such host|HTTP 50[0234]|Bad Gateway|"
        r"Service Unavailable|Gateway Time-?out|Something went wrong while executing|stream error", re.IGNORECASE)),
]  # fmt: skip


def classify_stderr(stderr: str) -> type[ForgeError]:
    for cls, pattern in _STDERR_PATTERNS:
        if pattern.search(stderr):
            return cls
    return ForgeError


def classify_http(status: int, body: str = "") -> type[ForgeError]:
    """The error class for an HTTP status. 403 with a rate-limit body is a rate limit, not an auth failure."""
    if status == 429 or (status == 403 and re.search(r"rate limit", body, re.IGNORECASE)):
        return RateLimited
    if status in (401, 403):
        return AuthError
    if status == 404:
        return NotFound
    if status in (406, 413, 422) and re.search(r"too large|exceed|diff", body, re.IGNORECASE):
        return TooLarge
    if status >= 500 or status == 408:
        return NetworkError
    return ForgeError
