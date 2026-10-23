"""Bounded, link-safe reads for `SupportsFileTransfer.read_files` (#100).

The workspace is untrusted, so what it holds decides nothing: every provider
lists the root with the same in-sandbox walk, decides here what may be read,
reads only that, and checks the result here again. A provider supplies only
the transport. Every entry the caller asked for comes back either as bytes or
as a `FileRefusal` naming the path and the reason; none is skipped.

The listing is only a plan. The sandbox can change any path after it, so the
reader opens each file once and checks the descriptor itself: the path the
kernel resolved it to, its type and its link count, then copies the bytes
from that same descriptor. Swapping a name or an ancestor for a symlink, or
linking in another file, after the listing is refused, not read.

Both the walk and the reads stream, so the sandbox's work and temp storage
are bounded by the limits: the walk stops once `max_entries + 1` records are
out (plus one pipe buffer), and at most `max_file_bytes + 1` bytes of one
file are held at a time.

The sandbox needs /proc and GNU find, xargs, head and stat, as the Docker and
E2B base images have. A file replaced by a FIFO between the reader's type
check and its open can block that read; it cannot return the FIFO's bytes.
"""

from __future__ import annotations

import fnmatch
import posixpath
import shlex
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
_LISTING_ENTRY_BYTES = 2 * 4096 + 64
# Per-file framing on top of the content: kind, length, the path and, for a
# refusal, the link or opened path it names, each up to PATH_MAX.
_READ_ENTRY_BYTES = 2 * 4096 + 64
_SIGPIPE_STATUS = 141


def _script(command: str, then: str, *, base64: bool, stopped_ok: bool = False) -> str:
    """Pipe `command` into `then` as it runs, exiting with `command`'s status.

    Nothing is spooled: once `then` has read what it needs it exits, and
    `command` stops on the broken pipe, which `stopped_ok` accepts.
    """
    encode = " | base64 -w0" if base64 else ""
    accept = (
        f'[ "$rc" -eq 0 ] || [ "$rc" -eq {_SIGPIPE_STATUS} ]' if stopped_ok else '[ "$rc" -eq 0 ]'
    )
    return (
        "s=$(mktemp) || exit 1; "
        f'{{ ({command}); echo "$?" > "$s"; }} | {then}{encode}; out=$?; '
        'rc=$(cat "$s"); rm -f "$s"; '
        f'if {accept}; then exit "$out"; fi; exit "${{rc:-1}}"'
    )


def listing_script(base_path: str, limits: ReadLimits, *, base64: bool = False) -> str:
    """The walk: at most `max_entries + 1` records, none deeper than `max_depth + 1`.

    find writes into head, which exits after the last record wanted, so the
    walk goes no further than that plus what one pipe buffer holds.
    """
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
    return f"cd -- {shlex.quote(base_path)} || exit 1; " + _script(
        find, head, base64=base64, stopped_ok=True
    )


def listing_cap(limits: ReadLimits) -> int:
    """The most listing bytes a sandbox following the walk can send."""
    return (limits.max_entries + 1) * _LISTING_ENTRY_BYTES


# One record per path on the command line, in order: "<kind> <n>\0<path>\0"
# then n bytes. Each file is opened once, as fd 3, and judged by what fd 3
# is, not by its name: the path the kernel holds for it must be the one asked
# for under the root (so no symlink, in the name or any ancestor, was
# followed), it must be a regular file, and it must have one link. Its bytes
# are then copied from fd 3, at most `max_file_bytes + 1` of them, into a
# temp file so the record can carry the length. Kinds: f content, l symlink
# (its target), r opened elsewhere (the path opened), s special, h hard link,
# b over the per-file limit, t over the total.
_READER = r"""
t=$1; shift; used=0
emit() { printf '%s %s\0%s\0%s' "$1" "${#2}" "$p" "$2"; }
for p; do
  if [ -L "./$p" ]; then
    x=$(readlink -- "./$p"; echo .); x=${x%.}; emit l "${x%?}"
  elif [ ! -f "./$p" ]; then
    emit s ""
  elif ! command exec 3<"./$p"; then
    exit 1
  else
    x=$(readlink /proc/self/fd/3; echo .); x=${x%.}; x=${x%?}
    if [ "$x" != "$ROOT/$p" ]; then
      emit r "$x"
    elif [ ! -f /proc/self/fd/3 ]; then
      emit s ""
    elif [ "$(stat -L -c %h /proc/self/fd/3)" != 1 ]; then
      emit h ""
    else
      head -c "$((FILE + 1))" <&3 > "$t" || exit 1
      n=$(wc -c < "$t")
      if [ "$n" -gt "$FILE" ]; then
        emit b ""
      elif [ "$((used + n))" -gt "$TOTAL" ]; then
        emit t ""
      else
        used=$((used + n)); printf 'f %s\0%s\0' "$n" "$p"; cat "$t" || exit 1
      fi
    fi
    exec 3<&-
  fi
done
"""


def read_script(base_path: str, limits: ReadLimits, *, base64: bool = False) -> str:
    """Read the NUL-separated paths on stdin under `base_path`, one record each."""
    settings = (
        f"ROOT={shlex.quote(base_path.rstrip('/'))} FILE={limits.max_file_bytes} "
        f"TOTAL={limits.max_total_bytes}"
    )
    reader = (
        "t=$(mktemp) || exit 1; "
        f'{settings} xargs -0 -r sh -c {shlex.quote(_READER)} sh "$t"; rc=$?; '
        'rm -f "$t"; exit "$rc"'
    )
    return f"cd -- {shlex.quote(base_path)} || exit 1; export LC_ALL=C; " + _script(
        reader, "cat", base64=base64
    )


