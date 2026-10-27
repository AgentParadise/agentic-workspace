"""Container creation failures retain Docker's diagnostic channels."""

import asyncio
from pathlib import Path

import pytest

from agentic_isolation.config import WorkspaceConfig
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
    provider = WorkspaceDockerProvider(workspace_base_dir=tmp_path)

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


async def test_cancel_during_docker_run_waits_for_it_then_removes_the_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    run_started = asyncio.Event()
    run_finished = asyncio.Event()

    class SlowRun:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            run_started.set()
            await asyncio.Event().wait()  # cancelled here
            raise AssertionError("unreachable")

        async def wait(self) -> int:
            await run_finished.wait()
            events.append("run finished")
            self.returncode = 0
            return 0

    async def spawn(*args: object, **kwargs: object) -> SlowRun:
        return SlowRun()

    async def cleanup(name: str) -> bool:
        events.append(f"cleanup {name}")
        return True

    provider = _provider(monkeypatch, tmp_path)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(provider, "_cleanup_container", cleanup)

    task = asyncio.create_task(provider.create(WorkspaceConfig(provider="docker")))
    await run_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    # A second cancel must not interrupt the cleanup.
    task.cancel()
    run_finished.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    for pending in list(provider._abandoned_creates):
        await pending

    assert events[0] == "run finished"
    assert len(events) == 2 and events[1].startswith("cleanup agentic-ws-")
    assert list(tmp_path.iterdir()) == []
