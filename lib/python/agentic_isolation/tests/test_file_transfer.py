"""The provider-neutral decisions behind `read_files` (#100).

The walk's records and the archive come from an untrusted sandbox, so these
feed both hand-built answers the conformance suite cannot provoke: a file
swapped or grown between the listing and the archive, and a sandbox that
answers outside the contract.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from agentic_isolation import FileRefusal, ReadFilesError, ReadFilesResult, ReadLimits
from agentic_isolation.providers.file_transfer import ReadPlan, collect, plan_reads


def _record(kind: str, path: str, *, nlink: int = 1, size: int = 1, target: str = "") -> bytes:
    depth = path.count("/") + 1
    return f"{kind} {nlink} {size} {depth}\0{path}\0{target}\0".encode()


def _archive(*members: tuple[str, bytes | str]) -> bytes:
    """A tar of regular files (bytes) and symlinks (str targets)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, body in members:
            info = tarfile.TarInfo(f"./{name}")
            if isinstance(body, str):
                info.type, info.linkname = tarfile.SYMTYPE, body
                archive.addfile(info)
            else:
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_limits_must_be_positive_ints(value: object) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        ReadLimits(max_entries=value)  # type: ignore[arg-type]  # the bad value is the test


def test_the_plan_looks_at_no_more_records_than_the_entry_limit_allows() -> None:
    listing = b"".join(_record("f", f"f{n}") for n in range(5))

    plan = plan_reads(listing, ["*"], ReadLimits(max_entries=2), "/workspace")

    assert plan == ReadPlan(
        reads=(("f0", 1), ("f1", 1)), refused=(FileRefusal("f2", "entry_limit_exceeded"),)
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


def test_a_file_swapped_for_a_symlink_after_the_listing_is_refused() -> None:
    plan = ReadPlan(reads=(("a", 1), ("b", 1)), refused=())

    got = collect(_archive(("a", "/etc/shadow"), ("b", "a")), plan, ReadLimits(), "/workspace")

    assert got == ReadFilesResult(
        files=(),
        refused=(FileRefusal("a", "escaping_symlink"), FileRefusal("b", "symlink")),
    )


def test_a_file_grown_after_the_listing_is_held_to_the_limits() -> None:
    plan = ReadPlan(reads=(("a", 1), ("b", 1)), refused=())
    limits = ReadLimits(max_file_bytes=4, max_total_bytes=5)

    got = collect(_archive(("a", b"12345"), ("b", b"1234")), plan, limits, "/workspace")

    assert got == ReadFilesResult(
        files=(("b", b"1234"),), refused=(FileRefusal("a", "file_too_large"),)
    )


@pytest.mark.parametrize(
    "members", [(("a", b"x"), ("other", b"y")), ()], ids=["unrequested", "missing"]
)
def test_an_archive_that_does_not_answer_the_plan_is_an_error(
    members: tuple[tuple[str, bytes], ...],
) -> None:
    plan = ReadPlan(reads=(("a", 1),), refused=())

    with pytest.raises(ReadFilesError):
        collect(_archive(*members), plan, ReadLimits(), "/workspace")
