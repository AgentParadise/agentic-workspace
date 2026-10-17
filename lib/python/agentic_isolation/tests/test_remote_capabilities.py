"""Checks for the remote-tier capabilities that need no daemon (#1911).

Behaviour is proven against real Docker in
tests/integration/test_remote_capabilities_conformance.py.
"""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_isolation import (
    StreamOutcome,
    SupportsFileTransfer,
    SupportsOwnership,
    SupportsStreamingExec,
    WorkspaceDockerProvider,
    WorkspaceLocalProvider,
    WorkspaceOwnershipError,
)
from agentic_isolation.providers import docker_capabilities
from agentic_isolation.providers.capabilities import mismatched_labels
from agentic_isolation.providers.docker_capabilities import DockerStreamSession


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


# The Docker stream session and read_files below run against real local
# processes and a real GNU tar archive; only the docker CLI call that would
# need a daemon is replaced.


async def _session_over(code: str) -> DockerStreamSession:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return DockerStreamSession("unused", proc, "/tmp/unused.pid", None)


async def test_stream_lines_have_no_length_limit() -> None:
    session = await _session_over(
        "import os; os.write(1, b'first\\n' + b'x' * 70000 + b'\\n' + b'y' * 70000)"
    )

    lines = [line async for line in session.lines()]

    assert lines == ["first", "x" * 70000, "y" * 70000]


async def test_a_docker_client_error_is_disconnected_not_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_daemon(*args: str) -> tuple[int, bytes, bytes]:
        return 1, b"", b"Cannot connect to the Docker daemon"

    monkeypatch.setattr(docker_capabilities, "_docker", no_daemon)
    session = await _session_over(
        "import sys; sys.stderr.write('Cannot connect to the Docker daemon'); sys.exit(1)"
    )

    lines = [line async for line in session.lines()]
    outcome = await session.outcome()

    assert lines == []
    assert outcome == StreamOutcome(
        end="disconnected", detail="Cannot connect to the Docker daemon"
    )


async def test_a_recorded_sandbox_exit_is_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    async def recorded(*args: str) -> tuple[int, bytes, bytes]:
        return 0, b"1\n", b""

    monkeypatch.setattr(docker_capabilities, "_docker", recorded)
    session = await _session_over("import sys; sys.exit(1)")

    assert await session.outcome() == StreamOutcome(end="completed", exit_code=1)


async def test_a_recorded_sandbox_exit_survives_repeated_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real supervisor runs locally; only the docker exec hop is replaced,
    # so the status file is really written, read and removed.
    async def local_exec(*args: str) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *args[2:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode if proc.returncode is not None else -1, out, err

    monkeypatch.setattr(docker_capabilities, "_docker", local_exec)
    pid_file = str(tmp_path / "command.pid")
    proc = await asyncio.create_subprocess_exec(
        *("sh", "-c", docker_capabilities._SUPERVISE, pid_file, "--"),
        *("sh", "-c", "echo payload; exit 7"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    session = DockerStreamSession("local", proc, pid_file, None)

    assert [line async for line in session.lines()] == ["payload"]
    first = await session.outcome()
    assert first == StreamOutcome(end="completed", exit_code=7)
    assert not Path(pid_file + ".rc").exists()
    assert await session.outcome() == first


async def test_cancel_after_a_disconnect_still_stops_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, ...]] = []

    async def record(*args: str) -> tuple[int, bytes, bytes]:
        requests.append(args)
        return 1, b"", b""  # no exit recorded: the sandbox may still run

    monkeypatch.setattr(docker_capabilities, "_docker", record)
    session = await _session_over("import time; time.sleep(60)")
    session._proc.kill()
    assert (await session.outcome()).disconnected
    requests.clear()

    with pytest.raises(RuntimeError, match="could not stop the sandbox process"):
        await session.cancel()

    assert [request[:2] for request in requests] == [("exec", "unused")]
    assert "kill -KILL" in requests[0][-2]


@pytest.mark.parametrize("failure", ["nonzero", "oserror"])
async def test_a_failed_stop_publishes_nothing_and_can_be_retried(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    requests: list[tuple[str, ...]] = []

    async def unavailable(*args: str) -> tuple[int, bytes, bytes]:
        requests.append(args)
        if failure == "oserror":
            raise OSError("docker client missing")
        return 1, b"", b"transport unavailable"

    monkeypatch.setattr(docker_capabilities, "_docker", unavailable)
    session = await _session_over("import time; time.sleep(60)")
    session._proc.kill()
    assert (await session.outcome()).disconnected
    requests.clear()

    with pytest.raises((RuntimeError, OSError)):
        await session.cancel()
    assert (await session.outcome()).disconnected
    requests.clear()
    with pytest.raises((RuntimeError, OSError)):
        await session.cancel()

    assert [request[:2] for request in requests] == [("exec", "unused")]
    assert "kill -KILL" in requests[0][-2]


async def test_concurrent_stops_share_one_confirmed_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stops: list[tuple[str, ...]] = []

    async def stopped(*args: str) -> tuple[int, bytes, bytes]:
        stops.append(args)
        await asyncio.sleep(0.05)
        session._proc.kill()  # the sandbox kill ends the docker client
        return 0, b"", b""

    monkeypatch.setattr(docker_capabilities, "_docker", stopped)
    session = await _session_over("import time; time.sleep(60)")

    await asyncio.gather(session.cancel(), session.cancel())

    assert len(stops) == 1
    assert await session.outcome() == StreamOutcome(end="completed", signal=signal.SIGKILL)


async def test_concurrent_observers_agree_on_a_recorded_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As above, the real supervisor writes the status file; requests are
    # serviced one at a time, as one docker daemon would.
    serviced = asyncio.Lock()

    async def local_exec(*args: str) -> tuple[int, bytes, bytes]:
        async with serviced:
            proc = await asyncio.create_subprocess_exec(
                *args[2:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await proc.communicate()
        return proc.returncode if proc.returncode is not None else -1, out, err

    monkeypatch.setattr(docker_capabilities, "_docker", local_exec)
    pid_file = str(tmp_path / "command.pid")
    proc = await asyncio.create_subprocess_exec(
        *("sh", "-c", docker_capabilities._SUPERVISE, pid_file, "--"),
        *("sh", "-c", "exit 7"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    session = DockerStreamSession("local", proc, pid_file, None)
    assert [line async for line in session.lines()] == []

    observed = await asyncio.gather(session.outcome(), session.outcome())

    for outcome in observed:
        assert outcome == StreamOutcome(end="completed", exit_code=7)


async def test_read_files_returns_every_name_of_a_hard_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.bin").write_bytes(bytes(range(256)))
    os.link(tmp_path / "a.bin", tmp_path / "b.bin")
    archive = subprocess.run(
        ["tar", "-cf", "-", "-C", str(tmp_path), "."], check=True, capture_output=True
    ).stdout

    async def tar_transport(*args: str) -> tuple[int, bytes, bytes]:
        return 0, archive, b""

    monkeypatch.setattr(docker_capabilities, "_docker", tar_transport)

    for name in ("a.bin", "b.bin"):
        assert await docker_capabilities.read_files("unused", [name], "/workspace") == [
            (name, bytes(range(256)))
        ]
