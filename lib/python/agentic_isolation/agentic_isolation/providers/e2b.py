"""E2B cloud sandboxes as workspaces (syntropic137/syntropic137#1915, step E).

Implements `WorkspaceProvider`, the three remote-tier capabilities of step A
(`SupportsStreamingExec`, `SupportsFileTransfer`, `SupportsOwnership`) and
`SupportsStagedTeardown`, over the `E2BSandboxClient` port so no line here
imports the e2b SDK. One sandbox is one workspace; its id is the workspace id.

Decisions that differ from Docker, and why:

- Output is merged INSIDE the sandbox (`exec "$@" 2>&1`), so `lines()` is in
  the process's write order by construction rather than by trusting the
  arrival order of two SDK callbacks. `execute` therefore returns the merged
  text as `stdout` and an empty `stderr`.
- The command timeout is enforced here: on expiry the sandbox process is
  killed and the outcome is `timed_out` only once the kill is confirmed.
- Teardown follows decision 7 of the research doc: the workspace directory
  is the sandbox's own disk, so "delete" means "kill the sandbox", and it
  comes LAST. `while_running` and `before_delete` both run while the sandbox
  is up; if `before_delete` raises, the sandbox is paused, not killed, and the
  report is `retained`. If E2B keeps refusing the pause (HTTP 503), the
  sandbox is left running and still never killed. A teardown reports
  `completed` only when the sandbox is absent from the E2B list afterwards; a kill that leaves it listed raises
  `SandboxRemovalError`, which is also the negative control of #1915.
- Credential bindings and host mounts are refused: neither has an E2B
  delivery yet, and silently dropping one would launch a workspace without
  what the caller asked for.

The SDK behaviour this relies on is cited in `e2b_client.py`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import posixpath
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from agentic_isolation.config import CredentialPolicyError, WorkspaceConfig
from agentic_isolation.contract import FinalizationOutcome, OperationError, WorkspaceRef
from agentic_isolation.providers.base import ExecuteResult, Workspace
from agentic_isolation.providers.capabilities import (
    OwnedWorkspace,
    StreamOutcome,
    StreamSession,
    TransferRoot,
    WorkspaceNotFoundError,
    WorkspaceOwnershipError,
    mismatched_labels,
)
from agentic_isolation.providers.e2b_client import (
    E2BSandboxClient,
    E2BTransportError,
    SandboxCommand,
    SdkE2BClient,
    shell_join,
)
from agentic_isolation.providers.teardown_report import TeardownReport, TeardownUsage

_PROVIDER = "e2b"
_WORKSPACE_ROOT = "/workspace"
# Runs the argv with stderr on the same pipe as stdout, in one process.
_MERGED = ("sh", "-c", 'exec "$@" 2>&1', "sh")


class SandboxRemovalError(RuntimeError):
    """A sandbox was killed but is still in the E2B list; the workspace was kept."""


class E2BWorkspaceProvider:
    """Workspaces as E2B sandboxes started from one pre-built template."""

    def __init__(
        self,
        template: str,
        *,
        client: E2BSandboxClient | None = None,
        lifetime_seconds: int = 3600,
    ) -> None:
        """
        Args:
            template: E2B template id or name, built by `build_template`.
            client: The sandbox port; the e2b SDK adapter by default.
            lifetime_seconds: E2B's sandbox timeout, after which E2B kills it.
        """
        self._template = template
        self._client = client if client is not None else SdkE2BClient()
        self._lifetime_seconds = lifetime_seconds
        self._workspaces: dict[str, Workspace] = {}
        self._retained: dict[str, Workspace] = {}

    @property
    def name(self) -> str:
        return _PROVIDER

    async def create(self, config: WorkspaceConfig) -> Workspace:
        if config.credentials:
            raise CredentialPolicyError(
                "the e2b provider has no credential delivery yet; refusing "
                f"{len(config.credentials)} binding(s)"
            )
        if config.mounts:
            raise ValueError("the e2b provider cannot mount host paths into a sandbox")
        sandbox_id = await self._client.create(
            template=self._template,
            metadata=config.labels,
            lifetime_seconds=self._lifetime_seconds,
        )
        workspace = Workspace(
            id=sandbox_id,
            provider=_PROVIDER,
            path=Path(_WORKSPACE_ROOT),
            config=config,
            metadata={"labels": dict(config.labels)},
            _handle=sandbox_id,
        )
        self._workspaces[sandbox_id] = workspace
        result = await self.execute(workspace, f"mkdir -p {_WORKSPACE_ROOT}", cwd="/")
        if not result.success:
            await self._client.kill(sandbox_id)
            self._workspaces.pop(sandbox_id, None)
            raise RuntimeError(f"sandbox {sandbox_id} could not create {_WORKSPACE_ROOT}")
        return workspace

    async def destroy(self, workspace: Workspace) -> TeardownReport:
        return await self.teardown(workspace)

    async def execute(
        self,
        workspace: Workspace,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecuteResult:
        started = time.monotonic()
        session = await self.start_stream(
            workspace, ["sh", "-c", command], cwd=cwd, env=env, timeout=timeout
        )
        lines = [line async for line in session.lines()]
        outcome = await session.outcome()
        if outcome.disconnected:
            raise E2BTransportError(
                f"lost the command stream in sandbox {workspace.id}: {outcome.detail}"
            )
        return ExecuteResult(
            exit_code=outcome.exit_code if outcome.exit_code is not None else -1,
            stdout="".join(f"{line}\n" for line in lines),
            stderr="",
            duration_ms=(time.monotonic() - started) * 1000,
            timed_out=outcome.timed_out,
        )

    async def write_file(self, workspace: Workspace, path: str, content: str | bytes) -> None:
        data = content.encode() if isinstance(content, str) else content
        await self._client.write_bytes(workspace.id, _contained(path), data)

    async def read_file(self, workspace: Workspace, path: str) -> str:
        return (await self._client.read_bytes(workspace.id, _contained(path))).decode()

    async def file_exists(self, workspace: Workspace, path: str) -> bool:
        result = await self.execute(workspace, shell_join(["test", "-e", _contained(path)]))
        return result.success

    async def start_stream(
        self,
        workspace: Workspace,
        sandbox_argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> StreamSession:
        merged_env = {**workspace.config.environment, **(env or {})}
        command = await self._client.start(
            workspace.id,
            shell_join([*_MERGED, *sandbox_argv]),
            cwd=_resolve_cwd(cwd),
            env=merged_env,
            timeout=timeout,
        )
        return E2BStreamSession(self._client, workspace.id, command, timeout=timeout)

    async def read_files(
        self,
        workspace: Workspace,
        patterns: Sequence[str],
        *,
        base_path: TransferRoot,
    ) -> list[tuple[str, bytes]]:
        listing = await self.execute(
            workspace,
            shell_join(["find", ".", "-type", "f", "-print0"]),
            cwd=base_path,
        )
        if not listing.success:
            raise FileNotFoundError(f"cannot list {base_path} in sandbox {workspace.id}")
        relative = sorted(
            entry.removeprefix("./") for entry in listing.stdout.rstrip("\n").split("\0") if entry
        )
        matched = [path for path in relative if any(fnmatch.fnmatch(path, p) for p in patterns)]
        return [
            (path, await self._client.read_bytes(workspace.id, f"{base_path}/{path}"))
            for path in matched
        ]

    async def list_owned(self, labels: Mapping[str, str]) -> list[OwnedWorkspace]:
        listed = await self._client.list(labels)
        # The server filters too; this keeps the contract if it ever does not.
        return [
            OwnedWorkspace(workspace_id=entry.sandbox_id, labels=dict(entry.metadata))
            for entry in listed
            if not mismatched_labels(entry.metadata, labels)
        ]

    async def attach(self, workspace_id: str, *, expect_labels: Mapping[str, str]) -> Workspace:
        found = [entry for entry in await self._client.list({}) if entry.sandbox_id == workspace_id]
        if not found:
            raise WorkspaceNotFoundError(workspace_id)
        labels = dict(found[0].metadata)
        mismatched = mismatched_labels(labels, expect_labels)
        if mismatched:
            raise WorkspaceOwnershipError(workspace_id, mismatched)
        cached = self._workspaces.get(workspace_id)
        if cached is not None:
            return cached
        workspace = Workspace(
            id=workspace_id,
            provider=_PROVIDER,
            path=Path(_WORKSPACE_ROOT),
            config=WorkspaceConfig(provider=_PROVIDER, labels=labels),
            metadata={"labels": labels},
            _handle=workspace_id,
        )
        self._workspaces[workspace_id] = workspace
        return workspace

    async def teardown(
        self,
        target: Workspace | WorkspaceRef,
        *,
        while_running: Callable[[], Awaitable[None]] | None = None,
        before_delete: Callable[[], Awaitable[None]] | None = None,
    ) -> TeardownReport:
        """Hooks first, kill last; a failed archive pauses instead of killing."""
        workspace = self._resolve_teardown_target(target)
        ref = WorkspaceRef(provider=_PROVIDER, resource_id=workspace.id)
        if workspace.id not in self._retained and while_running is not None:
            try:
                await while_running()
            except Exception as exc:
                return TeardownReport(
                    finalization=_hook_failed("while_running", exc), usage=TeardownUsage()
                )

        if before_delete is not None:
            try:
                await before_delete()
            except Exception as exc:
                error = _hook_failed("before_delete", exc).error
                try:
                    await self._client.pause(workspace.id)
                except E2BTransportError as pause_exc:
                    # Kept running rather than killed: still retained.
                    error = OperationError(
                        kind="internal",
                        message=f"{error.message}; pause refused, sandbox left running: "
                        f"{pause_exc}",
                    )
                self._workspaces.pop(workspace.id, None)
                self._retained[workspace.id] = workspace
                return TeardownReport(
                    finalization=FinalizationOutcome(
                        state="retained",
                        stage="before_delete",
                        error=error,
                        retained=ref,
                    ),
                    usage=TeardownUsage(),
                )

        await self._client.kill(workspace.id)
        still_listed = [
            entry
            for entry in await self._client.list(workspace.config.labels)
            if entry.sandbox_id == workspace.id
        ]
        if still_listed:
            raise SandboxRemovalError(
                f"sandbox {workspace.id} is still in the E2B list after kill; "
                "the workspace was kept. Retry teardown by ref."
            )
        self._workspaces.pop(workspace.id, None)
        self._retained.pop(workspace.id, None)
        return TeardownReport(
            finalization=FinalizationOutcome(state="completed"), usage=TeardownUsage()
        )

    def _resolve_teardown_target(self, target: Workspace | WorkspaceRef) -> Workspace:
        if not isinstance(target, WorkspaceRef):
            return target
        if target.provider == _PROVIDER:
            found = self._retained.get(target.resource_id) or self._workspaces.get(
                target.resource_id
            )
            if found is not None:
                return found
        raise LookupError(f"no workspace for {target.provider}:{target.resource_id}")


class E2BStreamSession:
    """One command in a sandbox: merged lines, a confirmed outcome, cancel, reattach."""

    def __init__(
        self,
        client: E2BSandboxClient,
        sandbox_id: str,
        command: SandboxCommand,
        *,
        timeout: float | None,
    ) -> None:
        self._client = client
        self._sandbox_id = sandbox_id
        self._command = command
        self._outcome = asyncio.ensure_future(self._watch(timeout))

    @property
    def pid(self) -> int:
        return self._command.pid

    async def _watch(self, timeout: float | None) -> StreamOutcome:
        waiting = asyncio.ensure_future(self._command.wait())
        done, _ = await asyncio.wait({waiting}, timeout=timeout)
        if not done:
            killed = await self._command.kill()
            ended = await waiting
            if killed and ended.end == "exited":
                return StreamOutcome(end="timed_out")
            return StreamOutcome(end="disconnected", detail="timeout kill not confirmed")
        ended = waiting.result()
        if ended.end == "exited":
            return StreamOutcome(end="completed", exit_code=ended.exit_code)
        return StreamOutcome(end="disconnected", detail=ended.detail)

    async def lines(self) -> AsyncIterator[str]:
        pending = ""
        async for chunk in self._command.output():
            pending += chunk
            *complete, pending = pending.split("\n")
            for line in complete:
                yield line
        if pending:
            yield pending

    async def outcome(self) -> StreamOutcome:
        return await asyncio.shield(self._outcome)

    async def cancel(self) -> None:
        await self._command.kill()
        await self.outcome()

    async def reconnect(self) -> StreamSession:
        command = await self._client.reconnect(self._sandbox_id, self._command.pid)
        return E2BStreamSession(self._client, self._sandbox_id, command, timeout=None)


def _contained(path: str) -> str:
    """`path` under /workspace, refusing absolute paths and `..` escapes."""
    resolved = posixpath.normpath(posixpath.join(_WORKSPACE_ROOT, path))
    if posixpath.isabs(path) or not resolved.startswith(f"{_WORKSPACE_ROOT}/"):
        raise ValueError(f"path {path!r} is not inside {_WORKSPACE_ROOT}")
    return resolved


def _resolve_cwd(cwd: str | None) -> str:
    if cwd is None:
        return _WORKSPACE_ROOT
    return posixpath.normpath(posixpath.join(_WORKSPACE_ROOT, cwd))


def _hook_failed(
    stage: Literal["while_running", "before_delete"], exc: Exception
) -> FinalizationOutcome:
    return FinalizationOutcome(
        state="failed",
        stage=stage,
        error=OperationError(kind="internal", message=f"{stage} hook raised {exc!r}"),
    )
