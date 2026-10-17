"""Offline tests of the E2B adapter, session transitions and reporter (#1915).

These drive the pinned SDK's real `AsyncCommandHandle` over synthetic event
streams, and the SDK's real template builder with only the network call
replaced. They establish this package's local wiring and decisions: how an
SDK error is mapped, when a kill counts as confirmed, what a failed create
cleans up. They do NOT establish E2B service behaviour; that is the gated
live module, tests/integration/test_e2b_live.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from http import HTTPStatus

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from e2b import AsyncSandbox, SandboxException, SandboxQuery, ServiceBusyException
from e2b.api.client.api.sandboxes import get_v2_sandboxes
from e2b.api.client.client import AuthenticatedClient
from e2b.api.client.models.listed_sandbox import ListedSandbox
from e2b.api.client.types import UNSET, Response
from e2b.envd.process import process_pb
from e2b.sandbox_async.commands.command_handle import AsyncCommandHandle

from agentic_isolation import WorkspaceConfig
from agentic_isolation.providers import (
    CommandExit,
    E2BTemplateSpec,
    E2BTransportError,
    E2BWorkspaceProvider,
    SandboxRemovalError,
    SdkE2BClient,
    StreamOutcome,
    build_template,
    e2b_client,
)
from agentic_isolation.providers import e2b as e2b_module
from agentic_isolation.providers.e2b_client import KILL_SIGNAL, UnstartedCommand, _ChunkQueue
from tests.e2b_fake import FakeE2BClient, FakeRun
from tests.integration import conftest as reporting

_DIGEST = "sha256:" + "a" * 64
_MANIFEST = "sha256:" + "b" * 64


@pytest.fixture
def client() -> FakeE2BClient:
    return FakeE2BClient()


@pytest.fixture(autouse=True)
def quick_kill_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(e2b_module, "_KILL_SECONDS", 0.05)


async def _hanging_session(
    client: FakeE2BClient, *, timeout: float | None = None
) -> tuple[E2BWorkspaceProvider, e2b_module.E2BStreamSession]:
    provider = E2BWorkspaceProvider("tpl", client=client)
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    client.responder = lambda _command: FakeRun(chunks=["started\n"], hang=True)
    session = await provider.start_stream(workspace, ["sleep", "60"], timeout=timeout)
    assert isinstance(session, e2b_module.E2BStreamSession)
    return provider, session


# Cancellation (R2): running and disconnected sessions, confirmed or not.


async def test_cancel_of_a_running_command_reports_the_kill_signal(
    client: FakeE2BClient,
) -> None:
    _, session = await _hanging_session(client)
    lines = session.lines()
    assert await anext(lines) == "started"

    await session.cancel()

    assert await session.outcome() == StreamOutcome(end="completed", signal=KILL_SIGNAL)
    assert client.commands[session.pid].killed
    assert [line async for line in lines] == []


async def test_cancel_after_a_lost_stream_still_kills_and_confirms(
    client: FakeE2BClient,
) -> None:
    provider = E2BWorkspaceProvider("tpl", client=client)
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    client.responder = lambda _command: FakeRun(
        exit=CommandExit(end="disconnected", detail="reset")
    )
    session = await provider.start_stream(workspace, ["sleep", "60"])
    assert (await session.outcome()).disconnected

    await session.cancel()

    assert await session.outcome() == StreamOutcome(end="completed", signal=KILL_SIGNAL)


async def test_refused_cancel_raises_and_claims_nothing(client: FakeE2BClient) -> None:
    _, session = await _hanging_session(client)
    client.commands[session.pid]._kill_works = False

    with pytest.raises(E2BTransportError, match="not confirmed"):
        await session.cancel()

    assert not session._outcome.done()
    client.commands[session.pid]._kill_works = True
    await session.cancel()


# Timeout (R4): every kill failure finishes within the bound, truthfully.


async def test_timeout_with_a_refused_kill_ends_disconnected(client: FakeE2BClient) -> None:
    client.command_kill_works = False
    _, session = await _hanging_session(client, timeout=0.01)

    outcome = await asyncio.wait_for(session.outcome(), timeout=1)
    lines = await asyncio.wait_for(_collect(session), timeout=1)

    assert outcome == StreamOutcome(end="disconnected", detail="timeout kill not confirmed")
    assert lines == ["started"]


# The shapes a kill can fail with: the adapter's own error, the SDK's
# ConnectError on a reset (envd/rpc.py), and an SDK exception.
_KILL_FAILURES = [
    E2BTransportError("kill rpc failed"),
    ConnectError(Code.UNAVAILABLE, "kill reset"),
    SandboxException("kill refused"),
]


@pytest.mark.parametrize("failure", _KILL_FAILURES, ids=["adapter", "connect", "sdk"])
async def test_timeout_with_a_raising_kill_ends_disconnected(
    client: FakeE2BClient, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    _, session = await _hanging_session(client, timeout=0.05)

    async def broken() -> bool:
        raise failure

    monkeypatch.setattr(client.commands[session.pid], "kill", broken)

    outcome = await asyncio.wait_for(session.outcome(), timeout=1)
    assert outcome == StreamOutcome(end="disconnected", detail="timeout kill not confirmed")
    assert session._waiting.done()


@pytest.mark.parametrize("failure", _KILL_FAILURES, ids=["adapter", "connect", "sdk"])
async def test_cancel_with_a_raising_kill_raises_and_claims_nothing(
    client: FakeE2BClient, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    _, session = await _hanging_session(client)

    async def broken() -> bool:
        raise failure

    monkeypatch.setattr(client.commands[session.pid], "kill", broken)

    with pytest.raises(E2BTransportError, match="not confirmed"):
        await session.cancel()

    assert session._cancelled is None and not session._outcome.done()
    session._waiting.cancel()
    await asyncio.gather(session._waiting, session._outcome, return_exceptions=True)


async def test_timeout_with_a_kill_but_no_end_ends_disconnected(
    client: FakeE2BClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, session = await _hanging_session(client, timeout=0.05)

    async def acknowledged_only() -> bool:
        return True  # acknowledged, but the command never reports its end

    monkeypatch.setattr(client.commands[session.pid], "kill", acknowledged_only)

    outcome = await asyncio.wait_for(session.outcome(), timeout=1)
    lines = await asyncio.wait_for(_collect(session), timeout=1)

    assert outcome == StreamOutcome(end="disconnected", detail="timeout kill not confirmed")
    assert session._waiting.done(), "the outcome left command.wait running"
    assert lines == ["started"]


async def _collect(session: e2b_module.E2BStreamSession) -> list[str]:
    return [line async for line in session.lines()]


# Failed creation (R5): nothing allocated is left unreported.


@dataclass
class _InitFailure:
    run: FakeRun
    raised: type[BaseException]


_INIT_FAILURES = {
    "nonzero": _InitFailure(FakeRun(exit=CommandExit(end="exited", exit_code=1)), RuntimeError),
    "transport": _InitFailure(
        FakeRun(exit=CommandExit(end="disconnected", detail="reset")), E2BTransportError
    ),
}


@pytest.mark.parametrize("shape", sorted(_INIT_FAILURES))
async def test_failed_initialisation_kills_the_sandbox(client: FakeE2BClient, shape: str) -> None:
    failure = _INIT_FAILURES[shape]
    client.responder = lambda _command: failure.run
    provider = E2BWorkspaceProvider("tpl", client=client)

    with pytest.raises(failure.raised):
        await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r5"}))

    assert await provider.list_owned({"run": "r5"}) == []
    assert client.events[-1] == "kill sbx-1"


async def test_cancelled_initialisation_kills_the_sandbox(client: FakeE2BClient) -> None:
    client.responder = lambda _command: FakeRun(hang=True)
    provider = E2BWorkspaceProvider("tpl", client=client)
    creating = asyncio.ensure_future(
        provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r5"}))
    )
    while not client.commands:
        await asyncio.sleep(0)

    creating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creating

    assert await provider.list_owned({"run": "r5"}) == []


async def test_failed_cleanup_after_failed_initialisation_names_the_sandbox(
    client: FakeE2BClient,
) -> None:
    client.responder = lambda _command: FakeRun(exit=CommandExit(end="exited", exit_code=1))
    client.kill_is_noop = True
    provider = E2BWorkspaceProvider("tpl", client=client)

    with pytest.raises(SandboxRemovalError) as leaked:
        await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r5"}))

    assert leaked.value.sandbox_id == "sbx-1"
    assert isinstance(leaked.value.__cause__, RuntimeError)


# The SDK adapter (R3, R8), over the pinned SDK's real command handle.


def _handle(events: AsyncGenerator[process_pb.StartResponse, None]) -> AsyncCommandHandle:
    async def kill() -> bool:
        return True

    return AsyncCommandHandle(17, kill, events)


async def _reset() -> AsyncGenerator[process_pb.StartResponse, None]:
    raise ConnectError(Code.UNAVAILABLE, "reset") from ConnectionResetError("reset")
    yield process_pb.StartResponse()


async def _truncated() -> AsyncGenerator[process_pb.StartResponse, None]:
    return
    yield process_pb.StartResponse()


async def _endless() -> AsyncGenerator[process_pb.StartResponse, None]:
    await asyncio.Event().wait()
    yield process_pb.StartResponse()


@pytest.mark.parametrize("events", [_reset, _truncated], ids=["reset", "truncated"])
async def test_sdk_stream_failure_is_disconnected(events: object) -> None:
    assert callable(events)
    chunks = _ChunkQueue()
    command = e2b_client._SdkCommand(_handle(events()), chunks)

    ended = await command.wait()

    assert ended.end == "disconnected" and ended.exit_code is None
    assert [chunk async for chunk in chunks.drain()] == []


async def test_sdk_disconnect_is_disconnected_not_a_cancellation() -> None:
    command = e2b_client._SdkCommand(_handle(_endless()), _ChunkQueue())
    waiting = asyncio.ensure_future(command.wait())
    await asyncio.sleep(0)

    await command.disconnect()

    assert (await waiting).end == "disconnected"


@pytest.mark.parametrize("failure", _KILL_FAILURES[1:], ids=["connect", "sdk"])
async def test_sdk_kill_failure_is_a_transport_error_and_unconfirmed(failure: Exception) -> None:
    async def kill() -> bool:
        raise failure

    command = e2b_client._SdkCommand(AsyncCommandHandle(17, kill, _endless()), _ChunkQueue())
    try:
        with pytest.raises(E2BTransportError, match="kill of pid 17") as raised:
            await command.kill()
        assert raised.value.__cause__ is failure
        assert await e2b_module._confirmed_kill(command) is False
    finally:
        await command.disconnect()


async def test_cancelling_the_waiter_still_propagates() -> None:
    command = e2b_client._SdkCommand(_handle(_endless()), _ChunkQueue())
    waiting = asyncio.ensure_future(command.wait())
    await asyncio.sleep(0)

    waiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiting


async def test_start_on_an_unreachable_sandbox_is_a_disconnected_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unreachable(_self: SdkE2BClient, sandbox_id: str) -> AsyncSandbox:
        raise ConnectError(Code.UNAVAILABLE, f"no route to {sandbox_id}")

    monkeypatch.setattr(SdkE2BClient, "_sandbox", unreachable)

    command = await SdkE2BClient(api_key="unused").start(
        "sbx-1", "echo hi", cwd=None, env={}, timeout=None
    )

    assert isinstance(command, UnstartedCommand)
    assert (await command.wait()).end == "disconnected"
    assert [chunk async for chunk in command.output()] == []
    assert await command.kill() is False


class _PausingSandbox:
    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.calls = 0

    async def pause(self) -> None:
        self.calls += 1
        if self.calls <= self.refusals:
            raise ServiceBusyException("snapshot in progress")


@pytest.mark.parametrize(("refusals", "paused"), [(0, True), (4, True), (5, False)])
async def test_pause_retries_a_busy_refusal_a_bounded_number_of_times(
    monkeypatch: pytest.MonkeyPatch, refusals: int, paused: bool
) -> None:
    sandbox = _PausingSandbox(refusals)
    sdk = SdkE2BClient(api_key="unused")
    sdk._sandboxes["sbx-1"] = sandbox  # type: ignore[assignment]
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    if paused:
        await sdk.pause("sbx-1")
    else:
        with pytest.raises(E2BTransportError, match="5 times"):
            await sdk.pause("sbx-1")

    assert sandbox.calls == min(refusals + 1, 5)
    assert len(slept) == min(refusals, 4)
    assert all(2**i <= s < 2**i + 1 for i, s in enumerate(slept))


async def test_cancelled_pause_backoff_stops_retrying_and_keeps_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _PausingSandbox(refusals=5)
    sdk = SdkE2BClient(api_key="unused")
    sdk._sandboxes["sbx-1"] = sandbox  # type: ignore[assignment]
    backing_off = asyncio.Event()

    async def held_sleep(_seconds: float) -> None:
        if not backing_off.is_set():  # hold the first backoff only
            backing_off.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", held_sleep)
    pausing = asyncio.ensure_future(sdk.pause("sbx-1"))
    await backing_off.wait()

    pausing.cancel()

    done, _ = await asyncio.wait({pausing}, timeout=1)
    assert done, "the cancelled pause kept retrying"
    with pytest.raises(asyncio.CancelledError):
        pausing.result()
    assert sandbox.calls == 1
    cached: object = sdk._sandboxes["sbx-1"]
    assert cached is sandbox


def _listed(sandbox_id: str, state: str, metadata: dict[str, str]) -> ListedSandbox:
    return ListedSandbox.from_dict(
        {
            "templateID": "tpl",
            "sandboxID": sandbox_id,
            "clientID": "client",
            "startedAt": "2026-10-17T00:00:00Z",
            "endAt": "2026-10-17T01:00:00Z",
            "cpuCount": 2,
            "memoryMB": 2048,
            "diskSizeMB": 1024,
            "state": state,
            "envdVersion": "0.2.0",
            "metadata": metadata,
        }
    )


@dataclass(frozen=True)
class _ListRequest:
    token: str
    metadata: object
    state: object
    next_token: object


async def test_list_reads_every_page_in_every_state_and_keeps_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = {"agentic.owner": "run-1", "agentic.role": "workspace"}
    pages: dict[object, tuple[list[ListedSandbox], dict[str, str]]] = {
        UNSET: ([_listed("sbx-1", "running", owned)], {"x-next-token": "page-2"}),
        "page-2": (
            [_listed("sbx-2", "paused", owned), _listed("sbx-3", "running", owned)],
            {"x-next-token": "page-3"},
        ),
        "page-3": ([_listed("sbx-4", "paused", owned)], {}),
    }
    requests: list[_ListRequest] = []

    async def get_page(
        *, client: AuthenticatedClient, **query: object
    ) -> Response[list[ListedSandbox]]:
        requests.append(
            _ListRequest(client.token, query["metadata"], query["state"], query["next_token"])
        )
        items, headers = pages[query["next_token"]]
        return Response(status_code=HTTPStatus.OK, content=b"", headers=headers, parsed=items)

    monkeypatch.setattr(get_v2_sandboxes, "asyncio_detailed", get_page)

    found = await SdkE2BClient(api_key="e2b_key").list(owned)

    assert [(entry.sandbox_id, entry.metadata) for entry in found] == [
        ("sbx-1", owned),
        ("sbx-2", owned),
        ("sbx-3", owned),
        ("sbx-4", owned),
    ]
    expected_metadata = "agentic.owner=run-1&agentic.role=workspace"
    # No state filter: the SDK then lists running AND paused sandboxes
    # (AsyncSandbox.list docstring, e2b 2.52.0).
    assert requests == [
        _ListRequest("e2b_key", expected_metadata, UNSET, UNSET),
        _ListRequest("e2b_key", expected_metadata, UNSET, "page-2"),
        _ListRequest("e2b_key", expected_metadata, UNSET, "page-3"),
    ]
    assert SandboxQuery(metadata=owned).state is None


async def test_build_template_forwards_the_spec_and_returns_the_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from e2b import AsyncTemplate

    seen: dict[str, object] = {}

    @dataclass
    class _Info:
        template_id: str
        build_id: str

    async def build(template: object, name: str, **kwargs: object) -> _Info:
        seen.update(kwargs, template=template, name=name)
        return _Info("tpl-9", "bld-3")

    monkeypatch.setattr(AsyncTemplate, "build", staticmethod(build))
    spec = E2BTemplateSpec(
        name="agentic",
        image=f"ghcr.io/org/image@{_DIGEST}",
        verified_oci_digest=_DIGEST,
        expected_manifest_hash=_MANIFEST,
        cpu_count=2,
        memory_mb=4096,
    )

    receipt = await build_template(
        spec, registry_username="bot", registry_password="pw", api_key="k"
    )

    assert (receipt.template_id, receipt.build_id, receipt.spec) == ("tpl-9", "bld-3", spec)
    assert (seen["name"], seen["cpu_count"], seen["memory_mb"], seen["api_key"]) == (
        "agentic",
        2,
        4096,
        "k",
    )
    built = vars(seen["template"])["_template"]
    assert built._base_image == spec.image
    assert built._registry_config == {"type": "registry", "username": "bot", "password": "pw"}


# The conformance reporter (R7).


def _report(nodeid: str, when: str, outcome: str) -> pytest.TestReport:
    return pytest.TestReport(
        nodeid=nodeid,
        location=("t", 0, nodeid),
        keywords={"e2b_live": 1},
        outcome=outcome,  # type: ignore[arg-type]
        longrepr=None,
        when=when,  # type: ignore[arg-type]
    )


def test_a_teardown_failure_after_a_passed_call_is_reported_failed() -> None:
    outcomes: dict[str, str] = {}
    for nodeid, phases in {
        "leaks": [("setup", "passed"), ("call", "passed"), ("teardown", "failed")],
        "clean": [("setup", "passed"), ("call", "passed"), ("teardown", "passed")],
        "skipped": [("setup", "skipped"), ("teardown", "passed")],
    }.items():
        for when, outcome in phases:
            reporting.record(outcomes, _report(nodeid, when, outcome))

    assert outcomes == {"leaks": "failed", "clean": "passed", "skipped": "skipped"}
