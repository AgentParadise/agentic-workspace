"""Attach exact harness parent context to shell execution, without parsing commands."""

from __future__ import annotations

import json
import os
import shlex
import sys

from agentic_session_store.child_hook import (
    MAX_HOOK_BYTES,
    _claude_identity,
    _identity,
    _parse,
)
from agentic_session_store.claude_permissions import (
    PERMISSIONS_ENV,
    parent_permissions,
)
from agentic_session_store.contract import SessionStoreContract


def command_context(content: bytes, environment) -> dict[str, object] | None:
    if SessionStoreContract.from_env(environment) is None:
        return None
    if len(content) > MAX_HOOK_BYTES:
        raise ValueError("Hook byte limit exceeded")
    event = _parse(content)
    if event.get("tool_name") != "Bash":
        return None
    if event.get("hook_event_name") != "PreToolUse":
        raise ValueError("Unsupported command hook event")
    inputs = event.get("tool_input")
    if not isinstance(inputs, dict):
        raise TypeError("Command input missing")
    key = "command"
    command = inputs.get(key)
    if not isinstance(command, str) or "\x00" in command:
        raise TypeError("Unsupported command input")
    harness = "claude"
    parent = (
        _claude_identity(event["agent_id"])
        if "agent_id" in event
        else _identity(event.get("session_id"))
    )
    exports = [
        ("AGENTIC_PARENT_HARNESS", harness),
        ("AGENTIC_PARENT_NATIVE_ID", parent),
    ]
    # The grant a delegated Claude inherits. When it cannot be read exactly it
    # is cleared, so syn-delegate refuses a Claude child instead of guessing.
    # Nothing else depends on it: the command itself still runs.
    # A native subagent may be narrower than its session (an agent definition
    # can restrict its tools), and that list is not in the payload. Its grant
    # is therefore unknown, never the session's.
    try:
        if "agent_id" in event:
            raise ValueError("A subagent's own tool grant is not observable")
        permissions = parent_permissions(event.get("permission_mode"), environment)
    except (ValueError, TypeError, OSError, UnicodeError, IndexError):
        unset = f"unset {PERMISSIONS_ENV}\n"
    else:
        unset = ""
        exports.append((PERMISSIONS_ENV, permissions.to_json()))
    prefix = (
        unset
        + "export "
        + " ".join(f"{name}={shlex.quote(value)}" for name, value in exports)
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {
                **inputs,
                key: prefix + "\n" + command,
            },
        }
    }


def main() -> int:
    try:
        result = command_context(sys.stdin.buffer.read(MAX_HOOK_BYTES + 1), os.environ)
    except (ValueError, TypeError, OSError, RecursionError):
        print("Delegate parent context unavailable.", file=sys.stderr)
        return 2
    if result is not None:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
