"""Conformance suite for the Docker `SupportsOperations` implementation.

Plan section 7 row 3, exit tests (a)-(m). Each test drives the real
`DockerOperations` (wrapper scripts, collector, spool, idempotency record,
group cancel) through a stand-in `docker` executable that runs `exec` as host
processes, so the logic runs here and in CI without a Docker daemon. What this
stand-in cannot prove is daemon behaviour itself (namespaces, `-e NAME`
filtering); that is left to the Docker-backed integration suite.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import signal
import stat
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import (
    CredentialBinding,
    CredentialRef,
    LaunchMarker,
    OperationRef,
    OperationSpec,
)
from agentic_isolation.providers import docker_operations
from agentic_isolation.providers.base import (
    SupportsOperations,
    Workspace,
    WorkspaceProvider,
    drain_operation,
    run_to_completion,
)
from agentic_isolation.providers.docker import WorkspaceDockerProvider
from agentic_isolation.providers.docker_operations import DockerOperations, _Operation
from agentic_isolation.providers.operation_spool import OperationFailure, OperationSpool

# `exec` drops docker's own flags and runs the command here. `container
# inspect` answers from $FAKE_DOCKER_STATE: absent = running, `removing` or
# `exited` (reported as `.State.Status`), `gone`, or `down` (daemon
# unreachable). Every argv is appended to $FAKE_DOCKER_LOG.
FAKE_DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
state=$(cat "$FAKE_DOCKER_STATE" 2>/dev/null)
case "$1" in
exec)
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
        -i) shift ;;
        -w) shift 2 ;;
        -e) case "$2" in *=*) export "$2" ;; esac; shift 2 ;;
        *) break ;;
        esac
    done
    shift
    exec "$@" ;;
container)
    case "$state" in
    gone) echo "Error: No such container: c1" >&2; exit 1 ;;
    down) echo "Cannot connect to the Docker daemon" >&2; exit 1 ;;
    removing|exited) echo "$state" ;;
    *) echo running ;;
    esac ;;
stop) exit 0 ;;
esac
"""

SECRET = "s3cr3t-value-0b5e"


class _Resolver:
    async def resolve(self, ref: CredentialRef) -> str:
        return SECRET


@pytest.fixture
def docker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _ops(docker_env: Path, *, retention: int = 1 << 20, page: int = 1 << 16) -> DockerOperations:
    return DockerOperations(
        credentials=_Resolver(),
        retention_bytes=retention,
        max_page_bytes=page,
        docker=str(docker_env / "docker"),
    )


def _workspace() -> Workspace:
    return Workspace(
        id="ws1", provider="docker", path=Path("."), config=WorkspaceConfig(), _handle="c1"
    )


def _spec(script: str, *, key: str = "k1", **overrides: object) -> OperationSpec:
    fields: dict[str, object] = {
        "execution_id": "exec-1",
        "operation_key": key,
        "argv": ("sh", "-c", script),
        "working_directory": ".",
        "environment": {},
        "credential_bindings": (),
        "timeout_seconds": 60,
        "metadata": {},
    }
    fields.update(overrides)
    return OperationSpec.model_validate(fields)


async def _pages(ops: DockerOperations, ref: OperationRef, max_bytes: int = 1 << 16) -> list[str]:
    """Every serialised page until complete."""
    out: list[str] = []
    cursor: str | None = None
    while True:
        page = await ops.observe(ref, cursor=cursor, max_bytes=max_bytes, wait_seconds=1.0)
        out.append(page.model_dump_json())
        cursor = page.next_cursor
        if page.complete:
            return out


