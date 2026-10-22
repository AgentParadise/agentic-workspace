"""Which upstream fault a claude CLI error result reports. See syntropic137#1605.

Input is the text of the CLI's ``is_error`` ``result`` event, as the CLI
wrote it: plain prose such as the quota sentence, or an API error such as
``API Error: 529 {"type":"error","error":{"type":"overloaded_error",...}}``.
Syntropic137's shortened label for the same fault (``API overloaded (HTTP
529)``) is also understood through its HTTP status, but only the raw text
keeps a quota sentence the API sent inside a 429 body, so pass the raw text.

Recognised, most specific first:

1. The weekly-limit sentence, alone or as an API error's message. Seen live
   twice: ``You've hit your weekly limit · resets Oct 15, 8am (UTC)``. QUOTA,
   with the reset placed in time (see `_reset_after`). Claude's other
   allowances (the 5-hour session limit, per-model weekly limits) have no
   recorded text yet, so they read as UNKNOWN rather than as a guessed
   spelling. Add each here with a fixture once it is seen.
2. The API error ``error.type`` in an embedded JSON body.
3. The HTTP status, from ``API Error: <status>`` or ``(HTTP <status>)``.

Anything else non-blank is UNKNOWN.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from agentic_isolation.harnesses.upstream_failure import UpstreamFailure, UpstreamFailureKind

__all__ = ["classify_upstream_failure"]

_KIND = UpstreamFailureKind

#: The Anthropic API's error ``type`` values, and what each means upstream.
_ERROR_TYPES: dict[str, UpstreamFailureKind] = {
    "overloaded_error": _KIND.CAPACITY,
    "rate_limit_error": _KIND.RATE_LIMITED,
    "authentication_error": _KIND.AUTH,
    "permission_error": _KIND.AUTH,
}

_HTTP_STATUSES: dict[str, UpstreamFailureKind] = {
    "529": _KIND.CAPACITY,
    "429": _KIND.RATE_LIMITED,
    "401": _KIND.AUTH,
    "403": _KIND.AUTH,
}

_HTTP_STATUS = re.compile(r"API Error: (\d{3})\b|\(HTTP (\d{3})\)")

#: Anchored at the start: the sentence is the whole of the CLI's error result,
#: and agent prose that merely mentions it must not read as a quota.
_WEEKLY_LIMIT = re.compile(r"\AYou['’]ve hit your weekly limit\b")

#: ``resets Oct 23, 3pm (UTC)``. No year: see `_reset_after`. A reset in any
#: zone other than UTC is not placed, and reads as unstated.
_RESET = re.compile(
    r"resets (?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}), "
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s?(?P<ampm>[ap]m) \(UTC\)"
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def classify_upstream_failure(
    reason: str | None, *, observed_at: datetime
) -> UpstreamFailure | None:
    """The upstream fault claude's error result ``reason`` reports.

    ``observed_at`` is when the result event was seen (timezone-aware); it
    places a year-less quota reset. Returns None only when ``reason`` is
    None or blank: claude reported no fault.
    """
    if observed_at.utcoffset() is None:
        raise ValueError(f"observed_at must be timezone-aware, got {observed_at!r}")
    text = (reason or "").strip()
    if not text:
        return None
    error = _api_error(text)
    message = error.get("message") if error else None
    for sentence in (text, message):
        if isinstance(sentence, str) and _WEEKLY_LIMIT.match(sentence.strip()):
            return UpstreamFailure(_KIND.QUOTA, _reset_after(sentence, observed_at))
    kind = _ERROR_TYPES.get(str(error.get("type"))) if error else None
    if kind is None:
        status = _HTTP_STATUS.search(text)
        kind = _HTTP_STATUSES.get(status.group(1) or status.group(2)) if status else None
    return UpstreamFailure(kind or _KIND.UNKNOWN)


def _api_error(text: str) -> dict[str, object] | None:
    """The ``error`` object of the first JSON body embedded in ``text``, if any."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        body, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    return error if isinstance(error, dict) else None


def _reset_after(sentence: str, observed_at: datetime) -> datetime | None:
    """The first time matching the sentence's ``resets ...`` strictly after ``observed_at``.

    The CLI prints no year, and a reset is always in the future, so the year
    is the observation's own, or the next one when that date has passed
    (``resets Jan 2`` seen on Dec 30). Later years are tried for Feb 29.
    """
    match = _RESET.search(sentence)
    if match is None or match["month"] not in _MONTHS:
        return None
    hour = int(match["hour"])
    if not 1 <= hour <= 12:
        return None
    hour = hour % 12 + (12 if match["ampm"] == "pm" else 0)
    minute = int(match["minute"] or 0)
    observed = observed_at.astimezone(UTC)
    for year in range(observed.year, observed.year + 9):
        try:
            candidate = datetime(
                year, _MONTHS.index(match["month"]) + 1, int(match["day"]), hour, minute, tzinfo=UTC
            )
        except ValueError:
            continue
        if candidate > observed:
            return candidate
    return None
