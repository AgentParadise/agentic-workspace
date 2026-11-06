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
from agentic_isolation.providers import file_transfer
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


def test_the_reader_names_a_file_removed_after_the_listing_as_vanished(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "gone").write_bytes(b"x")
    (root / "kept").write_bytes(b"y")
    limits = ReadLimits()
    plan = _plan(root, ["*"], limits)

    (root / "gone").unlink()

    assert _read_now(root, plan, limits) == ReadFilesResult(
        files=(("kept", b"y"),), refused=(FileRefusal("gone", "vanished"),)
    )


def test_the_reader_keeps_one_total_across_the_batches_xargs_splits_the_paths_into(
    tmp_path: Path,
) -> None:
    # 600 names of 250 bytes are more than one xargs command line holds, so
    # the reader runs more than once; the total must not restart with it.
    root = tmp_path / "root"
    root.mkdir()
    names = [f"{n:04}".ljust(250, "x") for n in range(600)]
    for name in names:
        (root / name).write_bytes(b"z")
    limits = ReadLimits(max_total_bytes=500)
    plan = ReadPlan(reads=tuple((name, 1) for name in names), refused=())

    out = _sh(read_script(str(root), limits), read_stdin(plan))

    kinds = []
    at = 0
    for name in names:
        kind, _, at = file_transfer._next_record(out, at, name)
        kinds.append(kind)
    assert kinds == [b"f"] * 500 + [b"t"] * 100


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


@pytest.mark.parametrize(
    ("patterns", "prefix"),
    [
        (["artifacts/output/**/*"], ("artifacts", "output")),
        (["artifacts/output/a.md", "artifacts/output/b/*"], ("artifacts", "output")),
        (["artifacts/output/*", "artifacts/logs/*"], ("artifacts",)),
        (["*.md"], ()),
        (["a*/output/*"], ()),
        (["artifacts/../etc/*"], ()),
        (["/etc/*"], ()),
        ([], ()),
    ],
)
def test_the_walk_starts_at_the_literal_prefix_every_pattern_shares(
    patterns: list[str], prefix: tuple[str, ...]
) -> None:
    assert file_transfer.walk_prefix(patterns) == prefix


_OUTPUT = ["artifacts/output/**/*"]


def _output_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    out = root / "artifacts" / "output"
    out.mkdir(parents=True)
    (out / "deliverable.md").write_bytes(b"the work")
    return root


def _plan_for(root: Path, patterns: list[str], limits: ReadLimits) -> ReadPlan:
    listing = _sh(listing_script(str(root), limits, patterns=patterns))
    return plan_reads(listing, patterns, limits, str(root))


def test_a_large_tree_beside_the_output_neither_counts_nor_truncates_the_read(
    tmp_path: Path,
) -> None:
    # A toolchain image keeps its cargo registry in /workspace/.tools, and a
    # superproject's .git/modules is as large: 20k entries the patterns can
    # never match (syntropic137/syntropic137#2185).
    root = _output_root(tmp_path)
    cache = root / ".tools" / "cargo"
    cache.mkdir(parents=True)
    for n in range(20_000):
        (cache / f"crate-{n:05}").touch()
    limits = ReadLimits()

    whole = plan_reads(_sh(listing_script(str(root), limits)), _OUTPUT, limits, str(root))
    assert [r.reason for r in whole.refused] == ["entry_limit_exceeded"], "the hazard"

    plan = _plan_for(root, _OUTPUT, limits)
    result = _read_now(root, plan, limits)
    assert result.complete
    assert result.files == (("artifacts/output/deliverable.md", b"the work"),)


def test_the_entry_limit_still_counts_what_is_under_the_prefix(tmp_path: Path) -> None:
    root = _output_root(tmp_path)
    for n in range(5):
        (root / "artifacts" / "output" / f"extra-{n}").touch()

    plan = _plan_for(root, _OUTPUT, ReadLimits(max_entries=3))

    assert [r.reason for r in plan.refused] == ["entry_limit_exceeded"]


def test_the_depth_limit_still_counts_from_the_root(tmp_path: Path) -> None:
    root = _output_root(tmp_path)
    deep = root / "artifacts" / "output" / "a"
    deep.mkdir()
    (deep / "b.md").touch()
    limits = ReadLimits(max_depth=3)

    plan = _plan_for(root, _OUTPUT, limits)

    assert FileRefusal("artifacts/output/a/b.md", "depth_exceeded") in plan.refused
    assert ("artifacts/output/deliverable.md", 8) in plan.reads