def _data(pages: list[str]) -> bytes:
    from agentic_isolation.contract import ObservationPage

    return b"".join(
        base64.b64decode(ObservationPage.model_validate_json(p).data_b64) for p in pages
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/status").read_text()
    return "\nState:\tZ" not in status


def test_docker_provider_supports_operations_without_widening_the_base() -> None:
    assert isinstance(WorkspaceDockerProvider(), SupportsOperations)
    assert "start" not in WorkspaceProvider.__dict__


@pytest.mark.parametrize("pid", ["0", "1", "-1", "bad", str(os.getpgrp())])
async def test_cancel_never_signals_an_unsafe_process_group_and_escalates(
    pid: str, docker_env: Path
) -> None:
    ops = _ops(docker_env)
    ref = OperationRef(
        workspace={"provider": "docker", "resource_id": "c1"},
        operation_id="unsafe",
        operation_key="unsafe",
    )
    operation = _Operation(
        ref=ref,
        spec=_spec("true"),
        container="c1",
        spool=OperationSpool(retention_bytes=1024),
        started_at="now",
        key=("ws1", "unsafe"),
        leader=(pid, "123"),
    )
    operation.leader_known.set()
    ops._operations[ref.operation_id] = operation
    # The stand-in runs `exec` here, so the namespace probe reports ours.
    own_namespace = os.readlink("/proc/self/ns/pid")
    command = AsyncMock(return_value=(0, own_namespace))
    ops._docker_cmd = command  # type: ignore[method-assign]
    status = await ops.cancel(ref, grace_seconds=0)
    assert status.metadata["cancel_escalation"] == "container_stopped"
    assert "refusing unsafe process group" in status.metadata["cancel_signal_failure"]
    calls = [c.args for c in command.await_args_list]
    assert not any("aw-signal" in c or "aw-processes" in c for c in calls)
    assert calls[-1] == ("stop", "-t", "0", "c1")


async def test_cancel_signals_a_leader_numbered_like_our_group_in_another_namespace(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production: the caller's pgrp and the in-container leader pid collide."""
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("exec sleep 30"))
    operation = ops._operations[ref.operation_id]
    await operation.leader_known.wait()
    assert operation.leader is not None
    leader = int(operation.leader[0])
    real_exec = ops._exec

    async def foreign_namespace(container: str, *argv: str) -> tuple[int, str]:
        if "aw-pidns" in argv:
            return 0, "pid:[1]\n"
        return await real_exec(container, *argv)

    monkeypatch.setattr(ops, "_exec", foreign_namespace)
    monkeypatch.setattr(docker_operations.os, "getpgrp", lambda: leader)
    status = await asyncio.wait_for(ops.cancel(ref, grace_seconds=1), timeout=20)
    assert status.state == "cancelled"
    assert "cancel_escalation" not in status.metadata
    assert not _alive(leader)


async def test_timeout_settles_when_the_leader_is_refused(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused group on the timeout path escalates and still settles."""
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("exec sleep 3", timeout_seconds=1))
    operation = ops._operations[ref.operation_id]
    await operation.leader_known.wait()
    assert operation.leader is not None
    monkeypatch.setattr(docker_operations.os, "getpgrp", lambda: int(operation.leader[0]))
    status, _ = await asyncio.wait_for(drain_operation(ops, ref), timeout=20)
    assert status.state == "timed_out"
    assert status.metadata["cancel_escalation"] == "container_stopped"
    assert "caller's own group" in status.metadata["cancel_signal_failure"]


async def test_timeout_settles_when_even_the_stop_cannot_run(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("exec sleep 2", timeout_seconds=1))

    async def unavailable(operation: _Operation, *, grace_seconds: float) -> None:
        raise docker_operations._error("provider_unavailable", "docker is gone")

    monkeypatch.setattr(ops, "_terminate_group", unavailable)
    status, _ = await asyncio.wait_for(drain_operation(ops, ref), timeout=20)
    assert status.state == "timed_out"
    assert status.metadata["cancel_signal_failure"] == "docker is gone"


def _scripted(ops: DockerOperations, tables: list[str]) -> list[tuple[str, ...]]:
    """Answer `_exec` from canned process tables; record every argv, signal nothing."""
    calls: list[tuple[str, ...]] = []

    async def command(container: str, *argv: str) -> tuple[int, str]:
        calls.append(argv)
        if argv[3] == "aw-processes":
            return 0, tables.pop(0) if len(tables) > 1 else tables[0]
        return 0, ""

    ops._exec = command  # type: ignore[method-assign]
    ops._docker_cmd = AsyncMock(return_value=(0, ""))  # type: ignore[method-assign]
    return calls


def _held_operation(leader: tuple[str, str]) -> _Operation:
    ref = OperationRef(
        workspace={"provider": "docker", "resource_id": "c1"},
        operation_id="held",
        operation_key="held",
    )
    return _Operation(
        ref=ref,
        spec=_spec("true"),
        container="c1",
        spool=OperationSpool(retention_bytes=1024),
        started_at="now",
        key=("ws1", "held"),
        leader=leader,
    )


async def test_cancel_never_signals_the_group_of_a_reused_leader(docker_env: Path) -> None:
    ops = _ops(docker_env)
    # pid 999999 now belongs to a process with another start time.
    calls = _scripted(ops, ["999999 1 999999 777\n"])
    operation = _held_operation(("999999", "123"))
    await ops._terminate_group(operation, grace_seconds=0)
    signals = [argv for argv in calls if argv[3] == "aw-signal"]
    assert signals == [("sh", "-c", signals[0][2], "aw-signal", "TERM", "", "123")]
    assert not operation.escalated


async def test_cancel_signals_survivors_of_an_exited_leader(docker_env: Path) -> None:
    ops = _ops(docker_env)
    # The leader 999999 is gone; its child 999998 is still in its group.
    calls = _scripted(ops, ["999998 1 999999 456\n", ""])
    operation = _held_operation(("999999", "123"))
    await ops._terminate_group(operation, grace_seconds=0)
    signals = [argv[4:] for argv in calls if argv[3] == "aw-signal"]
    assert signals == [("TERM", "999999", "123", "999998", "456")]


async def test_cancel_escalates_and_reports_a_failed_signal(docker_env: Path) -> None:
    ops = _ops(docker_env)

    async def command(container: str, *argv: str) -> tuple[int, str]:
        if argv[3] == "aw-processes":
            return 0, "999999 1 999999 123\n"
        return 126, "sh: not found"

    ops._exec = command  # type: ignore[method-assign]
    ops._docker_cmd = AsyncMock(return_value=(0, ""))  # type: ignore[method-assign]
    operation = _held_operation(("999999", "123"))
    await ops._terminate_group(operation, grace_seconds=0)
    metadata = ops._status(operation).metadata
    assert metadata["cancel_escalation"] == "container_stopped"
    assert metadata["cancel_signal_failure"] == "kill -TERM: exit 126: sh: not found"
    ops._docker_cmd.assert_awaited_once_with("stop", "-t", "0", "c1")


# Overrides the shell's `kill` builtin: fails as a real kill would, signals nothing.
_FAILING_KILL = "kill() { echo simulated-kill-failure >&2; return 42; }; "


def _start_of(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]


def _run_signal_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", _FAILING_KILL + docker_operations._SIGNAL, "aw-signal", *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


@contextlib.contextmanager
def _sleeper():
    """A live process in its own session, so no group around it is ours."""
    process = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait()


def test_signal_script_reports_a_kill_that_fails_on_a_live_target() -> None:
    with _sleeper() as pid:
        start = _start_of(pid)
        by_pid = _run_signal_script("TERM", "", start, str(pid), start)
        by_group = _run_signal_script("TERM", str(pid), start)
        assert _alive(pid)
    assert by_pid.returncode == 1, by_pid
    assert f"pid {pid}: simulated-kill-failure" in by_pid.stderr
    assert by_group.returncode == 1, by_group
    assert f"group {pid}: simulated-kill-failure" in by_group.stderr


def test_signal_script_accepts_a_kill_whose_target_vanished() -> None:
    with _sleeper() as pid:
        start = _start_of(pid)
    # Reaped: the pid and the group are empty, so a failed kill is no failure.
    result = _run_signal_script("TERM", str(pid), start, str(pid), start)
    assert (result.returncode, result.stderr) == (0, "")


def test_signal_script_accepts_a_target_that_died_during_its_kill() -> None:
    # The sleeper's parent shell reaps it; the override kills only that pid.
    helper = subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE, start_new_session=True
    )
    try:
        assert helper.stdout is not None
        pid = int(helper.stdout.readline())
        assert pid > 1 and os.getpgid(pid) not in (os.getpgrp(), 0, 1)
        start = _start_of(pid)
        racing_kill = (
            'kill() { command kill -s KILL "$3"; '
            'while [ -e "/proc/$3" ]; do sleep 0.01; done; echo vanished >&2; return 1; }; '
        )
        result = subprocess.run(
            ["sh", "-c", racing_kill + docker_operations._SIGNAL, "aw-signal", "TERM", "", start]
            + [str(pid), start],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        helper.kill()
        helper.wait()
    assert (result.returncode, result.stderr) == (0, "")


async def test_cancel_escalates_when_the_real_signal_script_fails(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_operations, "_SIGNAL", _FAILING_KILL + docker_operations._SIGNAL)
    ops = _ops(docker_env)
    with _sleeper() as pid:
        operation = _held_operation((str(pid), _start_of(pid)))
        await ops._terminate_group(operation, grace_seconds=0)
        assert _alive(pid)
    metadata = ops._status(operation).metadata
    assert metadata["cancel_escalation"] == "container_stopped"
    assert metadata["cancel_signal_failure"] == (
        f"kill -TERM: exit 1: group {pid}: simulated-kill-failure\n"
        f"pid {pid}: simulated-kill-failure"
    )
    assert "stop -t 0 c1" in (docker_env / "docker.log").read_text()


async def test_cancel_kills_a_descendant_that_left_the_group(docker_env: Path) -> None:
    ops = _ops(docker_env)
    pid_file = docker_env / "escaped.pid"
    script = f"setsid sh -c 'echo $$ > {pid_file}; exec sleep 300' >/dev/null 2>&1 & exec sleep 300"
    ref = await ops.start(_workspace(), _spec(script))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    escaped = int(pid_file.read_text())
    leader = int(ops._operations[ref.operation_id].leader[0])  # type: ignore[index]
    assert os.getpgid(escaped) not in (leader, os.getpgrp())
    try:
        status = await ops.cancel(ref, grace_seconds=0.5)
        assert status.state == "cancelled"
        assert "cancel_escalation" not in status.metadata, status.metadata
        assert not _alive(escaped)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(escaped, signal.SIGKILL)


async def test_missing_client_is_typed_and_execute_keeps_its_error_result(
    docker_env: Path,
) -> None:
    ops = _ops(docker_env)
    ops._docker = str(docker_env / "missing-docker")
    with pytest.raises(OperationFailure) as caught:
        await ops.start(_workspace(), _spec("true"))
    assert caught.value.error.kind == "provider_unavailable"
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    provider = WorkspaceDockerProvider()
    provider._operations = ops
    result = await provider.execute(_workspace(), "true")
    assert result.exit_code == -1 and "missing-docker" in result.stderr


async def test_a_start_returns_promptly_while_running(docker_env: Path) -> None:
    ops = _ops(docker_env)
    began = time.monotonic()
    ref = await ops.start(_workspace(), _spec("sleep 30"))
    assert time.monotonic() - began < 2
    assert (await ops.status(ref)).state == "running"
    await ops.cancel(ref, grace_seconds=0.5)


async def test_b_fifty_concurrent_same_key_starts_launch_once(docker_env: Path) -> None:
    ops = _ops(docker_env)
    counter = docker_env / "launches"
    spec = _spec(f"echo x >> {counter}")
    refs = await asyncio.gather(*(ops.start(_workspace(), spec) for _ in range(50)))
    assert len(set(refs)) == 1
    await drain_operation(ops, refs[0])
    assert counter.read_text() == "x\n"
    assert Path(os.environ["FAKE_DOCKER_LOG"]).read_text().count("exec -i") == 1


async def test_c_same_key_different_argv_is_conflict(docker_env: Path) -> None:
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("true"))
    with pytest.raises(OperationFailure) as caught:
        await ops.start(_workspace(), _spec("false"))
    assert caught.value.error.kind == "conflict"
    await drain_operation(ops, ref)


async def test_d_binding_reaches_env_but_never_argv_or_pages(docker_env: Path) -> None:
    ops = _ops(docker_env)
    spec = _spec(
        'echo "overlay=$OVERLAY"; printf %s "$GH_TOKEN" | sha256sum | cut -c1-64',
        environment={"OVERLAY": "on"},
        credential_bindings=(
            CredentialBinding(
                ref=CredentialRef(name="github", scope="repo"), delivery="env", target="GH_TOKEN"
            ),
        ),
    )
    ref = await ops.start(_workspace(), spec)
    pages = await _pages(ops, ref)
    digest = hashlib.sha256(SECRET.encode()).hexdigest()
    assert _data(pages) == f"overlay=on\n{digest}\n".encode()
    client_argv = Path(os.environ["FAKE_DOCKER_LOG"]).read_text()
    assert "-e GH_TOKEN " in client_argv
    assert SECRET not in client_argv
    assert all(
        SECRET not in p and base64.b64encode(SECRET.encode()).decode() not in p for p in pages
    )


async def test_e_marker_line_is_first_and_a_forged_one_is_not(docker_env: Path) -> None:
    ops = _ops(docker_env)
    marker = LaunchMarker(argv0="aw-launch-7f3", line="AW_LAUNCH nonce-91c")
    ref = await ops.start(
        _workspace(), _spec('echo "AW_LAUNCH nonce-91c"; echo work', launch_marker=marker)
    )
    lines = _data(await _pages(ops, ref)).decode().splitlines()
    assert lines[0] == marker.line
    # The workload's forgery is just output after the genuine line; a
    # consumer that counts only the first line counts exactly one launch.
    assert lines == [marker.line, marker.line, "work"]
    assert [i for i, line in enumerate(lines) if line == marker.line][0] == 0


async def test_f_rewriting_in_workspace_output_cannot_change_observed_bytes(
    docker_env: Path,
) -> None:
    ops = _ops(docker_env)
    decoy = docker_env / "output.log"
    script = (
        f"echo one | tee {decoy}; echo two | tee -a {decoy}; "
        f": > {decoy}; echo forged > {decoy}; echo three"
    )
    ref = await ops.start(_workspace(), _spec(script))
    assert _data(await _pages(ops, ref)) == b"one\ntwo\nthree\n"
    assert decoy.read_text() == "forged\n"


async def test_g_cursor_older_than_retention_reports_exact_gap(docker_env: Path) -> None:
    ops = _ops(docker_env, retention=100)
    ref = await ops.start(_workspace(), _spec("head -c 250 /dev/zero | tr '\\0' a"))
    first = await ops.observe(ref, cursor=None, max_bytes=1 << 16, wait_seconds=0)
    await drain_operation(ops, ref)
    page = await ops.observe(ref, cursor=first.cursor, max_bytes=1 << 16)
    assert page.gap.lost_bytes == 150
    assert base64.b64decode(page.data_b64) == b"a" * 100


async def test_h_foreign_generation_cursor_is_unknown_operation(docker_env: Path) -> None:
    # Both drained, and the foreign offset (3) lies inside the recipient's 13
    # bytes, so only the generation check can reject it.
    ops = _ops(docker_env)
    one = await ops.start(_workspace(), _spec("printf one", key="one"))
    two = await ops.start(_workspace(), _spec("printf 'thirteen byte'", key="two"))
    _, first = await drain_operation(ops, one)
    _, second = await drain_operation(ops, two)
    assert (len(first), len(second)) == (3, 13)
    foreign = (await ops.observe(one, cursor=None, max_bytes=4096)).next_cursor
    with pytest.raises(OperationFailure) as caught:
        await ops.observe(two, cursor=foreign, max_bytes=4096)
    assert caught.value.error.kind == "unknown_operation"


async def test_i_every_serialised_page_fits_max_page_bytes(docker_env: Path) -> None:
    ops = _ops(docker_env, page=700)
    ref = await ops.start(_workspace(), _spec("head -c 20000 /dev/urandom"))
    pages = await _pages(ops, ref, max_bytes=100_000)
    assert len(pages) > 10
    assert all(len(p.encode()) <= 700 for p in pages)
    assert len(_data(pages)) == 20000


async def test_j_late_read_drains_the_rest_and_only_then_completes(docker_env: Path) -> None:
    ops = _ops(docker_env, page=600)
    ref = await ops.start(_workspace(), _spec("head -c 3000 /dev/zero | tr '\\0' b"))
    while (await ops.status(ref)).state == "running":
        await asyncio.sleep(0.05)
    pages = await _pages(ops, ref)
    from agentic_isolation.contract import ObservationPage

    parsed = [ObservationPage.model_validate_json(p) for p in pages]
    assert [p.complete for p in parsed] == [False] * (len(parsed) - 1) + [True]
    assert _data(pages) == b"b" * 3000


async def test_k_cancel_kills_leader_grandchild_and_term_trapper(docker_env: Path) -> None:
    ops = _ops(docker_env)
    pids = docker_env / "pids"
    script = (
        f"(sh -c 'echo $$ >> {pids}; exec sleep 300' &); "
        f"sh -c 'trap \"\" TERM; echo $$ >> {pids}; exec sleep 300' >/dev/null 2>&1 & "
        f"echo $$ >> {pids}; exec sleep 300"
    )
    ref = await ops.start(_workspace(), _spec(script))
    for _ in range(100):
        if pids.exists() and len(pids.read_text().split()) == 3:
            break
        await asyncio.sleep(0.05)
    recorded = [int(p) for p in pids.read_text().split()]
    assert all(_alive(p) for p in recorded)
    status = await ops.cancel(ref, grace_seconds=0.5)
    assert status.state == "cancelled"
    assert not [p for p in recorded if _alive(p)]
    assert (await ops.cancel(ref, grace_seconds=0.5)).state == "cancelled"


async def test_l_destroyed_container_is_lost(docker_env: Path) -> None:
    ops = _ops(docker_env)
    (docker_env / "state").write_text("gone")
    ref = await ops.start(_workspace(), _spec("exit 137"))
    await drain_operation(ops, ref)
    assert (await ops.status(ref)).state == "lost"


async def test_l_container_removed_under_a_running_operation_is_lost(
    docker_env: Path,
) -> None:
    """`docker rm -f`: the kill ends the exec client while the daemon still says `removing`."""
    ops = _ops(docker_env)
    pid_file = docker_env / "pid"
    ref = await ops.start(_workspace(), _spec(f"echo $$ > {pid_file}; exec sleep 60"))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    assert (await ops.status(ref)).state == "running"
    state = docker_env / "state"
    state.write_text("removing")
    os.kill(int(pid_file.read_text()), signal.SIGKILL)
    for _ in range(10):
        await asyncio.sleep(0.05)
        assert (await ops.status(ref)).state == "running"
    state.write_text("gone")
    await drain_operation(ops, ref)
    assert (await ops.status(ref)).state == "lost"


async def test_l_killed_workload_in_a_stopped_container_keeps_its_status(
    docker_env: Path,
) -> None:
    ops = _ops(docker_env)
    (docker_env / "state").write_text("exited")
    ref = await ops.start(_workspace(), _spec("kill -KILL $$"))
    await drain_operation(ops, ref)
    status = await ops.status(ref)
    assert (status.state, status.exit_code, status.signal) == ("failed", 137, "SIGKILL")
    (docker_env / "state").write_text("gone")
    assert (await ops.status(ref)).state == "failed"


async def test_l_unreachable_daemon_is_provider_unavailable(docker_env: Path) -> None:
    ops = _ops(docker_env)
    (docker_env / "state").write_text("down")
    ref = await ops.start(_workspace(), _spec("exit 1"))
    for _ in range(100):
        try:
            await ops.status(ref)
        except OperationFailure as failure:
            assert failure.error.kind == "provider_unavailable"
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("status never reported provider_unavailable")
    (docker_env / "state").unlink()
    assert (await ops.status(ref)).state == "failed"


async def test_m_fake_exit_file_does_not_change_true_status(docker_env: Path) -> None:
    ops = _ops(docker_env)
    script = "echo 0 > exit_code; echo succeeded > status; printf 'exit 0\\n'; exit 7"
    ref = await ops.start(_workspace(), _spec(script))
    await drain_operation(ops, ref)
    status = await ops.status(ref)
    assert (status.state, status.exit_code) == ("failed", 7)


async def test_unknown_ref_is_unknown_operation(docker_env: Path) -> None:
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("true"))
    stranger = ref.model_copy(update={"operation_id": "nope"})
    with pytest.raises(OperationFailure) as caught:
        await ops.status(stranger)
    assert caught.value.error.kind == "unknown_operation"


async def test_regression_execute_runs_through_the_operation_contract(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = WorkspaceDockerProvider()
    provider._operations = _ops(docker_env)
    result = await provider.execute(
        _workspace(), "echo hi; echo err >&2; exit 3", env={"TOKEN_X": "v"}
    )
    assert (result.exit_code, result.stdout, result.stderr) == (3, "hi\n", "err\n")
    log = Path(os.environ["FAKE_DOCKER_LOG"]).read_text()
    assert "-e TOKEN_X " in log and "TOKEN_X=v" not in log


async def test_run_to_completion_returns_terminal_status_and_bytes(docker_env: Path) -> None:
    provider = WorkspaceDockerProvider()
    provider._operations = _ops(docker_env)
    status, output = await run_to_completion(provider, _workspace(), _spec("printf done"))
    assert (status.state, output) == ("succeeded", b"done")


async def test_run_to_completion_keeps_both_channels_in_the_stream(docker_env: Path) -> None:
    provider = WorkspaceDockerProvider()
    provider._operations = _ops(docker_env)
    spec = _spec("printf out; printf err >&2; exit 4")
    status, output = await run_to_completion(provider, _workspace(), spec)
    assert (status.state, status.exit_code) == ("failed", 4)
    assert sorted((output[:3], output[3:])) == [b"err", b"out"]


async def test_timeout_keeps_the_workloads_final_output(docker_env: Path) -> None:
    ops = _ops(docker_env)
    script = "trap 'echo final; exit 0' TERM; echo initial; while :; do sleep 0.1; done"
    ref = await ops.start(_workspace(), _spec(script, timeout_seconds=1))
    status, output = await drain_operation(ops, ref)
    # The stand-in's sh may also report the killed sleep on stderr.
    assert status.state == "timed_out"
    assert output.startswith(b"initial\n") and output.endswith(b"final\n")


class _ParkedResolver:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.failures = 0

    async def resolve(self, ref: CredentialRef) -> str:
        await self.release.wait()
        if self.failures:
            self.failures -= 1
            raise RuntimeError("resolver refused")
        return SECRET


def _bound_spec(key: str) -> OperationSpec:
    binding = CredentialBinding(
        ref=CredentialRef(name="token", scope="repo"), target="TOKEN", delivery="env"
    )
    return _spec("echo bound", key=key, credential_bindings=(binding,))


async def test_slow_resolver_does_not_block_another_workspace(docker_env: Path) -> None:
    resolver = _ParkedResolver()
    ops = _ops(docker_env)
    ops._credentials = resolver
    parked = asyncio.create_task(ops.start(_workspace(), _bound_spec("slow")))
    await asyncio.sleep(0.1)
    other = Workspace(
        id="ws2", provider="docker", path=Path("."), config=WorkspaceConfig(), _handle="c2"
    )
    try:
        ref = await asyncio.wait_for(ops.start(other, _spec("echo free")), timeout=5)
        assert not parked.done()
        assert (await drain_operation(ops, ref))[1] == b"free\n"
    finally:
        resolver.release.set()
        await drain_operation(ops, await parked)


async def test_resolver_failure_releases_the_key_for_a_retry(docker_env: Path) -> None:
    resolver = _ParkedResolver()
    resolver.failures = 1
    resolver.release.set()
    ops = _ops(docker_env)
    ops._credentials = resolver
    with pytest.raises(RuntimeError):
        await ops.start(_workspace(), _bound_spec("retry"))
    ref = await ops.start(_workspace(), _bound_spec("retry"))
    assert (await drain_operation(ops, ref))[1] == b"bound\n"


async def test_same_key_replays_for_the_workspace_lifetime_after_output_is_reclaimed(
    docker_env: Path,
) -> None:
    """EXP-V1-0006 5.1.3: the key's record lives as long as the workspace."""
    ops = DockerOperations(
        credentials=_Resolver(),
        retention_bytes=1 << 20,
        max_page_bytes=1 << 16,
        docker=str(docker_env / "docker"),
        record_retention_seconds=1.0,
    )
    first = await ops.start(_workspace(), _spec("echo once"))
    await drain_operation(ops, first)
    assert await ops.start(_workspace(), _spec("echo once")) == first
    assert (await ops.status(first)).state == "succeeded"
    await asyncio.sleep(1.1)
    # Past the window: the output is reclaimed but the key still replays.
    assert await ops.start(_workspace(), _spec("echo once")) == first
    assert ops._operations == {}
    with pytest.raises(OperationFailure) as caught:
        await ops.status(first)
    assert caught.value.error.kind == "unknown_operation"
    with pytest.raises(OperationFailure) as caught:
        await ops.start(_workspace(), _spec("echo twice"))
    assert caught.value.error.kind == "conflict"
    launches = (docker_env / "docker.log").read_text().count("aw-operation")
    assert launches == 1
    # Once the workspace is destroyed nothing of it is kept.
    await ops.forget_workspace("ws1")
    assert (ops._operations, ops._by_key, ops._launching) == ({}, {}, {})


async def test_a_running_operation_outlives_the_retention_window(docker_env: Path) -> None:
    ops = DockerOperations(
        credentials=_Resolver(),
        retention_bytes=1 << 20,
        max_page_bytes=1 << 16,
        docker=str(docker_env / "docker"),
        record_retention_seconds=0.0,
    )
    ref = await ops.start(_workspace(), _spec("sleep 1; echo late"))
    other = await ops.start(_workspace(), _spec("true", key="other"))
    assert await ops.start(_workspace(), _spec("sleep 1; echo late")) == ref
    assert (await drain_operation(ops, ref))[1] == b"late\n"
    await drain_operation(ops, other)


async def test_release_keeps_a_running_record(docker_env: Path) -> None:
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("sleep 0.5"))
    ops.release(ref)
    assert (await drain_operation(ops, ref))[0].state == "succeeded"
    ops.release(ref)
    assert ops._operations == {} and ops._by_key == {}


async def test_a_thousand_sequential_executes_leave_the_maps_empty(docker_env: Path) -> None:
    provider = WorkspaceDockerProvider()
    provider._operations = ops = _ops(docker_env)
    for i in range(1000):
        result = await provider.execute(_workspace(), f"echo {i}")
        assert result.stdout == f"{i}\n"
    assert (ops._operations, ops._by_key, ops._launching) == ({}, {}, {})


async def _kill_leader(operation: _Operation) -> None:
    """Cleanup only, after the assertions: end a workload the test left running."""
    if operation.leader is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(operation.leader[0]), signal.SIGKILL)
    if operation.collector is not None:
        await asyncio.wait_for(operation.collector, timeout=10)


