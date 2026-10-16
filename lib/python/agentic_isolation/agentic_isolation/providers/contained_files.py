"""Workspace file access confined to a workspace directory (APSS W-3.2-b).

A workspace path is resolved one component at a time from descriptors this
module opened itself, so a symlink, a `..` above the root or an absolute path
never reaches a file outside the workspace, and a FIFO, socket or device is
never opened.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from pathlib import Path, PurePosixPath


class NotARegularFileError(ValueError):
    """A workspace path names a FIFO, socket or device, which the file helpers never open.

    Opening a FIFO waits for its other end, which would hold the event loop,
    and a device node the workload made would reach the host's device.
    """


def _open_contained(root: Path, path: str, flags: int, *, create_parents: bool = False) -> int:
    """Open `path` under `root` and return its descriptor, or raise (APSS W-3.2-b).

    Every component, including each `..`, is resolved against a descriptor
    this walk opened itself with O_NOFOLLOW, so the check and the use are the
    same system call: a symlink the workload swaps in at any moment is
    refused, never followed, and `..` can never climb above `root`. A FIFO,
    socket or device is refused with NotARegularFileError and never waited on.
    """
    if Path(path).is_absolute():
        raise ValueError(f"workspace path {path!r} must be relative to the workspace")
    *parents, name = PurePosixPath(path).parts or (".",)
    if name in (".", ".."):
        raise ValueError(f"workspace path {path!r} names no file in the workspace")

    def escapes() -> ValueError:
        return ValueError(f"workspace path {path!r} escapes the workspace directory")

    def not_regular() -> NotARegularFileError:
        return NotARegularFileError(
            f"workspace path {path!r} is not a regular file (a FIFO, socket or device)"
        )

    nofollow = os.O_NOFOLLOW | os.O_CLOEXEC
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
    # The root itself is the provider's own path; only what lies beneath it
    # is writable by the workload.
    stack = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        for part in parents:
            if part == "..":
                if len(stack) == 1:
                    raise escapes()
                os.close(stack.pop())
                continue
            if create_parents:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=stack[-1])
            try:
                stack.append(os.open(part, directory_flags, dir_fd=stack[-1]))
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise escapes() from e
                raise
        # Checked before the open so a device node is never opened at all, and
        # again on the descriptor, which the workload cannot swap.
        with contextlib.suppress(FileNotFoundError):
            if _is_special(os.stat(name, dir_fd=stack[-1], follow_symlinks=False).st_mode):
                raise not_regular()
        try:
            # O_NONBLOCK: a FIFO swapped in after the check opens at once (or
            # fails ENXIO) instead of waiting for a writer or reader.
            leaf = os.open(name, flags | nofollow | os.O_NONBLOCK, 0o644, dir_fd=stack[-1])
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise escapes() from e
            if e.errno == errno.ENXIO:  # a FIFO with no reader, or a socket
                raise not_regular() from e
            raise
        if _is_special(os.fstat(leaf).st_mode):
            os.close(leaf)
            raise not_regular()
        return leaf
    finally:
        for fd in stack:
            os.close(fd)


def _is_special(mode: int) -> bool:
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode)


def exists_contained(root: Path, path: str) -> tuple[bool, bool]:
    """Whether `path` under `root` exists, and whether it is a regular file.

    Resolved by the same walk as `_open_contained`, so a symlink on the path,
    a `..` above `root` or an absolute path raises ValueError instead of
    answering for a file outside the workspace. A directory, FIFO, socket or
    device exists but is not regular, and a FIFO, socket or device is never
    opened.
    """
    try:
        fd = _open_contained(root, path, os.O_RDONLY)
    except FileNotFoundError:
        return False, False
    except NotARegularFileError:
        return True, False
    try:
        return True, stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
