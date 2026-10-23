"""The APSS operation contract over `docker exec` (plan section 5.2).

`DockerOperations` owns everything `WorkspaceDockerProvider` needs to satisfy
`SupportsOperations`: the idempotency record, one host-side collector per
operation, the trusted-pid launch wrapper and the process-group cancel. The
provider delegates to it, so the decisions below are made in one place.

- Output is read by a provider-owned `docker exec` client and appended to an
  `OperationSpool` in this process, outside every mount. The workload cannot
  reach it, so observed bytes are exactly what the workload printed. The
  client's stdout and stderr are read from separate pipes, so the legacy
  `execute` path can keep the two channels apart while the observation stream
  carries both as they arrive.
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
  container handles, and is lost with the process, which also loses the
  workspace (no reattach). EXP-V1-0006 5.1.3 requires it to live at least
  as long as the workspace, so it is split in two. The replay record, the
  key's spec and ref, is kept until `forget_workspace`: a same-key start
  always replays the original ref and launches nothing. The operation itself,
  its spool and buffers, is kept for `record_retention_seconds` after it
  reaches a terminal state, so `status`/`observe` answer inside that window
  and the old ref is `unknown_operation` after it. A caller that has
  collected the result of a key nobody else can replay can `release` both.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import posixpath
import signal
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from agentic_isolation.config import (
    CredentialPolicyError,
    check_credential_bindings,
    check_env_targets,
)
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
from agentic_isolation.providers.operation_spool import (
    OperationFailure,
    OperationSpool,
    redaction_policy,
)

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
# Prints `pid ppid pgrp start` for every process that is not a zombie. Run
# with the shell's builtins and /proc only: slim images ship no `kill` or `ps`.
_PROCESSES = (
    "for s in /proc/[0-9]*/stat; do "
    'st=$(cat "$s" 2>/dev/null) || continue; set -- ${st##*) }; '
    '[ "$1" = Z ] && continue; '
    'pid=${s#/proc/}; echo "${pid%/stat} $2 $3 ${20}"; '
    "done"
)
# Sends signal $1 to group $2 (skipped when empty) and to each following
# `pid start` pair, each only while its pid still has the recorded start
# time, so a reused pid is never signalled. A group whose leader has exited
# is still signalled: Linux keeps a pgid's number from reuse while any member
# lives. The shell's builtin `kill` needs no binary in the image. A kill that
# fails while its target still lives is reported on stderr and exits 1; one
# whose target vanished meanwhile is the outcome wanted, not a failure.
_SIGNAL = (
    "start_of() { st=$(cat /proc/$1/stat 2>/dev/null) || return 0; "
    'set -- ${st##*) }; echo "${20}"; }; '
    "in_group() { for s in /proc/[0-9]*/stat; do "
    'st=$(cat "$s" 2>/dev/null) || continue; set -- ${st##*) }; '
    '[ "$1" != Z ] && [ "$3" = "$group" ] && return 0; done; return 1; }; '
    "sig=$1; group=$2; group_start=$3; shift 3; failed=0; "
    'if [ -n "$group" ]; then cur=$(start_of "$group"); '
    'if [ -z "$cur" ] || [ "$cur" = "$group_start" ]; then '
    'if ! err=$(kill -s "$sig" -- "-$group" 2>&1) && in_group; then '
    'echo "group $group: $err" >&2; failed=1; fi; fi; fi; '
    'while [ $# -ge 2 ]; do cur=$(start_of "$1"); '
    'if [ "$cur" = "$2" ] && ! err=$(kill -s "$sig" "$1" 2>&1) '
    '&& [ "$(start_of "$1")" = "$2" ]; then echo "pid $1: $err" >&2; failed=1; fi; '
    "shift 2; done; exit $failed"
)

# pid -> (ppid, pgrp, start), as `_PROCESSES` reports it.
_Table = dict[str, tuple[str, str, str]]


def _parse_table(out: str) -> _Table:
    table: _Table = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 4 and all(f.isdecimal() for f in fields):
            table[fields[0]] = (fields[1], fields[2], fields[3])
    return table


def _leader_reused(table: _Table, leader: str, start: str) -> bool:
    """The leader's pid now names another process, so its group is empty."""
    return leader in table and table[leader][2] != start


