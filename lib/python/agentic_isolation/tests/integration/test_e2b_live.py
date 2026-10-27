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

The nested-docker test (#91) needs E2B_API_KEY and E2B_DOCKER_MANIFEST_HASH,
plus either E2B_DOCKER_BASE_IMAGE (it builds an `aw91-docker-test-*` template
and leaves it in place) or E2B_DOCKER_TEMPLATE (one built that way). The key
lives with the owner only: CI never has one, so CI reports this NOT RUN.

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
        # Bounded: list_owned waits out a list still showing what destroy removed.
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

    E2B's list can omit a sandbox for a moment after its create; this measures it.
    """
    started = time.monotonic()
    reads = 0
    try:
        async with asyncio.timeout(e2b_module._REMOVAL_SECONDS):
            while not any(entry.sandbox_id == sandbox_id for entry in await client.list(run)):
                reads += 1
                await asyncio.sleep(e2b_module._POLL_SECONDS)
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
        with pytest.raises(SandboxRemovalError):
            await broken.destroy(workspace)
        assert await client.lookup(workspace.id) is not None
        # Bounded: list_owned waits for a sandbox it holds to be listed.
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


async def test_a_killed_sandbox_is_gone_by_id_within_the_removal_bound(template: str) -> None:
    """Measures the teardown cause of PR #87: how long E2B knows and lists a killed sandbox.

    Removal is confirmed by id (`get_info` not found). The list is then read
    `_LIST_SAMPLES` times and each read printed, because a list that is not
    monotonic shows up only across reads. The sandbox is allocated outside the
    cleanup fixture, so any failure after the allocation removes it through the
    provider's own kill-and-confirm path, and names it if that cannot be confirmed.
    """
    client = SdkE2BClient()
    run = {"e2b_live.run": uuid.uuid4().hex}
    sandbox_id = await client.create(template=template, metadata=run, lifetime_seconds=300)
    try:
        await _await_listed(client, run, sandbox_id)
        killed = await client.kill(sandbox_id)
        started = time.monotonic()
        lookups = 0
        try:
            async with asyncio.timeout(e2b_module._REMOVAL_SECONDS):
                while await client.lookup(sandbox_id) is not None:
                    lookups += 1
                    await asyncio.sleep(e2b_module._POLL_SECONDS)
        except TimeoutError:
            pytest.fail(
                f"{sandbox_id} still known by id {e2b_module._REMOVAL_SECONDS:g}s after kill "
                f"(found on {lookups} lookup(s))"
            )
        gone = time.monotonic() - started
        listed = []
        for _ in range(_LIST_SAMPLES):
            listed.append(any(e.sandbox_id == sandbox_id for e in await client.list(run)))
            await asyncio.sleep(e2b_module._POLL_SECONDS)
        print(
            f"kill returned {killed}; {sandbox_id} was found by id on {lookups} lookup(s), "
            f"gone {gone:.2f}s after the kill; then listed per read: {listed}"
        )
    except BaseException as failure:
        try:
            await E2BWorkspaceProvider(template, client=client)._remove(sandbox_id)
        except Exception as leak:
            failure.add_note(f"cleanup of {sandbox_id} was not confirmed: {leak!r}")
        raise
    assert killed


# List reads printed after a confirmed removal, to show whether the list is monotonic.
_LIST_SAMPLES = 10


async def test_q5_private_ghcr_digest_builds_and_launches(tmp_path: Path) -> None:
    _, image, username, token, expected = _required(
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
    receipt = await build_template(spec, registry_username=username, registry_password=token)
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
        # Bounded: list_owned waits out a list still showing what destroy removed.
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
        assert got.files == (("probe", b"kept"),) and got.refused == ()
    finally:
        await provider.destroy(workspace)
        # Bounded: list_owned waits out a list still showing what destroy removed.
        assert await provider.list_owned(run) == []


async def test_live_operation_scrubs_a_binding_and_reports_the_real_exit_code(
    template: str,
) -> None:
    """#95 against real E2B: credential env, launch marker on stdin, true exit code."""
    from agentic_isolation.contract import (
        CredentialBinding,
        CredentialRef,
        LaunchMarker,
        OperationSpec,
    )
    from agentic_isolation.providers.base import run_to_completion

    class Vault:
        async def resolve(self, ref: CredentialRef) -> str:
            return "s3cr3t-live-e2b"

    provider = E2BWorkspaceProvider(template, credentials=Vault())
    workspace = await provider.create(
        WorkspaceConfig(provider="e2b", labels={"e2b_live.run": uuid.uuid4().hex})
    )
    try:
        spec = OperationSpec.model_validate(
            {
                "execution_id": "exec-live",
                "operation_key": "live-1",
                "argv": ("sh", "-c", 'echo "token=$TOKEN"; echo 0 > exit_code; exit 7'),
                "working_directory": ".",
                "environment": {},
                "credential_bindings": (
                    CredentialBinding(
                        ref=CredentialRef(name="t", scope="repo"), delivery="env", target="TOKEN"
                    ),
                ),
                "launch_marker": LaunchMarker(argv0="aw-live", line="AW_LAUNCH live"),
                "timeout_seconds": 60,
                "metadata": {},
            }
        )
        status, output = await run_to_completion(provider, workspace, spec)
        assert (status.state, status.exit_code) == ("failed", 7)
        assert output == b"AW_LAUNCH live\ntoken=[REDACTED]\n"
    finally:
        await provider.destroy(workspace)


async def test_live_cancel_stops_the_whole_group_in_the_sandbox(template: str) -> None:
    """#95 against real E2B: a TERM-trapping child and a grandchild die with cancel."""
    from agentic_isolation.contract import OperationSpec

    provider = E2BWorkspaceProvider(template)
    workspace = await provider.create(
        WorkspaceConfig(provider="e2b", labels={"e2b_live.run": uuid.uuid4().hex})
    )
    script = (
        "(sh -c 'echo $$ >> pids; exec sleep 300' &); "
        "sh -c 'trap \"\" TERM; echo $$ >> pids; exec sleep 300' >/dev/null 2>&1 & "
        "echo $$ >> pids; exec sleep 300"
    )
    try:
        spec = OperationSpec.model_validate(
            {
                "execution_id": "exec-live",
                "operation_key": "live-cancel",
                "argv": ("sh", "-c", script),
                "working_directory": ".",
                "environment": {},
                "credential_bindings": (),
                "timeout_seconds": 120,
                "metadata": {},
            }
        )
        ref = await provider.start(workspace, spec)
        while len((await provider.execute(workspace, "cat pids 2>/dev/null")).stdout.split()) < 3:
            await asyncio.sleep(0.5)
        assert (await provider.cancel(ref, grace_seconds=1.0)).state == "cancelled"
        alive = await provider.execute(
            workspace, 'for p in $(cat pids); do kill -0 "$p" 2>/dev/null && echo "$p"; done'
        )
        assert alive.stdout.strip() == ""
    finally:
        await provider.destroy(workspace)


async def test_a_fresh_provider_reattaches_cancels_and_pause_resume_keeps_the_sandbox(
    live: tuple[E2BWorkspaceProvider, Workspace],
) -> None:
    """#103: status and cancel from a second provider given only the ref and token."""
    from agentic_isolation.contract import OperationRef, OperationSpec

    provider, workspace = live
    spec = OperationSpec.model_validate(
        {
            "execution_id": "e2b-live-reattach",
            "operation_key": uuid.uuid4().hex,
            "argv": ("sh", "-c", "exec sleep 600"),
            "working_directory": ".",
            "environment": {},
            "credential_bindings": (),
            "timeout_seconds": 900,
            "metadata": {},
        }
    )
    ref = await provider.start(workspace, spec)
    token = provider.reattach_token(ref)
    await asyncio.sleep(2)

    fresh = E2BWorkspaceProvider(provider._template)
    persisted = OperationRef.model_validate_json(ref.model_dump_json())
    with pytest.raises(Exception, match="not owned"):
        await fresh.reattach(persisted, "0" * 32)
    await fresh.reattach(persisted, token)
    assert (await fresh.status(persisted)).state == "running"
    assert (await fresh.cancel(persisted, grace_seconds=2)).state == "cancelled"

    await fresh.pause(workspace)
    await fresh.resume(workspace)
    marker = await provider.execute(workspace, "echo resumed")
    assert "resumed" in marker.stdout


# Nested Docker (AgentParadise/agentic-workspace#91).

# postgres:16 and hello-world:latest index digests, read from Docker Hub 2026-10-27.
_POSTGRES_16 = "sha256:9e2cf6287f25c3151f4c9421ccce2b17f3e60061c111a29165e0d2cdd9ff0a97"
_HELLO_WORLD = "sha256:11a3eb1597e864c51b3f56733cb2e8a90bf0c284bfc05f9444d81c558bc848aa"
_COMPOSE = """\
services:
  db:
    image: postgres:16
    pull_policy: never
    environment:
      POSTGRES_PASSWORD: aw91-not-a-secret
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "postgres"]
      interval: 1s
      retries: 60
"""


async def _run(provider: E2BWorkspaceProvider, workspace: Workspace, command: str) -> str:
    started = time.monotonic()
    result = await provider.execute(workspace, command, timeout=300)
    print(f"$ {command}  [{time.monotonic() - started:.1f}s, exit {result.exit_code}]")
    print(result.stdout.rstrip())
    assert result.success, f"{command!r} failed: {result.stdout}"
    return result.stdout


async def test_nested_docker_template_runs_compose_postgres() -> None:
    """#91: the pinned workspace image plus `DockerEngine`, built and used on E2B.

    Builds a template named `aw91-docker-test-<hex>` from E2B_DOCKER_BASE_IMAGE
    (`repo@sha256:<digest>`), unless E2B_DOCKER_TEMPLATE names one already
    built this way. E2B_DOCKER_MANIFEST_HASH is the sha256 of the file at
    E2B_MANIFEST_PATH in the base image; the test reads it back from the
    workspace, so the Docker layer is shown not to have replaced the base.
    The template is NOT deleted: E2B templates are kept for the owner to review.
    """
    from agentic_isolation.providers import NESTED_DOCKER, DockerEngine, PrePull

    # The SDK reads the key from the environment. Never hold it in a local or
    # pass it as an argument: a failing traceback prints both (it did, #91).
    _, expected = _required("E2B_API_KEY", "E2B_DOCKER_MANIFEST_HASH")
    manifest = os.environ.get("E2B_MANIFEST_PATH", "/opt/agentic/version.json")
    template = os.environ.get("E2B_DOCKER_TEMPLATE")
    if not template:
        (image,) = _required("E2B_DOCKER_BASE_IMAGE")
        spec = E2BTemplateSpec(
            name=f"aw91-docker-test-{uuid.uuid4().hex[:8]}",
            image=image,
            verified_oci_digest=image.rpartition("@")[2],
            expected_manifest_hash=expected,
            cpu_count=2,
            memory_mb=4096,
            docker=DockerEngine(pre_pull=(PrePull("postgres:16", _POSTGRES_16),)),
        )
        started = time.monotonic()
        receipt = await build_template(spec)
        template = receipt.template_id
        print(
            f"built {spec.name}: template {receipt.template_id}, build {receipt.build_id}, "
            f"{time.monotonic() - started:.1f}s"
        )

    provider = E2BWorkspaceProvider(template, nested_docker=True)
    assert NESTED_DOCKER in provider.describe().capabilities
    run = {"e2b_live.run": uuid.uuid4().hex}
    started = time.monotonic()
    workspace = await provider.create(WorkspaceConfig(provider="e2b", labels=run))
    print(f"create with docker readiness: {time.monotonic() - started:.1f}s")
    try:
        hashed = await _run(provider, workspace, f"sha256sum {shlex.quote(manifest)}")
        assert "sha256:" + hashed.split()[0] == expected
        groups = await _run(provider, workspace, "id -un; id -nG")
        assert groups.split()[0] == "agent" and "docker" in groups.split()[1:]
        await _run(
            provider,
            workspace,
            "docker version --format '{{.Server.Version}}'; docker compose version --short",
        )
        hello = await _run(provider, workspace, f"docker run --rm hello-world@{_HELLO_WORLD}")
        assert "Hello from Docker!" in hello
        # Pre-pulled by digest at build time and tagged as compose names it.
        digests = await _run(
            provider, workspace, "docker image inspect postgres:16 --format '{{.RepoDigests}}'"
        )
        assert _POSTGRES_16 in digests
        await provider.write_file(workspace, "compose.yaml", _COMPOSE)
        await _run(provider, workspace, "docker compose up -d --wait --wait-timeout 120")
        ready = await _run(provider, workspace, "docker compose exec -T db pg_isready -U postgres")
        assert "accepting connections" in ready
        await _run(provider, workspace, "docker compose down -v")
    finally:
        await provider.destroy(workspace)
        assert await provider.list_owned(run) == []