@pytest.mark.parametrize(
    "stop",
    [
        docker_operations._error("provider_unavailable", "stop client cannot spawn"),
        (1, "Cannot connect to the Docker daemon"),
    ],
    ids=["unspawnable", "failed-exit"],
)
async def test_timeout_settles_when_a_persistent_workload_survives_a_failed_stop(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch, stop: object
) -> None:
    """The workload never exits; the timeout still settles within the documented bound."""
    monkeypatch.setattr(docker_operations, "_FINAL_OUTPUT_SECONDS", 0.1)
    monkeypatch.setattr(docker_operations, "_CLIENT_EXIT_SECONDS", 0.5)
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("exec sleep 60", timeout_seconds=1))
    operation = ops._operations[ref.operation_id]
    await operation.leader_known.wait()
    assert operation.leader is not None
    leader = int(operation.leader[0])
    real = ops._docker_cmd

    async def command(*argv: str) -> tuple[int, str]:
        if argv[0] == "stop":
            if isinstance(stop, OperationFailure):
                raise stop
            return stop  # type: ignore[return-value]
        return await real(*argv)

    monkeypatch.setattr(ops, "_docker_cmd", command)
    monkeypatch.setattr(docker_operations.os, "getpgrp", lambda: leader)
    try:
        status, _ = await asyncio.wait_for(drain_operation(ops, ref), timeout=10)
        assert status.state == "timed_out"
        assert status.metadata["cancel_escalation"] == "container_stop_failed"
        failure = status.metadata["cancel_signal_failure"]
        assert "caller's own group" in failure
        assert ("cannot spawn" in failure) or ("Cannot connect" in failure)
        assert "detached, the workload may still be running" in failure
        assert _alive(leader)  # truthfully not claimed stopped
    finally:
        await _kill_leader(operation)


