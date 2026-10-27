"""Nested-docker E2B templates and workspaces (AgentParadise/agentic-workspace#91).

Template construction runs the pinned SDK's real builder with only the network
call replaced; readiness and the published capability run the provider
against the in-memory client. Neither proves E2B runs Docker: that is
tests/integration/test_e2b_live.py::test_nested_docker_template_runs_compose_postgres.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agentic_isolation import WorkspaceConfig
from agentic_isolation.providers import (
    DOCKER_APT_KEY_FINGERPRINT,
    DOCKER_READY_CMD,
    NESTED_DOCKER,
    VERSIONS_PATH,
    CommandExit,
    DockerEngine,
    E2BTemplateSpec,
    E2BWorkspaceProvider,
    PrePull,
    RegistryCredentialsEnv,
    TemplateSpecError,
    build_template,
)
from agentic_isolation.providers import e2b as e2b_module
from tests.e2b_fake import FakeE2BClient, FakeRun

OCI = "sha256:" + "a" * 64
MANIFEST = "sha256:" + "b" * 64
PG = "sha256:" + "c" * 64


def _spec(**overrides: object) -> E2BTemplateSpec:
    fields: dict[str, object] = {
        "name": "aw-docker",
        "image": f"ghcr.io/org/image@{OCI}",
        "verified_oci_digest": OCI,
        "expected_manifest_hash": MANIFEST,
        "cpu_count": 2,
        "memory_mb": 4096,
        "docker": DockerEngine(pre_pull=(PrePull("postgres:16", PG),)),
    }
    fields.update(overrides)
    return E2BTemplateSpec(**fields)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def quick_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(e2b_module, "_REMOVAL_SECONDS", 5.0)
    monkeypatch.setattr(e2b_module, "_POLL_SECONDS", 0.01)


# Template construction.


@dataclass
class _Info:
    template_id: str
    build_id: str


async def _built(monkeypatch: pytest.MonkeyPatch, spec: E2BTemplateSpec) -> dict[str, object]:
    from e2b import AsyncTemplate

    seen: dict[str, object] = {}

    async def build(template: object, name: str, **kwargs: object) -> _Info:
        seen.update(kwargs, template=template, name=name)
        return _Info("tpl-d", "bld-d")

    monkeypatch.setattr(AsyncTemplate, "build", staticmethod(build))
    receipt = await build_template(spec, api_key="k")
    assert receipt.spec == spec and receipt.spec.docker == spec.docker
    return seen


async def test_docker_template_layers_the_pinned_engine_on_the_pinned_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(min_free_disk_mb=8192)
    seen = await _built(monkeypatch, spec)

    built = vars(seen["template"])["_template"]
    assert built._base_image == spec.image
    install, prepull, user = (step["args"] for step in built._instructions)
    assert install[1] == prepull[1] == "root"
    # E2B runs the ready command as the template's user, so it must be the
    # docker one, and it must be the last step before the ready command.
    assert str(built._instructions[-1]["type"]).endswith("USER") and user == ["agent"]
    for package in (
        "'docker-ce=5:29.9.2-1~debian.12~bookworm'",
        "'docker-ce-cli=5:29.9.2-1~debian.12~bookworm'",
        "'containerd.io=2.4.2-3~debian.12~bookworm'",
        "'docker-compose-plugin=5.6.1-1~debian.12~bookworm'",
        "'docker-buildx-plugin=0.38.1-1~debian.12~bookworm'",
    ):
        assert package in install[0]
    # The key is checked before the repository is added or any package trusted.
    key_check = install[0].index(f"= {DOCKER_APT_KEY_FINGERPRINT}")
    assert key_check < install[0].index("sources.list.d/docker.list")
    assert "usermod -aG docker agent" in install[0]
    assert "get.docker.com" not in install[0]
    # Pulled by digest, then tagged as compose names it.
    pinned = f"postgres@{PG}"
    assert prepull[0].index(f"docker pull {pinned}") < prepull[0].index(
        f"docker tag {pinned} postgres:16"
    )
    assert built._ready_cmd == DOCKER_READY_CMD == "docker info >/dev/null 2>&1"
    assert built._start_cmd is None
    assert (seen["cpu_count"], seen["memory_mb"], seen["min_free_disk_mb"]) == (2, 4096, 8192)


async def test_a_template_without_docker_is_the_image_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await _built(monkeypatch, _spec(docker=None, memory_mb=2048))
    built = vars(seen["template"])["_template"]
    assert built._instructions == [] and built._ready_cmd is None
    assert seen["min_free_disk_mb"] is None


async def test_no_pre_pull_step_when_none_is_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = await _built(monkeypatch, _spec(docker=DockerEngine()))
    assert len(vars(seen["template"])["_template"]._instructions) == 2


def test_a_docker_template_below_the_spec_floor_is_refused() -> None:
    with pytest.raises(TemplateSpecError, match="at least 2 vCPU and 4096 MB"):
        _spec(cpu_count=1)
    with pytest.raises(TemplateSpecError, match="at least 2 vCPU and 4096 MB"):
        _spec(memory_mb=2048)
    # The floor is config: a caller may lower it deliberately.
    lowered = DockerEngine(min_memory_mb=2048)
    assert _spec(memory_mb=2048, docker=lowered).docker == lowered


@pytest.mark.parametrize(
    "engine",
    [
        {"docker_ce_version": "latest"},
        {"docker_ce_version": "5:29.9.2-1~debian.12~bookworm; curl x | sh"},
        {"compose_version": "5.6.1-1~debian.13~trixie"},
        {"apt_suite": "bookworm stable"},
        {"apt_key_fingerprint": "9dc858229fc7dd38854ae2d88d81803c0ebfcd88"},
        {"docker_user": "agent; rm -rf /"},
        {"pre_pull": (PrePull("postgres:16", PG), PrePull("postgres:16", PG))},
        {"min_cpu_count": 0},
    ],
)
def test_docker_engine_refuses_an_unpinned_or_unsafe_input(engine: dict[str, object]) -> None:
    with pytest.raises(TemplateSpecError):
        DockerEngine(**engine)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("reference", "digest"),
    [("postgres@" + PG, PG), ("postgres:16", "16"), ("Postgres:16", PG), ("pg 16", PG)],
)
def test_pre_pull_refuses_what_is_not_a_tag_and_a_digest(reference: str, digest: str) -> None:
    with pytest.raises(TemplateSpecError):
        PrePull(reference, digest)


@pytest.mark.parametrize(
    ("reference", "repository"),
    [
        ("postgres:16", "postgres"),
        ("ghcr.io/syntropic137/event-store:0.18.0", "ghcr.io/syntropic137/event-store"),
        ("localhost:5000/x/y", "localhost:5000/x/y"),
        ("redis", "redis"),
    ],
)
def test_pre_pull_repository_drops_only_the_tag(reference: str, repository: str) -> None:
    assert PrePull(reference, PG).repository == repository


def test_min_free_disk_must_not_be_negative() -> None:
    with pytest.raises(TemplateSpecError):
        _spec(min_free_disk_mb=-1)


# Readiness and the published capability.


def _docker_answers(after: int = 0) -> tuple[FakeE2BClient, list[int]]:
    """A client whose `docker info` fails `after` times, then prints a version."""
    calls = [0]

    def respond(command: str) -> FakeRun:
        if "docker info" not in command:
            return FakeRun()
        calls[0] += 1
        if calls[0] <= after:
            return FakeRun(
                chunks=["Cannot connect to the Docker daemon\n"],
                exit=CommandExit(end="exited", exit_code=1),
            )
        return FakeRun(chunks=["29.9.2\n"])

    return FakeE2BClient(responder=respond), calls


async def test_create_waits_for_docker_info_as_the_workspace_user() -> None:
    client, calls = _docker_answers(after=2)
    provider = E2BWorkspaceProvider("tpl-d", client=client, nested_docker=True)

    workspace = await provider.create(WorkspaceConfig(provider="e2b"))

    assert calls[0] == 3
    probes = [cmd for _, cmd, _, _ in client.started if "docker info" in cmd]
    expected = shlex.join([*e2b_module._MERGED, "sh", "-c", e2b_module._DOCKER_READY])
    assert probes == [expected] * 3
    assert set(client.users) == {"agent"}
    assert workspace.id in client.sandboxes


async def test_create_removes_the_sandbox_when_docker_never_answers() -> None:
    client, _ = _docker_answers(after=1_000_000)
    provider = E2BWorkspaceProvider(
        "tpl-d", client=client, nested_docker=True, docker_ready_seconds=0.1
    )

    with pytest.raises(RuntimeError, match="did not answer `docker info`.*Cannot connect"):
        await provider.create(WorkspaceConfig(provider="e2b"))

    assert client.sandboxes == {}
    assert client.events[-1].startswith("kill ")


async def test_an_empty_docker_info_is_not_ready() -> None:
    client = FakeE2BClient(responder=lambda _cmd: FakeRun(chunks=[" \n"]))
    provider = E2BWorkspaceProvider(
        "tpl-d", client=client, nested_docker=True, docker_ready_seconds=0.1
    )
    with pytest.raises(RuntimeError, match="did not answer"):
        await provider.create(WorkspaceConfig(provider="e2b"))


async def test_a_plain_provider_never_probes_docker() -> None:
    client, calls = _docker_answers()
    await E2BWorkspaceProvider("tpl-1", client=client).create(WorkspaceConfig(provider="e2b"))
    assert calls[0] == 0


def test_only_a_nested_docker_provider_publishes_the_capability() -> None:
    client = FakeE2BClient()
    plain = E2BWorkspaceProvider("tpl-1", client=client).describe()
    docker = E2BWorkspaceProvider("tpl-d", client=client, nested_docker=True).describe()

    assert NESTED_DOCKER == "nested_docker"
    assert NESTED_DOCKER not in plain.capabilities
    assert docker.capabilities == (*plain.capabilities, NESTED_DOCKER)
    assert replace_capabilities(docker, plain.capabilities) == plain


def replace_capabilities(descriptor: object, capabilities: tuple[str, ...]) -> object:
    """`descriptor` with `capabilities` swapped, to show nothing else differs."""
    from agentic_isolation.contract import ProviderDescriptor

    assert isinstance(descriptor, ProviderDescriptor)
    return descriptor.model_copy(update={"capabilities": capabilities})


def test_docker_ready_seconds_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        E2BWorkspaceProvider(
            "tpl-d", client=FakeE2BClient(), nested_docker=True, docker_ready_seconds=0
        )


def test_specs_compare_by_value_so_a_receipt_records_the_engine() -> None:
    spec = _spec()
    assert replace(spec, docker=DockerEngine()) != spec


# The generated build steps, executed by a real shell with stand-in tools.


def _step(commands: list[str], marker: str) -> str:
    (step,) = [command for command in commands if marker in command]
    return step


def _sh(command: str, tmp_path: Path, **tools: str) -> subprocess.CompletedProcess[str]:
    """Run `command` with each of `tools` replaced by a script printing its value."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, output in tools.items():
        (bin_dir / name).write_text(f"#!/bin/sh\ncat <<'OUT'\n{output}OUT\n")
        (bin_dir / name).chmod(0o755)
    return subprocess.run(
        ["sh", "-c", command],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )


