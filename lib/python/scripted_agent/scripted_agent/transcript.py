"""The native transcript a real harness would leave on disk, written from the replay.

A real ``claude -p`` or ``codex exec`` writes its session to disk as it runs,
and that file, not stdout, is what the session-store capability captures
(``~/.claude/projects`` and ``~/.codex/sessions`` are linked into its spool
partition) and what ``agentic_isolation``'s transcript sources harvest. A
replay that wrote nothing there would leave a remote run with no transcript to
archive (syntropic137#1918, blocker B4).

The rows are the ones ``agentic_isolation`` already reads, never a new format:

- **claude**: ``$CLAUDE_CONFIG_DIR/projects/<cwd slug>/<session id>.jsonl``
  (``CLAUDE_CONFIG_DIR`` defaults to ``~/.claude``). One row per replayed
  ``user``/``assistant`` event carrying its ``message`` with the native
  envelope (``sessionId``, ``uuid``, ``parentUuid``, ``isSidechain``, ``cwd``,
  ``version``, ``timestamp``), as ``ClaudeTranscriptSource``,
  ``ClaudeNativeEvidenceReader`` and the claude conversation preview read
  them; the closing ``result`` becomes the final assistant text row, which is
  where a real transcript carries the last answer.
- **codex**: ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl``
  (``CODEX_HOME`` defaults to ``~/.codex``). A ``session_meta`` header,
  ``turn_context`` naming the model, then ``event_msg`` rows in the
  codex-cli 0.160.1 shapes pinned by
  ``agentic_isolation/tests/fixtures/codex_rollout_0.160.1.jsonl``:
  ``task_started``, ``item_completed`` (``AgentMessage``,
  ``CommandExecution``), ``task_complete``, and a ``response_item`` message
  per agent message. Other ``codex exec --json`` item kinds get no row.

Every model is the scripted one, as on stdout. Rows are written as the replay
prints them, stamped with the time they were printed, as a real harness writes.
"""

from __future__ import annotations

import json
import re
import shlex
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from scripted_agent.errors import TranscriptFailed
from scripted_agent.profile import SCRIPTED_AGENT_MODEL

Event = Mapping[str, Any]
Clock = Callable[[], datetime]

_SLUG: Final = re.compile(r"[^A-Za-z0-9]")


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def _home(environ: Mapping[str, str]) -> Path:
    """``$HOME`` from the phase's own environment, never the password database:
    a harness writes under the HOME it was started with."""
    home = environ.get("HOME")
    if not home:
        raise TranscriptFailed("HOME is not set; there is nowhere to write the transcript")
    return Path(home)


def claude_transcript_path(environ: Mapping[str, str], cwd: Path, session_id: str) -> Path:
    """Where Claude Code writes a session started in ``cwd``."""
    config = environ.get("CLAUDE_CONFIG_DIR") or str(_home(environ) / ".claude")
    return Path(config) / "projects" / _SLUG.sub("-", str(cwd)) / f"{session_id}.jsonl"


def codex_rollout_path(environ: Mapping[str, str], session_id: str, at: datetime) -> Path:
    """Where codex writes a session's rollout."""
    root = Path(environ.get("CODEX_HOME") or str(_home(environ) / ".codex")) / "sessions"
    at = at.astimezone(UTC)
    name = f"rollout-{at.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
    return root / at.strftime("%Y") / at.strftime("%m") / at.strftime("%d") / name


def now() -> datetime:
    return datetime.now(UTC)