def read_stdin(plan: ReadPlan) -> bytes:
    return b"".join(path.encode("utf-8", "surrogateescape") + b"\0" for path, _ in plan.reads)


def read_cap(plan: ReadPlan, limits: ReadLimits) -> int:
    """The most bytes the planned reads can produce within `limits`."""
    return limits.max_total_bytes + len(plan.reads) * _READ_ENTRY_BYTES


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def plan_reads(
    listing: bytes, patterns: Sequence[str], limits: ReadLimits, base_path: str
) -> ReadPlan:
    """Decide from the walk's records what to archive and what to refuse.

    The records come from the sandbox, so they are parsed strictly and only
    the first `max_entries + 1` are looked at, whatever the sandbox sent.
    The limits are then applied in path order, not the walk's, so which
    files fit the total does not depend on the order the directory lists.
    """
    fields = listing.split(b"\0")
    if fields[-1] != b"" or (len(fields) - 1) % _FIELDS:
        raise ReadFilesError("the sandbox listing is not whole NUL-terminated records")
    records: list[tuple[str, str, int, int, int, bytes]] = []
    refused: list[FileRefusal] = []
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
        records.append((path, kind, nlink, nbytes, level, raw_target))
    reads: list[tuple[str, int]] = []
    total = 0
    for path, kind, nlink, nbytes, level, raw_target in sorted(records):
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
    return ReadPlan(tuple(reads), tuple(refused))


def _inside(resolved: str, base_path: str) -> bool:
    return resolved == base_path or resolved.startswith(base_path.rstrip("/") + "/")


def _escapes(path: str, target: str, base_path: str) -> bool:
    if target.startswith("/"):
        return not _inside(posixpath.normpath(target), base_path)
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return resolved == ".." or resolved.startswith("../")


_REFUSED_BY_KIND: dict[bytes, RefusalReason] = {
    b"s": "special_file",
    b"h": "hard_link",
    b"b": "file_too_large",
    b"t": "total_bytes_exceeded",
}


def collect(stream: bytes, plan: ReadPlan, limits: ReadLimits, base_path: str) -> ReadFilesResult:
    """Check the sandbox's records for `plan.reads` against `limits` and return the result.

    The sandbox judged each file by the descriptor it read from, and the
    limits are applied here again; records for other names, out of order,
    or not whole are an error. With nothing planned, nothing was read and
    `stream` is ignored.
    """
    if not plan.reads:
        return ReadFilesResult(files=(), refused=_by_path(plan.refused))
    files: list[tuple[str, bytes]] = []
    refused = list(plan.refused)
    total = 0
    at = 0
    for path, _ in plan.reads:
        kind, payload, at = _next_record(stream, at, path)
        if kind == b"f":
            if len(payload) > limits.max_file_bytes:
                refused.append(FileRefusal(path, "file_too_large"))
            elif total + len(payload) > limits.max_total_bytes:
                refused.append(FileRefusal(path, "total_bytes_exceeded"))
            else:
                total += len(payload)
                files.append((path, payload))
        elif kind == b"l":
            escaping = _escapes(path, payload.decode("utf-8", "surrogateescape"), base_path)
            refused.append(FileRefusal(path, "escaping_symlink" if escaping else "symlink"))
        elif kind == b"r":
            opened = payload.decode("utf-8", "surrogateescape")
            inside = _inside(posixpath.normpath(opened), base_path)
            refused.append(FileRefusal(path, "symlink" if inside else "escaping_symlink"))
        elif kind in _REFUSED_BY_KIND:
            refused.append(FileRefusal(path, _REFUSED_BY_KIND[kind]))
        else:
            raise ReadFilesError(f"the sandbox read {path!r} as unknown kind {kind!r}")
    if at != len(stream):
        raise ReadFilesError("the sandbox sent records past the planned reads")
    return ReadFilesResult(files=tuple(sorted(files)), refused=_by_path(refused))


def _next_record(stream: bytes, at: int, path: str) -> tuple[bytes, bytes, int]:
    """The kind and payload of the record for `path` at `at`, and where the next starts."""
    head_end = stream.find(b"\0", at)
    path_end = stream.find(b"\0", head_end + 1) if head_end >= 0 else -1
    if path_end < 0:
        raise ReadFilesError(f"the sandbox read is missing {path!r}")
    kind, _, size = stream[at:head_end].partition(b" ")
    if not size.isdigit():
        raise ReadFilesError(f"the sandbox read has a malformed record {stream[at:head_end]!r}")
    named = stream[head_end + 1 : path_end].decode("utf-8", "surrogateescape")
    if named != path:
        raise ReadFilesError(f"the sandbox read {named!r} where {path!r} was planned")
    end = path_end + 1 + int(size)
    if end > len(stream):
        raise ReadFilesError(f"the sandbox read of {path!r} is cut short")
    return kind, stream[path_end + 1 : end], end


def _by_path(refused: Sequence[FileRefusal]) -> tuple[FileRefusal, ...]:
    return tuple(sorted(refused, key=lambda r: r.path))
