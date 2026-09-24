"""Workspace subagent-stop handler: observability only, never blocks.

Ported from Agentic Primitives tests/unit/claude/hooks/test_hooks.py
(TestWorkspaceSubagentStop), which AW does not carry as a suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
HANDLER = PROJECT_ROOT / "plugins" / "workspace" / "hooks" / "handlers" / "subagent-stop.py"
EVENTS_SRC = PROJECT_ROOT / "lib" / "python" / "agentic_events"


def _run(stdin: str, monkeypatch: pytest.MonkeyPatch) -> subprocess.CompletedProcess[str]:
    monkeypatch.setenv("PYTHONPATH", str(EVENTS_SRC))
    return subprocess.run(
        [sys.executable, str(HANDLER)],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.mark.parametrize(
    ("identity_fields", "expected"),
    [
        ({"agent_id": "child/native-雪"}, "child/native-雪"),
        ({"agent_id": "native-child", "subagent_id": "legacy-child"}, "native-child"),
        ({"subagent_id": "legacy-child"}, "legacy-child"),
        ({}, "unknown"),
    ],
)
def test_emits_actual_child_identity_without_copying_response(
    monkeypatch: pytest.MonkeyPatch, identity_fields: dict[str, str], expected: str
) -> None:
    event = {
        "session_id": "parent-root",
        "hook_event_name": "SubagentStop",
        "last_assistant_message": "private response must not be copied",
        **identity_fields,
    }
    result = _run(json.dumps(event), monkeypatch)
    assert result.stdout == ""
    emitted = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(emitted) == 1
    assert emitted[0]["context"]["subagent_id"] == expected
    assert "private response" not in result.stderr


@pytest.mark.parametrize("stdin", ["", "{nope", json.dumps({"session_id": "ws-sub-empty"})])
def test_never_blocks(monkeypatch: pytest.MonkeyPatch, stdin: str) -> None:
    result = _run(stdin, monkeypatch)
    assert result.returncode == 0
    assert '"decision": "block"' not in result.stdout
