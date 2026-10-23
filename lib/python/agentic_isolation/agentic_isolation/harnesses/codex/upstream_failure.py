"""Which upstream fault a codex CLI error message reports. See syntropic137#1605.

Input is codex's own words about a failed turn: the message of a
``turn.failed`` or ``error`` event on the ``codex exec --json`` stream, or a
login-failure tracing line codex writes to stdout. Each phrase is searched
for anywhere in it, so a caller's own prefix (Syntropic137 writes
``codex reported: <message>``) does not hide it.

Every phrase below is codex's own, from ``codex-rs/protocol/src/error.rs``
or a recorded run; codex spells its apostrophes both ways (``You’ve`` in
source, ``You've`` in a recorded line), so both are accepted. A phrasing not
listed reads as UNKNOWN, the safe direction to be wrong in: it is not
retried.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from agentic_isolation.harnesses.upstream_failure import UpstreamFailure, UpstreamFailureKind

__all__ = ["classify_upstream_failure"]

_KIND = UpstreamFailureKind

#: Checked in order; QUOTA before RATE_LIMITED so an allowance that is spent
#: never reads as transient.
_PHRASES: tuple[tuple[re.Pattern[str], UpstreamFailureKind], ...] = (
    # `UsageLimitReached`, every variant; and `QuotaExceeded`.
    (re.compile(r"You['’]ve hit your usage limit", re.IGNORECASE), _KIND.QUOTA),
    (re.compile(r"Quota exceeded\. Check your plan and billing details\."), _KIND.QUOTA),
    # `ServerOverloaded`, observed live in syntropic137#1303.
    (re.compile(r"Selected model is at capacity\. Please try a different model\."), _KIND.CAPACITY),
    # `RateLimitExceeded`, and an HTTP 429 in `unexpected status 429 Too Many Requests: ...`.
    (re.compile(r"rate limit exceeded:|unexpected status 429\b"), _KIND.RATE_LIMITED),
    # `CyberPolicy`, recorded twice in Syntropic137 (exec-61dad6055e6f, exec-898cd870650e).
    (re.compile(r"flagged for possible cybersecurity risk", re.IGNORECASE), _KIND.REFUSAL),
    (re.compile(r"unexpected status 40[13]\b"), _KIND.AUTH),
)

#: A login failure on stdout, e.g. ``ERROR codex_login::auth::manager: Failed to
#: refresh token: 401 Unauthorized``. All three are required: an error
#: severity, the login subsystem, and a failure marker, because healthy
#: lines name the subsystem too (``INFO codex_login::...: loaded cached credentials``).
_LOGIN_SEVERITY = re.compile(r"\b(?:ERROR|FATAL)\b")
_LOGIN_CONTEXT = re.compile(r"codex_login|auth::", re.IGNORECASE)
_LOGIN_FAILURE = re.compile(
    r"failed to refresh token|refresh_token_reused|invalid_grant|unauthorized"
    r"|authentication failed|token expired|login required|\b40[13]\b",
    re.IGNORECASE,
)

#: ``try again at Oct 9th, 2026 9:10 PM``. Codex prints no zone; the
#: workspace runs in UTC, so the time is read as UTC. A time with no date
#: (``try again at 9:10 PM``) cannot be placed and reads as unstated.
_RESET = re.compile(
    r"try again at (?P<month>[A-Z][a-z]{2})[a-z]* (?P<day>\d{1,2})(?:st|nd|rd|th)?, "
    r"(?P<year>\d{4}) (?P<hour>\d{1,2}):(?P<minute>\d{2}) ?(?P<ampm>[AP]M)",
    re.IGNORECASE,
)


def classify_upstream_failure(
    reason: str | None, *, observed_at: datetime
) -> UpstreamFailure | None:
    """The upstream fault codex's error message ``reason`` reports.

    ``observed_at`` is accepted for parity with the other harnesses and
    must be timezone-aware; codex states its reset with a year, so it is
    not needed to place one. Returns None only when ``reason`` is None or
    blank: codex reported no fault.
    """
    if observed_at.utcoffset() is None:
        raise ValueError(f"observed_at must be timezone-aware, got {observed_at!r}")
    text = (reason or "").strip()
    if not text:
        return None
    for phrase, kind in _PHRASES:
        if phrase.search(text):
            resets_at = _reset(text) if kind is _KIND.QUOTA else None
            return UpstreamFailure(kind, resets_at)
    if all(marker.search(text) for marker in (_LOGIN_SEVERITY, _LOGIN_CONTEXT, _LOGIN_FAILURE)):
        return UpstreamFailure(_KIND.AUTH)
    return UpstreamFailure(_KIND.UNKNOWN)


def _reset(message: str) -> datetime | None:
    match = _RESET.search(message)
    if match is None:
        return None
    try:
        stamp = datetime.strptime(
            "{month} {day} {year} {hour}:{minute} {ampm}".format(**match.groupdict()),
            "%b %d %Y %I:%M %p",
        )
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC)
