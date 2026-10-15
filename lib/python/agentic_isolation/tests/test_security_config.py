"""Unit tests for SecurityConfig."""

import json
from collections.abc import Callable, Mapping

import pytest

from agentic_isolation.config import (
    DockerDetectionError,
    ResolvedRuntime,
    SecurityConfig,
    WorkspaceRuntime,
)


class TestSecurityConfig:
    """Tests for SecurityConfig dataclass."""

    def test_default_is_production_hardened(self) -> None:
        """Default config should have all security features enabled."""
        config = SecurityConfig()

        assert config.cap_drop_all is True
        assert config.no_new_privileges is True
        assert config.read_only_root is True
        assert config.tmpfs_tmp is True
        assert config.tmpfs_home is True
        assert config.pids_limit == 256

    def test_production_classmethod(self) -> None:
        """SecurityConfig.production() should return hardened config."""
        config = SecurityConfig.production()

        assert config.cap_drop_all is True
        assert config.no_new_privileges is True
        assert config.read_only_root is True
        assert config.pids_limit == 256

    def test_development_classmethod(self) -> None:
        """SecurityConfig.development() should return relaxed config."""
        config = SecurityConfig.development()

        # These should be relaxed for development
        assert config.read_only_root is False
        assert config.runtime is WorkspaceRuntime.RUNC

        # These should still be secure
        assert config.cap_drop_all is True
        assert config.no_new_privileges is True

    def test_to_docker_run_args_includes_all_flags(self) -> None:
        """to_docker_run_args() should generate all security flags."""
        config = SecurityConfig.production()
        # Force runc for a predictable test
        config.runtime = WorkspaceRuntime.RUNC

        args = config.to_docker_run_args()

        assert "--cap-drop=ALL" in args
        assert "--security-opt=no-new-privileges" in args
        assert "--read-only" in args
        assert "--tmpfs=/tmp:rw,noexec,nosuid,size=256m" in args
        assert "--tmpfs=/home/agent:rw,exec,nosuid,size=128m,uid=1000,gid=1000" in args
        assert "--pids-limit=256" in args
        # Named even for runc: the daemon's default-runtime need not be runc.
        assert [arg for arg in args if arg.startswith("--runtime")] == ["--runtime=runc"]

    def test_to_docker_run_args_respects_disabled_features(self) -> None:
        """Disabled features should not appear in docker args."""
        config = SecurityConfig(
            cap_drop_all=False,
            read_only_root=False,
            tmpfs_tmp=False,
            runtime=WorkspaceRuntime.RUNC,
        )

        args = config.to_docker_run_args()

        assert "--cap-drop=ALL" not in args
        assert "--read-only" not in args
        assert "--tmpfs=/tmp:rw,noexec,nosuid,size=256m" not in args

    def test_custom_pids_limit(self) -> None:
        """Custom pids_limit should be used."""
        config = SecurityConfig(pids_limit=512)

        args = config.to_docker_run_args()

        assert "--pids-limit=512" in args

    def test_pids_limit_zero_not_included(self) -> None:
        """pids_limit of 0 should not be included (unlimited)."""
        config = SecurityConfig(pids_limit=0, runtime=WorkspaceRuntime.RUNC)

        args = config.to_docker_run_args()

        # Should not have any pids-limit arg
        assert not any("pids-limit" in arg for arg in args)


RUNC = {"io.containerd.runc.v2": {"path": "runc"}, "runc": {"path": "runc"}}
RUNSC = {"runsc": {"path": "/usr/local/bin/runsc"}}
KATA = {"kata": {"path": "/opt/kata/bin/kata-runtime"}}


