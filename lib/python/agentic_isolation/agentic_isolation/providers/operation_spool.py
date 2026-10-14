"""Host-side, append-only output spool for one operation (plan section 5.2).

The spool lives in the provider's own process memory, outside every bind
mount, so a workload has no handle on it: it cannot truncate, rewrite or
regrow what was observed. Only the provider's collector appends, and the only
loss is the spool's own retention eviction, which it counts exactly. That is
why a provider backed by this spool declares `integrity: exact`.

A cursor is `<generation>:<offset>`. The generation is fresh per spool, so a
cursor from another operation is recognised as foreign rather than read as an
offset into the wrong stream. Consumers treat cursors as opaque.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass

from agentic_isolation.contract import (
    MISSING,
    TERMINAL_STATES,
    ObservationGap,
    ObservationPage,
    OperationError,
    OperationRef,
    OperationState,
)


class OperationFailure(Exception):
    """A typed operation failure. Callers dispatch on `error.kind`."""

    def __init__(self, error: OperationError) -> None:
        super().__init__(f"{error.kind}: {error.message}")
        self.error = error


@dataclass(frozen=True)
class _Position:
    generation: str
    offset: int


class OperationSpool:
    """Append-only bytes with bounded retention and opaque, generation-tagged cursors."""

    def __init__(self, *, retention_bytes: int) -> None:
        if retention_bytes < 1:
            raise ValueError("retention_bytes must be at least 1")
        self._retention = retention_bytes
        self._generation = uuid.uuid4().hex
        self._retained = bytearray()
        # Absolute offset of the first retained byte; everything before it was evicted.
        self._base = 0

    @property
    def end(self) -> int:
        """Absolute offset one past the last byte ever appended."""
        return self._base + len(self._retained)

    def append(self, data: bytes) -> None:
        """Append collector output, evicting the oldest bytes beyond retention."""
        self._retained.extend(data)
        excess = len(self._retained) - self._retention
        if excess > 0:
            del self._retained[:excess]
            self._base += excess

    def page(
        self,
        ref: OperationRef,
        *,
        state: OperationState,
        cursor: str | None,
        max_page_bytes: int,
    ) -> ObservationPage:
        """The next page after `cursor`, serialised to at most `max_page_bytes`.

        Raises OperationFailure(unknown_operation) for a cursor this spool did
        not issue. A cursor older than retention resumes at the oldest retained
        byte and reports the evicted count as `gap.lost_bytes`. The page is
        `complete` only when `state` is terminal and nothing remains to read.
        """
        start = self._resolve(ref, cursor)
        gap: ObservationGap | None = None
        if start < self._base:
            gap = ObservationGap(lost_bytes=self._base - start)
            start = self._base
        available = self.end - start
        # Start from the largest payload whose base64 could fit, then shrink
        # until the whole serialised envelope does.
        size = min(available, (max_page_bytes * 3) // 4)
        while True:
            page = self._build(ref, state, cursor, start, size, gap)
            if len(page.model_dump_json().encode()) <= max_page_bytes:
                return page
            if size == 0:
                raise OperationFailure(
                    OperationError(
                        kind="rejected_policy",
                        message=(
                            f"max_page_bytes={max_page_bytes} cannot hold an empty "
                            "page envelope; request a larger page"
                        ),
                        ref=ref,
                    )
                )
            size = max(0, size - max(3, size // 8))

    def _build(
        self,
        ref: OperationRef,
        state: OperationState,
        cursor: str | None,
        start: int,
        size: int,
        gap: ObservationGap | None,
    ) -> ObservationPage:
        lo = start - self._base
        data = bytes(self._retained[lo : lo + size])
        next_offset = start + size
        return ObservationPage(
            schema="apss.workspace-observation/v1",
            ref=ref,
            state=state,
            cursor=cursor or self._encode(0),
            next_cursor=self._encode(next_offset),
            data_b64=base64.b64encode(data).decode("ascii"),
            gap=MISSING if gap is None else gap,
            complete=state in TERMINAL_STATES and next_offset == self.end,
        )

    def _encode(self, offset: int) -> str:
        return f"{self._generation}:{offset}"

    def _resolve(self, ref: OperationRef, cursor: str | None) -> int:
        if cursor is None:
            return 0
        position = _parse(cursor)
        if position is None or position.generation != self._generation:
            raise OperationFailure(
                OperationError(
                    kind="unknown_operation",
                    message=f"cursor {cursor!r} was not issued for operation {ref.operation_id}",
                    ref=ref,
                )
            )
        if position.offset > self.end:
            raise OperationFailure(
                OperationError(
                    kind="unknown_operation",
                    message=f"cursor {cursor!r} is beyond the end of operation {ref.operation_id}",
                    ref=ref,
                )
            )
        return position.offset


def _parse(cursor: str) -> _Position | None:
    generation, sep, offset = cursor.rpartition(":")
    if not sep or not generation or not offset.isdigit():
        return None
    return _Position(generation=generation, offset=int(offset))
