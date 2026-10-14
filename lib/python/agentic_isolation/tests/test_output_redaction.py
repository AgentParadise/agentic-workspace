"""Registered credential values never reach spool storage, pages or final output.

The rule is shared with the Rust spool through
`tests/fixtures/output_redaction/cases.json` at the repository root. Every
secret here is invented. Collected by CI's python-compatibility job.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import (
    CredentialBinding,
    CredentialRef,
    OperationRef,
)
from agentic_isolation.providers.base import run_to_completion
from agentic_isolation.providers.local import WorkspaceLocalProvider
from agentic_isolation.providers.operation_spool import REDACTED, OperationSpool
from tests.test_docker_operations import (
    SECRET,
    _data,
    _ops,
    _pages,
    _spec,
    _workspace,
    docker_env,  # noqa: F401  # pytest fixture
)

_FIXTURES = json.loads(
    (Path(__file__).parents[4] / "tests/fixtures/output_redaction/cases.json").read_text()
)
_CASES = {case["name"]: case for case in _FIXTURES["cases"]}
_CHANNEL_CASES = {case["name"]: case for case in _FIXTURES["channel_cases"]}
CANARY = "canary-INVENTED-sk-7f3a"
REF = OperationRef.model_validate(
    {
        "workspace": {"provider": "local", "resource_id": "ws"},
        "operation_id": "op",
        "operation_key": "k",
    }
)


def _drain(spool: OperationSpool, *, state: str = "succeeded", page: int = 4096) -> bytes:
    out, cursor = b"", None
    while True:
        observed = spool.page(REF, state=state, cursor=cursor, max_page_bytes=page)  # type: ignore[arg-type]
        out += base64.b64decode(observed.data_b64)
        cursor = observed.next_cursor
        if observed.complete:
            assert int(cursor.rsplit(":", 1)[1]) == spool.end
            return out


def test_the_marker_is_the_shared_fixture_marker() -> None:
    assert _FIXTURES["marker"].encode() == REDACTED


@pytest.mark.parametrize("name", sorted(_CASES))
def test_every_split_of_every_fixture_stores_the_same_redacted_bytes(name: str) -> None:
    case = _CASES[name]
    output, stored = case["output"].encode(), case["stored"].encode()
    splits = [[output]] + [[output[:k], output[k:]] for k in range(len(output) + 1)]
    splits.append([output[i : i + 1] for i in range(len(output))])
    for chunks in splits:
        spool = OperationSpool(retention_bytes=1 << 20, redact=case["values"])
        for chunk in chunks:
            spool.append(chunk)
        spool.finish()
        assert bytes(spool._retained) == stored, chunks
        assert _drain(spool, page=320) == stored, chunks
        assert spool.end == len(stored)


def _channel_splits(appends: list[list]) -> list[list[tuple[int, bytes]]]:
    """The appends as given, then with each one split at every offset."""
    whole = [(channel, text.encode()) for channel, text in appends]
    splits = [whole]
    for n, (channel, data) in enumerate(whole):
        for k in range(len(data) + 1):
            splits.append([*whole[:n], (channel, data[:k]), (channel, data[k:]), *whole[n + 1 :]])
    return splits


@pytest.mark.parametrize("name", sorted(_CHANNEL_CASES))
def test_channels_merged_into_storage_never_form_a_value(name: str) -> None:
    case = _CHANNEL_CASES[name]
    stored = case["stored"].encode()
    for appends in _channel_splits(case["appends"]):
        spool = OperationSpool(retention_bytes=1 << 20, redact=case["values"])
        copies: dict[int, bytes] = {}
        for channel, chunk in appends:
            copies[channel] = copies.get(channel, b"") + spool.append(chunk, channel=channel)
        for channel, tail in spool.finish().items():
            copies[channel] += tail
        assert bytes(spool._retained) == stored, appends
        assert _drain(spool, page=320) == stored, appends
        assert spool.end == len(stored)
        for value in case["values"]:
            for copy in copies.values():
                assert value.encode() not in copy, appends


def test_a_value_split_across_interleaved_channels_is_redacted() -> None:
    spool = OperationSpool(retention_bytes=1 << 20, redact=[CANARY])
    spool.append(b"out:" + CANARY[:9].encode(), channel=0)
    spool.append(b"err line\n", channel=1)
    spool.append(CANARY[9:].encode() + b"\n", channel=0)
    spool.finish()
    assert _drain(spool) == b"out:err line\n[REDACTED]\n"


def test_held_bytes_are_neither_counted_nor_complete_until_finished() -> None:
    spool = OperationSpool(retention_bytes=1 << 20, redact=[CANARY])
    spool.append(b"ok " + CANARY[:5].encode())
    # The undecided tail is not stored, so no cursor can point into it.
    assert spool.end == 3
    page = spool.page(REF, state="succeeded", cursor=None, max_page_bytes=4096)
    page = spool.page(REF, state="succeeded", cursor=page.next_cursor, max_page_bytes=4096)
    assert not page.complete
    spool.finish()
    assert _drain(spool) == b"ok " + CANARY[:5].encode()


def test_retention_and_gaps_count_stored_bytes() -> None:
    spool = OperationSpool(retention_bytes=12, redact=[CANARY])
    spool.append(f"{CANARY} tail-bytes".encode())
    spool.finish()
    stored = b"[REDACTED] tail-bytes"
    assert spool.end == len(stored)
    page = spool.page(REF, state="succeeded", cursor=None, max_page_bytes=4096)
    assert page.gap.lost_bytes == len(stored) - 12  # type: ignore[union-attr]
    assert base64.b64decode(page.data_b64) == stored[-12:]


def test_registered_values_are_never_in_the_repr() -> None:
    spool = OperationSpool(retention_bytes=8, redact=[CANARY])
    assert CANARY not in repr(spool._redactor)


# A workload that prints the canary whole, split across two writes with a
# pause between them so the collector reads them separately, and repeated, on
# both channels, plus the value a credential binding resolved to.
_CANARY_SCRIPT = (
    f"printf 'whole {CANARY}\\n'; "
    f"printf 'split {CANARY[:11]}'; sleep 0.3; printf '{CANARY[11:]}\\n'; "
    f"printf '{CANARY}{CANARY} rep\\n' >&2; "
    'printf "bound $AW_TOKEN\\n"; '
    f"printf 'tail {CANARY[:6]}'"
)
_BOUND = (
    CredentialBinding(
        ref=CredentialRef(name="invented", scope="test"), delivery="env", target="AW_TOKEN"
    ),
)


def _assert_clean(data: bytes) -> None:
    assert CANARY.encode() not in data
    assert SECRET.encode() not in data
    for k in range(4, len(CANARY)):
        assert CANARY[:k].encode() + CANARY[k:].encode() not in data


async def test_docker_pages_spool_and_channels_never_carry_the_canary(
    docker_env: Path,  # noqa: F811
) -> None:
    ops = _ops(docker_env)
    ref = await ops.start(
        _workspace(),
        _spec(_CANARY_SCRIPT, credential_bindings=_BOUND),
        separate_channels=True,
        redact=[CANARY],
    )
    data = _data(await _pages(ops, ref))
    stdout, stderr = ops.channels(ref)
    spool = ops._operations[ref.operation_id].spool
    for observed in (data, stdout, stderr, bytes(spool._retained)):
        _assert_clean(observed)
    # The undecided prefix at EOF is stored by `finish`, not dropped.
    assert stdout == (
        b"whole [REDACTED]\nsplit [REDACTED]\nbound [REDACTED]\ntail " + CANARY[:6].encode()
    )
    assert stderr == b"[REDACTED][REDACTED] rep\n"
    assert data.count(REDACTED) == 5


async def test_docker_without_registered_values_stores_output_verbatim(
    docker_env: Path,  # noqa: F811
) -> None:
    ops = _ops(docker_env)
    ref = await ops.start(_workspace(), _spec(f"printf '{CANARY}'"))
    assert _data(await _pages(ops, ref)) == CANARY.encode()


async def test_local_final_output_never_carries_the_canary(tmp_path: Path) -> None:
    class Resolver:
        async def resolve(self, ref: CredentialRef) -> str:
            return SECRET

    provider = WorkspaceLocalProvider(
        base_dir=tmp_path, allow_insecure=True, credentials=Resolver()
    )
    workspace = await provider.create(WorkspaceConfig())
    try:
        status, data = await asyncio.wait_for(
            run_to_completion(
                provider,
                workspace,
                _spec(_CANARY_SCRIPT, credential_bindings=_BOUND),
                redact=[CANARY],
            ),
            30,
        )
        assert status.state == "succeeded"
        _assert_clean(data)
        assert data.count(REDACTED) == 5
        assert data.endswith(b"tail " + CANARY[:6].encode())
        spool = next(iter(provider._operations._operations.values())).spool
        _assert_clean(bytes(spool._retained))
    finally:
        await provider.destroy(workspace)


class _OldProvider:
    """A provider whose `start` predates the `redact` option."""

    def __init__(self, inner: WorkspaceLocalProvider) -> None:
        self._inner = inner

    async def start(self, workspace, spec):  # type: ignore[no-untyped-def]
        return await self._inner.start(workspace, spec)

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)


async def test_an_unchanged_caller_still_runs_on_a_provider_without_redact(
    tmp_path: Path,
) -> None:
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    try:
        status, data = await asyncio.wait_for(
            run_to_completion(_OldProvider(provider), workspace, _spec("printf 'plain\\n'")),  # type: ignore[arg-type]
            30,
        )
        assert status.state == "succeeded"
        assert data == b"plain\n"
    finally:
        await provider.destroy(workspace)