async def test_execute_returns_when_a_persistent_workload_survives_a_failed_stop(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_operations, "_FINAL_OUTPUT_SECONDS", 0.1)
    monkeypatch.setattr(docker_operations, "_CLIENT_EXIT_SECONDS", 0.5)
    provider = WorkspaceDockerProvider()
    provider._operations = ops = _ops(docker_env)
    real = ops._docker_cmd
    seen: list[_Operation] = []

    async def command(*argv: str) -> tuple[int, str]:
        if argv[0] == "stop":
            return 1, "Cannot connect to the Docker daemon"
        return await real(*argv)

    async def refused(operation: _Operation) -> str:
        seen.append(operation)
        return "refused for the test"

    monkeypatch.setattr(ops, "_docker_cmd", command)
    monkeypatch.setattr(ops, "_unsafe_leader", refused)
    try:
        result = await asyncio.wait_for(
            provider.execute(_workspace(), "exec sleep 60", timeout=1), timeout=10
        )
        assert result.exit_code != 0
        assert seen and seen[0].state == "timed_out"
        assert _alive(int(seen[0].leader[0]))  # type: ignore[index]
    finally:
        for operation in seen:
            await _kill_leader(operation)


async def test_namespace_probe_that_cannot_run_escalates_to_stop(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("exec sleep 60"))
    operation = ops._operations[ref.operation_id]
    await operation.leader_known.wait()
    assert operation.leader is not None
    leader = int(operation.leader[0])
    real = ops._docker_cmd
    calls: list[tuple[str, ...]] = []

    async def command(*argv: str) -> tuple[int, str]:
        calls.append(argv)
        if "aw-pidns" in argv:
            raise docker_operations._error("provider_unavailable", "probe cannot spawn")
        if argv[0] == "stop":
            os.kill(leader, signal.SIGKILL)  # what a real stop does to the workload
            return 0, "c1"
        return await real(*argv)

    monkeypatch.setattr(ops, "_docker_cmd", command)
    monkeypatch.setattr(docker_operations.os, "getpgrp", lambda: leader)
    try:
        status = await asyncio.wait_for(ops.cancel(ref, grace_seconds=0), timeout=10)
        assert status.state == "cancelled"
        assert status.metadata["cancel_escalation"] == "container_stopped"
        assert "probe cannot spawn" in status.metadata["cancel_signal_failure"]
        assert not any("aw-signal" in c for c in calls)
        assert ("stop", "-t", "0", "c1") in calls
    finally:
        await _kill_leader(operation)


