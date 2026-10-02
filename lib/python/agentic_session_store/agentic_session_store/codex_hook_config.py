"""Compose capture hooks with existing Codex configuration without replacing it."""

from __future__ import annotations

import tomllib
from collections.abc import MutableMapping, MutableSequence
from pathlib import Path

import tomlkit

from agentic_session_store.hook_command import (
    HARNESS_TIMEOUT_SECONDS,
    LEGACY_COMMANDS,
    HookHarness,
    guarded_command,
)

TOOL_MATCHER = "^(spawn_agent|collaborationspawn_agent)$"
PRE_COMMAND = guarded_command(HookHarness.CODEX, deny=True)
REPORT_COMMAND = guarded_command(HookHarness.CODEX, deny=False)
# Kept for callers that look up the installed pre-launch command.
HOOK_COMMAND = PRE_COMMAND


def _group(command: str, matcher: str | None) -> dict[str, object]:
    group: dict[str, object] = {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": HARNESS_TIMEOUT_SECONDS,
            }
        ]
    }
    if matcher is not None:
        group = {"matcher": matcher, **group}
    return group


# Codex has no PostToolUseFailure: a failed spawn fires no post hook at all.
# SubagentStop matches on agent type; no matcher settles every native child.
CAPTURE_GROUPS = {
    "PreToolUse": _group(PRE_COMMAND, TOOL_MATCHER),
    "PostToolUse": _group(REPORT_COMMAND, TOOL_MATCHER),
    "SubagentStop": _group(REPORT_COMMAND, None),
}
EVENTS = tuple(CAPTURE_GROUPS)


# Normalized identities reported by the pinned Codex 0.156.1 hooks/list API
# for exactly the handlers in CAPTURE_GROUPS (recomputed 2026-09-25).
# The offline pinned-binary test must pass whenever handler configuration changes.
CAPTURE_HASHES = {
    "PreToolUse": (
        "pre_tool_use",
        "sha256:2317aa422744648eddb4998686f81461f72d4f6d3f0f385c54bb756b7acfb96f",
    ),
    "PostToolUse": (
        "post_tool_use",
        "sha256:22d0cd64d210d23608a6bcf08002062f70b22aebabbc6be53ff6e52ecd110610",
    ),
    "SubagentStop": (
        "subagent_stop",
        "sha256:292d6b903e95c581af2d8d07aa39670ef69d3fc313f3c8796bb65a34efdc8233",
    ),
}


def _trust_capture(hooks: MutableMapping, event: str, index: int, path: Path) -> bool:
    state = hooks.setdefault("state", tomlkit.table())
    if not isinstance(state, MutableMapping):
        raise TypeError("Codex hook state must be a table")
    label, digest = CAPTURE_HASHES[event]
    key = f"{path.resolve()}:{label}:{index}:0"
    entry = state.setdefault(key, tomlkit.table())
    if not isinstance(entry, MutableMapping):
        raise TypeError("Codex hook state entry must be a table")
    if entry.get("enabled") is False:
        raise ValueError("Capture hook is explicitly disabled")
    if entry.get("trusted_hash") == digest:
        return False
    entry["trusted_hash"] = digest
    return True


def _legacy(group: object) -> bool:
    if not isinstance(group, MutableMapping) or group.get("matcher") != TOOL_MATCHER:
        return False
    handlers = group.get("hooks")
    return (
        isinstance(handlers, MutableSequence)
        and len(handlers) == 1
        and isinstance(handlers[0], MutableMapping)
        and handlers[0].get("command") in LEGACY_COMMANDS
    )


def merge_capture_hooks(content: str, *, config_path: Path | None = None) -> str:
    """Preserve unrelated settings and comments; repeated installation is a no-op.

    tomlkit handles both inline arrays and arrays of tables. Reparse with the
    standard library before returning, so an invalid composition is never saved.
    An unguarded capture group from an older install is replaced at its own
    index, so its trust entry is updated rather than left for a stale handler.
    """
    document = tomlkit.parse(content)
    features = document.get("features", {})
    if not isinstance(features, MutableMapping):
        raise TypeError("Codex features configuration must be a table")
    if features.get("hooks") is False or features.get("codex_hooks") is False:
        raise ValueError("Codex hooks are explicitly disabled")
    hooks = document.setdefault("hooks", tomlkit.table())
    if not isinstance(hooks, MutableMapping):
        raise TypeError("Codex hooks configuration must be a table")
    changed = False
    for event, expected in CAPTURE_GROUPS.items():
        groups = hooks.setdefault(event, tomlkit.array())
        if not isinstance(groups, MutableSequence):
            raise TypeError("Codex hook event must contain matcher groups")
        index = next((i for i, group in enumerate(groups) if group == expected), None)
        if index is None:
            index = next((i for i, group in enumerate(groups) if _legacy(group)), None)
            if index is None:
                index = len(groups)
                groups.append(expected)
            else:
                groups[index] = expected
            changed = True
        if config_path is not None:
            changed = _trust_capture(hooks, event, index, config_path) or changed
    result = tomlkit.dumps(document) if changed else content
    tomllib.loads(result)
    return result


def remove_capture_hooks(content: str, *, config_path: Path | None = None) -> str:
    """Remove exactly the groups merge_capture_hooks adds, with their trust.

    Used when the session-store capability DEGRADES (#27): capture is off, so
    a fail-closed PreToolUse guard left behind could deny child launches in a
    workspace reported as running without capture.

    Codex keys hook trust by GROUP INDEX (``<path>:<label>:<index>:<handler>``),
    so deleting a group shifts every later group in that event down by one.
    Their trust entries are moved with them, or the operator's own hooks
    would silently become untrusted. Only groups equal to what this module
    writes are removed. An event list emptied here is dropped. Unchanged input
    is returned as is; the result is re-parsed before it is returned.
    """
    document = tomlkit.parse(content)
    hooks = document.get("hooks")
    if hooks is None:
        return content
    if not isinstance(hooks, MutableMapping):
        raise TypeError("Codex hooks configuration must be a table")
    state = hooks.get("state")
    if state is not None and not isinstance(state, MutableMapping):
        raise TypeError("Codex hook state must be a table")
    prefix = f"{config_path.resolve()}:" if config_path is not None else None
    changed = False
    for event, expected in CAPTURE_GROUPS.items():
        groups = hooks.get(event)
        if groups is None:
            continue
        if not isinstance(groups, MutableSequence):
            raise TypeError("Codex hook event must contain matcher groups")
        label = CAPTURE_HASHES[event][0]
        index = len(groups) - 1
        while index >= 0:
            if groups[index] != expected:
                index -= 1
                continue
            del groups[index]
            changed = True
            if state is not None and prefix is not None:
                _shift_trust(state, f"{prefix}{label}:", index)
            index -= 1
        if not groups:
            del hooks[event]
    if not changed:
        return content
    result = tomlkit.dumps(document)
    tomllib.loads(result)
    return result


def _shift_trust(state: MutableMapping, event_prefix: str, removed: int) -> None:
    """Drop the removed group's trust and move later groups' trust down one."""
    moves: list[tuple[int, str, str]] = []
    for key in list(state.keys()):
        if not key.startswith(event_prefix):
            continue
        group, sep, handler = key[len(event_prefix) :].partition(":")
        if not sep or not group.isdigit():
            continue
        position = int(group)
        if position == removed:
            del state[key]
        elif position > removed:
            moves.append((position, key, f"{event_prefix}{position - 1}:{handler}"))
    # Ascending by index, numerically: each target is either the removed slot
    # or one this loop has already vacated.
    for _position, old, new in sorted(moves):
        state[new] = state[old]
        del state[old]
