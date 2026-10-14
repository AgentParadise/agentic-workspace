"""The S1 exit test's own checks, run without Docker (ADR-042).

`tests/integration/test_harness_credential_presentation.py` only means
something if its assertions reject contaminated requests and sandboxes. These
cases feed it synthetic reports: headers built by hand, and sandbox
filesystems built as the tar streams `docker export` and `docker cp` produce,
read by the host scanner's real `scan_tar`.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from collections.abc import Callable

import pytest
from pydantic import SecretStr

from tests.integration.harness_credential_scan import Inventory, scan_tar
from tests.integration.test_harness_credential_presentation import (
    CLASSES,
    HOST_CANARIES,
    _assert_round_trip,
)

WIRE = "cap-s1-check-2b9e"
OTHER = "opaque-other-credential-51c0"
CANARY = HOST_CANARIES["ANTHROPIC_API_KEY"]
NEEDLES = {"wire": WIRE, **HOST_CANARIES}
WORK = "tmp/credential-probe-x1"
# The release image's build leftover: root-owned, 0600, unreadable to the
# agent user the CLI runs as.
ROOT_ONLY = "tmp/node-compile-cache/v22/cache-0"
# (name, bytes, uid, mode) as the daemon streams them.
Member = tuple[str, bytes, int, int]


def _tar(members: list[Member]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data, uid, mode in members:
            info = tarfile.TarInfo(name)
            info.size, info.uid, info.gid, info.mode = len(data), uid, uid, mode
            archive.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def _sandbox(extra: tuple[Member, ...] = ()) -> list[Member]:
    return [
        (f"{WORK}/home/a-first.json", b"{}", 1000, 0o600),
        (ROOT_ONLY, b"compiled bytes", 0, 0o600),
        *extra,
    ]


def _scan(members: list[Member], changed: set[str] | None = None) -> dict[str, object]:
    inventory = Inventory()
    written = changed if changed is not None else {"/" + m[0] for m in members if m[2] != 0}
    scan_tar(_tar(members), NEEDLES, written.__contains__, inventory)
    return inventory.as_report()


def _report(
    name: str,
    headers: list[list[str]],
    inventory: dict[str, object],
    cmdlines: list[str] | None = None,
) -> dict[str, object]:
    harness = CLASSES[name]
    return {
        "returncode": 0,
        "stderr": "",
        "requests": [
            {
                "method": "POST",
                "path": harness.model_path,
                "headers": [["Content-Type", "application/json"], *headers],
                "body": "{}",
            }
        ],
        "env": {"HOME": "/home/probe"},
        "cmdlines": cmdlines if cmdlines is not None else [" ".join(harness.argv)],
        **inventory,
    }


def _auth(name: str, value: str, spelling: str | None = None) -> list[str]:
    harness = CLASSES[name]
    return [spelling or harness.auth_header, harness.auth_prefix + value]


def _check(name: str, report: dict[str, object]) -> None:
    harness = CLASSES[name]
    presented = harness.presenter.present(SecretStr(WIRE), "http://127.0.0.1:1")
    stored_hash = hashlib.sha256(WIRE.encode()).hexdigest()
    _assert_round_trip(harness, presented, stored_hash, WIRE, report)


def _clean_inventory() -> dict[str, object]:
    return _scan(_sandbox())


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_a_clean_report_passes(name: str) -> None:
    _check(name, _report(name, [_auth(name, WIRE)], _clean_inventory()))


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_an_earlier_second_credential_under_the_auth_header_is_rejected(name: str) -> None:
    # Wrong value first, wire last: a header dict keeps only the last one.
    first = _auth(name, OTHER, CLASSES[name].auth_header.title())
    report = _report(name, [first, _auth(name, WIRE)], _clean_inventory())

    with pytest.raises(AssertionError):
        _check(name, report)


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_a_repeated_auth_header_is_rejected_even_when_the_values_agree(name: str) -> None:
    report = _report(name, [_auth(name, WIRE), _auth(name, WIRE)], _clean_inventory())

    with pytest.raises(AssertionError):
        _check(name, report)


def test_a_root_only_file_is_read_in_full_by_the_host_scan() -> None:
    report = _scan(_sandbox())

    assert report["unscanned"] == {}
    assert report["hits"] == {}
    tainted = _scan([(ROOT_ONLY, b"x" * (2 << 20) + CANARY.encode(), 0, 0o600)])
    assert tainted["hits"] == {"/" + ROOT_ONLY: ["ANTHROPIC_API_KEY"]}


def test_a_stream_cut_inside_a_file_fails_the_scan() -> None:
    # A cut at a header boundary reads as a clean end; scan_container
    # catches that through the daemon's exit status.
    whole = _tar(_sandbox(((f"{WORK}/home/b.json", b"y" * 4096, 1000, 0o600),))).getvalue()

    with pytest.raises(tarfile.TarError):
        scan_tar(io.BytesIO(whole[:4000]), NEEDLES, lambda _p: True, Inventory())


def _root_only_canary() -> dict[str, object]:
    return _scan(_sandbox((("tmp/node-compile-cache/v22/cache-1", CANARY.encode(), 0, 0o600),)))


def _second_file() -> dict[str, object]:
    return _scan(
        _sandbox(((f"{WORK}/home/b-second.json", f'{{"k": "{CANARY}"}}'.encode(), 1000, 0o600),))
    )


def _tail_past_a_mebibyte() -> dict[str, object]:
    return _scan(
        _sandbox(((f"{WORK}/home/big.log", b"x" * (2 << 20) + CANARY.encode(), 1000, 0o644),))
    )


def _outside_home_and_tmpdir() -> dict[str, object]:
    # Writable, but not under the probe's work root.
    return _scan(_sandbox((("var/tmp/scratch.txt", CANARY.encode(), 1000, 0o644),)))


def _wire_in_an_image_file() -> dict[str, object]:
    return _scan(_sandbox((("etc/agent.conf", WIRE.encode(), 0, 0o600),)))


def _credential_shape_written_outside_home() -> dict[str, object]:
    shaped = b"sk-" + b"Q" * 32
    return _scan(_sandbox((("home/agent/.cache/x.json", shaped, 1000, 0o644),)))


def _writable_mount() -> dict[str, object]:
    inventory = Inventory()
    scan_tar(_tar(_sandbox()), NEEDLES, lambda _p: False, inventory)
    scan_tar(
        _tar([("data/notes", CANARY.encode(), 1000, 0o644)]),
        NEEDLES,
        lambda _p: True,
        inventory,
        root="/mnt",
    )
    return inventory.as_report()


@pytest.mark.parametrize("name", sorted(CLASSES))
@pytest.mark.parametrize(
    "contaminated",
    [
        _root_only_canary,
        _second_file,
        _tail_past_a_mebibyte,
        _outside_home_and_tmpdir,
        _wire_in_an_image_file,
        _credential_shape_written_outside_home,
        _writable_mount,
    ],
)
def test_contaminated_sandbox_files_are_rejected(
    name: str, contaminated: Callable[[], dict[str, object]]
) -> None:
    report = _report(name, [_auth(name, WIRE)], contaminated())

    with pytest.raises(AssertionError):
        _check(name, report)


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_an_unscanned_location_is_rejected(name: str) -> None:
    inventory = Inventory()
    inventory.unscanned["/run/scratch"] = "tmpfs is gone once the container exits"
    report = _report(name, [_auth(name, WIRE)], inventory.as_report())

    with pytest.raises(AssertionError):
        _check(name, report)


@pytest.mark.parametrize("name", sorted(CLASSES))
@pytest.mark.parametrize("cmdlines", [[], ["python3 /probe/probe.py /probe/spec.json"]])
def test_a_missing_cli_command_line_is_rejected(name: str, cmdlines: list[str]) -> None:
    report = _report(name, [_auth(name, WIRE)], _clean_inventory(), cmdlines)

    with pytest.raises(AssertionError):
        _check(name, report)


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_a_canary_on_a_command_line_is_rejected(name: str) -> None:
    cmdlines = [" ".join(CLASSES[name].argv), f"helper --key {CANARY}"]
    report = _report(name, [_auth(name, WIRE)], _clean_inventory(), cmdlines)

    with pytest.raises(AssertionError):
        _check(name, report)
