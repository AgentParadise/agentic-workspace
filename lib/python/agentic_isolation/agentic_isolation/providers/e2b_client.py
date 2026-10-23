"""The narrow port `E2BWorkspaceProvider` drives, and its adapter over the e2b SDK.

syntropic137/syntropic137#1915. The provider never imports `e2b`: it speaks
`E2BSandboxClient`, which names only the sandbox operations it needs. The
real implementation, `SdkE2BClient`, is the one place the SDK is touched, so a
change of SDK version or call shape stays here. Unit tests inject a fake; the
behaviour below is what `SdkE2BClient` maps from the SDK, and every SDK
behaviour it relies on is cited (accessed 2026-10-17):

- Metadata on create and list filtering by it:
  https://e2b.dev/docs/sandbox/metadata and https://e2b.dev/docs/sandbox/list
- `list` and `get_info` read different endpoints: the list paginates
  `GET /v2/sandboxes` and `get_info` reads `GET /sandboxes/{id}`, raising
  `SandboxNotFoundException` on a 404 (`e2b/sandbox_async/sandbox_api.py` and
  `paginator.py` in e2b==2.52.0). The client filters nothing out of either, so
  a sandbox the list does not show yet, or still shows after its kill, is the
  list endpoint's lag. `get_info` (`lookup` here) is the per-id existence
  check the provider confirms a removal with: it sends a request and maps 404
  to not-found. `kill`'s bool is not one: `_cls_kill` returns True without a
  request when `config.debug` is set, and True on any 2xx; `connect` resumes a
  paused sandbox, so reading with it has a side effect.
- `kill` returns False when the sandbox was not found, and `pause`/`connect`
  resume: https://e2b.dev/docs/sandbox/persistence (a pause can be refused
  with HTTP 503, `ServiceBusyException`, while the sandbox keeps running)
- Background commands, `pid`, and reattaching with `commands.connect(pid)`:
  https://e2b.dev/docs/commands/background
- `commands.run(stdin=True)`, `send_stdin(pid, data)` and `close_stdin(pid)`
  (EOF; needs a recent envd), and stdout decoded as UTF-8 with replacement by
  an incremental decoder: `e2b/sandbox_async/commands/command.py` and
  `command_handle.py` in e2b==2.52.0
- File read as bytes and write: https://e2b.dev/docs/filesystem/read-write
- Commands and file operations run as E2B's `user` unless `user=` names
  another; any user in the sandbox's /etc/passwd may be named
  (`e2b/connection_config.py` in e2b==2.52.0: `Username = str`,
  `default_username = "user"`)
- `AsyncCommandHandle.kill` sends SIGKILL and `disconnect` stops receiving
  events without killing: the docstrings of the pinned SDK,
  `e2b/sandbox_async/commands/command_handle.py` in e2b==2.52.0. Its `wait`
  raises the stream's own error (for example `connectrpc.errors.ConnectError`),
  a plain `Exception` when the stream ends with no end event, and
  `CancelledError` after `disconnect`; all three are a lost stream here.

None of this has run against a real E2B account; see
tests/integration/test_e2b_live.py for the gated proof.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import signal
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from e2b import AsyncSandbox
    from e2b.sandbox_async.commands.command_handle import AsyncCommandHandle

# A command timeout is the provider's to enforce, so it is not an end here.
CommandEnd = Literal["exited", "disconnected"]


@dataclass(frozen=True)
class CommandExit:
    """How a sandbox command ended, as the transport reported it."""

    end: CommandEnd
    exit_code: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SandboxListing:
    """One sandbox the account can see, with the metadata it was created with."""

    sandbox_id: str
    metadata: Mapping[str, str]


class E2BTransportError(ConnectionError):
    """The E2B API or the sandbox's command channel could not be reached."""


class SandboxCommand(Protocol):
    """One command started in a sandbox."""

    @property
    def pid(self) -> int: ...

    def output(self) -> AsyncIterator[str]:
        """Stdout text chunks as they arrive. Iterate once."""
        ...

    async def wait(self) -> CommandExit:
        """Wait for the end of the command. Never raises for a non-zero exit."""
        ...

    async def kill(self) -> bool:
        """Kill the process in the sandbox with SIGKILL. False when it was already gone.

        Raises E2BTransportError when the kill could not be delivered.
        """
        ...


# The signal `SandboxCommand.kill` sends (the SDK's kill docstring).
KILL_SIGNAL = int(signal.SIGKILL)


