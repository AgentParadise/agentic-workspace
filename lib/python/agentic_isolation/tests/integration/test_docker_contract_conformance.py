"""Docker conformance for the plan PR 5 debt items, against a real daemon.

Hermetic: synthetic credentials from an injected resolver, no external API.
The unit tests in tests/test_docker_contract_debt.py prove the logic at the
subprocess boundary; these prove the same contract end to end through
`docker run` / `docker exec`. With REQUIRE_DOCKER_CONFORMANCE=1, or on a
GitHub Actions Linux runner (which always has a daemon), a missing daemon fails
instead of skipping. tests/integration/conftest.py prints one outcome line per
test, so a CI log names each of them.

Run with: pytest -m docker_conformance tests/integration/test_docker_contract_conformance.py
"""

import asyncio
import os
import sys
import time
from dataclasses import replace

import pytest

from agentic_isolation import (
    ResourceLimits,
    SecurityConfig,
    WorkspaceConfig,
    WorkspaceDockerProvider,
    WorkspaceRuntime,
)
from agentic_isolation.contract import CredentialBinding, CredentialRef

IMAGE = "python:3.12-slim"
SYNTHETIC_VALUE = "synthetic-conformance-credential-4d1e"

_REQUIRED = os.environ.get("REQUIRE_DOCKER_CONFORMANCE") == "1" or (
    os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux"
)
_AVAILABLE = WorkspaceDockerProvider.is_available()

pytestmark = [
    pytest.mark.docker_conformance,
    pytest.mark.integration,
    pytest.mark.skipif(not _AVAILABLE and not _REQUIRED, reason="Docker not available"),
]


@pytest.fixture(autouse=True)
def _docker_is_required() -> None:
    if _REQUIRED and not _AVAILABLE:
        pytest.fail(
            "Docker conformance is required here (REQUIRE_DOCKER_CONFORMANCE=1 or a GitHub"
            " Actions Linux runner) but the docker daemon is not available"
        )


class Vault:
    async def resolve(self, ref: CredentialRef) -> str:
        assert ref == CredentialRef(name="conformance/key", scope="vault")
        return SYNTHETIC_VALUE


async def test_bound_credential_reaches_the_container_and_no_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_spawn = asyncio.create_subprocess_exec
    argvs: list[list[str]] = []

    async def recording_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        argvs.append([str(arg) for arg in args])
        return await real_spawn(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_spawn)
    provider = WorkspaceDockerProvider(
        default_image=IMAGE, security=SecurityConfig.development(), credentials=Vault()
    )
    binding = CredentialBinding(
        ref=CredentialRef(name="conformance/key", scope="vault"),
        delivery="env",
        target="CONFORMANCE_KEY",
    )
    workspace = await provider.create(
        WorkspaceConfig(provider="docker", image=IMAGE, credentials=[binding])
    )
    try:
        result = await provider.execute(workspace, "printf '%s' \"$CONFORMANCE_KEY\"")
    finally:
        await provider.destroy(workspace)

    assert result.stdout == SYNTHETIC_VALUE
    assert any(argv[:2] == ["docker", "run"] for argv in argvs)
    assert not any(SYNTHETIC_VALUE in arg for argv in argvs for arg in argv)


async def test_timeout_seconds_stops_the_operation_tree_in_the_container() -> None:
    provider = WorkspaceDockerProvider(default_image=IMAGE, security=SecurityConfig.development())
    workspace = await provider.create(
        WorkspaceConfig(provider="docker", image=IMAGE, limits=ResourceLimits(timeout_seconds=1))
    )
    try:
        started = time.monotonic()
        result = await provider.execute(
            workspace, "(sleep 3; echo late > /workspace/late) & sleep 30"
        )
        elapsed = time.monotonic() - started
        await asyncio.sleep(4)
        late = await provider.execute(
            workspace, "test -e /workspace/late && echo present || echo absent", timeout=30
        )
    finally:
        await provider.destroy(workspace)

    assert result.timed_out is True
    # 1s limit, the operation's termination grace, and docker exec start-up overhead.
    assert elapsed < 5, f"one-second limit returned after {elapsed:.2f}s"
    assert late.stdout.strip() == "absent", "a timed-out descendant ran its delayed write"


async def test_unenforceable_disk_limit_is_refused_against_a_real_daemon() -> None:
    from agentic_isolation.providers.base import UnsupportedLimitError

    provider = WorkspaceDockerProvider(default_image=IMAGE, security=SecurityConfig.development())

    with pytest.raises(UnsupportedLimitError):
        await provider.create(
            WorkspaceConfig(provider="docker", image=IMAGE, limits=ResourceLimits(disk="1g"))
        )


@pytest.mark.parametrize("runtime", [WorkspaceRuntime.RUNC, None], ids=["runc", "auto"])
async def test_container_runs_under_the_resolved_runtime(
    monkeypatch: pytest.MonkeyPatch, runtime: WorkspaceRuntime | None
) -> None:
    # A daemon whose default-runtime is runc reports Runtime "runc" whether or
    # not --runtime was passed, so the docker run argv is checked as well.
    real_spawn = asyncio.create_subprocess_exec
    argvs: list[list[str]] = []

    async def recording_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        argvs.append([str(arg) for arg in args])
        return await real_spawn(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_spawn)
    security = replace(SecurityConfig.development(), runtime=runtime)
    resolved = security.resolve_runtime()
    provider = WorkspaceDockerProvider(default_image=IMAGE, security=security)
    workspace = await provider.create(WorkspaceConfig(provider="docker", image=IMAGE))
    try:
        inspect = await real_spawn(
            "docker",
            "inspect",
            "--format",
            "{{.HostConfig.Runtime}}",
            workspace.metadata["container_id"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await inspect.communicate()
    finally:
        await provider.destroy(workspace)

    assert inspect.returncode == 0, stderr.decode(errors="replace")
    assert stdout.decode().strip() == resolved.alias
    assert workspace.metadata["runtime_alias"] == resolved.alias
    (run,) = [argv for argv in argvs if argv[:3] == ["docker", "run", "-d"]]
    assert f"--runtime={resolved.alias}" in run