async def test_signalling_client_that_cannot_run_escalates_to_stop(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("exec sleep 60"))
    operation = ops._operations[ref.operation_id]
    await operation.leader_known.wait()
    assert operation.leader is not None
    leader = int(operation.leader[0])
    real = ops._docker_cmd

    async def command(*argv: str) -> tuple[int, str]:
        if "aw-processes" in argv:
            raise docker_operations._error("provider_unavailable", "exec cannot spawn")
        if argv[0] == "stop":
            os.kill(leader, signal.SIGKILL)
            return 0, "c1"
        return await real(*argv)

    monkeypatch.setattr(ops, "_docker_cmd", command)
    try:
        status = await asyncio.wait_for(ops.cancel(ref, grace_seconds=0), timeout=10)
        assert status.state == "cancelled"
        assert status.metadata["cancel_escalation"] == "container_stopped"
        assert "exec cannot spawn" in status.metadata["cancel_signal_failure"]
    finally:
        await _kill_leader(operation)


async def test_timeout_survives_an_unreachable_daemon_and_its_recovery(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_operations, "_FINAL_OUTPUT_SECONDS", 0.1)
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("exec sleep 2", timeout_seconds=1))
    operation = ops._operations[ref.operation_id]
    await operation.leader_known.wait()
    (docker_env / "state").write_text("down")

    async def unavailable(operation: _Operation, *, grace_seconds: float) -> None:
        raise docker_operations._error("provider_unavailable", "docker is gone")

    monkeypatch.setattr(ops, "_terminate_group", unavailable)
    try:
        await asyncio.wait_for(operation.collector, timeout=10)  # type: ignore[arg-type]
        with pytest.raises(OperationFailure) as caught:
            await ops.status(ref)
        assert caught.value.error.kind == "provider_unavailable"
        (docker_env / "state").write_text("running")
        assert (await ops.status(ref)).state == "timed_out"
        assert (await ops.status(ref)).state == "timed_out"
    finally:
        await _kill_leader(operation)


