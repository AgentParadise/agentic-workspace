"""Replayed streams as the session observer reads them back (agentic_isolation's EventParser).

The driver holds the server's stored tool-start count for a phase to the profile's
``tool_uses``, so every recording the replayer accepts must yield exactly that many
``tool_execution_started`` observations, and every one it cannot must be refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agentic_isolation.providers.claude_cli import event_parser
from agentic_isolation.providers.claude_cli.event_parser import EventParser

from scripted_agent.errors import RecordingRefused
from scripted_agent.profile import SCRIPTED_AGENT_MODEL, ScriptedStream
from scripted_agent.recording import SUBAGENT_TOOL_NAMES, Recording
from tests.test_replay import RECORDING, _stream

RECORDINGS = sorted(RECORDING.parent.rglob("*.jsonl"))


def _declared(path: Path) -> tuple[str, int]:
    """The CLI version and raw tool_use block count a profile would declare for ``path``."""
    header, *rows = (json.loads(line) for line in path.read_text().splitlines() if line.strip())
    blocks = sum(
        1
        for row in rows
        if row.get("type") == "assistant"
        for block in row["message"]["content"]
        if block.get("type") == "tool_use"
    )
    return header["_recording"]["cli_version"], blocks


def _is_replay_recording(path: Path) -> bool:
    first = path.read_text().splitlines()[0]
    return "_recording" in json.loads(first) and _declared(path)[1] > 0


def test_subagent_tools_are_the_observers() -> None:
    assert SUBAGENT_TOOL_NAMES == event_parser.SUBAGENT_TOOL_NAMES


@pytest.mark.parametrize(
    "path", [p for p in RECORDINGS if _is_replay_recording(p)], ids=lambda p: p.name
)
def test_every_accepted_recording_is_observed_as_declared(path: Path) -> None:
    cli_version, tool_uses = _declared(path)
    stream = ScriptedStream.model_validate(
        _stream(recording=str(path), cli_version=cli_version, tool_uses=tool_uses)
    )
    try:
        recording = Recording.load(stream)
    except RecordingRefused as e:
        assert "launches subagents" in str(e)
        return

    parser = EventParser(session_id="fresh")
    observed: list[Any] = [
        row
        for event in [*recording.events, recording.result]
        for row in parser.parse_line(json.dumps(event))
    ]
    starts = [row for row in observed if row.event_type.value == "tool_execution_started"]
    assert len(starts) == stream.tool_uses
    assert all(e["model"] == SCRIPTED_AGENT_MODEL for e in recording.events if "model" in e)


def test_the_subagent_recording_is_refused_not_miscounted() -> None:
    path = RECORDING.parent / "v2.0.76_claude-haiku-4-5_subagent-concurrent.jsonl"
    cli_version, tool_uses = _declared(path)
    stream = ScriptedStream.model_validate(
        _stream(recording=str(path), cli_version=cli_version, tool_uses=tool_uses)
    )
    with pytest.raises(RecordingRefused, match=r"launches subagents \(Task\)"):
        Recording.load(stream)
