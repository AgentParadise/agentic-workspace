"""APSS EXP-V1-0006 debt items closed in the Docker provider (plan PR 5).

W-3.8-d disk refusal, W-3.2-b write_file containment, W-3.8-b
`ResourceLimits.timeout_seconds`, W-6.3-a credential bindings. Each test
drives the provider and asserts on what reached the subprocess boundary.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

from agentic_isolation.config import ResourceLimits, WorkspaceConfig, host_env_credential
from agentic_isolation.contract import CredentialBinding, CredentialRef
from agentic_isolation.providers.base import (
    CredentialPolicyError,
    HostEnvironmentCredentials,
    UnsupportedLimitError,
    Workspace,
)
from agentic_isolation.providers.docker import WorkspaceDockerProvider
from agentic_isolation.providers.docker_operations import DockerOperations
from tests.test_docker_operations import FAKE_DOCKER

# Outer bound on each execute, so a lifecycle that never settles fails, not hangs.
_TEST_BOUND = 20

LIVE_VALUE = "sk-live-7f3a9c-must-never-appear-in-argv"


class Spawned:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> Spawned:
    record = Spawned()

    class Created:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"container-id\n", b""

    async def spawn(*args: object, **kwargs: object) -> Created:
        record.calls.append((args, kwargs))
        return Created()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return record


def _provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: object
) -> WorkspaceDockerProvider:
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path, **kwargs)  # type: ignore[arg-type]

    async def plain_image(_image: str) -> tuple[str, dict[str, str]]:
        return "sha256:" + "0" * 64, {}

    async def noop(*args: object, **kw: object) -> None:
        return None

    monkeypatch.setattr(provider, "_inspect_image", plain_image)
    monkeypatch.setattr(provider, "_ensure_network", noop)
    monkeypatch.setattr(provider, "_wait_for_running", noop)
    monkeypatch.setattr(provider, "_cleanup_container", noop)
    return provider


def _docker_run(spawned: Spawned) -> tuple[list[str], dict[str, object]]:
    runs = [(a, k) for a, k in spawned.calls if a[:2] == ("docker", "run")]
    assert len(runs) == 1
    args, kwargs = runs[0]
    return [str(arg) for arg in args], kwargs


# W-3.8-d


async def test_unenforceable_disk_limit_is_refused_before_docker_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawned: Spawned
) -> None:
    provider = _provider(monkeypatch, tmp_path)
    config = WorkspaceConfig(provider="docker", limits=ResourceLimits(disk="1g"))

    with pytest.raises(UnsupportedLimitError, match="disk limit '1g'"):
        await provider.create(config)

    assert spawned.calls == []
    assert list(tmp_path.iterdir()) == []


# W-6.3-a


def test_credential_value_in_environment_fails_validation() -> None:
    for key in ("API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "AGENTIC_SESSION_STORE_AUTH"):
        with pytest.raises(ValueError, match="name a credential"):
            WorkspaceConfig(environment={key: LIVE_VALUE})
    with pytest.raises(ValueError, match="name a credential"):
        WorkspaceConfig().with_env("SESSION_SECRET", LIVE_VALUE)


def test_auth_guard_does_not_catch_unrelated_words() -> None:
    config = WorkspaceConfig(environment={"GIT_AUTHOR_NAME": "agent", "OAUTHLIB_INSECURE": "0"})
    assert config.environment["GIT_AUTHOR_NAME"] == "agent"


def test_config_has_no_field_that_holds_a_credential_value() -> None:
    with pytest.raises(TypeError):
        WorkspaceConfig(secrets={"API_KEY": LIVE_VALUE})  # type: ignore[call-arg]


async def test_resolved_credential_reaches_docker_env_but_never_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawned: Spawned
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", LIVE_VALUE)
    provider = _provider(monkeypatch, tmp_path, credentials=HostEnvironmentCredentials())
    config = WorkspaceConfig(
        provider="docker",
        environment={"LOG_LEVEL": "debug"},
        credentials=[host_env_credential("ANTHROPIC_API_KEY")],
    )

    await provider.create(config)

    argv, kwargs = _docker_run(spawned)
    assert not any(LIVE_VALUE in arg for arg in argv)
    flag = argv.index("ANTHROPIC_API_KEY")
    assert argv[flag - 1] == "-e"
    assert "LOG_LEVEL=debug" in argv
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["ANTHROPIC_API_KEY"] == LIVE_VALUE


async def test_binding_delivers_under_its_target_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawned: Spawned
) -> None:
    class Vault:
        async def resolve(self, ref: CredentialRef) -> str:
            assert ref == CredentialRef(name="anthropic/prod", scope="vault")
            return LIVE_VALUE

    provider = _provider(monkeypatch, tmp_path, credentials=Vault())
    binding = CredentialBinding(
        ref=CredentialRef(name="anthropic/prod", scope="vault"),
        delivery="env",
        target="ANTHROPIC_API_KEY",
    )

    await provider.create(WorkspaceConfig(provider="docker", credentials=[binding]))

    argv, kwargs = _docker_run(spawned)
    assert "ANTHROPIC_API_KEY" in argv
    assert not any(LIVE_VALUE in arg for arg in argv)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["ANTHROPIC_API_KEY"] == LIVE_VALUE


async def test_credential_added_after_construction_is_refused_at_docker_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawned: Spawned
) -> None:
    provider = _provider(monkeypatch, tmp_path)
    config = WorkspaceConfig(provider="docker")
    config.environment["GH_TOKEN"] = LIVE_VALUE

    with pytest.raises(ValueError, match="name a credential"):
        await provider.create(config)
    assert not any(LIVE_VALUE in str(arg) for args, _ in spawned.calls for arg in args)


@pytest.mark.parametrize(
    ("kwargs", "binding", "match"),
    [
        ({}, host_env_credential("API_KEY"), "no CredentialResolver"),
        (
            {"credentials": HostEnvironmentCredentials()},
            CredentialBinding(
                ref=CredentialRef(name="API_KEY", scope="host-env"),
                delivery="file",
                target="/run/secrets/api_key",
            ),
            "delivery 'file'",
        ),
        (
            {"credentials": HostEnvironmentCredentials()},
            CredentialBinding(
                ref=CredentialRef(name="API_KEY", scope="vault"), delivery="env", target="API_KEY"
            ),
            "resolves only 'host-env'",
        ),
    ],
)
async def test_undeliverable_binding_fails_closed_before_docker_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spawned: Spawned,
    kwargs: dict[str, object],
    binding: CredentialBinding,
    match: str,
) -> None:
    monkeypatch.setenv("API_KEY", LIVE_VALUE)
    provider = _provider(monkeypatch, tmp_path, **kwargs)

    with pytest.raises(CredentialPolicyError, match=match):
        await provider.create(WorkspaceConfig(provider="docker", credentials=[binding]))
    assert spawned.calls == []


# W-3.2-b


def _workspace(tmp_path: Path, limits: ResourceLimits | None = None) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(
        id="ws-test",
        provider="docker",
        path=Path("/workspace"),
        config=WorkspaceConfig(provider="docker", limits=limits or ResourceLimits()),
        metadata={"workspace_dir": str(root)},
        _handle="agentic-ws-test",
    )


@pytest.mark.parametrize("path", ["../x", "a/../../x", "/etc/passwd"])
async def test_write_file_refuses_paths_outside_the_workspace(tmp_path: Path, path: str) -> None:
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    workspace = _workspace(tmp_path)

    with pytest.raises(ValueError, match="workspace"):
        await provider.write_file(workspace, path, "escaped")
    assert not (tmp_path / "x").exists()


async def test_write_file_refuses_a_symlink_that_leaves_the_workspace(tmp_path: Path) -> None:
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (Path(workspace.metadata["workspace_dir"]) / "link").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes"):
        await provider.write_file(workspace, "link/x", "escaped")
    with pytest.raises(ValueError, match="escapes"):
        await provider.read_file(workspace, "link/x")
    assert not (outside / "x").exists()


async def test_dot_dot_after_a_symlink_is_refused_not_folded(tmp_path: Path) -> None:
    """`link/../x` means "beside wherever link points", so it is an escape."""
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    workspace = _workspace(tmp_path)
    root = Path(workspace.metadata["workspace_dir"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes"):
        await provider.write_file(workspace, "link/../x", "escaped")
    assert not (tmp_path / "x").exists()
    assert not (root / "x").exists()

    await provider.write_file(workspace, "nested/../inside", "ok")
    assert (root / "inside").read_text() == "ok"


async def test_write_file_still_writes_nested_paths_inside(tmp_path: Path) -> None:
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    workspace = _workspace(tmp_path)

    await provider.write_file(workspace, "a/b/./c.txt", "ok")

    assert await provider.read_file(workspace, "a/b/c.txt") == "ok"
    assert await provider.file_exists(workspace, "a/b/c.txt")


# W-3.8-b


def test_timeout_seconds_must_be_positive() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ResourceLimits(timeout_seconds=0)


@pytest.fixture
def docker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The operations suite's stand-in `docker`, which runs `exec` as host processes."""
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "state"))
    return tmp_path


