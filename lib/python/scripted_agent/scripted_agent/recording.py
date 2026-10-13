"""A recorded claude session, checked against its phase and rewritten for replay.

The checks live here because the recording is the one input the driver cannot
see: it declares ``cli_version`` and ``tool_uses`` per phase, and the replayer
is the only place those declarations meet the file (#44 contract points 4, 5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from scripted_agent.errors import RecordingRefused
from scripted_agent.profile import SCRIPTED_AGENT_MODEL, ScriptedStream

Event = dict[str, Any]

_OFFSET: Final = "_offset_ms"
_TERMINAL: Final = "result"
SUBAGENT_TOOL_NAMES: Final = frozenset({"Task", "Agent"})
"""Tools the session observer records as a subagent start, not a tool execution
(``agentic_isolation``'s ``EventParser``), so they cannot be counted in ``tool_uses``."""


@dataclass(frozen=True)
class Recording:
    """The events of one session, ready to replay, with the model rewritten."""

    events: tuple[Event, ...]
    """Everything the CLI printed except its closing ``result`` event."""
    offsets_ms: tuple[int, ...]
    """When each event was printed, relative to the start of the session."""
    result: Event
    """The closing ``result`` event, held back so the stub's own
    ``TASK_RESULT`` replaces the recording's as the last message."""
    result_offset_ms: int
    """When the closing event was printed; the stub's verdict is never sooner."""

    @classmethod
    def load(cls, stream: ScriptedStream) -> Recording:
        """Read ``stream.recording`` and refuse it unless it is the stream declared."""
        if stream.harness != "claude":
            raise RecordingRefused(
                f"harness {stream.harness!r} cannot be replayed yet: no {stream.harness} "
                "recording exists to define its tool-use events, so the declared "
                "tool_uses could not be checked. Use a claude recording."
            )
        path = Path(stream.recording)
        try:
            lines = path.read_bytes().decode("utf-8").splitlines()
        except OSError as e:
            raise RecordingRefused(f"recording {path} cannot be read: {e}") from e
        except UnicodeDecodeError as e:
            raise RecordingRefused(f"recording {path} is not UTF-8: {e}") from e
        try:
            rows = [json.loads(line) for line in lines if line.strip()]
        except json.JSONDecodeError as e:
            raise RecordingRefused(f"recording {path} is not JSONL: {e}") from e
        header, records = _structure(path, rows)

        recorded_version = header["_recording"].get("cli_version")
        if recorded_version != stream.cli_version:
            raise RecordingRefused(
                f"recording {path} was captured from CLI {recorded_version!r} but the "
                f"profile declares cli_version {stream.cli_version!r}"
            )
        subagents = sorted(
            SUBAGENT_TOOL_NAMES & {str(b.get("name")) for e in records for b in _tool_blocks(e)}
        )
        if subagents:
            raise RecordingRefused(
                f"recording {path} launches subagents ({', '.join(subagents)}); the session "
                "observer records those as subagent starts, not tool executions, so no "
                "tool_uses count can match what the driver reads back. Use a recording "
                "without subagents."
            )
        tool_uses = sum(len(_tool_blocks(event)) for event in records)
        if tool_uses != stream.tool_uses:
            raise RecordingRefused(
                f"recording {path} has {tool_uses} tool_use events but the profile "
                f"declares tool_uses {stream.tool_uses}; the driver would misread "
                "the difference as dropped observations"
            )

        *replayed, result = records
        return cls(
            events=tuple(_rewrite(e) for e in replayed),
            offsets_ms=tuple(_offset(e) for e in replayed),
            result=_rewrite(result),
            result_offset_ms=_offset(result),
        )


def _structure(path: Path, rows: list[Any]) -> tuple[Event, list[Event]]:
    """The header and the events, refused unless every row is an object, the header
    carries ``_recording``, and exactly one ``result`` closes the session."""
    if not rows or not isinstance(rows[0], dict) or not isinstance(rows[0].get("_recording"), dict):
        raise RecordingRefused(
            f'recording {path} must open with a {{"_recording": {{...}}}} header object'
        )
    header, *records = rows
    for number, row in enumerate(records, start=2):
        if not isinstance(row, dict):
            raise RecordingRefused(f"recording {path} line {number} is not a JSON object")
    terminals = [n for n, e in enumerate(records) if e.get("type") == _TERMINAL]
    if terminals != [len(records) - 1]:
        raise RecordingRefused(
            f"recording {path} must end with exactly one {_TERMINAL!r} event; found "
            f"{len(terminals)} at event(s) {[n + 1 for n in terminals]} of {len(records)}"
        )
    return header, records


def _offset(event: Event) -> int:
    return int(event.get(_OFFSET, 0))


def _tool_blocks(event: Event) -> list[Event]:
    if event.get("type") != "assistant":
        return []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _rewrite(event: Event) -> Event:
    """``event`` as the CLI would print it, every model reported as the scripted one."""
    return {k: _rewrite_value(k, v) for k, v in event.items() if k != _OFFSET}


def _rewrite_value(key: str, value: Any) -> Any:
    if key == "model" and isinstance(value, str):
        return SCRIPTED_AGENT_MODEL
    if key == "modelUsage" and isinstance(value, dict):
        return {SCRIPTED_AGENT_MODEL: _merge_usage(value.values())}
    if isinstance(value, dict):
        return {k: _rewrite_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_value("", v) for v in value]
    return value


def _merge_usage(per_model: Any) -> dict[str, Any]:
    """One model's usage from several: counts add up, the context window does not."""
    merged: dict[str, Any] = {}
    for usage in per_model:
        for k, v in usage.items():
            if k == "contextWindow":
                merged[k] = max(merged.get(k, 0), v)
            elif isinstance(v, int | float):
                merged[k] = merged.get(k, 0) + v
            else:
                merged.setdefault(k, v)
    return merged
