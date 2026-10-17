"""E2BWorkspaceProvider against an in-memory client (syntropic137/syntropic137#1915).

These prove the provider's own decisions: what it sends to the sandbox port,
how it reads the answers, and the teardown order. They prove nothing about
E2B; tests/integration/test_e2b_live.py does that, and is NOT RUN without a key.
"""

from __future__ import annotations

import shlex
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
from tests.e2b_fake import FakeE2BClient, FakeRun

OCI = "sha256:" + "a" * 64
MANIFEST = "sha256:" + "b" * 64


@pytest.fixture
def client() -> FakeE2BClient:
    return FakeE2BClient()


@pytest.fixture
def provider(client: FakeE2BClient) -> E2BWorkspaceProvider:
    return E2BWorkspaceProvider("tmpl-1", client=client)


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


async def test_negative_control_a_noop_kill_fails_cleanup(
    provider: E2BWorkspaceProvider, client: FakeE2BClient
) -> None:
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels={"run": "r1"}))
    client.kill_is_noop = True

    with pytest.raises(SandboxRemovalError):
        await provider.destroy(workspace)
    assert await provider.list_owned({"run": "r1"})


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
