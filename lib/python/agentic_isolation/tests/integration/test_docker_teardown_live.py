"""Docker-backed teardown usage (plan section 7 row 6, paper cuts PC-162/PC-152).

`tests/test_staged_teardown.py` proves the mapping through a stand-in
`docker`. These prove the numbers come from a real container's cgroup: an
OOM-killed workload is reported as one, and a quiet one is not. They skip
without Docker, so a green run without Docker proves nothing: read the pass
and skip counts. CI job: python-compatibility (agentic_isolation).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_isolation import (
    ResourceLimits,
    SecurityConfig,
    WorkspaceConfig,
    WorkspaceDockerProvider,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not WorkspaceDockerProvider.is_available(), reason="Docker not available"),
]

IMAGE = "python:3.12-slim"


def _provider() -> WorkspaceDockerProvider:
    return WorkspaceDockerProvider(default_image=IMAGE, security=SecurityConfig.development())


def _config(memory: str) -> WorkspaceConfig:
    return WorkspaceConfig(provider="docker", image=IMAGE, limits=ResourceLimits(memory=memory))


async def test_an_oom_killed_workload_is_reported_as_one() -> None:
    provider = _provider()
    workspace = await provider.create(_config("32m"))
    try:
        # `tail` buffers an endless line, so it grows until the kernel kills it.
        result = await provider.execute(workspace, "tail /dev/zero", timeout=120)
        assert result.exit_code == 137, result
    finally:
        report = await provider.destroy(workspace)

    assert report.finalization.state == "completed"
    assert report.usage.oom_killed is True
    assert report.usage.oom_kills is not None and report.usage.oom_kills >= 1
    assert report.usage.memory_peak_bytes is not None
    assert report.usage.memory_peak_bytes >= 16 * 1024 * 1024


async def test_a_quiet_workload_is_measured_and_not_oom_killed() -> None:
    provider = _provider()
    workspace = await provider.create(_config("256m"))
    try:
        await provider.execute(workspace, "true", timeout=60)
    finally:
        report = await provider.destroy(workspace)

    assert report.usage.oom_kills == 0
    assert report.usage.oom_killed is False
    assert report.usage.cpu_usage_seconds is not None


async def test_root_owned_mode_000_files_do_not_retain_the_workspace(tmp_path: Path) -> None:
    """The syntropic137 CI failure: the runner uid cannot delete what the
    container's root wrote, so teardown went through the container fallback."""
    provider = WorkspaceDockerProvider(
        default_image=IMAGE,
        security=SecurityConfig.development(),
        workspace_base_dir=str(tmp_path),
    )
    workspace = await provider.create(_config("256m"))
    workspace_dir = Path(workspace.metadata["workspace_dir"])
    try:
        result = await provider.execute(
            workspace,
            "mkdir /workspace/owned && echo x > /workspace/owned/locked"
            " && chmod 000 /workspace/owned/locked /workspace/owned",
            timeout=60,
        )
        assert result.exit_code == 0, result
    finally:
        report = await provider.destroy(workspace)

    assert report.finalization.state == "completed", report
    assert not workspace_dir.exists()
    if os.geteuid() != 0:
        # A root host deletes it alone; only another uid needs the fallback.
        assert report.usage.delete_fallback == "deleted"
