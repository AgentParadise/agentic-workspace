"""claude's upstream-failure classifier, through the package export. See syntropic137#1605.

Fixtures are claude CLI `result` events in `fixtures/upstream_failure/claude.jsonl`;
see the README beside them for which are recorded live.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic_isolation.harnesses.claude import classify_upstream_failure
from agentic_isolation.harnesses.upstream_failure import UpstreamFailure, UpstreamFailureKind

_FIXTURES = Path(__file__).parent / "fixtures" / "upstream_failure" / "claude.jsonl"
_SEEN = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)


def _result(session_id: str) -> str:
    for line in _FIXTURES.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["session_id"] == session_id:
            return event["result"]
    raise KeyError(session_id)


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        (
            "quota-oct-23",
            UpstreamFailure(UpstreamFailureKind.QUOTA, datetime(2026, 10, 23, 15, tzinfo=UTC)),
        ),
        (
            "quota-oct-15",
            UpstreamFailure(UpstreamFailureKind.QUOTA, datetime(2026, 10, 15, 8, tzinfo=UTC)),
        ),
        ("overloaded", UpstreamFailure(UpstreamFailureKind.CAPACITY)),
        ("rate-limited", UpstreamFailure(UpstreamFailureKind.RATE_LIMITED)),
        ("unauthenticated", UpstreamFailure(UpstreamFailureKind.AUTH)),
        ("forbidden", UpstreamFailure(UpstreamFailureKind.AUTH)),
    ],
)
def test_recorded_fault_is_classified(session_id: str, expected: UpstreamFailure) -> None:
    assert classify_upstream_failure(_result(session_id), observed_at=_SEEN) == expected


def test_unplaced_fault_is_unknown_never_none() -> None:
    assert classify_upstream_failure(_result("unplaced"), observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.UNKNOWN
    )


@pytest.mark.parametrize(
    "reason", ["You've hit your session limit", "Unknown error", "API Error: 500"]
)
def test_unrecognised_fault_text_is_unknown_not_a_guess(reason: str) -> None:
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.UNKNOWN
    )


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_clean_run_is_none(reason: str | None) -> None:
    assert classify_upstream_failure(reason, observed_at=_SEEN) is None


def test_clean_fixture_is_none() -> None:
    assert classify_upstream_failure(_result("clean"), observed_at=_SEEN) is None


def test_quota_inside_a_429_body_is_quota_not_rate_limited() -> None:
    # syntropic137#1605: the sentence must stay QUOTA (never retried) when the
    # CLI sends it as a rate_limit_error's message.
    reason = (
        'API Error: 429 {"type":"error","error":{"type":"rate_limit_error",'
        '"message":"You\'ve hit your weekly limit · resets Oct 23, 3pm (UTC)"}}'
    )
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.QUOTA, datetime(2026, 10, 23, 15, tzinfo=UTC)
    )


def test_reset_rolls_over_december_to_january() -> None:
    failure = classify_upstream_failure(
        "You've hit your weekly limit · resets Jan 2, 9am (UTC)",
        observed_at=datetime(2026, 12, 30, 23, 0, tzinfo=UTC),
    )
    assert failure == UpstreamFailure(
        UpstreamFailureKind.QUOTA, datetime(2027, 1, 2, 9, tzinfo=UTC)
    )


def test_reset_already_passed_this_year_is_next_year() -> None:
    failure = classify_upstream_failure(
        _result("quota-oct-15"), observed_at=datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
    )
    assert failure is not None
    assert failure.resets_at == datetime(2027, 10, 15, 8, tzinfo=UTC)


def test_reset_12am_and_12pm_and_minutes() -> None:
    def reset(text: str) -> datetime | None:
        failure = classify_upstream_failure(
            f"You’ve hit your weekly limit · resets {text} (UTC)", observed_at=_SEEN
        )
        assert failure is not None
        return failure.resets_at

    assert reset("Oct 20, 12am") == datetime(2026, 10, 20, 0, tzinfo=UTC)
    assert reset("Oct 20, 12pm") == datetime(2026, 10, 20, 12, tzinfo=UTC)
    assert reset("Oct 20, 3:30pm") == datetime(2026, 10, 20, 15, 30, tzinfo=UTC)


def test_quota_without_a_placeable_reset_is_quota_with_no_reset() -> None:
    for reason in (
        "You've hit your weekly limit",
        "You've hit your weekly limit · resets Oct 23, 3pm (Europe/Berlin)",
    ):
        assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(
            UpstreamFailureKind.QUOTA
        )


def test_quota_sentence_quoted_mid_text_is_not_quota() -> None:
    reason = "Tool failed after the agent wrote: You've hit your weekly limit"
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.UNKNOWN
    )


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("API overloaded (HTTP 529)", UpstreamFailureKind.CAPACITY),
        ("Rate limited (HTTP 429)", UpstreamFailureKind.RATE_LIMITED),
        ("Authentication failed (HTTP 401)", UpstreamFailureKind.AUTH),
        ("Permission denied (HTTP 403)", UpstreamFailureKind.AUTH),
    ],
)
def test_syntropic137_labels_with_a_status_are_placed(
    reason: str, kind: UpstreamFailureKind
) -> None:
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(kind)


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        # Syntropic137's `_extract_error_reason` when the API message equals the label.
        ("API overloaded", UpstreamFailureKind.CAPACITY),
        ("Rate limited", UpstreamFailureKind.RATE_LIMITED),
        ("Authentication failed", UpstreamFailureKind.AUTH),
        ("Permission denied", UpstreamFailureKind.AUTH),
        # ... and when the API message differs from it.
        ("API overloaded: Overloaded", UpstreamFailureKind.CAPACITY),
        (
            "Rate limited: Number of requests has exceeded your limit",
            UpstreamFailureKind.RATE_LIMITED,
        ),
        ("Authentication failed: invalid x-api-key", UpstreamFailureKind.AUTH),
        ("Permission denied: access denied", UpstreamFailureKind.AUTH),
    ],
)
def test_syntropic137_labels_without_a_status_are_placed(
    reason: str, kind: UpstreamFailureKind
) -> None:
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(kind)


def test_quota_after_the_rate_limited_label_is_quota_not_rate_limited() -> None:
    # The weekly-limit sentence as a rate_limit_error's message, after
    # Syntropic137's `_extract_error_reason` has replaced the body with its label.
    reason = "Rate limited: You've hit your weekly limit · resets Oct 23, 3pm (UTC)"
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.QUOTA, datetime(2026, 10, 23, 15, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    ("error_type", "kind"),
    [
        ("overloaded_error", UpstreamFailureKind.CAPACITY),
        ("rate_limit_error", UpstreamFailureKind.RATE_LIMITED),
        ("authentication_error", UpstreamFailureKind.AUTH),
        ("permission_error", UpstreamFailureKind.AUTH),
    ],
)
def test_bare_structured_error_is_placed_by_its_type(
    error_type: str, kind: UpstreamFailureKind
) -> None:
    # No HTTP status anywhere and a message no label matches: only `error.type` places it.
    reason = json.dumps(
        {"type": "error", "error": {"type": error_type, "message": "upstream said no"}}
    )
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(kind)


@pytest.mark.parametrize(
    "reason",
    [
        "Invalid request: prompt is too long",
        "API internal error",
        "Resource not found (HTTP 404)",
        "The agent wrote: Rate limited",
        "Rate limitedness: unclear",
        "rate limited",
        '{"type":"error","error":{"type":"api_error","message":"Internal server error"}}',
    ],
)
def test_other_labels_and_quoted_labels_are_unknown(reason: str) -> None:
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.UNKNOWN
    )


def test_naive_observed_at_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        classify_upstream_failure("Connection refused", observed_at=datetime(2026, 10, 13))


def test_resets_at_is_quota_only_and_utc() -> None:
    at = datetime(2026, 10, 23, 15, tzinfo=UTC)
    with pytest.raises(ValueError, match="only meaningful for QUOTA"):
        UpstreamFailure(UpstreamFailureKind.RATE_LIMITED, at)
    with pytest.raises(ValueError, match="UTC"):
        UpstreamFailure(UpstreamFailureKind.QUOTA, datetime(2026, 10, 23, 15))
