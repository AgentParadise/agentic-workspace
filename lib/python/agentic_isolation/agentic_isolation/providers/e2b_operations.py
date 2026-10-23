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
  (`AsyncCommandHandle`'s incremental decoder in e2b==2.52.0), so the
  sandbox sends the output as hex lines (`_ENCODE`) and the bytes are
  restored here, before the spool sees them.
- A descendant that leaves the group is found by `OWNER_VARIABLE` in its
  environ, as `LocalOperations` does, and by its parent links, as Docker
  does, remembered by pid and start time once seen (`_TERMINATE`).
- Where Docker stops the container when the operation will not die, this
  kills the sandbox (`metadata["cancel_escalation"]`). A kill that fails or
  is not confirmed raises `provider_unavailable` and the operation settles
  `failed`, with the reason in `metadata["cancel_signal_failure"]`.
- Records live as long as the workspace; `forget_workspace` drops them at
  teardown.

Reattach (#103), which Docker does not offer: the wrapper keeps a state
directory in the sandbox (`STATE_ROOT/<operation_id>`) holding the SHA-256 of
the operation's token, its execution id and start time, the group pid, and,
once known, the exit status or the word a confirmed termination wrote. Ids
are random, so two provider instances never mint the same one. A fresh
instance given the ref and the token (`reattach_token`) checks the digest in
the sandbox, then answers `status` and `cancel` from that directory and
/proc, never from caller memory. What it cannot see it does not guess:

- a probe that does not answer raises `provider_unavailable`; a missing
  directory or a token that does not match raises `unknown_operation`;
- no exit status and no process carrying the token is `lost`, or `failed`
  after a cancel this instance could not confirm, never `cancelled`;
- `observe` raises `unsupported_capability`: the output was spooled by the
  instance that started the operation and went with it.

The directory is written by the workload's user, so a reattached status is
only as trustworthy as the workload: it can forge its own exit status, but
it cannot make a ref with another operation's token pass the digest.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import posixpath
import secrets as secrets_module
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
#
# Everything the inner wrapper and the workload print goes through `_ENCODE`,
# and the workload's status comes back on fd 4, never through that pipe. The
# encoder drops `OWNER_VARIABLE`, so a cancel never kills it before the
# workload's last output is sent.
#
# `$state` is the reattach directory: the token digest, execution id (hex) and
# start time go in before the workload starts, its group as soon as it has
# one, and its status (with the end time) once `wait` returns. A directory
# that cannot be made leaves the operation running but not reattachable.
_OUTER = (
    "exec 2>&1 3>&1; IFS= read -r argv0; IFS= read -r line; "
    "inner=$1; encode=$2; state=$3; shift 3; "
    'mkdir -p "$state" 2>/dev/null && printf "%s\\n" "$OWNER_DIGEST" > "$state/owner" '
    '&& printf "%s\\n" "$EXECUTION_HEX" > "$state/execution" '
    '&& printf "%s\\n" "$STARTED_AT" > "$state/started"; '
    "unset OWNER_DIGEST EXECUTION_HEX STARTED_AT; "
    'status=$( { { setsid sh -c "$inner" "$argv0" "$line" "$@" 2>&1 3>&- 4>&- & '
    'echo "$!" > "$state/group" 2>/dev/null; wait "$!"; '
    'echo "$?" >&4; } | env -u AGENTIC_E2B_OPERATION sh -c "$encode" >&3; } 4>&1 ); '
    '{ date -u +%Y-%m-%dT%H:%M:%SZ > "$state/ended" && echo "$status" > "$state/exit"; } '
    "2>/dev/null; "
    'exit "$status"'
)
# Each read of the workload's output, as one line of lowercase hex. The SDK
# decodes stdout as UTF-8 with replacement (`AsyncCommandHandle` in
# e2b==2.52.0), which would rewrite any other byte; hex survives it exactly.
# `dd` without `iflag=fullblock` returns what one read gave, so a chunk is
# sent as soon as the workload writes it.
_ENCODE = (
    "while chunk=$(dd bs=65536 count=1 2>/dev/null | od -An -v -tx1 | tr -d ' \\n') "
    '&& [ -n "$chunk" ]; do printf "%s\\n" "$chunk"; done'
)
# Line one is the group's pid, then the marker line if one was given, then the
# workload replaces this shell.
_INNER = 'echo "$$"; [ -n "$1" ] && printf "%s\\n" "$1"; shift; exec "$@"'
# Every process started for an operation inherits this variable, set to the
# operation's token, so a descendant that leaves the group (`setsid`) or loses
# its parent is still found by its own /proc environ.
OWNER_VARIABLE = "AGENTIC_E2B_OPERATION"
# Members of operation $2 (group $1): every non-zombie in the group and its
# descendants (by parent pid, to a fixed point), every process whose environ
# carries `$OWNER_VARIABLE=$2`, and every process already seen as a member in
# this command, by pid and start time. So a descendant that cleared its
# environment, left the group and was orphaned by the first signal is still
# found, and a reused pid is not. Parent links are followed from the group
# only: the wrapper outside it carries the token too, and its other child is
# the encoder, which must outlive the workload. TERM each member, wait up to
# $3 tenths of a second for all to go, then KILL and wait up to 2s. Exits 0
# only once none is left, 1 listing the survivors, 2 for a group it refuses to
# signal. On exit 0 it writes $5 (`cancelled` or `timed_out`) to `stopped` in
# the reattach directory $4, so a fresh instance reads why the group is gone.
# /proc, `cat` and `grep` only: slim images ship no `ps`. `members`
# sets `$m` rather than printing it, so `$known` outlives each scan.
_TERMINATE = (
    "group=$1; owner=$2; grace=$3; state=$4; word=$5; "
    '[ "$group" -gt 1 ] 2>/dev/null || exit 2; known=" "; '
    'stopped() { echo "$word" > "$state/stopped" 2>/dev/null; exit 0; }; '
    "members() { procs=; for s in /proc/[0-9]*/stat; do "
    'st=$(cat "$s" 2>/dev/null) || continue; p=${s#/proc/}; set -- ${st##*) }; '
    '[ "$1" != Z ] && procs="$procs ${p%/stat},$2,$3,${20}"; done; '
    'd=" "; grow=1; while [ $grow = 1 ]; do grow=0; for r in $procs; do '
    'IFS=,; set -- $r; unset IFS; case "$d" in *" $1 "*) continue;; esac; '
    'case "$d|$known" in *" $2 "*\\|*|*" $1:$4 "*) ;; *) [ "$3" = "$group" ] || continue;; esac; '
    'd="$d$1 "; known="$known$1:$4 "; grow=1; done; done; m=$d; '
    'for e in $(grep -lzxF "$owner" /proc/[0-9]*/environ 2>/dev/null); do '
    'p=${e#/proc/}; p=${p%/environ}; case "$m" in *" $p "*) ;; *) m="$m$p ";; esac; done; }; '
    'signal() { members; for p in $m; do kill -s "$1" "$p" 2>/dev/null; done; '
    'kill -s "$1" -- "-$group" 2>/dev/null; }; '
    'settle() { n=$1; while members; [ -n "${m# }" ]; do [ "$n" -le 0 ] && return 1; '
    "sleep 0.1; n=$((n-1)); done; return 0; }; "
    'signal TERM; settle "$grace" && stopped; '
    "signal KILL; settle 20 && stopped; members; echo survivors $m; exit 1"
)
# Where each operation's reattach directory lives, by operation id.
STATE_ROOT = "/tmp/agentic-operations"
# Prints the reattach directory $1 as `name=value` lines, then `alive=1` when
# some process carries `$OWNER_VARIABLE=$2`, which the wrapper does until the
# workload's status is written. Exits 3 when there is no such directory.
_PROBE = (
    'd=$1; [ -r "$d/owner" ] || exit 3; '
    "for f in owner execution started group exit ended stopped; do "
    'v=$(head -n 1 "$d/$f" 2>/dev/null); printf "%s=%s\n" "$f" "$v"; done; '
    'if grep -qzxF "$2" /proc/[0-9]*/environ 2>/dev/null; '
    "then echo alive=1; else echo alive=0; fi"
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
class _Reattached:
    """An operation another instance started, known only from its sandbox."""

    ref: OperationRef
    token: str
    sandbox_id: str
    execution_id: str
    started_at: str
    group: int | None = None
    cancel_requested: bool = False
    cancel_confirmed: bool = False
    ended_at: str | None = None
    stopping: asyncio.Lock = field(default_factory=asyncio.Lock)
    signal_failure: str | None = None
    escalated: bool = False
    stop_failed: bool = False

    def note(self, failure: str) -> None:
        self.signal_failure = (
            failure if self.signal_failure is None else f"{self.signal_failure}; {failure}"
        )


@dataclass(frozen=True)
class _Probe:
    """One read of a reattach directory and of /proc."""

    owner: str
    execution_id: str
    started_at: str
    group: int | None
    exit_code: int | None
    ended_at: str | None
    stopped: str
    alive: bool


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _state_dir(ref: OperationRef) -> str:
    return posixpath.join(STATE_ROOT, ref.operation_id)


@dataclass
class _Operation:
    ref: OperationRef
    spec: OperationSpec
    sandbox_id: str
    spool: OperationSpool
    started_at: str
    # `OWNER_VARIABLE`'s value for this operation, unguessable by other workloads.
    token: str = field(default_factory=lambda: secrets_module.token_hex(16))
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
    # The group would not die, so the sandbox was killed; `stop_failed` if that failed too.
    escalated: bool = False
    stop_failed: bool = False
    collector: asyncio.Task[None] | None = None

    def note(self, failure: str) -> None:
        self.signal_failure = (
            failure if self.signal_failure is None else f"{self.signal_failure}; {failure}"
        )


def _stop_failure_metadata(operation: _Operation | _Reattached) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if operation.escalated:
        metadata["cancel_escalation"] = (
            "sandbox_kill_failed" if operation.stop_failed else "sandbox_killed"
        )
    if operation.signal_failure is not None:
        metadata["cancel_signal_failure"] = operation.signal_failure
    return metadata


def _decode(line: str) -> bytes:
    """One `_ENCODE` line as the bytes the workload wrote.

    A line that is not hex was printed by the wrapper's shell before the
    encoder ran (a failed `setsid`, say), and is kept as printed.
    """
    try:
        return bytes.fromhex(line)
    except ValueError:
        return f"{line}\n".encode()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error(
    kind: Literal[
        "unknown_operation", "conflict", "provider_unavailable", "unsupported_capability"
    ],
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
        self._reattached: dict[str, _Reattached] = {}
        self._by_key: dict[tuple[str, str], _Replay] = {}
        self._launching: dict[tuple[str, str], asyncio.Lock] = {}

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
                integrity="exact",
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
        ref = OperationRef(
            workspace=WorkspaceRef(provider=_PROVIDER, resource_id=workspace.id),
            # Random, not a counter: a fresh instance must not mint a live id again.
            operation_id=f"{workspace.id}-op-{secrets_module.token_hex(8)}",
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
            shlex.join(
                [
                    "sh",
                    "-c",
                    _OUTER,
                    "aw-operation",
                    _INNER,
                    _ENCODE,
                    _state_dir(ref),
                    *spec.argv,
                ]
            ),
            cwd=posixpath.normpath(posixpath.join("/workspace", spec.working_directory)),
            env={
                **workspace.config.checked_environment(),
                **spec.environment,
                **secrets,
                OWNER_VARIABLE: operation.token,
                # Read and unset by the wrapper before the workload starts.
                "OWNER_DIGEST": _digest(operation.token),
                "EXECUTION_HEX": spec.execution_id.encode().hex(),
                "STARTED_AT": operation.started_at,
            },
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
        for reattached in list(self._reattached.values()):
            if reattached.sandbox_id == workspace_id:
                del self._reattached[reattached.ref.operation_id]

    def reattach_token(self, ref: OperationRef) -> str:
        """The secret a fresh instance needs, with `ref`, to `reattach`.

        Only the instance that started the operation (or one that reattached
        it) has it; persist it beside the ref and treat it as a credential.
        """
        operation = self._operations.get(ref.operation_id) or self._reattached.get(ref.operation_id)
        if operation is None or operation.ref != ref:
            raise _error(
                "unknown_operation", f"no operation {ref.operation_id} in this provider", ref
            )
        return operation.token

    async def reattach(self, ref: OperationRef, token: str) -> None:
        """Make `status` and `cancel` answer for `ref`, started by another instance.

        Ownership is checked in the sandbox against the digest the launch
        wrote, so a ref with a forged or another operation's token raises
        `unknown_operation`; a sandbox that does not answer raises
        `provider_unavailable`.
        """
        known = self._operations.get(ref.operation_id) or self._reattached.get(ref.operation_id)
        if known is not None:
            if known.ref != ref or not secrets_module.compare_digest(known.token, token):
                raise _error(
                    "unknown_operation", f"{ref.operation_id} is not owned by this token", ref
                )
            return
        probe = await self._probe(ref, token)
        self._reattached[ref.operation_id] = _Reattached(
            ref=ref,
            token=token,
            sandbox_id=ref.workspace.resource_id,
            execution_id=probe.execution_id,
            started_at=probe.started_at,
            group=probe.group,
        )

    async def _probe(self, ref: OperationRef, token: str) -> _Probe:
        """Read the reattach directory, checking that `token` owns it."""
        command = await self._client.start(
            ref.workspace.resource_id,
            shlex.join(
                ["sh", "-c", _PROBE, "aw-probe", _state_dir(ref), f"{OWNER_VARIABLE}={token}"]
            ),
            cwd="/",
            env={},
            timeout=None,
            user=self._run_as,
        )
        ended, output = await _finish(command, _CONTROL_SECONDS)
        if ended.end == "exited" and ended.exit_code == 3:
            raise _error(
                "unknown_operation",
                f"no reattach state for {ref.operation_id} in sandbox {ref.workspace.resource_id}",
                ref,
            )
        if ended.end != "exited" or ended.exit_code != 0:
            raise _error(
                "provider_unavailable",
                f"reading {ref.operation_id} in sandbox {ref.workspace.resource_id} did not "
                f"answer ({ended.end} {ended.exit_code} {ended.detail or ''}{output.strip()}); "
                "its state is unknown",
                ref,
            )
        fields = dict(line.partition("=")[::2] for line in output.splitlines() if "=" in line)
        if not secrets_module.compare_digest(fields.get("owner", ""), _digest(token)):
            raise _error("unknown_operation", f"{ref.operation_id} is not owned by this token", ref)
        try:
            execution_id = bytes.fromhex(fields.get("execution", "")).decode()
        except ValueError:
            execution_id = ""
        group, code = fields.get("group", ""), fields.get("exit", "")
        return _Probe(
            owner=fields["owner"],
            execution_id=execution_id or ref.operation_id,
            started_at=fields.get("started", ""),
            group=int(group) if group.isdigit() else None,
            exit_code=int(code) if code.isdigit() else None,
            ended_at=fields.get("ended") or None,
            stopped=fields.get("stopped", ""),
            alive=fields.get("alive") == "1",
        )

    async def _reattached_status(self, operation: _Reattached) -> OperationStatus:
        metadata = _stop_failure_metadata(operation)
        common: dict[str, object] = {
            "schema": "apss.workspace-status/v1",
            "ref": operation.ref,
            "execution_id": operation.execution_id,
            "metadata": metadata,
        }
        if operation.started_at:
            common["started_at"] = operation.started_at
        if operation.cancel_confirmed:
            # Confirmed here; after an escalation the sandbox is gone, so not probed.
            return OperationStatus.model_validate(
                {**common, "state": "cancelled", "ended_at": operation.ended_at or _now()}
            )
        probe = await self._probe(operation.ref, operation.token)
        operation.group = probe.group if probe.group is not None else operation.group
        exit_code: int | None = None
        signal_name: str | None = None
        state: OperationState
        if probe.stopped in ("cancelled", "timed_out"):
            state = "cancelled" if probe.stopped == "cancelled" else "timed_out"
        elif probe.exit_code is not None:
            exit_code = probe.exit_code
            if exit_code > 128 and exit_code - 128 < signal.NSIG:
                signal_name = signal.Signals(exit_code - 128).name
            state = "succeeded" if exit_code == 0 else "failed"
        elif probe.alive:
            return OperationStatus.model_validate({**common, "state": "running"})
        else:
            # Gone with no status: never `cancelled` unless a termination said so.
            state = "failed" if operation.cancel_requested else "lost"
        return OperationStatus.model_validate(
            {
                **common,
                "state": state,
                "exit_code": exit_code,
                "signal": signal_name,
                "ended_at": probe.ended_at or _now(),
            }
        )

    async def _cancel_reattached(
        self, operation: _Reattached, *, grace_seconds: float
    ) -> OperationStatus:
        status = await self._reattached_status(operation)
        if status.state in TERMINAL_STATES:
            return status
        deadline = asyncio.get_running_loop().time() + _LEADER_SECONDS
        # The group is written just after the launch; wait for it rather than guess.
        while operation.group is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
            status = await self._reattached_status(operation)
            if status.state in TERMINAL_STATES:
                return status
        async with operation.stopping:
            operation.cancel_requested = True
            try:
                await self._stop(operation, grace_seconds=grace_seconds, reason="cancelled")
            except OperationFailure as e:
                operation.note(e.error.message)
                raise
            except asyncio.CancelledError:
                operation.note("cancel interrupted before termination was confirmed")
                raise
            operation.cancel_confirmed = True
            operation.ended_at = _now()
        return await self._reattached_status(operation)

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
                        await self._stop(operation, grace_seconds=1.0, reason="timed_out")
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
        text = ""
        pending = b""
        try:
            async for chunk in command.output():
                text += chunk
                *lines, text = text.split("\n")
                for line in lines:
                    data = _decode(line)
                    if operation.leader_known.is_set():
                        self._record(operation, data)
                        continue
                    pending += data
                    header, newline, rest = pending.partition(b"\n")
                    if not newline:
                        continue
                    if header.isdigit():
                        operation.group = int(header)
                    else:
                        # Not the wrapper's line (it failed before exec); keep it.
                        rest = pending
                    operation.leader_known.set()
                    pending = b""
                    if rest:
                        self._record(operation, rest)
            # Only a wrapper that failed before encoding ends without a newline.
            tail = pending + text.encode()
            if tail:
                self._record(operation, tail)
        finally:
            operation.leader_known.set()

    @staticmethod
    def _record(operation: _Operation, data: bytes) -> None:
        operation.spool.append(data)
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
        if isinstance(operation, _Reattached):
            return await self._reattached_status(operation)
        metadata = {**operation.spec.metadata, **_stop_failure_metadata(operation)}
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
        if isinstance(operation, _Reattached):
            raise _error(
                "unsupported_capability",
                f"{ref.operation_id} was reattached; its output was observed by the "
                "instance that started it and is not recoverable",
                ref,
            )
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
        if isinstance(operation, _Reattached):
            return await self._cancel_reattached(operation, grace_seconds=grace_seconds)
        if operation.state not in TERMINAL_STATES:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_LEADER_SECONDS):
                    await operation.leader_known.wait()
            async with operation.stopping:
                if operation.state in TERMINAL_STATES:
                    return await self.status(ref)
                operation.cancel_requested = True
                try:
                    await self._stop(operation, grace_seconds=grace_seconds, reason="cancelled")
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

    async def _stop(
        self,
        operation: _Operation | _Reattached,
        *,
        grace_seconds: float,
        reason: Literal["cancelled", "timed_out"],
    ) -> None:
        """Terminate the operation in the sandbox; failing that, kill the sandbox.

        As Docker stops the container when its group will not die, the
        escalation ends every process of the workspace. A kill that fails, or
        a sandbox still found after it, raises provider_unavailable: the
        operation may still be running.
        """
        try:
            await self._terminate(operation, grace_seconds=grace_seconds, reason=reason)
            return
        except OperationFailure as e:
            operation.note(e.error.message)
        operation.escalated = True
        try:
            await self._client.kill(operation.sandbox_id)
            deadline = asyncio.get_running_loop().time() + _CONTROL_SECONDS
            while await self._client.lookup(operation.sandbox_id) is not None:
                if asyncio.get_running_loop().time() >= deadline:
                    raise _error(
                        "provider_unavailable",
                        f"sandbox {operation.sandbox_id} was killed but is still found "
                        f"{_CONTROL_SECONDS:g}s later; its termination is unconfirmed",
                        operation.ref,
                    )
                await asyncio.sleep(0.1)
        except OperationFailure:
            operation.stop_failed = True
            raise
        except Exception as e:
            operation.stop_failed = True
            raise _error(
                "provider_unavailable",
                f"killing sandbox {operation.sandbox_id} failed: {e!r}",
                operation.ref,
            ) from e

    async def _terminate(
        self,
        operation: _Operation | _Reattached,
        *,
        grace_seconds: float,
        reason: Literal["cancelled", "timed_out"] = "cancelled",
    ) -> None:
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
            shlex.join(
                [
                    "sh",
                    "-c",
                    _TERMINATE,
                    "aw-terminate",
                    str(operation.group),
                    f"{OWNER_VARIABLE}={operation.token}",
                    str(tenths),
                    _state_dir(operation.ref),
                    reason,
                ]
            ),
            cwd="/",
            env={},
            timeout=None,
            user=self._run_as,
        )
        ended, output = await _finish(command, _CONTROL_SECONDS + grace_seconds)
        if ended.end != "exited" or ended.exit_code != 0:
            raise _error(
                "provider_unavailable",
                f"terminating group {operation.group} in sandbox {operation.sandbox_id} "
                f"was not confirmed ({ended.end} {ended.exit_code} {ended.detail or ''}"
                f"{output.strip()})",
                operation.ref,
            )

    def _lookup(self, ref: OperationRef) -> _Operation | _Reattached:
        operation = self._operations.get(ref.operation_id) or self._reattached.get(ref.operation_id)
        if operation is None or operation.ref != ref:
            raise _error(
                "unknown_operation", f"no operation {ref.operation_id} in this provider", ref
            )
        return operation


async def _finish(command: SandboxCommand, seconds: float) -> tuple[CommandExit, str]:
    """How a control command ended, and what it printed, within `seconds`."""
    chunks: list[str] = []

    async def drain() -> None:
        async for text in command.output():
            chunks.append(text)

    # Drained alongside `wait`, never before it: the SDK adapter ends its
    # output only once `wait` has seen the command end.
    reader = asyncio.create_task(drain())
    try:
        async with asyncio.timeout(seconds):
            ended = await command.wait()
            await reader
    except TimeoutError:
        ended = CommandExit(end="disconnected", detail="control command did not answer")
    finally:
        if not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
    return ended, "".join(chunks)
