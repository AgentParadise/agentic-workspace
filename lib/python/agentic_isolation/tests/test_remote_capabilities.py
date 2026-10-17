"""Shape checks for the remote-tier capabilities that need no daemon (#1911).

Behaviour is proven against real Docker in
tests/integration/test_remote_capabilities_conformance.py.
"""

from agentic_isolation import (
    StreamOutcome,
    SupportsFileTransfer,
    SupportsOwnership,
    SupportsStreamingExec,
    WorkspaceDockerProvider,
    WorkspaceLocalProvider,
    WorkspaceOwnershipError,
)
from agentic_isolation.providers.capabilities import mismatched_labels


def test_docker_provider_claims_all_three_capabilities() -> None:
    provider = WorkspaceDockerProvider()

    assert isinstance(provider, SupportsStreamingExec)
    assert isinstance(provider, SupportsFileTransfer)
    assert isinstance(provider, SupportsOwnership)


def test_local_provider_claims_none_of_them() -> None:
    provider = WorkspaceLocalProvider.__new__(WorkspaceLocalProvider)

    assert not isinstance(provider, SupportsStreamingExec)
    assert not isinstance(provider, SupportsFileTransfer)
    assert not isinstance(provider, SupportsOwnership)


def test_stream_outcome_ends_are_exclusive() -> None:
    timed_out = StreamOutcome(end="timed_out")
    disconnected = StreamOutcome(end="disconnected")

    assert (timed_out.timed_out, timed_out.disconnected, timed_out.completed) == (
        True,
        False,
        False,
    )
    assert (disconnected.timed_out, disconnected.disconnected, disconnected.completed) == (
        False,
        True,
        False,
    )


def test_a_missing_expected_label_is_a_mismatch() -> None:
    assert mismatched_labels(
        {"syn.host_id": "h"}, {"syn.host_id": "h", "syn.execution_id": "e"}
    ) == ["syn.execution_id"]
    error = WorkspaceOwnershipError("ws-1", ["syn.execution_id"])
    assert error.mismatched == ("syn.execution_id",)
    assert "ws-1" in str(error)
