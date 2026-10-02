"""Codex sandbox policy for delegated runs: which mode, and whether it works.

Codex exits 0 even when every shell tool call fails inside a sandbox that
cannot start, so a delegate that did nothing would be recorded as completed.
syn-delegate therefore probes the sandbox itself, live, with the exact mode,
working directory and environment the delegate will use, and refuses to
launch when the probe fails. There is no status file to trust or forge.

This is a reliability guard, not a security boundary: an agent can always run
``codex`` directly. The boundary is the container's seccomp and AppArmor
policy, which the probe only observes.

Modes that disable Codex's own sandbox are rejected, never passed through.

Nested delegation. A delegate's shell commands run inside its sandbox, so a
``syn-delegate`` started there needs two things workspace-write does not give
by default (agentic-workspace#19): the retained child journal, which lives on
the spool outside the working directory, and the network, to reach its own
model. ``SandboxGrant`` adds exactly those, ``network_access`` and two extra
writable roots: the journal's partition directory, and this partition's Claude
transcript root ``$SPOOL/$PARTITION/claude`` so a Claude grandchild's own
transcript is captured. Egress stays governed by the container's network
policy. ``read-only`` gets neither, so a read-only
delegate cannot delegate further; that denial is reported (see
``delegate.DENIAL_MARKER``), not silent.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

SANDBOX_MODE_ENV = "AGENTIC_DELEGATE_CODEX_SANDBOX"
PROBE_TIMEOUT_SECONDS = 30.0


class CodexSandboxMode(StrEnum):
    """Codex ``--sandbox`` values a delegate may request."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


DEFAULT_SANDBOX_MODE = CodexSandboxMode.WORKSPACE_WRITE

# Values that turn Codex's sandbox off. Named so the refusal is explicit
# rather than an "unknown value" that invites someone to add it.
FORBIDDEN_SANDBOX_MODES = frozenset({"danger-full-access", "external-sandbox"})


def parse_sandbox_mode(value: str | None) -> CodexSandboxMode:
    """Validate a requested sandbox mode; empty or absent means the default."""
    if value is None or not value.strip():
        return DEFAULT_SANDBOX_MODE
    candidate = value.strip()
    if candidate in FORBIDDEN_SANDBOX_MODES:
        raise ValueError(
            f"Codex sandbox mode {candidate!r} disables the sandbox and is not allowed"
        )
    try:
        return CodexSandboxMode(candidate)
    except ValueError:
        allowed = ", ".join(mode.value for mode in CodexSandboxMode)
        raise ValueError(
            f"Unknown Codex sandbox mode {candidate!r}; expected one of: {allowed}"
        ) from None


def resolve_sandbox_mode(
    requested: str | None, environment: Mapping[str, str]
) -> CodexSandboxMode:
    """Command-line value wins over ``AGENTIC_DELEGATE_CODEX_SANDBOX``."""
    return parse_sandbox_mode(
        requested if requested is not None else environment.get(SANDBOX_MODE_ENV)
    )


@dataclass(frozen=True)
class SandboxGrant:
    """What a workspace-write delegate gets beyond Codex's defaults."""

    writable_roots: tuple[str, ...] = ()
    network_access: bool = False

    def config(self, mode: CodexSandboxMode) -> list[str]:
        """``-c`` overrides, identical for the probe and the delegate.

        Only workspace-write has writable roots or a network switch; the keys
        replace any value from ``CODEX_HOME``, so its config cannot widen them.
        """
        if mode is not CodexSandboxMode.WORKSPACE_WRITE:
            return []
        roots = "[" + ", ".join(json.dumps(root) for root in self.writable_roots) + "]"
        return [
            "-c",
            f"sandbox_workspace_write.writable_roots={roots}",
            "-c",
            "sandbox_workspace_write.network_access="
            + ("true" if self.network_access else "false"),
        ]


@dataclass(frozen=True)
class CodexSandboxStatus:
    """Probe verdict. Anything but a clean exit means unavailable."""

    available: bool
    detail: str


def probe(
    mode: CodexSandboxMode,
    *,
    environment: Mapping[str, str],
    cwd: str | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    grant: SandboxGrant | None = None,
) -> CodexSandboxStatus:
    """Run ``codex sandbox`` in ``mode`` exactly as the delegate will run.

    With a grant, the probe also writes inside every extra writable root, so a
    host policy that cannot mount one (for example an AppArmor profile without
    the rule) refuses the launch instead of failing the delegate later.
    """
    extra = [] if grant is None else grant.config(mode)
    roots = () if grant is None or not extra else grant.writable_roots
    check = (
        ["true"]
        if not roots
        else [
            "/bin/sh",
            "-c",
            (
                'for root do : > "$root/.codex-sandbox-probe" && rm -f '
                '"$root/.codex-sandbox-probe" || exit 1; done'
            ),
            "probe",
            *roots,
        ]
    )
    command = [
        "codex",
        "sandbox",
        "-c",
        f'sandbox_mode="{mode.value}"',
        *extra,
        "--",
        *check,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            env=dict(environment),
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CodexSandboxStatus(False, "codex is not installed")
    except subprocess.TimeoutExpired:
        return CodexSandboxStatus(
            False, f"codex sandbox probe timed out after {timeout:g}s"
        )
    except OSError as error:
        return CodexSandboxStatus(
            False, f"codex sandbox probe could not start: {error.strerror}"
        )
    if result.returncode == 0:
        return CodexSandboxStatus(True, f"codex sandbox {mode.value} probe passed")
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    cause = next((line for line in lines if "bwrap" in line or "rror" in line), None)
    detail = (cause or (lines[-1] if lines else "no output")).strip()[:300]
    return CodexSandboxStatus(False, f"probe exit {result.returncode}: {detail}")
