"""The APSS operation contract over E2B sandbox commands (#95).

`E2BOperations` owns everything `E2BWorkspaceProvider` needs to satisfy
`SupportsOperations`, with the `OperationSpec` semantics of `DockerOperations`:

- Output is read from the SDK's command stream and appended to an
  `OperationSpool` in this process, outside the sandbox, so the workload
  cannot rewrite what was observed. Values resolved from
  `spec.credential_bindings` and those the caller registers in `redact` are
  replaced with the spool's `[REDACTED]` marker before anything is stored.
- Credential values travel only in the SDK command's `envs`, so they are
  scoped to that one launch and never appear in an argv.
- The launch marker's argv0 and line are written to the command's stdin and
  stdin is closed; a wrapper reads them into shell variables (never an argv
  or an exported variable) and prints the line before it execs the workload.
- The wrapper starts the workload as leader of a new session and first prints
  its pid, which is the operation's process group. Cancel and timeout send
  that group TERM, then KILL after the grace, and confirm no member is left,
  all in ONE further sandbox command (`commands.run`); the SDK's own kill
  is SIGKILL to one pid, which would leave the group running.
- Status is the exit code the SDK reports for the command, never anything
  the workload can write.

Differences from Docker, and why:

- stderr is merged into stdout inside the sandbox, as `E2BWorkspaceProvider`
  already does, so the stream is in write order.
- The SDK decodes the stream as UTF-8 with replacement
  (`AsyncCommandHandle`'s incremental decoder in e2b==2.52.0), so bytes that
  are not UTF-8 arrive replaced. The descriptor therefore publishes
  `continuity_detected`, not `exact`.
- There is no container to stop when a group will not die: an unconfirmed
  termination raises `provider_unavailable` and the operation settles
  `failed`, with the reason in `metadata["cancel_signal_failure"]`.
- Records live as long as the workspace; `forget_workspace` drops them at
  teardown. There is no reattach, as for Docker.
"""

from __future__ import annotations

import asyncio
import contextlib
import posixpath
import shlex
import signal
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from agentic_isolation.config import CredentialPolicyError, check_credential_bindings
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
from agentic_isolation.providers.e2b_client import CommandExit, E2BSandboxClient, SandboxCommand
from agentic_isolation.providers.operation_spool import (
    OperationFailure,
    OperationSpool,
    redaction_policy,
)

_PROVIDER = "e2b"
# Reads the marker's argv0 and line from stdin, then runs the inner wrapper as
# a background job (not a group leader, so `setsid` execs in place and its pid
# is the new group). `wait` returns the workload's status, 128+n for a signal.
_OUTER = (
    "exec 2>&1; IFS= read -r argv0; IFS= read -r line; inner=$1; shift; "
    'setsid sh -c "$inner" "$argv0" "$line" "$@" & wait "$!"'
)
# Line one is the group's pid, then the marker line if one was given, then the
# workload replaces this shell.
_INNER = 'echo "$$"; [ -n "$1" ] && printf "%s\\n" "$1"; shift; exec "$@"'
# TERM group $1, wait up to $2 tenths of a second for every non-zombie member
# to go, then KILL and wait up to 2s. Exits 0 only once none is left, 2 for a
# group it refuses to signal. Builtins and /proc only: slim images ship no `ps`.
_TERMINATE = (
    'group=$1; grace=$2; [ "$group" -gt 1 ] 2>/dev/null || exit 2; '
    "alive() { for s in /proc/[0-9]*/stat; do "
    'st=$(cat "$s" 2>/dev/null) || continue; set -- ${st##*) }; '
    '[ "$1" != Z ] && [ "$3" = "$group" ] && return 0; done; return 1; }; '
    'settle() { n=$1; while alive; do [ "$n" -le 0 ] && return 1; '
    "sleep 0.1; n=$((n-1)); done; return 0; }; "
    'kill -s TERM -- "-$group" 2>/dev/null; settle "$grace" && exit 0; '
    'kill -s KILL -- "-$group" 2>/dev/null; settle 20'
)
# How long a termination command, or a command's end after one, may take.
_CONTROL_SECONDS = 30.0
# After a timeout's termination, the stream is read this long for final output.
_FINAL_OUTPUT_SECONDS = 5.0
# How long a cancel waits for the wrapper's pid line before refusing to guess.
_LEADER_SECONDS = 30.0


