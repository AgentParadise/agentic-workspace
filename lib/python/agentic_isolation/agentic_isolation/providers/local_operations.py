"""The APSS operation contract over host processes (plan section 7 row 4).

`LocalOperations` owns everything `WorkspaceLocalProvider` needs to satisfy
`SupportsOperations`: the idempotency record, one collector per operation and
the process-group cancel. It mirrors `DockerOperations` without a container:

- The workload is launched directly as leader of a new session, so its pid is
  its process group and the launching task holds the handle. Status comes
  from that handle's exit code, never from anything the workload can write.
- Output is appended to an `OperationSpool` in this process. A Local workload
  runs as our own uid in our own pid namespace, so unlike Docker it could in
  principle reach into this process; the descriptor therefore claims
  `continuity_detected`, not `exact` (fixture `workspace-provider/local.json`).
- The launch marker line is appended to the spool by this process before the
  workload exists, so it is always first and cannot be forged into first place.
  Its argv0 names the wrapper in Docker; Local has no wrapper, so it is unused.
- Cancel refuses a group numbered <= 1 or equal to the caller's own group: the
  caller's namespace IS the workload's here, so the own-group check from
  `DockerOperations._unsafe_leader` is always real. A refusal still kills the
  leader by its handle, and reports the refusal in the status metadata.
- Credential values reach only the workload's environment, never an argv.
- Records live until `forget_workspace`; Local workspaces are short-lived and
  test-only, so there is no retention window.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from agentic_isolation.contract import (
    TERMINAL_STATES,
    EnforceableLimits,
    LaunchMarker,
    ObservationDescriptor,
    ObservationPage,
    OperationError,
    OperationRef,
    OperationSpec,
    OperationState,
    OperationStatus,
    ProviderDescriptor,
    WorkspaceRef,
)
from agentic_isolation.providers.base import CredentialResolver, Workspace
from agentic_isolation.providers.operation_spool import OperationFailure, OperationSpool

# Values published in tests/fixtures/apss_exp_v1_0006/valid/workspace-provider/local.json.
RETENTION_BYTES = 1 << 20
MAX_PAGE_BYTES = 16384

# After a timeout or cancel, how long KILL is given to clear the group.
_KILL_WAIT_SECONDS = 2.0


def unsafe_group(pgid: int) -> str | None:
    """Why `pgid` must not be signalled, or None when it may be."""
    if pgid <= 1:
        return f"refusing unsafe process group {pgid}"
    if pgid == os.getpgrp():
        return f"refusing unsafe process group {pgid}: it is the caller's own group"
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error(
    kind: Literal[
        "unknown_operation",
        "conflict",
        "rejected_policy",
        "unsupported_capability",
        "provider_unavailable",
    ],
    message: str,
    ref: OperationRef | None = None,
) -> OperationFailure:
    if ref is None:
        return OperationFailure(OperationError(kind=kind, message=message))
    return OperationFailure(OperationError(kind=kind, message=message, ref=ref))


def _proc_table() -> dict[int, tuple[int, int]]:
    """pid -> (ppid, pgrp) for every live, non-zombie process."""
    table: dict[int, tuple[int, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2 :].split()
        if fields and fields[0] != "Z":
            table[int(entry.name)] = (int(fields[1]), int(fields[2]))
    return table


def _members(pgid: int, tracked: set[int]) -> set[int]:
    """The group, every process still tracked, and all their descendants.

    A descendant that left the group (`setsid`) is found through its parent
    chain, and once seen stays tracked after its parent dies.
    """
    table = _proc_table()
    found = {pid for pid, (_, pgrp) in table.items() if pgrp == pgid} | (tracked & table.keys())
    grew = True
    while grew:
        grew = False
        for pid, (ppid, _) in table.items():
            if pid not in found and ppid in found:
                found.add(pid)
                grew = True
    return {pid for pid in found if pid > 1 and pid != os.getpid()}


@dataclass
class _Operation:
    ref: OperationRef
    spec: OperationSpec
    workspace_id: str
    spool: OperationSpool
    started_at: str
    process: asyncio.subprocess.Process
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    state: OperationState = "running"
    exit_code: int | None = None
    signal: str | None = None
    ended_at: str | None = None
    cancel_requested: bool = False
    timed_out: bool = False
    tracked: set[int] = field(default_factory=set)
    signal_failure: str | None = None
    collector: asyncio.Task[None] | None = None


class LocalOperations:
    """Start, observe, status and cancel operations as host processes."""

    def __init__(
        self,
        *,
        credentials: CredentialResolver | None,
        retention_bytes: int = RETENTION_BYTES,
        max_page_bytes: int = MAX_PAGE_BYTES,
    ) -> None:
        self._credentials = credentials
        self._retention = retention_bytes
        self._max_page = max_page_bytes
        self._operations: dict[str, _Operation] = {}
        self._by_key: dict[tuple[str, str], _Operation] = {}
        # One lock per key: racing same-key starts see the first one's record.
        self._launching: dict[tuple[str, str], asyncio.Lock] = {}
        self._sequence = 0

    def describe(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            schema="apss.workspace-provider/v1",
            provider="local",
            security_profile="insecure-local",
            enforceable_limits=EnforceableLimits(
                cpu=False, memory=False, disk=False, network=False, timeout=True
            ),
            capabilities=(),
            observation=ObservationDescriptor(
                retention_bytes=self._retention,
                max_page_bytes=self._max_page,
                integrity="continuity_detected",
            ),
            interactive=False,
            environment_kinds=(),
        )

    async def start(
        self, workspace: Workspace, spec: OperationSpec, *, base_env: dict[str, str]
    ) -> OperationRef:
        """Accept `spec` once per `(workspace, operation_key)`.

        `base_env` is the environment the provider's `execute` would use; the
        spec's overlay and resolved credentials are laid over it.
        """
        key = (workspace.id, spec.operation_key)
        async with self._launching.setdefault(key, asyncio.Lock()):
            existing = self._by_key.get(key)
            if existing is not None:
                if existing.spec != spec:
                    raise _error(
                        "conflict",
                        f"operation_key {spec.operation_key!r} already started "
                        "with a different spec",
                        existing.ref,
                    )
                return existing.ref
            return await self._launch(workspace, spec, key, base_env)

    async def _launch(
        self,
        workspace: Workspace,
        spec: OperationSpec,
        key: tuple[str, str],
        base_env: dict[str, str],
    ) -> OperationRef:
        root = workspace.path.resolve()
        cwd = (root / spec.working_directory).resolve()
        if not cwd.is_relative_to(root):
            raise _error(
                "rejected_policy",
                f"working_directory {spec.working_directory!r} leaves workspace {workspace.id}",
            )
        env = {**base_env, **spec.environment}
        for binding in spec.credential_bindings:
            if binding.delivery != "env":
                raise _error(
                    "unsupported_capability",
                    f"credential delivery {binding.delivery!r} is not supported by local",
                )
            if self._credentials is None:
                raise _error(
                    "rejected_policy",
                    "credential bindings need a CredentialResolver; none was injected",
                )
            env[binding.target] = await self._credentials.resolve(binding.ref)
        self._sequence += 1
        ref = OperationRef(
            workspace=WorkspaceRef(provider="local", resource_id=workspace.id),
            operation_id=f"{workspace.id}-op{self._sequence}",
            operation_key=spec.operation_key,
        )
        spool = OperationSpool(retention_bytes=self._retention)
        if isinstance(spec.launch_marker, LaunchMarker):
            spool.append(f"{spec.launch_marker.line}\n".encode())
        cwd.mkdir(parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *spec.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
                env=env,
                start_new_session=True,
            )
        except OSError as e:
            raise _error("provider_unavailable", f"could not launch {spec.argv[0]!r}: {e}") from e
        operation = _Operation(
            ref=ref,
            spec=spec,
            workspace_id=workspace.id,
            spool=spool,
            started_at=_now(),
            process=process,
        )
        operation.collector = asyncio.create_task(self._collect(operation))
        self._operations[ref.operation_id] = operation
        self._by_key[key] = operation
        return ref

    async def _collect(self, operation: _Operation) -> None:
        process = operation.process
        assert process.stdout is not None  # PIPE
        reader = asyncio.ensure_future(self._pump(operation, process.stdout))
        try:
            async with asyncio.timeout(operation.spec.timeout_seconds):
                await asyncio.shield(reader)
                await process.wait()
        except TimeoutError:
            operation.timed_out = True
            await self._terminate(operation, grace_seconds=1.0)
            # A descendant still holding the pipe cannot hold settlement.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_KILL_WAIT_SECONDS):
                    await asyncio.shield(reader)
            await process.wait()
        finally:
            if not reader.done():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
        self._settle(operation, process.returncode)

    async def _pump(self, operation: _Operation, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            operation.spool.append(chunk)
            operation.changed.set()

    @staticmethod
    def _settle(operation: _Operation, code: int | None) -> None:
        operation.ended_at = _now()
        if code is not None and code < 0:
            # Killed by a signal: report it the way a shell (and Docker) does.
            operation.signal = signal.Signals(-code).name
            code = 128 - code
        operation.exit_code = code
        if operation.cancel_requested:
            operation.state = "cancelled"
        elif operation.timed_out:
            operation.state = "timed_out"
        else:
            operation.state = "succeeded" if code == 0 else "failed"
        operation.changed.set()

    async def status(self, ref: OperationRef) -> OperationStatus:
        return self._status(self._lookup(ref))

    def _status(self, operation: _Operation) -> OperationStatus:
        metadata = dict(operation.spec.metadata)
        if operation.signal_failure is not None:
            metadata["cancel_signal_failure"] = operation.signal_failure
        if operation.state not in TERMINAL_STATES:
            return OperationStatus(
                schema="apss.workspace-status/v1",
                ref=operation.ref,
                state=operation.state,
                started_at=operation.started_at,
                execution_id=operation.spec.execution_id,
                metadata=metadata,
            )
        return OperationStatus(
            schema="apss.workspace-status/v1",
            ref=operation.ref,
            state=operation.state,
            exit_code=operation.exit_code,
            signal=operation.signal,
            started_at=operation.started_at,
            ended_at=operation.ended_at or _now(),
            execution_id=operation.spec.execution_id,
            metadata=metadata,
        )

    async def observe(
        self,
        ref: OperationRef,
        *,
        cursor: str | None,
        max_bytes: int,
        wait_seconds: float = 0,
    ) -> ObservationPage:
        operation = self._lookup(ref)
        limit = min(max_bytes, self._max_page)
        page = operation.spool.page(ref, state=operation.state, cursor=cursor, max_page_bytes=limit)
        if page.complete or page.data_b64 or wait_seconds <= 0:
            return page
        operation.changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(operation.changed.wait(), timeout=wait_seconds)
        return operation.spool.page(ref, state=operation.state, cursor=cursor, max_page_bytes=limit)

    async def cancel(self, ref: OperationRef, *, grace_seconds: float) -> OperationStatus:
        operation = self._lookup(ref)
        if operation.state not in TERMINAL_STATES:
            operation.cancel_requested = True
            await self._terminate(operation, grace_seconds=grace_seconds)
            if operation.collector is not None:
                await operation.collector
        return self._status(operation)

    async def _terminate(self, operation: _Operation, *, grace_seconds: float) -> None:
        """TERM the group and its escaped descendants, then KILL after the grace.

        A refused group (init, or our own) is never signalled; the leader is
        then killed through its handle alone, and the refusal is recorded.
        """
        pgid = operation.process.pid
        if refusal := unsafe_group(pgid):
            self._note(operation, refusal)
            with contextlib.suppress(ProcessLookupError):
                operation.process.kill()
            return
        for sig, wait in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, _KILL_WAIT_SECONDS)):
            operation.tracked |= _members(pgid, operation.tracked)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, sig)
            for pid in operation.tracked:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, sig)
            if await self._gone(operation, pgid, within=wait):
                return
        self._note(operation, f"process group {pgid} survived SIGKILL")

    @staticmethod
    async def _gone(operation: _Operation, pgid: int, *, within: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while _members(pgid, operation.tracked):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    @staticmethod
    def _note(operation: _Operation, failure: str) -> None:
        if operation.signal_failure is None:
            operation.signal_failure = failure
        else:
            operation.signal_failure = f"{operation.signal_failure}; {failure}"

    async def forget_workspace(self, workspace_id: str) -> None:
        """Cancel anything still running in a destroyed workspace and drop its records."""
        for operation in [o for o in self._operations.values() if o.workspace_id == workspace_id]:
            await self.cancel(operation.ref, grace_seconds=1.0)
            del self._operations[operation.ref.operation_id]
        for key in [key for key in self._by_key if key[0] == workspace_id]:
            del self._by_key[key]
            self._launching.pop(key, None)

    def _lookup(self, ref: OperationRef) -> _Operation:
        operation = self._operations.get(ref.operation_id)
        if operation is None or operation.ref != ref:
            raise _error(
                "unknown_operation", f"no operation {ref.operation_id} in this provider", ref
            )
        return operation
