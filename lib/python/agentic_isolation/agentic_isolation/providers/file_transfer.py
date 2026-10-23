"""Bounded, link-safe reads for `SupportsFileTransfer.read_files` (#100).

The workspace is untrusted, so what it holds decides nothing: every provider
lists the root with the same in-sandbox walk, decides here what may be read,
archives only that, and checks the archive here again. A provider supplies
only the transport. Every entry the caller asked for comes back either as
bytes or as a `FileRefusal` naming the path and the reason; none is skipped.

The sandbox needs GNU find, head and tar, as the Docker and E2B base images
have. A file swapped between the listing and the archive is still caught as a
symlink or special file by the archive check; a new hard link made in that
window is not, and can only alias a file the sandbox can already read.
"""

from __future__ import annotations

import fnmatch
import io
import posixpath
import shlex
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

RefusalReason = Literal[
    "hard_link",
    "symlink",
    "escaping_symlink",
    "special_file",
    "file_too_large",
    "total_bytes_exceeded",
    "depth_exceeded",
    "entry_limit_exceeded",
]


@dataclass(frozen=True)
class ReadLimits:
    """Bounds on one `read_files` call. The defaults are finite on purpose.

    - `max_total_bytes`: content returned across all files.
    - `max_file_bytes`: content of any one file.
    - `max_entries`: entries the walk lists, of every type and whether or not
      they match, since listing them is the cost.
    - `max_depth`: path components of an entry; `a/b.txt` is depth 2. The
      walk does not descend below it.
    """

    max_total_bytes: int = 64 * 1024 * 1024
    max_file_bytes: int = 16 * 1024 * 1024
    max_entries: int = 10_000
    max_depth: int = 32

    def __post_init__(self) -> None:
        for name in ("max_total_bytes", "max_file_bytes", "max_entries", "max_depth"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"ReadLimits.{name} must be a positive int, got {value!r}")


# What a caller that passes no limits gets: finite, never unlimited.
DEFAULT_READ_LIMITS = ReadLimits()


@dataclass(frozen=True)
class FileRefusal:
    """An entry `read_files` would not return, relative to the root, and why.

    `entry_limit_exceeded` names the first entry past the limit: the walk
    stopped there, so entries after it were neither read nor refused.
    """

    path: str
    reason: RefusalReason


@dataclass(frozen=True)
class ReadFilesResult:
    """What `read_files` returned and what it refused, each sorted by path."""

    files: tuple[tuple[str, bytes], ...]
    refused: tuple[FileRefusal, ...]

    @property
    def complete(self) -> bool:
        """True when the walk listed the whole root (no entry limit was hit)."""
        return all(r.reason != "entry_limit_exceeded" for r in self.refused)


class ReadFilesError(RuntimeError):
    """The sandbox's walk or archive failed, or answered outside the contract."""


@dataclass(frozen=True)
class ReadPlan:
    """The entries to archive (path, listed size) and those already refused."""

    reads: tuple[tuple[str, int], ...]
    refused: tuple[FileRefusal, ...]


_FIELDS = 3  # "%y %n %s %d", "%P", "%l": each NUL-terminated
_SPECIAL = {"b", "c", "p", "s", "D"}
# Per-entry tar overhead allowed on top of the content: header, GNU long
# name and long link records for paths up to PATH_MAX, and block padding.
_TAR_ENTRY_OVERHEAD = 3 * 512 + 2 * 4096 + 512
_TAR_TRAILER = 20 * 512
_LISTING_ENTRY_BYTES = 2 * 4096 + 64


def _script(command: str, emit: str, *, base64: bool) -> str:
    """Run `command` into a sandbox temp file, then emit it, keeping its status."""
    encode = " | base64 -w0" if base64 else ""
    return (
        "t=$(mktemp) || exit 1; "
        f'{command} > "$t"; rc=$?; '
        f'if [ "$rc" -eq 0 ]; then {emit} "$t"{encode}; rc=$?; fi; '
        'rm -f "$t"; exit "$rc"'
    )


def listing_script(base_path: str, limits: ReadLimits, *, base64: bool = False) -> str:
    """The walk: at most `max_entries + 1` records, none deeper than `max_depth + 1`."""
    find = shlex.join(
        [
            "find",
            ".",
            "-mindepth",
            "1",
            "-maxdepth",
            str(limits.max_depth + 1),
            "-printf",
            r"%y %n %s %d\0%P\0%l\0",
        ]
    )
    head = f"head -z -n {_FIELDS * (limits.max_entries + 1)}"
    return f"cd -- {shlex.quote(base_path)} || exit 1; " + _script(find, head, base64=base64)


def listing_cap(limits: ReadLimits) -> int:
    """The most listing bytes a sandbox following the walk can send."""
    return (limits.max_entries + 1) * _LISTING_ENTRY_BYTES


def archive_script(base_path: str, *, base64: bool = False) -> str:
    """Archive the NUL-separated `./path` names on stdin, never following links."""
    tar = shlex.join(
        [
            "tar",
            "-C",
            base_path,
            "--no-recursion",
            "--null",
            "--verbatim-files-from",
            "-cf",
            "-",
            "-T",
            "-",
        ]
    )
    return _script(tar, "cat", base64=base64)


