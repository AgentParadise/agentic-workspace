"""`SupportsStagedTeardown` runs caller work at the points where it is safe.

`destroy()` collapses stop, remove and delete-workspace into one call, which is
right for a caller with nothing to do in between. Session capture has work to
do in between, and the order is forced:

  - the exporter must run while the container is still RUNNING, because there
    is nothing to exec into afterwards
  - the archive must precede deleting the workspace directory, because that
    directory IS the spool
  - a FAILED archive must not delete anything, or a failed upload silently
    becomes permanent loss

An earlier draft exposed three freely callable methods and documented the
order. Review rejected that, correctly: an API whose misuse costs data should
not depend on the caller reading a docstring. These tests pin the ordering as
enforced behaviour, not as prose.

Hermetic - no Docker daemon.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agentic_isolation import SupportsStagedTeardown, TeardownReport, TeardownUsage
from agentic_isolation.contract import MISSING, OperationSpec, WorkspaceRef
from agentic_isolation.providers.base import drain_operation
from agentic_isolation.providers.docker import (
    ContainerRemovalError,
    WorkspaceDockerProvider,
    _usage_from_cgroup,
)
from agentic_isolation.providers.docker_operations import DockerOperations
from agentic_isolation.providers.local import WorkspaceLocalProvider


class _Workspace:
    def __init__(self, handle: str = "container-abc", workspace_dir: str | None = None):
        self.id = "ws-1"
        self._handle = handle
        self.metadata: dict[str, Any] = {}
        if workspace_dir is not None:
            self.metadata["workspace_dir"] = workspace_dir


class _Proc:
    returncode = 0

    async def wait(self) -> int:
        return 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    @property
    def stdout(self) -> Any:
        class _R:
            async def read(self, _n: int) -> bytes:
                return b""

        return _R()


@pytest.fixture(autouse=True)
def _temp_base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # With no `workspace_base_dir`, the provider only deletes the
    # `agentic-ws-` directories `create()` makes under the temp dir.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


def _provider() -> Any:
    p = WorkspaceDockerProvider.__new__(WorkspaceDockerProvider)
    p._log_snapshots = {}
    p._workspaces = {}
    p._retained = {}
    p._lock = asyncio.Lock()
    p._teardown_locks = {}
    p._workspace_base_dir = None
    p._workspace_host_dir = None
    p._operations = DockerOperations(credentials=None, retention_bytes=1024, max_page_bytes=1024)
    return p


# `docker exec [flags] CONTAINER CMD...` runs CMD here and every container is
# running, so operation records come from the real producer without a daemon.
_FAKE_DOCKER = r"""#!/bin/sh
case "$1" in
exec)
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
        -i) shift ;;
        -w|-e) shift 2 ;;
        *) break ;;
        esac
    done
    shift
    exec "$@" ;;