def _operations_provider(docker_env: Path) -> WorkspaceDockerProvider:
    """A provider whose execute runs the real `DockerOperations` lifecycle.

    Only the `docker` client is a stand-in: launch marker, collector, spool,
    deadline and group termination are all the production code.
    """
    provider = WorkspaceDockerProvider(workspace_base_dir=docker_env)
    provider._operations = DockerOperations(
        credentials=None,
        retention_bytes=1 << 20,
        max_page_bytes=1 << 16,
        docker=str(docker_env / "docker"),
    )
    return provider


async def test_limits_timeout_seconds_ends_execute_timed_out(docker_env: Path) -> None:
    """No per-call timeout: the workspace's own limit must end `sleep 30`."""
    provider = _operations_provider(docker_env)
    workspace = _workspace(docker_env, ResourceLimits(timeout_seconds=1))

    started = time.monotonic()
    result = await asyncio.wait_for(provider.execute(workspace, "sleep 30"), _TEST_BOUND)
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert "Command timed out" in result.stderr
    # The 1s limit plus termination's grace; the 3600s default would not return.
    assert elapsed < 5, f"one-second limit returned after {elapsed:.2f}s"


async def test_per_call_timeout_overrides_the_workspace_limit(docker_env: Path) -> None:
    provider = _operations_provider(docker_env)
    workspace = _workspace(docker_env, ResourceLimits(timeout_seconds=1))

    result = await asyncio.wait_for(
        provider.execute(workspace, "sleep 1.5; echo done", timeout=4), _TEST_BOUND
    )

    assert (result.timed_out, result.exit_code, result.stdout) == (False, 0, "done\n")