def _members(table: _Table, leader: str, start: str, tracked: dict[str, str]) -> dict[str, str]:
    """Every live process of the operation: its group, then their descendants.

    A descendant that left the group (`setsid`, `setpgid`) is still found
    through its parent chain, and once seen stays tracked by pid and start
    time even after its parent dies.
    """
    found = {
        pid: row[2]
        for pid, row in table.items()
        if (row[1] == leader and not _leader_reused(table, leader, start))
        or tracked.get(pid) == row[2]
    }
    grew = True
    while grew:
        grew = False
        for pid, (ppid, _, child_start) in table.items():
            if pid not in found and ppid in found:
                found[pid] = child_start
                grew = True
    return {pid: s for pid, s in found.items() if int(pid) > 1}


def _unattributed(table: _Table, start: str, tracked: dict[str, str]) -> list[str]:
    """Orphans of the container's init that started after the operation's wrapper.

    A descendant that left the group and lost its parent before cancel took
    its first look was never tracked and has no parent chain back to the
    operation, so nothing proves it is not ours. Anything adopted by init
    since the wrapper started may be such an escapee; one started before it
    cannot be.
    """
    return sorted(
        (
            pid
            for pid, (ppid, _, child_start) in table.items()
            if ppid == "1" and int(child_start) >= int(start) and tracked.get(pid) != child_start
        ),
        key=int,
    )


_Presence = Literal["running", "removing", "stopped", "gone"]

# `docker rm -f` kills the container, which ends the exec client with the
# kill's status, before the daemon finishes removing it. While the daemon
# reports `removing`, that status is no recorded outcome, so settlement waits
# this long for the container to be gone before leaving `status` to re-probe.
_REMOVAL_WAIT_SECONDS = 30.0
# The same window is the operation's settlement deadline: once it has passed
# with the container still `removing`, every retry (status, observe, a drain
# through them) raises provider_unavailable instead of waiting on the daemon.

# After a timeout terminates the group, the pipes are read this long for the
# workload's final output (a TERM handler's last words) before giving up.
_FINAL_OUTPUT_SECONDS = 5.0

# Then the exec client is given this long to exit before it is killed, so a
# timeout settles even when the workload outlives a failed stop: at most
# timeout + grace + 2s KILL wait + stop + these two windows after the start.
_CLIENT_EXIT_SECONDS = 5.0

# Every docker control command (inspect, stop, the cancel's execs) is given
# this long before its client is killed and reaped and the command counts as
# provider_unavailable, so a hung daemon cannot hold a settlement or a cancel.
_CONTROL_SECONDS = 30.0
# How long a killed control client is given to be reaped.
_REAP_SECONDS = 5.0

# How long a cancel waits for the wrapper's launch header, the trusted leader
# it signals. One that has not come by then is never signalled: the cancel
# stops the container instead, so a silent exec client cannot hold it.
_LEADER_SECONDS = 30.0

# How long a terminal operation keeps answering status and observe before its
# spool is reclaimed. Same-key replay outlives it; see the module docstring.
RECORD_RETENTION_SECONDS = 3600.0

# Prints the pid namespace of wherever `docker exec` runs, to compare with ours.
_PID_NAMESPACE = "readlink /proc/self/ns/pid"


@dataclass
class _KeyLock:
    """Serialises starts of one key; dropped when its last start leaves."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@dataclass(frozen=True)
class _Replay:
    """What a same-key start needs, kept for the workspace's lifetime."""

    spec: OperationSpec
    ref: OperationRef
    # `redaction_policy` of the caller's registration; a replay must match it.
    policy: bytes = field(default_factory=lambda: redaction_policy(()), repr=False)