# Release review of #57, finding 1: values delivered through the docker
# client's environment must never steer that client.
@pytest.mark.parametrize("target", ["DOCKER_HOST", "LD_PRELOAD", "PATH"])
async def test_a_binding_that_would_steer_the_host_client_is_refused(
    docker_env: Path, target: str
) -> None:
    ops = _ops(docker_env)
    binding = CredentialBinding(
        ref=CredentialRef(name="token", scope="s"), delivery="env", target=target
    )
    with pytest.raises(OperationFailure) as caught:
        await ops.start(_workspace(), _spec("true", credential_bindings=(binding,)))
    assert caught.value.error.kind == "rejected_policy"
    assert target in caught.value.error.message and SECRET not in caught.value.error.message
    assert not Path(os.environ["FAKE_DOCKER_LOG"]).exists()  # no client launched
    assert (ops._operations, ops._by_key) == ({}, {})


@pytest.mark.parametrize("name", ["DOCKER_HOST", "LD_PRELOAD", "PATH"])
async def test_execute_env_that_would_steer_the_host_client_is_refused(
    docker_env: Path, name: str
) -> None:
    host_env = dict(os.environ)
    provider = WorkspaceDockerProvider()
    provider._operations = _ops(docker_env)
    result = await provider.execute(_workspace(), "true", env={name: "/evil"})
    assert result.exit_code == -1 and name in result.stderr
    assert not Path(os.environ["FAKE_DOCKER_LOG"]).exists()
    assert dict(os.environ) == host_env


