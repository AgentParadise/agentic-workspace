"""The Docker provider runs, logs and records the runtime SecurityConfig resolves."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from agentic_isolation.config import (
    DockerDetectionError,
    SecurityConfig,
    WorkspaceConfig,
    WorkspaceRuntime,
)
from agentic_isolation.providers.docker import WorkspaceDockerProvider

RUNC = {"runc": {"path": "runc"}}
KATA = {"kata-clh": {"path": "/opt/kata/bin/containerd-shim-kata-clh-v2"}}


class Created:
    async def communicate(self) -> tuple[bytes, bytes]:
        return b"container-id\n", b""

    @property
    def returncode(self) -> int:
        return 0


def _provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtimes: Mapping[str, object]
) -> tuple[WorkspaceDockerProvider, list[tuple[object, ...]]]:
    spawned: list[tuple[object, ...]] = []

    async def spawn(*args: object, **kwargs: object) -> Created:
        spawned.append(args)
        return Created()

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    async def plain_image(_image: str) -> tuple[str, dict[str, str]]:
        return "sha256:" + "0" * 64, {}

    monkeypatch.setattr(SecurityConfig, "_docker_runtimes", None)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        SecurityConfig, "_docker_info", staticmethod(lambda _template: json.dumps(runtimes))
    )
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(provider, "_inspect_image", plain_image)
    monkeypatch.setattr(provider, "_ensure_network", noop)
    monkeypatch.setattr(provider, "_wait_for_running", noop)
    return provider, spawned


async def test_kata_runs_under_its_alias_and_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider, spawned = _provider(monkeypatch, tmp_path, {**RUNC, **KATA})
    security = SecurityConfig(runtime=WorkspaceRuntime.KATA)

    workspace = await provider.create(WorkspaceConfig(provider="docker", security=security))

    (argv,) = spawned
    assert [arg for arg in argv if str(arg).startswith("--runtime")] == ["--runtime=kata-clh"]
    assert workspace.metadata["runtime_requested"] == "kata"
    assert workspace.metadata["runtime_effective"] == "kata"
    assert workspace.metadata["runtime_alias"] == "kata-clh"


async def test_auto_on_runc_only_host_runs_runc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider, spawned = _provider(monkeypatch, tmp_path, RUNC)

    workspace = await provider.create(WorkspaceConfig(provider="docker"))

    (argv,) = spawned
    assert [arg for arg in argv if str(arg).startswith("--runtime")] == ["--runtime=runc"]
    assert workspace.metadata["runtime_requested"] == "auto"
    assert workspace.metadata["runtime_effective"] == "runc"
    assert workspace.metadata["runtime_alias"] == "runc"


async def test_explicit_runc_runs_under_runc_and_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider, spawned = _provider(monkeypatch, tmp_path, {**RUNC, **KATA})
    security = SecurityConfig(runtime=WorkspaceRuntime.RUNC)

    workspace = await provider.create(WorkspaceConfig(provider="docker", security=security))

    (argv,) = spawned
    assert [arg for arg in argv if str(arg).startswith("--runtime")] == ["--runtime=runc"]
    assert workspace.metadata["runtime_requested"] == "runc"
    assert workspace.metadata["runtime_alias"] == "runc"


async def test_unregistered_runtime_is_refused_before_anything_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider, spawned = _provider(monkeypatch, tmp_path, RUNC)
    security = SecurityConfig(runtime=WorkspaceRuntime.KATA)

    with pytest.raises(DockerDetectionError, match="refusing to fall back to runc"):
        await provider.create(WorkspaceConfig(provider="docker", security=security))

    assert spawned == []
    assert list(tmp_path.iterdir()) == []