@dataclass
class _Operation:
    ref: OperationRef
    spec: OperationSpec
    container: str
    spool: OperationSpool
    started_at: str
    # (workspace id, operation_key), the idempotency record's key.
    key: tuple[str, str]
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    leader: tuple[str, str] | None = None
    state: OperationState = "running"
    exit_code: int | None = None
    signal: str | None = None
    ended_at: str | None = None
    cancel_requested: bool = False
    # The cancel's termination returned: every process is confirmed gone or
    # the container stopped. Only this publishes `cancelled`.
    cancel_confirmed: bool = False
    escalated: bool = False
    # The escalation's `docker stop` could not be run or reported failure.
    stop_failed: bool = False
    # The spec's timeout fired; kept here so a settlement retry still knows.
    timed_out: bool = False
    # Held while the group is being terminated, and by every settlement, so a
    # cancel's outcome is decided only once its termination has confirmed.
    stopping: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Every process seen as part of the operation at cancel, pid -> start.
    tracked: dict[str, str] = field(default_factory=dict)
    # Why signalling or stopping could not be done, `; `-joined.
    signal_failure: str | None = None
    collector: asyncio.Task[None] | None = None
    leader_known: asyncio.Event = field(default_factory=asyncio.Event)
    client_finished: bool = False
    # Loop time after which a settlement still undecided by a `removing`
    # container is provider_unavailable; set when the exec client finishes.
    settle_by: float | None = None
    # Loop time the operation reached a terminal state; retention runs from here.
    settled_at: float | None = None
    # Per-channel copies (stdout, stderr), kept only when the caller asked.
    channels: tuple[bytearray, bytearray] | None = None


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
        record_retention_seconds: float = RECORD_RETENTION_SECONDS,
    ) -> None:
        self._credentials = credentials
        self._retention = retention_bytes
        self._max_page = max_page_bytes
        self._docker = docker
        self._record_retention = record_retention_seconds
        self._operations: dict[str, _Operation] = {}
        self._by_key: dict[tuple[str, str], _Replay] = {}
        # One lock per (workspace, operation_key) with a start in flight: a slow
        # credential resolver holds back only racing starts of the same key.
        self._launching: dict[tuple[str, str], _KeyLock] = {}
        # Workspaces being forgotten: they admit no new start.
        self._forgetting: set[str] = set()
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
        separate_channels: bool = False,
        redact: Collection[str] = (),
    ) -> OperationRef:
        """Accept `spec` once per `(workspace, operation_key)`.

        `redact` registers credential values the workload may print; they,
        and every value this provider resolves or delivers, are replaced in
        the spool, its pages and `channels` before anything is stored.

        `delivered_env` and `separate_channels` are for the legacy `execute`
        path only: values the caller already holds, delivered exactly like
        resolved credentials, and a per-channel copy of the output for
        `channels`.
        """
        container = workspace._handle
        if not isinstance(container, str) or not container:
            raise _error("unknown_operation", f"workspace {workspace.id} has no container")
        if workspace.id in self._forgetting:
            raise _error("unknown_operation", f"workspace {workspace.id} is being torn down")
        self._prune_expired()
        key = (workspace.id, spec.operation_key)
        # The key's lock spans the launch, so racing same-key starts see the
        # record of the first and launch nothing.
        launch = self._launching.setdefault(key, _KeyLock())
        launch.users += 1
        try:
            async with launch.lock:
                return await self._start_locked(
                    workspace, spec, key, container, delivered_env, separate_channels, redact
                )
        finally:
            launch.users -= 1
            if not launch.users:
                del self._launching[key]

    async def _start_locked(
        self,
        workspace: Workspace,
        spec: OperationSpec,
        key: tuple[str, str],
        container: str,
        delivered_env: dict[str, str] | None,
        separate_channels: bool,
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
            # A replay must not claim a registration the spool never had.
            if existing.policy != redaction_policy(redact):
                raise _error(
                    "conflict",
                    f"operation_key {spec.operation_key!r} already started "
                    "with different redaction values",
                    existing.ref,
                )
            return existing.ref
        # Credential values ride in the docker client's own environment, so
        # a name that steers that client (DOCKER_HOST, PATH, LD_PRELOAD...)
        # is refused before anything is resolved or launched.
        try:
            check_credential_bindings(list(spec.credential_bindings))
            check_env_targets(delivered_env or {})
        except CredentialPolicyError as e:
            raise OperationFailure(OperationError(kind="rejected_policy", message=str(e))) from e
        secrets = dict(delivered_env or {})
        # Every value delivered or resolved, kept apart from `secrets`: two
        # bindings may name one target, and the overwritten value must still
        # be registered.
        resolved = list(secrets.values())
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
                        message="credential bindings need a CredentialResolver; none was injected",
                    )
                )
            secrets[binding.target] = await self._credentials.resolve(binding.ref)
            resolved.append(secrets[binding.target])
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
            spool=OperationSpool(retention_bytes=self._retention, redact=(*redact, *resolved)),
            started_at=_now(),
            key=key,
            channels=(bytearray(), bytearray()) if separate_channels else None,
        )
        process = await self._spawn(
            *self.client_argv(container, spec, secret_names=tuple(secrets)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
        self._by_key[key] = _Replay(spec=spec, ref=ref, policy=redaction_policy(redact))
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

    def channels(self, ref: OperationRef) -> tuple[bytes, bytes]:
        """The (stdout, stderr) bytes of an operation started with `separate_channels`."""
        operation = self._lookup(ref)
        if operation.channels is None:
            raise _error("unknown_operation", f"{ref.operation_id} kept no separate channels", ref)
        return bytes(operation.channels[0]), bytes(operation.channels[1])

    def release(self, ref: OperationRef) -> None:
        """Drop a terminal operation and its replay record once collected.

        Only for keys no other caller can replay, such as `execute`'s private
        ones; an operation that is not terminal yet is left to the retention
        window and its replay record to `forget_workspace`.
        """
        operation = self._lookup(ref)
        if operation.state in TERMINAL_STATES:
            del self._operations[operation.ref.operation_id]
            if (replay := self._by_key.get(operation.key)) is not None and replay.ref == ref:
                del self._by_key[operation.key]

    async def forget_workspace(self, workspace_id: str) -> None:
        """Drop every record of a destroyed workspace, replay records included.

        New starts are refused first and starts already admitted are waited
        out, so none records an operation after its workspace was forgotten.
        """
        self._forgetting.add(workspace_id)
        try:
            for key, launch in list(self._launching.items()):
                if key[0] == workspace_id:
                    async with launch.lock:
                        pass
            self._drop_records(workspace_id)
        finally:
            self._forgetting.discard(workspace_id)

    def _drop_records(self, workspace_id: str) -> None:
        for operation in list(self._operations.values()):
            if operation.key[0] == workspace_id:
                del self._operations[operation.ref.operation_id]
        for key in [key for key in self._by_key if key[0] == workspace_id]:
            del self._by_key[key]

    def _prune_expired(self) -> None:
        """Reclaim terminal operations older than the window; replay records stay."""
        cutoff = asyncio.get_running_loop().time() - self._record_retention
        for operation in list(self._operations.values()):
            if operation.settled_at is not None and operation.settled_at <= cutoff:
                del self._operations[operation.ref.operation_id]

    async def _collect(self, operation: _Operation, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None and process.stderr is not None  # both PIPE
        # The readers run outside the timeout, so a timed-out operation's
        # pipes keep draining while its group is terminated.
        readers = asyncio.gather(
            self._read_stdout(operation, process.stdout),
            self._pump(operation, process.stderr, channel=1),
        )
        try:
            async with asyncio.timeout(operation.spec.timeout_seconds):
                await asyncio.shield(readers)
                await process.wait()
        except TimeoutError:
            operation.timed_out = True
            try:
                async with operation.stopping:
                    await self._terminate_group(operation, grace_seconds=1.0)
            except OperationFailure as e:
                # Even `docker stop` failed. Record why and settle anyway: an
                # unsettled operation would hold every drain forever.
                self._note(operation, e.error.message)
            try:
                async with asyncio.timeout(_FINAL_OUTPUT_SECONDS):
                    await asyncio.shield(readers)
            except TimeoutError:
                pass
            try:
                async with asyncio.timeout(_CLIENT_EXIT_SECONDS):
                    await process.wait()
            except TimeoutError:
                # The workload outlived the stop. Detach from it, and say so.
                # `wait` also waits for the pipes, which the workload still
                # holds; closing the transport kills the client and drops them.
                process._transport.close()  # type: ignore[attr-defined]  # noqa: SLF001
                await process.wait()
                self._note(
                    operation,
                    f"exec client still attached {_CLIENT_EXIT_SECONDS:g}s after the "
                    "timeout's termination; detached, the workload may still be running",
                )
        finally:
            if not readers.done():
                readers.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await readers
            operation.leader_known.set()
        for channel, stored in operation.spool.finish().items():
            self._keep(operation, stored, channel=channel)
        operation.client_finished = True
        operation.settle_by = asyncio.get_running_loop().time() + _REMOVAL_WAIT_SECONDS
        await self._settle(operation, process.returncode, wait=True)

    async def _read_stdout(self, operation: _Operation, stream: asyncio.StreamReader) -> None:
        try:
            header = await stream.readline()
            fields = header.decode(errors="replace").split()
            if len(fields) == 2 and all(f.isdigit() for f in fields):
                operation.leader = (fields[0], fields[1])
            elif header:
                # Not the wrapper's line (it failed before exec); keep it.
                self._record(operation, header, channel=0)
        finally:
            operation.leader_known.set()
        await self._pump(operation, stream, channel=0)

    async def _pump(
        self, operation: _Operation, stream: asyncio.StreamReader, *, channel: Literal[0, 1]
    ) -> None:
        while chunk := await stream.read(65536):
            self._record(operation, chunk, channel=channel)

    @staticmethod
    def _record(operation: _Operation, chunk: bytes, *, channel: Literal[0, 1]) -> None:
        DockerOperations._keep(
            operation, operation.spool.append(chunk, channel=channel), channel=channel
        )
        operation.changed.set()

    @staticmethod
    def _keep(operation: _Operation, stored: bytes, *, channel: int) -> None:
        """Copy what the spool stored, never the raw chunk, to `channels`."""
        if operation.channels is not None:
            operation.channels[channel].extend(stored)

    @staticmethod
    def _note(operation: _Operation, failure: str) -> None:
        """Add to the recorded signalling and stopping failures."""
        if operation.signal_failure is None:
            operation.signal_failure = failure
        else:
            operation.signal_failure = f"{operation.signal_failure}; {failure}"

    async def _settle(self, operation: _Operation, code: int | None, *, wait: bool) -> None:
        """Decide the terminal state; `wait` waits out a removal in progress.

        Waits for a cancel still confirming, which decides between cancelled
        and failed.
        """
        async with operation.stopping:
            await self._settle_stopped(operation, code, wait=wait)

    async def _settle_stopped(self, operation: _Operation, code: int | None, *, wait: bool) -> None:
        if operation.state in TERMINAL_STATES:
            return
        try:
            presence = await (
                self._settled_presence(operation) if wait else self._presence(operation.container)
            )
        except OperationFailure:
            # Cannot tell yet; `status` re-probes and answers provider_unavailable.
            operation.exit_code = code
            operation.changed.set()
            return
        if presence == "removing":
            # Still being destroyed; `status` re-probes until it is gone.
            operation.exit_code = code
            operation.changed.set()
            return
        operation.ended_at = _now()
        if operation.cancel_requested and not operation.cancel_confirmed:
            # The cancel could not confirm the operation ended: say so.
            operation.exit_code = code
            operation.state = "failed"
        elif operation.cancel_requested:
            operation.state = "cancelled"
        elif operation.timed_out:
            operation.state = "timed_out"
        elif presence == "gone":
            operation.state = "lost"
        else:
            operation.exit_code = code
            if code is not None and code > 128 and code - 128 < signal.NSIG:
                operation.signal = signal.Signals(code - 128).name
            operation.state = "succeeded" if code == 0 else "failed"
        operation.settled_at = asyncio.get_running_loop().time()
        operation.changed.set()

    async def status(self, ref: OperationRef) -> OperationStatus:
        operation = self._lookup(ref)
        await self._resettle(operation)
        return self._status(operation)

    async def _resettle(self, operation: _Operation) -> None:
        """Retry a settlement the collector left undecided; provider_unavailable if it still cannot.

        Skipped while a cancel is confirming: the cancel settles once it has.
        A container still `removing` past the settlement deadline is
        provider_unavailable too, so no retry loop waits on it forever.
        """
        if (
            operation.client_finished
            and operation.state not in TERMINAL_STATES
            and not operation.stopping.locked()
        ):
            presence = await self._presence(operation.container)  # raises provider_unavailable
            if presence == "removing" and self._past_settlement(operation):
                raise _error(
                    "provider_unavailable",
                    f"container {operation.container} is still being removed "
                    f"{_REMOVAL_WAIT_SECONDS:g}s after the operation's client finished; "
                    "its outcome cannot be settled until the daemon finishes removing it",
                    operation.ref,
                )
            await self._settle(operation, operation.exit_code, wait=False)

    @staticmethod
    def _past_settlement(operation: _Operation) -> bool:
        return (
            operation.settle_by is not None
            and asyncio.get_running_loop().time() >= operation.settle_by
        )

    def _status(self, operation: _Operation) -> OperationStatus:
        metadata = dict(operation.spec.metadata)
        if operation.escalated:
            metadata["cancel_escalation"] = (
                "container_stop_failed" if operation.stop_failed else "container_stopped"
            )
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
        # The collector may have finished without deciding (daemon down, a
        # removal in progress); without this a drain would wait on it forever.
        await self._resettle(operation)
        page = operation.spool.page(ref, state=operation.state, cursor=cursor, max_page_bytes=limit)
        if page.complete or page.data_b64 or wait_seconds <= 0:
            return page
        operation.changed.clear()
        try:
            await asyncio.wait_for(operation.changed.wait(), timeout=wait_seconds)
        except TimeoutError:
            pass
        await self._resettle(operation)
        return operation.spool.page(ref, state=operation.state, cursor=cursor, max_page_bytes=limit)

    async def cancel(self, ref: OperationRef, *, grace_seconds: float) -> OperationStatus:
        operation = self._lookup(ref)
        if operation.state not in TERMINAL_STATES:
            # Waited for before taking the lock, never under it: a timeout
            # takes `stopping` before it releases a header that never came.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_LEADER_SECONDS):
                    await operation.leader_known.wait()
            # Requested under the lock, so no settlement can publish
            # `cancelled` until this termination has confirmed or failed.
            async with operation.stopping:
                if operation.state in TERMINAL_STATES:
                    return self._status(operation)
                operation.cancel_requested = True
                if not operation.leader_known.is_set():
                    self._note(
                        operation,
                        f"no launch header within {_LEADER_SECONDS:g}s; stopping the container",
                    )
                try:
                    await self._terminate_group(operation, grace_seconds=grace_seconds)
                except asyncio.CancelledError:
                    # Recorded before the lock is released, so the settlement
                    # it lets in publishes the unconfirmed outcome.
                    self._note(operation, "cancel interrupted before termination was confirmed")
                    raise
                operation.cancel_confirmed = True
            if operation.collector is not None:
                await operation.collector
            # The collector may have settled before this cancel confirmed.
            await self._resettle(operation)
        return self._status(operation)

    async def _terminate_group(self, operation: _Operation, *, grace_seconds: float) -> None:
        """TERM the operation, KILL after the grace, confirm; else stop the container.

        Each round re-reads the process table, refuses to signal the group of
        a leader whose pid was reused, and signals by pid every descendant
        that escaped the group, so a confirmed cancel means all of them died.
        A docker client that cannot run while signalling also escalates. A
        stop that cannot run, fails, or is not then observed `stopped` or
        `gone` is recorded and raised as provider_unavailable: the operation
        may still be running.
        """
        if operation.leader is not None and (refusal := await self._unsafe_leader(operation)):
            # Never signal init or our own group; stopping the container still
            # ends the operation, so a refusal escalates rather than raising.
            self._note(operation, refusal)
        elif operation.leader is not None:
            try:
                if await self._signal_group(operation, *operation.leader, grace_seconds):
                    return
            except OperationFailure as e:
                self._note(operation, f"signalling: {e.error.message}")
        # No trusted or safe pid, or the operation would not die: escalate, and say so.
        operation.escalated = True
        try:
            code, out = await self._docker_cmd("stop", "-t", "0", operation.container)
        except OperationFailure:
            operation.stop_failed = True
            raise
        if code != 0:
            operation.stop_failed = True
            raise _error(
                "provider_unavailable",
                f"docker stop {operation.container}: exit {code}: {out.strip()}",
                operation.ref,
            )
        # A stop that reported success is confirmed by the daemon's own state:
        # only `stopped` or `gone` is evidence; `removing` is waited out, bounded.
        deadline = asyncio.get_running_loop().time() + _REMOVAL_WAIT_SECONDS
        try:
            while (presence := await self._presence(operation.container)) == "removing":
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.1)
        except OperationFailure:
            operation.stop_failed = True
            raise
        if presence == "running":
            operation.stop_failed = True
            raise _error(
                "provider_unavailable",
                f"docker stop {operation.container} reported success but the container "
                "is still running",
                operation.ref,
            )
        if presence == "removing":
            operation.stop_failed = True
            raise _error(
                "provider_unavailable",
                f"docker stop {operation.container} reported success but the container "
                f"is still being removed {_REMOVAL_WAIT_SECONDS:g}s later; its termination "
                "is unconfirmed",
                operation.ref,
            )

    async def _signal_group(
        self, operation: _Operation, pid: str, start: str, grace_seconds: float
    ) -> bool:
        """TERM, then KILL; whether every process of the operation is confirmed gone."""
        for sig, wait in (("TERM", grace_seconds), ("KILL", 2.0)):
            code, out = await self._exec(
                operation.container, "sh", "-c", _PROCESSES, "aw-processes"
            )
            if code != 0:
                self._note(operation, f"process table: exit {code}: {out.strip()}")
                return False
            table = _parse_table(out)
            operation.tracked.update(_members(table, pid, start, operation.tracked))
            group = "" if _leader_reused(table, pid, start) else pid
            pairs = [part for item in operation.tracked.items() for part in item]
            code, out = await self._exec(
                operation.container, "sh", "-c", _SIGNAL, "aw-signal", sig, group, start, *pairs
            )
            if code != 0:
                self._note(operation, f"kill -{sig}: exit {code}: {out.strip()}")
                return False
            if await self._group_gone(operation, within=wait):
                return await self._nothing_unattributed(operation, start)
        return False

    async def _nothing_unattributed(self, operation: _Operation, start: str) -> bool:
        """Whether no process the operation may have detached is left running.

        Ownership of an orphan adopted by init cannot be proven, so any such
        survivor fails the confirmation closed and cancel escalates to
        stopping the container; that also ends unrelated detached processes
        of the same workspace, which is the price of a confirmed cancel.
        """
        code, out = await self._exec(operation.container, "sh", "-c", _PROCESSES, "aw-processes")
        if code != 0:
            self._note(operation, f"process table: exit {code}: {out.strip()}")
            return False
        if orphans := _unattributed(_parse_table(out), start, operation.tracked):
            self._note(
                operation,
                f"processes {orphans} were orphaned to init after the operation began "
                "and may be its detached descendants; stopping the container",
            )
            return False
        return True

    async def _unsafe_leader(self, operation: _Operation) -> str | None:
        """Why the leader's group must not be signalled, or None when it may be.

        Init and malformed pids are refused everywhere. Our own group is a
        hazard only where the signal lands in our pid namespace; in a
        container's own namespace an equal number names another process. A
        namespace that cannot be read, on either side, counts as ours, so
        doubt costs a container stop, never a stray kill.
        """
        assert operation.leader is not None
        pid = operation.leader[0]
        if not pid.isdecimal() or int(pid) <= 1:
            return f"refusing unsafe process group {pid!r}"
        if int(pid) != os.getpgrp():
            return None
        refusal = f"refusing unsafe process group {pid!r}: it is the caller's own group"
        try:
            ours = os.readlink("/proc/self/ns/pid")
        except OSError as e:
            return f"{refusal} (our pid namespace is unreadable: {e})"
        try:
            code, out = await self._exec(
                operation.container, "sh", "-c", _PID_NAMESPACE, "aw-pidns"
            )
        except OperationFailure as e:
            return f"{refusal} (namespace probe failed: {e.error.message})"
        if code != 0 or not out.strip() or out.strip() == ours:
            return refusal
        return None

    async def _group_gone(self, operation: _Operation, *, within: float) -> bool:
        """Whether the group and every tracked process are gone; a failed probe is not."""
        assert operation.leader is not None
        pid, start = operation.leader
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while True:
            code, out = await self._exec(
                operation.container, "sh", "-c", _PROCESSES, "aw-processes"
            )
            if code == 0:
                table = _parse_table(out)
                group = not _leader_reused(table, pid, start) and any(
                    row[1] == pid for row in table.values()
                )
                tracked = any(
                    table.get(p, ("", "", ""))[2] == s for p, s in operation.tracked.items()
                )
                if not group and not tracked:
                    return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _exec(self, container: str, *argv: str) -> tuple[int, str]:
        return await self._docker_cmd("exec", container, *argv)

    async def _spawn(self, *argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        """Start a docker client process; a client that cannot launch is provider_unavailable."""
        try:
            return await asyncio.create_subprocess_exec(*argv, **kwargs)
        except OSError as e:
            raise _error("provider_unavailable", f"could not launch {argv[0]!r}: {e}") from e

    async def _docker_cmd(self, *argv: str) -> tuple[int, str]:
        process = await self._spawn(
            self._docker,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(_CONTROL_SECONDS):
                out, _ = await process.communicate()
        except TimeoutError:
            await self._reap(process)
            raise _error(
                "provider_unavailable",
                f"docker {' '.join(argv[:2])} did not answer within {_CONTROL_SECONDS:g}s",
            ) from None
        except asyncio.CancelledError:
            await self._reap(process)
            raise
        return process.returncode or 0, out.decode(errors="replace")

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        """Kill an abandoned control client and reap it within `_REAP_SECONDS`.

        `wait` also waits for the pipes, which a child of the client may still
        hold; closing the transport drops them, so EOF is never waited for.
        """
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        process._transport.close()  # type: ignore[attr-defined]  # noqa: SLF001
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_REAP_SECONDS):
                await process.wait()

    async def _settled_presence(self, operation: _Operation) -> _Presence:
        """Presence, waiting out a removal in progress until the settlement deadline."""
        while (presence := await self._presence(operation.container)) == "removing":
            if self._past_settlement(operation):
                break
            await asyncio.sleep(0.1)
        return presence

    async def _presence(self, container: str) -> _Presence:
        code, out = await self._docker_cmd(
            "container", "inspect", "-f", "{{.State.Status}}", container
        )
        if code == 0:
            match out.strip():
                case "running" | "paused" | "restarting":
                    return "running"
                case "removing":
                    return "removing"
                case _:
                    return "stopped"
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