async def test_timed_out_operation_tree_is_killed_and_cannot_act_later(docker_env: Path) -> None:
    """A descendant of the timed-out command must not perform its delayed write."""
    provider = _operations_provider(docker_env)
    workspace = _workspace(docker_env, ResourceLimits(timeout_seconds=1))
    marker = docker_env / "late-side-effect"

    result = await asyncio.wait_for(
        provider.execute(workspace, f"(sleep 2; echo late > '{marker}') & sleep 30"),
        _TEST_BOUND,
    )
    await asyncio.sleep(2.5)

    assert result.timed_out is True
    assert not marker.exists(), "a descendant of the timed-out command kept running"
    # Settled and released: no operation record outlives the call.
    assert provider._operations._operations == {}


# W-6.3-a: bindings are validated as contract references with deliverable targets


@pytest.mark.parametrize(
    "target",
    [
        "PATH",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "LD_PRELOAD",
        "HTTPS_PROXY",
        "path",
        "API_KEY=x",
        "1X",
    ],
)
async def test_binding_target_that_steers_the_host_client_is_refused_before_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawned: Spawned, target: str
) -> None:
    resolved: list[CredentialRef] = []

    class Vault:
        async def resolve(self, ref: CredentialRef) -> str:
            resolved.append(ref)
            return LIVE_VALUE

    provider = _provider(monkeypatch, tmp_path, credentials=Vault())
    binding = CredentialBinding(
        ref=CredentialRef(name="workload/key", scope="vault"), delivery="env", target=target
    )

    with pytest.raises(CredentialPolicyError):
        WorkspaceConfig(provider="docker", credentials=[binding])
    config = WorkspaceConfig(provider="docker")
    config.credentials.append(binding)
    with pytest.raises(CredentialPolicyError):
        await provider.create(config)
    assert resolved == []
    assert spawned.calls == []
    assert list(tmp_path.iterdir()) == []