container) echo running ;;
esac
"""


def _record(monkeypatch: pytest.MonkeyPatch, sink: list[str]) -> None:
    async def _spawn(*argv: str, **_k: object) -> Any:
        sink.append(" ".join(argv[:2]))
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)


class TestCapabilityIsOptional:
    def test_docker_advertises_it(self) -> None:
        assert isinstance(_provider(), SupportsStagedTeardown)

    def test_local_does_not(self) -> None:
        assert not isinstance(
            WorkspaceLocalProvider.__new__(WorkspaceLocalProvider), SupportsStagedTeardown
        )


class TestHooksRunAtTheSafePoints:
    def test_while_running_runs_before_the_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The exporter is invoked here. After `docker stop` there is nothing to
        # exec into, so running it later would make the verdict unobtainable.
        seq: list[str] = []
        _record(monkeypatch, seq)

        async def _hook() -> None:
            seq.append("HOOK while_running")

        asyncio.run(_provider().teardown(_Workspace(), while_running=_hook))
        assert seq.index("HOOK while_running") < seq.index("docker stop")

    def test_before_delete_runs_after_removal_and_before_deletion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seq: list[str] = []
        _record(monkeypatch, seq)
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        (d / "spool.jsonl").write_text("transcript")
        seen: dict[str, bool] = {}

        async def _archive() -> None:
            seq.append("HOOK before_delete")
            seen["spool_present"] = (d / "spool.jsonl").exists()

        asyncio.run(_provider().teardown(_Workspace(workspace_dir=str(d)), before_delete=_archive))
        assert seq.index("docker rm") < seq.index("HOOK before_delete")
        assert seen["spool_present"], "the archive hook must still see the spool"
        assert not d.exists(), "the workspace is deleted once archiving succeeded"

    def test_full_order(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        seq: list[str] = []
        _record(monkeypatch, seq)
        d = tmp_path / "agentic-ws-1"
        d.mkdir()

        async def _run() -> None:
            seq.append("HOOK while_running")

        async def _arch() -> None:
            seq.append("HOOK before_delete")

        asyncio.run(
            _provider().teardown(
                _Workspace(workspace_dir=str(d)),
                while_running=_run,
                before_delete=_arch,
            )
        )
        assert seq == [
            "HOOK while_running",
            "docker exec",
            "docker inspect",
            "docker stop",
            "docker logs",
            "docker rm",
            "HOOK before_delete",
        ], seq


class TestFailedArchiveRetainsTheData:
    """The property the whole design exists for."""

    def test_workspace_survives_a_failing_before_delete(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _record(monkeypatch, [])
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        (d / "spool.jsonl").write_text("transcript")

        async def _fails() -> None:
            raise RuntimeError("store unreachable")

        report = asyncio.run(
            _provider().teardown(_Workspace(workspace_dir=str(d)), before_delete=_fails)
        )

        assert report.finalization.state == "retained"
        # Deleting anyway would turn a retryable upload failure into permanent
        # loss, which is exactly what this capability exists to prevent.
        assert (d / "spool.jsonl").exists()

    def test_container_is_still_torn_down_when_archiving_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seq: list[str] = []
        _record(monkeypatch, seq)
        d = tmp_path / "agentic-ws-1"
        d.mkdir()

        async def _fails() -> None:
            raise RuntimeError("store unreachable")

        asyncio.run(_provider().teardown(_Workspace(workspace_dir=str(d)), before_delete=_fails))

        # Retaining data must not come at the cost of stranding a container.
        assert "docker stop" in seq and "docker rm" in seq

    def test_failing_while_running_leaves_the_container_intact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seq: list[str] = []
        _record(monkeypatch, seq)

        async def _fails() -> None:
            raise RuntimeError("exporter blew up")

        report = asyncio.run(_provider().teardown(_Workspace(), while_running=_fails))

        # Recoverable: nothing has been destroyed yet, so a caller can retry.
        assert seq == [], seq
        assert report.finalization.state == "failed"
        assert report.finalization.stage == "while_running"
        assert "exporter blew up" in report.finalization.error.message


class TestOperationRecordsFollowTheOutcome:
    """Idempotency records go with the workspace, and only once it is gone."""

    def test_retained_keeps_them_and_completed_forgets_them(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _record(monkeypatch, [])
        provider = _provider()
        forgotten: list[str] = []

        async def _forget(workspace_id: str) -> None:
            forgotten.append(workspace_id)

        monkeypatch.setattr(provider._operations, "forget_workspace", _forget)
        d = tmp_path / "agentic-ws-1"
        d.mkdir()

        async def _fails() -> None:
            raise RuntimeError("store unreachable")

        first = asyncio.run(
            provider.teardown(_Workspace(workspace_dir=str(d)), before_delete=_fails)
        )
        # Still registered for a retry by ref, so its records stay too.
        assert first.finalization.state == "retained"
        assert forgotten == []

        assert first.finalization.retained is not MISSING
        second = asyncio.run(provider.teardown(first.finalization.retained))
        assert second.finalization.state == "completed"
        assert forgotten == ["ws-1"]

    def test_records_from_the_real_producer_replay_until_completed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The spy above pins the call; this pins what the call does to records
        # `DockerOperations.start` actually wrote, and that a neighbour's stay.
        docker = tmp_path / "docker"
        docker.write_text(_FAKE_DOCKER)
        docker.chmod(0o755)
        provider = _provider()
        provider._operations = ops = DockerOperations(
            credentials=None, retention_bytes=1024, max_page_bytes=1024, docker=str(docker)
        )
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        ws = _Workspace(workspace_dir=str(d))
        other = _Workspace(handle="container-other")
        other.id = "ws-2"
        spec = OperationSpec.model_validate(
            {
                "execution_id": "exec-1",
                "operation_key": "k1",
                "argv": ("true",),
                "working_directory": ".",
                "environment": {},
                "credential_bindings": (),
                "timeout_seconds": 60,
                "metadata": {},
            }
        )

        async def _fails() -> None:
            raise RuntimeError("store unreachable")

        async def _scenario() -> None:
            ref = await ops.start(ws, spec)
            other_ref = await ops.start(other, spec)
            await drain_operation(ops, ref)
            await drain_operation(ops, other_ref)
            _record(monkeypatch, [])

            first = await provider.teardown(ws, before_delete=_fails)
            assert first.finalization.state == "retained"
            assert ref.operation_id in ops._operations
            # Same key, same spec: still a replay of the original, not a launch.
            assert await ops.start(ws, spec) == ref

            assert first.finalization.retained is not MISSING
            second = await provider.teardown(first.finalization.retained)
            assert second.finalization.state == "completed"
            assert ref.operation_id not in ops._operations
            assert ("ws-1", "k1") not in ops._by_key
            assert ops._by_key[("ws-2", "k1")].ref == other_ref
            assert other_ref.operation_id in ops._operations

        asyncio.run(_scenario())


class TestRegistryIsNotLeaked:
    def test_teardown_deregisters_the_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # destroy() popped from _workspaces; a staged path that did not would
        # grow the registry without bound for any caller that switched.
        _record(monkeypatch, [])
        provider = _provider()
        ws = _Workspace()
        provider._workspaces[ws.id] = ws
        asyncio.run(provider.teardown(ws))
        assert provider._workspaces == {}

    def test_a_failed_while_running_hook_KEEPS_the_registration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deliberately the opposite of an earlier version of this test. If the
        # hook fails the container is still LIVE, and deregistering would make
        # a running container invisible to the provider that owns it. The
        # registration is what lets a caller find it and retry.
        _record(monkeypatch, [])
        provider = _provider()
        ws = _Workspace()
        provider._workspaces[ws.id] = ws

        async def _fails() -> None:
            raise RuntimeError("exporter blew up")

        asyncio.run(provider.teardown(ws, while_running=_fails))
        assert provider._workspaces == {ws.id: ws}

    def test_a_failed_before_delete_KEEPS_the_workspace_resolvable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Reverses an earlier version of this test, which asserted the
        # registration was DROPPED here (plan finding B2). The container is
        # gone, but the directory is not, and dropping the registration left a
        # retained directory nobody could name. It moves to the retained
        # registry instead, under the ref the report carries.
        _record(monkeypatch, [])
        provider = _provider()
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        ws = _Workspace(workspace_dir=str(d))
        provider._workspaces[ws.id] = ws

        async def _fails() -> None:
            raise RuntimeError("store unreachable")

        report = asyncio.run(provider.teardown(ws, before_delete=_fails))
        assert report.finalization.retained == WorkspaceRef(
            provider="docker", resource_id="container-abc"
        )
        assert provider._workspaces == {}
        assert provider._resolve_teardown_target(report.finalization.retained) is ws
        assert d.exists()


class TestRetainedThenRetried:
    """Plan section 7 row 6: retain on a failed archive, then retry by ref."""

    def test_retry_by_ref_completes_and_deletes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seq: list[str] = []
        _record(monkeypatch, seq)
        provider = _provider()
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        (d / "spool.jsonl").write_text("transcript")
        ws = _Workspace(workspace_dir=str(d))
        provider._workspaces[ws.id] = ws

        async def _fails() -> None:
            raise RuntimeError("store unreachable")

        first = asyncio.run(provider.teardown(ws, before_delete=_fails))
        assert first.finalization.state == "retained"
        assert first.finalization.stage == "before_delete"
        assert d.exists()
        commands_after_first = list(seq)

        archived: list[bool] = []

        async def _archives() -> None:
            archived.append((d / "spool.jsonl").exists())

        assert first.finalization.retained is not MISSING
        second = asyncio.run(
            provider.teardown(first.finalization.retained, before_delete=_archives)
        )
        assert second.finalization.state == "completed"
        assert archived == [True], "the retry archives the spool the first attempt kept"
        assert not d.exists()
        # The container went with the first attempt: nothing to stop again.
        assert seq == commands_after_first, seq
        with pytest.raises(LookupError):
            provider._resolve_teardown_target(first.finalization.retained)

    def test_a_ref_to_a_live_workspace_resolves_after_a_failed_while_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seq: list[str] = []
        _record(monkeypatch, seq)
        provider = _provider()
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        ws = _Workspace(workspace_dir=str(d))
        provider._workspaces[ws.id] = ws

        async def _fails() -> None:
            raise RuntimeError("exporter blew up")

        asyncio.run(provider.teardown(ws, while_running=_fails))
        ref = WorkspaceRef(provider="docker", resource_id=ws._handle)
        report = asyncio.run(provider.teardown(ref))
        assert report.finalization.state == "completed"
        assert "docker rm" in seq
        assert not d.exists()

    def test_an_unknown_ref_is_refused(self) -> None:
        with pytest.raises(LookupError):
            asyncio.run(_provider().teardown(WorkspaceRef(provider="docker", resource_id="nope")))

    def test_a_directory_that_cannot_be_deleted_is_retained_not_completed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _record(monkeypatch, [])
        provider = _provider()
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        (d / "spool.jsonl").write_text("transcript")
        ws = _Workspace(workspace_dir=str(d))
        provider._workspaces[ws.id] = ws

        real_rmtree = shutil.rmtree

        def _denied(path: str) -> None:
            raise PermissionError(f"cannot delete {path}")

        monkeypatch.setattr(shutil, "rmtree", _denied)
        first = asyncio.run(provider.teardown(ws))
        assert first.finalization.state == "retained", first
        assert first.finalization.stage == "before_delete"
        assert first.usage.delete_failures is not None
        assert "PermissionError" in first.usage.delete_failures[0]
        # No workspace image is recorded, so the root container fallback
        # refused to run.
        assert first.usage.delete_fallback == "refused"
        assert d.exists()
        ref = WorkspaceRef(provider="docker", resource_id=ws._handle)
        assert first.finalization.retained == ref
        assert provider._resolve_teardown_target(ref) is ws

        monkeypatch.setattr(shutil, "rmtree", real_rmtree)
        second = asyncio.run(provider.teardown(ref))
        assert second.finalization.state == "completed"
        assert not d.exists(), "completed must mean the directory is gone"
        with pytest.raises(LookupError):
            provider._resolve_teardown_target(ref)

    def test_failed_container_removal_archives_and_deletes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rm_exit = [1]

        async def _spawn(*argv: str, **_k: object) -> _Proc:
            return _ExitProc(rm_exit[0]) if argv[1] == "rm" else _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        provider = _provider()
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        (d / "spool.jsonl").write_text("transcript")
        ws = _Workspace(workspace_dir=str(d))
        provider._workspaces[ws.id] = ws
        archived: list[bool] = []

        async def _archives() -> None:
            archived.append(True)

        with pytest.raises(ContainerRemovalError):
            asyncio.run(provider.teardown(ws, before_delete=_archives))
        assert archived == [], "before_delete ran while the container may still exist"
        assert d.exists()
        assert provider._workspaces == {ws.id: ws}, "the live registration was dropped"
        assert provider._retained == {}

        rm_exit[0] = 0
        ref = WorkspaceRef(provider="docker", resource_id=ws._handle)
        report = asyncio.run(provider.teardown(ref, before_delete=_archives))
        assert report.finalization.state == "completed"
        assert archived == [True]
        assert not d.exists()


class TestDestroyIsUnchanged:
    def test_destroy_still_stops_removes_and_deletes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seq: list[str] = []
        _record(monkeypatch, seq)
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        provider = _provider()
        ws = _Workspace(workspace_dir=str(d))
        provider._workspaces[ws.id] = ws

        report = asyncio.run(provider.destroy(ws))

        assert seq == ["docker exec", "docker inspect", "docker stop", "docker logs", "docker rm"]
        assert report.finalization.state == "completed"
        assert not d.exists()
        assert provider._workspaces == {}


class TestAsyncFailureModes:
    """The cases plain happy-path tests cannot reach.

    Review found both of these by reading, not by running: a cancel during the
    log snapshot skipped removal entirely, leaving a stopped-but-present and
    already-deregistered container; and two concurrent teardowns could both
    proceed, letting one delete a directory the other was still archiving.
    """

    def test_real_task_cancellation_during_the_stop_still_removes(self) -> None:
        """Cancel the TASK, not a hand-raised CancelledError.

        An earlier version of this test raised CancelledError from inside the
        subprocess factory. That proves the `finally` runs, but it does NOT
        exercise the shield: the await in the finally only needs shielding when
        the surrounding TASK is being cancelled. The weaker test passed with
        the shield removed, so it was verifying nothing about the thing it was
        written for.
        """
        seq: list[str] = []

        async def _main() -> None:
            provider = _provider()
            reached_logs = asyncio.Event()

            async def _spawn(*argv: str, **_k: object) -> Any:
                head = " ".join(argv[:2])
                seq.append(head)
                if head == "docker logs":
                    reached_logs.set()
                    await asyncio.sleep(3600)  # park here until cancelled
                return _Proc()

            import agentic_isolation.providers.docker as mod

            orig = asyncio.create_subprocess_exec
            mod.asyncio.create_subprocess_exec = _spawn  # type: ignore[assignment]
            try:
                ws = _Workspace()
                provider._workspaces[ws.id] = ws
                task = asyncio.create_task(provider.teardown(ws))
                await reached_logs.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                # The `finally` is what guarantees this. Removing the shield
                # does NOT break it - checked by mutation - because asyncio
                # delivers cancellation once. Removing the `finally` does.
                assert "docker rm" in seq, seq
                assert provider._workspaces == {}
            finally:
                mod.asyncio.create_subprocess_exec = orig  # type: ignore[assignment]

        asyncio.run(_main())

    def test_concurrent_teardown_cannot_delete_during_an_archive(self, tmp_path: Path) -> None:
        """A second teardown must wait, not race the first one's archive."""

        async def _main() -> None:
            provider = _provider()
            d = tmp_path / "agentic-ws-1"
            d.mkdir()
            (d / "spool.jsonl").write_text("transcript")
            ws = _Workspace(workspace_dir=str(d))
            provider._workspaces[ws.id] = ws

            async def _spawn(*_a: str, **_k: object) -> Any:
                return _Proc()

            import agentic_isolation.providers.docker as mod

            orig = asyncio.create_subprocess_exec
            mod.asyncio.create_subprocess_exec = _spawn  # type: ignore[assignment]

            observed: dict[str, bool] = {}
            started = asyncio.Event()

            async def _slow_archive() -> None:
                started.set()
                await asyncio.sleep(0.05)
                # If the other teardown raced ahead, the spool is gone by now.
                observed["spool_present"] = (d / "spool.jsonl").exists()
                raise RuntimeError("store unreachable")

            try:
                first = asyncio.create_task(provider.teardown(ws, before_delete=_slow_archive))
                await started.wait()
                second = asyncio.create_task(provider.teardown(ws))
                results = await asyncio.gather(first, second, return_exceptions=True)
            finally:
                mod.asyncio.create_subprocess_exec = orig  # type: ignore[assignment]

            assert observed["spool_present"], (
                "a concurrent teardown deleted the workspace mid-archive"
            )
            first_report, second_report = results
            assert isinstance(first_report, TeardownReport)
            assert isinstance(second_report, TeardownReport)
            assert first_report.finalization.state == "retained"
            # The second caller waited, then found the workspace retained.
            assert second_report.finalization.state == "completed"
            assert not d.exists()

        asyncio.run(_main())

    def test_a_third_caller_waits_for_the_second_archive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The lock entry survives while a woken waiter has not yet resumed."""
        _record(monkeypatch, [])

        async def _main() -> None:
            provider = _provider()
            d = tmp_path / "agentic-ws-1"
            d.mkdir()
            (d / "spool.jsonl").write_text("transcript")
            ws = _Workspace(workspace_dir=str(d))
            provider._workspaces[ws.id] = ws
            first_started = asyncio.Event()
            first_release = asyncio.Event()
            second_started = asyncio.Event()
            second_release = asyncio.Event()
            order: list[str] = []

            async def _first_archive() -> None:
                first_started.set()
                await first_release.wait()
                raise RuntimeError("store unreachable")

            async def _second_archive() -> None:
                second_started.set()
                await second_release.wait()
                order.append("second archived")

            first = asyncio.create_task(provider.teardown(ws, before_delete=_first_archive))
            await first_started.wait()
            second = asyncio.create_task(provider.teardown(ws, before_delete=_second_archive))
            await asyncio.sleep(0)
            first_release.set()
            first_report = await first
            assert first_report.finalization.retained is not MISSING
            await second_started.wait()
            third = asyncio.create_task(provider.teardown(first_report.finalization.retained))
            for _ in range(5):
                await asyncio.sleep(0)
            assert not third.done(), "the third caller ran during the second archive"
            assert (d / "spool.jsonl").exists(), "the spool was deleted mid-archive"
            second_release.set()
            second_report = await second
            order.append("second done")
            # The third caller resolved its ref before waiting, then finds the
            # directory already gone: deletion is idempotent.
            third_report = await third
            assert second_report.finalization.state == "completed"
            assert third_report.finalization.state == "completed"
            assert order == ["second archived", "second done"]
            assert not d.exists()
            assert provider._teardown_locks == {}

        asyncio.run(_main())


class _ExitProc(_Proc):
    """A finished `docker` call that exited with `code`."""

    def __init__(self, code: int) -> None:
        self.returncode = code

    async def wait(self) -> int:
        return self.returncode


class _Reader:
    def __init__(self, out: bytes) -> None:
        self._out = [out]

    async def read(self, _n: int) -> bytes:
        return self._out.pop() if self._out else b""


class _OutputProc(_Proc):
    """A finished `docker` call that printed `out`."""

    def __init__(self, out: bytes) -> None:
        self._reader = _Reader(out)

    @property
    def stdout(self) -> _Reader:
        return self._reader


_CGROUP_OUTPUT = (
    b"memory.events.low 0\n"
    b"memory.events.oom 3\n"
    b"memory.events.oom_kill 2\n"
    b"cpu.stat.usage_usec 4250000\n"
    b"cpu.stat.nr_throttled 7\n"
    b"cpu.stat.throttled_usec 1500000\n"
    b"memory.peak 67108864\n"
)


class TestUsageReachesTheReport:
    """Usage is read before `docker stop` and arrives in what `destroy()` returns.

    PC-162/PC-152: two phases died with exit 137 and nothing recorded whether
    they were OOM-killed. `destroy()` is the call Syntropic137 reads the
    report from, so that is the hop asserted here.
    """

    def _docker(self, monkeypatch: pytest.MonkeyPatch, oom_killed: bytes) -> list[str]:
        seq: list[str] = []

        async def _spawn(*argv: str, **_k: object) -> _Proc:
            seq.append(" ".join(argv[:2]))
            if argv[1] == "exec":
                return _OutputProc(_CGROUP_OUTPUT)
            if argv[1] == "inspect":
                return _OutputProc(oom_killed)
            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        return seq

    def test_destroy_reports_cgroup_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._docker(monkeypatch, b"false\n")
        report = asyncio.run(_provider().destroy(_Workspace()))
        assert report.usage == TeardownUsage(
            cpu_usage_seconds=4.25,
            cpu_throttled_seconds=1.5,
            nr_throttled=7,
            memory_peak_bytes=67108864,
            oom_kills=2,
            oom_killed=True,
        )

    def test_a_retry_reports_the_usage_measured_before_the_container_went(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._docker(monkeypatch, b"false\n")
        provider = _provider()
        d = tmp_path / "agentic-ws-1"
        d.mkdir()

        async def _fails() -> None:
            raise RuntimeError("store unreachable")

        first = asyncio.run(
            provider.teardown(_Workspace(workspace_dir=str(d)), before_delete=_fails)
        )
        assert first.finalization.retained is not MISSING
        second = asyncio.run(provider.teardown(first.finalization.retained))
        assert second.usage.oom_kills == 2
        assert second.usage == first.usage

    def test_docker_oom_flag_counts_when_the_cgroup_is_unreadable(self) -> None:
        usage = _usage_from_cgroup("", oom_killed=True)
        assert usage.oom_killed is True
        assert usage.oom_kills is None

    def test_nothing_read_is_unknown_not_zero(self) -> None:
        assert _usage_from_cgroup("", oom_killed=None) == TeardownUsage()

    def test_positional_construction_keeps_the_network_fields(self) -> None:
        # `delete_fallback` was added after the network fields so existing
        # positional callers keep their meaning.
        usage = TeardownUsage(None, None, None, None, None, None, None, None, 123, 456)
        assert (usage.net_rx_bytes, usage.net_tx_bytes) == (123, 456)
        assert usage.delete_fallback is None

    def test_no_kills_counted_is_false(self) -> None:
        usage = _usage_from_cgroup("memory.events.oom_kill 0\n", oom_killed=None)
        assert (usage.oom_kills, usage.oom_killed) == (0, False)


class TestContainerOwnedFilesAreDeletedThroughAContainer:
    """`rmtree` hits EACCES on files the container wrote under another uid.

    The provider then empties the directory from a short-lived root
    container and removes the emptied directory itself. Seen on GitHub
    runners, whose uid differs from the container user's.
    """

    def _setup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_exit: int = 0
    ) -> tuple[Any, _Workspace, Path, list[tuple[str, ...]]]:
        provider = _provider()
        provider._workspace_base_dir = tmp_path.resolve()
        d = tmp_path / "ws-1"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "owned-by-root").write_text("x")
        ws = _Workspace(workspace_dir=str(d))
        ws.metadata["image_id"] = "sha256:feed"
        provider._workspaces[ws.id] = ws
        calls: list[tuple[str, ...]] = []

        async def _spawn(*argv: str, **_k: object) -> Any:
            calls.append(argv)
            if argv[1] == "run":
                if run_exit == 0:
                    shutil.rmtree(d / "sub")  # what `find -delete` would do
                return _ExitProc(run_exit)
            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        real_rmtree = shutil.rmtree
        denied = [True]

        def _rmtree(path: str) -> None:
            if denied and Path(path) == d:
                denied.clear()
                raise PermissionError(13, "Permission denied", path)
            real_rmtree(path)

        monkeypatch.setattr(shutil, "rmtree", _rmtree)
        return provider, ws, d, calls

    def test_permission_error_runs_the_fallback_with_the_exact_safe_argv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        provider, ws, d, calls = self._setup(monkeypatch, tmp_path)
        report = asyncio.run(provider.teardown(ws))

        runs = [c for c in calls if c[1] == "run"]
        assert runs == [
            (
                "docker",
                "run",
                "--rm",
                "--name",
                "container-abc-delete",
                "--network",
                "none",
                "--pull",
                "never",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--pids-limit",
                "64",
                "--entrypoint",
                "find",
                "--mount",
                f"type=bind,source={d.resolve()},target=/workspace-delete",
                "sha256:feed",
                "/workspace-delete",
                "-mindepth",
                "1",
                "-delete",
            )
        ]
        assert report.finalization.state == "completed", report
        assert report.usage.delete_fallback == "deleted"
        assert report.usage.delete_failures is None
        assert not d.exists()

    def test_docker_in_docker_mounts_the_host_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        provider, ws, _d, calls = self._setup(monkeypatch, tmp_path)
        provider._workspace_host_dir = Path("/host/workspaces")
        asyncio.run(provider.teardown(ws))

        (run,) = [c for c in calls if c[1] == "run"]
        assert "type=bind,source=/host/workspaces/ws-1,target=/workspace-delete" in run

    def test_a_failed_fallback_is_retained_and_says_why(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        provider, ws, d, _calls = self._setup(monkeypatch, tmp_path, run_exit=125)
        report = asyncio.run(provider.teardown(ws))

        assert report.finalization.state == "retained", report
        assert report.usage.delete_fallback == "failed"
        failures = report.usage.delete_failures
        assert failures is not None
        assert "PermissionError" in failures[0]
        assert "docker run exited 125" in failures[1]
        assert d.exists()

    @pytest.mark.parametrize("via", ["outside", "ancestor-symlink", "symlink"])
    def test_a_dir_that_is_not_ours_is_never_deleted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, via: str
    ) -> None:
        # Host rmtree is real here, so a missing containment check would
        # delete the sentinel rather than merely run the fallback.
        provider = _provider()
        provider._workspace_base_dir = (tmp_path / "base").resolve()
        provider._workspace_base_dir.mkdir()
        outside = tmp_path / "outside" / "ws-1"
        outside.mkdir(parents=True)
        sentinel = outside / "must-survive"
        sentinel.write_text("not the provider's")
        candidate = {
            "outside": outside,
            "ancestor-symlink": provider._workspace_base_dir / "redirect" / "ws-1",
            "symlink": provider._workspace_base_dir / "ws-1",
        }[via]
        if via == "ancestor-symlink":
            candidate.parent.symlink_to(outside.parent, target_is_directory=True)
        elif via == "symlink":
            candidate.symlink_to(outside, target_is_directory=True)
        calls: list[str] = []
        _record(monkeypatch, calls)
        ws = _Workspace(workspace_dir=str(candidate))
        ws.metadata["image_id"] = "sha256:feed"
        provider._workspaces[ws.id] = ws
        report = asyncio.run(provider.teardown(ws))

        assert sentinel.exists(), report
        assert report.finalization.state == "retained"
        assert report.usage.delete_failures is not None
        assert "refused" in report.usage.delete_failures[0]
        assert report.usage.delete_fallback is None
        assert "docker run" not in calls

    def test_an_already_absent_dir_is_completed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _record(monkeypatch, [])
        provider = _provider()
        provider._workspace_base_dir = (tmp_path / "base").resolve()
        ws = _Workspace(workspace_dir=str(provider._workspace_base_dir / "ws-1"))
        provider._workspaces[ws.id] = ws
        report = asyncio.run(provider.teardown(ws))

        assert report.finalization.state == "completed"

    def test_a_directory_rmtree_deletes_does_not_use_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _record(monkeypatch, [])
        provider = _provider()
        d = tmp_path / "agentic-ws-1"
        d.mkdir()
        ws = _Workspace(workspace_dir=str(d))
        provider._workspaces[ws.id] = ws
        report = asyncio.run(provider.teardown(ws))

        assert report.finalization.state == "completed"
        assert report.usage.delete_fallback is None
