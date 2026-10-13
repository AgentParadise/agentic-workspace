"""Schema/model parity on input shapes the vendored fixtures do not spell.

Each case alters one value in an upstream success fixture. The schema's verdict
is asserted explicitly, and the model must agree with it. Accepted documents
must also serialize back to a schema-valid document.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentic_isolation.contract import parse_document

from ._apss_contract import Json, apply_edits, load, schema_accepts

DIGEST = "sha256:" + "a" * 64

# (fixture, JSON Pointer, replacement value, schema verdict)
CASES: list[tuple[str, str, Json, bool]] = [
    # Every integer field: integral numbers spelled as floats are integers.
    ("workspace-operation/minimal.json", "/timeout_seconds", 1.0, True),
    ("workspace-operation/minimal.json", "/timeout_seconds", 1.5, False),
    ("workspace-operation/minimal.json", "/timeout_seconds", 0.0, False),
    ("workspace-operation/minimal.json", "/timeout_seconds", True, False),
    ("content-ref/empty-content.json", "/size_bytes", 0.0, True),
    ("content-ref/empty-content.json", "/size_bytes", 0.5, False),
    ("content-ref/empty-content.json", "/size_bytes", False, False),
    ("workspace-status/succeeded.json", "/exit_code", 0.0, True),
    ("workspace-status/succeeded.json", "/exit_code", 2.5, False),
    ("workspace-status/succeeded.json", "/exit_code", True, False),
    ("workspace-observation/gap-exact.json", "/gap/lost_bytes", 3.0, True),
    ("workspace-observation/gap-exact.json", "/gap/lost_bytes", 3.5, False),
    ("workspace-observation/gap-exact.json", "/gap/lost_bytes", True, False),
    ("workspace-provider/docker.json", "/observation/retention_bytes", 1024.0, True),
    ("workspace-provider/docker.json", "/observation/retention_bytes", 1.25, False),
    ("workspace-provider/docker.json", "/observation/retention_bytes", True, False),
    ("workspace-provider/docker.json", "/observation/max_page_bytes", 512.0, True),
    ("workspace-provider/docker.json", "/observation/max_page_bytes", 0.5, False),
    ("workspace-provider/docker.json", "/observation/max_page_bytes", True, False),
    # Anchored patterns, with their accepting and rejecting neighbours.
    ("content-ref/empty-content.json", "/digest", DIGEST, True),
    ("content-ref/empty-content.json", "/digest", DIGEST + "\n", True),
    ("content-ref/empty-content.json", "/digest", DIGEST + "\n\n", False),
    ("content-ref/empty-content.json", "/digest", DIGEST + "a", False),
    ("workspace-observation/running.json", "/data_b64", "YQ==", True),
    ("workspace-observation/running.json", "/data_b64", "YQ==\n", True),
    ("workspace-observation/running.json", "/data_b64", "YQ==\n\n", False),
    ("workspace-observation/running.json", "/data_b64", "YQ=", False),
    # Timestamps: `format: date-time` is an annotation, not an assertion.
    ("workspace-status/running.json", "/started_at", "yesterday", True),
    ("workspace-status/running.json", "/started_at", "0", True),
    ("workspace-status/running.json", "/started_at", "2026-10-13T09:00:00.123456789Z", True),
    ("workspace-status/succeeded.json", "/ended_at", "2026-10-13T09:05:00+02:00", True),
    ("workspace-status/succeeded.json", "/ended_at", 0, False),
]


def _model_accepts(document: Json) -> bool:
    try:
        parse_document(json.dumps(document))
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize(
    ("fixture", "pointer", "value", "verdict"),
    CASES,
    ids=[f"{f.split('/')[0]}{p}={v!r}" for f, p, v, _ in CASES],
)
def test_model_agrees_with_schema_on_altered_value(
    fixture: str, pointer: str, value: Json, verdict: bool
) -> None:
    document = apply_edits(load(f"valid/{fixture}"), [{"path": pointer, "value": value}], [])
    assert schema_accepts(document) is verdict
    assert _model_accepts(document) is verdict
    if verdict:
        dumped = json.loads(parse_document(json.dumps(document)).model_dump_json())
        assert schema_accepts(dumped)


@pytest.mark.parametrize(
    "timestamp",
    ["yesterday", "0", "2026-10-13T09:00:00.123456789Z", "2026-10-13T09:00:00+02:00"],
)
def test_timestamp_string_survives_serialization_unchanged(timestamp: str) -> None:
    document = apply_edits(
        load("valid/workspace-status/running.json"),
        [{"path": "/started_at", "value": timestamp}],
        [],
    )
    dumped = json.loads(parse_document(json.dumps(document)).model_dump_json())
    assert dumped["started_at"] == timestamp
