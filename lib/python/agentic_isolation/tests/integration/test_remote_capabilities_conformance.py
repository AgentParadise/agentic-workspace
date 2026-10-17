"""Conformance for the remote-tier capabilities (syntropic137/syntropic137#1911).

Every test takes a `provider` and a `make_workspace` factory from the
parametrised fixture below, so a new provider (E2B, #1915) joins by adding one
entry to `_PROVIDERS`; the assertions do not change.

These run against a REAL Docker daemon, never a double. They carry the
`docker_conformance` marker, and on a GitHub Actions Linux runner (the
`python-compatibility` job runs `uv run pytest` for agentic_isolation), or with
REQUIRE_DOCKER_CONFORMANCE=1, a missing daemon fails instead of skipping.

Run with: pytest -m docker_conformance tests/integration/test_remote_capabilities_conformance.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from agentic_isolation import (
    SecurityConfig,
    StreamOutcome,
    StreamSession,
    SupportsFileTransfer,
    SupportsOwnership,
    SupportsStreamingExec,
    Workspace,
    WorkspaceConfig,
    WorkspaceDockerProvider,
    WorkspaceOwnershipError,
)

IMAGE = "python:3.12-slim"

_REQUIRED = os.environ.get("REQUIRE_DOCKER_CONFORMANCE") == "1" or (
    os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux"
)
_AVAILABLE = WorkspaceDockerProvider.is_available()

pytestmark = [
    pytest.mark.docker_conformance,
    pytest.mark.integration,
    pytest.mark.skipif(not _AVAILABLE and not _REQUIRED, reason="Docker not available"),
]

# Every byte value, so any text decode on the way back corrupts it.
BINARY = bytes(range(256)) * 4


MakeWorkspace = Callable[[dict[str, str]], Awaitable[Workspace]]


def _docker() -> WorkspaceDockerProvider:
    return WorkspaceDockerProvider(default_image=IMAGE, security=SecurityConfig.development())


_PROVIDERS: dict[str, Callable[[], WorkspaceDockerProvider]] = {"docker": _docker}


@pytest.fixture(autouse=True)
def _docker_is_required() -> None:
    if _REQUIRED and not _AVAILABLE:
        pytest.fail(
            "Docker conformance is required here (REQUIRE_DOCKER_CONFORMANCE=1 or a GitHub"
            " Actions Linux runner) but the docker daemon is not available"
        )


@pytest.fixture(params=sorted(_PROVIDERS))
def provider(request: pytest.FixtureRequest) -> WorkspaceDockerProvider:
    made = _PROVIDERS[request.param]()
    assert isinstance(made, SupportsStreamingExec)
    assert isinstance(made, SupportsFileTransfer)
    assert isinstance(made, SupportsOwnership)
    return made


@pytest.fixture
async def make_workspace(
    provider: WorkspaceDockerProvider,
) -> AsyncIterator[MakeWorkspace]:
    created: list[Workspace] = []

    async def make(labels: dict[str, str]) -> Workspace:
        workspace = await provider.create(
            WorkspaceConfig(provider=provider.name, image=IMAGE, labels=labels)
        )
        created.append(workspace)
        return workspace

    yield make
    for workspace in created:
        await provider.destroy(workspace)


async def _run(session: StreamSession) -> tuple[list[str], StreamOutcome]:
    lines = [line async for line in session.lines()]
    return lines, await session.outcome()


async def test_stream_merges_stdout_and_stderr_in_write_order(
    provider: WorkspaceDockerProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    script = "echo out-1; echo err-1 >&2; echo out-2; echo err-2 >&2; exit 7"
    session = await provider.start_stream(workspace, ["sh", "-c", script])

    lines, outcome = await _run(session)

    assert lines == ["out-1", "err-1", "out-2", "err-2"]
    assert outcome == StreamOutcome(end="completed", exit_code=7)


async def test_stream_env_reaches_the_command_not_the_argv(
    provider: WorkspaceDockerProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    session = await provider.start_stream(
        workspace, ["sh", "-c", 'echo "$CONFORMANCE_VALUE"'], env={"CONFORMANCE_VALUE": "e-91c"}
    )

    lines, outcome = await _run(session)

    assert lines == ["e-91c"]
    assert outcome.completed and outcome.exit_code == 0


async def test_stream_timeout_stops_the_sandbox_process(
    provider: WorkspaceDockerProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    marker = f"/tmp/timed-out-{uuid.uuid4().hex}"
    session = await provider.start_stream(
        workspace, ["sh", "-c", f"echo started; sleep 4; touch {marker}"], timeout=1
    )

    lines, outcome = await asyncio.wait_for(_run(session), timeout=30)

    assert lines == ["started"]
    assert outcome == StreamOutcome(end="timed_out")
    assert not outcome.disconnected
    # The sandbox process is gone, not merely the local pipe: it never wakes.
    await asyncio.sleep(5)
    assert (await provider.execute(workspace, f"test -e {marker}")).exit_code != 0


async def test_stream_lost_transport_is_disconnected_and_process_lives(
    provider: WorkspaceDockerProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    marker = f"/tmp/alive-{uuid.uuid4().hex}"
    session = await provider.start_stream(
        workspace, ["sh", "-c", f"echo started; sleep 3; touch {marker}; sleep 60"]
    )
    lines = session.lines()
    assert await anext(lines) == "started"

    # Lose the transport: kill the local client, as a dropped connection would.
    # Reaching into the Docker session is the only way to cut its pipe.
    session._proc.kill()  # type: ignore[attr-defined]
    outcome = await session.outcome()

    assert outcome == StreamOutcome(end="disconnected")
    assert not outcome.timed_out and not outcome.completed
    await asyncio.sleep(5)
    alive = await provider.execute(workspace, f"test -e {marker}")
    assert alive.exit_code == 0, "the sandbox process died with the transport"


async def test_cancel_stops_the_sandbox_process(
    provider: WorkspaceDockerProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    marker = f"/tmp/cancelled-{uuid.uuid4().hex}"
    session = await provider.start_stream(
        workspace, ["sh", "-c", f"echo started; sleep 3; touch {marker}"]
    )
    lines = session.lines()
    assert await anext(lines) == "started"

    await session.cancel()
    outcome = await session.outcome()

    assert outcome.completed and outcome.exit_code is None and outcome.signal is not None
    await asyncio.sleep(4)
    assert (await provider.execute(workspace, f"test -e {marker}")).exit_code != 0


@pytest.mark.parametrize("base_path", ["/workspace", "/spool"])
async def test_read_files_round_trips_binary(
    provider: WorkspaceDockerProvider, make_workspace: MakeWorkspace, base_path: str
) -> None:
    workspace = await make_workspace({})
    write = (
        "import sys; d = bytes(range(256)) * 4; "
        f"import os; os.makedirs('{base_path}/out/nested', exist_ok=True); "
        f"open('{base_path}/out/nested/blob.bin', 'wb').write(d); "
        f"open('{base_path}/out/skip.txt', 'w').write('no')"
    )
    written = await provider.execute(workspace, f'python3 -c "{write}"')
    assert written.exit_code == 0, written.stderr

    files = await provider.read_files(workspace, ["out/*.bin"], base_path=base_path)  # type: ignore[arg-type]

    assert files == [("out/nested/blob.bin", BINARY)]


async def test_list_owned_returns_exactly_the_host_workspaces(
    provider: WorkspaceDockerProvider, make_workspace: MakeWorkspace
) -> None:
    host, other = f"host-{uuid.uuid4().hex}", f"host-{uuid.uuid4().hex}"
    mine = [await make_workspace({"syn.host_id": host}) for _ in range(2)]
    await make_workspace({"syn.host_id": other})
    await make_workspace({})

    owned = await provider.list_owned({"syn.host_id": host})

    assert sorted(w.workspace_id for w in owned) == sorted(w.id for w in mine)
    assert all(w.labels["syn.host_id"] == host for w in owned)


async def test_attach_returns_a_usable_workspace_on_a_cache_miss(
    provider: WorkspaceDockerProvider, make_workspace: MakeWorkspace
) -> None:
    host = f"host-{uuid.uuid4().hex}"
    labels = {"syn.host_id": host, "syn.execution_id": "exec-attach"}
    workspace = await make_workspace(labels)

    # A fresh provider has no cache, as after a restart.
    attached = await _PROVIDERS[provider.name]().attach(workspace.id, expect_labels=labels)

    assert attached.id == workspace.id
    session = await _PROVIDERS[provider.name]().start_stream(attached, ["echo", "reattached"])
    lines, outcome = await _run(session)
    assert lines == ["reattached"] and outcome.exit_code == 0


async def test_attach_with_mismatched_labels_raises(
    provider: WorkspaceDockerProvider, make_workspace: MakeWorkspace
) -> None:
    host = f"host-{uuid.uuid4().hex}"
    workspace = await make_workspace({"syn.host_id": host, "syn.execution_id": "exec-a"})

    with pytest.raises(WorkspaceOwnershipError) as refused:
        await _PROVIDERS[provider.name]().attach(
            workspace.id, expect_labels={"syn.host_id": host, "syn.execution_id": "exec-b"}
        )
    assert refused.value.mismatched == ("syn.execution_id",)

    with pytest.raises(WorkspaceOwnershipError):
        await provider.attach(workspace.id, expect_labels={"syn.host_id": "someone-else"})
