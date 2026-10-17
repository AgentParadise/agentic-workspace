"""An in-memory `E2BSandboxClient` for unit tests. It proves the provider, not E2B.

It refuses to construct outside a pytest run, so it can never stand in for
the SDK adapter in a real process.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field

from agentic_isolation.providers.e2b_client import (
    CommandExit,
    E2BTransportError,
    SandboxListing,
)


@dataclass
class FakeRun:
    """What one started command does: print `chunks`, then exit or hang."""

    chunks: list[str] = field(default_factory=list)
    exit: CommandExit = field(default_factory=lambda: CommandExit(end="exited", exit_code=0))
    hang: bool = False


class FakeCommand:
    def __init__(self, pid: int, run: FakeRun, *, kill_works: bool) -> None:
        self._pid = pid
        self._run = run
        self._kill_works = kill_works
        self._finished = asyncio.Event()
        self.killed = False
        if not run.hang:
            self._finished.set()

    @property
    def pid(self) -> int:
        return self._pid

    async def output(self) -> AsyncIterator[str]:
        for chunk in self._run.chunks:
            yield chunk
        await self._finished.wait()

    async def wait(self) -> CommandExit:
        await self._finished.wait()
        if self.killed:
            return CommandExit(end="exited", exit_code=137)
        return self._run.exit

    async def kill(self) -> bool:
        if not self._kill_works:
            return False
        self.killed = True
        self._finished.set()
        return True


@dataclass
class FakeSandbox:
    metadata: dict[str, str]
    files: dict[str, bytes] = field(default_factory=dict)
    paused: bool = False


class FakeE2BClient:
    def __init__(self, *, responder: Callable[[str], FakeRun] | None = None) -> None:
        if "PYTEST_CURRENT_TEST" not in os.environ:
            raise RuntimeError("FakeE2BClient is a test double; it refuses to run outside pytest")
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.started: list[tuple[str, str, str | None, dict[str, str]]] = []
        self.commands: dict[int, FakeCommand] = {}
        self.responder = responder or (lambda _command: FakeRun())
        self.kill_is_noop = False
        self.command_kill_works = True
        self.pause_refused = False
        self.events: list[str] = []

    async def create(
        self, *, template: str, metadata: Mapping[str, str], lifetime_seconds: int
    ) -> str:
        sandbox_id = f"sbx-{len(self.sandboxes) + 1}"
        self.sandboxes[sandbox_id] = FakeSandbox(metadata=dict(metadata))
        self.events.append(f"create {sandbox_id} {template}")
        return sandbox_id

    async def list(self, metadata: Mapping[str, str]) -> list[SandboxListing]:
        return [
            SandboxListing(sandbox_id, dict(box.metadata))
            for sandbox_id, box in self.sandboxes.items()
            if all(box.metadata.get(k) == v for k, v in metadata.items())
        ]

    async def kill(self, sandbox_id: str) -> bool:
        self.events.append(f"kill {sandbox_id}")
        if self.kill_is_noop:
            return True
        return self.sandboxes.pop(sandbox_id, None) is not None

    async def pause(self, sandbox_id: str) -> None:
        self.events.append(f"pause {sandbox_id}")
        if self.pause_refused:
            raise E2BTransportError(f"pause of {sandbox_id} refused")
        self.sandboxes[sandbox_id].paused = True

    async def start(
        self,
        sandbox_id: str,
        command: str,
        *,
        cwd: str | None,
        env: Mapping[str, str],
        timeout: float | None,
    ) -> FakeCommand:
        self.started.append((sandbox_id, command, cwd, dict(env)))
        pid = len(self.commands) + 1
        made = FakeCommand(pid, self.responder(command), kill_works=self.command_kill_works)
        self.commands[pid] = made
        return made

    async def reconnect(self, sandbox_id: str, pid: int) -> FakeCommand:
        self.events.append(f"reconnect {sandbox_id} {pid}")
        return FakeCommand(pid, FakeRun(chunks=["again\n"]), kill_works=True)

    async def read_bytes(self, sandbox_id: str, path: str) -> bytes:
        try:
            return self.sandboxes[sandbox_id].files[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    async def write_bytes(self, sandbox_id: str, path: str, content: bytes) -> None:
        self.sandboxes[sandbox_id].files[path] = content
