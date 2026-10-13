"""The APSS operation contract over `docker exec` (plan section 5.2).

`DockerOperations` owns everything `WorkspaceDockerProvider` needs to satisfy
`SupportsOperations`: the idempotency record, one host-side collector per
operation, the trusted-pid launch wrapper and the process-group cancel. The
provider delegates to it, so the decisions below are made in one place.

- Output is read by a provider-owned `docker exec` client and appended to an
  `OperationSpool` in this process, outside every mount. The workload cannot
  reach it, so observed bytes are exactly what the workload printed.
- Status comes from the client's daemon-reported exit code, never from
  anything the workload can write.
- The wrapper records its own pid and start time on the first stdout line
  before `exec`, when no workload exists yet to forge it, then execs the
  workload as leader of a new session. Cancel signals that group TERM then
  KILL and confirms with a start-time comparison, the pattern proven in every
  pinned image by `implementations/docker/tests/fixtures/process_tree_cancel.sh`.
- Credential values travel only in the client process's environment and reach
  the workload as `docker exec -e NAME`; they are never in an argv.
- The idempotency record is in-process infrastructure state, like the
  container handles: it lives as long as the workspace, and is lost with the
  process, which also loses the workspace (no reattach).
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

# Runs as the exec's main process. Reads the launch marker's argv0 and line
# from stdin into shell variables (never an argv or an exported variable),
# then starts the inner wrapper as a background job, which a non-interactive
# shell does not make a group leader, so `setsid` execs in place: its pid is
# the new session's pgid. `wait` returns the workload's status, 128+n for a
# signal.
_OUTER = (
    "IFS= read -r argv0; IFS= read -r line; inner=$1; shift; "
    'setsid sh -c "$inner" "$argv0" "$line" "$@" & wait "$!"'
)
# Runs as session leader. Line one is the trusted pid and start time, then the
# marker line if one was given, then the workload replaces this shell, so
# neither value remains readable through its argv.
_INNER = (
    'printf "%s %s\\n" "$$" "$(cut -d" " -f22 /proc/$$/stat)"; '
    '[ -n "$1" ] && printf "%s\\n" "$1"; shift; exec "$@"'
)
# Prints `alive` while the recorded leader (same start time, not a zombie) or
# any non-zombie member of its process group remains, else `gone`.
_GROUP_ALIVE = (
    "leader=$1; start=$2; "
    "for s in /proc/[0-9]*/stat; do "
    'st=$(cat "$s" 2>/dev/null) || continue; set -- ${st##*) }; '
    "pid=${s#/proc/}; pid=${pid%/stat}; "
    '[ "$1" = Z ] && continue; '
    '[ "$pid" = "$leader" ] && [ "${20}" != "$start" ] && continue; '
    'if [ "$3" = "$leader" ]; then echo alive; exit 0; fi; '
    "done; echo gone"
)

_Presence = Literal["running", "stopped", "gone"]


@dataclass
class _Operation:
    ref: OperationRef
    spec: OperationSpec
    container: str
    spool: OperationSpool
    started_at: str
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    leader: tuple[str, str] | None = None
    state: OperationState = "running"
    exit_code: int | None = None
    signal: str | None = None
    ended_at: str | None = None
    cancel_requested: bool = False
    escalated: bool = False
    collector: asyncio.Task[None] | None = None
    leader_known: asyncio.Event = field(default_factory=asyncio.Event)
    client_finished: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error(
    kind: Literal["unknown_operation", "conflict", "provider_unavailable"],
    message: str,
    ref: OperationRef | None = None,
) -> OperationFailure:
    if ref is None:
        return OperationFailure(OperationError(kind=kind, message=message))
    return OperationFailure(OperationError(kind=kind, message=message, ref=ref))


class DockerOperations:
    """Start, observe, status and cancel operations in Docker workspaces."""

    def __init__(
        self,
        *,
        credentials: CredentialResolver | None,
        retention_bytes: int,
        max_page_bytes: int,
        docker: str = "docker",
    ) -> None:
        self._credentials = credentials
        self._retention = retention_bytes
        self._max_page = max_page_bytes
        self._docker = docker
        self._operations: dict[str, _Operation] = {}
        self._by_key: dict[tuple[str, str], _Operation] = {}
        self._lock = asyncio.Lock()
        self._sequence = 0

    def describe(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            schema="apss.workspace-provider/v1",
            provider="docker",
            security_profile="isolated",
            enforceable_limits=EnforceableLimits(
                cpu=True, memory=True, disk=False, network=True, timeout=True
            ),
            capabilities=("operations", "cancel", "launch_marker", "credential_env"),
            observation=ObservationDescriptor(
                retention_bytes=self._retention,
                max_page_bytes=self._max_page,
                integrity="exact",
            ),
            interactive=False,
            environment_kinds=("oci-image",),
        )

    async def start(
        self,
        workspace: Workspace,
        spec: OperationSpec,
        *,
        delivered_env: dict[str, str] | None = None,
    ) -> OperationRef:
        """Accept `spec` once per `(workspace, operation_key)`.

        `delivered_env` is for the legacy `execute` path only: values the
        caller already holds, delivered exactly like resolved credentials.
        """
        container = workspace._handle
        if not isinstance(container, str) or not container:
            raise _error("unknown_operation", f"workspace {workspace.id} has no container")
        key = (workspace.id, spec.operation_key)
        # The lock spans the launch, so racing same-key starts see the record
        # of the first and launch nothing.
        async with self._lock:
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
            secrets = dict(delivered_env or {})
            for binding in spec.credential_bindings:
                if binding.delivery != "env":
                    raise OperationFailure(
                        OperationError(
                            kind="unsupported_capability",
                            message=f"credential delivery {binding.delivery!r} "
                            "is not supported by docker",
                        )
                    )
                if self._credentials is None:
                    raise OperationFailure(
                        OperationError(
                            kind="rejected_policy",
                            message="credential bindings need a CredentialResolver; "
                            "none was injected",
                        )
                    )
                secrets[binding.target] = await self._credentials.resolve(binding.ref)
            self._sequence += 1
            ref = OperationRef(
                workspace=WorkspaceRef(provider="docker", resource_id=container),
                operation_id=f"{workspace.id}-op{self._sequence}",
                operation_key=spec.operation_key,
            )
            operation = _Operation(
                ref=ref,
                spec=spec,
                container=container,
                spool=OperationSpool(retention_bytes=self._retention),
                started_at=_now(),
            )
            process = await asyncio.create_subprocess_exec(
                *self.client_argv(container, spec, secret_names=tuple(secrets)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, **secrets},
            )
            marker = spec.launch_marker
            argv0, line = (
                (marker.argv0, marker.line) if isinstance(marker, LaunchMarker) else ("sh", "")
            )
            if process.stdin is not None:
                process.stdin.write(f"{argv0}\n{line}\n".encode())
                await process.stdin.drain()
                process.stdin.close()
            operation.collector = asyncio.create_task(self._collect(operation, process))
            self._operations[ref.operation_id] = operation
            self._by_key[key] = operation
            return ref

    def client_argv(
        self, container: str, spec: OperationSpec, *, secret_names: tuple[str, ...]
    ) -> list[str]:
        """The `docker exec` argv: environment overlay by value, secrets by name only."""
        workdir = posixpath.normpath(posixpath.join("/workspace", spec.working_directory))
        argv = [self._docker, "exec", "-i", "-w", workdir]
        for name, value in spec.environment.items():
            argv.extend(["-e", f"{name}={value}"])
        for name in secret_names:
            argv.extend(["-e", name])
        argv.extend([container, "sh", "-c", _OUTER, "aw-operation", _INNER, *spec.argv])
        return argv

    async def _collect(self, operation: _Operation, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None  # created with stdout=PIPE
        timed_out = False
        try:
            async with asyncio.timeout(operation.spec.timeout_seconds):
                header = await process.stdout.readline()
                fields = header.decode(errors="replace").split()
                if len(fields) == 2 and all(f.isdigit() for f in fields):
                    operation.leader = (fields[0], fields[1])
                elif header:
                    # Not the wrapper's line (it failed before exec); keep it.
                    operation.spool.append(header)
                operation.leader_known.set()
                while chunk := await process.stdout.read(65536):
                    operation.spool.append(chunk)
                    operation.changed.set()
                await process.wait()
        except TimeoutError:
            timed_out = True
            await self._terminate_group(operation, grace_seconds=1.0)
            await process.wait()
        finally:
            operation.leader_known.set()
        operation.client_finished = True
        await self._settle(operation, process.returncode, timed_out=timed_out)

    async def _settle(self, operation: _Operation, code: int | None, *, timed_out: bool) -> None:
        if operation.state in TERMINAL_STATES:
            return
        try:
            presence = await self._presence(operation.container)
        except OperationFailure:
            # Cannot tell yet; `status` re-probes and answers provider_unavailable.
            operation.exit_code = code
            operation.changed.set()
            return
        operation.ended_at = _now()
        if operation.cancel_requested:
            operation.state = "cancelled"
        elif timed_out:
            operation.state = "timed_out"
        elif presence == "gone":
            operation.state = "lost"
        else:
            operation.exit_code = code
            if code is not None and code > 128 and code - 128 < signal.NSIG:
                operation.signal = signal.Signals(code - 128).name
            operation.state = "succeeded" if code == 0 else "failed"
        operation.changed.set()

    async def status(self, ref: OperationRef) -> OperationStatus:
        operation = self._lookup(ref)
        if operation.client_finished and operation.state not in TERMINAL_STATES:
            await self._presence(operation.container)  # raises provider_unavailable
            await self._settle(operation, operation.exit_code, timed_out=False)
        return self._status(operation)

    def _status(self, operation: _Operation) -> OperationStatus:
        metadata = dict(operation.spec.metadata)
        if operation.escalated:
            metadata["cancel_escalation"] = "container_stopped"
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
        try:
            await asyncio.wait_for(operation.changed.wait(), timeout=wait_seconds)
        except TimeoutError:
            pass
        return operation.spool.page(ref, state=operation.state, cursor=cursor, max_page_bytes=limit)

    async def cancel(self, ref: OperationRef, *, grace_seconds: float) -> OperationStatus:
        operation = self._lookup(ref)
        if operation.state not in TERMINAL_STATES:
            operation.cancel_requested = True
            await operation.leader_known.wait()
            await self._terminate_group(operation, grace_seconds=grace_seconds)
            if operation.collector is not None:
                await operation.collector
        return self._status(operation)

    async def _terminate_group(self, operation: _Operation, *, grace_seconds: float) -> None:
        """TERM the group, KILL after the grace, confirm; else stop the container."""
        if operation.leader is not None:
            pid, start = operation.leader
            for sig, wait in (("TERM", grace_seconds), ("KILL", 2.0)):
                await self._exec(operation.container, "kill", f"-{sig}", "--", f"-{pid}")
                if await self._group_gone(operation.container, pid, start, within=wait):
                    return
        # No trusted pid, or the group would not die: escalate, and say so.
        operation.escalated = True
        await self._docker_cmd("stop", "-t", "0", operation.container)

    async def _group_gone(self, container: str, pid: str, start: str, *, within: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while True:
            code, out = await self._exec(
                container, "sh", "-c", _GROUP_ALIVE, "aw-probe", pid, start
            )
            if code == 0 and out.strip() == "gone":
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _exec(self, container: str, *argv: str) -> tuple[int, str]:
        return await self._docker_cmd("exec", container, *argv)

    async def _docker_cmd(self, *argv: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            self._docker,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await process.communicate()
        return process.returncode or 0, out.decode(errors="replace")

    async def _presence(self, container: str) -> _Presence:
        code, out = await self._docker_cmd(
            "container", "inspect", "-f", "{{.State.Running}}", container
        )
        if code == 0:
            return "running" if out.strip() == "true" else "stopped"
        if "no such" in out.lower():
            return "gone"
        raise _error(
            "provider_unavailable", f"docker could not report container {container}: {out.strip()}"
        )

    def _lookup(self, ref: OperationRef) -> _Operation:
        operation = self._operations.get(ref.operation_id)
        if operation is None or operation.ref != ref:
            raise _error(
                "unknown_operation", f"no operation {ref.operation_id} in this provider", ref
            )
        return operation