def _docker_info_runtimes(configured: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    """What ``docker info`` reports for daemon.json ``runtimes`` on a runc host.

    Moby's daemon/info_unix.go copies only Path and Args into each entry and
    the JSON tags omit empty fields, so a ``runtimeType`` never reaches us and
    a runtimeType-only runtime is reported as ``{}``.
    """
    reported: dict[str, object] = {**RUNC}
    for alias, spec in configured.items():
        reported[alias] = {key: spec[key] for key in ("path", "runtimeArgs") if spec.get(key)}
    return reported


def _runtime_args(args: list[str]) -> list[str]:
    return [arg for arg in args if arg.startswith("--runtime")]


@pytest.fixture
def docker_host(monkeypatch: pytest.MonkeyPatch) -> Callable[[Mapping[str, object]], list[str]]:
    """Stub the ``_docker_info`` seam with a host whose Runtimes are given.

    Returns the list of templates queried, so a test can see whether Docker was probed.
    """
    queries: list[str] = []
    monkeypatch.setattr(SecurityConfig, "_docker_runtimes", None)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")

    def install(runtimes: Mapping[str, object]) -> list[str]:
        def docker_info(template: str) -> str:
            queries.append(template)
            assert template == "{{json .Runtimes}}"
            return json.dumps(runtimes) + "\n"

        monkeypatch.setattr(SecurityConfig, "_docker_info", staticmethod(docker_info))
        return queries

    return install


class TestResolveRuntime:
    """One resolver: explicit runtimes fail closed, auto keeps today's default."""

    def test_kata_registered_emits_its_alias(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host({**RUNC, **KATA})
        config = SecurityConfig(runtime=WorkspaceRuntime.KATA)

        assert _runtime_args(config.to_docker_run_args()) == ["--runtime=kata"]
        assert config.resolve_runtime() == ResolvedRuntime(
            requested=WorkspaceRuntime.KATA,
            effective=WorkspaceRuntime.KATA,
            alias="kata",
            runtime_path="/opt/kata/bin/kata-runtime",
        )

    def test_runtime_type_only_kata_is_unverifiable_and_raises(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host(_docker_info_runtimes({"kata": {"runtimeType": "io.containerd.kata.v2"}}))
        config = SecurityConfig(runtime=WorkspaceRuntime.KATA)

        with pytest.raises(DockerDetectionError, match=r"kata \(no path\).*cannot be verified"):
            config.resolve_runtime()

    @pytest.mark.parametrize(
        "path", ["/usr/bin/not-kata-runtime", "/usr/bin/runc-kata", "io.containerd.notkata.v2"]
    )
    def test_names_merely_containing_kata_raise(
        self, docker_host: Callable[[Mapping[str, object]], list[str]], path: str
    ) -> None:
        docker_host({**RUNC, "kata": {"path": path}})

        with pytest.raises(DockerDetectionError, match="refusing to fall back to runc"):
            SecurityConfig(runtime=WorkspaceRuntime.KATA).resolve_runtime()

    @pytest.mark.parametrize(
        "path",
        [
            "/usr/bin/kata-runtime",
            "/opt/kata/bin/containerd-shim-kata-v2",
            "/opt/kata/bin/containerd-shim-kata-qemu-v2",
            "/opt/kata/bin/containerd-shim-kata-clh-v2",
            "/opt/kata/bin/containerd-shim-kata-fc-v2",
        ],
    )
    def test_kata_binary_and_vmm_shims_are_recognised(
        self, docker_host: Callable[[Mapping[str, object]], list[str]], path: str
    ) -> None:
        docker_host({**RUNC, "vm": {"path": path}})

        resolved = SecurityConfig(runtime=WorkspaceRuntime.KATA).resolve_runtime()

        assert (resolved.effective, resolved.alias, resolved.runtime_path) == (
            WorkspaceRuntime.KATA,
            "vm",
            path,
        )

    def test_kata_under_a_custom_alias_is_found_by_its_shim(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host({**RUNC, "kata-clh": {"path": "/opt/kata/bin/containerd-shim-kata-v2"}})
        config = SecurityConfig(runtime=WorkspaceRuntime.KATA)

        assert _runtime_args(config.to_docker_run_args()) == ["--runtime=kata-clh"]

    def test_kata_missing_raises(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host(RUNC)
        config = SecurityConfig(runtime=WorkspaceRuntime.KATA)

        with pytest.raises(DockerDetectionError, match="refusing to fall back to runc"):
            config.to_docker_run_args()

    def test_alias_named_kata_that_runs_runc_raises(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host({**RUNC, "kata": {"path": "/usr/bin/runc"}})
        config = SecurityConfig(runtime=WorkspaceRuntime.KATA)

        with pytest.raises(DockerDetectionError, match=r"kata \(/usr/bin/runc\)"):
            config.resolve_runtime()

    def test_gvisor_missing_raises_at_resolution(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host(RUNC)
        config = SecurityConfig(runtime=WorkspaceRuntime.GVISOR)

        with pytest.raises(DockerDetectionError, match="runtime gvisor was requested"):
            config.to_docker_run_args()

    def test_explicit_runtime_without_docker_cli_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(SecurityConfig, "_docker_runtimes", None)
        monkeypatch.setattr("shutil.which", lambda _name: None)

        with pytest.raises(DockerDetectionError, match="docker CLI not found"):
            SecurityConfig(runtime=WorkspaceRuntime.SYSBOX).resolve_runtime()

    def test_sysbox_registered_emits_its_alias(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host({**RUNC, "sysbox-runc": {"path": "/usr/bin/sysbox-runc"}})
        config = SecurityConfig(runtime=WorkspaceRuntime.SYSBOX)

        assert _runtime_args(config.to_docker_run_args()) == ["--runtime=sysbox-runc"]

    def test_explicit_runc_never_probes_docker(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        queries = docker_host({**RUNC, **RUNSC})
        config = SecurityConfig(runtime=WorkspaceRuntime.RUNC)

        assert _runtime_args(config.to_docker_run_args()) == ["--runtime=runc"]
        assert queries == []

    @pytest.mark.parametrize("runtime", list(WorkspaceRuntime))
    def test_every_explicit_runtime_is_named_in_docker_run_args(
        self, docker_host: Callable[[Mapping[str, object]], list[str]], runtime: WorkspaceRuntime
    ) -> None:
        # One host with every runtime registered, so only the request decides.
        # A new WorkspaceRuntime fails here until it is given its alias.
        alias = {
            WorkspaceRuntime.RUNC: "runc",
            WorkspaceRuntime.GVISOR: "runsc",
            WorkspaceRuntime.KATA: "kata",
            WorkspaceRuntime.SYSBOX: "sysbox-runc",
        }[runtime]
        docker_host({**RUNC, **RUNSC, **KATA, "sysbox-runc": {"path": "/usr/bin/sysbox-runc"}})
        config = SecurityConfig(runtime=runtime)

        assert _runtime_args(config.to_docker_run_args()) == [f"--runtime={alias}"]
        assert config.resolve_runtime().alias == alias

    # Regression guards: auto mode is today's default and passed before AW-1 too.

    def test_auto_on_runc_only_host_emits_runtime_runc(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host(RUNC)
        config = SecurityConfig()

        assert _runtime_args(config.to_docker_run_args()) == ["--runtime=runc"]
        resolved = config.resolve_runtime()
        assert resolved.requested is None
        assert resolved.effective is WorkspaceRuntime.RUNC

    def test_auto_with_runsc_registered_uses_runsc(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host({**RUNC, **RUNSC})
        config = SecurityConfig()

        assert _runtime_args(config.to_docker_run_args()) == ["--runtime=runsc"]
        resolved = config.resolve_runtime()
        assert resolved.requested is None
        assert resolved.effective is WorkspaceRuntime.GVISOR

    def test_auto_ignores_kata(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host({**RUNC, **KATA})

        assert _runtime_args(SecurityConfig().to_docker_run_args()) == ["--runtime=runc"]

    def test_auto_with_runtime_type_only_runsc_uses_runsc(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        # Docker's documented gVisor setup; docker info reports it as {"runsc": {}}.
        docker_host(_docker_info_runtimes({"runsc": {"runtimeType": "io.containerd.runsc.v1"}}))
        config = SecurityConfig()

        assert _runtime_args(config.to_docker_run_args()) == ["--runtime=runsc"]
        assert config.resolve_runtime() == ResolvedRuntime(
            requested=None,
            effective=WorkspaceRuntime.GVISOR,
            alias="runsc",
            runtime_path="",
        )

    def test_auto_with_runsc_alias_running_runc_is_runc(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host({**RUNC, "runsc": {"path": "/usr/bin/runc"}})

        assert SecurityConfig().resolve_runtime().effective is WorkspaceRuntime.RUNC
        # The builtin runc, not the misleading "runsc" alias.
        assert _runtime_args(SecurityConfig().to_docker_run_args()) == ["--runtime=runc"]

    def test_auto_without_docker_cli_is_runc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(SecurityConfig, "_docker_runtimes", None)
        monkeypatch.setattr("shutil.which", lambda _name: None)

        resolved = SecurityConfig().resolve_runtime()

        assert resolved.effective is WorkspaceRuntime.RUNC
        assert resolved.requested is None

    def test_gvisor_registered_as_gvisor_emits_that_alias(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        # Before AW-1 this host got --runtime=runsc, which Docker rejects.
        docker_host({**RUNC, "gvisor": {"path": "/usr/local/bin/runsc"}})

        assert _runtime_args(SecurityConfig().to_docker_run_args()) == ["--runtime=gvisor"]

    def test_several_unconventional_aliases_are_ambiguous(
        self, docker_host: Callable[[Mapping[str, object]], list[str]]
    ) -> None:
        docker_host(
            {
                **RUNC,
                "kata-clh": {"path": "/opt/kata/bin/containerd-shim-kata-clh-v2"},
                "kata-qemu": {"path": "/opt/kata/bin/containerd-shim-kata-qemu-v2"},
            }
        )

        with pytest.raises(DockerDetectionError, match="kata-clh, kata-qemu"):
            SecurityConfig(runtime=WorkspaceRuntime.KATA).resolve_runtime()

    def test_failed_docker_info_raises_and_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed docker info is an error, never "runc", and is not cached."""
        monkeypatch.setattr(SecurityConfig, "_docker_runtimes", None)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
        answers: list[str | DockerDetectionError] = [
            DockerDetectionError("daemon down"),
            json.dumps({**RUNC, **RUNSC}),
        ]

        def docker_info(_template: str) -> str:
            answer = answers.pop(0)
            if isinstance(answer, DockerDetectionError):
                raise answer
            return answer

        monkeypatch.setattr(SecurityConfig, "_docker_info", staticmethod(docker_info))

        with pytest.raises(DockerDetectionError, match="daemon down"):
            SecurityConfig().resolve_runtime()
        assert SecurityConfig._docker_runtimes is None
        assert SecurityConfig().resolve_runtime().effective is WorkspaceRuntime.GVISOR

    def test_unreadable_runtimes_raise(
        self,
        docker_host: Callable[[Mapping[str, object]], list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(SecurityConfig, "_docker_info", staticmethod(lambda _t: "<no value>"))

        with pytest.raises(DockerDetectionError, match="unreadable Runtimes"):
            SecurityConfig().resolve_runtime()
