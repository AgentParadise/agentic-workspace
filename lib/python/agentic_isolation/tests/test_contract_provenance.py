"""The vendored EXP-V1-0006 contract is the one the Rust core is pinned to."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import tomllib

from ._apss_contract import FIXTURES, SCHEMAS

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parents[2]
PROVENANCE = SCHEMAS.parent / "PROVENANCE.md"


def _vendored_rev() -> str:
    match = re.search(r"^- Source commit: `([0-9a-f]{40})`", PROVENANCE.read_text(), re.M)
    assert match, "PROVENANCE.md must record the 40-character source commit"
    return match.group(1)


def _digests() -> dict[str, str]:
    rows = re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$", PROVENANCE.read_text(), re.M)
    return dict(rows)


def test_vendored_rev_equals_cargo_apss_pin() -> None:
    cargo = tomllib.loads((REPO / "Cargo.toml").read_text())
    pin = cargo["workspace"]["dependencies"]["apss-workspace-standard"]["rev"]
    assert _vendored_rev() == pin
    assert f"?rev={pin}#{pin}" in (REPO / "Cargo.lock").read_text()


def test_every_vendored_file_matches_its_recorded_digest() -> None:
    recorded = _digests()
    on_disk = {
        str(path.relative_to(PACKAGE)): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in (SCHEMAS, FIXTURES)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    assert on_disk == recorded
