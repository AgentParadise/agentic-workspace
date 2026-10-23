"""Optional provider capabilities for the remote workspace tier.

syntropic137/syntropic137#1911, step A of the build plan in
docs/research/2026-10-08-remote-executor-e2b.md (decision 4) in syntropic137.
Each is a separate protocol a caller tests for with `isinstance`, like
`SupportsWorkspaceLogs`, so no existing provider has to grow methods it
cannot honour. Docker implements all three first; E2B (#1915) joins later.

The streaming entry point is `start_stream`, not decision 4's `start`:
`SupportsOperations.start` (APSS EXP-V1-0006) already holds that name on the
Docker provider, and one class cannot carry two methods with one name.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from agentic_isolation.contract import OperationRef
from agentic_isolation.providers.base import Workspace
from agentic_isolation.providers.file_transfer import (
    DEFAULT_READ_LIMITS,
    ReadFilesResult,
    ReadLimits,
)

# Absolute roots `read_files` accepts. Returned paths are relative to the root.
TransferRoot = Literal["/workspace", "/spool", "/opt/agentic"]
TRANSFER_ROOTS: tuple[TransferRoot, ...] = ("/workspace", "/spool", "/opt/agentic")

StreamEnd = Literal["completed", "timed_out", "disconnected"]


@dataclass(frozen=True)
class StreamOutcome:
    """How a streamed command ended, as far as the provider can tell.

    `end` is one of three exclusive cases:

    - `completed`: the sandbox process ended and the provider saw it end.
      `exit_code` is its status, or None when `signal` killed it.
    - `timed_out`: the timeout expired and the provider stopped the sandbox
      process. `exit_code` is None.
    - `disconnected`: the stream to the process was lost. The process may
      still be running; nothing about its exit is known, so `exit_code` and
      `signal` are None. `reconnect()` is the way back, where supported.
      This includes a transport that failed before the process started:
      a client error is never reported as the sandbox's exit status.
      `detail` carries the transport's own diagnostic, when it gave one.
    """

    end: StreamEnd
    exit_code: int | None = None
    signal: int | None = None
    detail: str | None = None

    @property
    def completed(self) -> bool:
        return self.end == "completed"

    @property
    def timed_out(self) -> bool:
        return self.end == "timed_out"

    @property
    def disconnected(self) -> bool:
        return self.end == "disconnected"


class StreamReconnectUnsupportedError(RuntimeError):
    """The provider cannot reattach to a running sandbox process.

    Raised rather than returning a session that pretends to reattach: a
    session with no output and an invented exit status is a fabricated answer.
    """


@runtime_checkable
class StreamSession(Protocol):
    """One command running in a workspace, streamed as it runs."""

    def lines(self) -> AsyncIterator[str]:
        """Stdout and stderr MERGED in the order the process wrote them.

        One line per item, without the trailing newline. Iterate once.
        Lines have no length limit, and a final line without a newline is
        still delivered.
        """
        ...

    async def outcome(self) -> StreamOutcome:
        """Wait for the command to end and say how it ended."""
        ...

    async def cancel(self) -> None:
        """Stop the SANDBOX process, not only the local end of the pipe.

        Works after `disconnected` too: losing the stream does not end the
        process, so the stop is still attempted and confirmed.
        """
        ...

    async def reconnect(self) -> StreamSession:
        """Reattach to the same sandbox process after `disconnected`.

        Raises StreamReconnectUnsupportedError where the provider cannot.
        """
        ...


@runtime_checkable
class SupportsStreamingExec(Protocol):
    """Optional capability: run a command and stream its merged output."""

    async def start_stream(
        self,
        workspace: Workspace,
        sandbox_argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> StreamSession:
        """Start `sandbox_argv` in the workspace and return its session.

        `sandbox_argv` is what runs inside the sandbox; any transport argv
        (for Docker, `docker exec ...`) is the provider's business. Per-command
        environment goes in `env`, never in the argv.
        """
        ...


@runtime_checkable
class SupportsFileTransfer(Protocol):
    """Optional capability: binary-safe, pattern-aware reads from a workspace.

    Separate from `WorkspaceProvider.read_file`, which keeps its documented
    relative-path, text contract.
    """

    async def read_files(
        self,
        workspace: Workspace,
        patterns: Sequence[str],
        *,
        base_path: TransferRoot,
        limits: ReadLimits = DEFAULT_READ_LIMITS,
    ) -> ReadFilesResult:
        """Read every regular file under `base_path` whose relative path
        matches any pattern, within `limits`.

        Patterns use `fnmatch` semantics on the POSIX relative path, so `*`
        also matches `/`. Content is the exact bytes. A matching entry that is
        a hard link, a symlink (escaping the root or not; none is followed), a
        device, FIFO or socket, or past a limit is returned as a
        `FileRefusal`, never skipped. Omitting `limits` gets the finite
        `DEFAULT_READ_LIMITS`. Raises `ReadFilesError` when the sandbox's
        walk or archive fails or breaks the contract, and its subclass
        `ReadFilesTimeoutError` when either outlasts `limits.timeout_seconds`.
        """
        ...


@dataclass(frozen=True)
class OwnedWorkspace:
    """A workspace a provider can see, and the labels it was created with."""

    workspace_id: str
    labels: Mapping[str, str]


class WorkspaceNotFoundError(LookupError):
    """No workspace with the requested id exists in this provider."""


class WorkspaceOwnershipError(PermissionError):
    """The workspace exists but its labels do not match the caller's claim."""

    def __init__(self, workspace_id: str, mismatched: Sequence[str]) -> None:
        self.workspace_id = workspace_id
        self.mismatched = tuple(mismatched)
        super().__init__(
            f"refusing to attach workspace {workspace_id}: label(s) "
            f"{', '.join(self.mismatched)} do not match the expected value"
        )


