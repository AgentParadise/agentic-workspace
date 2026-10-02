"""A delegated Claude gets its parent's grant, never a broader one (#20)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agentic_session_store.claude_permissions import (
    CODEX_PARENT_PERMISSIONS,
    PERMISSIONS_ENV,
    ClaudePermissions,
    delegated_permissions,
    launch_lists,
    parent_permissions,
)
from agentic_session_store.command_context import command_context


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], (None, (), ())),
        (["--tools", "Bash,Read", "-p", "x"], (("Bash", "Read"), (), ())),
        (["--tools", "Bash", "Read", "--model", "m"], (("Bash", "Read"), (), ())),
        (["--tools=Bash,Read"], (("Bash", "Read"), (), ())),
        (["--tools", ""], ((), (), ())),
        (
            ["--allowedTools", "Bash(git log:*)", "Edit", "--verbose"],
            (None, ("Bash(git log:*)", "Edit"), ()),
        ),
        (["--allowed-tools", "Bash(git *),Read"], (None, ("Bash(git *)", "Read"), ())),
        # Repeated: the narrower reading. Last allow list, every deny list.
        (
            ["--tools", "Bash,Read,Edit", "--tools", "Read"],
            (("Read",), (), ()),
        ),
        (
            ["--allowedTools", "Bash", "Edit", "--allowedTools", "Read"],
            (None, ("Read",), ()),
        ),
        (
            ["--disallowedTools", "WebFetch", "--disallowed-tools", "Bash,WebFetch"],
            (None, (), ("WebFetch", "Bash")),
        ),
        # After `--` nothing is an option.
        (["--", "--tools", "Bash"], (None, (), ())),
    ],
)
def test_launch_lists_reads_claude_variadic_options(argv, expected):
    assert launch_lists(argv) == expected


def test_arguments_keep_lists_away_from_the_prompt():
    grant = ClaudePermissions("default", ("Bash", "Read"), ("Bash(git *)",), ("Edit",))
    assert grant.arguments() == [
        "--permission-mode",
        "default",
        "--tools",
        "Bash,Read",
        "--allowedTools",
        "Bash(git *)",
        "--disallowedTools",
        "Edit",
    ]
    assert ClaudePermissions("bypassPermissions").arguments() == [
        "--permission-mode",
        "bypassPermissions",
    ]
    # An empty tool list is kept: it disables every tool, as for the parent.
    assert ClaudePermissions("plan", ()).arguments()[-2:] == ["--tools", ""]


def test_round_trip_and_rejections():
    grant = ClaudePermissions("dontAsk", None, ("Bash",), ())
    assert ClaudePermissions.from_json(grant.to_json()) == grant
    for value in [
        "{}",
        '{"mode":"yolo","tools":null,"allowed":[],"disallowed":[]}',
        '{"mode":"default","tools":"Bash","allowed":[],"disallowed":[]}',
        '{"mode":"default","tools":null,"allowed":[1],"disallowed":[]}',
        '{"mode":"default","tools":null,"allowed":[],"disallowed":[],"x":1}',
        '{"mode":"default","tools":null,"allowed":[""],"disallowed":[]}',
        "[]",
    ]:
        with pytest.raises((ValueError, TypeError)):
            ClaudePermissions.from_json(value)


def test_delegated_permissions_by_parent():
    grant = ClaudePermissions("acceptEdits", ("Bash",))
    assert (
        delegated_permissions(
            {
                "AGENTIC_PARENT_HARNESS": "claude",
                "AGENTIC_PARENT_NATIVE_ID": "p",
                PERMISSIONS_ENV: grant.to_json(),
            }
        )
        == grant
    )
    # Codex parent: dontAsk with the tools Codex itself has.
    assert delegated_permissions({"CODEX_THREAD_ID": "t"}) == CODEX_PARENT_PERMISSIONS
    assert CODEX_PARENT_PERMISSIONS.mode == "dontAsk"
    with pytest.raises(ValueError):
        delegated_permissions(
            {"AGENTIC_PARENT_HARNESS": "claude", "AGENTIC_PARENT_NATIVE_ID": "p"}
        )


def _fake_proc(tmp_path: Path, argv: list[str]) -> Path:
    proc = tmp_path / "proc"
    for pid, parent, name in ((40, 30, "python3"), (30, 8, "sh"), (8, 1, "claude (x)")):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "stat").write_text(f"{pid} ({name}) S {parent} 1 1\n")
    (proc / "8" / "cmdline").write_bytes(
        b"".join(part.encode() + b"\x00" for part in ["claude", *argv])
    )
    return proc


def test_parent_permissions_come_from_the_ancestor_claude(tmp_path):
    proc = _fake_proc(tmp_path, ["-p", "go", "--tools", "Bash,Read", "--model", "m"])
    grant = parent_permissions(
        "bypassPermissions", {"CLAUDE_PID": "8"}, proc=proc, self_pid=40
    )
    assert grant == ClaudePermissions("bypassPermissions", ("Bash", "Read"))
    # A CLAUDE_PID that is not this hook's ancestor is not trusted.
    (proc / "9").mkdir()
    (proc / "9" / "stat").write_text("9 (claude) S 1 1 1\n")
    (proc / "9" / "cmdline").write_bytes(b"claude\x00")
    with pytest.raises(ValueError):
        parent_permissions("default", {"CLAUDE_PID": "9"}, proc=proc, self_pid=40)
    with pytest.raises(ValueError):
        parent_permissions("default", {}, proc=proc, self_pid=40)
    with pytest.raises(TypeError):
        parent_permissions(None, {"CLAUDE_PID": "8"}, proc=proc, self_pid=40)


def _event(mode="bypassPermissions"):
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "session_id": "parent",
            "permission_mode": mode,
            "tool_input": {"command": f'printf "%s" "${{{PERMISSIONS_ENV}-unset}}"'},
        }
    ).encode()


ENV = {
    "AGENTIC_SESSION_STORE_PROVIDER": "local",
    "AGENTIC_SESSION_STORE_PARTITION": "run",
}


def _run(output):
    return subprocess.run(
        ["sh", "-c", output["hookSpecificOutput"]["updatedInput"]["command"]],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, PERMISSIONS_ENV: "stale"},
    ).stdout


def test_command_context_exports_the_parent_grant(tmp_path, monkeypatch):
    import agentic_session_store.command_context as module

    proc = _fake_proc(tmp_path, ["--tools", "Bash", "--allowedTools", "Bash(git *)"])

    def fake(mode, environment):
        return parent_permissions(mode, environment, proc=proc, self_pid=40)

    monkeypatch.setattr(module, "parent_permissions", fake)
    output = command_context(_event("dontAsk"), {**ENV, "CLAUDE_PID": "8"})
    assert ClaudePermissions.from_json(_run(output)) == ClaudePermissions(
        "dontAsk", ("Bash",), ("Bash(git *)",)
    )


def test_command_context_clears_an_unreadable_grant(tmp_path):
    # No CLAUDE_PID: the grant is unknown, so a stale value is cleared and the
    # command still runs.
    output = command_context(_event(), ENV)
    assert _run(output) == "unset"
