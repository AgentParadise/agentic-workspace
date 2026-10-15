"""`OperationSpool` pages carry `loss_detected` exactly when a drain lost bytes.

Lifecycle hazard LH-6 (APSS EXP-V1-0006). Both the local and Docker providers
page through this spool, so the rules are checked here, on the serialised page
a consumer reads:

(a) pages drained before any eviction carry no `loss_detected`;
(b) the page that reports a gap carries `loss_detected: true`;
(c) every later page of that drain carries it too, through the completing
    page, which has no gap;
(d) a fresh drain from `cursor=None` after eviction carries it on every page.

Collected by CI's python-compatibility job (`uv run pytest -q`).
"""

from __future__ import annotations

import base64
import json

from agentic_isolation.contract import ObservationPage, OperationRef
from agentic_isolation.providers.operation_spool import OperationSpool

REF = OperationRef.model_validate(
    {
        "workspace": {"provider": "local", "resource_id": "ws"},
        "operation_id": "op",
        "operation_key": "k",
    }
)
TOKEN = "invented-token"


def _wire(page: ObservationPage) -> dict:
    return json.loads(page.model_dump_json())


def _small_page(spool: OperationSpool) -> int:
    """A page bound a few payload bytes larger than a lossy envelope, so a
    drain of a few dozen bytes takes several pages."""
    whole = spool.page(REF, state="succeeded", cursor=None, max_page_bytes=4096)
    envelope = len(whole.model_dump_json().encode()) - len(whole.data_b64)
    return envelope + len(":lossy") + 12


def _drain(spool: OperationSpool, cursor: str | None, max_page_bytes: int) -> list[dict]:
    """Every page from `cursor` through the completing one, as serialised."""
    pages = []
    while True:
        page = spool.page(REF, state="succeeded", cursor=cursor, max_page_bytes=max_page_bytes)
        pages.append(_wire(page))
        cursor = page.next_cursor
        if page.complete:
            return pages


def test_a_pages_before_any_eviction_carry_no_loss() -> None:
    spool = OperationSpool(retention_bytes=64, redact=[TOKEN])
    spool.append(f"{TOKEN} tail-bytes and more".encode())
    spool.finish()
    pages = _drain(spool, None, _small_page(spool))
    assert len(pages) >= 3, pages
    assert b"".join(base64.b64decode(p["data_b64"]) for p in pages) == (
        b"[REDACTED] tail-bytes and more"
    )
    for page in pages:
        assert "gap" not in page
        assert "loss_detected" not in page


def test_b_the_gap_page_carries_loss() -> None:
    spool = OperationSpool(retention_bytes=64)
    spool.append(b"abcdefgh")
    first = spool.page(REF, state="running", cursor=None, max_page_bytes=4096)
    assert "loss_detected" not in _wire(first)
    # Evict past the cursor the reader holds.
    spool.append(b"x" * 72)
    page = _wire(spool.page(REF, state="running", cursor=first.next_cursor, max_page_bytes=4096))
    assert page["gap"] == {"lost_bytes": 8}
    assert page["loss_detected"] is True


def test_c_loss_is_sticky_to_the_completing_page_without_a_gap() -> None:
    spool = OperationSpool(retention_bytes=40, redact=[TOKEN])
    spool.append(b"head ")
    first = spool.page(REF, state="running", cursor=None, max_page_bytes=4096)
    # The evicted range holds a redaction marker: offsets count the stored
    # "[REDACTED]", not the raw value.
    spool.append(f"{TOKEN} middle {TOKEN} and then the tail-bytes".encode())
    spool.finish()
    stored = b"head [REDACTED] middle [REDACTED] and then the tail-bytes"
    pages = _drain(spool, first.next_cursor, _small_page(spool))
    assert pages[0]["gap"] == {"lost_bytes": len(stored) - 40 - len(b"head ")}
    assert len(pages) >= 3, pages
    for index, page in enumerate(pages):
        assert page.get("loss_detected") is True, index
    for page in pages[1:]:
        assert "gap" not in page
    assert pages[-1]["complete"] is True


def test_d_a_fresh_drain_after_eviction_carries_loss_on_every_page() -> None:
    spool = OperationSpool(retention_bytes=40, redact=[TOKEN])
    spool.append(f"head {TOKEN} middle {TOKEN} and then the tail-bytes".encode())
    spool.finish()
    pages = _drain(spool, None, _small_page(spool))
    assert len(pages) >= 3, pages
    assert (
        b"".join(base64.b64decode(p["data_b64"]) for p in pages)
        == (b"head [REDACTED] middle [REDACTED] and then the tail-bytes"[-40:])
    )
    for index, page in enumerate(pages):
        assert page.get("loss_detected") is True, index
