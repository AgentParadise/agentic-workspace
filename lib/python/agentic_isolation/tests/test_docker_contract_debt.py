"""APSS EXP-V1-0006 debt items closed in the Docker provider (plan PR 5).

W-3.8-d disk refusal, W-3.2-b write_file containment, W-3.8-b
`ResourceLimits.timeout_seconds`, W-6.3-a credential bindings. Each test
drives the provider and asserts on what reached the subprocess boundary.
"""

import asyncio
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


async def test_limits_timeout_seconds_ends_execute_timed_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No per-call timeout: the workspace's own limit must end `sleep 5`."""
    real_spawn = asyncio.create_subprocess_exec

    async def run_locally(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        argv = [str(arg) for arg in args]
        assert argv[:2] == ["docker", "exec"]
        return await real_spawn(*argv[argv.index("sh") :], **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", run_locally)
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    workspace = _workspace(tmp_path, ResourceLimits(timeout_seconds=1))

    result = await provider.execute(workspace, "sleep 5")

    assert result.timed_out is True
