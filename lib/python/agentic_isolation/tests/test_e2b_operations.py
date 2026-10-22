"""E2B-only `SupportsOperations` checks (#95); the shared ones are in the conformance suite.

These run over `FakeE2BClient` in its executing mode, so what they observe is
what the provider sent through the `E2BSandboxClient` port.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

import pytest

from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import (
    CredentialBinding,
    CredentialRef,
    LaunchMarker,
    OperationSpec,
)
from agentic_isolation.providers import e2b_operations
from agentic_isolation.providers.base import run_to_completion
from agentic_isolation.providers.e2b import E2BWorkspaceProvider
from agentic_isolation.providers.e2b_client import CommandExit
from agentic_isolation.providers.operation_spool import OperationFailure
from tests.e2b_fake import FakeE2BClient, FakeRun

SECRET = "s3cr3t-e2b-5d2"


class _Vault:
    async def resolve(self, ref: CredentialRef) -> str:
        return SECRET


def _spec(script: str, **overrides: object) -> OperationSpec:
    fields: dict[str, object] = {
        "execution_id": "exec-1",
        "operation_key": "k1",
        "argv": ("sh", "-c", script),
        "working_directory": ".",
        "environment": {},
        "credential_bindings": (),
        "timeout_seconds": 60,
        "metadata": {},
    }
    fields.update(overrides)
    return OperationSpec.model_validate(fields)


async def test_a_credential_binding_reaches_the_workload_env_scrubbed_and_never_argv(
    tmp_path: Path,
) -> None:
    client = FakeE2BClient(executing_root=tmp_path)
    provider = E2BWorkspaceProvider("t", client=client, credentials=_Vault())
    workspace = await provider.create(WorkspaceConfig())
    spec = _spec(
        'printf %s "$TOKEN" > token; echo "token=$TOKEN"',
        credential_bindings=(
            CredentialBinding(
                ref=CredentialRef(name="t", scope="repo"), delivery="env", target="TOKEN"
            ),
        ),
    )
    async with asyncio.timeout(30):
        status, output = await run_to_completion(provider, workspace, spec)
    assert status.state == "succeeded"
    assert (tmp_path / "token").read_text() == SECRET
    assert output == b"token=[REDACTED]\n"
    _, command, _, env = client.started[-1]
    assert env["TOKEN"] == SECRET
    assert SECRET not in command


async def test_the_launch_marker_travels_on_stdin_not_the_command_line(tmp_path: Path) -> None:
    client = FakeE2BClient(executing_root=tmp_path)
    provider = E2BWorkspaceProvider("t", client=client)
    workspace = await provider.create(WorkspaceConfig())
    marker = LaunchMarker(argv0="aw-launch-e2b", line="AW_LAUNCH nonce-e2b")
    async with asyncio.timeout(30):
        _, output = await run_to_completion(
            provider, workspace, _spec("echo work", launch_marker=marker)
        )
    assert output == b"AW_LAUNCH nonce-e2b\nwork\n"
    assert client.stdins[-1] == b"aw-launch-e2b\nAW_LAUNCH nonce-e2b\n"
    assert "nonce-e2b" not in client.started[-1][1]


async def test_teardown_forgets_the_workspaces_operations(tmp_path: Path) -> None:
    provider = E2BWorkspaceProvider("t", client=FakeE2BClient(executing_root=tmp_path))
    workspace = await provider.create(WorkspaceConfig())
    async with asyncio.timeout(30):
        ref = await provider.start(workspace, _spec("true"))
        await run_to_completion(provider, workspace, _spec("true", operation_key="k2"))
        await provider.destroy(workspace)
    with pytest.raises(OperationFailure) as caught:
        await provider.status(ref)
    assert caught.value.error.kind == "unknown_operation"


async def test_the_executing_double_keeps_sandbox_roots_off_the_host(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    client = FakeE2BClient(executing_root=root)
    provider = E2BWorkspaceProvider("t", client=client)
    workspace = await provider.create(WorkspaceConfig())
    result = await provider.execute(
        workspace, "touch /spool/probe /workspace/probe && echo /workspaces", cwd="/"
    )
    assert result.success, result.stdout
    assert (client.spool_root / "probe").is_file()
    assert (root / "probe").is_file()
    assert result.stdout.strip() == "/workspaces"


def _alive(pid: int) -> bool:
    try:
        return "\nState:\tZ" not in Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return False


async def test_cancel_stops_a_descendant_that_left_the_group(tmp_path: Path) -> None:
    provider = E2BWorkspaceProvider("t", client=FakeE2BClient(executing_root=tmp_path))
    workspace = await provider.create(WorkspaceConfig())
    script = (
        "setsid sh -c 'echo $$ > escaped.pid; exec sleep 300' >/dev/null 2>&1 & "
        "while :; do sleep 1; done"
    )
    escaped: int | None = None
    try:
        async with asyncio.timeout(30):
            ref = await provider.start(workspace, _spec(script))
            while not (tmp_path / "escaped.pid").exists():
                await asyncio.sleep(0.01)
            escaped = int((tmp_path / "escaped.pid").read_text())
            assert _alive(escaped)
            status = await provider.cancel(ref, grace_seconds=0.1)
        assert status.state == "cancelled"
        assert "cancel_escalation" not in status.metadata
        assert not _alive(escaped)
    finally:
        if escaped is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(escaped, signal.SIGKILL)


def _unkillable(command: str) -> FakeRun:
    if "aw-terminate" in command:
        return FakeRun(chunks=["survivors 4321\n"], exit=CommandExit(end="exited", exit_code=1))
    if "aw-operation" in command:
        return FakeRun(chunks=["4321\n"], hang=True)
    return FakeRun()


async def test_an_unconfirmed_termination_escalates_to_killing_the_sandbox() -> None:
    client = FakeE2BClient(responder=_unkillable)
    provider = E2BWorkspaceProvider("t", client=client)
    workspace = await provider.create(WorkspaceConfig())
    ref = await provider.start(workspace, _spec("ignored"))
    async with asyncio.timeout(30):
        status = await provider.cancel(ref, grace_seconds=0)
    assert f"kill {workspace.id}" in client.events
    assert status.state == "cancelled"
    assert status.metadata["cancel_escalation"] == "sandbox_killed"
    assert "survivors 4321" in status.metadata["cancel_signal_failure"]


async def test_a_sandbox_that_survives_its_kill_fails_the_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(e2b_operations, "_CONTROL_SECONDS", 0.3)
    client = FakeE2BClient(responder=_unkillable)
    provider = E2BWorkspaceProvider("t", client=client)
    workspace = await provider.create(WorkspaceConfig())
    ref = await provider.start(workspace, _spec("ignored"))
    client.kill_is_noop = True
    async with asyncio.timeout(30):
        with pytest.raises(OperationFailure) as raised:
            await provider.cancel(ref, grace_seconds=0)
    assert raised.value.error.kind == "provider_unavailable"
    assert raised.value.error.ref == ref
    assert f"kill {workspace.id}" in client.events
    status = await provider.status(ref)
    assert status.metadata["cancel_escalation"] == "sandbox_kill_failed"
    assert status.state not in ("cancelled", "succeeded")
