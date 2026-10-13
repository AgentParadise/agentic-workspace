"""syntropic137's verdict rules, run from its own source rather than restated.

``syntropic137/`` holds three files from syntropic137 @ ``UPSTREAM_SHA``, byte for byte,
each checked against ``VENDORED_SHA256`` before anything is executed:

- ``phase_verdict.py``: ``VerdictReader``, run whole. It decides which text is a report
  (``TASK_RESULT:``, one JSON value, ``TASK_RESULT_END``) and how several settle.
- ``value_objects.py``: only the four enums ``phase_verdict`` imports are executed; the
  rest of the module needs syn_domain.
- ``EventStreamProcessor.py``: only ``_handle_result_event``, which decides whether a
  claude ``result`` is the agent's to read or the harness's own error. Its token capture
  and error-message formatting are stubbed; neither touches the verdict.

What is restated here is where the processor feeds the reader from: every non-blank
assistant ``text`` block (``_handle_assistant_event``) and every codex ``agent_message``
(``CodexStreamProcessor._handle_item_completed``).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import sys
import types
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

UPSTREAM_SHA: Final = "1017ec7b9c514afb1eceda0773c0a53031ff867a"
VENDORED = Path(__file__).parent / "syntropic137"
VENDORED_SHA256: Final = {
    "phase_verdict.py": "4b8d9511d504a5e43cfbb595a3491666793948b20de66d88a5112cf4aed5961f",
    "value_objects.py": "66e61cea4aef77f202bc379ccf744271b963c1966bd3bd7e14c60295d02c29b6",
    "EventStreamProcessor.py": "63a4dfa8b66d725c058dbf81e68ed65d7acc3624122746f75b0621499bf5754a",
}
_VALUE_OBJECTS_MODULE: Final = (
    "syn_domain.contexts.orchestration.domain.aggregate_execution.value_objects"
)
_IMPORTED_ENUMS: Final = frozenset(
    {"FailureClassification", "ReportedFailureReason", "ReviewVerdict", "SideEffectStatus"}
)


def _source(name: str) -> str:
    data = (VENDORED / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != VENDORED_SHA256[name]:
        raise AssertionError(f"{VENDORED / name} is not syntropic137's @ {UPSTREAM_SHA}")
    return data.decode("utf-8")


def _exec_nodes(nodes: list[ast.stmt], filename: str, namespace: dict[str, Any]) -> None:
    """Execute ``nodes``, statements lifted from vendored source, in ``namespace``."""
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(module, filename, "exec"), namespace)


def _load_phase_verdict() -> types.ModuleType:
    enums = types.ModuleType(_VALUE_OBJECTS_MODULE)
    enums.__dict__["StrEnum"] = StrEnum
    tree = ast.parse(_source("value_objects.py"))
    selected = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in _IMPORTED_ENUMS]
    _exec_nodes(selected, str(VENDORED / "value_objects.py"), enums.__dict__)
    previous = sys.modules.get(_VALUE_OBJECTS_MODULE)
    sys.modules[_VALUE_OBJECTS_MODULE] = enums
    try:
        module = types.ModuleType("syntropic137_phase_verdict")
        module.__file__ = str(VENDORED / "phase_verdict.py")
        sys.modules[module.__name__] = module
        exec(compile(_source("phase_verdict.py"), module.__file__, "exec"), module.__dict__)
        return module
    finally:
        if previous is None:
            sys.modules.pop(_VALUE_OBJECTS_MODULE, None)
        else:
            sys.modules[_VALUE_OBJECTS_MODULE] = previous


def _load_result_handler() -> Any:
    tree = ast.parse(_source("EventStreamProcessor.py"))
    (handler,) = (
        n
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "EventStreamProcessor"
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_result_event"
    )
    namespace: dict[str, Any] = {"_extract_error_reason": str}
    _exec_nodes([handler], str(VENDORED / "EventStreamProcessor.py"), namespace)
    return namespace["_handle_result_event"]


phase_verdict = _load_phase_verdict()
_handle_result_event = _load_result_handler()


def read_verdict(*messages: str) -> Any:
    """The ``AgentVerdict`` syntropic137's ``VerdictReader`` settles ``messages`` to."""
    reader = phase_verdict.VerdictReader()
    for message in messages:
        reader.read(message)
    return reader.verdict


def claude_verdict(events: list[dict[str, Any]]) -> Any:
    """The ``AgentVerdict`` syntropic137's ``EventStreamProcessor`` reads from a claude
    stream-json stream: assistant text blocks, then the closing ``result`` through the
    processor's own ``_handle_result_event``."""
    processor = types.SimpleNamespace(
        _verdict_reader=phase_verdict.VerdictReader(),
        _capture_result_tokens=lambda event: None,
        _error_reason=None,
        _last_agent_message=None,
    )
    for event in events:
        if event.get("type") == "assistant":
            for block in event["message"].get("content") or []:
                said = str(block.get("text", "")) if isinstance(block, dict) else ""
                if block.get("type") == "text" and said.strip():
                    processor._verdict_reader.read(said)
        elif event.get("type") == "result":
            asyncio.run(_handle_result_event(processor, event))
    return processor._verdict_reader.verdict
