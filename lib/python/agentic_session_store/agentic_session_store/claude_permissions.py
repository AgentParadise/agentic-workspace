"""What a delegated Claude may do: exactly what its parent may, never more.

``claude -p`` with no permission flags runs in the default mode, where every
Bash call that changes anything needs an approval nobody can give, so a
delegated Claude could not run shell commands (agentic-workspace#20). Granting
it ``bypassPermissions`` unconditionally would make delegation a way to widen
a parent's permissions. So the child inherits the parent's own grant:

* Claude parent. The Bash ``PreToolUse`` context hook (``command_context``)
  reads the permission mode from the hook payload, which the harness supplies,
  and the session's ``--tools``, ``--allowedTools`` and ``--disallowedTools``
  from the argv of the Claude process that runs the hook (``CLAUDE_PID``,
  checked to be the hook's ancestor). It exports them as one JSON value; the
  shim passes them to the child as the same flags. Permission rules from
  settings files need no forwarding: the child reads the same files.
* Codex parent. Codex exposes no permission mode to its shell. The parent can
  run shell commands and edit files, and the delegate runs inside the
  parent's own OS sandbox, so the child gets ``dontAsk`` with exactly those
  tools allowed: anything else is denied, never prompted for.
* Unknown. A Claude parent whose grant is missing or malformed is refused
  (``parent_permissions_unavailable``), never guessed.

When a list is given more than once, the narrower reading is kept: the last
``--tools`` and ``--allowedTools``, and the union of every
``--disallowedTools``. So an ambiguity can only make the child narrower.

Measured against Claude Code 2.1.281 (offline fixture): the hook payload's
``permission_mode`` is one of ``default``, ``acceptEdits``, ``plan``,
``dontAsk`` and ``bypassPermissions`` (``auto`` without eligibility and
``manual`` both report ``default``), and ``--permission-mode`` accepts every
one of them in ``-p`` mode. Bash that writes runs under ``bypassPermissions``
and under ``dontAsk`` with ``--allowedTools Bash``, and is refused under
``default`` and ``acceptEdits``.

A native subagent's own tool list (from its agent definition) is not visible
to the hook, and may be narrower than the session's, so a Bash call made by a
subagent exports no grant and a Claude delegate from there is refused.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PERMISSIONS_ENV = "AGENTIC_PARENT_CLAUDE_PERMISSIONS"
CLAUDE_PID_ENV = "CLAUDE_PID"

PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions", "auto"}
)
# Codex parent: shell and file edits, the tools Codex itself has.
CODEX_PARENT_MODE = "dontAsk"
CODEX_PARENT_ALLOWED = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")

_TOOLS = ("--tools",)
_ALLOWED = ("--allowedTools", "--allowed-tools")
_DISALLOWED = ("--disallowedTools", "--disallowed-tools")
_MAX_ENTRIES = 256
_MAX_ENTRY_BYTES = 1024


@dataclass(frozen=True)
class ClaudePermissions:
    """A Claude permission grant: mode plus the session's tool lists.

    ``tools`` is ``None`` when the parent did not restrict availability.
    """

    mode: str
    tools: tuple[str, ...] | None = None
    allowed: tuple[str, ...] = ()
    disallowed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in PERMISSION_MODES:
            raise ValueError("Unknown Claude permission mode")
        for values in (self.tools or (), self.allowed, self.disallowed):
            if len(values) > _MAX_ENTRIES:
                raise ValueError("Too many Claude tool entries")
            for value in values:
                if (
                    not isinstance(value, str)
                    or not value
                    or "\x00" in value
                    or len(value.encode("utf-8")) > _MAX_ENTRY_BYTES
                ):
                    raise ValueError("Invalid Claude tool entry")

    def arguments(self) -> list[str]:
        """Flags for the child. Every list is followed by another option, so
        Claude's variadic parser cannot swallow the prompt."""
        arguments = ["--permission-mode", self.mode]
        if self.tools is not None:
            # One comma-joined value; an empty list disables every tool.
            arguments += ["--tools", ",".join(self.tools)]
        if self.allowed:
            arguments += ["--allowedTools", *self.allowed]
        if self.disallowed:
            arguments += ["--disallowedTools", *self.disallowed]
        return arguments

    def to_json(self) -> str:
        return json.dumps(
            {
                "mode": self.mode,
                "tools": None if self.tools is None else list(self.tools),
                "allowed": list(self.allowed),
                "disallowed": list(self.disallowed),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> ClaudePermissions:
        document = json.loads(value)
        if not isinstance(document, dict) or set(document) != {
            "mode",
            "tools",
            "allowed",
            "disallowed",
        }:
            raise ValueError("Malformed Claude permissions")
        tools = document["tools"]

        def strings(item: object) -> tuple[str, ...]:
            if not isinstance(item, list) or not all(
                isinstance(entry, str) for entry in item
            ):
                raise ValueError("Malformed Claude permissions")
            return tuple(item)

        mode = document["mode"]
        if not isinstance(mode, str):
            raise TypeError("Malformed Claude permissions")
        return cls(
            mode=mode,
            tools=None if tools is None else strings(tools),
            allowed=strings(document["allowed"]),
            disallowed=strings(document["disallowed"]),
        )


CODEX_PARENT_PERMISSIONS = ClaudePermissions(
    mode=CODEX_PARENT_MODE, allowed=CODEX_PARENT_ALLOWED
)


def _split(values: Sequence[str]) -> list[str]:
    """Claude accepts comma or space separated lists. A rule such as
    ``Bash(git log:*)`` keeps its inner commas and spaces."""
    entries: list[str] = []
    for value in values:
        current, depth = "", 0
        for character in value:
            if character == "(":
                depth += 1
            elif character == ")":
                depth = max(0, depth - 1)
            if character in ", " and depth == 0:
                if current:
                    entries.append(current)
                current = ""
                continue
            current += character
        if current:
            entries.append(current)
    return entries


def launch_lists(
    argv: Sequence[str],
) -> tuple[tuple[str, ...] | None, tuple[str, ...], tuple[str, ...]]:
    """``--tools``, ``--allowedTools`` and ``--disallowedTools`` from a Claude
    argv, read the way its variadic parser reads them: values run until the
    next argument that starts with ``-``."""
    tools: tuple[str, ...] | None = None
    allowed: tuple[str, ...] = ()
    disallowed: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        index += 1
        if argument == "--":
            break
        name, equals, inline = argument.partition("=")
        if name not in _TOOLS + _ALLOWED + _DISALLOWED:
            continue
        values = [inline] if equals else []
        if not equals:
            while index < len(argv) and not argv[index].startswith("-"):
                values.append(argv[index])
                index += 1
        entries = _split(values)
        if name in _TOOLS:
            tools = tuple(entries)
        elif name in _ALLOWED:
            allowed = tuple(entries)
        else:
            disallowed.extend(entry for entry in entries if entry not in disallowed)
    return tools, allowed, tuple(disallowed)


def _ancestors(pid: int, proc: Path) -> set[int]:
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen and len(seen) < 64:
        seen.add(current)
        stat = (proc / str(current) / "stat").read_text()
        # The command name is parenthesised and may itself contain spaces.
        current = int(stat.rsplit(")", 1)[1].split()[1])
    return seen


def parent_permissions(
    mode: object,
    environment: Mapping[str, str],
    *,
    proc: Path = Path("/proc"),
    self_pid: int | None = None,
) -> ClaudePermissions:
    """The grant of the Claude session running this hook.

    Raises ``ValueError``, ``TypeError`` or ``OSError`` when it cannot be read
    exactly.
    """
    if not isinstance(mode, str):
        raise TypeError("Hook payload has no permission mode")
    claude = int(environment.get(CLAUDE_PID_ENV, ""))
    if claude <= 1 or claude not in _ancestors(
        os.getpid() if self_pid is None else self_pid, proc
    ):
        raise ValueError("CLAUDE_PID is not this hook's ancestor")
    raw = (proc / str(claude) / "cmdline").read_bytes()
    argv = [part.decode("utf-8") for part in raw.split(b"\x00")[:-1]]
    tools, allowed, disallowed = launch_lists(argv[1:])
    return ClaudePermissions(mode, tools, allowed, disallowed)


def delegated_permissions(environment: Mapping[str, str]) -> ClaudePermissions:
    """The grant a delegated Claude gets, from its parent's context.

    Raises ``ValueError`` when a Claude parent's grant is missing or malformed.
    """
    harness = environment.get("AGENTIC_PARENT_HARNESS")
    if harness is None and environment.get("AGENTIC_PARENT_NATIVE_ID") is None:
        harness = "codex"
    if harness == "codex":
        return CODEX_PARENT_PERMISSIONS
    value = environment.get(PERMISSIONS_ENV)
    if value is None:
        raise ValueError("Parent Claude permissions are unavailable")
    return ClaudePermissions.from_json(value)
