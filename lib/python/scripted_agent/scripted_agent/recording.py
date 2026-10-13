"""A recorded claude or codex session, checked against its phase and rewritten for replay.

The checks live here because the recording is the one input the driver cannot
see: it declares ``cli_version`` and ``tool_uses`` per phase, and the replayer
is the only place those declarations meet the file (#44 contract points 4, 5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from scripted_agent.errors import RecordingRefused
from scripted_agent.profile import SCRIPTED_AGENT_MODEL, ScriptedStream

Event = dict[str, Any]
Harness = Literal["claude", "codex"]

_OFFSET: Final = "_offset_ms"
TERMINALS: Final[dict[Harness, str]] = {"claude": "result", "codex": "turn.completed"}
"""The event each harness closes its session with: claude stream-json's ``result``,
``codex exec --json``'s ``turn.completed``."""
CODEX_TOOL_ITEM_TYPES: Final = frozenset({"command_execution", "file_change"})
"""The ``item.started`` item types syntropic137's ``CodexStreamProcessor`` records as
a tool start (``_handle_item_started`` @ 1017ec7b); every other item is not a tool."""
CODEX_VERDICT_ITEM_ID: Final = "scripted_agent_verdict"
TASK_RESULT_MARKER: Final = "TASK_RESULT:"
"""What syntropic137's ``VerdictReader`` (``phase_verdict.py`` @ 1017ec7b) looks for in
every message the agent says. It settles all of them by precedence, FAILURE over
SUCCESS, not by which came last, so a recorded report anywhere in the stream would
outrank the profile's verdict whenever the recorded run failed."""
CLAUDE_SUCCESS: Final = {"is_error": False, "subtype": "success"}
"""The status the profile's closing ``result`` always carries. syntropic137's
``_handle_result_event`` (``EventStreamProcessor.py`` @ 1017ec7b) reads an
``is_error`` result as the harness's own failure text and never hands it to
``VerdictReader``, so a recorded harness error would otherwise swallow the verdict."""
WITHHELD_REPORT: Final = "[scripted-agent replay: the recorded run's own verdict is withheld]"
"""What a recorded message carrying a report says instead, so the only report the
consumer reads is the one ``closing`` writes."""
SUBAGENT_TOOL_NAMES: Final = frozenset({"Task", "Agent"})
"""Tools the session observer records as a subagent start, not a tool execution
(``agentic_isolation``'s ``EventParser``), so they cannot be counted in ``tool_uses``."""


