"""Read what the workspace lifecycle decided about each capability (#27).

``workspace/entrypoint.sh`` section 5.7 runs every active capability's doctor
before the agent starts. It then appends ONE ``capability_status`` row per
capability to that capability's doctor audit file, beside the doctor's own
payload. That row is the lifecycle's verdict:

* ``ready``: the doctor passed.
* ``degraded``: the doctor failed, the capability's manifest declares
  ``failure_policy=degrade`` and ``AGENTIC_<CAP>_REQUIRED`` is not ``1``. The
  workspace started anyway with that capability DISABLED (for session-store:
  no transcript capture, no upload). This is the state an orchestrator should
  surface, for example as "capture degraded" on the execution.
* ``failed``: the doctor failed and the capability was required, so the
  workspace exited before the agent ran.

The format is owned here and in the entrypoint, not by any consumer, so a
consumer reads it through :func:`read_capability_status` and never parses the
audit file itself. The full contract is in ``docs/workspace-capabilities.md``
("Capability status contract").

Agents run through ``docker exec`` in production, so the environment flag the
entrypoint also exports (``AGENTIC_<CAP>_READY=0``) reaches only the
container's own command, never an exec'd agent or the host. The audit row is
the channel that does.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentic_isolation.harnesses import ExecFn, exec_argv

STATUS_RECORD = "capability_status"
"""The ``record`` value that marks a lifecycle row among doctor payloads."""

AUDIT_ROOT = "/var/agentic"
"""Where the entrypoint puts each capability's audit directory by default.

Mirrors ``${AGENTIC_CAPABILITY_AUDIT_DIR:-/var/agentic/${cap}-doctor}`` in
entrypoint.sh 5.7. A deployment that sets AGENTIC_CAPABILITY_AUDIT_DIR passes
the same value as ``audit_dir``.
"""

MAX_AUDIT_BYTES = 1024 * 1024
"""How much of the audit tail is read. A torn first line is ignored."""

READ_TIMEOUT_SECONDS = 10.0

_CAPABILITY_NAME = re.compile(r"[a-z0-9-]+")
"""The entrypoint's capability-name charset (__capability_name_safe)."""

# One exec: the container's hostname first, then this capability's status
# rows. The hostname is how a row written by THIS container is told apart from
# one written by another container sharing a persisted audit directory. Rows
# are SELECTED before the byte bound is applied, so doctor payloads (which can
# be large and repeat on every start) never push a verdict out of the window.
_READ_SCRIPT = (
    'cat /proc/sys/kernel/hostname 2>/dev/null || printf "%s\\n" "${HOSTNAME:-}"; '
    'cat "$1"/*.jsonl 2>/dev/null | grep -F \'"record":"capability_status"\' '
    f"| tail -c {MAX_AUDIT_BYTES}; exit 0"
)


class CapabilityStatusError(Exception):
    """The status could not be read, or a status row broke the contract."""


class CapabilityState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class FailurePolicy(StrEnum):
    """A capability's declared failure policy (its ``capability.conf``)."""

    FAIL = "fail"
    DEGRADE = "degrade"


class CapabilityStatus(BaseModel):
    """One ``capability_status`` row, schema version 1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    record: Literal["capability_status"]
    capability: str = Field(pattern=r"^[a-z0-9-]+$")
    provider: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    status: CapabilityState
    policy: FailurePolicy
    required: bool
    doctor_exit: int = Field(ge=0)
    failed_checks: tuple[str, ...]
    host: str
    at: datetime

    @property
    def degraded(self) -> bool:
        """True when the workspace is running with this capability disabled."""
        return self.status is CapabilityState.DEGRADED


def _validated_name(capability: str) -> str:
    if not _CAPABILITY_NAME.fullmatch(capability):
        raise ValueError("Capability names are [a-z0-9-]+")
    return capability


def default_audit_dir(capability: str) -> str:
    """The audit directory the entrypoint uses when none is configured."""
    return f"{AUDIT_ROOT}/{_validated_name(capability)}-doctor"


def parse_capability_status(
    audit_text: str, capability: str, *, host: str | None
) -> CapabilityStatus | None:
    """The last status row for ``capability`` written by ``host``, if any.

    Doctor payloads, torn lines and rows for other capabilities are skipped.
    A row that claims to be this capability's status and does not validate is
    a contract violation and raises, because reporting "unknown" for it would
    hide a degraded workspace behind a parse error. ``host=None`` accepts rows
    from any container.

    ``None`` means UNKNOWN, never ready: the capability was not active, the
    entrypoint did not reach 5.7, or the audit path was unwritable (the row
    then went to the container's stderr instead).
    """
    _validated_name(capability)
    latest: CapabilityStatus | None = None
    for line in audit_text.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("record") != STATUS_RECORD or data.get("capability") != capability:
            continue
        # Another container's row is not this workspace's verdict, malformed
        # or not, so it is filtered before it can raise.
        if host is not None and data.get("host") != host:
            continue
        try:
            status = CapabilityStatus.model_validate(data)
        except ValidationError as error:
            raise CapabilityStatusError(
                f"Malformed {STATUS_RECORD} row for {capability}"
            ) from error
        latest = status
    return latest


async def read_capability_status(
    execute: ExecFn,
    capability: str,
    *,
    audit_dir: str | None = None,
    timeout: float = READ_TIMEOUT_SECONDS,
) -> CapabilityStatus | None:
    """Read this workspace's lifecycle verdict for one capability.

    ``execute`` is any :class:`ExecFn`, for example a bound
    ``workspace.execute``. See :func:`parse_capability_status` for what
    ``None`` means.
    """
    directory = audit_dir if audit_dir is not None else default_audit_dir(capability)
    _validated_name(capability)
    if not directory.startswith("/"):
        raise ValueError("audit_dir must be an absolute path in the workspace")
    result = await exec_argv(
        execute,
        ["sh", "-c", _READ_SCRIPT, "capability-status", directory],
        timeout=timeout,
    )
    if result.exit_code != 0:
        raise CapabilityStatusError(f"Reading {capability} status exited {result.exit_code}")
    host, _, rows = result.stdout.partition("\n")
    return parse_capability_status(rows, capability, host=host.strip() or None)
