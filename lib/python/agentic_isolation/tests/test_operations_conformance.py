"""Provider-neutral `SupportsOperations` conformance, run for Docker and Local.

Plan section 7 row 4: the protocol-level checks of the row 3 suite (a, c, e,
f, g, h, i, j, k, m, unknown ref, run_to_completion), driven only through the
`SupportsOperations` surface of each provider. Docker runs through the stand-in
`docker` of `test_docker_operations`; its Docker-only checks (client argv,
daemon presence, namespaces) stay there. Local-only checks (the W-6.2 opt-in
guard, the pgid guard, escaped descendants, destroy) follow the suite.

Cancel tests spawn process groups: run locally as `setsid -w uv run pytest`.
Each test is bounded by `asyncio.timeout` instead of a pytest-timeout plugin.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import stat
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_isolation import AgenticWorkspace
from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import (
    LaunchMarker,
    ObservationPage,
    OperationRef,
    OperationSpec,
    ProviderDescriptor,
)
from agentic_isolation.providers import local_operations
from agentic_isolation.providers.base import (
    SupportsOperations,
    Workspace,
    drain_operation,
    run_to_completion,
)
from agentic_isolation.providers.docker import WorkspaceDockerProvider
from agentic_isolation.providers.docker_operations import DockerOperations
from agentic_isolation.providers.local import InsecureProviderError, WorkspaceLocalProvider
from agentic_isolation.providers.local_operations import LocalOperations, unsafe_group
from agentic_isolation.providers.operation_spool import OperationFailure
from tests.test_docker_operations import FAKE_DOCKER

FIXTURES = Path(__file__).parent / "fixtures" / "apss_exp_v1_0006" / "valid" / "workspace-provider"
TEST_SECONDS = 30

Harness = tuple[SupportsOperations, Workspace, Path]
Factory = Callable[..., Awaitable[Harness]]


def _spec(script: str, *, key: str = "k1", **overrides: object) -> OperationSpec:
    fields: dict[str, object] = {
        "execution_id": "exec-1",
        "operation_key": key,
        "argv": ("sh", "-c", script),
        "working_directory": ".",
        "environment": {},
        "credential_bindings": (),
        "timeout_seconds": 60,
        "metadata": {},
    }
    fields.update(overrides)
    return OperationSpec.model_validate(fields)


@asynccontextmanager
async def _bounded() -> AsyncIterator[None]:
    async with asyncio.timeout(TEST_SECONDS):
        yield


@pytest.fixture(params=["docker", "local"])
async def make(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Factory]:
    """Build a provider of the parametrised kind with the given spool bounds."""
    built: list[tuple[WorkspaceLocalProvider, Workspace]] = []

    async def factory(*, retention: int = 1 << 20, page: int = 1 << 16) -> Harness:
        if request.param == "docker":
            fake = tmp_path / "docker"
            fake.write_text(FAKE_DOCKER)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
            monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "state"))
            monkeypatch.chdir(tmp_path)
            docker = WorkspaceDockerProvider()
            docker._operations = DockerOperations(
                credentials=None, retention_bytes=retention, max_page_bytes=page, docker=str(fake)
            )
            workspace = Workspace(
                id="ws1", provider="docker", path=Path("."), config=WorkspaceConfig(), _handle="c1"
            )
            return docker, workspace, tmp_path
        local = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
        local._operations = LocalOperations(
            credentials=None, retention_bytes=retention, max_page_bytes=page
        )
        workspace = await local.create(WorkspaceConfig())
        built.append((local, workspace))
        return local, workspace, tmp_path

    yield factory
    for local, workspace in built:
        await local.destroy(workspace)


async def _pages(ops: SupportsOperations, ref: OperationRef, max_bytes: int = 1 << 16) -> list[str]:
    out: list[str] = []
    cursor: str | None = None
    while True:
        page = await ops.observe(ref, cursor=cursor, max_bytes=max_bytes, wait_seconds=1.0)
        out.append(page.model_dump_json())
        cursor = page.next_cursor
        if page.complete:
            return out


def _data(pages: list[str]) -> bytes:
    return b"".join(
        base64.b64decode(ObservationPage.model_validate_json(p).data_b64) for p in pages
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return "\nState:\tZ" not in Path(f"/proc/{pid}/status").read_text()


async def _wait_for_pids(path: Path, count: int) -> list[int]:
    while not (path.exists() and len(path.read_text().split()) == count):
        await asyncio.sleep(0.05)
    return [int(p) for p in path.read_text().split()]


async def test_the_provider_supports_operations_and_publishes_a_valid_descriptor(
    make: Factory,
) -> None:
    ops, _, _ = await make()
    assert isinstance(ops, SupportsOperations)
    descriptor = ops.describe()
    assert ProviderDescriptor.model_validate_json(descriptor.model_dump_json()) == descriptor


async def test_a_start_returns_promptly_while_running(make: Factory) -> None:
    ops, workspace, _ = await make()
    async with _bounded():
        began = time.monotonic()
        ref = await ops.start(workspace, _spec("sleep 30"))
        assert time.monotonic() - began < 2
        assert (await ops.status(ref)).state == "running"
        assert (await ops.cancel(ref, grace_seconds=0.5)).state == "cancelled"


async def test_b_concurrent_same_key_starts_launch_once(make: Factory) -> None:
    ops, workspace, tmp = await make()
    counter = tmp / "launches"
    spec = _spec(f"echo x >> {counter}")
    async with _bounded():
        refs = await asyncio.gather(*(ops.start(workspace, spec) for _ in range(50)))
        assert len(set(refs)) == 1
        await drain_operation(ops, refs[0])
    assert counter.read_text() == "x\n"


async def test_c_same_key_different_argv_is_conflict(make: Factory) -> None:
    ops, workspace, _ = await make()
    async with _bounded():
        ref = await ops.start(workspace, _spec("true"))
        with pytest.raises(OperationFailure) as caught:
            await ops.start(workspace, _spec("false"))
        assert caught.value.error.kind == "conflict"
        await drain_operation(ops, ref)


async def test_e_marker_line_is_first_and_a_forged_one_is_not(make: Factory) -> None:
    ops, workspace, _ = await make()
    marker = LaunchMarker(argv0="aw-launch-7f3", line="AW_LAUNCH nonce-91c")
    async with _bounded():
        ref = await ops.start(
            workspace, _spec('echo "AW_LAUNCH nonce-91c"; echo work', launch_marker=marker)
        )
        lines = _data(await _pages(ops, ref)).decode().splitlines()
    assert lines == [marker.line, marker.line, "work"]


async def test_f_rewriting_in_workspace_output_cannot_change_observed_bytes(
    make: Factory,
) -> None:
    ops, workspace, tmp = await make()
    decoy = tmp / "output.log"
    script = (
        f"echo one | tee {decoy}; echo two | tee -a {decoy}; "
        f": > {decoy}; echo forged > {decoy}; echo three"
    )
    async with _bounded():
        ref = await ops.start(workspace, _spec(script))
        assert _data(await _pages(ops, ref)) == b"one\ntwo\nthree\n"
    assert decoy.read_text() == "forged\n"


async def test_g_cursor_older_than_retention_reports_exact_gap(make: Factory) -> None:
    ops, workspace, _ = await make(retention=100)
    async with _bounded():
        ref = await ops.start(workspace, _spec("head -c 250 /dev/zero | tr '\\0' a"))
        first = await ops.observe(ref, cursor=None, max_bytes=1 << 16, wait_seconds=0)
        await drain_operation(ops, ref)
        page = await ops.observe(ref, cursor=first.cursor, max_bytes=1 << 16)
    assert page.gap.lost_bytes == 150
    assert base64.b64decode(page.data_b64) == b"a" * 100


async def test_h_foreign_generation_cursor_is_unknown_operation(make: Factory) -> None:
    ops, workspace, _ = await make()
    async with _bounded():
        one = await ops.start(workspace, _spec("printf one", key="one"))
        two = await ops.start(workspace, _spec("printf 'thirteen byte'", key="two"))
        _, first = await drain_operation(ops, one)
        _, second = await drain_operation(ops, two)
        assert (len(first), len(second)) == (3, 13)
        foreign = (await ops.observe(one, cursor=None, max_bytes=4096)).next_cursor
        with pytest.raises(OperationFailure) as caught:
            await ops.observe(two, cursor=foreign, max_bytes=4096)
    assert caught.value.error.kind == "unknown_operation"


async def test_i_every_serialised_page_fits_max_page_bytes(make: Factory) -> None:
    ops, workspace, _ = await make(page=700)
    async with _bounded():
        ref = await ops.start(workspace, _spec("head -c 20000 /dev/urandom"))
        pages = await _pages(ops, ref, max_bytes=100_000)
    assert len(pages) > 10
    assert all(len(p.encode()) <= 700 for p in pages)
    assert len(_data(pages)) == 20000


async def test_j_late_read_drains_the_rest_and_only_then_completes(make: Factory) -> None:
    ops, workspace, _ = await make(page=600)
    async with _bounded():
        ref = await ops.start(workspace, _spec("head -c 3000 /dev/zero | tr '\\0' b"))
        while (await ops.status(ref)).state == "running":
            await asyncio.sleep(0.05)
        pages = await _pages(ops, ref)
    parsed = [ObservationPage.model_validate_json(p) for p in pages]
    assert [p.complete for p in parsed] == [False] * (len(parsed) - 1) + [True]
    assert _data(pages) == b"b" * 3000


async def test_k_cancel_kills_leader_grandchild_and_term_trapper(make: Factory) -> None:
    ops, workspace, tmp = await make()
    pids = tmp / "pids"
    script = (
        f"(sh -c 'echo $$ >> {pids}; exec sleep 300' &); "
        f"sh -c 'trap \"\" TERM; echo $$ >> {pids}; exec sleep 300' >/dev/null 2>&1 & "
        f"echo $$ >> {pids}; exec sleep 300"
    )
    async with _bounded():
        ref = await ops.start(workspace, _spec(script))
        recorded = await _wait_for_pids(pids, 3)
        assert all(_alive(p) for p in recorded)
        assert (await ops.cancel(ref, grace_seconds=0.5)).state == "cancelled"
        assert not [p for p in recorded if _alive(p)]
        assert (await ops.cancel(ref, grace_seconds=0.5)).state == "cancelled"


async def test_m_fake_exit_file_does_not_change_true_status(make: Factory) -> None:
    ops, workspace, _ = await make()
    script = "echo 0 > exit_code; echo succeeded > status; printf 'exit 0\\n'; exit 7"
    async with _bounded():
        ref = await ops.start(workspace, _spec(script))
        await drain_operation(ops, ref)
        status = await ops.status(ref)
    assert (status.state, status.exit_code) == ("failed", 7)


async def test_a_signalled_workload_reports_its_signal(make: Factory) -> None:
    ops, workspace, _ = await make()
    async with _bounded():
        ref = await ops.start(workspace, _spec("kill -KILL $$"))
        await drain_operation(ops, ref)
        status = await ops.status(ref)
    assert (status.state, status.exit_code, status.signal) == ("failed", 137, "SIGKILL")


async def test_timeout_settles_as_timed_out_with_the_final_output(make: Factory) -> None:
    ops, workspace, _ = await make()
    script = "trap 'echo final; exit 0' TERM; echo initial; while :; do sleep 0.1; done"
    async with _bounded():
        ref = await ops.start(workspace, _spec(script, timeout_seconds=1))
        status, output = await drain_operation(ops, ref)
    assert status.state == "timed_out"
    assert output.startswith(b"initial\n") and output.endswith(b"final\n")


async def test_unknown_ref_is_unknown_operation(make: Factory) -> None:
    ops, workspace, _ = await make()
    async with _bounded():
        ref = await ops.start(workspace, _spec("true"))
        stranger = ref.model_copy(update={"operation_id": "nope"})
        with pytest.raises(OperationFailure) as caught:
            await ops.status(stranger)
        await drain_operation(ops, ref)
    assert caught.value.error.kind == "unknown_operation"


async def test_run_to_completion_keeps_both_channels_in_the_stream(make: Factory) -> None:
    ops, workspace, _ = await make()
    async with _bounded():
        status, output = await run_to_completion(
            ops, workspace, _spec("printf out; printf err >&2; exit 4")
        )
    assert (status.state, status.exit_code) == ("failed", 4)
    assert sorted((output[:3], output[3:])) == [b"err", b"out"]


# --- Local only -------------------------------------------------------------


@pytest.mark.parametrize("opt_in", [None, False, 1, "yes"])
def test_local_without_an_explicit_insecure_opt_in_raises(opt_in: object) -> None:
    kwargs = {} if opt_in is None else {"allow_insecure": opt_in}
    with pytest.raises(InsecureProviderError, match="allow_insecure=True"):
        WorkspaceLocalProvider(**kwargs)  # type: ignore[arg-type]  # the wrong types are the test


async def test_agentic_workspace_cannot_reach_local_implicitly() -> None:
    """The registry builds providers with no arguments, so `"local"` must refuse there too."""
    with pytest.raises(InsecureProviderError):
        async with AgenticWorkspace.create(provider="local"):
            pytest.fail("an implicit local workspace was created")


async def test_agentic_workspace_runs_on_an_opted_in_local_provider(tmp_path: Path) -> None:
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    async with AgenticWorkspace(WorkspaceConfig(provider="local"), provider=provider) as ws:
        assert (await ws.execute("printf ok")).stdout == "ok"


def test_local_describe_matches_the_published_local_descriptor() -> None:
    """Local is not `exact`: its workload shares our uid and pid namespace."""
    published = json.loads((FIXTURES / "local.json").read_text())
    described = WorkspaceLocalProvider(allow_insecure=True).describe()
    assert described.model_dump(mode="json", by_alias=True) == published
    assert described.observation.integrity == "continuity_detected"


@pytest.mark.parametrize("pgid", [0, 1, -1])
def test_unsafe_group_refuses_init_and_non_positive_groups(pgid: int) -> None:
    assert unsafe_group(pgid) is not None


def test_unsafe_group_refuses_the_callers_own_group() -> None:
    refusal = unsafe_group(os.getpgrp())
    assert refusal is not None and "caller's own group" in refusal
    assert unsafe_group(os.getpgrp() + 1) is None


async def test_cancel_never_signals_a_refused_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the guard refuses, killpg is never called; the leader dies by its handle."""
    killed_groups: list[int] = []
    monkeypatch.setattr(local_operations, "unsafe_group", lambda pgid: "refusing for the test")
    monkeypatch.setattr(local_operations.os, "killpg", lambda g, s: killed_groups.append(g))
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    async with _bounded():
        ref = await provider.start(workspace, _spec("exec sleep 300"))
        status = await provider.cancel(ref, grace_seconds=0.5)
    assert status.state == "cancelled"
    assert status.metadata["cancel_signal_failure"] == "refusing for the test"
    assert killed_groups == []
    await provider.destroy(workspace)