# Finding 2: a settlement that cannot reach the daemon must not hold a drain.
async def test_timeout_drain_ends_when_the_daemon_stays_down_at_settlement(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_operations, "_FINAL_OUTPUT_SECONDS", 0.1)
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("exec sleep 30", timeout_seconds=1))
    operation = ops._operations[ref.operation_id]
    await operation.leader_known.wait()
    (docker_env / "state").write_text("down")
    try:
        with pytest.raises(OperationFailure) as caught:
            await asyncio.wait_for(drain_operation(ops, ref), timeout=15)
        assert caught.value.error.kind == "provider_unavailable"
        assert operation.client_finished and operation.state == "running"
    finally:
        await _kill_leader(operation)


async def test_timeout_drain_settles_once_the_daemon_recovers(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The premise reproduction: the drain blocked long after the daemon came back."""
    monkeypatch.setattr(docker_operations, "_FINAL_OUTPUT_SECONDS", 0.1)
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec("exec sleep 30", timeout_seconds=1))
    operation = ops._operations[ref.operation_id]
    await operation.leader_known.wait()
    state = docker_env / "state"
    state.write_text("down")
    try:
        await asyncio.wait_for(operation.collector, timeout=15)  # type: ignore[arg-type]
        assert operation.state == "running"
        state.write_text("")
        status, _ = await asyncio.wait_for(drain_operation(ops, ref), timeout=5)
        assert status.state == "timed_out"
    finally:
        await _kill_leader(operation)


async def test_a_hung_docker_control_command_is_killed_at_its_deadline(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_operations, "_CONTROL_SECONDS", 0.5)
    pid_file = docker_env / "hung.pid"
    hung = docker_env / "hung-docker"
    hung.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    hung.chmod(hung.stat().st_mode | stat.S_IEXEC)
    ops = _ops(docker_env)
    ops._docker = str(hung)
    began = time.monotonic()
    with pytest.raises(OperationFailure) as caught:
        await asyncio.wait_for(ops._presence("c1"), timeout=10)
    assert caught.value.error.kind == "provider_unavailable"
    assert time.monotonic() - began < 5
    with pytest.raises(ProcessLookupError):  # killed and reaped, not left behind
        os.kill(int(pid_file.read_text()), 0)


# Finding 3: `cancelled` is published only once the cancel has confirmed.
async def _cancel_with_held_confirmation(
    ops: DockerOperations, monkeypatch: pytest.MonkeyPatch, *, confirmed: bool
) -> tuple[OperationRef, _Operation, asyncio.Task[object]]:
    ref = await ops.start(_workspace(), _spec("exec sleep 30"))
    operation = ops._operations[ref.operation_id]
    await operation.leader_known.wait()
    release = asyncio.Event()
    asked = asyncio.Event()

    async def held(operation: _Operation, *, within: float) -> bool:
        asked.set()
        await release.wait()
        return confirmed

    monkeypatch.setattr(ops, "_group_gone", held)
    cancel: asyncio.Task[object] = asyncio.create_task(ops.cancel(ref, grace_seconds=1.0))
    await asyncio.wait_for(asked.wait(), timeout=10)
    # TERM has ended the workload, so the client exits while the cancel is
    # still confirming: the race the collector used to win with `cancelled`.
    for _ in range(200):
        if operation.client_finished:
            break
        await asyncio.sleep(0.05)
    assert operation.client_finished
    await asyncio.sleep(0.2)
    assert (await ops.status(ref)).state == "running"
    page = await ops.observe(ref, cursor=None, max_bytes=1024, wait_seconds=0.1)
    assert not page.complete
    release.set()
    return ref, operation, cancel


async def test_cancel_is_published_only_after_confirmation(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _ops(docker_env)
    ref, _, cancel = await _cancel_with_held_confirmation(ops, monkeypatch, confirmed=True)
    status = await asyncio.wait_for(cancel, timeout=10)
    assert status.state == "cancelled"  # type: ignore[attr-defined]
    assert (await ops.status(ref)).state == "cancelled"


async def test_unconfirmed_cancel_settles_failed_not_cancelled(
    docker_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _ops(docker_env)
    real = ops._docker_cmd

    async def stop_fails(*argv: str) -> tuple[int, str]:
        if argv[0] == "stop":
            return 1, "Cannot connect to the Docker daemon"
        return await real(*argv)

    monkeypatch.setattr(ops, "_docker_cmd", stop_fails)
    ref, operation, cancel = await _cancel_with_held_confirmation(
        ops, monkeypatch, confirmed=False
    )
    with pytest.raises(OperationFailure) as caught:
        await asyncio.wait_for(cancel, timeout=20)
    assert caught.value.error.kind == "provider_unavailable"
    await asyncio.wait_for(operation.collector, timeout=10)  # type: ignore[arg-type]
    status = await ops.status(ref)
    assert status.state == "failed"
    assert status.metadata["cancel_escalation"] == "container_stop_failed"


async def test_forget_workspace_waits_out_an_in_flight_start(docker_env: Path) -> None:
    """Teardown used to drop the records while a slow start was still launching."""
    resolving = asyncio.Event()
    release = asyncio.Event()

    class _Slow:
        async def resolve(self, ref: CredentialRef) -> str:
            resolving.set()
            await release.wait()
            return SECRET

    ops = _ops(docker_env)
    ops._credentials = _Slow()
    binding = CredentialBinding(
        ref=CredentialRef(name="token", scope="s"), delivery="env", target="API_TOKEN"
    )
    start = asyncio.create_task(
        ops.start(_workspace(), _spec("true", credential_bindings=(binding,)))
    )
    await asyncio.wait_for(resolving.wait(), timeout=10)
    forget = asyncio.create_task(ops.forget_workspace("ws1"))
    await asyncio.sleep(0.1)
    assert not forget.done()
    with pytest.raises(OperationFailure) as caught:
        await ops.start(_workspace(), _spec("true", key="k2"))
    assert caught.value.error.kind == "unknown_operation"
    release.set()
    ref = await asyncio.wait_for(start, timeout=10)
    await asyncio.wait_for(forget, timeout=10)
    assert (ops._operations, ops._by_key, ops._launching) == ({}, {}, {})
    with pytest.raises(OperationFailure):
        await ops.status(ref)