def _pub(fingerprint: str) -> str:
    return (
        "pub:-:4096:1:8D81803C0EBFCD88:1487788586:::-:::scESA::::::23::0:\n"
        f"fpr:::::::::{fingerprint}:\n"
    )


_SUB = (
    "sub:-:4096:1:7EA0A9C3F273FCD8:1487792064:::-:::s::::::23:\n"
    "fpr:::::::::D3306A018370199E527AE7997EA0A9C3F273FCD8:\n"
)
_OTHER = "0123456789ABCDEF0123456789ABCDEF01234567"


@pytest.mark.parametrize(
    ("listing", "trusted"),
    [
        (_pub(DOCKER_APT_KEY_FINGERPRINT) + _SUB, True),
        (_pub(_OTHER), False),
        # Docker's key plus any other primary key: apt would trust both.
        (_pub(DOCKER_APT_KEY_FINGERPRINT) + _SUB + _pub(_OTHER), False),
        (_pub(_OTHER) + _pub(DOCKER_APT_KEY_FINGERPRINT), False),
        ("", False),
    ],
)
def test_the_key_check_trusts_exactly_one_primary_key_docker_s(
    tmp_path: Path, listing: str, trusted: bool
) -> None:
    check = _step(DockerEngine().install_commands(), "gpg --show-keys")
    assert (_sh(check, tmp_path, gpg=listing).returncode == 0) is trusted


