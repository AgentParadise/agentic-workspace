"""The host-side reader for the entrypoint's capability status rows (#27).

The rows themselves are written by workspace/entrypoint.sh section 5.7 and are
pinned end to end by tests/integration/test_entrypoint_capabilities.py. These
tests pin the parse: what counts as the current verdict, what is ignored, and
what is refused loudly.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from agentic_isolation.capability_status import (
    STATUS_RECORD,
    CapabilityState,
    CapabilityStatus,
    CapabilityStatusError,
    FailurePolicy,
    default_audit_dir,
    parse_capability_status,
    read_capability_status,
)
from agentic_isolation.providers.base import ExecuteResult

HOST = "ws-1234"


def row(**overrides: object) -> str:
    base: dict[str, object] = {
        "schema_version": 1,
        "record": STATUS_RECORD,
        "capability": "session-store",
        "provider": "apss",
        "status": "degraded",
        "policy": "degrade",
        "required": False,
        "doctor_exit": 1,
        "failed_checks": ["store_reachable"],
        "host": HOST,
        "at": "2026-10-02T12:00:00Z",
    }
    base.update(overrides)
    # Compact, exactly as entrypoint.sh 5.7 writes it.
    return json.dumps(base, separators=(",", ":"))


DOCTOR_ROW = json.dumps({"capability": "session-store", "passed": False, "checks": []})


def result(stdout: str, exit_code: int = 0) -> ExecuteResult:
    return ExecuteResult(exit_code=exit_code, stdout=stdout, stderr="", duration_ms=0)


def test_the_last_row_for_this_capability_and_host_is_the_verdict() -> None:
    text = "\n".join(
        [
            row(status="ready", failed_checks=[], doctor_exit=0, host="other-ws"),
            DOCTOR_ROW,
            row(),
            row(capability="memory", status="failed", policy="fail"),
            "",
        ]
    )
    status = parse_capability_status(text, "session-store", host=HOST)
    assert status is not None
    assert status.status is CapabilityState.DEGRADED
    assert status.policy is FailurePolicy.DEGRADE
    assert status.degraded is True
    assert status.failed_checks == ("store_reachable",)
    assert status.at == datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def test_rows_from_another_container_are_not_this_workspaces_verdict() -> None:
    text = row(host="other-ws")
    assert parse_capability_status(text, "session-store", host=HOST) is None


def test_no_row_means_unknown_not_ready() -> None:
    # A doctor payload alone says nothing about what the lifecycle decided.
    assert parse_capability_status(DOCTOR_ROW, "session-store", host=HOST) is None
    assert parse_capability_status("", "session-store", host=HOST) is None


def test_a_torn_or_foreign_line_is_ignored() -> None:
    text = "\n".join(['{"capability": "session-st', "not json", "[1, 2]", row()])
    status = parse_capability_status(text, "session-store", host=HOST)
    assert status is not None and status.degraded


def test_a_malformed_status_row_is_a_contract_violation() -> None:
    with pytest.raises(CapabilityStatusError):
        parse_capability_status(row(status="sort-of"), "session-store", host=HOST)
    with pytest.raises(CapabilityStatusError):
        parse_capability_status(row(surprise=True), "session-store", host=HOST)


def test_a_ready_row_is_not_degraded() -> None:
    status = parse_capability_status(
        row(status="ready", failed_checks=[], doctor_exit=0),
        "session-store",
        host=HOST,
    )
    assert isinstance(status, CapabilityStatus)
    assert status.degraded is False


def test_default_audit_dir_matches_the_entrypoint() -> None:
    assert default_audit_dir("session-store") == "/var/agentic/session-store-doctor"


@pytest.mark.parametrize("name", ["", "../etc", "a b", "Session-Store", "a/b"])
def test_capability_name_is_validated_before_it_reaches_a_path(name: str) -> None:
    with pytest.raises(ValueError):
        default_audit_dir(name)


@pytest.mark.asyncio
async def test_reader_reads_host_then_rows_through_one_exec() -> None:
    execute = AsyncMock(return_value=result(f"{HOST}\n{DOCTOR_ROW}\n{row()}\n"))
    status = await read_capability_status(execute, "session-store")
    assert status is not None and status.degraded
    command = execute.await_args.args[0]
    assert "/var/agentic/session-store-doctor" in command


@pytest.mark.asyncio
async def test_reader_honours_an_explicit_audit_dir() -> None:
    execute = AsyncMock(return_value=result(f"{HOST}\n{row()}\n"))
    await read_capability_status(execute, "session-store", audit_dir="/audit")
    assert "/audit" in execute.await_args.args[0]


@pytest.mark.asyncio
async def test_reader_refuses_a_relative_audit_dir() -> None:
    with pytest.raises(ValueError):
        await read_capability_status(AsyncMock(), "session-store", audit_dir="audit")


@pytest.mark.asyncio
async def test_a_failed_read_is_an_error_not_unknown() -> None:
    execute = AsyncMock(return_value=result("", exit_code=126))
    with pytest.raises(CapabilityStatusError):
        await read_capability_status(execute, "session-store")


def test_a_malformed_row_from_another_container_is_not_this_workspaces_problem() -> None:
    text = "\n".join([row(host="other-ws", status="sort-of"), row()])
    status = parse_capability_status(text, "session-store", host=HOST)
    assert status is not None and status.degraded


@pytest.mark.asyncio
async def test_reader_selects_status_rows_before_bounding_the_read() -> None:
    execute = AsyncMock(return_value=result(f"{HOST}\n{row()}\n"))
    await read_capability_status(execute, "session-store")
    command = execute.await_args.args[0]
    # The byte bound applies to status rows only, so a large doctor payload
    # (or many of them) can never push this workspace's verdict out.
    assert command.index("capability_status") < command.index("tail -c")


async def _local_sh(
    command: str,
    *,
    timeout: float | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> ExecuteResult:
    """Run the reader's real shell script on this machine."""
    import subprocess

    done = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=30)
    return ExecuteResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)


@pytest.mark.asyncio
async def test_other_containers_rows_cannot_push_this_verdict_out(tmp_path) -> None:
    """A shared audit dir with > 1 MiB of other hosts' rows written later."""
    probe = await _local_sh(
        "cat /proc/sys/kernel/hostname 2>/dev/null || printf '%s\\n' \"${HOSTNAME:-}\""
    )
    me = "".join(c for c in probe.stdout.strip() if c.isalnum() or c in "._-")
    assert me, "the test host reports no hostname"
    foreign = row(host="other-ws", status="ready", failed_checks=[], doctor_exit=0)
    copies = (1024 * 1024) // len(foreign) + 100
    (tmp_path / "2026-10-02.jsonl").write_text(row(host=me) + "\n" + (foreign + "\n") * copies)
    status = await read_capability_status(_local_sh, "session-store", audit_dir=str(tmp_path))
    assert status is not None and status.degraded