@pytest.mark.parametrize("make", ["missing", "a file"])
def test_a_prefix_that_is_no_directory_reads_nothing(tmp_path: Path, make: str) -> None:
    root = tmp_path / "workspace"
    (root / ".tools").mkdir(parents=True)
    (root / ".tools" / "x").touch()
    if make == "a file":
        (root / "artifacts").write_bytes(b"not a directory")

    plan = _plan_for(root, _OUTPUT, ReadLimits())

    assert plan == ReadPlan((), ())


def test_a_prefix_that_looks_like_a_find_option_is_a_path_not_an_action(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "-delete").mkdir(parents=True)
    (root / "-delete" / "x").write_bytes(b"x")
    (root / "keep").write_bytes(b"keep")
    limits = ReadLimits()

    result = _read_now(root, _plan_for(root, ["-delete/*"], limits), limits)

    assert result.files == (("-delete/x", b"x"),)
    assert (root / "keep").read_bytes() == b"keep"


def test_an_ancestor_swapped_for_a_symlink_is_listed_never_walked(tmp_path: Path) -> None:
    # Walking from the root, find meets the link itself and does not follow
    # it, so no name, size or target from outside the root reaches the listing.
    outside = tmp_path / "outside"
    (outside / "output").mkdir(parents=True)
    (outside / "output" / "secret-name").write_bytes(b"x")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "artifacts").symlink_to(outside)

    listing = _sh(listing_script(str(root), ReadLimits(), patterns=_OUTPUT))

    assert b"secret-name" not in listing
    assert listing.split(b"\0")[1] == b"artifacts"


def test_one_globstar_widens_and_many_do_not_multiply() -> None:
    assert file_transfer._variants("a/**/b") == ("a/**/b", "a/b")
    assert file_transfer._variants("a/**/b/**/c") == ("a/**/b/**/c",)
    assert file_transfer._variants("a/[**/].txt") == ("a/[**/].txt",)
    many = "/".join(["**", "x"] * 30)
    assert file_transfer._variants(many) == (many,)


@pytest.mark.parametrize("link", ["artifacts", "artifacts/output"])
def test_a_symlink_on_the_prefix_walks_the_whole_root_and_never_follows_it(
    tmp_path: Path, link: str
) -> None:
    outside = tmp_path / "outside"
    (outside / "output").mkdir(parents=True)
    (outside / "output" / "secret").write_bytes(b"not yours")
    (outside / "secret").write_bytes(b"not yours")
    root = tmp_path / "workspace"
    root.mkdir()
    if link == "artifacts":
        (root / "artifacts").symlink_to(outside)
    else:
        (root / "artifacts").mkdir()
        (root / "artifacts" / "output").symlink_to(outside)
    limits = ReadLimits()

    plan = _plan_for(root, _OUTPUT, limits)
    result = _read_now(root, plan, limits)

    assert result.files == ()
    assert all(b"not yours" not in body for _, body in result.files)
    assert plan.reads == ()


@pytest.mark.parametrize(
    ("path", "pattern", "matches"),
    [
        ("artifacts/output/deliverable.md", "artifacts/output/**/*", True),
        ("artifacts/output/a/b.md", "artifacts/output/**/*", True),
        ("artifacts/input/x.md", "artifacts/output/**/*", False),
        ("a/b/c.txt", "**/*.txt", True),
        ("c.txt", "**/*.txt", True),
        ("x/y/z", "x/**/y/**/z", False),  # two globstars: plain fnmatch only
        ("x/z", "x/**/y/**/z", False),
        ("a/[].txt", "a/[**/].txt", False),
        ("x/y/z", "*", True),
    ],
)
def test_globstar_may_match_no_directory_and_nothing_else_changes(
    path: str, pattern: str, matches: bool
) -> None:
    assert file_transfer._matches(path, [pattern]) is matches


def test_the_output_glob_reads_a_deliverable_at_the_top_of_the_output(tmp_path: Path) -> None:
    root = _output_root(tmp_path)
    nested = root / "artifacts" / "output" / "notes"
    nested.mkdir()
    (nested / "n.md").write_bytes(b"n")
    limits = ReadLimits()

    result = _read_now(root, _plan_for(root, _OUTPUT, limits), limits)

    assert [p for p, _ in result.files] == [
        "artifacts/output/deliverable.md",
        "artifacts/output/notes/n.md",
    ]


def test_a_pattern_deeper_than_the_limit_costs_the_limit_not_the_pattern(tmp_path: Path) -> None:
    deep = "/".join(["segment0"] * 300 + ["*"])
    limits = ReadLimits(max_depth=4)
    script = listing_script(str(tmp_path), limits, patterns=[deep])
    assert len(script) < 2_000

    root = tmp_path / "workspace"
    (root / "/".join(["segment0"] * 6)).mkdir(parents=True)
    plan = _plan_for(root, [deep], limits)
    assert [r.reason for r in plan.refused] == ["depth_exceeded"]
