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
    monkeypatch.setattr(provider, "_ensure_network", noop)
    monkeypatch.setattr(provider, "_cleanup_container", noop)

    with pytest.raises(RuntimeError) as exc:
        await provider.create(WorkspaceConfig(provider="docker"))

    message = str(exc.value)
    assert expected in message
    assert f"docker create exited {returncode}" in message
    if stderr:
        assert "stdout detail" not in message