class E2BSandboxClient(Protocol):
    """The sandbox operations `E2BWorkspaceProvider` needs, and nothing else."""

    async def create(
        self, *, template: str, metadata: Mapping[str, str], lifetime_seconds: int
    ) -> str:
        """Create a sandbox and return its id."""
        ...

    async def list(self, metadata: Mapping[str, str]) -> list[SandboxListing]:
        """Running and paused sandboxes carrying ALL of `metadata`."""
        ...

    async def lookup(self, sandbox_id: str) -> SandboxListing | None:
        """One running or paused sandbox by id, None when E2B does not know it.

        Unlike `list`, this does not lag a create.
        """
        ...

    async def kill(self, sandbox_id: str) -> bool:
        """Kill a sandbox. False when it was not found."""
        ...

    async def pause(self, sandbox_id: str) -> None:
        """Pause a sandbox, keeping its disk and memory.

        Raises E2BTransportError when the pause is still refused after the
        adapter's retries; the sandbox is then still running.
        """
        ...

    async def start(
        self,
        sandbox_id: str,
        command: str,
        *,
        cwd: str | None,
        env: Mapping[str, str],
        timeout: float | None,
        user: str,
        stdin: bytes | None = None,
    ) -> SandboxCommand:
        """Start a shell command in the sandbox, as `user`, without waiting for it.

        `stdin`, when given, is written to the command's stdin, which is then
        closed; otherwise the command gets no stdin stream.

        A transport that fails before the command starts is not raised: the
        returned command has no output and ends `disconnected`, so a client
        error is never read as the sandbox's exit status.
        """
        ...

    async def reconnect(self, sandbox_id: str, pid: int) -> SandboxCommand:
        """Reattach to a running command by pid."""
        ...

    async def read_bytes(self, sandbox_id: str, path: str, *, user: str) -> bytes:
        """The exact bytes of a file, read as `user`. Raises FileNotFoundError when absent."""
        ...

    async def write_bytes(self, sandbox_id: str, path: str, content: bytes, *, user: str) -> None:
        """Write a file as `user`, creating parent directories."""
        ...


_PAUSE_ATTEMPTS = 5


class SdkE2BClient:
    """`E2BSandboxClient` over the e2b SDK (pinned in the `e2b` extra).

    The API key is read by the SDK from `E2B_API_KEY` unless `api_key` is given.
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        try:
            import e2b  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ImportError(
                "E2BWorkspaceProvider needs the e2b SDK: install agentic-isolation[e2b]"
            ) from exc
        self._api_key = api_key
        self._sandboxes: dict[str, AsyncSandbox] = {}

    async def _sandbox(self, sandbox_id: str) -> AsyncSandbox:
        from e2b import AsyncSandbox, SandboxException

        cached = self._sandboxes.get(sandbox_id)
        if cached is not None:
            return cached
        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self._api_key)
        except SandboxException as exc:
            raise E2BTransportError(f"cannot connect to sandbox {sandbox_id}: {exc}") from exc
        self._sandboxes[sandbox_id] = sandbox
        return sandbox

    async def create(
        self, *, template: str, metadata: Mapping[str, str], lifetime_seconds: int
    ) -> str:
        from e2b import AsyncSandbox

        sandbox = await AsyncSandbox.create(
            template=template,
            metadata=dict(metadata),
            timeout=lifetime_seconds,
            api_key=self._api_key,
        )
        self._sandboxes[sandbox.sandbox_id] = sandbox
        return sandbox.sandbox_id

    async def list(self, metadata: Mapping[str, str]) -> list[SandboxListing]:
        from e2b import AsyncSandbox, SandboxQuery

        paginator = AsyncSandbox.list(
            query=SandboxQuery(metadata=dict(metadata)), api_key=self._api_key
        )
        found: list[SandboxListing] = []
        while paginator.has_next:
            for info in await paginator.next_items():
                found.append(SandboxListing(info.sandbox_id, dict(info.metadata)))
        return found

    async def lookup(self, sandbox_id: str) -> SandboxListing | None:
        from e2b import AsyncSandbox, SandboxNotFoundException

        try:
            info = await AsyncSandbox.get_info(sandbox_id, api_key=self._api_key)
        except SandboxNotFoundException:
            return None
        return SandboxListing(info.sandbox_id, dict(info.metadata))

    async def kill(self, sandbox_id: str) -> bool:
        from e2b import AsyncSandbox

        self._sandboxes.pop(sandbox_id, None)
        return bool(await AsyncSandbox.kill(sandbox_id, api_key=self._api_key))

    async def pause(self, sandbox_id: str) -> None:
        from e2b import ServiceBusyException

        sandbox = await self._sandbox(sandbox_id)
        # A 503 means a previous snapshot is still finishing and the sandbox
        # keeps running (persistence docs). Pausing twice is harmless, so the
        # pause is retried a bounded number of times with jittered backoff.
        for attempt in range(_PAUSE_ATTEMPTS):
            try:
                await sandbox.pause()
            except ServiceBusyException as exc:
                if attempt == _PAUSE_ATTEMPTS - 1:
                    raise E2BTransportError(
                        f"E2B refused to pause sandbox {sandbox_id} {_PAUSE_ATTEMPTS} "
                        "times; it is still running"
                    ) from exc
                await asyncio.sleep(2**attempt + random.uniform(0, 1))
            else:
                self._sandboxes.pop(sandbox_id, None)
                return

    async def start(
        self,
        sandbox_id: str,
        command: str,
        *,
        cwd: str | None,
        env: Mapping[str, str],
        timeout: float | None,
        user: str,
        stdin: bytes | None = None,
    ) -> SandboxCommand:
        chunks = _ChunkQueue()
        try:
            sandbox = await self._sandbox(sandbox_id)
            handle = await sandbox.commands.run(
                command,
                background=True,
                envs=dict(env),
                cwd=cwd,
                user=user,
                on_stdout=chunks.put,
                stdin=stdin is not None,
                # 0 means the SDK does not bound the command; ours is enforced
                # by `timeout` in the sandbox (see E2BWorkspaceProvider).
                timeout=0,
            )
        except Exception as exc:
            # Any failure before a handle exists is the transport's, whatever
            # its type (SandboxException, ConnectError, an httpx error).
            return UnstartedCommand(f"cannot start a command in {sandbox_id}: {exc!r}")
        if stdin is not None:
            try:
                await sandbox.commands.send_stdin(handle.pid, stdin)
                await sandbox.commands.close_stdin(handle.pid)
            except Exception as exc:
                # The command would wait on stdin forever: kill it, report it lost.
                with contextlib.suppress(Exception):
                    await handle.kill()
                return UnstartedCommand(f"cannot write stdin of pid {handle.pid}: {exc!r}")
        return _SdkCommand(handle, chunks)

    async def reconnect(self, sandbox_id: str, pid: int) -> SandboxCommand:
        from e2b import SandboxException

        sandbox = await self._sandbox(sandbox_id)
        chunks = _ChunkQueue()
        try:
            handle = await sandbox.commands.connect(pid, timeout=0, on_stdout=chunks.put)
        except SandboxException as exc:
            raise E2BTransportError(f"cannot reattach to pid {pid}: {exc}") from exc
        return _SdkCommand(handle, chunks)

    async def read_bytes(self, sandbox_id: str, path: str, *, user: str) -> bytes:
        from e2b import FileNotFoundException

        sandbox = await self._sandbox(sandbox_id)
        try:
            return bytes(await sandbox.files.read(path, format="bytes", user=user))
        except FileNotFoundException as exc:
            raise FileNotFoundError(path) from exc

    async def write_bytes(self, sandbox_id: str, path: str, content: bytes, *, user: str) -> None:
        sandbox = await self._sandbox(sandbox_id)
        await sandbox.files.write(path, content, user=user)


class _ChunkQueue:
    """Stdout chunks from an SDK callback, closed with None when the command ends."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def put(self, chunk: str) -> None:
        self._queue.put_nowait(chunk)

    def close(self) -> None:
        self._queue.put_nowait(None)

    async def drain(self) -> AsyncIterator[str]:
        while (chunk := await self._queue.get()) is not None:
            yield chunk