async def test_local_cancel_kills_a_descendant_that_left_the_group(tmp_path: Path) -> None:
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    pids = tmp_path / "pids"
    script = f"setsid sh -c 'echo $$ >> {pids}; exec sleep 300' & echo $$ >> {pids}; exec sleep 300"
    async with _bounded():
        ref = await provider.start(workspace, _spec(script))
        recorded = await _wait_for_pids(pids, 2)
        assert all(_alive(p) for p in recorded)
        assert (await provider.cancel(ref, grace_seconds=0.5)).state == "cancelled"
    assert not [p for p in recorded if _alive(p)]
    await provider.destroy(workspace)


async def test_local_destroy_ends_running_operations_and_forgets_them(tmp_path: Path) -> None:
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    pid_file = tmp_path / "pid"
    async with _bounded():
        ref = await provider.start(workspace, _spec(f"echo $$ > {pid_file}; exec sleep 300"))
        (pid,) = await _wait_for_pids(pid_file, 1)
        await provider.destroy(workspace)
    assert not _alive(pid)
    with pytest.raises(OperationFailure) as caught:
        await provider.status(ref)
    assert caught.value.error.kind == "unknown_operation"


async def test_local_credential_binding_reaches_env_but_never_argv(tmp_path: Path) -> None:
    from agentic_isolation.contract import CredentialBinding, CredentialRef

    class Vault:
        async def resolve(self, ref: CredentialRef) -> str:
            return "s3cr3t-local-9a1"

    provider = WorkspaceLocalProvider(base_dir=tmp_path, credentials=Vault(), allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    spec = _spec(
        'printf %s "$TOKEN" > token; tr "\\0" " " < /proc/$$/cmdline',
        credential_bindings=(
            CredentialBinding(
                ref=CredentialRef(name="t", scope="repo"), delivery="env", target="TOKEN"
            ),
        ),
    )
    async with _bounded():
        _, output = await run_to_completion(provider, workspace, spec)
    assert (workspace.path / "token").read_text() == "s3cr3t-local-9a1"
    assert b"s3cr3t" not in output
    await provider.destroy(workspace)


@pytest.mark.parametrize("launch", ["", "env -i "], ids=["inherited-env", "clean-env"])
async def test_local_cancel_kills_an_orphan_that_detached_before_cancel(
    tmp_path: Path, launch: str
) -> None:
    """A daemon whose intermediate parent exited before cancel is still owned,
    even when it was started without the workload's environment."""
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    orphan, middle, bystander = tmp_path / "orphan", tmp_path / "middle", tmp_path / "bystander"
    script = (
        f"sh -c 'echo $$ > {middle}; {launch}setsid sleep 300 >/dev/null 2>&1 & "
        f"echo $! > {orphan}'; exec sleep 300"
    )
    async with _bounded():
        other = await provider.start(
            workspace, _spec(f"echo $$ > {bystander}; exec sleep 300", key="k2")
        )
        ref = await provider.start(workspace, _spec(script))
        (orphan_pid,) = await _wait_for_pids(orphan, 1)
        (middle_pid,) = await _wait_for_pids(middle, 1)
        (bystander_pid,) = await _wait_for_pids(bystander, 1)
        while _alive(middle_pid):
            await asyncio.sleep(0.05)
        assert _alive(orphan_pid)
        assert (await provider.cancel(ref, grace_seconds=0.5)).state == "cancelled"
        assert not _alive(orphan_pid)
        assert (await provider.cancel(ref, grace_seconds=0.5)).state == "cancelled"
        assert _alive(bystander_pid)
        assert (await provider.status(other)).state == "running"
    await provider.destroy(workspace)


async def test_local_cancel_with_a_surviving_orphan_settles_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collector sees the leader die first, yet cancel and status say failed."""
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    orphan, middle = tmp_path / "orphan", tmp_path / "middle"
    script = (
        f"sh -c 'echo $$ > {middle}; setsid sleep 300 >/dev/null 2>&1 & echo $! > {orphan}'; "
        "exec sleep 300"
    )
    real_send = local_operations._send
    orphan_pid = 0
    try:
        async with _bounded():
            ref = await provider.start(workspace, _spec(script))
            (orphan_pid,) = await _wait_for_pids(orphan, 1)
            (middle_pid,) = await _wait_for_pids(middle, 1)
            while _alive(middle_pid):
                await asyncio.sleep(0.05)
            # The orphan cannot be signalled; the leader's group still can.
            monkeypatch.setattr(
                local_operations,
                "_send",
                lambda pid, start, sig: None if pid == orphan_pid else real_send(pid, start, sig),
            )
            monkeypatch.setattr(local_operations, "_KILL_WAIT_SECONDS", 0.2)
            cancelled = await provider.cancel(ref, grace_seconds=0.2)
            assert _alive(orphan_pid)
            assert cancelled.state == "failed"
            assert str(orphan_pid) in cancelled.metadata["cancel_signal_failure"]
            assert (await provider.status(ref)).state == "failed"
            assert (await provider.cancel(ref, grace_seconds=0.2)).state == "failed"
    finally:
        monkeypatch.setattr(local_operations, "_send", real_send)
        if orphan_pid and _alive(orphan_pid):
            os.kill(orphan_pid, signal.SIGKILL)
    await provider.destroy(workspace)


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(["sleep", "300"], start_new_session=True)


def _reap(process: subprocess.Popen[bytes]) -> None:
    """Kill through the `kill` binary: the `signals` fixture replaces `os.kill`."""
    subprocess.run(["kill", "-KILL", str(process.pid)], check=False)
    process.wait(timeout=10)


def _scripted_operation(leader: int, *, reaped: bool, tracked: dict[int, int]) -> object:
    """An operation record whose leader and tracked set the test controls."""
    process = SimpleNamespace(pid=leader, returncode=0 if reaped else None)
    return local_operations._Operation(
        ref=None,  # type: ignore[arg-type]
        spec=_spec("true"),
        workspace_id="ws",
        spool=None,  # type: ignore[arg-type]
        started_at="",
        process=process,  # type: ignore[arg-type]
        token="scripted",
        tracked=tracked,
    )


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record every group and pid signal instead of sending it."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(local_operations.os, "killpg", lambda g, s: sent.append((-g, s)))
    monkeypatch.setattr(local_operations.os, "kill", lambda p, s: sent.append((p, s)))
    if hasattr(local_operations.signal, "pidfd_send_signal"):
        by_fd: dict[int, int] = {}
        real_open = os.pidfd_open

        def pidfd_open(pid: int) -> int:
            fd = real_open(pid)
            by_fd[fd] = pid
            return fd

        monkeypatch.setattr(local_operations.os, "pidfd_open", pidfd_open)
        monkeypatch.setattr(
            local_operations.signal, "pidfd_send_signal", lambda fd, s: sent.append((by_fd[fd], s))
        )
    monkeypatch.setattr(local_operations, "_KILL_WAIT_SECONDS", 0.2)
    return sent


async def test_local_never_signals_a_reused_leader_or_tracked_pid(
    monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    """Replacements keep their pid but not their start time; survivors keep both."""
    reused_leader, reused_child, survivor = _sleeper(), _sleeper(), _sleeper()
    try:
        table = {
            reused_leader.pid: (1, reused_leader.pid, 900),
            reused_child.pid: (1, reused_child.pid, 901),
            survivor.pid: (1, survivor.pid, 300),
        }
        starts = {pid: start for pid, (_, _, start) in table.items()}
        monkeypatch.setattr(local_operations, "_proc_table", lambda: table)
        monkeypatch.setattr(local_operations, "_start_time", starts.get)
        operation = _scripted_operation(
            reused_leader.pid, reaped=True, tracked={reused_child.pid: 100, survivor.pid: 300}
        )

        def term_kills_survivor(pid: int, start: int, sig: signal.Signals) -> None:
            real_send(pid, start, sig)
            if pid == survivor.pid:
                table.pop(survivor.pid)

        real_send = local_operations._send
        monkeypatch.setattr(local_operations, "_send", term_kills_survivor)
        ops = LocalOperations(credentials=None)
        async with _bounded():
            await ops._terminate(operation, grace_seconds=0.2)  # type: ignore[arg-type]
        assert signals == [(survivor.pid, signal.SIGTERM)]
        assert not operation.unconfirmed  # type: ignore[attr-defined]
    finally:
        for process in (reused_leader, reused_child, survivor):
            _reap(process)


async def test_local_never_signals_a_pid_reused_between_term_and_kill(
    monkeypatch: pytest.MonkeyPatch, signals: list[tuple[int, int]]
) -> None:
    """Ownership is rechecked at each signal, so KILL skips a replacement."""
    child = _sleeper()
    try:
        monkeypatch.setattr(
            local_operations, "_proc_table", lambda: {child.pid: (1, child.pid, 300)}
        )
        monkeypatch.setattr(
            local_operations, "_start_time", lambda pid: 300 if not signals else 400
        )
        operation = _scripted_operation(child.pid + 1, reaped=True, tracked={child.pid: 300})
        ops = LocalOperations(credentials=None)
        async with _bounded():
            await ops._terminate(operation, grace_seconds=0.2)  # type: ignore[arg-type]
        assert signals == [(child.pid, signal.SIGTERM)]
        operation.cancel_requested = True  # type: ignore[attr-defined]
        LocalOperations._settle(operation, -9)  # type: ignore[arg-type]
        assert operation.state == "failed"  # type: ignore[attr-defined]
    finally:
        _reap(child)


@pytest.mark.parametrize(
    "overrides",
    [
        {"argv": ("sh", "\u0000")},
        {"environment": {"A=B": "C"}},
        {"working_directory": "file"},
    ],
    ids=["nul-argv", "env-name-with-equals", "cwd-is-a-file"],
)
async def test_local_unlaunchable_spec_is_a_typed_rejection(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    (workspace.path / "file").write_text("in the way")
    bad = OperationSpec.model_validate_json(_spec("echo bad", **overrides).model_dump_json())
    async with _bounded():
        with pytest.raises(OperationFailure) as caught:
            await provider.start(workspace, bad)
        assert caught.value.error.kind == "rejected_policy"
        assert provider._operations._operations == {}
        _, output = await run_to_completion(provider, workspace, _spec("echo fixed"))
    assert output == b"fixed\n"
    await provider.destroy(workspace)


async def test_local_marker_comes_from_a_named_launcher_the_workload_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher runs under argv0 with the line; the same pid then holds neither."""
    seen = tmp_path / "launcher"
    wrapper = tmp_path / "sh"
    wrapper.write_text(f'#!/bin/sh\nprintf "%s\\n" "$$" "$@" > {seen}\nexec /bin/sh "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(local_operations, "_SHELL", str(wrapper))
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    marker = LaunchMarker(argv0="aw-launch-5d2", line="AW_LAUNCH nonce-77e")
    script = (
        'echo "pid $$"; tr "\\0" " " < /proc/$$/cmdline; echo; tr "\\0" "\\n" < /proc/$$/environ'
    )
    async with _bounded():
        _, output = await run_to_completion(
            provider, workspace, _spec(script, launch_marker=marker)
        )
    launcher = seen.read_text().splitlines()
    assert launcher[1] == "-c" and launcher[3:5] == [marker.argv0, marker.line]
    lines = output.decode().splitlines()
    assert lines[0] == marker.line
    assert lines[1] == f"pid {launcher[0]}"
    assert lines[2].split()[:2] == ["sh", "-c"]
    assert marker.line not in "\n".join(lines[2:])
    assert marker.argv0 not in "\n".join(lines[2:])
    await provider.destroy(workspace)


@pytest.mark.parametrize("field", ["argv0", "line"])
async def test_local_unlaunchable_marker_is_a_typed_rejection(tmp_path: Path, field: str) -> None:
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    values = {"argv0": "aw-launch", "line": "AW_LAUNCH"}
    values[field] += "\u0000"
    bad = _spec("echo bad", launch_marker=LaunchMarker(**values))
    bad = OperationSpec.model_validate_json(bad.model_dump_json())
    async with _bounded():
        with pytest.raises(OperationFailure) as caught:
            await provider.start(workspace, bad)
        assert caught.value.error.kind == "rejected_policy"
        assert provider._operations._operations == {}
        fixed = _spec("echo fixed", launch_marker=LaunchMarker(argv0="aw-launch", line="AW_LAUNCH"))
        _, output = await run_to_completion(provider, workspace, fixed)
    assert output == b"AW_LAUNCH\nfixed\n"
    await provider.destroy(workspace)


async def test_local_marker_finds_a_program_on_a_workspace_relative_path(tmp_path: Path) -> None:
    """Relative PATH entries resolve in the operation's directory, marked or not."""
    provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
    workspace = await provider.create(WorkspaceConfig())
    program = workspace.path / "bin" / "aw-program"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\necho ran\n")
    program.chmod(0o755)
    marker = LaunchMarker(argv0="aw-launch", line="AW_LAUNCH")
    async with _bounded():
        plain = _spec("", key="plain", argv=("aw-program",), environment={"PATH": "bin"})
        _, output = await run_to_completion(provider, workspace, plain)
        assert output == b"ran\n"
        marked = _spec("", key="marked", argv=("aw-program",), environment={"PATH": "bin"})
        marked = marked.model_copy(update={"launch_marker": marker})
        _, output = await run_to_completion(provider, workspace, marked)
        assert output == b"AW_LAUNCH\nran\n"
        missing = _spec("", key="missing", argv=("aw-absent",), launch_marker=marker)
        with pytest.raises(OperationFailure) as caught:
            await provider.start(workspace, missing)
        assert caught.value.error.kind == "provider_unavailable"
    await provider.destroy(workspace)
