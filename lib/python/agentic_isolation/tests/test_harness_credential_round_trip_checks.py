"""The S1 exit test's own checks, run without Docker (ADR-042).

`tests/integration/test_harness_credential_presentation.py` only means
something if its assertions reject contaminated requests and sandboxes. These
cases feed it synthetic reports: headers built by hand, and files planted on
disk and inventoried by the probe's real `_inventory`.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import SecretStr

from tests.integration.harness_credential_probe import _inventory
from tests.integration.test_harness_credential_presentation import (
    CLASSES,
    HOST_CANARIES,
    _assert_round_trip,
)

WIRE = "cap-s1-check-2b9e"
OTHER = "opaque-other-credential-51c0"
CANARY = HOST_CANARIES["ANTHROPIC_API_KEY"]


def _report(name: str, headers: list[list[str]], inventory: dict[str, object]) -> dict[str, object]:
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


def _clean_inventory(tmp_path: Path) -> dict[str, object]:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{}")
    return _inventory([tmp_path])


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_a_clean_report_passes(name: str, tmp_path: Path) -> None:
    _check(name, _report(name, [_auth(name, WIRE)], _clean_inventory(tmp_path)))


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_an_earlier_second_credential_under_the_auth_header_is_rejected(
    name: str, tmp_path: Path
) -> None:
    # Wrong value first, wire last: a header dict keeps only the last one.
    first = _auth(name, OTHER, CLASSES[name].auth_header.title())
    report = _report(name, [first, _auth(name, WIRE)], _clean_inventory(tmp_path))

    with pytest.raises(AssertionError):
        _check(name, report)


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_a_repeated_auth_header_is_rejected_even_when_the_values_agree(
    name: str, tmp_path: Path
) -> None:
    report = _report(name, [_auth(name, WIRE), _auth(name, WIRE)], _clean_inventory(tmp_path))

    with pytest.raises(AssertionError):
        _check(name, report)


def _plant_second_file(tmp_path: Path) -> None:
    (tmp_path / "home" / "b-second.json").write_text(f'{{"key": "{CANARY}"}}')


def _plant_tail_past_a_mebibyte(tmp_path: Path) -> None:
    (tmp_path / "home" / "big.log").write_text("x" * (2 << 20) + CANARY)


def _plant_outside_home(tmp_path: Path) -> None:
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "scratch.txt").write_text(CANARY)


def _plant_symlink_out_of_the_roots(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "leak.txt").write_text(CANARY)
    (tmp_path / "home" / "link.txt").symlink_to(outside / "leak.txt")


def _plant_symlink_loop(tmp_path: Path) -> None:
    (tmp_path / "home" / "loop").symlink_to(tmp_path / "home" / "loop")


def _plant_unreadable_file(tmp_path: Path) -> None:
    locked = tmp_path / "home" / "locked.txt"
    locked.write_text("anything")
    locked.chmod(0)


@pytest.mark.parametrize("name", sorted(CLASSES))
@pytest.mark.parametrize(
    "plant",
    [
        _plant_second_file,
        _plant_tail_past_a_mebibyte,
        _plant_outside_home,
        _plant_symlink_out_of_the_roots,
        _plant_symlink_loop,
        _plant_unreadable_file,
    ],
)
def test_contaminated_or_unreadable_sandbox_files_are_rejected(
    name: str, plant: Callable[[Path], None], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "a-first.json").write_text("{}")
    plant(tmp_path)
    if plant is _plant_unreadable_file and os.geteuid() == 0:
        pytest.skip("running as root; mode 0 does not stop reads")
    report = _report(name, [_auth(name, WIRE)], _inventory([tmp_path]))

    with pytest.raises(AssertionError):
        _check(name, report)