class UnstartedCommand:
    """A command the transport never started: no output, no pid, a lost stream."""

    def __init__(self, detail: str) -> None:
        self._detail = detail

    @property
    def pid(self) -> int:
        raise E2BTransportError(f"the command never started: {self._detail}")

    async def output(self) -> AsyncIterator[str]:
        return
        yield

    async def wait(self) -> CommandExit:
        return CommandExit(end="disconnected", detail=self._detail)

    async def kill(self) -> bool:
        return False


class _SdkCommand:
    def __init__(self, handle: AsyncCommandHandle, chunks: _ChunkQueue) -> None:
        self._handle = handle
        self._chunks = chunks

    @property
    def pid(self) -> int:
        return int(self._handle.pid)

    def output(self) -> AsyncIterator[str]:
        return self._chunks.drain()

    async def wait(self) -> CommandExit:
        from e2b import CommandExitException

        try:
            result = await self._handle.wait()
            return CommandExit(end="exited", exit_code=result.exit_code)
        except CommandExitException as exc:
            return CommandExit(end="exited", exit_code=exc.exit_code)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # Not our cancellation: the handle's reader was stopped by
            # `disconnect`, which leaves the process running.
            return CommandExit(end="disconnected", detail="command stream disconnected")
        except Exception as exc:
            # ConnectError on a reset, a plain Exception on a stream that
            # ended with no end event: the process's exit is unknown.
            return CommandExit(end="disconnected", detail=repr(exc))
        finally:
            self._chunks.close()

    async def kill(self) -> bool:
        try:
            return bool(await self._handle.kill())
        except Exception as exc:
            # ConnectError on a reset, SandboxException from the envd mapping:
            # the kill was not acknowledged and the process may still run.
            raise E2BTransportError(f"kill of pid {self._handle.pid} failed: {exc!r}") from exc

    async def disconnect(self) -> None:
        """Stop receiving the stream without killing the process."""
        await self._handle.disconnect()
