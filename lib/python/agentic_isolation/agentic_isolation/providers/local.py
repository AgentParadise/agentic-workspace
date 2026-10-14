"""Local filesystem provider for development and testing.

This provider creates workspaces in the local filesystem,
useful for development and testing without Docker overhead.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import (
    ObservationPage,
    OperationError,
    OperationRef,
    OperationSpec,
    OperationStatus,
    ProviderDescriptor,
)
from agentic_isolation.providers.base import (
    BaseProvider,
    CredentialResolver,
    ExecuteResult,
    Workspace,
    resolve_env_credentials,
)
from agentic_isolation.providers.local_operations import LocalOperations
from agentic_isolation.providers.operation_spool import OperationFailure

logger = logging.getLogger(__name__)


class InsecureProviderError(RuntimeError):
    """An insecure provider was constructed without its explicit opt-in (APSS W-6.2)."""


class WorkspaceLocalProvider(BaseProvider):
    """Local filesystem workspace provider.

    Creates isolated directories in a temporary location.
    Suitable for development and testing, but provides
    no real isolation from the host system.

    WARNING: No security isolation! Agent code runs with your permissions.
    Use WorkspaceDockerProvider for untrusted workloads.

    Construction refuses unless `allow_insecure=True` is passed (APSS W-6.2),
    so no code path, default or registry lookup, reaches it by accident.

    Usage:
        provider = WorkspaceLocalProvider(allow_insecure=True)
        workspace = await provider.create(config)
        result = await provider.execute(workspace, "echo hello")
        await provider.destroy(workspace)
    """

    def __init__(
        self,
        base_dir: str | Path | None = None,
        shell: str = "/bin/bash",
        credentials: CredentialResolver | None = None,
        *,
        allow_insecure: bool = False,
    ):
        """Initialize local provider.

        Args:
            base_dir: Base directory for workspaces (default: system temp)
            shell: Shell to use for command execution
            credentials: Resolves `WorkspaceConfig.credentials`. Without one,
                a config that binds a credential is refused.
            allow_insecure: Must be `True`: the caller acknowledges that
                workloads run unisolated, as this user, on this host.

        Raises:
            InsecureProviderError: `allow_insecure` is not `True`.
        """
        if allow_insecure is not True:
            raise InsecureProviderError(
                "WorkspaceLocalProvider runs workloads unisolated on the host and is "
                "test-only; construct it with allow_insecure=True to opt in, or use "
                "WorkspaceDockerProvider"
            )
        self._base_dir = Path(base_dir) if base_dir else Path(tempfile.gettempdir())
        self._shell = shell
        self._workspaces: dict[str, Workspace] = {}
        self._credentials = credentials
        # Resolved credential values per workspace id, kept off the Workspace
        # (whose metadata is rendered and serialised) and dropped on destroy.
        self._credential_env: dict[str, dict[str, str]] = {}
        self._operations = LocalOperations(credentials=credentials)

    @property
    def name(self) -> str:
        """Provider name."""
        return "local"

    async def create(self, config: WorkspaceConfig) -> Workspace:
        """Create a local workspace directory."""
        credential_env = await resolve_env_credentials(
            config.credentials, self._credentials, provider=self.name
        )
        workspace_id = f"ws-{uuid.uuid4().hex[:12]}"
        workspace_path = self._base_dir / "agentic-workspaces" / workspace_id

        # Create workspace directory
        workspace_path.mkdir(parents=True, exist_ok=True)

        # Create working directory if different from root
        if config.working_dir != "/workspace":
            working_path = workspace_path / config.working_dir.lstrip("/")
            working_path.mkdir(parents=True, exist_ok=True)

        workspace = Workspace(
            id=workspace_id,
            provider=self.name,
            path=workspace_path,
            config=config,
            metadata={
                "shell": self._shell,
                "base_dir": str(self._base_dir),
            },
        )

        self._workspaces[workspace_id] = workspace
        self._credential_env[workspace_id] = credential_env
        return workspace

    async def destroy(self, workspace: Workspace) -> None:
        """Cancel its operations and remove the workspace directory."""
        await self._operations.forget_workspace(workspace.id)
        if workspace.id in self._workspaces:
            del self._workspaces[workspace.id]
        self._credential_env.pop(workspace.id, None)

        if workspace.path.exists():
            shutil.rmtree(workspace.path, ignore_errors=True)

    def describe(self) -> ProviderDescriptor:
        """What this provider can enforce: little, and it says so."""
        return self._operations.describe()

    async def start(self, workspace: Workspace, spec: OperationSpec) -> OperationRef:
        """Start `spec` as a host process in the workspace directory."""
        return await self._operations.start(
            workspace, spec, base_env=self._build_exec_env(workspace)
        )

    async def status(self, ref: OperationRef) -> OperationStatus:
        return await self._operations.status(ref)

    async def observe(
        self,
        ref: OperationRef,
        *,
        cursor: str | None,
        max_bytes: int,
        wait_seconds: float = 0,
    ) -> ObservationPage:
        return await self._operations.observe(
            ref, cursor=cursor, max_bytes=max_bytes, wait_seconds=wait_seconds
        )

    async def cancel(self, ref: OperationRef, *, grace_seconds: float) -> OperationStatus:
        return await self._operations.cancel(ref, grace_seconds=grace_seconds)

    async def execute(
        self,
        workspace: Workspace,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecuteResult:
        """Execute a command in the workspace."""
        work_dir = self._resolve_work_dir(workspace, cwd)
        exec_env = self._build_exec_env(workspace, env)

        start_time = time.time()
        timed_out = False

        try:
            # Use asyncio subprocess
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                env=exec_env,
            )

            # Wait with optional timeout
            effective_timeout = timeout or workspace.config.limits.timeout_seconds
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                timed_out = True
                stdout = b""
                stderr = f"Command timed out after {effective_timeout}s".encode()

            duration_ms = (time.time() - start_time) * 1000

            # Handle exit code - returncode is None until process completes
            exit_code = process.returncode
            if exit_code is None:
                exit_code = 124 if timed_out else 0

            return ExecuteResult(
                exit_code=exit_code,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                duration_ms=duration_ms,
                timed_out=timed_out,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return ExecuteResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                duration_ms=duration_ms,
                timed_out=False,
            )

    async def write_file(
        self,
        workspace: Workspace,
        path: str,
        content: str | bytes,
    ) -> None:
        """Write a file in the workspace."""
        file_path = workspace.path / path.lstrip("/")
        file_path.parent.mkdir(parents=True, exist_ok=True)

        mode = "wb" if isinstance(content, bytes) else "w"
        encoding = None if isinstance(content, bytes) else "utf-8"

        with open(file_path, mode, encoding=encoding) as f:
            f.write(content)

    async def read_file(
        self,
        workspace: Workspace,
        path: str,
    ) -> str:
        """Read a file from the workspace.

        A FIFO, socket or device is refused as `rejected_policy` and never
        waited on: it is checked before the open and again on the descriptor,
        and O_NONBLOCK lets a FIFO swapped in between open at once.
        """
        file_path = workspace.path / path.lstrip("/")

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if _is_special(file_path.stat().st_mode):
            raise _not_regular(path)
        try:
            fd = os.open(file_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            if e.errno == errno.ENXIO:  # a socket
                raise _not_regular(path) from e
            raise
        with open(fd, encoding="utf-8") as f:
            if _is_special(os.fstat(fd).st_mode):
                raise _not_regular(path)
            return f.read()

    async def file_exists(
        self,
        workspace: Workspace,
        path: str,
    ) -> bool:
        """Check if a file exists in the workspace."""
        file_path = workspace.path / path.lstrip("/")
        return file_path.exists()

    def _build_exec_env(
        self,
        workspace: Workspace,
        env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build environment variables for command execution."""
        exec_env = os.environ.copy()
        exec_env.update(workspace.config.checked_environment())
        exec_env.update(self._credential_env.get(workspace.id, {}))
        if env:
            exec_env.update(env)
        exec_env["WORKSPACE_ID"] = workspace.id
        exec_env["WORKSPACE_PATH"] = str(workspace.path)
        return exec_env

    def _resolve_work_dir(
        self,
        workspace: Workspace,
        cwd: str | None = None,
    ) -> Path:
        """Resolve and ensure the working directory exists."""
        if cwd:
            work_dir = workspace.path / cwd.lstrip("/")
        else:
            work_dir = workspace.path / workspace.config.working_dir.lstrip("/")
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    async def stream(
        self,
        workspace: Workspace,
        command: list[str],
        *,
        timeout_seconds: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Stream stdout lines from command execution.

        Args:
            workspace: Workspace to execute in
            command: Command as list of strings
            timeout_seconds: Max execution time
            cwd: Working directory override
            env: Additional environment variables

        Yields:
            Individual stdout lines as they are produced
        """
        work_dir = self._resolve_work_dir(workspace, cwd)
        exec_env = self._build_exec_env(workspace, env)

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(work_dir),
            env=exec_env,
        )

        async for line in self._read_stream_lines(proc, timeout_seconds):
            yield line


def _is_special(mode: int) -> bool:
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode)


def _not_regular(path: str) -> OperationFailure:
    return OperationFailure(
        OperationError(kind="rejected_policy", message=f"{path} is not a regular file")
    )
