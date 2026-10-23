"""Termination through the real SDK adapter (#95): `_SdkCommand` over e2b's `AsyncCommandHandle`.

Only the envd event stream is generated; the handle, `_ChunkQueue`,
`_SdkCommand` and `E2BOperations._terminate` are the production ones, so the
queue closes exactly when the pinned SDK (e2b==2.52.0) lets it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

e2b_handle = pytest.importorskip("e2b.sandbox_async.commands.command_handle")
from e2b.envd.process import process_pb  # noqa: E402
from protobuf import Oneof  # noqa: E402

from agentic_isolation.contract import OperationRef, WorkspaceRef  # noqa: E402
from agentic_isolation.providers import e2b_operations  # noqa: E402
from agentic_isolation.providers.e2b_client import _ChunkQueue, _SdkCommand  # noqa: E402
from agentic_isolation.providers.operation_spool import OperationFailure  # noqa: E402


def _ended(exit_code: int, output: bytes = b"") -> object:
    async def events() -> AsyncIterator[object]:
        if output:
            data = process_pb.ProcessEvent.DataEvent(output=Oneof("stdout", output))
            yield process_pb.StartResponse(event=process_pb.ProcessEvent(event=Oneof("data", data)))
        end = process_pb.ProcessEvent.EndEvent(exit_code=exit_code, exited=True)
        yield process_pb.StartResponse(event=process_pb.ProcessEvent(event=Oneof("end", end)))

    async def kill() -> bool:
        return True

    chunks = _ChunkQueue()
    handle = e2b_handle.AsyncCommandHandle(1234, kill, events(), on_stdout=chunks.put)
    return _SdkCommand(handle, chunks)


class _Client:
    def __init__(self, exit_code: int, output: bytes = b"") -> None:
        self._exit_code = exit_code
        self._output = output

    async def start(self, *args: object, **kwargs: object) -> object:
        return _ended(self._exit_code, self._output)


def _operation() -> SimpleNamespace:
    ref = OperationRef(
        workspace=WorkspaceRef(provider="e2b", resource_id="sbx"),
        operation_id="sbx-op1",
        operation_key="k1",
    )
    return SimpleNamespace(group=1234, token="t", sandbox_id="sbx", ref=ref)


def _operations(client: _Client) -> e2b_operations.E2BOperations:
    return e2b_operations.E2BOperations(
        client,  # type: ignore[arg-type]
        run_as="user",
        credentials=None,
        retention_bytes=1024,
        max_page_bytes=1024,
    )


async def test_a_successful_termination_is_confirmed_without_waiting_out_the_deadline() -> None:
    loop = asyncio.get_running_loop()
    began = loop.time()
    await _operations(_Client(0))._terminate(_operation(), grace_seconds=0)  # type: ignore[arg-type]
    assert loop.time() - began < 5


async def test_a_failed_termination_is_reported_with_its_output() -> None:
    with pytest.raises(OperationFailure) as raised:
        await _operations(_Client(1, b"7 survived\n"))._terminate(  # type: ignore[arg-type]
            _operation(), grace_seconds=0
        )
    assert raised.value.error.kind == "provider_unavailable"
    assert "exited 1" in raised.value.error.message
    assert "7 survived" in raised.value.error.message
