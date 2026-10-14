"""`drain_operation` never presents evicted output as complete.

Collected by CI's python-compatibility job (`uv run pytest -q`).
"""

from __future__ import annotations

from agentic_isolation.contract import (
    ObservationGap,
    ObservationPage,
    OperationRef,
    OperationStatus,
)
from agentic_isolation.providers.base import OBSERVATION_LOST_BYTES, drain_operation
from agentic_isolation.providers.operation_spool import OperationSpool

REF = OperationRef(
    workspace={"provider": "local", "resource_id": "ws"}, operation_id="op", operation_key="key"
)


def _status() -> OperationStatus:
    return OperationStatus(
        schema="apss.workspace-status/v1",
        ref=REF,
        state="succeeded",
        exit_code=0,
        started_at="now",
        ended_at="now",
        execution_id="test",
        metadata={"caller": "kept"},
    )


class _SpoolPort:
    """Pages from a real spool; `feed` appends between reads, as a live collector would."""

    def __init__(self, spool: OperationSpool, feed: list[bytes]) -> None:
        self.spool = spool
        self.feed = feed

    async def observe(
        self, ref: OperationRef, *, cursor: str | None, max_bytes: int, wait_seconds: float
    ) -> ObservationPage:
        state = "running" if self.feed else "succeeded"
        page = self.spool.page(ref, state=state, cursor=cursor, max_page_bytes=max_bytes)
        if self.feed:
            self.spool.append(self.feed.pop(0))
        return page

    async def status(self, ref: OperationRef) -> OperationStatus:
        return _status()


async def test_a_gap_on_the_first_page_is_reported() -> None:
    spool = OperationSpool(retention_bytes=4)
    spool.append(b"abcdefgh")
    status, data = await drain_operation(_SpoolPort(spool, []), REF)
    assert data == b"efgh"
    assert status.metadata == {"caller": "kept", OBSERVATION_LOST_BYTES: "4"}


async def test_gaps_on_later_pages_are_summed() -> None:
    spool = OperationSpool(retention_bytes=4)
    spool.append(b"ab")
    # Read "ab", then 6 more arrive and 2 are evicted; read "efgh", then 7 more
    # arrive and 3 are evicted before the next read.
    status, data = await drain_operation(_SpoolPort(spool, [b"cdefgh", b"ijklmno"]), REF)
    assert data == b"abefghlmno"
    assert status.metadata[OBSERVATION_LOST_BYTES] == "5"


async def test_a_gap_of_unknown_extent_is_reported_as_unknown() -> None:
    pages = [
        ObservationPage(
            ref=REF,
            state="running",
            cursor="c0",
            next_cursor="c1",
            data_b64="",
            gap=ObservationGap(lost_bytes=3),
            complete=False,
        ),
        ObservationPage(
            ref=REF,
            state="succeeded",
            cursor="c1",
            next_cursor="c2",
            data_b64="eA==",
            gap=ObservationGap(lost_bytes=None),
            complete=True,
        ),
    ]

    class Port:
        async def observe(self, ref: OperationRef, **_: object) -> ObservationPage:
            return pages.pop(0)

        async def status(self, ref: OperationRef) -> OperationStatus:
            return _status()

    status, data = await drain_operation(Port(), REF)
    assert data == b"x"
    assert status.metadata[OBSERVATION_LOST_BYTES] == "unknown"


async def test_an_unbroken_drain_adds_no_loss_indicator() -> None:
    spool = OperationSpool(retention_bytes=64)
    status, data = await drain_operation(_SpoolPort(spool, [b"abc", b"def"]), REF)
    assert data == b"abcdef"
    assert status.metadata == {"caller": "kept"}
