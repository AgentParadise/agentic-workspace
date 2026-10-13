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
import hashlib
import os
import signal
import stat
import time
from pathlib import Path

import pytest

from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import (
    CredentialBinding,
    CredentialRef,
    LaunchMarker,
    OperationRef,
    OperationSpec,
)
from agentic_isolation.providers.base import (
    SupportsOperations,
    Workspace,
    WorkspaceProvider,
    drain_operation,
    run_to_completion,
)
from agentic_isolation.providers.docker import WorkspaceDockerProvider
from agentic_isolation.providers.docker_operations import DockerOperations
from agentic_isolation.providers.operation_spool import OperationFailure

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
    ops = _ops(docker_env)
    one = await ops.start(_workspace(), _spec("echo one", key="one"))
    two = await ops.start(_workspace(), _spec("echo two", key="two"))
    foreign = (await ops.observe(one, cursor=None, max_bytes=4096, wait_seconds=1)).next_cursor
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
    assert (result.exit_code, result.stdout) == (3, "hi\nerr\n")
    log = Path(os.environ["FAKE_DOCKER_LOG"]).read_text()
    assert "-e TOKEN_X " in log and "TOKEN_X=v" not in log


async def test_run_to_completion_returns_terminal_status_and_bytes(docker_env: Path) -> None:
    provider = WorkspaceDockerProvider()
    provider._operations = _ops(docker_env)
    status, output = await run_to_completion(provider, _workspace(), _spec("printf done"))
    assert (status.state, output) == ("succeeded", b"done")
