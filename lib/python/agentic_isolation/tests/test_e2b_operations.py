"""E2B-only `SupportsOperations` checks (#95); the shared ones are in the conformance suite.

These run over `FakeE2BClient` in its executing mode, so what they observe is
what the provider sent through the `E2BSandboxClient` port.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import (
    CredentialBinding,
    CredentialRef,
    LaunchMarker,
    OperationSpec,
)
from agentic_isolation.providers.base import run_to_completion
from agentic_isolation.providers.e2b import E2BWorkspaceProvider
from agentic_isolation.providers.operation_spool import OperationFailure
from tests.e2b_fake import FakeE2BClient

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
