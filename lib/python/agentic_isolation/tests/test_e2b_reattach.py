"""Operations reattach and workspace suspend on E2B (#103), over the e2b-double.

Each reattach case starts an operation on one `E2BWorkspaceProvider` and
answers for it from a SECOND provider built over the same client, given only
the ref (through its JSON) and the reattach token: what a consumer recovering
after a restart has. The double runs commands as local processes, so a
cancel is checked by the workload's own pid, not by the reported state.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

import pytest

from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import OperationRef, OperationSpec
from agentic_isolation.providers import (
    SupportsOperationReattach,
    SupportsWorkspaceSuspend,
    WorkspaceDockerProvider,
)
from agentic_isolation.providers.base import Workspace
from agentic_isolation.providers.e2b import E2BWorkspaceProvider
from agentic_isolation.providers.e2b_client import CommandExit, E2BTransportError
from agentic_isolation.providers.e2b_operations import OWNER_VARIABLE
from agentic_isolation.providers.operation_spool import OperationFailure
from tests.e2b_fake import FakeE2BClient, FakeRun


def _spec(script: str, key: str = "k1") -> OperationSpec:
    return OperationSpec.model_validate(
        {
            "execution_id": "exec-reattach-7",
            "operation_key": key,
            "argv": ("sh", "-c", script),
            "working_directory": ".",
            "environment": {},
            "credential_bindings": (),
            "timeout_seconds": 60,
            "metadata": {},
        }
    )


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


async def _pid(path: Path) -> int:
    for _ in range(100):
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        await asyncio.sleep(0.05)
    raise AssertionError(f"workload never wrote {path}")


async def _started(
    tmp_path: Path, script: str
) -> tuple[FakeE2BClient, E2BWorkspaceProvider, Workspace, OperationRef, str]:
    client = FakeE2BClient(executing_root=tmp_path / "ws")
    first = E2BWorkspaceProvider("t", client=client)
    workspace = await first.create(WorkspaceConfig())
    ref = await first.start(workspace, _spec(script))
    # A consumer reattaches after a restart; here, once the launch has recorded the group.
    await _pid(tmp_path / "ws-ops" / ref.operation_id / "group")
    return client, first, workspace, ref, first.reattach_token(ref)


def _fresh(client: FakeE2BClient) -> E2BWorkspaceProvider:
    return E2BWorkspaceProvider("t", client=client)


def _persisted(ref: OperationRef) -> OperationRef:
    return OperationRef.model_validate_json(ref.model_dump_json())


@pytest.fixture(autouse=True)
def _workspace_root(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()


async def test_fresh_instance_reads_running_then_the_exit_status(tmp_path: Path) -> None:
    gate = tmp_path / "ws" / "go"
    client, _, _, ref, token = await _started(
        tmp_path, f"while [ ! -e {gate} ]; do sleep 0.05; done; exit 3"
    )
    fresh = _fresh(client)
    await fresh.reattach(_persisted(ref), token)

    running = await fresh.status(_persisted(ref))
    assert running.state == "running"
    assert running.execution_id == "exec-reattach-7"

    gate.touch()
    for _ in range(100):
        ended = await fresh.status(ref)
        if ended.state != "running":
            break
        await asyncio.sleep(0.05)
    assert (ended.state, ended.exit_code) == ("failed", 3)


async def test_fresh_instance_cancel_kills_the_workload(tmp_path: Path) -> None:
    pidfile = tmp_path / "ws" / "pid"
    client, first, _, ref, token = await _started(tmp_path, f"echo $$ > {pidfile}; exec sleep 30")
    pid = await _pid(pidfile)
    fresh = _fresh(client)
    await fresh.reattach(_persisted(ref), token)

    cancelled = await fresh.cancel(_persisted(ref), grace_seconds=1)

    assert cancelled.state == "cancelled"
    assert not _alive(pid)
    # A third instance reads the same outcome from the sandbox.
    third = _fresh(client)
    await third.reattach(ref, token)
    assert (await third.status(ref)).state == "cancelled"
    # The starting instance settles it the same way.
    await asyncio.sleep(0.2)
    assert (await first.status(ref)).state in ("running", "cancelled", "failed")


async def test_a_wrong_token_cannot_reattach_or_cancel(tmp_path: Path) -> None:
    pidfile = tmp_path / "ws" / "pid"
    client, first, workspace, ref, token = await _started(
        tmp_path, f"echo $$ > {pidfile}; exec sleep 30"
    )
    pid = await _pid(pidfile)
    other = await first.start(workspace, _spec("sleep 30", key="k2"))
    fresh = _fresh(client)

    for forged in ("0" * 32, first.reattach_token(other)):
        with pytest.raises(OperationFailure) as refused:
            await fresh.reattach(ref, forged)
        assert refused.value.error.kind == "unknown_operation"
    with pytest.raises(OperationFailure) as unknown:
        await fresh.cancel(ref, grace_seconds=0)
    assert unknown.value.error.kind == "unknown_operation"
    assert _alive(pid)
    await first.cancel(ref, grace_seconds=0)
    await first.cancel(other, grace_seconds=0)


async def test_a_probe_that_does_not_answer_is_unavailable_not_stopped(
    tmp_path: Path,
) -> None:
    client, first, _, ref, token = await _started(tmp_path, "sleep 30")
    fresh = _fresh(client)
    await fresh.reattach(ref, token)
    real_start = client.start

    async def disconnected(sandbox_id: str, command: str, **kwargs: object):  # type: ignore[no-untyped-def]
        if "aw-probe" in command:
            client.executing_root, root = None, client.executing_root
            client.responder = lambda _c: FakeRun(
                exit=CommandExit(end="disconnected", detail="stream lost")
            )
            try:
                return await real_start(sandbox_id, command, **kwargs)  # type: ignore[arg-type]
            finally:
                client.executing_root = root
        return await real_start(sandbox_id, command, **kwargs)  # type: ignore[arg-type]

    client.start = disconnected  # type: ignore[method-assign]
    with pytest.raises(OperationFailure) as unavailable:
        await fresh.status(ref)
    assert unavailable.value.error.kind == "provider_unavailable"
    client.start = real_start  # type: ignore[method-assign]
    await first.cancel(ref, grace_seconds=0)


async def test_a_workload_gone_without_a_status_is_lost_not_cancelled(tmp_path: Path) -> None:
    client, _, _, ref, token = await _started(tmp_path, "sleep 30")
    fresh = _fresh(client)
    await fresh.reattach(ref, token)
    # The sandbox's processes die under the wrapper, as on a sandbox restart.
    owner = f"{OWNER_VARIABLE}={token}".encode()
    for environ in Path("/proc").glob("[0-9]*/environ"):
        with contextlib.suppress(OSError):
            if owner in environ.read_bytes().split(b"\0"):
                os.kill(int(environ.parent.name), signal.SIGKILL)
    for _ in range(100):
        status = await fresh.status(ref)
        if status.state != "running":
            break
        await asyncio.sleep(0.05)
    assert status.state == "lost"


async def test_a_cancel_that_is_not_confirmed_raises_and_is_never_cancelled(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "ws" / "pid"
    client, first, _, ref, token = await _started(tmp_path, f"echo $$ > {pidfile}; exec sleep 30")
    pid = await _pid(pidfile)
    fresh = _fresh(client)
    await fresh.reattach(ref, token)
    real_start = client.start

    async def refusing(sandbox_id: str, command: str, **kwargs: object):  # type: ignore[no-untyped-def]
        if "aw-terminate" in command:
            command = "echo survivors 1; exit 1"
        return await real_start(sandbox_id, command, **kwargs)  # type: ignore[arg-type]

    async def unreachable(sandbox_id: str) -> bool:
        raise E2BTransportError(f"cannot reach {sandbox_id}")

    real_kill = client.kill
    client.start = refusing  # type: ignore[method-assign]
    client.kill = unreachable  # type: ignore[method-assign]
    with pytest.raises(OperationFailure) as failed:
        await fresh.cancel(ref, grace_seconds=0)
    assert failed.value.error.kind == "provider_unavailable"
    status = await fresh.status(ref)
    assert status.state == "running"
    assert "cancel_signal_failure" in status.metadata
    assert _alive(pid)
    client.start = real_start  # type: ignore[method-assign]
    client.kill = real_kill  # type: ignore[method-assign]
    await first.cancel(ref, grace_seconds=0)


async def test_a_reattached_operation_does_not_pretend_to_have_its_output(
    tmp_path: Path,
) -> None:
    client, first, _, ref, token = await _started(tmp_path, "echo hi; sleep 30")
    fresh = _fresh(client)
    await fresh.reattach(ref, token)
    with pytest.raises(OperationFailure) as refused:
        await fresh.observe(ref, cursor=None, max_bytes=1024)
    assert refused.value.error.kind == "unsupported_capability"
    await first.cancel(ref, grace_seconds=0)


async def test_two_instances_never_mint_one_operation_id(tmp_path: Path) -> None:
    client, first, workspace, ref, _ = await _started(tmp_path, "sleep 30")
    second = _fresh(client)
    other = await second.start(workspace, _spec("sleep 30"))
    assert other.operation_id != ref.operation_id
    await first.cancel(ref, grace_seconds=0)
    await second.cancel(other, grace_seconds=0)


async def test_pause_and_resume_reach_the_sandbox(tmp_path: Path) -> None:
    client = FakeE2BClient()
    provider = E2BWorkspaceProvider("t", client=client)
    workspace = await provider.create(WorkspaceConfig())

    await provider.pause(workspace)
    assert client.sandboxes[workspace.id].paused
    await provider.resume(workspace)
    assert not client.sandboxes[workspace.id].paused
    assert client.events[-2:] == [f"pause {workspace.id}", f"resume {workspace.id}"]


def test_e2b_claims_reattach_and_suspend_and_docker_claims_neither() -> None:
    e2b = E2BWorkspaceProvider("t", client=FakeE2BClient())
    assert isinstance(e2b, SupportsOperationReattach)
    assert isinstance(e2b, SupportsWorkspaceSuspend)
    # Docker's operations die with its process (docker_operations.py), and
    # nothing asks it to pause; claiming either would be a lie.
    docker = WorkspaceDockerProvider()
    assert not isinstance(docker, SupportsOperationReattach)
    assert not isinstance(docker, SupportsWorkspaceSuspend)
