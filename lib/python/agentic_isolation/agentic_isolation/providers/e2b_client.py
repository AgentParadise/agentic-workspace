"""The narrow port `E2BWorkspaceProvider` drives, and its adapter over the e2b SDK.

syntropic137/syntropic137#1915. The provider never imports `e2b`: it speaks
`E2BSandboxClient`, which names only the sandbox operations it needs. The
real implementation, `SdkE2BClient`, is the one place the SDK is touched, so a
change of SDK version or call shape stays here. Unit tests inject a fake; the
behaviour below is what `SdkE2BClient` maps from the SDK, and every SDK
behaviour it relies on is cited (accessed 2026-10-17):

- Metadata on create and list filtering by it:
  https://e2b.dev/docs/sandbox/metadata and https://e2b.dev/docs/sandbox/list
- `kill` returns False when the sandbox was not found, and `pause`/`connect`
  resume: https://e2b.dev/docs/sandbox/persistence (a pause can be refused
  with HTTP 503, `ServiceBusyException`, while the sandbox keeps running)
- Background commands, `pid`, and reattaching with `commands.connect(pid)`:
  https://e2b.dev/docs/commands/background
- File read as bytes and write: https://e2b.dev/docs/filesystem/read-write

None of this has run against a real E2B account; see
tests/integration/test_e2b_live.py for the gated proof.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
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
        """Kill the process in the sandbox. False when it was already gone."""
        ...


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
    ) -> SandboxCommand:
        """Start a shell command in the sandbox without waiting for it."""
        ...

    async def reconnect(self, sandbox_id: str, pid: int) -> SandboxCommand:
        """Reattach to a running command by pid."""
        ...

    async def read_bytes(self, sandbox_id: str, path: str) -> bytes:
        """The exact bytes of a file. Raises FileNotFoundError when absent."""
        ...

    async def write_bytes(self, sandbox_id: str, path: str, content: bytes) -> None:
        """Write a file, creating parent directories."""
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

    async def kill(self, sandbox_id: str) -> bool:
        from e2b import AsyncSandbox

        self._sandboxes.pop(sandbox_id, None)
        return bool(await AsyncSandbox.kill(sandbox_id, api_key=self._api_key))

    async def pause(self, sandbox_id: str) -> None:
        import asyncio
        import random

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
    ) -> SandboxCommand:
        from e2b import SandboxException

        sandbox = await self._sandbox(sandbox_id)
        chunks = _ChunkQueue()
        try:
            handle = await sandbox.commands.run(
                command,
                background=True,
                envs=dict(env),
                cwd=cwd,
                on_stdout=chunks.put,
                # 0 means the SDK does not bound the command; ours is enforced
                # by `timeout` in the sandbox (see E2BWorkspaceProvider).
                timeout=0,
            )
        except SandboxException as exc:
            raise E2BTransportError(f"cannot start a command in {sandbox_id}: {exc}") from exc
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

    async def read_bytes(self, sandbox_id: str, path: str) -> bytes:
        from e2b import FileNotFoundException

        sandbox = await self._sandbox(sandbox_id)
        try:
            return bytes(await sandbox.files.read(path, format="bytes"))
        except FileNotFoundException as exc:
            raise FileNotFoundError(path) from exc

    async def write_bytes(self, sandbox_id: str, path: str, content: bytes) -> None:
        sandbox = await self._sandbox(sandbox_id)
        await sandbox.files.write(path, content)


class _ChunkQueue:
    """Stdout chunks from an SDK callback, closed with None when the command ends."""

    def __init__(self) -> None:
        import asyncio

        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def put(self, chunk: str) -> None:
        self._queue.put_nowait(chunk)

    def close(self) -> None:
        self._queue.put_nowait(None)

    async def drain(self) -> AsyncIterator[str]:
        while (chunk := await self._queue.get()) is not None:
            yield chunk


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
        from e2b import CommandExitException, SandboxException

        try:
            result = await self._handle.wait()
            return CommandExit(end="exited", exit_code=result.exit_code)
        except CommandExitException as exc:
            return CommandExit(end="exited", exit_code=exc.exit_code)
        except (SandboxException, OSError) as exc:
            return CommandExit(end="disconnected", detail=str(exc))
        finally:
            self._chunks.close()

    async def kill(self) -> bool:
        return bool(await self._handle.kill())


def shell_join(argv: Sequence[str]) -> str:
    """Quote an argv as one POSIX shell command line."""
    import shlex

    return shlex.join(argv)
