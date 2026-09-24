"""Contract guard checks captured stream shape, not just parser defaults."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "claude_cli_canary.py"
SCRIPT_FUNCTIONS = runpy.run_path(str(SCRIPT), run_name="canary_test")
CHECK_CAPTURE = SCRIPT_FUNCTIONS["check_capture"]
CAPTURE_LIVE = SCRIPT_FUNCTIONS["capture_live"]


def usage() -> dict[str, int]:
    return {
        "input_tokens": 2,
        "output_tokens": 3,
        "cache_creation_input_tokens": 4,
        "cache_read_input_tokens": 5,
    }


def capture_lines() -> list[dict]:
    return [
        {"type": "system", "subtype": "init", "tools": ["Read", "Agent"]},
        {
            "type": "assistant",
            "message": {
                "usage": usage(),
                "content": [
                    {
                        "type": "tool_use",
                        "id": "agent_1",
                        "name": "Agent",
                        "input": {"description": "Inspect"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "agent_1", "is_error": False}]
            },
        },
        {
            "type": "result",
            "is_error": False,
            "total_cost_usd": 0.01,
            "duration_ms": 1000,
            "duration_api_ms": 900,
            "num_turns": 1,
            "usage": usage(),
        },
    ]


def run_canary(tmp_path: Path, events: list[dict]) -> subprocess.CompletedProcess[str]:
    capture = tmp_path / "capture.jsonl"
    capture.write_text("\n".join(json.dumps(event) for event in events))
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(capture), "--require-subagent"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_complete_agent_capture_passes(tmp_path: Path) -> None:
    result = run_canary(tmp_path, capture_lines())
    assert result.returncode == 0, result.stderr


def test_missing_contract_fields_fail(tmp_path: Path) -> None:
    events = capture_lines()
    del events[1]["message"]["usage"]
    del events[2]["message"]["content"][0]["is_error"]
    del events[3]["usage"]
    result = run_canary(tmp_path, events)
    assert result.returncode == 1
    assert "assistant.message.usage missing input_tokens" in result.stderr
    assert "tool_result missing is_error" in result.stderr
    assert "result missing usage" in result.stderr


def test_unclosed_subagent_fails(tmp_path: Path) -> None:
    events = capture_lines()
    events[2]["message"]["content"][0]["tool_use_id"] = "other"
    result = run_canary(tmp_path, events)
    assert result.returncode == 1
    assert "subagent lifecycle incomplete" in result.stderr


@pytest.mark.parametrize(
    ("event_index", "path", "expected"),
    [
        (1, ("message", "usage", field), f"assistant.message.usage missing {field}")
        for field in usage()
    ]
    + [(3, ("usage", field), f"result.usage missing {field}") for field in usage()]
    + [
        (3, (field,), f"result missing {field}")
        for field in ("is_error", "total_cost_usd", "duration_ms", "duration_api_ms", "num_turns")
    ]
    + [
        (1, ("message", "content", 0, field), f"tool_use missing {field}")
        for field in ("id", "name", "input")
    ]
    + [
        (2, ("message", "content", 0, field), f"tool_result missing {field}")
        for field in ("tool_use_id", "is_error")
    ],
)
def test_each_parser_dependency_is_required(
    event_index: int, path: tuple[str | int, ...], expected: str
) -> None:
    events = capture_lines()
    value = events[event_index]
    for key in path[:-1]:
        value = value[key]
    del value[path[-1]]
    problems = CHECK_CAPTURE([json.dumps(event) for event in events], require_subagent=True)
    assert any(expected in problem for problem in problems), problems


def test_unknown_stream_event_fails() -> None:
    events = capture_lines()
    events.insert(1, {"type": "new_kind"})
    problems = CHECK_CAPTURE([json.dumps(event) for event in events], require_subagent=True)
    assert any("unknown stream event type" in problem for problem in problems)


def test_missing_declared_subagent_tool_fails() -> None:
    events = capture_lines()
    events[0]["tools"] = ["Read"]
    problems = CHECK_CAPTURE([json.dumps(event) for event in events], require_subagent=True)
    assert any("did not declare an Agent or Task" in problem for problem in problems)


def test_live_probe_uses_bounded_read_only_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[0] == "claude"
        assert "--restricted" in command
        assert "--safe-mode" in command
        assert "Bash" not in command
        assert command[command.index("--max-budget-usd") + 1] == "0.25"
        assert Path(str(kwargs["cwd"]), "canary.txt").read_text() == "canary-ok\n"
        return subprocess.CompletedProcess(
            command, 0, "\n".join(map(json.dumps, capture_lines())), ""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    lines, exit_code = CAPTURE_LIVE(None, 0.25)
    assert exit_code == 0
    assert CHECK_CAPTURE(lines, require_subagent=True) == []


def test_failed_subagent_fails(tmp_path: Path) -> None:
    events = capture_lines()
    events[2]["message"]["content"][0]["is_error"] = True
    result = run_canary(tmp_path, events)
    assert result.returncode == 1
    assert "1 subagent(s) stopped with an error" in result.stderr
