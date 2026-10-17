"""Docker container provider for production isolation.

This provider creates Docker containers for isolated execution,
providing real process and filesystem isolation with security hardening.

Features:
- Security hardening (cap-drop, no-new-privileges, read-only root)
- OCI runtime selection (runc, gVisor, Kata, Sysbox; gVisor auto-detected)
- Real-time stdout streaming
- Resource limits (memory, CPU, pids)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import posixpath
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from agentic_isolation.config import (
    CODEX_IMAGE_LABEL,
    AppArmorProfileNotLoadedError,
    SecurityConfig,
    WorkspaceConfig,
    is_apparmor_profile_error,
)
from agentic_isolation.contract import (
    FinalizationOutcome,
    ObservationPage,
    OperationError,
    OperationRef,
    OperationSpec,
    OperationStatus,
    ProviderDescriptor,
    WorkspaceRef,
)
from agentic_isolation.harnesses import ExecFn, TranscriptSource, get_harness
from agentic_isolation.providers import docker_capabilities
from agentic_isolation.providers.base import (
    BaseProvider,
    CredentialResolver,
    ExecuteResult,
    UnsupportedLimitError,
    Workspace,
    drain_operation,
    resolve_env_credentials,
)
from agentic_isolation.providers.capabilities import (
    OwnedWorkspace,
    StreamSession,
    TransferRoot,
    WorkspaceNotFoundError,
    WorkspaceOwnershipError,
    mismatched_labels,
)
from agentic_isolation.providers.contained_files import (
    NotARegularFileError,
    _open_contained,
)
from agentic_isolation.providers.docker_operations import DockerOperations
from agentic_isolation.providers.operation_spool import OperationFailure
from agentic_isolation.providers.teardown_report import (
    DeleteFallback,
    TeardownReport,
    TeardownUsage,
)

# Security tmpfs targets that an explicit mount may never replace or sit beneath:
# /home/agent holds harness configuration and credentials, /tmp and /var/agentic
# hold per-run scratch and capability state. Only the session spool is meant to
# outlive the container (docs/persistent-workspace-mounts.md).
PROTECTED_TMPFS_TARGETS = (
    PurePosixPath("/home/agent"),
    PurePosixPath("/tmp"),
    PurePosixPath("/var/agentic"),
)
PERSISTABLE_TMPFS_TARGET = PurePosixPath("/spool")

logger = logging.getLogger(__name__)

# Default network for workspace containers
DEFAULT_NETWORK = "agentic-workspace-net"


class WorkspaceDockerProvider(BaseProvider):
    """Production-grade Docker workspace provider.

    Creates isolated Docker containers with security hardening for agent execution.
    Supports real-time stdout streaming for observability.

    Security features (enabled by default):
    - --cap-drop=ALL: Remove all Linux capabilities
    - --security-opt=no-new-privileges: Block privilege escalation
    - --read-only: Immutable root filesystem
    - --tmpfs: Writable /tmp and /home/agent in memory
    - --pids-limit: Process count limit
    - --runtime=<alias>: the resolved runtime, always named (gVisor if available)

    Usage:
        provider = WorkspaceDockerProvider()
        workspace = await provider.create(config)

        # Execute with full output
        result = await provider.execute(workspace, "echo hello")

        # Stream output in real-time
        async for line in provider.stream(workspace, ["python", "-u", "agent.py"]):
            print(line)

        await provider.destroy(workspace)
    """

    def __init__(
        self,
        *,
        default_image: str = "python:3.12-slim",
        default_network: str = DEFAULT_NETWORK,
        security: SecurityConfig | None = None,
        workspace_base_dir: Path | str | None = None,
        workspace_host_dir: Path | str | None = None,
        credentials: CredentialResolver | None = None,
        observation_retention_bytes: int = 64 << 20,
        observation_max_page_bytes: int = 4 << 20,
    ):
        """Initialize Docker provider.

        Args:
            default_image: Default Docker image for workspaces
            default_network: Docker network for containers
            security: Security configuration (defaults to production)
            workspace_base_dir: Base directory for workspace files (this process)
            workspace_host_dir: Base directory for Docker volume mounts (host path).
                Required when running inside a container (Docker-in-Docker).
                If not set, uses workspace_base_dir for mounts.
            credentials: Resolves `WorkspaceConfig.credentials` and
                `OperationSpec.credential_bindings`. Without one, a config or
                spec that binds a credential is refused.
            observation_retention_bytes: Output retained per operation.
            observation_max_page_bytes: Largest serialised observation page.
        """
        self._default_image = default_image
        self._default_network = default_network
        self._security = security or SecurityConfig.production()
        self._workspace_base_dir = (
            Path(workspace_base_dir).resolve() if workspace_base_dir else None
        )
        # Host dir is used for Docker -v mounts and must refer to the Docker
        # *host* filesystem, not this process's filesystem.  When running inside
        # a container (DinD), resolve() would map to the container's CWD — wrong.
        self._workspace_host_dir = Path(workspace_host_dir) if workspace_host_dir else None
        self._credentials = credentials
        self._workspaces: dict[str, Workspace] = {}
        # Workspaces whose container is gone but whose directory is kept,
        # keyed by container name (the `WorkspaceRef.resource_id`). An entry
        # lives from container removal until the directory is deleted, so a
        # `retained` ref resolves for as long as there is data to retry with.
        self._retained: dict[str, _Retained] = {}
        # Container output captured during teardown, keyed by container name.
        # Insertion-ordered so eviction is oldest-first. See _remember_logs.
        self._log_snapshots: dict[str, str] = {}
        # One teardown at a time per workspace. Concurrent teardowns would
        # otherwise race, and one could delete a directory another is still
        # archiving from.
        self._teardown_locks: dict[str, _TeardownLock] = {}
        self._lock = asyncio.Lock()
        self._operations = DockerOperations(
            credentials=credentials,
            retention_bytes=observation_retention_bytes,
            max_page_bytes=observation_max_page_bytes,
        )

    @property
    def name(self) -> str:
        """Provider name."""
        return "docker"

    async def _inspect_image(self, image: str) -> tuple[str, dict[str, str]]:
        """The immutable image ID and labels of ``image``; pulls once if absent.

        The caller launches by the returned ID, so the label that decided the
        security policy belongs to exactly the image that runs, even if the
        tag is moved between inspect and ``docker run``. Fails closed: an image
        whose labels cannot be read is not launched.
        """
        for attempt in range(2):
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "image",
                "inspect",
                "--format",
                # Not `.Config.Labels`: some Docker versions omit the key for
                # unlabelled images, which makes that template fail.
                "{{.Id}} {{json .Config}}",
                image,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                image_id, _, config_json = stdout.decode().strip().partition(" ")
                config: object = json.loads(config_json or "null")
                labels: object = config.get("Labels") if isinstance(config, dict) else None
                if not image_id.startswith("sha256:") or not (
                    labels is None or isinstance(labels, dict)
                ):
                    raise RuntimeError(f"Unreadable inspect output for image {image}")
                return image_id, {str(key): str(value) for key, value in (labels or {}).items()}
            if attempt == 0:
                pull = await asyncio.create_subprocess_exec(
                    "docker",
                    "pull",
                    "--quiet",
                    image,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await pull.communicate()
        raise RuntimeError(
            f"Cannot inspect image {image} to derive its security policy: "
            f"{stderr.decode(errors='replace').strip()[:300]}"
        )

    @staticmethod
    def is_available() -> bool:
        """Check if Docker is available."""
        return shutil.which("docker") is not None

    async def create(self, config: WorkspaceConfig) -> Workspace:
        """Create a Docker container workspace with security hardening."""
        # Docker run has no portable per-container disk quota (--storage-opt
        # works only on some storage drivers), so a hard disk limit is refused
        # rather than accepted and silently ignored (APSS W-3.8-d).
        if config.limits.disk is not None:
            raise UnsupportedLimitError(
                f"disk limit {config.limits.disk!r} cannot be enforced by the docker "
                "provider; remove ResourceLimits.disk"
            )

        # Resolve plugin env vars before creating container
        if config.plugins:
            config.resolve_plugin_env()

        # Resolved before anything is created; a refused binding leaves nothing.
        credential_env = await resolve_env_credentials(
            config.credentials, self._credentials, provider=self.name
        )

        # The Codex sandbox policy comes from the image's own declaration, and
        # is settled before anything is created so a mismatch leaves nothing.
        image = config.image or self._default_image
        image_id, labels = await self._inspect_image(image)
        security = (config.security or self._security).resolve_for_image(
            bool(labels.get(CODEX_IMAGE_LABEL))
        )
        # Before anything is created: an unregistered runtime is refused here.
        runtime = security.resolve_runtime()

        short_id = uuid.uuid4().hex[:8]
        workspace_id = f"ws-{short_id}"
        container_name = f"agentic-ws-{short_id}"

        # Create workspace directory for file I/O
        if self._workspace_base_dir:
            self._workspace_base_dir.mkdir(parents=True, exist_ok=True)
            workspace_dir = self._workspace_base_dir / workspace_id
        else:
            workspace_dir = Path(tempfile.mkdtemp(prefix=f"agentic-ws-{short_id}-"))
        workspace_dir.mkdir(parents=True, exist_ok=True)
        # The hardened container has no DAC override capability and images may
        # run as a UID different from the host caller. The random per-workspace
        # directory is therefore writable by the container identity.
        os.chmod(workspace_dir, 0o777)

        # Determine host path for Docker volume mount
        # When running Docker-in-Docker, container path != host path
        if self._workspace_host_dir:
            host_mount_dir = self._workspace_host_dir / workspace_id
        else:
            host_mount_dir = workspace_dir

        # Ensure network exists
        await self._ensure_network(self._default_network)

        # Build docker run command
        cmd = self._build_run_command(
            container_name=container_name,
            workspace_id=workspace_id,
            workspace_dir=host_mount_dir,  # Use HOST path for volume mount
            image=image_id,  # the inspected image, not whatever the tag is now
            config=config,
            security=security,
            credential_names=tuple(credential_env),
        )

        logger.info(
            "Creating workspace container (id=%s, image=%s, runtime requested=%s "
            "effective=%s alias=%s)",
            workspace_id,
            image,
            runtime.requested or "auto",
            runtime.effective,
            runtime.alias,
        )

        try:
            # Credential values reach docker through its own environment;
            # `-e NAME` makes the CLI copy them, so no value is in the argv.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **credential_env} if credential_env else None,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                detail = stderr.decode(errors="replace").strip()
                if not detail:
                    detail = stdout.decode(errors="replace").strip()
                if not detail:
                    detail = "no output on stderr or stdout"
                if security.apparmor_profile is not None and is_apparmor_profile_error(detail):
                    raise AppArmorProfileNotLoadedError(security.apparmor_profile)
                raise RuntimeError(
                    f"Failed to create container: {detail} (docker create exited {proc.returncode})"
                )

            container_id = stdout.decode().strip()

            # Wait for container to be running
            await self._wait_for_running(container_name)

            workspace = Workspace(
                id=workspace_id,
                provider=self.name,
                path=Path(config.working_dir),
                config=config,
                metadata={
                    "container_id": container_id,
                    "container_name": container_name,
                    "image": image,
                    "image_id": image_id,
                    "workspace_dir": str(workspace_dir),
                    "runtime_requested": str(runtime.requested or "auto"),
                    "runtime_effective": str(runtime.effective),
                    "runtime_alias": runtime.alias,
                },
                _handle=container_name,  # Use name for docker exec
            )

            async with self._lock:
                self._workspaces[workspace_id] = workspace

            logger.info("Container created (id=%s, container=%s)", workspace_id, container_name)
            return workspace

        except Exception as e:
            logger.exception("Failed to create container: %s", e)
            await self._cleanup_container(container_name)
            shutil.rmtree(workspace_dir, ignore_errors=True)
            raise

    def _build_run_command(
        self,
        *,
        container_name: str,
        workspace_id: str,
        workspace_dir: Path,
        image: str,
        config: WorkspaceConfig,
        security: SecurityConfig,
        credential_names: tuple[str, ...] = (),
    ) -> list[str]:
        """Build docker run command with security hardening.

        `credential_names` are passed as bare `-e NAME`; their values must be
        in the docker client's environment, never in this argv.
        """
        cmd = [
            "docker",
            "run",
            "-d",  # Detached
            f"--name={container_name}",
            f"--network={self._default_network}",
        ]

        # Security hardening
        # An explicit persistent mount replaces the default tmpfs at that path.
        # Docker otherwise accepts both and the tmpfs hides the durable volume.
        mount_targets = {mount.to_docker_mount()["target"] for mount in config.mounts}
        for target in mount_targets:
            target_path = PurePosixPath(target)
            for protected in PROTECTED_TMPFS_TARGETS:
                if target_path == protected or protected in target_path.parents:
                    raise ValueError(
                        f"Mount target {target} would persist the protected tmpfs "
                        f"{protected}; only {PERSISTABLE_TMPFS_TARGET} may be replaced"
                    )
        cmd.extend(
            arg
            for arg in security.to_docker_run_args()
            if not (
                arg.startswith("--tmpfs=")
                and arg.partition("=")[2].partition(":")[0] in mount_targets
            )
        )

        # Resource limits
        limits = config.limits
        cmd.append(f"--memory={limits.memory}")
        cmd.append(f"--cpus={limits.cpu}")

        # Network isolation (if limits.network is False)
        if hasattr(limits, "network") and limits.network is False:
            cmd.append("--network=none")

        # Workspace mount
        cmd.append(f"-v={workspace_dir}:/workspace:rw")
        cmd.append("-w=/workspace")
        targets = {"/workspace"}
        for mount in config.mounts:
            target = mount.to_docker_mount()["target"]
            if target in targets:
                raise ValueError(f"Duplicate Docker mount target: {target}")
            targets.add(target)
            cmd.extend(["--mount", mount.to_docker_run_arg()])

        # Environment variables
        env_vars = {
            "WORKSPACE_ID": workspace_id,
            **config.checked_environment(),
        }
        for key, value in env_vars.items():
            cmd.extend(["-e", f"{key}={value}"])
        for name in credential_names:
            cmd.extend(["-e", name])

        # Labels
        cmd.extend(
            [
                f"--label=agentic.workspace.id={workspace_id}",
                f"--label=agentic.provider={self.name}",
            ]
        )
        for key, value in config.labels.items():
            # The internal labels are the workspace's identity: attach
            # authorises by them, so a caller may not overwrite them.
            if key in (
                docker_capabilities.WORKSPACE_ID_LABEL,
                docker_capabilities.PROVIDER_LABEL,
            ):
                raise ValueError(f"Docker label {key!r} is reserved for the provider")
            # Validate label values to prevent command injection
            label_value = str(value)
            if "\n" in label_value or "\r" in label_value:
                raise ValueError(
                    f"Invalid Docker label value for key {key!r}: "
                    "label values must not contain newline characters"
                )
            cmd.append(f"--label={key}={label_value}")

        # Image and keep-alive command
        cmd.append(image)
        cmd.extend(["sleep", "infinity"])

        return cmd

    async def destroy(self, workspace: Workspace) -> TeardownReport:
        """Stop and remove the container, then delete the host workspace.

        The unhooked case of `teardown()`, so the two cannot drift into
        different orderings or different cleanup.
        """
        logger.info("Destroying workspace (id=%s)", workspace.id)
        return await self.teardown(workspace)

    #: Cap on returned log bytes. `docker logs --tail` bounds LINES, and a
    #: single agent-controlled line can be arbitrarily long, so lines are not a
    #: resource bound. Keep the TAIL of the output: the finalizer verdict is
    #: printed last, during shutdown.
    _MAX_LOG_BYTES = 256 * 1024

    #: How many teardown snapshots to retain. Bounded so a long-lived provider
    #: cannot accumulate log text for every container it has ever destroyed.
    _MAX_LOG_SNAPSHOTS = 64

    async def logs(self, workspace: Workspace, *, tail: int = 200) -> str:
        """Return the container's combined stdout and stderr (best effort).

        Implements `SupportsWorkspaceLogs`. Does not raise for any operational
        failure to retrieve logs: it is called during teardown, where the
        container may already be gone, and a caller reading a capture verdict
        must not be able to fail the teardown that produced it. Every such path
        yields "". `asyncio.CancelledError` DOES propagate, because swallowing
        cancellation would make shutdown hang.

        If the container was destroyed through this provider, the snapshot
        taken between `docker stop` and `docker rm` is returned instead of
        querying a container that no longer exists. Without that, the finalizer
        verdict would be unreachable by construction: it is printed during stop
        and the container is removed immediately afterwards.

        The returned text is UNTRUSTED - it is whatever the agent and its
        tooling printed - so callers must parse defensively and must not print
        it to a terminal unsanitised. It is also FORGEABLE: the agent usually
        runs as the same user as the finalizer, so a success line read from
        here is evidence, not proof. See SupportsWorkspaceLogs.
        """
        container_name = getattr(workspace, "_handle", None)
        if not container_name:
            return ""

        snapshot = self._log_snapshots.get(container_name)
        if snapshot is not None:
            return snapshot

        return await self._read_container_logs(container_name, tail=tail)

    async def _read_container_logs(self, container_name: str, *, tail: int) -> str:
        """`docker logs` with every operational failure collapsed to "".

        Split out so teardown can reuse it while the container still exists.
        """
        # A negative or absurd tail asks docker for the entire history, which
        # defeats the byte cap's purpose of bounding work as well as memory.
        safe_tail = max(1, min(int(tail), 10_000))

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "logs",
                "--tail",
                str(safe_tail),
                container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            # Drain incrementally into a bounded buffer rather than calling
            # communicate(), which would materialise the WHOLE response first.
            # --tail bounds lines, and one agent-controlled line can be
            # arbitrarily long, so buffering everything and truncating
            # afterwards bounds the RESULT while leaving memory unbounded -
            # which during destroy() means an agent could kill the
            # orchestrator on its way out.
            out = await asyncio.wait_for(self._drain_tail(proc.stdout), timeout=10)
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.CancelledError:
            # Shutdown must not be blocked by a best-effort log read.
            if proc is not None:
                await self._terminate(proc)
            raise
        except (TimeoutError, OSError, ValueError) as exc:
            logger.debug("docker logs failed for %s: %s", container_name, exc)
            if proc is not None:
                await self._terminate(proc)
            return ""
        except Exception as exc:  # noqa: BLE001 - contract is "never fail teardown"
            logger.debug("docker logs raised unexpectedly for %s: %s", container_name, exc)
            if proc is not None:
                await self._terminate(proc)
            return ""

        if proc.returncode != 0:
            # Expected once the container has been removed. Debug, not warning:
            # a teardown race here is normal and not actionable.
            logger.debug("docker logs exited %s for %s", proc.returncode, container_name)
            return ""

        # NOTE: _MAX_LOG_BYTES bounds the SOURCE bytes retained. Decoding with
        # errors="replace" turns each invalid byte into U+FFFD, so the decoded
        # string can re-encode larger than the cap. That is acceptable: the
        # point is bounding what is read and held, not the exact size of the
        # returned str.
        return out.decode(errors="replace")

    @classmethod
    async def _drain_tail(cls, stream: asyncio.StreamReader | None) -> bytes:
        """Read to EOF keeping only the last `_MAX_LOG_BYTES` bytes.

        The finalizer verdict is printed last, during shutdown, so the tail is
        the part worth keeping.
        """
        if stream is None:
            return b""
        buf = bytearray()
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            buf += chunk
            excess = len(buf) - cls._MAX_LOG_BYTES
            if excess > 0:
                del buf[:excess]
        return bytes(buf)

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        """Kill a subprocess without letting cleanup raise or hang."""
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            return  # already exited
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, OSError):
            logger.debug("gave up waiting for a killed docker subprocess")

    def _remember_logs(self, container_name: str, text: str) -> None:
        """Retain a teardown snapshot, evicting oldest first."""
        if not text:
            return
        self._log_snapshots[container_name] = text
        while len(self._log_snapshots) > self._MAX_LOG_SNAPSHOTS:
            self._log_snapshots.pop(next(iter(self._log_snapshots)))

    def _build_docker_exec_cmd(
        self,
        container_name: str,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        interactive: bool = False,
    ) -> list[str]:
        """Build a docker exec command with optional cwd and env."""
        exec_cmd = ["docker", "exec"]
        if interactive:
            exec_cmd.append("-i")

        exec_cmd.extend(["-w", cwd or "/workspace"])

        if env:
            for key, value in env.items():
                exec_cmd.extend(["-e", f"{key}={value}"])

        exec_cmd.append(container_name)
        exec_cmd.extend(command)
        return exec_cmd

    async def execute(
        self,
        workspace: Workspace,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecuteResult:
        """Execute a command in the Docker container, via the operation contract.

        `stdout` and `stderr` are the workload's own channels, captured apart
        at the client, while the operation's observation stream carries both.
        `env` values are delivered like credentials: by name on the client
        argv, value only in the client's environment.
        """
        if not workspace._handle:
            return ExecuteResult(
                exit_code=-1, stdout="", stderr="Container not available", duration_ms=0
            )
        start_time = time.perf_counter()
        spec = OperationSpec(
            execution_id=workspace.id,
            operation_key=f"execute-{uuid.uuid4().hex}",
            argv=("sh", "-c", command),
            working_directory=_relative_workdir(cwd),
            environment={},
            credential_bindings=(),
            timeout_seconds=max(1, math.ceil(timeout or workspace.config.limits.timeout_seconds)),
            metadata={},
        )
        try:
            ref = await self._operations.start(
                workspace, spec, delivered_env=env, separate_channels=True
            )
            status, _ = await drain_operation(self, ref)
            out, err = self._operations.channels(ref)
            # The key is private to this call, so no replay can need the record.
            self._operations.release(ref)
        except OperationFailure as e:
            return ExecuteResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                duration_ms=(time.perf_counter() - start_time) * 1000,
            )
        duration_ms = (time.perf_counter() - start_time) * 1000
        stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
        if status.state == "timed_out":
            return ExecuteResult(
                exit_code=-1,
                stdout=stdout,
                stderr=f"{stderr}Command timed out",
                duration_ms=duration_ms,
                timed_out=True,
            )
        exit_code = status.exit_code if isinstance(status.exit_code, int) else -1
        return ExecuteResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr
            if status.state in ("succeeded", "failed")
            else f"operation {status.state}",
            duration_ms=duration_ms,
        )

    def describe(self) -> ProviderDescriptor:
        """SupportsOperations: published before any launch."""
        return self._operations.describe()

    async def start(
        self, workspace: Workspace, spec: OperationSpec, *, redact: Collection[str] = ()
    ) -> OperationRef:
        """SupportsOperations: accept `spec`; idempotent per operation_key."""
        return await self._operations.start(workspace, spec, redact=redact)

    async def status(self, ref: OperationRef) -> OperationStatus:
        """SupportsOperations: status from the daemon-reported exit code."""
        return await self._operations.status(ref)

    async def observe(
        self,
        ref: OperationRef,
        *,
        cursor: str | None,
        max_bytes: int,
        wait_seconds: float = 0,
    ) -> ObservationPage:
        """SupportsOperations: a page of the host-side spool."""
        return await self._operations.observe(
            ref, cursor=cursor, max_bytes=max_bytes, wait_seconds=wait_seconds
        )

    async def cancel(self, ref: OperationRef, *, grace_seconds: float) -> OperationStatus:
        """SupportsOperations: group TERM then KILL, confirmed, else container stop."""
        return await self._operations.cancel(ref, grace_seconds=grace_seconds)

    async def stream(
        self,
        workspace: Workspace,
        command: list[str],
        *,
        timeout_seconds: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Stream stdout lines from command execution in real-time.

        This is the key method for observability - yields lines as they
        are produced, enabling real-time dashboard updates.

        ARCHITECTURE NOTE — stderr=STDOUT is intentional (ADR-043):
        -----------------------------------------------------------------
        Git observability hooks (post-commit, pre-push, etc.) emit JSONL event
        lines to STDERR. Using stderr=STDOUT merges them into stdout so callers
        receive both Claude's stream-json output AND git hook JSONL on one pipe.
        parse_jsonl_line() distinguishes them: hook events have "event_type",
        Claude stream-json has "type".

        NOTE: This class (WorkspaceDockerProvider) is used in agentic_isolation
        contexts. The dashboard's primary docker exec path is in:
          syn_adapters/workspace_backends/agentic/adapter.py → AgenticEventStreamAdapter.stream()
        Both must keep stderr=STDOUT. Do not revert to PIPE or DEVNULL.
        """
        container_name = workspace._handle
        if not container_name:
            raise RuntimeError("Container not available")

        exec_cmd = self._build_docker_exec_cmd(
            container_name,
            command,
            cwd=cwd,
            env=env,
            interactive=True,
        )

        logger.debug("Starting stream (container=%s, cmd=%s)", container_name, command)

        # stderr=STDOUT: merge stderr so git hook JSONL events reach the engine.
        # See docstring above for rationale. Do NOT change to PIPE or DEVNULL.
        proc = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async for line in self._read_stream_lines(proc, timeout_seconds):
            yield line

    async def start_stream(
        self,
        workspace: Workspace,
        sandbox_argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> StreamSession:
        """SupportsStreamingExec: merged stdout+stderr, with an outcome.

        Additive beside `stream()`, which keeps its behaviour and callers.
        """
        container_name = workspace._handle
        if not container_name:
            raise RuntimeError("Container not available")
        return await docker_capabilities.DockerStreamSession.start(
            container_name, sandbox_argv, cwd=cwd, env=env, timeout=timeout
        )

    async def read_files(
        self,
        workspace: Workspace,
        patterns: Sequence[str],
        *,
        base_path: TransferRoot,
    ) -> list[tuple[str, bytes]]:
        """SupportsFileTransfer: exact bytes, from inside the container."""
        container_name = workspace._handle
        if not container_name:
            raise RuntimeError("Container not available")
        return await docker_capabilities.read_files(container_name, patterns, base_path)

    async def list_owned(self, labels: Mapping[str, str]) -> list[OwnedWorkspace]:
        """SupportsOwnership: this provider's workspaces carrying all of `labels`."""
        return await docker_capabilities.list_owned(self.name, labels)

    async def attach(self, workspace_id: str, *, expect_labels: Mapping[str, str]) -> Workspace:
        """SupportsOwnership: a handle to an existing workspace, labels checked first."""
        found = await docker_capabilities.find_containers(
            self.name, {docker_capabilities.WORKSPACE_ID_LABEL: workspace_id}
        )
        if not found:
            raise WorkspaceNotFoundError(f"no docker workspace {workspace_id}")
        # The labels checked must be those of the container returned, so an
        # id that names more than one container cannot be attached at all.
        if len(found) > 1:
            raise WorkspaceOwnershipError(workspace_id, [docker_capabilities.WORKSPACE_ID_LABEL])
        container_name, found_labels = found[0]
        mismatched = mismatched_labels(found_labels, expect_labels)
        if mismatched:
            raise WorkspaceOwnershipError(workspace_id, mismatched)
        async with self._lock:
            cached = self._workspaces.get(workspace_id)
        if cached is not None:
            if cached._handle != container_name:
                raise WorkspaceOwnershipError(
                    workspace_id, [docker_capabilities.WORKSPACE_ID_LABEL]
                )
            return cached
        source = await self._docker_read(
            "inspect",
            "--format",
            '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}',
            container_name,
        )
        if self._workspace_base_dir:
            workspace_dir = str(self._workspace_base_dir / workspace_id)
        else:
            workspace_dir = (source or "").strip()
        labels = {
            key: value
            for key, value in found_labels.items()
            if key
            not in (docker_capabilities.WORKSPACE_ID_LABEL, docker_capabilities.PROVIDER_LABEL)
        }
        workspace = Workspace(
            id=workspace_id,
            provider=self.name,
            path=Path("/workspace"),
            config=WorkspaceConfig(provider=self.name, labels=labels),
            metadata={
                "container_name": container_name,
                "workspace_dir": workspace_dir,
                "attached": True,
            },
            _handle=container_name,
        )
        async with self._lock:
            return self._workspaces.setdefault(workspace_id, workspace)

    async def write_file(
        self,
        workspace: Workspace,
        path: str,
        content: str | bytes,
    ) -> None:
        """Write a file in the Docker container via mounted volume."""
        workspace_dir = workspace.metadata.get("workspace_dir")
        if not workspace_dir:
            raise RuntimeError("Workspace directory not available")

        data = content.encode() if isinstance(content, str) else content
        fd = _open_contained(
            Path(workspace_dir), path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, create_parents=True
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)

    async def read_file(
        self,
        workspace: Workspace,
        path: str,
    ) -> str:
        """Read a file from the Docker container via mounted volume."""
        workspace_dir = workspace.metadata.get("workspace_dir")
        if not workspace_dir:
            raise RuntimeError("Workspace directory not available")

        try:
            fd = _open_contained(Path(workspace_dir), path, os.O_RDONLY)
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {path}") from None
        with os.fdopen(fd, "rb") as handle:
            return handle.read().decode()

    async def file_exists(
        self,
        workspace: Workspace,
        path: str,
    ) -> bool:
        """Whether a file or directory exists here; a FIFO, socket or device does not."""
        workspace_dir = workspace.metadata.get("workspace_dir")
        if not workspace_dir:
            return False

        try:
            fd = _open_contained(Path(workspace_dir), path, os.O_RDONLY)
        except (FileNotFoundError, NotARegularFileError):
            return False
        os.close(fd)
        return True

    def transcript_source(self, workspace: Workspace, agent: str) -> TranscriptSource | None:
        """Return a `TranscriptSource` for `agent` in `workspace`.

        `None` if `agent` names a harness this build does not know (see
        `AgentName.parse` - lenient by design, never raises). The bound
        `exec_fn` below satisfies the `ExecFn` protocol by calling this
        provider's own `execute()` against `workspace`, exactly like any
        other consumer of `WorkspaceProvider.execute`.
        """
        plugin = get_harness(agent)
        if plugin is None:
            return None

        async def exec_fn(
            command: str,
            *,
            timeout: float | None = None,
            cwd: str | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecuteResult:
            return await self.execute(workspace, command, timeout=timeout, cwd=cwd, env=env)

        exec_fn_typed: ExecFn = exec_fn
        return plugin.transcript_source(exec_fn_typed)

    async def _ensure_network(self, network_name: str) -> None:
        """Ensure Docker network exists."""
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "network",
            "inspect",
            network_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        if proc.returncode != 0:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "network",
                "create",
                network_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

    async def _wait_for_running(self, container_name: str, timeout: float = 30.0) -> None:
        """Wait for container to be in running state."""
        start = time.perf_counter()
        while time.perf_counter() - start < timeout:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}",
                container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if stdout.decode().strip().lower() == "true":
                return
            await asyncio.sleep(0.1)

        raise RuntimeError(f"Container {container_name} did not start within {timeout}s")

    async def teardown(
        self,
        target: Workspace | WorkspaceRef,
        *,
        while_running: Callable[[], Awaitable[None]] | None = None,
        before_delete: Callable[[], Awaitable[None]] | None = None,
    ) -> TeardownReport:
        """Tear down with caller hooks at the two safe points.

        Implements `SupportsStagedTeardown`. The step order is enforced here
        rather than left to the caller, because getting it wrong destroys data.
        """
        workspace = self._resolve_teardown_target(target)
        # Single-flight per workspace. Without it two concurrent callers both
        # proceed, and one can delete the workspace directory while the other
        # is still inside a slow `before_delete` - which would break the
        # retention guarantee from the outside rather than from a bug in this
        # function.
        entry = self._teardown_locks.get(workspace.id)
        if entry is None:
            entry = self._teardown_locks[workspace.id] = _TeardownLock()
        entry.users += 1
        try:
            async with entry.lock:
                report = await self._teardown_locked(
                    workspace, while_running=while_running, before_delete=before_delete
                )
                # The workspace is gone, so its idempotency records may go too.
                # A RETAINED one is not gone: it stays registered for a retry
                # by ref, and so do its records.
                if report.finalization.state == "completed":
                    await self._operations.forget_workspace(workspace.id)
                return report
        finally:
            # Drop the entry only once no caller holds it or waits on it.
            # `lock.locked()` cannot tell: a waiter woken by `release()` does
            # not hold the lock until it resumes, so a third caller arriving in
            # that gap would build a FRESH lock and could delete the directory
            # while the second was still archiving from it. The count includes
            # waiters and is decremented on cancellation too.
            entry.users -= 1
            if entry.users == 0:
                self._teardown_locks.pop(workspace.id, None)

    def _resolve_teardown_target(self, target: Workspace | WorkspaceRef) -> Workspace:
        """The workspace a ref names: a retained one first, else a live one."""
        if not isinstance(target, WorkspaceRef):
            return target
        if target.provider == "docker":
            retained = self._retained.get(target.resource_id)
            if retained is not None:
                return retained.workspace
            for workspace in self._workspaces.values():
                if workspace._handle == target.resource_id:
                    return workspace
        raise LookupError(f"no workspace for {target.provider}:{target.resource_id}")

    async def _teardown_locked(
        self,
        workspace: Workspace,
        *,
        while_running: Callable[[], Awaitable[None]] | None,
        before_delete: Callable[[], Awaitable[None]] | None,
    ) -> TeardownReport:
        container_name = workspace._handle
        ref = WorkspaceRef(provider="docker", resource_id=container_name)

        retained = self._retained.get(container_name)
        if retained is not None:
            # A retry after a failed archive. The container went with the
            # first attempt, so there is nothing to run `while_running` in or
            # to stop, and its usage was measured then.
            usage = retained.usage
        else:
            # Deliberately still REGISTERED here. A hook that raises, hangs, or
            # is cancelled leaves a live container, and deregistering first
            # would make that container invisible to the provider that owns it.
            if while_running is not None:
                try:
                    await while_running()
                except Exception as exc:
                    return TeardownReport(
                        finalization=_hook_failed("while_running", exc), usage=TeardownUsage()
                    )

            # Before the stop: the container's cgroup goes with it.
            usage = await self._measure_usage(container_name)
            removed = False
            try:
                await self._stop(container_name)
            finally:
                # The `finally` is what guarantees removal: a cancel or an
                # error during the stop or the log snapshot must not leave a
                # stopped-but-present container behind. Verified by mutation -
                # dropping this block strands one.
                #
                # The shield is NOT what makes that work, and an earlier
                # comment here wrongly said it was. asyncio delivers
                # cancellation once, so this await completes on its own after
                # a single cancel. The shield covers the narrower case of a
                # SECOND cancellation arriving while removal is in flight,
                # which is cheap to hold onto and would otherwise leak a
                # container.
                removed = await asyncio.shield(self._remove(container_name))
                # The registration MOVES rather than drops: the container is
                # gone, but the directory is not until the delete below. A
                # failed or cancelled `before_delete` leaves the entry here, so
                # the ref it is reported under still resolves for a retry.
                # Only once removal is confirmed: a container `docker rm`
                # failed on may still be running against the directory.
                if removed:
                    async with self._lock:
                        self._workspaces.pop(workspace.id, None)
                        self._retained[container_name] = _Retained(workspace, usage)
            if not removed:
                # Still registered as live, so `teardown(ref)` retries the
                # whole sequence. Nothing was archived or deleted.
                raise ContainerRemovalError(
                    f"docker rm -f {container_name} failed; the container may still "
                    f"exist and workspace {workspace.id} was kept. Retry teardown by "
                    "ref once the Docker daemon is reachable."
                )

        if before_delete is not None:
            # The container is gone but the spool is still on disk. Any
            # exception here, CancelledError included, skips the delete below
            # by plain control flow. That is what keeps a failed upload
            # retryable instead of permanently lost.
            try:
                await before_delete()
            except Exception as exc:
                outcome = _hook_failed("before_delete", exc)
                return TeardownReport(
                    finalization=FinalizationOutcome(
                        state="retained",
                        stage="before_delete",
                        error=outcome.error,
                        retained=ref,
                    ),
                    usage=usage,
                )

        delete_failures, delete_fallback = await self._delete_workspace_dir(workspace)
        usage = replace(usage, delete_fallback=delete_fallback)
        if delete_failures:
            # `completed` would claim a directory that is still on disk, so the
            # workspace stays retained under the same ref for a retry.
            logger.warning(
                "teardown could not delete workspace %s: %s", workspace.id, delete_failures
            )
            return TeardownReport(
                finalization=FinalizationOutcome(
                    state="retained",
                    stage="before_delete",
                    error=OperationError(
                        kind="internal",
                        message=f"workspace directory deletion failed: {delete_failures[0]}",
                    ),
                    retained=ref,
                ),
                usage=replace(usage, delete_failures=delete_failures),
            )
        async with self._lock:
            self._retained.pop(container_name, None)
        return TeardownReport(finalization=FinalizationOutcome(state="completed"), usage=usage)

    #: Prints `<file>.<key> <value>` for the container's own cgroup v2 files.
    #: Each file is optional, so a kernel or runtime that lacks one costs
    #: that field, not the rest.
    _CGROUP_SCRIPT = (
        "cd /sys/fs/cgroup 2>/dev/null || exit 0; "
        "for f in memory.events cpu.stat; do "
        '[ -r "$f" ] && while read -r k v; do echo "$f.$k $v"; done < "$f"; '
        "done; "
        '[ -r memory.peak ] && echo "memory.peak $(cat memory.peak)"; '
        "exit 0"
    )

    async def _measure_usage(self, container_name: str) -> TeardownUsage:
        """What the container used, read while it still exists. Never raises.

        The cgroup files are read from inside the container, where cgroup v2
        with a private cgroup namespace (Docker's default) roots
        `/sys/fs/cgroup` at the container's own cgroup. `State.OOMKilled` is
        asked separately, because Docker can know of a kill when the files
        cannot be read.
        """
        cgroup = await self._docker_read("exec", container_name, "sh", "-c", self._CGROUP_SCRIPT)
        state = await self._docker_read("inspect", "-f", "{{.State.OOMKilled}}", container_name)
        oom_killed = {"true": True, "false": False}.get((state or "").strip().lower())
        return _usage_from_cgroup(cgroup or "", oom_killed=oom_killed)

    async def _docker_read(self, *args: str) -> str | None:
        """A short `docker` read for teardown: its stdout, or None on any failure."""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out = await asyncio.wait_for(self._drain_tail(proc.stdout), timeout=10)
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.CancelledError:
            if proc is not None:
                await self._terminate(proc)
            raise
        except Exception as exc:  # noqa: BLE001 - a measurement must never fail teardown
            logger.debug("docker %s failed during teardown: %s", args[0], exc)
            if proc is not None:
                await self._terminate(proc)
            return None
        if proc.returncode != 0:
            return None
        return out.decode(errors="replace")

    async def _delete_workspace_dir(
        self, workspace: Workspace
    ) -> tuple[tuple[str, ...], DeleteFallback | None]:
        """Delete the host workspace directory. Irreversible.

        Returns what could not be deleted, empty when the directory is gone,
        and how the container fallback went when it was needed. An already
        absent directory is success, so a retry is idempotent. A directory
        that is not one this provider created under its base directory is
        refused and left untouched.

        The container writes as its own uid, so files it leaves in
        subdirectories it created can be undeletable by this process (CI
        runners are the common case). On `PermissionError` the contents are
        deleted from a short-lived root container instead, and the emptied
        directory, which this process created, is removed here.
        """
        workspace_dir = workspace.metadata.get("workspace_dir")
        if not workspace_dir or not os.path.lexists(workspace_dir):
            return (), None
        # Checked before ANY delete, host or container: a path outside the
        # base directory is never this provider's to remove.
        if self._owned_workspace_dir(workspace, workspace_dir) is None:
            return (f"{workspace_dir}: refused: {self._NOT_OWNED}",), None
        error = _rmtree(workspace_dir)
        fallback: DeleteFallback | None = None
        why = ""
        if isinstance(error, PermissionError):
            fallback, why = await self._delete_contents_in_container(workspace, workspace_dir)
            if fallback == "deleted":
                error = _rmtree(workspace_dir)
        if not os.path.lexists(workspace_dir):
            return (), fallback
        detail = f"{workspace_dir}: {error!r}" if error else f"{workspace_dir}: still present"
        if why:
            return (detail, f"container delete {fallback}: {why}"), fallback
        return (detail,), fallback

    #: Bounds the whole `docker run` of the fallback delete.
    _DELETE_TIMEOUT_SECONDS = 60.0
    #: Bounds removing the fallback's container after its run failed.
    _DELETE_CLEANUP_TIMEOUT_SECONDS = 15.0
    #: Where the workspace directory is mounted inside the fallback container.
    _DELETE_TARGET = "/workspace-delete"

    #: Why a workspace directory is refused for deletion.
    _NOT_OWNED = "not a directory this provider created under its base directory"

    def _owned_workspace_dir(self, workspace: Workspace, workspace_dir: str) -> Path | None:
        """`workspace_dir` resolved, or None when this provider did not create it.

        Only a real directory directly under the provider's base directory
        qualifies: `ws-<id>` under `workspace_base_dir`, or the
        `agentic-ws-` mkdtemp directory when no base is configured. A symlink,
        or a path resolving anywhere else (a symlinked ancestor included), is
        refused, so neither the host nor the root container ever deletes a
        directory the provider did not create.
        """
        path = Path(workspace_dir)
        if path.is_symlink() or not path.is_dir():
            return None
        if self._workspace_base_dir is not None:
            if path.name != workspace.id:
                return None
            base = self._workspace_base_dir
        else:
            if not path.name.startswith("agentic-ws-"):
                return None
            base = Path(tempfile.gettempdir()).resolve()
        resolved = path.resolve()
        return resolved if resolved == base / path.name else None

    def _delete_mount_source(self, workspace: Workspace, workspace_dir: str) -> str | None:
        """The Docker host path of `workspace_dir`, or None when it is not ours."""
        owned = self._owned_workspace_dir(workspace, workspace_dir)
        if owned is None:
            return None
        if self._workspace_host_dir is not None and self._workspace_base_dir is not None:
            source = str(self._workspace_host_dir / owned.name)
        else:
            source = str(owned)
        # `--mount` is comma separated; a comma would smuggle in an option.
        return None if "," in source else source

    def _delete_contents_command(self, workspace: Workspace, source: str) -> list[str]:
        """`docker run` deleting everything under `source`, as root, offline.

        `find -delete` never follows symlinks and unlinks them as entries, so
        nothing outside the mount is touched. DAC_OVERRIDE is the one
        capability root needs to unlink entries in another uid's directories,
        including mode 000 ones; everything else stays dropped.
        """
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            f"{workspace._handle}-delete",
            "--network",
            "none",
            "--pull",
            "never",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--pids-limit",
            "64",
            "--entrypoint",
            "find",
            "--mount",
            f"type=bind,source={source},target={self._DELETE_TARGET}",
            workspace.metadata["image_id"],
            self._DELETE_TARGET,
            "-mindepth",
            "1",
            "-delete",
        ]

    async def _delete_contents_in_container(
        self, workspace: Workspace, workspace_dir: str
    ) -> tuple[DeleteFallback, str]:
        """Empty `workspace_dir` from a root container. Never raises.

        Uses the workspace's own image, which is already local: no pull, and
        no second image to trust. Returns the outcome and why it was not
        `deleted` (empty when it was).
        """
        source = self._delete_mount_source(workspace, workspace_dir)
        if source is None:
            return "refused", self._NOT_OWNED
        if not workspace.metadata.get("image_id"):
            return "refused", "no workspace image recorded to run the delete in"
        cmd = self._delete_contents_command(workspace, source)
        name = f"{workspace._handle}-delete"
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._DELETE_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            # The cancellation is what propagates; a cleanup error must not
            # replace it.
            if proc is not None:
                with contextlib.suppress(Exception):
                    await self._terminate(proc)
                    await asyncio.shield(self._remove_delete_container(name))
            raise
        except Exception as exc:  # noqa: BLE001 - reported as retained, never raised
            why = repr(exc)
            if proc is not None:
                # Killing the client leaves the container running; `rm -f`
                # stops it before it deletes anything more.
                with contextlib.suppress(Exception):
                    await self._terminate(proc)
                left = await self._remove_delete_container(name)
                if left:
                    why = f"{why}; {left}"
            return "failed", why
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[-500:]
            return "failed", f"docker run exited {proc.returncode}: {detail}"
        return "deleted", ""

    async def _remove_delete_container(self, name: str) -> str:
        """`docker rm -f` the fallback's container, bounded. Raises only on cancellation.

        Returns why the container may still exist, empty when it is gone.
        Unlike `_remove`, a stalled client is killed after
        `_DELETE_CLEANUP_TIMEOUT_SECONDS`, so the fallback stays bounded.
        """
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            code = await asyncio.wait_for(proc.wait(), timeout=self._DELETE_CLEANUP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            if proc is not None:
                with contextlib.suppress(Exception):
                    await self._terminate(proc)
            raise
        except Exception as exc:  # noqa: BLE001 - reported with the failed fallback
            if proc is not None:
                with contextlib.suppress(Exception):
                    await self._terminate(proc)
            return f"docker rm -f {name} failed: {exc!r}"
        return "" if code == 0 else f"docker rm -f {name} exited {code}"

    async def _stop(self, container_name: str) -> None:
        """`docker stop`, then snapshot the logs it caused to be written."""
        # This "-t 5" grace is coupled to __TERM_GRACE_TICKS in
        # workspace/entrypoint.sh's section 6
        # wrapper (ADR-040): that constant must stay strictly below this
        # value, with headroom left over for the post-agent finalize hooks
        # (e.g. a session-store upload) to actually run before docker's own
        # SIGKILL lands. Changing this value without adjusting that one can
        # silently make finalize stop running on every SIGTERM shutdown.
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "stop",
            "-t",
            "5",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        # Between stop and rm is the ONLY window in which the finalizer's
        # output can still be read: `docker stop` is what triggers finalizers,
        # and `docker rm` destroys the log stream they wrote to. A caller
        # asking for logs after removal would otherwise always get nothing,
        # which would make the session-capture verdict unreachable rather than
        # merely unread.
        self._remember_logs(
            container_name, await self._read_container_logs(container_name, tail=200)
        )

    async def _remove(self, container_name: str) -> bool:
        """`docker rm -f`. True when the container is gone.

        `rm -f` exits 0 for a container that is already absent, so a nonzero
        exit is a real failure (daemon unreachable, removal refused), not a
        missing container.
        """
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0

    async def _cleanup_container(self, container_name: str) -> None:
        """Stop and remove a container.

        Composed from the same steps the staged protocol exposes, so the
        combined path and the staged path cannot drift into behaving
        differently.
        """
        await self._stop(container_name)
        if not await self._remove(container_name):
            logger.warning("docker rm -f %s failed; the container may still exist", container_name)


def _rmtree(path: str) -> OSError | None:
    """`shutil.rmtree`, returning its error instead of raising it."""
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return exc
    return None


class ContainerRemovalError(RuntimeError):
    """`docker rm` failed during teardown. The workspace is still registered."""


@dataclass
class _TeardownLock:
    """A workspace's teardown lock and how many callers hold or await it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@dataclass(frozen=True)
class _Retained:
    """A workspace whose container is gone and whose directory is not yet."""

    workspace: Workspace
    usage: TeardownUsage


def _hook_failed(
    stage: Literal["while_running", "before_delete"], exc: Exception
) -> FinalizationOutcome:
    """A hook's exception as a `failed` outcome, logged with its traceback."""
    logger.warning("teardown hook %s failed", stage, exc_info=exc)
    return FinalizationOutcome(
        state="failed",
        stage=stage,
        error=OperationError(kind="internal", message=f"{stage} hook raised {exc!r}"),
    )


def _usage_from_cgroup(text: str, *, oom_killed: bool | None) -> TeardownUsage:
    """Map `_CGROUP_SCRIPT` output and Docker's OOM flag onto `TeardownUsage`."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            values[key] = int(value)

    def seconds(key: str) -> float | None:
        usec = values.get(key)
        return None if usec is None else usec / 1_000_000

    oom_kills = values.get("memory.events.oom_kill")
    if oom_kills:
        oom_killed = True
    elif oom_killed is None and oom_kills is not None:
        oom_killed = False
    return TeardownUsage(
        cpu_usage_seconds=seconds("cpu.stat.usage_usec"),
        cpu_throttled_seconds=seconds("cpu.stat.throttled_usec"),
        nr_throttled=values.get("cpu.stat.nr_throttled"),
        memory_peak_bytes=values.get("memory.peak"),
        oom_kills=oom_kills,
        oom_killed=oom_killed,
    )


def _relative_workdir(cwd: str | None) -> str:
    """`execute`'s cwd (absolute, or relative to /workspace) as a spec path."""
    if not cwd:
        return "."
    relative = posixpath.relpath(posixpath.join("/workspace", cwd), "/workspace")
    return relative