@runtime_checkable
class SupportsOwnership(Protocol):
    """Optional capability: find workspaces by label and reattach to one."""

    async def list_owned(self, labels: Mapping[str, str]) -> list[OwnedWorkspace]:
        """Every workspace carrying ALL of `labels` with the given values."""
        ...

    async def attach(self, workspace_id: str, *, expect_labels: Mapping[str, str]) -> Workspace:
        """Return a usable handle to an existing workspace.

        Raises WorkspaceNotFoundError when it does not exist, and
        WorkspaceOwnershipError when any of `expect_labels` is missing or has
        a different value.
        """
        ...


def mismatched_labels(actual: Mapping[str, str], expected: Mapping[str, str]) -> list[str]:
    """The keys of `expected` that `actual` lacks or holds a different value for."""
    return sorted(key for key, value in expected.items() if actual.get(key) != value)


@runtime_checkable
class SupportsOperationReattach(Protocol):
    """Optional capability: answer for an operation another instance started.

    For a provider whose operations outlive the process that started them.
    The caller persists the ref and the opaque token from `reattach_token`;
    after `reattach`, `SupportsOperations.status` and `cancel` work for that
    ref as they do for one started here. A provider whose operations die with
    their process (Docker) does not implement this.
    """

    def reattach_token(self, ref: OperationRef) -> str:
        """The secret proving ownership of `ref`, from the instance holding it.

        Raises OperationFailure `unknown_operation` for a ref it does not hold.
        """
        ...

    async def reattach(self, ref: OperationRef, token: str) -> None:
        """Take over status and cancel for `ref`, checking `token` owns it.

        Raises OperationFailure `unknown_operation` when the operation is not
        found or `token` does not own it, and `provider_unavailable` when the
        provider cannot read its state.
        """
        ...


@runtime_checkable
class SupportsWorkspaceSuspend(Protocol):
    """Optional capability: pause a workspace, keeping its disk and memory, and resume it."""

    async def pause(self, workspace: Workspace) -> None:
        """Suspend every process of `workspace`. Raises when it may still be running."""
        ...

    async def resume(self, workspace: Workspace) -> None:
        """Run a paused `workspace` again; a running one is left as it is."""
        ...