@dataclass(frozen=True)
class Recording:
    """The events of one session, ready to replay, with the model rewritten."""

    harness: Harness
    events: tuple[Event, ...]
    """Everything the CLI printed except its closing event."""
    offsets_ms: tuple[int, ...]
    """When each event was printed, relative to the start of the session."""
    result: Event
    """The closing event, held back so the stub's own ``TASK_RESULT`` is the
    last message (see ``closing``)."""
    result_offset_ms: int
    """When the closing event was printed; the stub's verdict is never sooner."""

    @classmethod
    def load(cls, stream: ScriptedStream) -> Recording:
        """Read ``stream.recording`` and refuse it unless it is the stream declared."""
        harness = stream.harness
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
        header, records = _structure(path, rows, TERMINALS[harness])

        recorded_version = header["_recording"].get("cli_version")
        if recorded_version != stream.cli_version:
            raise RecordingRefused(
                f"recording {path} was captured from CLI {recorded_version!r} but the "
                f"profile declares cli_version {stream.cli_version!r}"
            )
        subagents = sorted(
            SUBAGENT_TOOL_NAMES
            & {str(b.get("name")) for e in records for b in _tool_blocks(harness, e)}
        )
        if subagents:
            raise RecordingRefused(
                f"recording {path} launches subagents ({', '.join(subagents)}); the session "
                "observer records those as subagent starts, not tool executions, so no "
                "tool_uses count can match what the driver reads back. Use a recording "
                "without subagents."
            )
        tool_uses = sum(len(_tool_blocks(harness, event)) for event in records)
        if tool_uses != stream.tool_uses:
            raise RecordingRefused(
                f"recording {path} has {tool_uses} tool_use events but the profile "
                f"declares tool_uses {stream.tool_uses}; the driver would misread "
                "the difference as dropped observations"
            )

        *replayed, result = records
        events = [_withhold_report(harness, _rewrite(e)) for e in replayed]
        if harness == "codex" and events:
            # Codex names no model on stdout; its consumer takes the first top-level
            # ``model`` on the stream before falling back to the rollout on disk.
            events[0] = {**events[0], "model": SCRIPTED_AGENT_MODEL}
        return cls(
            harness=harness,
            events=tuple(events),
            offsets_ms=tuple(_offset(e) for e in replayed),
            result=_rewrite(result),
            result_offset_ms=_offset(result),
        )

    def closing(self, verdict: str) -> tuple[Event, ...]:
        """The events that end the replay, carrying ``verdict`` where the consumer reads
        it: claude's ``result`` field, or a last codex ``agent_message`` before
        ``turn.completed``. The recorded run's harness status is history, not the
        profile's: claude's closing result always reports success (``CLAUDE_SUCCESS``)."""
        if self.harness == "claude":
            return ({**self.result, **CLAUDE_SUCCESS, "result": verdict},)
        message = {"id": CODEX_VERDICT_ITEM_ID, "type": "agent_message", "text": verdict}
        return ({"type": "item.completed", "item": message}, self.result)


def _structure(path: Path, rows: list[Any], terminal: str) -> tuple[Event, list[Event]]:
    """The header and the events, refused unless every row is an object, the header
    carries ``_recording``, and exactly one ``terminal`` event closes the session."""
    if not rows or not isinstance(rows[0], dict) or not isinstance(rows[0].get("_recording"), dict):
        raise RecordingRefused(
            f'recording {path} must open with a {{"_recording": {{...}}}} header object'
        )
    header, *records = rows
    for number, row in enumerate(records, start=2):
        if not isinstance(row, dict):
            raise RecordingRefused(f"recording {path} line {number} is not a JSON object")
    terminals = [n for n, e in enumerate(records) if e.get("type") == terminal]
    if terminals != [len(records) - 1]:
        raise RecordingRefused(
            f"recording {path} must end with exactly one {terminal!r} event; found "
            f"{len(terminals)} at event(s) {[n + 1 for n in terminals]} of {len(records)}"
        )
    return header, records


def _offset(event: Event) -> int:
    return int(event.get(_OFFSET, 0))


def _tool_blocks(harness: Harness, event: Event) -> list[Event]:
    """The tool starts ``event`` carries, as the harness's consumer counts them."""
    if harness == "codex":
        item = event.get("item")
        if event.get("type") == "item.started" and isinstance(item, dict):
            return [item] if item.get("type") in CODEX_TOOL_ITEM_TYPES else []
        return []
    if event.get("type") != "assistant":
        return []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _withhold_report(harness: Harness, event: Event) -> Event:
    """``event`` with every message its consumer reads for a verdict cleared of the
    recording's own ``TASK_RESULT``: codex ``agent_message`` items, claude assistant
    ``text`` blocks. Everything else, including the event itself, is kept."""
    if harness == "codex":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and _reports(item):
            return {**event, "item": {**item, "text": WITHHELD_REPORT}}
        return event
    message = event.get("message")
    if event.get("type") != "assistant" or not isinstance(message, dict):
        return event
    content = message.get("content")
    if not isinstance(content, list):
        return event
    blocks = [
        {**b, "text": WITHHELD_REPORT}
        if isinstance(b, dict) and b.get("type") == "text" and _reports(b)
        else b
        for b in content
    ]
    return {**event, "message": {**message, "content": blocks}}


def _reports(said: Event) -> bool:
    return TASK_RESULT_MARKER in str(said.get("text", ""))


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