def test_the_apt_source_is_one_line_naming_the_suite(tmp_path: Path) -> None:
    source = _step(DockerEngine().install_commands(), "printf")
    target = tmp_path / "docker.list"
    command = source.replace("/etc/apt/sources.list.d/docker.list", str(target))
    assert _sh(command, tmp_path, dpkg="amd64\n").returncode == 0
    assert target.read_text() == (
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] "
        "https://download.docker.com/linux/debian bookworm stable\n"
    )


@pytest.mark.parametrize(
    ("os_release", "accepted"),
    [
        ("ID=debian\nVERSION_CODENAME=bookworm\n", True),
        ("ID=debian\nVERSION_CODENAME=trixie\n", False),
        ("ID=debian\nVERSION_CODENAME=bookworm-backports\n", False),
    ],
)
def test_a_base_on_another_debian_release_fails_the_build(
    tmp_path: Path, os_release: str, accepted: bool
) -> None:
    release = tmp_path / "os-release"
    release.write_text(os_release)
    check = _step(DockerEngine().install_commands(), "/etc/os-release")
    command = check.replace("/etc/os-release", str(release))
    assert (_sh(command, tmp_path).returncode == 0) is accepted


def test_the_build_records_every_resolved_version_and_expects_the_pins() -> None:
    engine = DockerEngine()
    record = _step(engine.install_commands(), "dpkg-query")
    assert record.endswith(f"> {VERSIONS_PATH}")
    for package in (*engine.expected_versions(), "ca-certificates", "curl", "gnupg", "iptables"):
        assert f" {package}" in record
    assert engine.expected_versions()["containerd.io"] == engine.containerd_version