def archive_stdin(plan: ReadPlan) -> bytes:
    return b"".join(
        b"./" + path.encode("utf-8", "surrogateescape") + b"\0" for path, _ in plan.reads
    )


def archive_cap(plan: ReadPlan, limits: ReadLimits) -> int:
    """The most archive bytes the planned reads can produce within `limits`."""
    return limits.max_total_bytes + len(plan.reads) * _TAR_ENTRY_OVERHEAD + _TAR_TRAILER


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def _escapes(path: str, target: str, base_path: str) -> bool:
    if target.startswith("/"):
        resolved = posixpath.normpath(target)
        return resolved != base_path and not resolved.startswith(base_path.rstrip("/") + "/")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return resolved == ".." or resolved.startswith("../")


def plan_reads(
    listing: bytes, patterns: Sequence[str], limits: ReadLimits, base_path: str
) -> ReadPlan:
    """Decide from the walk's records what to archive and what to refuse.

    The records come from the sandbox, so they are parsed strictly and only
    the first `max_entries + 1` are looked at, whatever the sandbox sent.
    """
    fields = listing.split(b"\0")
    if fields[-1] != b"" or (len(fields) - 1) % _FIELDS:
        raise ReadFilesError("the sandbox listing is not whole NUL-terminated records")
    reads: list[tuple[str, int]] = []
    refused: list[FileRefusal] = []
    total = 0
    for index in range(0, len(fields) - 1, _FIELDS):
        head, raw_path, raw_target = fields[index : index + _FIELDS]
        path = raw_path.decode("utf-8", "surrogateescape")
        if index // _FIELDS == limits.max_entries:
            refused.append(FileRefusal(path, "entry_limit_exceeded"))
            break
        try:
            kind, links, size, depth = head.decode().split(" ")
            nlink, nbytes, level = int(links), int(size), int(depth)
        except ValueError as exc:
            raise ReadFilesError(f"the sandbox listing has a malformed record {head!r}") from exc
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise ReadFilesError(f"the sandbox listing named a path outside the root: {path!r}")
        if level > limits.max_depth:
            # A directory past the limit is not walked, so what it holds is unknown.
            if kind == "d" or _matches(path, patterns):
                refused.append(FileRefusal(path, "depth_exceeded"))
            continue
        if kind == "d" or not _matches(path, patterns):
            continue
        if kind == "l":
            target = raw_target.decode("utf-8", "surrogateescape")
            escaping = _escapes(path, target, base_path)
            refused.append(FileRefusal(path, "escaping_symlink" if escaping else "symlink"))
        elif kind != "f":
            refused.append(FileRefusal(path, "special_file"))
        elif nlink > 1:
            refused.append(FileRefusal(path, "hard_link"))
        elif nbytes > limits.max_file_bytes:
            refused.append(FileRefusal(path, "file_too_large"))
        elif total + nbytes > limits.max_total_bytes:
            refused.append(FileRefusal(path, "total_bytes_exceeded"))
        else:
            total += nbytes
            reads.append((path, nbytes))
    return ReadPlan(tuple(sorted(reads)), tuple(refused))


def collect(archive: bytes, plan: ReadPlan, limits: ReadLimits, base_path: str) -> ReadFilesResult:
    """Check the sandbox's archive of `plan.reads` against `limits` and return the result.

    Anything that changed since the listing is refused here by what the
    archive says it is now; an archive holding other names is an error.
    With nothing planned, no archive was taken and `archive` is ignored.
    """
    if not plan.reads:
        return ReadFilesResult(files=(), refused=_by_path(plan.refused))
    expected = {path for path, _ in plan.reads}
    files: list[tuple[str, bytes]] = []
    refused = list(plan.refused)
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as members:
            for member in members:
                path = member.name.removeprefix("./")
                if path not in expected:
                    raise ReadFilesError(f"the sandbox archive holds an unrequested entry {path!r}")
                expected.remove(path)
                if member.islnk():
                    refused.append(FileRefusal(path, "hard_link"))
                elif member.issym():
                    escaping = _escapes(path, member.linkname, base_path)
                    refused.append(FileRefusal(path, "escaping_symlink" if escaping else "symlink"))
                elif not member.isreg():
                    refused.append(FileRefusal(path, "special_file"))
                elif member.size > limits.max_file_bytes:
                    refused.append(FileRefusal(path, "file_too_large"))
                elif total + member.size > limits.max_total_bytes:
                    refused.append(FileRefusal(path, "total_bytes_exceeded"))
                else:
                    handle = members.extractfile(member)
                    assert handle is not None  # extractfile returns a reader for a regular file
                    total += member.size
                    files.append((path, handle.read()))
    except tarfile.TarError as exc:
        raise ReadFilesError(f"the sandbox archive is not a readable tar: {exc}") from exc
    if expected:
        raise ReadFilesError(f"the sandbox archive is missing {', '.join(sorted(expected))}")
    return ReadFilesResult(files=tuple(sorted(files)), refused=_by_path(refused))


def _by_path(refused: Sequence[FileRefusal]) -> tuple[FileRefusal, ...]:
    return tuple(sorted(refused, key=lambda r: r.path))