class Transcript:
    """One session's native transcript, written as the replay prints it.

    Opened (exclusive create, so a replay never overwrites another session)
    before the first event is printed, so a phase with nowhere to write fails
    before it says anything. Each printed event is appended and flushed as it
    goes, with the time it was printed, so a phase that fails part way leaves
    the partial session a real harness would. The replayer records the closing
    events here BEFORE printing them, so the stream never reports success for
    a session whose transcript was not written.
    """

    def __init__(
        self,
        harness: str,
        session_id: str,
        environ: Mapping[str, str],
        cwd: Path,
        version: str,
        clock: Clock = now,
    ) -> None:
        self.harness = harness
        self.session_id = session_id
        self.cwd = cwd
        self.version = version
        self._clock = clock
        self._started = clock()
        self._ordinal = 0
        self._parent: str | None = None
        self._turn_id = str(uuid.uuid4())
        self._last_message: str | None = None
        if harness == "claude":
            self.path = claude_transcript_path(environ, cwd, session_id)
        else:
            self.path = codex_rollout_path(environ, session_id, self._started)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("x", encoding="utf-8")
        except OSError as e:
            raise self._failed(e) from e
        if harness == "codex":
            self._codex_header()

    def _failed(self, e: OSError) -> TranscriptFailed:
        return TranscriptFailed(f"{self.harness} transcript {self.path} cannot be written: {e}")

    def _write(self, row: Mapping[str, Any]) -> None:
        try:
            self._file.write(json.dumps(row) + "\n")
            self._file.flush()
        except OSError as e:
            raise self._failed(e) from e

    def add(self, event: Event) -> None:
        """Record one event the replay printed (or is about to print)."""
        if self.harness == "claude":
            self._claude(event)
        else:
            self._codex(event)

    def close(self) -> Path:
        """Finish the session (codex records ``task_complete``) and close the file."""
        if self.harness == "codex":
            done = self._clock()
            self._codex_row(
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": self._turn_id,
                    "last_agent_message": self._last_message,
                    "started_at": int(self._started.timestamp()),
                    "completed_at": int(done.timestamp()),
                    "duration_ms": int((done - self._started).total_seconds() * 1000),
                },
            )
        try:
            self._file.close()
        except OSError as e:
            raise self._failed(e) from e
        return self.path

    def abandon(self) -> None:
        """Close what was written so far, as a harness that died part way leaves it."""
        try:
            self._file.close()
        except OSError:
            pass

    # -- claude ------------------------------------------------------------

    def _claude(self, event: Event) -> None:
        kind = event.get("type")
        message = event.get("message")
        if kind in ("user", "assistant") and isinstance(message, Mapping):
            extra = {}
            if "tool_use_result" in event:
                extra["toolUseResult"] = event["tool_use_result"]
            self._claude_row(kind, message, str(event.get("uuid") or uuid.uuid4()), extra)
        elif kind == "result" and isinstance(event.get("result"), str):
            # A real transcript carries the last answer as an assistant row;
            # stream-json's ``result`` repeats it.
            message = {
                "model": SCRIPTED_AGENT_MODEL,
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": event["result"]}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
            }
            self._claude_row("assistant", message, str(uuid.uuid4()), {})

    def _claude_row(
        self, kind: str, message: Mapping[str, Any], row_uuid: str, extra: Mapping[str, Any]
    ) -> None:
        self._write(
            {
                "parentUuid": self._parent,
                "isSidechain": False,
                "userType": "external",
                "cwd": str(self.cwd),
                "sessionId": self.session_id,
                "version": self.version,
                "type": kind,
                "message": dict(message),
                "uuid": row_uuid,
                "timestamp": _iso(self._clock()),
                **extra,
            }
        )
        self._parent = row_uuid

    # -- codex -------------------------------------------------------------

    def _codex_row(self, kind: str, payload: Mapping[str, Any]) -> None:
        self._write(
            {
                "timestamp": _iso(self._clock()),
                "ordinal": self._ordinal,
                "type": kind,
                "payload": dict(payload),
            }
        )
        self._ordinal += 1

    def _codex_header(self) -> None:
        self._codex_row(
            "session_meta",
            {
                "session_id": self.session_id,
                "id": self.session_id,
                "timestamp": _iso(self._started),
                "cwd": str(self.cwd),
                "originator": "codex_exec",
                "cli_version": self.version,
                "source": "exec",
                # The exporter records a codex session's model from this field
                # (apss-session-exporter 0.6.0 reports a real rollout's as
                # "openai"), so it names the scripted model, as stdout does.
                "model_provider": SCRIPTED_AGENT_MODEL,
            },
        )
        self._codex_row(
            "event_msg",
            {
                "type": "task_started",
                "turn_id": self._turn_id,
                "started_at": int(self._started.timestamp()),
            },
        )
        self._codex_row(
            "turn_context",
            {"turn_id": self._turn_id, "cwd": str(self.cwd), "model": SCRIPTED_AGENT_MODEL},
        )

    def _codex(self, event: Event) -> None:
        item = event.get("item")
        if event.get("type") != "item.completed" or not isinstance(item, Mapping):
            return
        native = _codex_item(item)
        if native is None:
            return
        self._codex_row(
            "event_msg",
            {
                "type": "item_completed",
                "thread_id": self.session_id,
                "turn_id": self._turn_id,
                "item": native,
            },
        )
        if native["type"] == "AgentMessage":
            self._last_message = native["content"][0]["text"]
            # Each agent message is also a response_item, as in the pinned
            # rollout; the exporter counts messages from these rows.
            self._codex_row(
                "response_item",
                {
                    "type": "message",
                    "id": native["id"],
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self._last_message}],
                },
            )


def _codex_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``codex exec --json`` item as the rollout records it; None for kinds it has no row for."""
    kind = item.get("type")
    if kind == "agent_message" and isinstance(item.get("text"), str):
        return {
            "type": "AgentMessage",
            "id": item.get("id"),
            "content": [{"type": "Text", "text": item["text"]}],
        }
    if kind == "command_execution" and isinstance(item.get("command"), str):
        try:
            argv = shlex.split(item["command"])
        except ValueError:
            argv = [item["command"]]
        return {
            "type": "CommandExecution",
            "id": item.get("id"),
            "command": argv,
            "status": item.get("status"),
            "aggregated_output": item.get("aggregated_output"),
            "exit_code": item.get("exit_code"),
        }
    return None
