"""Live E2B proofs for syntropic137/syntropic137#1915 (Q4, Q5, negative control, pause).

Every test here needs a real E2B account: E2B_API_KEY, plus E2B_TEMPLATE (a
template built by `build_template` from the pinned workspace image). Without
them each test is reported NOT RUN by tests/integration/conftest.py, never as
a pass, and REQUIRE_E2B_LIVE=1 turns NOT RUN into a failure. None of these has
been run yet: no E2B key existed when they were written (owner decision).

Run: E2B_API_KEY=... E2B_TEMPLATE=... REQUIRE_E2B_LIVE=1 \\
     uv run --extra dev --extra e2b pytest -m e2b_live tests/integration -rA
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

from agentic_isolation import E2BWorkspaceProvider, Workspace, WorkspaceConfig
from agentic_isolation.providers import SdkE2BClient
from agentic_isolation.providers.e2b import SandboxRemovalError

pytestmark = [pytest.mark.e2b_live, pytest.mark.integration]


@pytest.fixture
def template() -> str:
    if not (os.environ.get("E2B_API_KEY") and os.environ.get("E2B_TEMPLATE")):
        if os.environ.get("REQUIRE_E2B_LIVE") == "1":
            pytest.fail("REQUIRE_E2B_LIVE=1 but E2B_API_KEY or E2B_TEMPLATE is unset")
        pytest.skip("NOT RUN: E2B_API_KEY and E2B_TEMPLATE are required")
    return os.environ["E2B_TEMPLATE"]


@pytest.fixture
async def live(template: str) -> AsyncIterator[tuple[E2BWorkspaceProvider, Workspace]]:
    provider = E2BWorkspaceProvider(template)
    run = {"e2b_live.run": uuid.uuid4().hex}
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels=run))
    try:
        yield provider, workspace
    finally:
        await provider.destroy(workspace)
        assert await provider.list_owned(run) == []


class _NoopKillClient(SdkE2BClient):
    async def kill(self, sandbox_id: str) -> bool:
        return True


async def test_negative_control_noop_kill_leaves_a_sandbox_and_fails_cleanup(
    template: str,
) -> None:
    run = {"e2b_live.run": uuid.uuid4().hex}
    broken = E2BWorkspaceProvider(template, client=_NoopKillClient())
    workspace = await broken.create(WorkspaceConfig(provider="e2b", labels=run))
    try:
        with pytest.raises(SandboxRemovalError):
            await broken.destroy(workspace)
        assert [o.workspace_id for o in await broken.list_owned(run)] == [workspace.id]
    finally:
        await E2BWorkspaceProvider(template).destroy(workspace)


async def test_q4_codex_bwrap_sandbox_runs_inside_the_microvm(
    live: tuple[E2BWorkspaceProvider, Workspace],
) -> None:
    provider, workspace = live
    result = await provider.execute(
        workspace, "bwrap --ro-bind / / --dev /dev --proc /proc --unshare-all true && echo bwrap-ok"
    )
    assert result.success, result.stdout
    assert "bwrap-ok" in result.stdout


async def test_q5_root_is_read_only_and_workspace_tmpfs_is_writable(
    live: tuple[E2BWorkspaceProvider, Workspace],
) -> None:
    provider, workspace = live
    root = await provider.execute(workspace, "touch /e2b-ro-probe", cwd="/")
    assert not root.success, "the sandbox root accepted a write"
    work = await provider.execute(workspace, "touch probe && echo ok")
    assert work.success and "ok" in work.stdout


async def test_q5_template_came_from_the_private_ghcr_digest(
    live: tuple[E2BWorkspaceProvider, Workspace],
) -> None:
    expected = os.environ.get("E2B_EXPECTED_MANIFEST_HASH")
    if not expected:
        pytest.skip("NOT RUN: E2B_EXPECTED_MANIFEST_HASH is required")
    provider, workspace = live
    result = await provider.execute(workspace, "sha256sum /opt/agentic/manifest.yaml", cwd="/")
    assert result.success
    assert result.stdout.split()[0] == expected.removeprefix("sha256:")


async def test_pause_and_resume_keep_the_spool(template: str) -> None:
    provider = E2BWorkspaceProvider(template)
    run = {"e2b_live.run": uuid.uuid4().hex}
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels=run))
    try:
        await provider.execute(workspace, "mkdir -p /spool && printf kept > /spool/probe")

        async def fail_archive() -> None:
            raise OSError("simulated archive failure")

        report = await provider.teardown(workspace, before_delete=fail_archive)
        assert report.finalization.state == "retained"
        got = await provider.read_files(workspace, ["probe"], base_path="/spool")
        assert got == [("probe", b"kept")]
    finally:
        await provider.destroy(workspace)
        assert await provider.list_owned(run) == []
