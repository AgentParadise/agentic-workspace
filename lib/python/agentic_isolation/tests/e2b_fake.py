"""An in-memory `E2BSandboxClient` for unit tests. It proves the provider, not E2B.

It refuses to construct outside a pytest run, so it can never stand in for
the SDK adapter in a real process.

By default a started command is scripted (`FakeRun`). With `executing_root`
it really runs, as a local `sh -c` in a new session, so the operation
conformance suite can check real pids and files. Each behaviour of
`ExecutedCommand` is what the pinned SDK does (e2b==2.52.0):
`/workspace` is the sandbox's own disk, mapped here to `executing_root`;
`envs` are added to the sandbox's environment; `stdin` is written then closed
(`send_stdin`, `close_stdin`); stdout is decoded as UTF-8 with replacement by
an incremental decoder (`AsyncCommandHandle`); `kill` is SIGKILL to the one
pid and nothing else (its docstring), so descendants survive it.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

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


class ExecutedCommand:
    """A command really running as a local process; see the module docstring."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    async def output(self) -> AsyncIterator[str]:
        assert self._process.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await self._process.stdout.read(65536):
            if text := decoder.decode(chunk):
                yield text
        if tail := decoder.decode(b"", final=True):
            yield tail

    async def wait(self) -> CommandExit:
        code = await self._process.wait()
        # envd reports a signal death the way a shell does.
        return CommandExit(end="exited", exit_code=code if code >= 0 else 128 - code)

    async def kill(self) -> bool:
        if self._process.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.kill(self._process.pid, signal.SIGKILL)
        return True


@dataclass
class FakeSandbox:
    metadata: dict[str, str]
    files: dict[str, bytes] = field(default_factory=dict)
    paused: bool = False


class FakeE2BClient:
    def __init__(
        self,
        *,
        responder: Callable[[str], FakeRun] | None = None,
        executing_root: Path | None = None,
    ) -> None:
        if "PYTEST_CURRENT_TEST" not in os.environ:
            raise RuntimeError("FakeE2BClient is a test double; it refuses to run outside pytest")
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.started: list[tuple[str, str, str | None, dict[str, str]]] = []
        self.commands: dict[int, FakeCommand] = {}
        self.responder = responder or (lambda _command: FakeRun())
        self.executing_root = executing_root
        # The stdin each start was given, in call order.
        self.stdins: list[bytes | None] = []
        self.executed: list[ExecutedCommand] = []
        self.kill_is_noop = False
        self.command_kill_works = True
        self.pause_refused = False
        self.events: list[str] = []
        # The user each start, read and write ran as, in call order.
        self.users: list[str] = []
        # How many list reads still show a sandbox after its kill (E2B's lag).
        self.listed_after_kill = 0
        self._dying: dict[str, tuple[FakeSandbox, int]] = {}
        # How many list reads omit a sandbox after its create (E2B's lag);
        # `lookup` by id never lags.
        self.unlisted_after_create = 0
        self._unlisted: dict[str, int] = {}
        # List reads, counted from 1, that omit every sandbox: E2B's list is
        # not monotonic, so an id can be absent on one read and back on the next.
        self.dropped_list_reads: set[int] = set()
        self.list_reads = 0
        # How many lookups still find a sandbox after its kill.
        self.found_after_kill = 0
        self._fading: dict[str, tuple[FakeSandbox, int]] = {}

    async def create(
        self, *, template: str, metadata: Mapping[str, str], lifetime_seconds: int
    ) -> str:
        sandbox_id = f"sbx-{len(self.sandboxes) + 1}"
        self.sandboxes[sandbox_id] = FakeSandbox(metadata=dict(metadata))
        if self.unlisted_after_create:
            self._unlisted[sandbox_id] = self.unlisted_after_create
        self.events.append(f"create {sandbox_id} {template}")
        return sandbox_id

    async def list(self, metadata: Mapping[str, str]) -> list[SandboxListing]:
        self.list_reads += 1
        visible = dict(self.sandboxes)
        for sandbox_id, (box, reads) in list(self._dying.items()):
            visible[sandbox_id] = box
            if reads <= 1:
                del self._dying[sandbox_id]
            else:
                self._dying[sandbox_id] = (box, reads - 1)
        for sandbox_id, reads in list(self._unlisted.items()):
            visible.pop(sandbox_id, None)
            if reads <= 1:
                del self._unlisted[sandbox_id]
            else:
                self._unlisted[sandbox_id] = reads - 1
        if self.list_reads in self.dropped_list_reads:
            return []
        return [
            SandboxListing(sandbox_id, dict(box.metadata))
            for sandbox_id, box in visible.items()
            if all(box.metadata.get(k) == v for k, v in metadata.items())
        ]

    async def lookup(self, sandbox_id: str) -> SandboxListing | None:
        box = self.sandboxes.get(sandbox_id)
        if box is None and sandbox_id in self._fading:
            box, reads = self._fading[sandbox_id]
            if reads <= 1:
                del self._fading[sandbox_id]
            else:
                self._fading[sandbox_id] = (box, reads - 1)
        return None if box is None else SandboxListing(sandbox_id, dict(box.metadata))

    async def kill(self, sandbox_id: str) -> bool:
        self.events.append(f"kill {sandbox_id}")
        if self.kill_is_noop:
            return True
        box = self.sandboxes.pop(sandbox_id, None)
        if box is not None and self.listed_after_kill:
            self._dying[sandbox_id] = (box, self.listed_after_kill)
        if box is not None and self.found_after_kill:
            self._fading[sandbox_id] = (box, self.found_after_kill)
        return box is not None

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
        user: str,
        stdin: bytes | None = None,
    ) -> FakeCommand | ExecutedCommand:
        self.started.append((sandbox_id, command, cwd, dict(env)))
        self.users.append(user)
        self.stdins.append(stdin)
        if self.executing_root is not None:
            return await self._execute(command, cwd=cwd, env=env, stdin=stdin)
        pid = len(self.commands) + 1
        made = FakeCommand(pid, self.responder(command), kill_works=self.command_kill_works)
        self.commands[pid] = made
        return made

    async def _execute(
        self, command: str, *, cwd: str | None, env: Mapping[str, str], stdin: bytes | None
    ) -> ExecutedCommand:
        assert self.executing_root is not None
        local = Path(cwd or "/workspace")
        if local.is_relative_to("/workspace"):
            local = self.executing_root / local.relative_to("/workspace")
        process = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            command,
            cwd=local,
            env={**os.environ, **env},
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        if process.stdin is not None:
            process.stdin.write(stdin or b"")
            await process.stdin.drain()
            process.stdin.close()
        made = ExecutedCommand(process)
        self.executed.append(made)
        return made

    async def reconnect(self, sandbox_id: str, pid: int) -> FakeCommand:
        self.events.append(f"reconnect {sandbox_id} {pid}")
        return FakeCommand(pid, FakeRun(chunks=["again\n"]), kill_works=True)

    async def read_bytes(self, sandbox_id: str, path: str, *, user: str) -> bytes:
        self.users.append(user)
        try:
            return self.sandboxes[sandbox_id].files[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    async def write_bytes(self, sandbox_id: str, path: str, content: bytes, *, user: str) -> None:
        self.users.append(user)
        self.sandboxes[sandbox_id].files[path] = content
