"""What a teardown reports: how finalization ended, and what the workspace used.

`TeardownReport` is the typed receipt `SupportsStagedTeardown.teardown` and
Docker's `destroy` return (plan section 4.4). It is a provider-side value, not
an APSS document: `finalization` is the contract's `FinalizationOutcome`, and
`usage` is what the provider could measure before the workspace went away.

`TeardownUsage` field names match Syntropic137's `TeardownReportLike` one for
one (syn `teardown_usage.py`), so syn maps `report.usage` without a rename
table. Every field is `None` when the provider did not measure it; `None` is
"unknown", never zero.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_isolation.contract import FinalizationOutcome


@dataclass(frozen=True)
class TeardownUsage:
    """Resource usage measured at teardown. `None` means not measured."""

    cpu_usage_seconds: float | None = None
    cpu_throttled_seconds: float | None = None
    nr_throttled: int | None = None
    memory_peak_bytes: int | None = None
    #: The cgroup's `oom_kill` count: processes the kernel killed for memory.
    oom_kills: int | None = None
    #: True when ANY source saw an OOM kill: `oom_kills > 0`, or Docker's
    #: `State.OOMKilled`. Kept beside the count because Docker can know a kill
    #: happened when the cgroup counters could not be read.
    oom_killed: bool | None = None
    disk_bytes_at_teardown: int | None = None
    delete_failures: tuple[str, ...] | None = None
    net_rx_bytes: int | None = None
    net_tx_bytes: int | None = None


@dataclass(frozen=True)
class TeardownReport:
    """The outcome of one teardown call.

    `finalization.state` is what a caller dispatches on:
    - `completed`: the workspace is gone.
    - `retained`: `before_delete` failed after the container was removed. The
      workspace directory is kept, and `finalization.retained` is the ref a
      retry passes back to `teardown`.
    - `failed` at `while_running`: nothing was destroyed and the workspace is
      still registered, so the same call can be retried.
    """

    finalization: FinalizationOutcome
    usage: TeardownUsage