# Registry credentials named by environment variable, never passed (#91).

_ENV = RegistryCredentialsEnv("AW91_TEST_REGISTRY_USER", "AW91_TEST_REGISTRY_TOKEN")


async def test_registry_env_reads_the_credentials_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from e2b import AsyncTemplate

    seen: dict[str, object] = {}

    async def build(template: object, name: str, **kwargs: object) -> _Info:
        seen["template"] = template
        return _Info("tpl-r", "bld-r")

    monkeypatch.setattr(AsyncTemplate, "build", staticmethod(build))
    monkeypatch.setenv(_ENV.username_variable, "invented-user")
    monkeypatch.setenv(_ENV.password_variable, "invented-token-0000")
    await build_template(_spec(docker=None, memory_mb=2048), registry_env=_ENV)
    built = vars(seen["template"])["_template"]
    assert built._registry_config == {
        "type": "registry",
        "username": "invented-user",
        "password": "invented-token-0000",
    }


async def test_registry_env_refuses_unset_variables_and_explicit_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_ENV.username_variable, raising=False)
    monkeypatch.delenv(_ENV.password_variable, raising=False)
    with pytest.raises(TemplateSpecError, match="unset: AW91_TEST_REGISTRY_USER"):
        await build_template(_spec(), registry_env=_ENV)
    with pytest.raises(TemplateSpecError, match="not both"):
        await build_template(_spec(), registry_env=_ENV, registry_password="x")
