"""The provider-neutral decisions behind `read_files` (#100).

The walk's records and the read's come from an untrusted sandbox, so these
feed both hand-built answers the conformance suite cannot provoke, a sandbox
that answers outside the contract, and run the real scripts under `sh` on a
local root, changing it between the listing and the read where that is the
case, to check what the reader does and how much the walk and read cost.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

import pytest

from agentic_isolation import FileRefusal, ReadFilesError, ReadFilesResult, ReadLimits
from agentic_isolation.providers.file_transfer import (
    ReadPlan,
    collect,
    listing_script,
    plan_reads,
    read_cap,
    read_script,
    read_stdin,
)


def _record(kind: str, path: str, *, nlink: int = 1, size: int = 1, target: str = "") -> bytes:
    depth = path.count("/") + 1
    return f"{kind} {nlink} {size} {depth}\0{path}\0{target}\0".encode()


def _read(*records: tuple[str, str, bytes]) -> bytes:
    """The reader's records: (path, kind, payload) each."""
    return b"".join(
        f"{kind} {len(payload)}\0{path}\0".encode() + payload for path, kind, payload in records
    )


def _sh(script: str, stdin: bytes = b"", path: str | None = None) -> bytes:
    env = {**os.environ, "PATH": path or os.environ["PATH"]}
    return subprocess.run(
        ["sh", "-c", script], input=stdin, capture_output=True, check=True, env=env
    ).stdout


def _plan(root: Path, patterns: list[str], limits: ReadLimits) -> ReadPlan:
    return plan_reads(_sh(listing_script(str(root), limits)), patterns, limits, str(root))


def _read_now(root: Path, plan: ReadPlan, limits: ReadLimits) -> ReadFilesResult:
    out = _sh(read_script(str(root), limits), read_stdin(plan))
    assert len(out) <= read_cap(plan, limits)
    return collect(out, plan, limits, str(root))


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_limits_must_be_positive_ints(value: object) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        ReadLimits(max_entries=value)  # type: ignore[arg-type]  # the bad value is the test


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), None])
def test_the_timeout_must_be_positive_and_finite(value: object) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ReadLimits(timeout_seconds=value)  # type: ignore[arg-type]  # the bad value is the test


def test_the_plan_looks_at_no_more_records_than_the_entry_limit_allows() -> None:
    listing = b"".join(_record("f", f"f{n}") for n in range(5))

    plan = plan_reads(listing, ["*"], ReadLimits(max_entries=2), "/workspace")

    assert plan == ReadPlan(
        reads=(("f0", 1), ("f1", 1)), refused=(FileRefusal("f2", "entry_limit_exceeded"),)
    )


@pytest.mark.parametrize("order", list(itertools.permutations(("a", "b", "c"))))
def test_the_total_is_allocated_in_path_order_whatever_order_the_walk_lists(
    order: tuple[str, ...],
) -> None:
    listing = b"".join(_record("f", path, size=4) for path in order)

    plan = plan_reads(listing, ["*"], ReadLimits(max_total_bytes=8), "/workspace")

    assert plan == ReadPlan(
        reads=(("a", 4), ("b", 4)), refused=(FileRefusal("c", "total_bytes_exceeded"),)
    )


@pytest.mark.parametrize(
    "listing",
    [
        b"f 1 1 1\0half",
        b"f one 1 1\0a\0\0",
        _record("f", "../etc/passwd"),
        _record("f", "/etc/passwd"),
    ],
)
def test_a_listing_outside_the_contract_is_an_error(listing: bytes) -> None:
    with pytest.raises(ReadFilesError):
        plan_reads(listing, ["*"], ReadLimits(), "/workspace")


def test_a_sandbox_that_reports_symlinks_is_refused_by_where_they_lead() -> None:
    plan = ReadPlan(reads=(("a", 1), ("b", 1), ("c", 1), ("d", 1)), refused=())

    got = collect(
        _read(
            ("a", "l", b"/etc/shadow"),
            ("b", "l", b"a"),
            ("c", "r", b"/etc/shadow"),
            ("d", "r", b"/workspace/elsewhere"),
        ),
        plan,
        ReadLimits(),
        "/workspace",
    )

    assert got == ReadFilesResult(
        files=(),
        refused=(
            FileRefusal("a", "escaping_symlink"),
            FileRefusal("b", "symlink"),
            FileRefusal("c", "escaping_symlink"),
            FileRefusal("d", "symlink"),
        ),
    )


def test_content_past_the_limits_is_refused_whatever_the_sandbox_sends() -> None:
    plan = ReadPlan(reads=(("a", 1), ("b", 1)), refused=())
    limits = ReadLimits(max_file_bytes=4, max_total_bytes=5)

    got = collect(_read(("a", "f", b"12345"), ("b", "f", b"1234")), plan, limits, "/workspace")

    assert got == ReadFilesResult(
        files=(("b", b"1234"),), refused=(FileRefusal("a", "file_too_large"),)
    )


