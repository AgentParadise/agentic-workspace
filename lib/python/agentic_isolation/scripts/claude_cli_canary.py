"""Check a Claude CLI JSONL capture or run a bounded live contract canary.

Run from this Python project's directory:
    uv run python scripts/claude_cli_canary.py capture.jsonl --require-subagent
    uv run python scripts/claude_cli_canary.py --live
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from agentic_isolation.providers.claude_cli.event_parser import (
    KNOWN_STREAM_EVENT_TYPES,
    SUBAGENT_TOOL_NAMES,
    EventParser,
)
from agentic_isolation.providers.claude_cli.types import EventType

USAGE_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
)
RESULT_FIELDS = frozenset(
    {"is_error", "total_cost_usd", "duration_ms", "duration_api_ms", "num_turns", "usage"}
)
TOOL_USE_FIELDS = frozenset({"id", "name", "input"})
TOOL_RESULT_FIELDS = frozenset({"tool_use_id", "is_error"})
LIVE_PROMPT = (
    "Use the Agent or Task subagent tool to delegate this exact task: "
    "read canary.txt using the Read tool and report its text. "
    "Do not read the file yourself."
)


def _missing_fields(value: Any, fields: frozenset[str]) -> list[str]:
    if not isinstance(value, dict):
        return sorted(fields)
    return sorted(fields - value.keys())


def check_capture(lines: list[str], require_subagent: bool = False) -> list[str]:
    """Return violations of fields and lifecycle events the parser depends on."""
    parser = EventParser(session_id="canary")
    problems: list[str] = []
    assistant_count = result_count = tool_result_count = 0
    starts = stops = failed_stops = 0
    init_tools: list[str] | None = None

    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {line_number}: invalid JSON: {exc}")
            continue
        if not isinstance(raw, dict):
            problems.append(f"line {line_number}: expected JSON object")
            continue
        if "_recording" in raw:
            continue

        kind = raw.get("type")
        if kind not in KNOWN_STREAM_EVENT_TYPES:
            problems.append(f"line {line_number}: unknown stream event type {kind!r}")
            continue

        if kind == "system" and raw.get("subtype") == "init":
            tools = raw.get("tools")
            if isinstance(tools, list) and all(isinstance(name, str) for name in tools):
                init_tools = tools
            elif require_subagent:
                problems.append(f"line {line_number}: system/init missing tools list")

        if kind == "assistant":
            assistant_count += 1
            message = raw.get("message")
            if not isinstance(message, dict):
                problems.append(f"line {line_number}: assistant missing message object")
                continue
            for field in _missing_fields(message.get("usage"), USAGE_FIELDS):
                problems.append(f"line {line_number}: assistant.message.usage missing {field}")
            content = message.get("content")
            if not isinstance(content, list):
                problems.append(f"line {line_number}: assistant.message missing content list")
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    for field in _missing_fields(item, TOOL_USE_FIELDS):
                        problems.append(f"line {line_number}: tool_use missing {field}")
        elif kind == "user":
            message = raw.get("message")
            if not isinstance(message, dict):
                problems.append(f"line {line_number}: user missing message object")
                continue
            content = message.get("content")
            if not isinstance(content, list):
                problems.append(f"line {line_number}: user.message missing content list")
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_result_count += 1
                    for field in _missing_fields(item, TOOL_RESULT_FIELDS):
                        problems.append(f"line {line_number}: tool_result missing {field}")
                    if "is_error" in item and not isinstance(item["is_error"], bool):
                        problems.append(f"line {line_number}: tool_result.is_error is not boolean")
        elif kind == "result":
            result_count += 1
            for field in _missing_fields(raw, RESULT_FIELDS):
                problems.append(f"line {line_number}: result missing {field}")
            for field in _missing_fields(raw.get("usage"), USAGE_FIELDS):
                problems.append(f"line {line_number}: result.usage missing {field}")
            if raw.get("is_error") is not False:
                problems.append(f"line {line_number}: result reports an error")

        try:
            events = parser.parse_line(line)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            problems.append(f"line {line_number}: parser rejected event: {exc}")
            continue
        starts += sum(event.event_type == EventType.SUBAGENT_STARTED for event in events)
        for event in events:
            if event.event_type == EventType.SUBAGENT_STOPPED:
                stops += 1
                if event.success is not True:
                    failed_stops += 1

    if not assistant_count:
        problems.append("no assistant messages found")
    if not result_count:
        problems.append("no result event found")
    if not tool_result_count:
        problems.append("no tool_result found; capture must exercise a tool")
    if require_subagent:
        if init_tools is None or not SUBAGENT_TOOL_NAMES.intersection(init_tools):
            problems.append("system/init did not declare an Agent or Task tool")
        if not starts or starts != stops or parser.get_summary().subagent_count != starts:
            problems.append(f"subagent lifecycle incomplete: {starts} starts, {stops} stops")
        if failed_stops:
            problems.append(f"{failed_stops} subagent(s) stopped with an error")
    return problems


def capture_live(model: str | None, max_budget_usd: float) -> tuple[list[str], int]:
    """Run a read-only subagent probe in a temporary directory."""
    with tempfile.TemporaryDirectory(prefix="claude-contract-") as workdir:
        Path(workdir, "canary.txt").write_text("canary-ok\n")
        command = [
            "claude",
            "-p",
            "--safe-mode",
            "--restricted",
            "--tools",
            "Read,Agent,Task",
            "--permission-mode",
            "dontAsk",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-budget-usd",
            str(max_budget_usd),
        ]
        if model:
            command.extend(["--model", model])
        command.append(LIVE_PROMPT)
        result = subprocess.run(
            command, cwd=workdir, capture_output=True, text=True, timeout=300, check=False
        )
        return result.stdout.splitlines(), result.returncode


def main() -> int:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument("capture", nargs="?", type=Path, help="JSONL capture, or - for stdin")
    arg_parser.add_argument("--require-subagent", action="store_true")
    arg_parser.add_argument("--live", action="store_true", help="run a bounded real CLI probe")
    arg_parser.add_argument("--model", help="model for --live, otherwise CLI default")
    arg_parser.add_argument("--max-budget-usd", type=float, default=0.25)
    args = arg_parser.parse_args()

    if args.live:
        if args.capture is not None:
            arg_parser.error("capture path cannot be combined with --live")
        try:
            lines, exit_code = capture_live(args.model, args.max_budget_usd)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            print(f"Claude CLI probe failed: {exc}", file=sys.stderr)
            return 1
        problems = check_capture(lines, require_subagent=True)
        if exit_code:
            problems.append(f"Claude CLI exited {exit_code}")
    else:
        if args.capture is None:
            arg_parser.error("provide a capture path or --live")
        try:
            lines = (
                sys.stdin.readlines()
                if str(args.capture) == "-"
                else args.capture.read_text().splitlines()
            )
        except OSError as exc:
            print(f"Cannot read capture: {exc}", file=sys.stderr)
            return 1
        problems = check_capture(lines, require_subagent=args.require_subagent)

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("Claude CLI capture contract OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