@dataclass(frozen=True)
class _Replay:
    spec: OperationSpec
    ref: OperationRef
    policy: bytes


@dataclass
class _Operation:
    ref: OperationRef
    spec: OperationSpec
    sandbox_id: str
    spool: OperationSpool
    started_at: str
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    leader_known: asyncio.Event = field(default_factory=asyncio.Event)
    # The session leader's pid, which is the operation's process group.
    group: int | None = None
    state: OperationState = "running"
    exit_code: int | None = None
    signal: str | None = None
    ended_at: str | None = None
    timed_out: bool = False
    cancel_requested: bool = False
    # Only a confirmed termination publishes `cancelled`.
    cancel_confirmed: bool = False
    # Held by a termination and by settlement, so a cancel decides its outcome first.
    stopping: asyncio.Lock = field(default_factory=asyncio.Lock)
    signal_failure: str | None = None
    collector: asyncio.Task[None] | None = None

    def note(self, failure: str) -> None:
        self.signal_failure = (
            failure if self.signal_failure is None else f"{self.signal_failure}; {failure}"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error(
    kind: Literal["unknown_operation", "conflict", "provider_unavailable"],
    message: str,
    ref: OperationRef | None = None,
) -> OperationFailure:
    return OperationFailure(OperationError(kind=kind, message=message, ref=ref))


class E2BOperations:
    """Start, observe, status and cancel operations in E2B sandboxes."""

    def __init__(
        self,
        client: E2BSandboxClient,
        *,
        run_as: str,
        credentials: CredentialResolver | None,
        retention_bytes: int,
        max_page_bytes: int,
    ) -> None:
        self._client = client
        self._run_as = run_as
        self._credentials = credentials
        self._retention = retention_bytes
        self._max_page = max_page_bytes
        self._operations: dict[str, _Operation] = {}
        self._by_key: dict[tuple[str, str], _Replay] = {}
        self._launching: dict[tuple[str, str], asyncio.Lock] = {}
        self._sequence = 0

    def describe(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            schema="apss.workspace-provider/v1",
            provider=_PROVIDER,
            security_profile="isolated",
            enforceable_limits=EnforceableLimits(
                cpu=False, memory=False, disk=False, network=False, timeout=True
            ),
            capabilities=("operations", "cancel", "launch_marker", "credential_env"),
            observation=ObservationDescriptor(
                retention_bytes=self._retention,
                max_page_bytes=self._max_page,
                integrity="continuity_detected",
            ),
            interactive=False,
            environment_kinds=("e2b-template",),
        )

    async def start(
        self, workspace: Workspace, spec: OperationSpec, *, redact: Collection[str] = ()
    ) -> OperationRef:
        key = (workspace.id, spec.operation_key)
        # Racing same-key starts serialise here and see the first one's record.
        async with self._launching.setdefault(key, asyncio.Lock()):
            return await self._start_locked(workspace, spec, key, redact)

    async def _start_locked(
        self,
        workspace: Workspace,
        spec: OperationSpec,
        key: tuple[str, str],
        redact: Collection[str],
    ) -> OperationRef:
        existing = self._by_key.get(key)
        if existing is not None:
            if existing.spec != spec:
                raise _error(
                    "conflict",
                    f"operation_key {spec.operation_key!r} already started with a different spec",
                    existing.ref,
                )
            if existing.policy != redaction_policy(redact):
                raise _error(
                    "conflict",
                    f"operation_key {spec.operation_key!r} already started "
                    "with different redaction values",
                    existing.ref,
                )
            return existing.ref
        try:
            check_credential_bindings(list(spec.credential_bindings))
        except CredentialPolicyError as e:
            raise OperationFailure(OperationError(kind="rejected_policy", message=str(e))) from e
        secrets: dict[str, str] = {}
        # Kept apart from `secrets`: two bindings may name one target, and the
        # overwritten value must still be redacted.
        resolved: list[str] = []
        for binding in spec.credential_bindings:
            if binding.delivery != "env":
                raise OperationFailure(
                    OperationError(
                        kind="unsupported_capability",
                        message=f"credential delivery {binding.delivery!r} is not supported by e2b",
                    )
                )
            if self._credentials is None:
                raise OperationFailure(
                    OperationError(
                        kind="rejected_policy",
                        message="credential bindings need a CredentialResolver; none was injected",
                    )
                )
            secrets[binding.target] = await self._credentials.resolve(binding.ref)
            resolved.append(secrets[binding.target])
        self._sequence += 1
        ref = OperationRef(
            workspace=WorkspaceRef(provider=_PROVIDER, resource_id=workspace.id),
            operation_id=f"{workspace.id}-op{self._sequence}",
            operation_key=spec.operation_key,
        )
        operation = _Operation(
            ref=ref,
            spec=spec,
            sandbox_id=workspace.id,
            spool=OperationSpool(retention_bytes=self._retention, redact=(*redact, *resolved)),
            started_at=_now(),
        )
        marker = spec.launch_marker
        argv0, line = (
            (marker.argv0, marker.line) if isinstance(marker, LaunchMarker) else ("sh", "")
        )
        command = await self._client.start(
            workspace.id,
            shlex.join(["sh", "-c", _OUTER, "aw-operation", _INNER, *spec.argv]),
            cwd=posixpath.normpath(posixpath.join("/workspace", spec.working_directory)),
            env={**workspace.config.checked_environment(), **spec.environment, **secrets},
            timeout=None,
            user=self._run_as,
            stdin=f"{argv0}\n{line}\n".encode(),
        )
        operation.collector = asyncio.create_task(self._collect(operation, command))
        self._operations[ref.operation_id] = operation
        self._by_key[key] = _Replay(spec=spec, ref=ref, policy=redaction_policy(redact))
        return ref

    def forget_workspace(self, workspace_id: str) -> None:
        """Drop every record of a destroyed workspace, replay records included."""
        for operation in list(self._operations.values()):
            if operation.sandbox_id == workspace_id:
                del self._operations[operation.ref.operation_id]
        for key in [key for key in self._by_key if key[0] == workspace_id]:
            del self._by_key[key]

    async def _collect(self, operation: _Operation, command: SandboxCommand) -> None:
        reader = asyncio.create_task(self._read(operation, command))
        waiting = asyncio.ensure_future(command.wait())
        ended: CommandExit | None = None
        try:
            done, _ = await asyncio.wait({waiting}, timeout=operation.spec.timeout_seconds)
            if not done:
                operation.timed_out = True
                async with operation.stopping:
                    try:
                        await self._terminate(operation, grace_seconds=1.0)
                    except OperationFailure as e:
                        operation.note(e.error.message)
                done, _ = await asyncio.wait({waiting}, timeout=_CONTROL_SECONDS)
            if done:
                ended = waiting.result()
            else:
                waiting.cancel()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_FINAL_OUTPUT_SECONDS):
                    await asyncio.shield(reader)
        finally:
            if not reader.done():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            operation.leader_known.set()
        operation.spool.finish()
        async with operation.stopping:
            self._settle(operation, ended)

    async def _read(self, operation: _Operation, command: SandboxCommand) -> None:
        pending = ""
        try:
            async for text in command.output():
                if operation.leader_known.is_set():
                    self._record(operation, text)
                    continue
                pending += text
                header, newline, rest = pending.partition("\n")
                if not newline:
                    continue
                if header.isdecimal():
                    operation.group = int(header)
                else:
                    # Not the wrapper's line (it failed before exec); keep it.
                    rest = pending
                operation.leader_known.set()
                if rest:
                    self._record(operation, rest)
            if pending and not operation.leader_known.is_set():
                self._record(operation, pending)
        finally:
            operation.leader_known.set()

    @staticmethod
    def _record(operation: _Operation, text: str) -> None:
        operation.spool.append(text.encode())
        operation.changed.set()

    @staticmethod
    def _settle(operation: _Operation, ended: CommandExit | None) -> None:
        if operation.state in TERMINAL_STATES:
            return
        operation.ended_at = _now()
        if operation.cancel_requested and not operation.cancel_confirmed:
            operation.state = "failed"
        elif operation.cancel_requested:
            operation.state = "cancelled"
        elif operation.timed_out:
            operation.state = "timed_out"
        elif ended is None or ended.end != "exited":
            operation.state = "lost"
        else:
            code = ended.exit_code
            operation.exit_code = code
            if code is not None and code > 128 and code - 128 < signal.NSIG:
                operation.signal = signal.Signals(code - 128).name
            operation.state = "succeeded" if code == 0 else "failed"
        operation.changed.set()

    async def status(self, ref: OperationRef) -> OperationStatus:
        operation = self._lookup(ref)
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
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_LEADER_SECONDS):
                    await operation.leader_known.wait()
            async with operation.stopping:
                if operation.state in TERMINAL_STATES:
                    return await self.status(ref)
                operation.cancel_requested = True
                try:
                    await self._terminate(operation, grace_seconds=grace_seconds)
                except OperationFailure as e:
                    operation.note(e.error.message)
                    raise
                except asyncio.CancelledError:
                    operation.note("cancel interrupted before termination was confirmed")
                    raise
                operation.cancel_confirmed = True
            if operation.collector is not None:
                await operation.collector
        return await self.status(ref)

    async def _terminate(self, operation: _Operation, *, grace_seconds: float) -> None:
        """TERM the group, KILL after the grace, and confirm none is left, in the sandbox.

        Raises provider_unavailable when there is no trusted group or the
        termination is not confirmed: the operation may still be running.
        """
        if operation.group is None:
            raise _error(
                "provider_unavailable",
                f"no launch header from {operation.ref.operation_id}; "
                "its process group is unknown and was not signalled",
                operation.ref,
            )
        tenths = max(0, round(grace_seconds * 10))
        command = await self._client.start(
            operation.sandbox_id,
            shlex.join(["sh", "-c", _TERMINATE, "aw-terminate", str(operation.group), str(tenths)]),
            cwd="/",
            env={},
            timeout=None,
            user=self._run_as,
        )
        chunks: list[str] = []

        async def drain() -> None:
            async for text in command.output():
                chunks.append(text)

        # Drained alongside `wait`, never before it: the SDK adapter ends its
        # output only once `wait` has seen the command end.
        reader = asyncio.create_task(drain())
        try:
            async with asyncio.timeout(_CONTROL_SECONDS + grace_seconds):
                ended = await command.wait()
                await reader
        except TimeoutError:
            ended = CommandExit(end="disconnected", detail="termination did not answer")
        finally:
            if not reader.done():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
        output = "".join(chunks)
        if ended.end != "exited" or ended.exit_code != 0:
            raise _error(
                "provider_unavailable",
                f"terminating group {operation.group} in sandbox {operation.sandbox_id} "
                f"was not confirmed ({ended.end} {ended.exit_code} {ended.detail or ''}"
                f"{output.strip()})",
                operation.ref,
            )

    def _lookup(self, ref: OperationRef) -> _Operation:
        operation = self._operations.get(ref.operation_id)
        if operation is None or operation.ref != ref:
            raise _error(
                "unknown_operation", f"no operation {ref.operation_id} in this provider", ref
            )
        return operation
