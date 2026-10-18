"""E2BWorkspaceProvider against an in-memory client (syntropic137/syntropic137#1915).

These prove the provider's own decisions: what it sends to the sandbox port,
how it reads the answers, and the teardown order. They prove nothing about
E2B; tests/integration/test_e2b_live.py does that, and is NOT RUN without a key.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Mapping
from pathlib import Path

import pytest

from agentic_isolation import (
    ResourceLimits,
    StreamOutcome,
    SupportsFileTransfer,
    SupportsOwnership,
    SupportsStagedTeardown,
    SupportsStreamingExec,
    WorkspaceConfig,
    WorkspaceNotFoundError,
    WorkspaceOwnershipError,
    WorkspaceProvider,
)
from agentic_isolation.config import CredentialPolicyError
from agentic_isolation.contract import CredentialBinding, CredentialRef, WorkspaceRef
from agentic_isolation.providers import (
    CommandExit,
    E2BTemplateSpec,
    E2BTransportError,
    E2BWorkspaceProvider,
    SandboxRemovalError,
    TemplateSpecError,
    manifest_hash,
)
from agentic_isolation.providers import e2b as e2b_module
from tests.e2b_fake import FakeE2BClient, FakeRun

OCI = "sha256:" + "a" * 64
MANIFEST = "sha256:" + "b" * 64


@pytest.fixture
def client() -> FakeE2BClient:
    return FakeE2BClient()


@pytest.fixture
def provider(client: FakeE2BClient) -> E2BWorkspaceProvider:
    return E2BWorkspaceProvider("tmpl-1", client=client)


@pytest.fixture(autouse=True)
def quick_removal_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    # Polls fast, but leaves a successful lag observation a budget that a
    # loaded runner cannot exhaust; tests that expect expiry ask for
    # `short_removal_bound`.
    monkeypatch.setattr(e2b_module, "_REMOVAL_SECONDS", 5.0)
    monkeypatch.setattr(e2b_module, "_REMOVAL_POLL_SECONDS", 0.01)


@pytest.fixture
def short_removal_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(e2b_module, "_REMOVAL_SECONDS", 0.05)


def test_fake_refuses_to_construct_outside_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    with pytest.raises(RuntimeError, match="outside pytest"):
        FakeE2BClient()


def test_provider_implements_the_port_and_every_capability(
    provider: E2BWorkspaceProvider,
) -> None:
    assert isinstance(provider, WorkspaceProvider)
    assert isinstance(provider, SupportsStreamingExec)
    assert isinstance(provider, SupportsFileTransfer)
    assert isinstance(provider, SupportsOwnership)
    assert isinstance(provider, SupportsStagedTeardown)


async def test_create_starts_the_template_with_labels_as_metadata(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"syn.host_id": "h7"}))

    assert client.events[0] == f"create {workspace.id} tmpl-1"
    assert client.sandboxes[workspace.id].metadata == {"syn.host_id": "h7"}


async def test_create_checks_both_roots_and_every_operation_runs_as_the_image_user(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    await provider.write_file(workspace, "out", b"x")
    await provider.read_file(workspace, "out")
    await provider.execute(workspace, "true")
    await provider.read_files(workspace, ["out"], base_path="/workspace")

    _, setup, cwd, _ = client.started[0]
    assert (setup, cwd) == (
        shlex.join(
            [*e2b_module._MERGED, "sh", "-c", e2b_module._writable_roots(["/workspace", "/spool"])]
        ),
        "/",
    )
    assert "test -w /workspace && test -w /spool" in setup
    # create, write, read, execute, and the listing of read_files
    assert client.users == ["agent"] * 5


async def test_run_as_names_the_user_every_operation_runs_as(client: FakeE2BClient) -> None:
    provider = E2BWorkspaceProvider("tmpl-1", client=client, run_as="builder")
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    await provider.write_file(workspace, "out", b"x")

    assert client.users == ["builder", "builder"]


async def test_create_fails_and_kills_when_a_root_is_not_writable(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    client.responder = lambda command: (
        FakeRun(
            chunks=["mkdir: cannot create directory '/spool': Permission denied\n"],
            exit=CommandExit(end="exited", exit_code=1),
        )
        if "test -w" in command
        else FakeRun()
    )

    with pytest.raises(RuntimeError, match="not writable by user agent: mkdir: .*/spool"):
        await provider.create(WorkspaceConfig(provider="e2b"))
    assert client.sandboxes == {}


async def test_create_refuses_credential_bindings(provider: E2BWorkspaceProvider) -> None:
    binding = CredentialBinding(
        ref=CredentialRef(scope="host-env", name="GH_TOKEN"), target="GH_TOKEN", delivery="env"
    )
    with pytest.raises(CredentialPolicyError):
        await provider.create(WorkspaceConfig(provider="e2b", credentials=[binding]))


async def test_create_refuses_a_credential_added_to_the_environment_before_allocating(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    config = WorkspaceConfig(provider="e2b")
    config.environment["GH_TOKEN"] = "not-a-real-token"

    with pytest.raises(ValueError, match="credential"):
        await provider.create(config)

    assert client.events == []
    assert client.started == []


async def test_commands_refuse_a_credential_added_to_a_live_workspace(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b", environment={"BASE": "1"}))
    started = len(client.started)
    workspace.config.environment["GH_TOKEN"] = "not-a-real-token"

    with pytest.raises(ValueError, match="credential"):
        await provider.execute(workspace, "true")
    with pytest.raises(ValueError, match="credential"):
        await provider.start_stream(workspace, ["true"])

    assert len(client.started) == started
    assert all("not-a-real-token" not in env.values() for *_, env in client.started)


async def test_create_refuses_network_denial_before_allocating(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    config = WorkspaceConfig(provider="e2b", limits=ResourceLimits(network=False))

    with pytest.raises(ValueError, match="network"):
        await provider.create(config)

    assert client.events == []
    assert client.sandboxes == {}


async def test_stream_merges_in_the_sandbox_and_env_stays_out_of_the_argv(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b", environment={"BASE": "1"}))
    await provider.start_stream(workspace, ["echo", "a b"], env={"VALUE": "s3cr3t"}, cwd="sub")

    _, command, cwd, env = client.started[-1]
    assert shlex.split(command) == ["sh", "-c", 'exec "$@" 2>&1', "sh", "echo", "a b"]
    assert "s3cr3t" not in command
    assert env == {"BASE": "1", "VALUE": "s3cr3t"}
    assert cwd == "/workspace/sub"


async def test_lines_join_split_chunks_and_keep_a_final_partial_line(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    client.responder = lambda _c: FakeRun(
        chunks=["out-", "1\nerr-1\n", "tail"], exit=CommandExit(end="exited", exit_code=7)
    )
    session = await provider.start_stream(workspace, ["x"])

    assert [line async for line in session.lines()] == ["out-1", "err-1", "tail"]
    assert await session.outcome() == StreamOutcome(end="completed", exit_code=7)


async def test_timeout_kills_the_sandbox_process_and_reports_timed_out(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    client.responder = lambda _c: FakeRun(chunks=["started\n"], hang=True)
    session = await provider.start_stream(workspace, ["sleep", "60"], timeout=0.05)

    assert [line async for line in session.lines()] == ["started"]
    assert await session.outcome() == StreamOutcome(end="timed_out")
    assert client.commands[max(client.commands)].killed


async def test_a_lost_stream_is_disconnected_with_no_exit_code(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    client.responder = lambda _c: FakeRun(
        exit=CommandExit(end="disconnected", detail="gone"), hang=False
    )
    client.command_kill_works = False
    session = await provider.start_stream(workspace, ["x"], timeout=5)

    outcome = await session.outcome()

    assert outcome == StreamOutcome(end="disconnected", detail="gone")
    assert outcome.exit_code is None


async def test_reconnect_reattaches_to_the_same_pid(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    session = await provider.start_stream(workspace, ["x"])
    pid = max(client.commands)

    again = await session.reconnect()

    assert client.events[-1] == f"reconnect {workspace.id} {pid}"
    assert [line async for line in again.lines()] == ["again"]


async def test_read_files_matches_patterns_and_returns_exact_bytes(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    blob = bytes(range(256))
    client.sandboxes[workspace.id].files.update(
        {"/spool/a/one.jsonl": blob, "/spool/b.txt": b"skip"}
    )
    client.responder = lambda c: FakeRun(chunks=["./b.txt\0./a/one.jsonl\0"] if "find" in c else [])

    got = await provider.read_files(workspace, ["*.jsonl"], base_path="/spool")

    assert got == [("a/one.jsonl", blob)]
    assert client.started[-1][2] == "/spool"


async def test_file_paths_cannot_escape_the_workspace(provider: E2BWorkspaceProvider) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    for bad in ("/etc/passwd", "../etc/passwd", "a/../../x"):
        with pytest.raises(ValueError):
            await provider.read_file(workspace, bad)
    await provider.write_file(workspace, "notes/a.txt", "hi")
    assert await provider.read_file(workspace, "notes/a.txt") == "hi"


async def test_list_owned_and_attach_check_labels(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    mine = await provider.create(WorkspaceConfig(provider="e2b", labels={"syn.host_id": "h1"}))
    await provider.create(WorkspaceConfig(provider="e2b", labels={"syn.host_id": "h2"}))

    owned = await provider.list_owned({"syn.host_id": "h1"})
    assert [o.workspace_id for o in owned] == [mine.id]

    fresh = E2BWorkspaceProvider("tmpl-1", client=client)
    attached = await fresh.attach(mine.id, expect_labels={"syn.host_id": "h1"})
    assert attached.id == mine.id
    with pytest.raises(WorkspaceOwnershipError):
        await fresh.attach(mine.id, expect_labels={"syn.host_id": "h2"})
    with pytest.raises(WorkspaceNotFoundError):
        await fresh.attach("sbx-missing", expect_labels={})


async def test_attach_finds_a_sandbox_the_list_does_not_show_yet(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    client.unlisted_after_create = 1000  # E2B's list lags the create throughout
    mine = await provider.create(WorkspaceConfig(provider="e2b", labels={"syn.host_id": "h1"}))

    fresh = E2BWorkspaceProvider("tmpl-1", client=client)
    attached = await fresh.attach(mine.id, expect_labels={"syn.host_id": "h1"})
    assert attached.id == mine.id
    assert attached.config.labels == {"syn.host_id": "h1"}
    with pytest.raises(WorkspaceOwnershipError):
        await E2BWorkspaceProvider("tmpl-1", client=client).attach(
            mine.id, expect_labels={"syn.host_id": "h2"}
        )


async def test_teardown_runs_both_hooks_before_the_kill(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))

    async def while_running() -> None:
        client.events.append("while_running")

    async def before_delete() -> None:
        client.events.append("before_delete")

    report = await provider.teardown(
        workspace, while_running=while_running, before_delete=before_delete
    )

    assert client.events[-3:] == ["while_running", "before_delete", f"kill {workspace.id}"]
    assert report.finalization.state == "completed"
    assert workspace.id not in client.sandboxes


async def test_a_failed_archive_pauses_instead_of_killing_and_retries_by_ref(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))

    async def failing() -> None:
        raise OSError("upload refused")

    report = await provider.teardown(workspace, before_delete=failing)

    assert report.finalization.state == "retained"
    assert client.sandboxes[workspace.id].paused
    assert f"kill {workspace.id}" not in client.events
    ref = WorkspaceRef(provider="e2b", resource_id=workspace.id)
    assert report.finalization.retained == ref

    retry = await provider.teardown(ref)

    assert retry.finalization.state == "completed"
    assert workspace.id not in client.sandboxes


async def test_a_refused_pause_leaves_the_sandbox_running_and_never_kills_it(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    client.pause_refused = True

    async def failing() -> None:
        raise OSError("upload refused")

    report = await provider.teardown(workspace, before_delete=failing)

    assert report.finalization.state == "retained"
    assert "pause refused" in report.finalization.error.message
    assert workspace.id in client.sandboxes
    assert f"kill {workspace.id}" not in client.events


async def test_a_lost_stream_fails_execute_with_a_transport_error(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    client.responder = lambda _c: FakeRun(exit=CommandExit(end="disconnected", detail="reset"))

    with pytest.raises(E2BTransportError, match="reset"):
        await provider.execute(workspace, "true")


async def test_a_failed_while_running_hook_destroys_nothing(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))

    async def failing() -> None:
        raise RuntimeError("exporter failed")

    report = await provider.teardown(workspace, while_running=failing)

    assert report.finalization.state == "failed"
    assert workspace.id in client.sandboxes and not client.sandboxes[workspace.id].paused


async def test_teardown_waits_for_a_killed_sandbox_to_leave_the_list(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r3"}))
    client.listed_after_kill = 3  # E2B still lists it for three reads after the kill

    report = await provider.destroy(workspace)

    assert report.finalization.state == "completed"
    assert client.events.count(f"kill {workspace.id}") == 1, "the kill was retried"
    assert await provider.list_owned({"run": "r3"}) == []


async def test_teardown_of_a_sandbox_not_yet_listed_waits_to_see_it_leave(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    client.unlisted_after_create = 1
    client.listed_after_kill = 3
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r5"}))

    report = await provider.destroy(workspace)

    assert report.finalization.state == "completed"
    assert client.events.count(f"kill {workspace.id}") == 1
    assert await provider.list_owned({"run": "r5"}) == []


async def test_teardown_of_a_sandbox_e2b_no_longer_knows_completes_without_the_list(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r6"}))
    del client.sandboxes[workspace.id]  # expired or killed elsewhere

    report = await provider.destroy(workspace)

    assert report.finalization.state == "completed"
    assert client.events.count(f"kill {workspace.id}") == 1


@pytest.mark.usefixtures("short_removal_bound")
async def test_a_sandbox_never_listed_is_killed_by_id_and_its_removal_is_unconfirmed(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    client.unlisted_after_create = 1_000_000
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r7"}))

    with pytest.raises(SandboxRemovalError, match=workspace.id):
        await provider.destroy(workspace)
    assert client.events.count(f"kill {workspace.id}") == 1
    assert workspace.id not in client.sandboxes


@pytest.mark.parametrize("failing", ["list", "lookup"])
async def test_a_failed_read_before_the_kill_still_kills_by_id(
    provider: E2BWorkspaceProvider,
    client: FakeE2BClient,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    if failing == "lookup":
        client.unlisted_after_create = 1_000_000  # so the id is looked up
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r9"}))

    async def unavailable(*args: object) -> object:
        raise E2BTransportError(f"{failing} endpoint unavailable")

    monkeypatch.setattr(client, failing, unavailable)

    with pytest.raises(E2BTransportError, match=f"{failing} endpoint") as failure:
        await provider.destroy(workspace)
    assert client.events.count(f"kill {workspace.id}") == 1, "the read failure skipped the kill"
    assert workspace.id not in client.sandboxes
    notes = getattr(failure.value, "__notes__", [])
    assert any("removal is unconfirmed" in note for note in notes)


async def test_a_failed_kill_after_a_failed_read_keeps_both_errors(
    provider: E2BWorkspaceProvider, client: FakeE2BClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))

    async def unavailable(*args: object) -> object:
        raise E2BTransportError("list endpoint unavailable")

    async def refused(sandbox_id: str) -> bool:
        raise E2BTransportError("kill endpoint unavailable")

    monkeypatch.setattr(client, "list", unavailable)
    monkeypatch.setattr(client, "kill", refused)

    with pytest.raises(E2BTransportError, match="list endpoint") as failure:
        await provider.destroy(workspace)
    notes = getattr(failure.value, "__notes__", [])
    assert any("kill endpoint unavailable" in note for note in notes)
    assert workspace.id in client.sandboxes


async def test_a_failed_create_still_kills_when_the_list_fails(
    provider: E2BWorkspaceProvider, client: FakeE2BClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.responder = lambda command: FakeRun(exit=CommandExit(end="exited", exit_code=1))

    async def unavailable(*args: object) -> object:
        raise E2BTransportError("list endpoint unavailable")

    monkeypatch.setattr(client, "list", unavailable)

    with pytest.raises(SandboxRemovalError, match="still allocated.*list endpoint"):
        await provider.create(WorkspaceConfig(provider="e2b"))
    assert client.events.count("kill sbx-1") == 1, "the failed create skipped the kill"
    assert client.sandboxes == {}


@pytest.mark.parametrize("list_reply", ["never", "late"])
@pytest.mark.usefixtures("short_removal_bound")
async def test_the_removal_bound_covers_the_list_requests_themselves(
    provider: E2BWorkspaceProvider,
    client: FakeE2BClient,
    monkeypatch: pytest.MonkeyPatch,
    list_reply: str,
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r4"}))

    async def slow_list(labels: Mapping[str, str]) -> list[object]:
        if list_reply == "never":
            await asyncio.Event().wait()
        await asyncio.sleep(0.3)  # an empty page, but six times past the bound
        return []

    monkeypatch.setattr(client, "list", slow_list)

    with pytest.raises(SandboxRemovalError, match=workspace.id):
        await asyncio.wait_for(provider.destroy(workspace), 0.2)
    assert client.events.count(f"kill {workspace.id}") == 1, "the kill was retried"


async def test_cancelling_a_removal_wait_is_not_reported_as_a_removal_failure(
    provider: E2BWorkspaceProvider, client: FakeE2BClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b"))
    listing = asyncio.Event()

    async def stalled_list(labels: Mapping[str, str]) -> list[object]:
        listing.set()
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(client, "list", stalled_list)
    removing = asyncio.create_task(provider.destroy(workspace))
    await asyncio.wait_for(listing.wait(), 1)
    removing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(removing, 1)


class _FailsOnce(FakeE2BClient):
    """A fake whose first call to `failing` raises a transport error."""

    def __init__(self, failing: str) -> None:
        super().__init__()
        self._failing = failing

    async def kill(self, sandbox_id: str) -> bool:
        if self._failing == "kill":
            self._failing = ""
            raise E2BTransportError("connection reset during kill")
        return await super().kill(sandbox_id)

    async def list(self, metadata: Mapping[str, str]) -> list[object]:
        if self._failing == "list":
            self._failing = ""
            raise E2BTransportError("connection reset during list")
        return await super().list(metadata)


@pytest.mark.parametrize("failing", ["kill", "list"])
async def test_the_live_removal_measurement_frees_its_sandbox_when_it_fails(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    from tests.integration import test_e2b_live as live

    client = _FailsOnce(failing)
    client.listed_after_kill = 2
    monkeypatch.setattr(live, "SdkE2BClient", lambda: client)

    with pytest.raises(E2BTransportError, match=f"during {failing}"):
        await live.test_a_killed_sandbox_leaves_the_list_within_the_removal_bound("tmpl-1")
    assert client.sandboxes == {} and await client.list({}) == [], "the measurement leaked"


@pytest.mark.usefixtures("short_removal_bound")
async def test_the_live_removal_measurement_names_a_sandbox_it_could_not_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration import test_e2b_live as live

    client = _FailsOnce("list")
    client.kill_is_noop = True
    monkeypatch.setattr(live, "SdkE2BClient", lambda: client)

    with pytest.raises(E2BTransportError) as failure:
        await live.test_a_killed_sandbox_leaves_the_list_within_the_removal_bound("tmpl-1")
    assert any("sbx-1" in note for note in failure.value.__notes__)


async def test_the_live_removal_measurement_passes_and_frees_on_a_lagging_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration import test_e2b_live as live

    client = FakeE2BClient()
    client.listed_after_kill = 2
    monkeypatch.setattr(live, "SdkE2BClient", lambda: client)

    await live.test_a_killed_sandbox_leaves_the_list_within_the_removal_bound("tmpl-1")
    assert client.events.count("kill sbx-1") == 1 and await client.list({}) == []


@pytest.mark.usefixtures("short_removal_bound")
async def test_negative_control_a_noop_kill_fails_cleanup(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r1"}))
    client.kill_is_noop = True

    with pytest.raises(SandboxRemovalError):
        await provider.destroy(workspace)
    assert await provider.list_owned({"run": "r1"})


@pytest.mark.usefixtures("short_removal_bound")
async def test_negative_control_a_noop_kill_fails_cleanup_while_the_list_lags_create(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    client.unlisted_after_create = 3
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r8"}))
    client.kill_is_noop = True

    with pytest.raises(SandboxRemovalError):
        await provider.destroy(workspace)
    assert await provider.list_owned({"run": "r8"})


@pytest.mark.usefixtures("short_removal_bound")
async def test_the_live_negative_control_holds_while_the_list_lags_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration import test_e2b_live as live

    client = FakeE2BClient()
    client.unlisted_after_create = 3
    monkeypatch.setattr(live, "SdkE2BClient", lambda: client)

    await live.test_negative_control_noop_kill_leaves_a_sandbox_and_fails_cleanup("tmpl-1")
    assert client.sandboxes == {}, "the negative control leaked its sandbox"


@pytest.mark.usefixtures("short_removal_bound")
async def test_the_live_wait_for_listing_fails_when_the_sandbox_never_appears() -> None:
    from tests.integration import test_e2b_live as live

    client = FakeE2BClient()
    client.unlisted_after_create = 1_000_000
    run = {"run": "r9"}
    sandbox_id = await client.create(template="tmpl-1", metadata=run, lifetime_seconds=60)

    with pytest.raises(pytest.fail.Exception, match="not listed"):
        await live._await_listed(client, run, sandbox_id)  # type: ignore[arg-type]


def test_template_spec_records_a_pinned_verified_sized_build(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_bytes(b"name: claude-cli\n")
    spec = E2BTemplateSpec(
        name="claude-cli",
        image=f"ghcr.io/org/claude-cli@{OCI}",
        verified_oci_digest=OCI,
        expected_manifest_hash=manifest_hash(manifest),
        cpu_count=2,
        memory_mb=4096,
    )
    assert spec.expected_manifest_hash == (
        "sha256:" + __import__("hashlib").sha256(b"name: claude-cli\n").hexdigest()
    )


@pytest.mark.parametrize(
    ("image", "verified", "manifest", "cpu", "mem"),
    [
        ("ghcr.io/org/img:latest", OCI, MANIFEST, 2, 4096),
        (f"ghcr.io/org/img@{OCI}", "sha256:" + "c" * 64, MANIFEST, 2, 4096),
        (f"ghcr.io/org/img@{OCI}", OCI, OCI, 2, 4096),
        (f"ghcr.io/org/img@{OCI}", OCI, MANIFEST, 0, 4096),
        (f"ghcr.io/org/img@{OCI}", OCI, MANIFEST, 2, 0),
    ],
)
def test_template_spec_refuses_what_would_not_be_pinned_verified_and_sized(
    image: str, verified: str, manifest: str, cpu: int, mem: int
) -> None:
    with pytest.raises(TemplateSpecError):
        E2BTemplateSpec("t", image, verified, manifest, cpu, mem)
