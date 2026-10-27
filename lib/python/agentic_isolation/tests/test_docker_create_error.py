"""Container creation failures retain Docker's diagnostic channels."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_isolation.config import SecurityConfig, WorkspaceConfig, WorkspaceRuntime
from agentic_isolation.providers.docker import WorkspaceDockerProvider


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (125, b"", b"", "no output on stderr or stdout"),
        (1, b"stdout detail", b"", "stdout detail"),
        (2, b"stdout detail", b"stderr detail", "stderr detail"),
        (3, b"", b"bad byte: \xff", "bad byte:"),
    ],
)
async def test_failed_create_reports_output_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    expected: str,
) -> None:
    class FailedCreate:
        async def communicate(self) -> tuple[bytes, bytes]:
            return stdout, stderr

        @property
        def returncode(self) -> int:
            return returncode

    async def spawn(*args: object, **kwargs: object) -> FailedCreate:
        return FailedCreate()

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def plain_image(_image: str) -> tuple[str, dict[str, str]]:
        return "sha256:" + "0" * 64, {}

    monkeypatch.setattr(provider, "_inspect_image", plain_image)
    monkeypatch.setattr(provider, "_ensure_network", noop)

    async def removed(_name: str) -> bool:
        return True

    monkeypatch.setattr(provider, "_cleanup_container", removed)

    with pytest.raises(RuntimeError) as exc:
        await provider.create(WorkspaceConfig(provider="docker"))

    message = str(exc.value)
    assert expected in message
    assert f"docker create exited {returncode}" in message
    if stderr:
        assert "stdout detail" not in message


def _provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WorkspaceDockerProvider:
    # Explicit runc: auto would probe the real daemon before `docker run`.
    security = replace(SecurityConfig.production(), runtime=WorkspaceRuntime.RUNC)
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path, security=security)

    async def plain_image(_image: str) -> tuple[str, dict[str, str]]:
        return "sha256:" + "0" * 64, {}

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(provider, "_inspect_image", plain_image)
    monkeypatch.setattr(provider, "_ensure_network", noop)
    return provider


async def test_failed_rm_after_failed_create_is_noted_on_the_original_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FailedCreate:
        returncode = 125

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"boom"

    async def spawn(*args: object, **kwargs: object) -> FailedCreate:
        return FailedCreate()

    async def rm_failed(_name: str) -> bool:
        return False

    provider = _provider(monkeypatch, tmp_path)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(provider, "_cleanup_container", rm_failed)

    with pytest.raises(RuntimeError) as exc:
        await provider.create(WorkspaceConfig(provider="docker"))

    # The type callers catch is unchanged; the survivor is reported on it.
    assert type(exc.value) is RuntimeError
    notes = getattr(exc.value, "__notes__", [])
    assert any("may still exist" in note for note in notes)


class _Run:
    """A `docker run` client that exits only when `finish` is set or it is killed."""

    def __init__(self, exits: bool = True) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.returncode: int | None = None
        self.killed = False
        self._exits = exits

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        if not self._exits:
            await asyncio.Event().wait()
        await self.finish.wait()
        self.returncode = 0
        return b"cid\n", b""

    async def wait(self) -> int:
        await self.finish.wait()
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.finish.set()


def _wire(
    monkeypatch: pytest.MonkeyPatch, provider: WorkspaceDockerProvider, run: _Run, events: list[str]
) -> None:
    async def spawn(*args: object, **kwargs: object) -> _Run:
        return run

    async def remove(name: str) -> bool:
        events.append(f"rm {name}")
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(provider, "_remove", remove)


async def test_repeated_cancel_keeps_create_pending_until_the_container_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    run = _Run(exits=False)  # still running when the cancel lands
    provider = _provider(monkeypatch, tmp_path)
    _wire(monkeypatch, provider, run, events)

    async with asyncio.timeout(5):
        task = asyncio.create_task(provider.create(WorkspaceConfig(provider="docker")))
        await run.started.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "create returned before its cleanup finished"
        assert events == []
        run.finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(events) == 1 and events[0].startswith("rm agentic-ws-")
    assert list(tmp_path.iterdir()) == []


def test_cleanup_finishes_when_asyncio_run_shuts_down_mid_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    provider = _provider(monkeypatch, tmp_path)

    async def main() -> None:
        run = _Run(exits=False)
        _wire(monkeypatch, provider, run, events)
        task = asyncio.create_task(provider.create(WorkspaceConfig(provider="docker")))
        await run.started.wait()
        # Cleanup is already under way when shutdown starts, so shutdown
        # cancels it directly rather than starting it.
        task.cancel()
        await asyncio.sleep(0.01)
        # The client exits a moment into shutdown, as a real `docker run` does.
        asyncio.get_running_loop().call_later(0.05, run.finish.set)
        # Returning makes asyncio.run cancel every task, then wait for them.

    asyncio.run(main())

    assert len(events) == 1 and events[0].startswith("rm agentic-ws-")
    assert list(tmp_path.iterdir()) == []


async def test_a_docker_run_that_never_exits_is_killed_then_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    run = _Run(exits=False)
    provider = _provider(monkeypatch, tmp_path)
    monkeypatch.setattr(provider, "_ABANDON_RUN_TIMEOUT_SECONDS", 0.05)
    _wire(monkeypatch, provider, run, events)

    async with asyncio.timeout(5):
        task = asyncio.create_task(provider.create(WorkspaceConfig(provider="docker")))
        await run.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert run.killed
    assert len(events) == 1 and events[0].startswith("rm agentic-ws-")
    assert list(tmp_path.iterdir()) == []


async def test_a_stalled_rm_cannot_hang_the_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _Run(exits=False)
    provider = _provider(monkeypatch, tmp_path)
    monkeypatch.setattr(provider, "_ABANDON_RM_TIMEOUT_SECONDS", 0.05)
    _wire(monkeypatch, provider, run, [])

    async def stalled_rm(_name: str) -> bool:
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(provider, "_remove", stalled_rm)

    async with asyncio.timeout(5):
        task = asyncio.create_task(provider.create(WorkspaceConfig(provider="docker")))
        await run.started.wait()
        task.cancel()
        run.finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert list(tmp_path.iterdir()) == []


async def test_cancel_while_waiting_for_the_container_to_run_removes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    run = _Run()
    run.finish.set()  # `docker run -d` returned; the container exists
    provider = _provider(monkeypatch, tmp_path)
    _wire(monkeypatch, provider, run, events)
    waiting = asyncio.Event()

    async def never_running(_name: str) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "_wait_for_running", never_running)

    async with asyncio.timeout(5):
        task = asyncio.create_task(provider.create(WorkspaceConfig(provider="docker")))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(events) == 1 and events[0].startswith("rm agentic-ws-")
    assert list(tmp_path.iterdir()) == []