@pytest.mark.parametrize(
    "read",
    [
        _read(("a", "f", b"x"), ("other", "f", b"y")),
        _read(("other", "f", b"y")),
        _read(("a", "q", b"")),
        b"f 9\0a\0short",
        b"f x\0a\0",
        b"",
    ],
    ids=["unrequested", "renamed", "unknown-kind", "cut-short", "malformed", "missing"],
)
def test_a_read_that_does_not_answer_the_plan_is_an_error(read: bytes) -> None:
    plan = ReadPlan(reads=(("a", 1),), refused=())

    with pytest.raises(ReadFilesError):
        collect(read, plan, ReadLimits(), "/workspace")


# The real scripts on a real root. Each change below is made after the
# listing and before the read starts, so it is deterministic.


def test_the_reader_refuses_an_ancestor_swapped_for_a_symlink_out_of_the_root(
    tmp_path: Path,
) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "out").mkdir(parents=True)
    outside.mkdir()
    (root / "out" / "secret").write_bytes(b"safe")
    (outside / "secret").write_bytes(b"OUTSIDE_SECRET")
    limits = ReadLimits()
    plan = _plan(root, ["out/*"], limits)

    (root / "out").rename(root / "old-out")
    (root / "out").symlink_to(outside, target_is_directory=True)

    assert plan.reads == (("out/secret", 4),)
    assert _read_now(root, plan, limits) == ReadFilesResult(
        files=(), refused=(FileRefusal("out/secret", "escaping_symlink"),)
    )


def test_the_reader_refuses_a_name_swapped_for_a_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    (root / "b").write_bytes(b"b")
    limits = ReadLimits()
    plan = _plan(root, ["a"], limits)

    (root / "a").unlink()
    (root / "a").symlink_to("b")

    assert _read_now(root, plan, limits) == ReadFilesResult(
        files=(), refused=(FileRefusal("a", "symlink"),)
    )


def test_the_reader_refuses_a_hard_link_made_after_the_listing(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "out").mkdir(parents=True)
    outside.mkdir()
    (root / "out" / "secret").write_bytes(b"safe")
    (outside / "secret").write_bytes(b"OUTSIDE_SECRET")
    limits = ReadLimits()
    plan = _plan(root, ["out/*"], limits)

    (root / "out" / "secret").unlink()
    os.link(outside / "secret", root / "out" / "secret")

    assert plan.reads == (("out/secret", 4),)
    assert _read_now(root, plan, limits) == ReadFilesResult(
        files=(), refused=(FileRefusal("out/secret", "hard_link"),)
    )


def test_the_reader_returns_exact_bytes_of_what_it_opened(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "d").mkdir(parents=True)
    blob = bytes(range(256)) * 3 + "\n\0é".encode()
    (root / "d" / "-odd name\n").write_bytes(blob)
    (root / "e").write_bytes(b"")
    limits = ReadLimits()

    got = _read_now(root, _plan(root, ["*"], limits), limits)

    assert got == ReadFilesResult(files=(("d/-odd name\n", blob), ("e", b"")), refused=())


def test_the_reader_holds_no_more_of_a_file_grown_after_the_listing_than_the_limit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "grow").write_bytes(b"x")
    limits = ReadLimits(max_file_bytes=64, max_total_bytes=64)
    plan = _plan(root, ["grow"], limits)

    with (root / "grow").open("wb") as grown:
        grown.truncate(8 * 1024 * 1024)
    out = _sh(read_script(str(root), limits), read_stdin(plan))

    assert len(out) < 1024
    assert collect(out, plan, limits, str(root)) == ReadFilesResult(
        files=(), refused=(FileRefusal("grow", "file_too_large"),)
    )


_COUNTING_FIND = """#!/bin/sh
{real} "$@"; rc=$?
echo "$rc" > {status}
exit "$rc"
"""


def test_the_walk_stops_once_the_entry_limit_is_out(tmp_path: Path) -> None:
    # Far more entries than one pipe buffer of records: a walk that ran to
    # the end before the limit was applied exits 0, one cut short does not.
    root = tmp_path / "root"
    root.mkdir()
    for n in range(8000):
        (root / f"unmatched-entry-{n:05}").touch()
    tools = tmp_path / "bin"
    tools.mkdir()
    status = tmp_path / "find.status"
    real = subprocess.run(["sh", "-c", "command -v find"], capture_output=True, check=True)
    (tools / "find").write_text(
        _COUNTING_FIND.format(real=real.stdout.decode().strip(), status=status)
    )
    (tools / "find").chmod(0o755)
    limits = ReadLimits(max_entries=1)

    listing = _sh(listing_script(str(root), limits), path=f"{tools}:{os.environ['PATH']}")

    assert status.read_text().strip() != "0"
    plan = plan_reads(listing, ["wanted"], limits, str(root))
    assert [r.reason for r in plan.refused] == ["entry_limit_exceeded"]
