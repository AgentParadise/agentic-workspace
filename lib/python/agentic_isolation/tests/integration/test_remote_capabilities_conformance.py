"""Conformance for the remote-tier capabilities (syntropic137/syntropic137#1911).

The assertions are shared and speak only the published Protocols. Everything
provider-specific (availability, whether a missing backend fails or skips,
fresh-provider creation, workspace config, how to cut the transport) lives in
a `ConformanceHarness`, so a new provider (E2B, #1915) joins by adding one
entry to `_HARNESSES`; the assertions do not change.

The Docker harness runs against a REAL Docker daemon, never a double. These
tests carry the `docker_conformance` marker, and on a GitHub Actions Linux
runner (the `python-compatibility` job runs `uv run pytest` for
agentic_isolation), or with REQUIRE_DOCKER_CONFORMANCE=1, a missing daemon
fails instead of skipping.

Run with: pytest -m docker_conformance tests/integration/test_remote_capabilities_conformance.py
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from agentic_isolation import (
    E2BWorkspaceProvider,
    FileRefusal,
    ReadFilesResult,
    ReadLimits,
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
    WorkspaceProvider,
)
from agentic_isolation.providers.capabilities import TransferRoot
from agentic_isolation.providers.docker_capabilities import DockerStreamSession
from agentic_isolation.providers.e2b import E2BStreamSession
from agentic_isolation.providers.e2b_client import SdkE2BClient, _SdkCommand
from tests.e2b_fake import FakeE2BClient

pytestmark = [pytest.mark.docker_conformance, pytest.mark.integration]

# Every byte value, so any text decode on the way back corrupts it.
BINARY = bytes(range(256)) * 4


class ConformanceProvider(
    WorkspaceProvider, SupportsStreamingExec, SupportsFileTransfer, SupportsOwnership, Protocol
):
    """The surface the shared assertions use: the port plus all three capabilities."""


@dataclass(frozen=True)
class ConformanceHarness:
    """What one provider supplies so the shared assertions can run against it."""

    available: Callable[[], bool]
    # True where a missing backend must fail the run rather than skip it.
    required: Callable[[], bool]
    new_provider: Callable[[], ConformanceProvider]
    workspace_config: Callable[[str, dict[str, str]], WorkspaceConfig]
    # Cut the live transport of a running session, as a dropped connection would.
    interrupt_transport: Callable[[StreamSession], None]
    # Point the provider's new sessions at a transport that cannot be reached.
    break_transport: Callable[[pytest.MonkeyPatch, ConformanceProvider], None]


_DOCKER_IMAGE = "python:3.12-slim"


def _docker_provider() -> ConformanceProvider:
    return WorkspaceDockerProvider(
        default_image=_DOCKER_IMAGE, security=SecurityConfig.development()
    )


def _docker_required() -> bool:
    return os.environ.get("REQUIRE_DOCKER_CONFORMANCE") == "1" or (
        os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux"
    )


def _docker_interrupt(session: StreamSession) -> None:
    # Killing the local client is the only way to cut a docker exec's pipe.
    assert isinstance(session, DockerStreamSession)
    session._proc.kill()


def _docker_break(monkeypatch: pytest.MonkeyPatch, _provider: ConformanceProvider) -> None:
    monkeypatch.setenv("DOCKER_HOST", f"unix:///nonexistent/{uuid.uuid4().hex}.sock")


def _e2b_available() -> bool:
    return bool(os.environ.get("E2B_API_KEY")) and bool(os.environ.get("E2B_TEMPLATE"))


def _e2b_interrupt(session: StreamSession) -> None:
    # The pinned SDK's `AsyncCommandHandle.disconnect` stops the event stream
    # without killing the process (its docstring, e2b==2.52.0).
    assert isinstance(session, E2BStreamSession)
    command = session._command
    assert isinstance(command, _SdkCommand)
    asyncio.get_running_loop().create_task(command.disconnect())


def _e2b_break(monkeypatch: pytest.MonkeyPatch, provider: ConformanceProvider) -> None:
    # The adapter caches each sandbox with its command channel already built,
    # so the cache is emptied and the reconnect goes to a domain that does
    # not resolve: the real SDK connect path fails, not a stand-in.
    assert isinstance(provider, E2BWorkspaceProvider)
    client = provider._client
    assert isinstance(client, SdkE2BClient)
    monkeypatch.setattr(client, "_sandboxes", {})
    monkeypatch.setenv("E2B_DOMAIN", f"nonexistent-{uuid.uuid4().hex}.invalid")


_HARNESSES: dict[str, ConformanceHarness] = {
    "docker": ConformanceHarness(
        available=WorkspaceDockerProvider.is_available,
        required=_docker_required,
        new_provider=_docker_provider,
        workspace_config=lambda name, labels: WorkspaceConfig(
            provider=name, image=_DOCKER_IMAGE, labels=labels
        ),
        interrupt_transport=_docker_interrupt,
        break_transport=_docker_break,
    ),
    # syntropic137/syntropic137#1915. Needs E2B_API_KEY and E2B_TEMPLATE (a
    # template built by `build_template`). Without them every case reports
    # "NOT RUN", and REQUIRE_E2B_LIVE=1 turns that into a failure.
    "e2b": ConformanceHarness(
        available=_e2b_available,
        required=lambda: os.environ.get("REQUIRE_E2B_LIVE") == "1",
        new_provider=lambda: E2BWorkspaceProvider(os.environ["E2B_TEMPLATE"]),
        workspace_config=lambda name, labels: WorkspaceConfig(provider=name, labels=labels),
        interrupt_transport=_e2b_interrupt,
        break_transport=_e2b_break,
    ),
}


MakeWorkspace = Callable[[dict[str, str]], Awaitable[Workspace]]


@pytest.fixture(params=sorted(_HARNESSES))
def harness(request: pytest.FixtureRequest) -> ConformanceHarness:
    chosen = _HARNESSES[request.param]
    if not chosen.available():
        if chosen.required():
            pytest.fail(
                f"{request.param} conformance is required here but its backend is not available"
            )
        pytest.skip(f"NOT RUN: {request.param} backend not available")
    return chosen


@pytest.fixture
def provider(harness: ConformanceHarness) -> ConformanceProvider:
    made = harness.new_provider()
    assert isinstance(made, SupportsStreamingExec)
    assert isinstance(made, SupportsFileTransfer)
    assert isinstance(made, SupportsOwnership)
    return made


@pytest.fixture
async def make_workspace(
    harness: ConformanceHarness, provider: ConformanceProvider
) -> AsyncIterator[MakeWorkspace]:
    created: list[Workspace] = []
    # Every workspace this test makes carries the run label, so cleanup can
    # be checked against the provider's own list (#1915: nothing left behind).
    run = {"conformance.run": uuid.uuid4().hex}

    async def make(labels: dict[str, str]) -> Workspace:
        workspace = await provider.create(
            harness.workspace_config(provider.name, {**labels, **run})
        )
        created.append(workspace)
        return workspace

    yield make
    # Destroy every workspace even when one fails, so one error leaks nothing.
    failures: list[Exception] = []
    for workspace in created:
        try:
            await provider.destroy(workspace)
        except Exception as exc:
            failures.append(exc)
    for other in failures[1:]:
        failures[0].add_note(f"destroying another workspace also failed: {other!r}")
    if failures:
        raise failures[0]
    assert await provider.list_owned(run) == [], "workspaces were left in the provider list"


async def _run(session: StreamSession) -> tuple[list[str], StreamOutcome]:
    lines = [line async for line in session.lines()]
    return lines, await session.outcome()


async def test_stream_merges_stdout_and_stderr_in_write_order(
    provider: ConformanceProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    script = "echo out-1; echo err-1 >&2; echo out-2; echo err-2 >&2; exit 7"
    session = await provider.start_stream(workspace, ["sh", "-c", script])

    lines, outcome = await _run(session)

    assert lines == ["out-1", "err-1", "out-2", "err-2"]
    assert outcome == StreamOutcome(end="completed", exit_code=7)
    # A known exit stays known: observing it again does not lose it.
    assert await session.outcome() == outcome


async def test_stream_env_reaches_the_command_not_the_argv(
    provider: ConformanceProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    session = await provider.start_stream(
        workspace, ["sh", "-c", 'echo "$CONFORMANCE_VALUE"'], env={"CONFORMANCE_VALUE": "e-91c"}
    )

    lines, outcome = await _run(session)

    assert lines == ["e-91c"]
    assert outcome.completed and outcome.exit_code == 0


async def test_stream_timeout_stops_the_sandbox_process(
    provider: ConformanceProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    marker = f"/tmp/timed-out-{uuid.uuid4().hex}"
    session = await provider.start_stream(
        workspace, ["sh", "-c", f"echo started; sleep 6; touch {marker}"], timeout=3
    )

    lines, outcome = await asyncio.wait_for(_run(session), timeout=30)

    assert lines == ["started"]
    assert outcome == StreamOutcome(end="timed_out")
    assert not outcome.disconnected
    # The sandbox process is gone, not merely the local pipe: it never wakes.
    await asyncio.sleep(5)
    assert (await provider.execute(workspace, f"test -e {marker}")).exit_code != 0


async def test_stream_lost_transport_is_disconnected_and_cancel_still_stops_it(
    harness: ConformanceHarness, provider: ConformanceProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    alive = f"/tmp/alive-{uuid.uuid4().hex}"
    survived = f"/tmp/survived-{uuid.uuid4().hex}"
    session = await provider.start_stream(
        workspace, ["sh", "-c", f"echo started; sleep 3; touch {alive}; sleep 8; touch {survived}"]
    )
    lines = session.lines()
    assert await anext(lines) == "started"

    harness.interrupt_transport(session)
    outcome = await session.outcome()

    assert outcome.disconnected
    assert outcome.exit_code is None and outcome.signal is None
    assert not outcome.timed_out and not outcome.completed
    await asyncio.sleep(5)
    still_running = await provider.execute(workspace, f"test -e {alive}")
    assert still_running.exit_code == 0, "the sandbox process died with the transport"

    # Losing the stream did not end the process, so cancel must still reach it.
    await session.cancel()
    assert (await session.outcome()).signal is not None
    await asyncio.sleep(8)
    assert (await provider.execute(workspace, f"test -e {survived}")).exit_code != 0


async def test_stream_unreachable_transport_is_never_completion(
    harness: ConformanceHarness,
    provider: ConformanceProvider,
    make_workspace: MakeWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await make_workspace({})

    with monkeypatch.context() as patched:
        harness.break_transport(patched, provider)
        session = await provider.start_stream(workspace, ["echo", "should-not-run"])
        lines, outcome = await asyncio.wait_for(_run(session), timeout=30)

    assert "should-not-run" not in lines
    assert outcome.disconnected and outcome.exit_code is None
    # The sandbox's own non-zero exit is still a completion, not a transport loss.
    failed = await provider.start_stream(workspace, ["sh", "-c", "exit 1"])
    assert (await _run(failed))[1] == StreamOutcome(end="completed", exit_code=1)


async def test_stream_delivers_lines_of_any_length(
    provider: ConformanceProvider, make_workspace: MakeWorkspace
) -> None:
    workspace = await make_workspace({})
    write = "import os; os.write(1, b'first\\n' + b'x' * 70000 + b'\\nlast-without-newline')"
    session = await provider.start_stream(workspace, ["python3", "-c", write])

    lines, outcome = await _run(session)

    assert lines == ["first", "x" * 70000, "last-without-newline"]
    assert outcome == StreamOutcome(end="completed", exit_code=0)


async def test_cancel_stops_the_sandbox_process(
    provider: ConformanceProvider, make_workspace: MakeWorkspace
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


TransferProvider = ConformanceProvider | E2BWorkspaceProvider


@pytest.fixture(params=[*sorted(_HARNESSES), "e2b-double"])
async def transfer(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[TransferProvider, Workspace]]:
    """A provider and a fresh workspace for the `read_files` cases (#100).

    Docker is real and E2B live is local only, as above. The E2B double runs
    the provider's real sandbox commands against local roots, so CI covers the
    E2B read path without a key.
    """
    if request.param == "e2b-double":
        root = tmp_path / "sandbox"
        root.mkdir()
        client = FakeE2BClient(executing_root=root)
        client.spool_root.mkdir()
        made: TransferProvider = E2BWorkspaceProvider("t", client=client)
        workspace = await made.create(WorkspaceConfig(provider="e2b"))
    else:
        chosen = _HARNESSES[request.param]
        if not chosen.available():
            if chosen.required():
                pytest.fail(f"{request.param} conformance is required here but is not available")
            pytest.skip(f"NOT RUN: {request.param} backend not available")
        made = chosen.new_provider()
        workspace = await made.create(chosen.workspace_config(made.name, {}))
    try:
        assert isinstance(made, SupportsFileTransfer)
        yield made, workspace
    finally:
        await made.destroy(workspace)


async def _plant(provider: TransferProvider, workspace: Workspace, code: str) -> None:
    """Run `code` as Python in the sandbox, as an agent planting files would."""
    written = await provider.execute(workspace, shlex.join(["python3", "-c", code]))
    assert written.exit_code == 0, written.stdout + written.stderr


@pytest.mark.parametrize("base_path", ["/workspace", "/spool"])
async def test_read_files_round_trips_binary(
    transfer: tuple[TransferProvider, Workspace], base_path: TransferRoot
) -> None:
    provider, workspace = transfer
    await _plant(
        provider,
        workspace,
        f"import os; os.makedirs('{base_path}/out/nested', exist_ok=True); "
        f"open('{base_path}/out/nested/blob.bin', 'wb').write(bytes(range(256)) * 4); "
        f"open('{base_path}/out/skip.txt', 'w').write('no')",
    )

    got = await provider.read_files(workspace, ["out/*.bin"], base_path=base_path)

    assert got == ReadFilesResult(files=(("out/nested/blob.bin", BINARY),), refused=())
    assert got.complete


@pytest.mark.parametrize("base_path", ["/workspace", "/spool"])
async def test_read_files_refuses_every_name_of_a_hard_link(
    transfer: tuple[TransferProvider, Workspace], base_path: TransferRoot
) -> None:
    # Reverses the #85 contract, which returned the bytes under every name:
    # a hard link lets the sandbox alias a file it did not write there.
    provider, workspace = transfer
    await _plant(
        provider,
        workspace,
        f"import os; os.makedirs('{base_path}/out', exist_ok=True); "
        f"open('{base_path}/out/a.bin', 'wb').write(b'aliased'); "
        f"os.link('{base_path}/out/a.bin', '{base_path}/out/b.bin')",
    )

    for name in ("out/a.bin", "out/b.bin"):
        got = await provider.read_files(workspace, [name], base_path=base_path)
        assert got == ReadFilesResult(files=(), refused=(FileRefusal(name, "hard_link"),))
    both = await provider.read_files(workspace, ["out/*.bin"], base_path=base_path)
    assert both.refused == (
        FileRefusal("out/a.bin", "hard_link"),
        FileRefusal("out/b.bin", "hard_link"),
    )
    assert both.files == ()


async def test_read_files_refuses_symlinks_that_escape_the_root(
    transfer: tuple[TransferProvider, Workspace],
) -> None:
    provider, workspace = transfer
    await _plant(
        provider,
        workspace,
        "import os; os.makedirs('/workspace/out', exist_ok=True); "
        "os.symlink('/etc/passwd', '/workspace/out/absolute'); "
        "os.symlink('../../etc/passwd', '/workspace/out/relative')",
    )

    got = await provider.read_files(workspace, ["out/*"], base_path="/workspace")

    assert got == ReadFilesResult(
        files=(),
        refused=(
            FileRefusal("out/absolute", "escaping_symlink"),
            FileRefusal("out/relative", "escaping_symlink"),
        ),
    )


async def test_read_files_refuses_a_symlink_inside_the_root_and_reads_its_target(
    transfer: tuple[TransferProvider, Workspace],
) -> None:
    provider, workspace = transfer
    await _plant(
        provider,
        workspace,
        "import os; os.makedirs('/workspace/out', exist_ok=True); "
        "open('/workspace/out/real.txt', 'wb').write(b'real'); "
        "os.symlink('real.txt', '/workspace/out/alias.txt')",
    )

    got = await provider.read_files(workspace, ["out/*.txt"], base_path="/workspace")

    assert got == ReadFilesResult(
        files=(("out/real.txt", b"real"),), refused=(FileRefusal("out/alias.txt", "symlink"),)
    )


async def test_read_files_refuses_special_files(
    transfer: tuple[TransferProvider, Workspace],
) -> None:
    provider, workspace = transfer
    await _plant(
        provider,
        workspace,
        "import os, socket; os.makedirs('/workspace/out', exist_ok=True); "
        "os.mkfifo('/workspace/out/fifo'); "
        "socket.socket(socket.AF_UNIX).bind('/workspace/out/sock')",
    )

    got = await provider.read_files(workspace, ["out/*"], base_path="/workspace")

    assert got == ReadFilesResult(
        files=(),
        refused=(FileRefusal("out/fifo", "special_file"), FileRefusal("out/sock", "special_file")),
    )


async def test_read_files_refuses_a_file_over_the_per_file_limit(
    transfer: tuple[TransferProvider, Workspace],
) -> None:
    provider, workspace = transfer
    await _plant(
        provider,
        workspace,
        "import os; os.makedirs('/workspace/out', exist_ok=True); "
        "open('/workspace/out/fits', 'wb').write(b'x' * 10); "
        "open('/workspace/out/over', 'wb').write(b'x' * 11)",
    )

    got = await provider.read_files(
        workspace, ["out/*"], base_path="/workspace", limits=ReadLimits(max_file_bytes=10)
    )

    assert got == ReadFilesResult(
        files=(("out/fits", b"x" * 10),), refused=(FileRefusal("out/over", "file_too_large"),)
    )


async def test_read_files_refuses_what_would_pass_the_total_byte_limit(
    transfer: tuple[TransferProvider, Workspace],
) -> None:
    provider, workspace = transfer
    await _plant(
        provider,
        workspace,
        "import os; os.makedirs('/workspace/out', exist_ok=True); "
        "[open(f'/workspace/out/{n}', 'wb').write(b'x' * 4) for n in 'abc']",
    )

    got = await provider.read_files(
        workspace, ["out/*"], base_path="/workspace", limits=ReadLimits(max_total_bytes=10)
    )

    assert got == ReadFilesResult(
        files=(("out/a", b"xxxx"), ("out/b", b"xxxx")),
        refused=(FileRefusal("out/c", "total_bytes_exceeded"),),
    )


async def test_read_files_stops_the_walk_at_the_entry_limit_and_says_so(
    transfer: tuple[TransferProvider, Workspace],
) -> None:
    provider, workspace = transfer
    await _plant(
        provider,
        workspace,
        "[open(f'/spool/f{n}', 'wb').write(b'x') for n in range(5)]",
    )

    capped = await provider.read_files(
        workspace, ["f*"], base_path="/spool", limits=ReadLimits(max_entries=3)
    )
    whole = await provider.read_files(
        workspace, ["f*"], base_path="/spool", limits=ReadLimits(max_entries=100)
    )

    assert not capped.complete
    assert [r.reason for r in capped.refused] == ["entry_limit_exceeded"]
    assert len(capped.files) <= 3
    assert whole.complete and [path for path, _ in whole.files] == [f"f{n}" for n in range(5)]


async def test_read_files_does_not_walk_below_the_depth_limit(
    transfer: tuple[TransferProvider, Workspace],
) -> None:
    provider, workspace = transfer
    await _plant(
        provider,
        workspace,
        "import os; os.makedirs('/workspace/a/b/c', exist_ok=True); "
        "open('/workspace/a/ok.txt', 'wb').write(b'ok'); "
        "open('/workspace/a/b/edge.txt', 'wb').write(b'edge'); "
        "open('/workspace/a/b/c/deep.txt', 'wb').write(b'deep')",
    )

    got = await provider.read_files(
        workspace, ["a/*"], base_path="/workspace", limits=ReadLimits(max_depth=2)
    )

    assert got == ReadFilesResult(
        files=(("a/ok.txt", b"ok"),),
        refused=(
            FileRefusal("a/b/c", "depth_exceeded"),
            FileRefusal("a/b/edge.txt", "depth_exceeded"),
        ),
    )


async def test_read_files_without_limits_is_bounded_by_default(
    transfer: tuple[TransferProvider, Workspace],
) -> None:
    # One byte past the default per-file limit: a caller that passes no
    # limits must still be refused, not handed the whole file.
    provider, workspace = transfer
    over = ReadLimits().max_file_bytes + 1
    await _plant(provider, workspace, f"open('/spool/big', 'wb').truncate({over})")

    got = await provider.read_files(workspace, ["big"], base_path="/spool")

    assert got == ReadFilesResult(files=(), refused=(FileRefusal("big", "file_too_large"),))


async def test_list_owned_returns_exactly_the_host_workspaces(
    provider: ConformanceProvider, make_workspace: MakeWorkspace
) -> None:
    host, other = f"host-{uuid.uuid4().hex}", f"host-{uuid.uuid4().hex}"
    mine = [await make_workspace({"syn.host_id": host}) for _ in range(2)]
    await make_workspace({"syn.host_id": other})
    await make_workspace({})

    owned = await provider.list_owned({"syn.host_id": host})

    assert sorted(w.workspace_id for w in owned) == sorted(w.id for w in mine)
    assert all(w.labels["syn.host_id"] == host for w in owned)


async def test_attach_returns_a_usable_workspace_on_a_cache_miss(
    harness: ConformanceHarness, provider: ConformanceProvider, make_workspace: MakeWorkspace
) -> None:
    host = f"host-{uuid.uuid4().hex}"
    labels = {"syn.host_id": host, "syn.execution_id": "exec-attach"}
    workspace = await make_workspace(labels)

    # A fresh provider has no cache, as after a restart.
    attached = await harness.new_provider().attach(workspace.id, expect_labels=labels)

    assert attached.id == workspace.id
    session = await harness.new_provider().start_stream(attached, ["echo", "reattached"])
    lines, outcome = await _run(session)
    assert lines == ["reattached"] and outcome.exit_code == 0


async def test_attach_with_mismatched_labels_raises(
    harness: ConformanceHarness, provider: ConformanceProvider, make_workspace: MakeWorkspace
) -> None:
    host = f"host-{uuid.uuid4().hex}"
    workspace = await make_workspace({"syn.host_id": host, "syn.execution_id": "exec-a"})

    with pytest.raises(WorkspaceOwnershipError) as refused:
        await harness.new_provider().attach(
            workspace.id, expect_labels={"syn.host_id": host, "syn.execution_id": "exec-b"}
        )
    assert refused.value.mismatched == ("syn.execution_id",)

    with pytest.raises(WorkspaceOwnershipError):
        await provider.attach(workspace.id, expect_labels={"syn.host_id": "someone-else"})
