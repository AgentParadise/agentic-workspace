"""Host-side, append-only output spool for one operation (plan section 5.2).

The spool lives in the provider's own process memory, outside every bind
mount, so a workload has no handle on it: it cannot truncate, rewrite or
regrow what was observed. Only the provider's collector appends, and the only
loss is the spool's own retention eviction, which it counts exactly. That is
why a provider backed by this spool declares `integrity: exact`.

A cursor is `<generation>:<offset>`, suffixed `:lossy` once the drain that
holds it has crossed a retention gap. The generation is fresh per spool, so a
cursor from another operation is recognised as foreign rather than read as an
offset into the wrong stream. Consumers treat cursors as opaque.

Credential values registered at construction never reach storage: each
channel is scanned before its bytes are stored, every occurrence is replaced
by `REDACTED`, and a channel's tail that could still be the start of a value
is held back until the next append or `finish` decides it. Offsets, `end`,
retention and `gap.lost_bytes` all count stored (redacted) bytes, so cursors
stay consistent whatever the marker's length. The rule is the same as the
Rust spool's, checked against `tests/fixtures/output_redaction/cases.json`.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Collection
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
    # A drain that reached this cursor lost bytes to retention on the way.
    lossy: bool = False


REDACTED = b"[REDACTED]"


class OutputRedactor:
    """Replaces registered values in a byte stream, whatever its chunking.

    The output is a function of the whole stream: at each position the
    longest registered value that matches is replaced, and a position whose
    match the stream has not yet decided is held until it is.
    """

    def __init__(self, values: Collection[str | bytes]) -> None:
        encoded = {v.encode() if isinstance(v, str) else bytes(v) for v in values}
        # An empty value hides nothing; longest first so it wins at a position.
        self._values = sorted((v for v in encoded if v), key=len, reverse=True)

    def __bool__(self) -> bool:
        return bool(self._values)

    def __repr__(self) -> str:
        # Never the values themselves.
        return f"OutputRedactor({len(self._values)} values)"

    def scan(self, data: bytes, *, final: bool) -> tuple[bytes, bytes]:
        """`(released, held)`: redacted bytes safe to store, and the raw tail
        still undecided. `final` decides everything, so nothing is held."""
        out = bytearray()
        i = 0
        while True:
            at, value = self._next(data, i, final)
            if at < 0:
                out += data[i:]
                return bytes(out), b""
            out += data[i:at]
            if value is None:
                return bytes(out), data[at:]
            out += REDACTED
            i = at + len(value)

    def _next(self, data: bytes, start: int, final: bool) -> tuple[int, bytes | None]:
        """The first position from `start` holding a whole value (and the
        longest such value), or undecided (`None`), or `-1` for neither."""
        best, found = -1, None
        for value in self._values:
            at = data.find(value, start)
            if at >= 0 and (best < 0 or at < best):
                best, found = at, value
        if final:
            return best, found
        n = len(data)
        last = n if best < 0 else best + 1
        for at in range(max(start, n - len(self._values[0]) + 1), last):
            tail = data[at:]
            # Undecided: a value longer than what is left could still start
            # here (at `best`, longer than the whole value found there).
            if any(len(v) > len(tail) and v.startswith(tail) for v in self._values):
                return at, None
        return best, found


class OperationSpool:
    """Append-only bytes with bounded retention and opaque, generation-tagged cursors.

    `redact` registers credential values that must never be stored or paged;
    with none registered the spool stores output exactly as appended.
    """

    def __init__(self, *, retention_bytes: int, redact: Collection[str | bytes] = ()) -> None:
        if retention_bytes < 1:
            raise ValueError("retention_bytes must be at least 1")
        self._retention = retention_bytes
        self._generation = uuid.uuid4().hex
        self._retained = bytearray()
        # Absolute offset of the first retained byte; everything before it was evicted.
        self._base = 0
        self._redactor = OutputRedactor(redact)
        # Per channel: raw bytes not yet stored because they may begin a value.
        self._held: dict[int, bytes] = {}

    @property
    def end(self) -> int:
        """Absolute offset one past the last byte ever appended."""
        return self._base + len(self._retained)

    def append(self, data: bytes, *, channel: int = 0) -> bytes:
        """Append collector output read from `channel`; return what was stored.

        Channels are redacted separately, since a value printed on one is not
        interleaved with another's bytes. What is returned is the redacted
        bytes this call stored, for a caller that keeps its own copy.
        """
        if not self._redactor:
            self._store(data)
            return data
        released, held = self._redactor.scan(self._held.pop(channel, b"") + data, final=False)
        if held:
            self._held[channel] = held
        self._store(released)
        return released

    def finish(self) -> dict[int, bytes]:
        """Decide and store every held tail: the collector has read its last
        byte. Returns what each channel stored. Call before the terminal
        state is published, so `complete` covers the final bytes."""
        stored = {}
        for channel, held in sorted(self._held.items()):
            stored[channel], _ = self._redactor.scan(held, final=True)
            self._store(stored[channel])
        self._held.clear()
        return stored

    def _store(self, data: bytes) -> None:
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
        Once a drain has crossed a gap, its page and every later page carry
        `loss_detected`: the loss travels in the cursors it was handed.
        """
        position = self._resolve(ref, cursor)
        start = position.offset
        gap: ObservationGap | None = None
        if start < self._base:
            gap = ObservationGap(lost_bytes=self._base - start)
            start = self._base
        lossy = position.lossy or gap is not None
        available = self.end - start
        # Start from the largest payload whose base64 could fit, then shrink
        # until the whole serialised envelope does.
        size = min(available, (max_page_bytes * 3) // 4)
        while True:
            page = self._build(ref, state, cursor, start, size, gap, lossy)
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
        lossy: bool,
    ) -> ObservationPage:
        lo = start - self._base
        data = bytes(self._retained[lo : lo + size])
        next_offset = start + size
        return ObservationPage(
            schema="apss.workspace-observation/v1",
            ref=ref,
            state=state,
            cursor=cursor or self._encode(0, lossy=False),
            next_cursor=self._encode(next_offset, lossy=lossy),
            data_b64=base64.b64encode(data).decode("ascii"),
            gap=MISSING if gap is None else gap,
            loss_detected=True if lossy else MISSING,
            complete=state in TERMINAL_STATES and next_offset == self.end and not self._held,
        )

    def _encode(self, offset: int, *, lossy: bool) -> str:
        return f"{self._generation}:{offset}" + (_LOSSY if lossy else "")

    def _resolve(self, ref: OperationRef, cursor: str | None) -> _Position:
        if cursor is None:
            return _Position(generation=self._generation, offset=0)
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
        return position


_LOSSY = ":lossy"


def _parse(cursor: str) -> _Position | None:
    lossy = cursor.endswith(_LOSSY)
    generation, sep, offset = cursor.removesuffix(_LOSSY).rpartition(":")
    if not sep or not generation or not offset.isdigit():
        return None
    return _Position(generation=generation, offset=int(offset), lossy=lossy)
