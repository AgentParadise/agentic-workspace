"""codex's upstream-failure classifier, through the package export. See syntropic137#1605.

Fixtures are `codex exec --json` `turn.failed` events in
`fixtures/upstream_failure/codex.jsonl`; see the README beside them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic_isolation.harnesses.codex import classify_upstream_failure
from agentic_isolation.harnesses.upstream_failure import UpstreamFailure, UpstreamFailureKind

_FIXTURES = Path(__file__).parent / "fixtures" / "upstream_failure" / "codex.jsonl"
_SEEN = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)


def _messages() -> list[str]:
    lines = _FIXTURES.read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["error"]["message"] for line in lines]


def test_recorded_faults_are_classified() -> None:
    capacity, usage_limit, refusal = _messages()
    assert classify_upstream_failure(capacity, observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.CAPACITY
    )
    assert classify_upstream_failure(usage_limit, observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.QUOTA, datetime(2026, 10, 9, 21, 10, tzinfo=UTC)
    )
    assert classify_upstream_failure(refusal, observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.REFUSAL
    )


def test_syntropic137_prefix_does_not_hide_the_phrase() -> None:
    capacity = _messages()[0]
    assert classify_upstream_failure(
        f"codex reported: {capacity}", observed_at=_SEEN
    ) == UpstreamFailure(UpstreamFailureKind.CAPACITY)


@pytest.mark.parametrize(
    ("reason", "kind", "resets_at"),
    [
        # codex-rs/protocol/src/error.rs spellings, with its U+2019 apostrophe.
        (
            "You’ve hit your usage limit. To get more access now, send a request to your admin"
            " or try again at Nov 1st, 2026 7:05 AM.",
            UpstreamFailureKind.QUOTA,
            datetime(2026, 11, 1, 7, 5, tzinfo=UTC),
        ),
        (
            "You’ve hit your usage limit for GPT-5. Switch to another model now,"
            " or try again later.",
            UpstreamFailureKind.QUOTA,
            None,
        ),
        ("Quota exceeded. Check your plan and billing details.", UpstreamFailureKind.QUOTA, None),
        ("rate limit exceeded: slow down", UpstreamFailureKind.RATE_LIMITED, None),
        ("unexpected status 429 Too Many Requests: {}", UpstreamFailureKind.RATE_LIMITED, None),
        ("unexpected status 401 Unauthorized: {}", UpstreamFailureKind.AUTH, None),
        (
            "ERROR codex_login::auth::manager: Failed to refresh token: 401 Unauthorized",
            UpstreamFailureKind.AUTH,
            None,
        ),
        (
            "Authentication failed (HTTP 401): codex CLI login - "
            "ERROR codex_login::auth::manager: Failed to refresh token: 401 Unauthorized",
            UpstreamFailureKind.AUTH,
            None,
        ),
    ],
)
def test_codex_source_spellings(
    reason: str, kind: UpstreamFailureKind, resets_at: datetime | None
) -> None:
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(kind, resets_at)


@pytest.mark.parametrize(
    "reason",
    [
        "codex stream ended without a terminal turn.completed event",
        "INFO codex_login::auth::manager: loaded cached credentials",
        "We’re currently experiencing high demand, which may cause temporary errors.",
    ],
)
def test_unrecognised_fault_is_unknown_never_none(reason: str) -> None:
    assert classify_upstream_failure(reason, observed_at=_SEEN) == UpstreamFailure(
        UpstreamFailureKind.UNKNOWN
    )


@pytest.mark.parametrize("reason", [None, "", "  \n"])
def test_clean_run_is_none(reason: str | None) -> None:
    assert classify_upstream_failure(reason, observed_at=_SEEN) is None