async def test_workload_credential_cannot_choose_the_host_docker_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attacker_bin = tmp_path / "attacker-bin"
    attacker_bin.mkdir()
    marker = tmp_path / "host-code-executed"
    fake_docker = attacker_bin / "docker"
    fake_docker.write_text(f"#!/bin/sh\nprintf executed > '{marker}'\n")
    fake_docker.chmod(0o755)

    class Vault:
        async def resolve(self, ref: CredentialRef) -> str:
            return str(attacker_bin)

    provider = _provider(monkeypatch, tmp_path, credentials=Vault())
    config = WorkspaceConfig(provider="docker")
    config.credentials.append(
        CredentialBinding(
            ref=CredentialRef(name="workload/path", scope="vault"), delivery="env", target="PATH"
        )
    )

    with pytest.raises(CredentialPolicyError, match="reserved"):
        await provider.create(config)
    assert not marker.exists()


@pytest.mark.parametrize(
    "raw",
    [
        {"API_KEY": LIVE_VALUE},
        LIVE_VALUE,
        {
            "ref": {"name": "k", "scope": "vault"},
            "delivery": "env",
            "target": "K",
            "value": LIVE_VALUE,
        },
    ],
)
async def test_raw_credential_values_are_refused_everywhere_they_can_enter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawned: Spawned, raw: object
) -> None:
    resolved: list[CredentialRef] = []

    class Vault:
        async def resolve(self, ref: CredentialRef) -> str:
            resolved.append(ref)
            return LIVE_VALUE

    with pytest.raises(CredentialPolicyError) as constructed:
        WorkspaceConfig(credentials=[raw])  # type: ignore[list-item]
    with pytest.raises(CredentialPolicyError) as fluent:
        WorkspaceConfig().with_credential(raw)  # type: ignore[arg-type]
    for refused in (constructed, fluent):
        assert LIVE_VALUE not in str(refused.value)

    config = WorkspaceConfig(provider="docker")
    config.credentials.append(raw)  # type: ignore[arg-type]
    provider = _provider(monkeypatch, tmp_path, credentials=Vault())
    with pytest.raises(CredentialPolicyError) as consumed:
        await provider.create(config)
    assert LIVE_VALUE not in str(consumed.value)
    assert resolved == []
    assert spawned.calls == []


# W-3.2-b: containment holds when the workload swaps a symlink in mid-write


def _swap_on_open(
    monkeypatch: pytest.MonkeyPatch, name: str, outside: Path, *, replace_dir: bool = False
) -> None:
    """Turn workspace entry `name` into a symlink to `outside` just before it is opened."""
    real_open = os.open

    def racing_open(
        path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if dir_fd is not None and path == name:
            if replace_dir:
                os.rmdir(name, dir_fd=dir_fd)
            os.symlink(outside, name, dir_fd=dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", racing_open)


@pytest.mark.parametrize("content", ["escaped", b"escaped"])
async def test_symlink_swapped_in_before_the_final_open_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | bytes
) -> None:
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("preserve")
    _swap_on_open(monkeypatch, "target.txt", outside)

    with pytest.raises(ValueError, match="escapes"):
        await provider.write_file(workspace, "target.txt", content)
    assert outside.read_text() == "preserve"


async def test_parent_directory_swapped_for_a_symlink_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _swap_on_open(monkeypatch, "sub", outside, replace_dir=True)

    with pytest.raises(ValueError, match="escapes"):
        await provider.write_file(workspace, "sub/new.txt", "escaped")
    assert list(outside.iterdir()) == []


async def test_read_and_exists_do_not_follow_a_swapped_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(LIVE_VALUE)
    root = Path(workspace.metadata["workspace_dir"])
    _swap_on_open(monkeypatch, "a.txt", outside)
    with pytest.raises(ValueError, match="escapes"):
        await provider.read_file(workspace, "a.txt")
    (root / "a.txt").unlink()
    _swap_on_open(monkeypatch, "b.txt", outside)
    with pytest.raises(ValueError, match="escapes"):
        await provider.file_exists(workspace, "b.txt")
