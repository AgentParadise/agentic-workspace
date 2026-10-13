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
- A launch marker is printed by a transient `sh` named by the marker's argv0,
  which the workload then replaces in place (the `_INNER` shape of Docker), so
  the line is first and is in neither the workload's argv nor its environment.
- The leader is a child subreaper (`PR_SET_CHILD_SUBREAPER`, kept across the
  launcher's exec), so a descendant whose parent exits is reparented to the
  leader, not to init, and stays on the leader's parent chain whatever its
  session or environment. Every process the workload starts also inherits an
  ownership token in its environment, and every process holding the
  operation's output pipe is owned, so a descendant that outlives the leader
  is still found while it keeps the operation running. A leader that exited
  before cancel took the first look at its tree leaves descendants that hold
  neither unconfirmable: if anything owned was still running, cancel fails
  closed instead of claiming `cancelled`; if nothing was, the operation keeps
  its own outcome, since nothing was cancelled.
  Processes are owned by pid plus start time; a pid whose start time changed
  is a replacement and is never signalled.
- Cancel refuses a group numbered <= 1 or equal to the caller's own group: the
  caller's namespace IS the workload's here, so the own-group check from
  `DockerOperations._unsafe_leader` is always real. A refusal still kills every
  owned process one by one, and reports the refusal in the status metadata. A
  group is only signalled while the leader is unreaped, so its id is still ours.
- If an owned process survives KILL, the operation settles `failed`, never
  `cancelled`, with the survivors named in the status metadata. The collector
  settles only once any cancel in progress has confirmed or failed.
- Credential values reach only the workload's environment, never an argv.
- Records live until `forget_workspace`; Local workspaces are short-lived and
  test-only, so there is no retention window.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import secrets
import shutil
import signal
import subprocess
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

# Inherited by every process an operation starts; its value is per operation.
OWNER_VARIABLE = "AGENTIC_LOCAL_OPERATION"

# The named launcher: $0 is the marker's argv0, $1 its line, the rest the argv.
_SHELL = "/bin/sh"
_LAUNCHER = 'printf "%s\\n" "$1"; shift; exec "$@"'

_PR_SET_CHILD_SUBREAPER = 36
_libc = ctypes.CDLL(None, use_errno=True)


def _become_subreaper() -> None:
    """Run in the forked child before exec: orphaned descendants come to it."""
    if _libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER) failed")


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


def _stat_fields(pid: int) -> list[str] | None:
    """The fields of /proc/<pid>/stat after the command name, or None."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat[stat.rfind(")") + 2 :].split()


def _start_time(pid: int) -> int | None:
    """The start time (field 22) that tells a pid's process from a replacement."""
    fields = _stat_fields(pid)
    return int(fields[19]) if fields and fields[0] != "Z" else None


