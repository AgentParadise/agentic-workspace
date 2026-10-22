"""What kind of upstream fault a harness reported. See syntropic137#1605.

Whether a failed run is worth retrying depends on what the provider said:
no capacity this second (retry), too many requests (retry, later), an
allowance spent until a calendar time (do not retry), or credentials
refused (an operator must act). Recognising which one a harness's own error
text means is harness knowledge: it changes when a CLI changes. So each
harness package exports its own pure ``classify_upstream_failure`` and owns
its own strings; this module holds only the shared answer type.

Every classifier follows one rule: ``None`` means the harness reported no
fault (no text, or blank text). Any other text is a fault, and a fault the
classifier cannot place is ``UNKNOWN``, never ``None`` and never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

__all__ = ["UpstreamFailure", "UpstreamFailureKind"]


class UpstreamFailureKind(StrEnum):
    """The kind of upstream fault, which decides what the caller does next."""

    #: Transient: the provider is overloaded (claude HTTP 529, codex "at capacity").
    CAPACITY = "capacity"
    #: Transient: too many requests (HTTP 429, claude ``rate_limit_error``).
    RATE_LIMITED = "rate_limited"
    #: NOT transient: the account's allowance is spent until a calendar time.
    QUOTA = "quota"
    #: Credentials refused: HTTP 401/403, claude authentication or permission
    #: errors, codex login failures. Retrying cannot fix it.
    AUTH = "auth"
    #: The provider's content policy declined the request. Retrying the same
    #: request on the same harness gets the same answer.
    REFUSAL = "refusal"
    #: The harness reported a fault this classifier could not place.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UpstreamFailure:
    """One classified upstream fault.

    ``resets_at`` is set only for ``QUOTA``, and only when the harness stated
    when the allowance returns. It is always timezone-aware UTC.
    """

    kind: UpstreamFailureKind
    resets_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.resets_at is None:
            return
        if self.kind is not UpstreamFailureKind.QUOTA:
            raise ValueError(f"resets_at is only meaningful for QUOTA, not {self.kind}")
        if self.resets_at.utcoffset() != timedelta(0):
            raise ValueError(f"resets_at must be timezone-aware UTC, got {self.resets_at!r}")
        object.__setattr__(self, "resets_at", self.resets_at.astimezone(UTC))
