"""The ported profile model is the one syntropic137's driver writes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from scripted_agent.profile import ScriptedAgentProfile

CONTRACT = Path(__file__).resolve().parents[1] / "scripted_agent" / "contract"
SCHEMA = CONTRACT / "scripted_agent_profile.schema.json"
PROVENANCE = CONTRACT / "PROVENANCE.md"


def test_vendored_schema_matches_its_recorded_digest() -> None:
    recorded = re.search(
        r"^\| `scripted_agent/contract/scripted_agent_profile\.schema\.json` "
        r"\| `([0-9a-f]{64})` \|$",
        PROVENANCE.read_text(),
        re.M,
    )
    assert recorded, "PROVENANCE.md must record the vendored schema's SHA-256"
    assert hashlib.sha256(SCHEMA.read_bytes()).hexdigest() == recorded[1]


def test_provenance_pins_a_full_source_commit() -> None:
    assert re.search(r"^- Source commit: `[0-9a-f]{40}`", PROVENANCE.read_text(), re.M)


def test_ported_model_generates_the_vendored_schema() -> None:
    # The same check upstream runs on its own model: if they match here, the
    # replayer accepts exactly the documents the driver can write.
    generated = (
        json.dumps(ScriptedAgentProfile.model_json_schema(), indent=2, sort_keys=True) + "\n"
    )
    assert SCHEMA.read_text(encoding="utf-8") == generated
