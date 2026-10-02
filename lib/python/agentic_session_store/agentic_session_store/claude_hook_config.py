"""Compose durable capture with existing Claude settings without overriding policy."""

from __future__ import annotations

import json

from agentic_session_store.child_hook import _parse
from agentic_session_store.hook_command import (
    HARNESS_TIMEOUT_SECONDS,
    LEGACY_COMMANDS,
    HookHarness,
    guarded_command,
)

TOOL_MATCHER = "^(Agent|Task)$"


def _group(*, deny: bool, matcher: str | None = TOOL_MATCHER) -> dict[str, object]:
    group: dict[str, object] = {
        "hooks": [
            {
                "type": "command",
                "command": guarded_command(HookHarness.CLAUDE, deny=deny),
                "timeout": HARNESS_TIMEOUT_SECONDS,
            }
        ]
    }
    if matcher is not None:
        group = {"matcher": matcher, **group}
    return group


# Exports the parent's Bash context for syn-delegate (command_context).
CONTEXT_GROUP: dict[str, object] = {
    "matcher": "^Bash$",
    "hooks": [
        {
            "type": "command",
            "command": "python3 -m agentic_session_store.command_context",
            "timeout": 10,
        }
    ],
}

# SubagentStop matches on agent type; no matcher settles every native child.
CAPTURE_GROUPS: dict[str, dict[str, object]] = {
    "PreToolUse": _group(deny=True),
    "PostToolUse": _group(deny=False),
    "PostToolUseFailure": _group(deny=False),
    "SubagentStop": _group(deny=False, matcher=None),
}


def _legacy(group: object) -> bool:
    if not isinstance(group, dict) or group.get("matcher") != TOOL_MATCHER:
        return False
    hooks = group.get("hooks")
    return (
        isinstance(hooks, list)
        and len(hooks) == 1
        and isinstance(hooks[0], dict)
        and hooks[0].get("command") in LEGACY_COMMANDS
    )


def merge_capture_hooks(content: str) -> str:
    document = _parse(content) if content.strip() else {}
    if document.get("disableAllHooks") is True:
        raise ValueError("Claude hooks are explicitly disabled")
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError("Claude hooks must be an object")
    changed = False
    for event, expected in CAPTURE_GROUPS.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise TypeError("Claude hook event must contain matcher groups")
        if expected in groups:
            continue
        # An unguarded recorder from an older install fails open; replace it
        # in place rather than running both.
        legacy = next((i for i, group in enumerate(groups) if _legacy(group)), None)
        if legacy is None:
            groups.append(expected)
        else:
            groups[legacy] = expected
        changed = True
    context = dict(CONTEXT_GROUP)
    if context not in hooks["PreToolUse"]:
        hooks["PreToolUse"].append(context)
        changed = True
    return json.dumps(document, indent=2) + "\n" if changed else content


def remove_capture_hooks(content: str) -> str:
    """Remove exactly the groups merge_capture_hooks adds, and nothing else.

    Used when the session-store capability DEGRADES (#27): capture is off, so
    a fail-closed PreToolUse guard left behind could deny child launches in a
    workspace reported as running without capture. Only groups equal to what
    this module writes are removed; an operator's group is never touched,
    even one that mentions the recorder. An event list emptied here is
    dropped. Unchanged input is returned as is.
    """
    if not content.strip():
        return content
    document = _parse(content)
    hooks = document.get("hooks")
    if hooks is None:
        return content
    if not isinstance(hooks, dict):
        raise TypeError("Claude hooks must be an object")
    owned: dict[str, list[dict[str, object]]] = {
        event: [group] for event, group in CAPTURE_GROUPS.items()
    }
    owned["PreToolUse"] = [CAPTURE_GROUPS["PreToolUse"], CONTEXT_GROUP]
    changed = False
    for event, groups_owned in owned.items():
        groups = hooks.get(event)
        if groups is None:
            continue
        if not isinstance(groups, list):
            raise TypeError("Claude hook event must contain matcher groups")
        kept = [group for group in groups if group not in groups_owned]
        if len(kept) == len(groups):
            continue
        changed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return json.dumps(document, indent=2) + "\n" if changed else content
