"""Local cancel confirmation and destroy ordering under interruption and held starts.

Collected by CI's python-compatibility job (`uv run pytest -q`). Every await is
bounded so a regression fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import CredentialBinding, CredentialRef
from agentic_isolation.providers.local import WorkspaceLocalProvider
from agentic_isolation.providers.operation_spool import OperationFailure
from tests.test_docker_operations import _spec


async def test_interrupted_cancel_confirmation_never_publishes_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    ref = await provider.start(workspace, _spec("exec sleep 30"))
    operation = provider._operations._operations[ref.operation_id]
    entered = asyncio.Event()

    async def held(op: object, *, grace_seconds: float) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider._operations, "_signal_and_confirm", held)
    cancel = asyncio.create_task(provider.cancel(ref, grace_seconds=0))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cancel.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancel
        # The real collector settles once the workload ends after the lock is released.
        operation.process.kill()
        await asyncio.wait_for(operation.collector, 5)
        status = await provider.status(ref)
        assert status.state == "failed", status.state
        assert "interrupted" in status.metadata["cancel_signal_failure"]
    finally:
        if operation.process.returncode is None:
            operation.process.kill()
        await asyncio.wait_for(operation.collector, 5)
        await provider.destroy(workspace)


class _HeldResolver:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve(self, ref: CredentialRef) -> str:
        self.entered.set()
        await self.release.wait()
        return "test-token"


async def test_destroy_drains_a_start_held_in_credential_resolution(tmp_path: Path) -> None:
    resolver = _HeldResolver()
    provider = WorkspaceLocalProvider(base_dir=tmp_path, credentials=resolver, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    binding = CredentialBinding(
        ref=CredentialRef(name="token", scope="test"), delivery="env", target="TOKEN"
    )
    held = asyncio.create_task(
        provider.start(workspace, _spec("exec sleep 30", credential_bindings=(binding,)))
    )
    destroy: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(resolver.entered.wait(), 5)
        destroy = asyncio.create_task(provider.destroy(workspace))
        await asyncio.sleep(0.2)
        assert not destroy.done(), "destroy completed with an admitted start still resolving"
        # Teardown has begun: a start of a new key is refused, not admitted.
        with pytest.raises(OperationFailure) as refused:
            await asyncio.wait_for(provider.start(workspace, _spec("true", key="k2")), 5)
        assert refused.value.error.kind == "unknown_operation"
        resolver.release.set()
        ref = await asyncio.wait_for(held, 5)
        await asyncio.wait_for(destroy, 10)
        assert not workspace.path.exists()
        assert not provider._operations._operations
        assert not provider._operations._by_key
        # The launched workload was cancelled before the directory went.
        with pytest.raises(OperationFailure):
            await provider.status(ref)
        # Closure survives destruction.
        with pytest.raises(OperationFailure):
            await asyncio.wait_for(provider.start(workspace, _spec("true", key="k3")), 5)
        assert not workspace.path.exists()
    finally:
        resolver.release.set()
        for task in (held, destroy):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task


async def test_destroy_cancels_the_drained_start_workload(tmp_path: Path) -> None:
    resolver = _HeldResolver()
    provider = WorkspaceLocalProvider(base_dir=tmp_path, credentials=resolver, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    binding = CredentialBinding(
        ref=CredentialRef(name="token", scope="test"), delivery="env", target="TOKEN"
    )
    held = asyncio.create_task(
        provider.start(workspace, _spec("exec sleep 30", credential_bindings=(binding,)))
    )
    await asyncio.wait_for(resolver.entered.wait(), 5)
    destroy = asyncio.create_task(provider.destroy(workspace))
    await asyncio.sleep(0.1)
    resolver.release.set()
    ref = await asyncio.wait_for(held, 5)
    operation = provider._operations._operations.get(ref.operation_id)
    process = operation.process if operation is not None else None
    try:
        await asyncio.wait_for(destroy, 10)
        assert process is not None
        assert process.returncode is not None, "the drained start's workload survived destroy"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 5)