def _proc_table() -> dict[int, tuple[int, int, int]]:
    """pid -> (ppid, pgrp, start time) for every live, non-zombie process."""
    table: dict[int, tuple[int, int, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        fields = _stat_fields(int(entry.name))
        if fields and fields[0] != "Z":
            table[int(entry.name)] = (int(fields[1]), int(fields[2]), int(fields[19]))
    return table


def _carries(pid: int, token: str) -> bool:
    """Whether `pid` was started with this operation's ownership token."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return f"{OWNER_VARIABLE}={token}".encode() in environ.split(b"\0")


def _send(pid: int, start: int, sig: signal.Signals) -> None:
    """Signal `pid` only if it is still the process that started at `start`.

    Where the platform has pidfds, the pidfd pins the process before the
    check, so a pid reused after it cannot receive the signal. Without them
    the window is the gap between reading the start time and `kill`.
    """
    if not hasattr(os, "pidfd_open"):
        if _start_time(pid) == start:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        return
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if _start_time(pid) == start:
            signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def _holds(pid: int, pipe: int) -> bool:
    """Whether `pid` has the pipe with inode `pipe` open."""
    target = f"pipe:[{pipe}]"
    try:
        fds = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return False
    for fd in fds:
        with contextlib.suppress(OSError):
            if os.readlink(fd) == target:
                return True
    return False


def _invalid_launch(argv: tuple[str, ...], env: dict[str, str]) -> str | None:
    """Why the OS cannot launch this argv and environment, or None."""
    if any("\0" in arg for arg in argv):
        return "argv contains a NUL byte"
    for name, value in env.items():
        if not name or "=" in name or "\0" in name:
            return f"environment variable name {name!r} is not valid"
        if "\0" in value:
            return f"environment variable {name!r} contains a NUL byte"
    return None


def _resolvable(program: str, cwd: Path, env: dict[str, str]) -> bool:
    """Whether exec would find `program`, checked before the launcher runs it.

    Relative and empty PATH entries are taken from `cwd`, where the launcher
    runs, not from this process's own working directory.
    """
    if "/" in program:
        path = cwd / program
        return path.is_file() and os.access(path, os.X_OK)
    search = os.pathsep.join(
        str(cwd / entry) for entry in env.get("PATH", os.defpath).split(os.pathsep)
    )
    return shutil.which(program, path=search) is not None


@dataclass
class _Operation:
    ref: OperationRef
    spec: OperationSpec
    workspace_id: str
    spool: OperationSpool
    started_at: str
    process: asyncio.subprocess.Process
    token: str
    # Inode of the output pipe; whoever still holds it keeps the operation open.
    pipe: int | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    state: OperationState = "running"
    exit_code: int | None = None
    signal: str | None = None
    ended_at: str | None = None
    cancel_requested: bool = False
    timed_out: bool = False
    # pid -> start time of every process seen owned, kept after it reparents.
    tracked: dict[int, int] = field(default_factory=dict)
    signal_failure: str | None = None
    unconfirmed: bool = False
    # Held while a cancel or timeout signals and confirms; settling waits on it.
    stopping: asyncio.Lock = field(default_factory=asyncio.Lock)
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
        argv: tuple[str, ...] = spec.argv
        if isinstance(spec.launch_marker, LaunchMarker):
            marker = spec.launch_marker
            argv = (_SHELL, "-c", _LAUNCHER, marker.argv0, marker.line, *spec.argv)
        if reason := _invalid_launch(argv, env):
            raise _error("rejected_policy", f"cannot launch {spec.argv[0]!r}: {reason}")
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise _error(
                "rejected_policy",
                f"working_directory {spec.working_directory!r} cannot be prepared: {e}",
            ) from e
        # The launcher reports exec failure as exit 127, after acceptance.
        if argv is not spec.argv and not _resolvable(spec.argv[0], cwd, env):
            raise _error("provider_unavailable", f"could not launch {spec.argv[0]!r}")
        token = secrets.token_hex(16)
        env[OWNER_VARIABLE] = token
        read_end, write_end = os.pipe()
        inode = os.fstat(read_end).st_ino
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=write_end,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
                env=env,
                start_new_session=True,
                preexec_fn=_become_subreaper,
            )
        except (OSError, subprocess.SubprocessError) as e:
            os.close(read_end)
            raise _error("provider_unavailable", f"could not launch {spec.argv[0]!r}: {e}") from e
        finally:
            os.close(write_end)
        output = asyncio.StreamReader()
        loop = asyncio.get_running_loop()
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(output), os.fdopen(read_end, "rb", 0)
        )
        self._sequence += 1
        ref = OperationRef(
            workspace=WorkspaceRef(provider="local", resource_id=workspace.id),
            operation_id=f"{workspace.id}-op{self._sequence}",
            operation_key=spec.operation_key,
        )
        operation = _Operation(
            ref=ref,
            spec=spec,
            workspace_id=workspace.id,
            spool=OperationSpool(retention_bytes=self._retention),
            started_at=_now(),
            process=process,
            token=token,
            pipe=inode,
        )
        operation.collector = asyncio.create_task(self._collect(operation, output, transport))
        self._operations[ref.operation_id] = operation
        self._by_key[key] = operation
        return ref

    async def _collect(
        self,
        operation: _Operation,
        output: asyncio.StreamReader,
        transport: asyncio.ReadTransport,
    ) -> None:
        process = operation.process
        reader = asyncio.ensure_future(self._pump(operation, output))
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
            transport.close()
        # A cancel still confirming decides between cancelled and failed.
        async with operation.stopping:
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
        if operation.unconfirmed:
            operation.state = "failed"
        elif operation.cancel_requested:
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
        """TERM the group and every owned process, then KILL after the grace.

        A refused group (init, or our own) is never signalled as a group; its
        owned processes are signalled one by one and the refusal is recorded.
        Survivors of KILL mark the operation unconfirmed, so it settles failed.
        """
        async with operation.stopping:
            await self._signal_and_confirm(operation, grace_seconds=grace_seconds)

    async def _signal_and_confirm(self, operation: _Operation, *, grace_seconds: float) -> None:
        pgid = operation.process.pid
        if refusal := unsafe_group(pgid):
            self._note(operation, refusal)
        # Once the leader has exited, its subreaper no longer collects orphans,
        # so a tree never looked at while it lived cannot be fully confirmed.
        unseen = not operation.tracked and (
            operation.process.returncode is not None or _start_time(pgid) is None
        )
        if unseen and not self._owned(operation):
            # Nothing of it is left running: the workload ended on its own.
            operation.cancel_requested = False
            return
        for sig, wait in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, _KILL_WAIT_SECONDS)):
            owned = self._owned(operation)
            operation.tracked.update(owned)
            # An unreaped leader holds its pid, so the group id is still ours.
            if refusal is None and operation.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, sig)
            for pid, start in owned.items():
                _send(pid, start, sig)
            if await self._gone(operation, within=wait):
                if unseen:
                    operation.unconfirmed = True
                    self._note(
                        operation,
                        "the workload leader exited before cancel; descendants that "
                        "left its output pipe and environment cannot be confirmed gone",
                    )
                return
        survivors = sorted(self._owned(operation))
        operation.unconfirmed = True
        self._note(operation, f"owned processes {survivors} survived SIGKILL")

    @staticmethod
    def _owned(operation: _Operation) -> dict[int, int]:
        """pid -> start time of every live process this operation owns.

        Owned: a tracked process whose start time still matches, the leader's
        group while the leader is unreaped, anything carrying the ownership
        token or holding the output pipe, and every descendant of those through the parent chain. A
        refused group is never counted, since it may hold the caller.
        """
        table = _proc_table()
        pgid = operation.process.pid
        ours = operation.process.returncode is None and unsafe_group(pgid) is None
        leader = pgid if ours else None
        # This process holds the pipe's read end; its children are not owned.
        found = {
            pid: start
            for pid, (_, pgrp, start) in table.items()
            if pid > 1
            and pid != os.getpid()
            and (
                operation.tracked.get(pid) == start
                or pgrp == leader
                or _carries(pid, operation.token)
                or (operation.pipe is not None and _holds(pid, operation.pipe))
            )
        }
        grew = True
        while grew:
            grew = False
            for pid, (ppid, _, start) in table.items():
                if pid not in found and ppid in found:
                    found[pid] = start
                    grew = True
        return {pid: s for pid, s in found.items() if pid > 1 and pid != os.getpid()}

    @classmethod
    async def _gone(cls, operation: _Operation, *, within: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while cls._owned(operation):
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
