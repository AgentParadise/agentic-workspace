"""Live E2B proofs for syntropic137/syntropic137#1915 (Q4, Q5, negative control, pause).

Every test here needs a real E2B account: E2B_API_KEY, plus E2B_TEMPLATE (a
template built by `build_template` from the pinned workspace image, which
must carry `codex` and its manifest). The private-build test also needs
E2B_PRIVATE_IMAGE (`ghcr.io/...@sha256:<digest>`), E2B_GHCR_USERNAME,
E2B_GHCR_TOKEN and E2B_EXPECTED_MANIFEST_HASH; E2B_MANIFEST_PATH names where
the image carries its version manifest (default /opt/agentic/version.json, the
path the platform reads; the test fails if it is absent, it does not assume
it). E2B_BUILD_RECEIPT, when
set, is where the build receipt is written as JSON.

Without those inputs each test is reported NOT RUN by
tests/integration/conftest.py, never as a pass, and REQUIRE_E2B_LIVE=1 turns
every NOT RUN into a failure. None of these has been run yet: no E2B key
existed when they were written (owner decision).

Run: REQUIRE_E2B_LIVE=1 uv run --locked --all-extras pytest -rA -k e2b \
     tests/integration/test_remote_capabilities_conformance.py tests/integration/test_e2b_live.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agentic_isolation import E2BWorkspaceProvider, Workspace, WorkspaceConfig
from agentic_isolation.providers import E2BTemplateSpec, SdkE2BClient, build_template
from agentic_isolation.providers import e2b as e2b_module
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


class _NoopKill:
    """The SDK client with a kill that claims success and does nothing."""

    def __init__(self, client: SdkE2BClient) -> None:
        self._client = client

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)

    async def kill(self, sandbox_id: str) -> bool:
        return True


async def _await_listed(client: SdkE2BClient, run: dict[str, str], sandbox_id: str) -> float:
    """Seconds until `sandbox_id` is in the E2B list; fails past the removal bound.

    E2B's list can omit a sandbox for a moment after its create, and absence
    from it proves a removal only for a sandbox it has shown.
    """
    started = time.monotonic()
    reads = 0
    try:
        async with asyncio.timeout(e2b_module._REMOVAL_SECONDS):
            while not any(entry.sandbox_id == sandbox_id for entry in await client.list(run)):
                reads += 1
                await asyncio.sleep(e2b_module._REMOVAL_POLL_SECONDS)
    except TimeoutError:
        pytest.fail(
            f"{sandbox_id} not listed {e2b_module._REMOVAL_SECONDS:g}s after create "
            f"(missing on {reads} read(s))"
        )
    elapsed = time.monotonic() - started
    print(f"{sandbox_id} was missing from the list on {reads} read(s), {elapsed:.2f}s after create")
    return elapsed


async def test_negative_control_noop_kill_leaves_a_sandbox_and_fails_cleanup(
    template: str,
) -> None:
    run = {"e2b_live.run": uuid.uuid4().hex}
    client = SdkE2BClient()
    broken = E2BWorkspaceProvider(template, client=_NoopKill(client))  # type: ignore[arg-type]
    workspace = await broken.create(WorkspaceConfig(provider="e2b", labels=run))
    try:
        # Without this, a sandbox not yet listed would pass as removed.
        await _await_listed(client, run, workspace.id)
        with pytest.raises(SandboxRemovalError):
            await broken.destroy(workspace)
        assert [o.workspace_id for o in await broken.list_owned(run)] == [workspace.id]
    finally:
        await E2BWorkspaceProvider(template, client=client).destroy(workspace)


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


async def test_q5_e2b_root_is_writable_so_a_read_only_root_is_not_an_e2b_property(
    live: tuple[E2BWorkspaceProvider, Workspace],
) -> None:
    """Q5's answer for E2B: the root filesystem is NOT read-only.

    The first live run (PR #87, 2026-10-17) measured `/` as `rw,relatime`,
    and the provider has no way to remount it. The Docker provider's
    read-only root is therefore not a property of E2B workspaces; this test
    asserts the measured answer so that a change in E2B fails here and sends
    the research doc back for review, instead of the property being assumed.
    """
    provider, workspace = live
    options = await provider.execute(workspace, _mount_field("/", 6), cwd="/")
    assert "rw" in options.stdout.strip().split(","), (
        f"E2B now mounts / as {options.stdout.strip()!r}: Q5's answer changed, revisit it"
    )


async def test_q5_workspace_and_spool_are_writable_by_the_command_user(
    live: tuple[E2BWorkspaceProvider, Workspace],
) -> None:
    """The roots are the image's directories on the sandbox disk, not tmpfs mounts."""
    provider, workspace = live
    user = await provider.execute(workspace, "id -un", cwd="/")
    assert user.stdout.strip() == "agent", user.stdout
    for writable in ("/tmp", "/workspace", "/spool"):
        probe = await provider.execute(workspace, f"touch {writable}/q5-probe && echo ok", cwd="/")
        assert probe.success and "ok" in probe.stdout, probe.stdout


async def test_a_killed_sandbox_leaves_the_list_within_the_removal_bound(template: str) -> None:
    """Measures the teardown cause of PR #87: how long E2B lists a killed sandbox.

    The sandbox is allocated outside the cleanup fixture, so any failure after
    the allocation removes it through the provider's own kill-and-confirm path
    before the failure propagates, and names it if that cannot be confirmed.
    """
    client = SdkE2BClient()
    run = {"e2b_live.run": uuid.uuid4().hex}
    sandbox_id = await client.create(template=template, metadata=run, lifetime_seconds=300)
    try:
        # Absence after the kill means nothing for a sandbox the list never showed.
        await _await_listed(client, run, sandbox_id)
        killed = await client.kill(sandbox_id)
        started = time.monotonic()
        reads_listed = 0
        try:
            async with asyncio.timeout(e2b_module._REMOVAL_SECONDS):
                while any(entry.sandbox_id == sandbox_id for entry in await client.list(run)):
                    reads_listed += 1
                    await asyncio.sleep(e2b_module._REMOVAL_POLL_SECONDS)
        except TimeoutError:
            pytest.fail(
                f"{sandbox_id} not seen gone {e2b_module._REMOVAL_SECONDS:g}s after kill "
                f"(listed on {reads_listed} read(s))"
            )
        print(
            f"kill returned {killed}; {sandbox_id} was still listed on {reads_listed} read(s), "
            f"for {time.monotonic() - started:.2f}s after the kill"
        )
    except BaseException as failure:
        try:
            await E2BWorkspaceProvider(template, client=client)._remove(sandbox_id, run)
        except Exception as leak:
            failure.add_note(f"cleanup of {sandbox_id} was not confirmed: {leak!r}")
        raise
    assert killed


async def test_q5_private_ghcr_digest_builds_and_launches(tmp_path: Path) -> None:
    api_key, image, username, token, expected = _required(
        "E2B_API_KEY",
        "E2B_PRIVATE_IMAGE",
        "E2B_GHCR_USERNAME",
        "E2B_GHCR_TOKEN",
        "E2B_EXPECTED_MANIFEST_HASH",
    )
    manifest = os.environ.get("E2B_MANIFEST_PATH", "/opt/agentic/version.json")
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
