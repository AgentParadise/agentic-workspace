"""`exists_contained`: existence and file type under a workspace root (APSS W-3.2-b).

Each case builds the file on disk and asks the helper, so the answer comes
from the confined walk, never from a path the test resolved itself.
"""

import os
import time
from pathlib import Path

import pytest

from agentic_isolation.providers import contained_files, docker
from agentic_isolation.providers.contained_files import exists_contained
from tests.test_docker_contract_debt import _open_far_end_later


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def test_a_regular_file_exists_and_is_regular(root: Path) -> None:
    (root / "notes").mkdir()
    (root / "notes" / "plan.md").write_text("plan")

    assert exists_contained(root, "notes/plan.md") == (True, True)


def test_a_fifo_exists_but_is_not_regular_and_is_never_waited_on(root: Path) -> None:
    os.mkfifo(root / "artifact.md")

    began = time.monotonic()
    assert exists_contained(root, "artifact.md") == (True, False)
    assert time.monotonic() - began < 1.0


def test_a_fifo_is_refused_before_it_is_ever_opened(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    # A device node is the case this guards, but making one needs CAP_MKNOD,
    # so a FIFO stands in: the descriptor check alone would open it first.
    os.mkfifo(root / "artifact.md")
    real_open = os.open
    opened: list[object] = []

    def recording_open(
        path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        opened.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", recording_open)

    assert exists_contained(root, "artifact.md") == (True, False)
    assert "artifact.md" not in opened


def test_a_fifo_swapped_in_after_the_check_is_still_not_regular(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    (root / "artifact.md").write_text("regular when checked")
    real_open = os.open

    def racing_open(
        path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if dir_fd is not None and path == "artifact.md":
            os.unlink("artifact.md", dir_fd=dir_fd)
            os.mkfifo("artifact.md", dir_fd=dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", racing_open)
    # An open that waits on the FIFO is released after 3 s, so it fails the
    # timing check below instead of hanging the run.
    _open_far_end_later(root / "artifact.md", os.O_WRONLY, delay=3.0)

    began = time.monotonic()
    assert exists_contained(root, "artifact.md") == (True, False)
    assert time.monotonic() - began < 1.0


def test_a_directory_exists_but_is_not_regular(root: Path) -> None:
    (root / "notes").mkdir()

    assert exists_contained(root, "notes") == (True, False)


@pytest.mark.parametrize("path", ["missing.md", "missing/plan.md"])
def test_a_missing_file_does_not_exist(root: Path, path: str) -> None:
    assert exists_contained(root, path) == (False, False)


@pytest.mark.parametrize("path", ["secret", "link/secret"])
def test_a_symlink_escaping_the_root_is_refused(tmp_path: Path, root: Path, path: str) -> None:
    (tmp_path / "secret").write_text("outside the workspace")
    os.symlink(tmp_path / "secret", root / "secret")
    os.symlink(tmp_path, root / "link")

    with pytest.raises(ValueError, match="escapes the workspace directory"):
        exists_contained(root, path)


def test_dotdot_above_the_root_is_refused(tmp_path: Path, root: Path) -> None:
    (tmp_path / "secret").write_text("outside the workspace")

    with pytest.raises(ValueError, match="escapes the workspace directory"):
        exists_contained(root, "../secret")


def test_dotdot_that_stays_inside_the_root_resolves(root: Path) -> None:
    (root / "notes").mkdir()
    (root / "plan.md").write_text("plan")

    assert exists_contained(root, "notes/../plan.md") == (True, True)


def test_an_absolute_path_is_refused(tmp_path: Path, root: Path) -> None:
    (tmp_path / "secret").write_text("outside the workspace")

    with pytest.raises(ValueError, match="must be relative to the workspace"):
        exists_contained(root, str(tmp_path / "secret"))


def test_docker_still_exports_the_error_it_raises() -> None:
    # Importers of providers.docker.NotARegularFileError keep working after the move.
    assert docker.NotARegularFileError is contained_files.NotARegularFileError
    assert issubclass(contained_files.NotARegularFileError, ValueError)
