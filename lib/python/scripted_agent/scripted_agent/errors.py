"""Why the replayer stopped, and the exit code each reason maps to (sysexits.h)."""

from __future__ import annotations

from typing import ClassVar


class Refusal(Exception):
    """The replayer will not carry out this phase. Exits non-zero with the message."""

    exit_code: ClassVar[int] = 70  # EX_SOFTWARE


class ProfileRefused(Refusal):
    """No profile, an invalid one, or no phase/execution to read it for."""

    exit_code = 78  # EX_CONFIG


class RecordingRefused(Refusal):
    """The recording is not the stream the profile declares."""

    exit_code = 65  # EX_DATAERR


class SideEffectFailed(Refusal):
    """A workload or git/gh step the phase declares did not succeed."""

    exit_code = 69  # EX_UNAVAILABLE
