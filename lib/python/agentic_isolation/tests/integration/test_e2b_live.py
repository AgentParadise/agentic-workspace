"""Live E2B proofs for syntropic137/syntropic137#1915 (Q4, Q5, negative control, pause).

Every test here needs a real E2B account: E2B_API_KEY, plus E2B_TEMPLATE (a
template built by `build_template` from the pinned workspace image, which
must carry `codex` and its manifest). The private-build test also needs
E2B_PRIVATE_IMAGE (`ghcr.io/...@sha256:<digest>`), E2B_GHCR_USERNAME,
E2B_GHCR_TOKEN and E2B_EXPECTED_MANIFEST_HASH; E2B_MANIFEST_PATH names where
the image carries manifest.yaml (default /opt/agentic/manifest.yaml; the
test fails if it is absent, it does not assume it). E2B_BUILD_RECEIPT, when
set, is where the build receipt is written as JSON.

Without those inputs each test is reported NOT RUN by
tests/integration/conftest.py, never as a pass, and REQUIRE_E2B_LIVE=1 turns
every NOT RUN into a failure. None of these has been run yet: no E2B key
existed when they were written (owner decision).

Run: REQUIRE_E2B_LIVE=1 uv run --locked --all-extras pytest -rA -k e2b \
     tests/integration/test_remote_capabilities_conformance.py tests/integration/test_e2b_live.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agentic_isolation import E2BWorkspaceProvider, Workspace, WorkspaceConfig
from agentic_isolation.providers import E2BTemplateSpec, SdkE2BClient, build_template
from agentic_isolation.providers.e2b import SandboxRemovalError

pytestmark = [pytest.mark.e2b_live, pytest.mark.integration]


def _required(*names: str) -> list[str]:
    """The values of `names`; NOT RUN without them, a failure when live is required."""
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        if os.environ.get("REQUIRE_E2B_LIVE") == "1":
            pytest.fail(f"REQUIRE_E2B_LIVE=1 but {', '.join(missing)} unset")
        pytest.skip(f"NOT RUN: {', '.join(missing)} required")
    return [os.environ[name] for name in names]


@pytest.fixture
def template() -> str:
    return _required("E2B_API_KEY", "E2B_TEMPLATE")[1]


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


# The same policy the Docker codex test uses (test_codex_sandbox_seccomp.py).
_CODEX = ["codex", "sandbox", "-c", 'sandbox_mode="workspace-write"', "--"]


async def test_q4_codex_sandbox_allows_the_workspace_and_denies_outside_it(
    live: tuple[E2BWorkspaceProvider, Workspace],
) -> None:
    provider, workspace = live
    inside = await provider.execute(
        workspace, shlex.join([*_CODEX, "sh", "-c", "echo kept > /workspace/q4-inside"])
    )
    assert inside.success, inside.stdout
    assert await provider.read_file(workspace, "q4-inside") == "kept\n"

    outside = await provider.execute(workspace, shlex.join([*_CODEX, "touch", "/opt/q4-outside"]))
    assert not outside.success, "codex sandbox allowed a write outside the workspace"
    assert not (await provider.execute(workspace, "test -e /opt/q4-outside")).success


def _mount_field(mount_point: str, field: int) -> str:
    # /proc/self/mountinfo: field 5 is the mount point, 6 its options, and
    # the filesystem type follows the " - " separator.
    if field == 6:
        return f"awk '$5 == \"{mount_point}\" {{print $6}}' /proc/self/mountinfo | tail -n1"
    return (
        f'awk \'$5 == "{mount_point}" {{for (i = 7; $i != "-"; i++); print $(i + 1)}}\' '
        "/proc/self/mountinfo | tail -n1"
    )


async def test_q5_root_is_read_only_and_workspace_tmpfs_is_writable(
    live: tuple[E2BWorkspaceProvider, Workspace],
) -> None:
    provider, workspace = live
    options = await provider.execute(workspace, _mount_field("/", 6), cwd="/")
    assert "ro" in options.stdout.strip().split(","), f"root mount options: {options.stdout}"
    # EROFS positively, not any failure: EACCES would mean permissions, not a ro root.
    root = await provider.execute(workspace, "touch /e2b-ro-probe", cwd="/")
    assert not root.success and "Read-only file system" in root.stdout, root.stdout

    for writable in ("/tmp", "/workspace"):
        fstype = await provider.execute(workspace, _mount_field(writable, 7), cwd="/")
        assert fstype.stdout.strip() == "tmpfs", f"{writable} is {fstype.stdout!r}"
        probe = await provider.execute(workspace, f"touch {writable}/q5-probe && echo ok", cwd="/")
        assert probe.success and "ok" in probe.stdout, probe.stdout


async def test_q5_private_ghcr_digest_builds_and_launches(tmp_path: Path) -> None:
    api_key, image, username, token, expected = _required(
        "E2B_API_KEY",
        "E2B_PRIVATE_IMAGE",
        "E2B_GHCR_USERNAME",
        "E2B_GHCR_TOKEN",
        "E2B_EXPECTED_MANIFEST_HASH",
    )
    manifest = os.environ.get("E2B_MANIFEST_PATH", "/opt/agentic/manifest.yaml")
    spec = E2BTemplateSpec(
        name=f"agentic-q5-{uuid.uuid4().hex[:8]}",
        image=image,
        verified_oci_digest=image.rpartition("@")[2],
        expected_manifest_hash=expected,
        cpu_count=2,
        memory_mb=2048,
    )
    receipt = await build_template(
        spec, registry_username=username, registry_password=token, api_key=api_key
    )
    receipt_path = Path(os.environ.get("E2B_BUILD_RECEIPT", tmp_path / "receipt.json"))
    receipt_path.write_text(json.dumps(dataclasses.asdict(receipt), indent=2))
    print(f"build receipt: {receipt_path}")

    provider = E2BWorkspaceProvider(receipt.template_id)
    run = {"e2b_live.run": uuid.uuid4().hex}
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels=run))
    try:
        hashed = await provider.execute(workspace, f"sha256sum {shlex.quote(manifest)}", cwd="/")
        assert hashed.success, f"the built template has no {manifest}: {hashed.stdout}"
        assert "sha256:" + hashed.stdout.split()[0] == expected
    finally:
        await provider.destroy(workspace)
        assert await provider.list_owned(run) == []


async def _state(sandbox_id: str) -> str:
    from e2b import AsyncSandbox

    return str((await AsyncSandbox.get_info(sandbox_id)).state)


async def test_pause_and_resume_keep_the_spool(template: str) -> None:
    provider = E2BWorkspaceProvider(template)
    run = {"e2b_live.run": uuid.uuid4().hex}
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels=run))
    try:
        spooled = await provider.execute(workspace, "mkdir -p /spool && printf kept > /spool/probe")
        assert spooled.success, spooled.stdout
        assert await _state(workspace.id) == "running"

        async def fail_archive() -> None:
            raise OSError("simulated archive failure")

        report = await provider.teardown(workspace, before_delete=fail_archive)
        assert report.finalization.state == "retained"
        # A refused pause also reports retained; only the SDK state tells them apart.
        assert await _state(workspace.id) == "paused"

        got = await provider.read_files(workspace, ["probe"], base_path="/spool")
        assert await _state(workspace.id) == "running"
        assert got == [("probe", b"kept")]
    finally:
        await provider.destroy(workspace)
        assert await provider.list_owned(run) == []
