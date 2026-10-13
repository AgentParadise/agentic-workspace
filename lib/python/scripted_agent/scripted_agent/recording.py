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


@dataclass(frozen=True)
class Recording:
    """The events of one session, ready to replay, with the model rewritten."""

    events: tuple[Event, ...]
    """Everything the CLI printed except its closing ``result`` event."""
    offsets_ms: tuple[int, ...]
    """When each event was printed, relative to the start of the session."""
    result: Event | None
    """The closing ``result`` event, held back so the stub's own
    ``TASK_RESULT`` replaces the recording's as the last message."""

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
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise RecordingRefused(f"recording {path} cannot be read: {e}") from e
        try:
            header, *records = (json.loads(line) for line in lines if line.strip())
        except (json.JSONDecodeError, ValueError) as e:
            raise RecordingRefused(f"recording {path} is not JSONL with a header: {e}") from e

        recorded_version = header.get("_recording", {}).get("cli_version")
        if recorded_version != stream.cli_version:
            raise RecordingRefused(
                f"recording {path} was captured from CLI {recorded_version!r} but the "
                f"profile declares cli_version {stream.cli_version!r}"
            )
        tool_uses = sum(_tool_uses(event) for event in records)
        if tool_uses != stream.tool_uses:
            raise RecordingRefused(
                f"recording {path} has {tool_uses} tool_use events but the profile "
                f"declares tool_uses {stream.tool_uses}; the driver would misread "
                "the difference as dropped observations"
            )

        result = next((e for e in records if e.get("type") == "result"), None)
        replayed = [e for e in records if e is not result]
        return cls(
            events=tuple(_rewrite(e) for e in replayed),
            offsets_ms=tuple(int(e.get(_OFFSET, 0)) for e in replayed),
            result=_rewrite(result) if result is not None else None,
        )


def _tool_uses(event: Event) -> int:
    if event.get("type") != "assistant":
        return 0
    content = event.get("message", {}).get("content", [])
    return sum(
        1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use"
    )


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
