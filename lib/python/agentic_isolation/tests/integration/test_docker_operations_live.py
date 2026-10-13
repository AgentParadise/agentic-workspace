"""Docker-backed half of the operation conformance suite (plan section 7 row 3).

`tests/test_docker_operations.py` proves the provider's logic through a
stand-in `docker`. These repeat the exit tests whose meaning depends on the
daemon itself: `-e NAME` delivery across it, a real pid namespace for the
group cancel, and a container that is really gone. They skip without Docker,
like the rest of this directory, so a green run without Docker proves nothing:
read the pass and skip counts.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator

import pytest

from agentic_isolation import SecurityConfig, WorkspaceConfig, WorkspaceDockerProvider
from agentic_isolation.contract import (
    CredentialBinding,
    CredentialRef,
    ObservationPage,
    OperationRef,
    OperationSpec,
)
from agentic_isolation.providers.base import Workspace

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not WorkspaceDockerProvider.is_available(), reason="Docker not available"),
]

SECRET = "live-s3cr3t-7d1c"
IMAGE = "python:3.12-slim"


class _Resolver:
    async def resolve(self, ref: CredentialRef) -> str:
        return SECRET


@pytest.fixture
async def live() -> AsyncIterator[tuple[WorkspaceDockerProvider, Workspace]]:
    provider = WorkspaceDockerProvider(
        default_image=IMAGE, security=SecurityConfig.development(), credentials=_Resolver()
    )
    workspace = await provider.create(WorkspaceConfig(provider="docker", image=IMAGE))
    try:
        yield provider, workspace
    finally:
        await provider.destroy(workspace)


def _spec(script: str, *, key: str = "k1", **overrides: object) -> OperationSpec:
    fields: dict[str, object] = {
        "execution_id": "exec-live",
        "operation_key": key,
        "argv": ("sh", "-c", script),
        "working_directory": ".",
        "environment": {},
        "credential_bindings": (),
        "timeout_seconds": 120,
        "metadata": {},
    }
    fields.update(overrides)
    return OperationSpec.model_validate(fields)


async def _drain(provider: WorkspaceDockerProvider, ref: OperationRef) -> list[ObservationPage]:
    pages: list[ObservationPage] = []
    cursor: str | None = None
    while True:
        page = await provider.observe(ref, cursor=cursor, max_bytes=1 << 16, wait_seconds=1.0)
        pages.append(page)
        cursor = page.next_cursor
        if page.complete:
            return pages


async def test_a_start_returns_promptly_while_running(
    live: tuple[WorkspaceDockerProvider, Workspace],
) -> None:
    provider, workspace = live
    began = time.monotonic()
    ref = await provider.start(workspace, _spec("sleep 30"))
    assert time.monotonic() - began < 2
    assert (await provider.status(ref)).state == "running"
    await provider.cancel(ref, grace_seconds=1)


async def test_d_binding_reaches_the_workload_env_but_no_page(
    live: tuple[WorkspaceDockerProvider, Workspace],
) -> None:
    provider, workspace = live
    binding = CredentialBinding(
        ref=CredentialRef(name="github", scope="repo"), delivery="env", target="GH_TOKEN"
    )
    spec = _spec(
        'echo "overlay=$OVERLAY"; [ "$GH_TOKEN" = "' + SECRET + '" ] && echo bound',
        environment={"OVERLAY": "on"},
        credential_bindings=(binding,),
    )
    # The script text itself names the secret, so compare outputs, not argv.
    ref = await provider.start(workspace, spec)
    pages = await _drain(provider, ref)
    data = b"".join(base64.b64decode(p.data_b64) for p in pages)
    assert data == b"overlay=on\nbound\n"
    assert all(SECRET not in p.model_dump_json() for p in pages)


async def test_k_cancel_kills_leader_grandchild_and_term_trapper(
    live: tuple[WorkspaceDockerProvider, Workspace],
) -> None:
    provider, workspace = live
    script = (
        "(sleep 300 & echo grandchild=$!) ; "
        "sh -c 'trap \"\" TERM; echo trapper=$$; exec sleep 300' & "
        "echo leader=$$; wait"
    )
    ref = await provider.start(workspace, _spec(script))
    seen: dict[str, str] = {}
    cursor: str | None = None
    while len(seen) < 3:
        page = await provider.observe(ref, cursor=cursor, max_bytes=1 << 16, wait_seconds=1.0)
        cursor = page.next_cursor
        for line in base64.b64decode(page.data_b64).decode().splitlines():
            name, _, pid = line.partition("=")
            seen[name] = pid
    status = await provider.cancel(ref, grace_seconds=1)
    assert status.state == "cancelled"
    for pid in seen.values():
        result = await provider.execute(workspace, f"kill -0 {pid} 2>/dev/null && echo alive")
        assert "alive" not in result.stdout, seen


async def test_l_destroyed_container_is_lost(
    live: tuple[WorkspaceDockerProvider, Workspace],
) -> None:
    provider, workspace = live
    ref = await provider.start(workspace, _spec("sleep 60"))
    proc = await asyncio.create_subprocess_exec("docker", "rm", "-f", str(workspace._handle))
    await proc.wait()
    deadline = time.monotonic() + 20
    while (status := await provider.status(ref)).state == "running":
        assert time.monotonic() < deadline
        await asyncio.sleep(0.2)
    assert status.state == "lost"
