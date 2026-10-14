"""APSS EXP-V1-0006 0.2.1 lifecycle-hazard fixtures (LH-1..LH-7), executed.

Each vendored `behaviour/hazard-*.json` is driven step by step through the
`SupportsOperations` surface of every Python provider. The harness, not the
fixture, decides what happened:

- `given` conditions are established for real: a workload that double-forks a
  `setsid` descendant, a caller that abandons `cancel`, a stand-in `docker`
  whose `exec` is late or whose container is stuck `removing`, a credential
  resolver gated on `destroy`, a retention smaller than the output, a FIFO.
- `descendant_alive` is measured from `/proc`: any live process carrying this
  run's token in its environment, detached descendants included.
- `launches` is measured from a log every workload appends to as it starts.
- `settles_within_seconds` is enforced by `asyncio.timeout` and, for calls
  that could block the event loop, by a watchdog thread outside it.

A response matches an `expect_one_of` alternative by outcome, not prose:
status `state`, error `kind` and whether it carries a `ref`, observation
`state`/`complete`/`gap`/`loss_detected`, finalization `state`, probe fields.
The fixture's argv (`agent ...`) is replaced by a workload per hazard; its
credential binding and launch marker are kept.

Harness readings, where Python has no literal counterpart of a fixture call:
- an abandoned call is `asyncio.CancelledError`, read as kind `cancelled`;
- `destroy` is the provider's public `destroy`. For Docker the container
  boundary (`_measure_usage`, `_stop`, `_remove`) is a stand-in that logs to
  the stand-in docker's log, so the rest of teardown runs without a daemon;
- LH-7 `finalize` is Local `destroy` returning (no TeardownReport exists for
  Local), read as `completed`.

A provider that fails a fixture is a finding: it is listed in
`tests/conformance/COVERAGE.md` and marked `xfail(strict=True)` here, so the
case still runs every time and a fix turns it into an unexpected pass.

Run locally as `setsid -w uv run --extra dev pytest`; process cleanup signals
only processes carrying this run's token.
"""

from __future__ import annotations

import asyncio
import base64
import faulthandler
import json
import os
import signal
import stat
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentic_isolation.config import WorkspaceConfig
from agentic_isolation.contract import (
    CredentialRef,
    ObservationPage,
    OperationRef,
    OperationSpec,
    OperationStatus,
)
from agentic_isolation.providers.base import Workspace
from agentic_isolation.providers.docker import WorkspaceDockerProvider
from agentic_isolation.providers.docker_operations import DockerOperations
from agentic_isolation.providers.local import WorkspaceLocalProvider
from agentic_isolation.providers.local_operations import LocalOperations
from agentic_isolation.providers.operation_spool import OperationFailure
from agentic_isolation.providers.teardown_report import TeardownUsage

from ._apss_contract import FIXTURES

HAZARDS = {
    (doc := json.loads(path.read_text()))["hazard"]: doc
    for path in sorted((FIXTURES / "behaviour").glob("hazard-*.json"))
}
PROVIDERS = ("docker", "local")
TOKEN_VARIABLE = "AW_HARNESS_RUN"
GRACE_SECONDS = 2.0

# A stand-in `docker` that models a container: every process of it carries
# FAKE_DOCKER_RUN's token, and `stop` kills exactly those (it never touches
# anything else). `removing` is sticky, as a daemon stuck removing would be.
# FAKE_DOCKER_EXEC_DELAY holds `exec` back, so the launch header comes late.
FAKE_DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
state=$(cat "$FAKE_DOCKER_STATE" 2>/dev/null)
case "$1" in
exec)
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
        -i) shift ;;
        -w) shift 2 ;;
        -e) case "$2" in *=*) export "$2" ;; esac; shift 2 ;;
        *) break ;;
        esac
    done
    shift
    [ -n "$FAKE_DOCKER_EXEC_DELAY" ] && sleep "$FAKE_DOCKER_EXEC_DELAY"
    [ "$(cat "$FAKE_DOCKER_STATE" 2>/dev/null)" = exited ] && exit 137
    exec "$@" ;;
container)
    case "$state" in
    gone) echo "Error: No such container: c1" >&2; exit 1 ;;
    removing|exited) echo "$state" ;;
    *) echo running ;;
    esac ;;
stop)
    for p in /proc/[0-9]*; do
        [ "${p#/proc/}" = "$$" ] && continue
        tr '\0' '\n' < "$p/environ" 2>/dev/null \
            | grep -qx "AW_HARNESS_RUN=$FAKE_DOCKER_RUN" && kill -9 "${p#/proc/}" 2>/dev/null
    done
    [ "$state" = removing ] || echo exited > "$FAKE_DOCKER_STATE"
    exit 0 ;;
esac
"""

# Every workload logs its launch first. `$L` is the launch log.
_LOG = 'echo launched >> "$L"; '
WORKLOADS = {
    # Double fork: the middle shell exits, so the setsid'd sleeper is
    # reparented away from the workload before cancel.
    "LH-1": _LOG + "(setsid sleep 300 </dev/null >/dev/null 2>&1 &); exec sleep 300",
    # TERM is ignored, so confirmation has to wait out the grace.
    "LH-2": _LOG + "trap '' TERM; while :; do sleep 0.1; done",
    "LH-3": _LOG + "exec sleep 300",
    "LH-4": _LOG + "exec sleep 300",
    "LH-5": _LOG + "exec sleep 300",
    "LH-6": _LOG + "printf 'lost-it retained output\\n'",
    "LH-7": _LOG + "mkdir -p ../outputs && mkfifo ../outputs/pipe",
}


class FixtureMismatch(AssertionError):
    """A provider's measured response matched none of a step's alternatives."""


@dataclass
class _Gate:
    """A credential resolver that, when armed, blocks until released."""

    armed: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def resolve(self, ref: CredentialRef) -> str:
        if self.armed:
            self.entered.set()
            await self.release.wait()
        return "harness-credential"


@dataclass
class Subject:
    """One provider under test and the conditions the harness can impose on it."""

    name: str
    provider: Any
    operations: DockerOperations | LocalOperations
    workspace: Workspace
    root: Path
    token: str
    launch_log: Path
    gate: _Gate
    state_file: Path | None
    monkeypatch: pytest.MonkeyPatch
    ref: OperationRef | None = None
    # Whether the resolver had been released each time destroy reached the
    # operation teardown: proof destroy began while start was still blocked.
    destroy_saw_release: list[bool] = field(default_factory=list)

    def spec(self, fixture_spec: dict[str, Any], hazard: str) -> OperationSpec:
        fields = dict(fixture_spec)
        fields["argv"] = ["sh", "-c", WORKLOADS[hazard]]
        fields["environment"] = {
            **fields["environment"],
            TOKEN_VARIABLE: self.token,
            "L": str(self.launch_log),
        }
        if hazard == "LH-5":
            fields["timeout_seconds"] = 1
        if hazard == "LH-6":
            # `produced_bytes` counts the workload's output only; a marker line
            # in the same stream would shift every offset.
            fields.pop("launch_marker", None)
        return OperationSpec.model_validate_json(json.dumps(fields))

    def launches(self) -> int:
        """Workloads the provider launched: `docker exec`s of this run, or Local's launch log."""
        if self.state_file is not None:
            log = self.root / "docker.log"
            lines = log.read_text().splitlines() if log.exists() else []
            return sum(1 for line in lines if line.startswith("exec ") and self.token in line)
        return len(self.launch_log.read_text().splitlines()) if self.launch_log.exists() else 0

    async def settled_launches(self, expected: int) -> int:
        """The launch count once it stops moving: up to 2s for a workload to log itself."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while self.launches() != expected and loop.time() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        return self.launches()

    def alive(self) -> list[int]:
        """Live, non-zombie processes carrying this run's token: measured, not declared."""
        needle = f"{TOKEN_VARIABLE}={self.token}".encode()
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                if needle not in (entry / "environ").read_bytes().split(b"\0"):
                    continue
                if "\nState:\tZ" in (entry / "status").read_text():
                    continue
            except OSError:
                continue
            found.append(int(entry.name))
        return found

    def records(self) -> int:
        """Operation records the provider holds for this workspace once a call returns."""
        ops = self.operations
        return sum(1 for key in ops._by_key if key[0] == self.workspace.id) + sum(
            1
            for operation in ops._operations.values()
            if operation.ref.workspace.resource_id in (self.workspace.id, self.workspace._handle)
        )

    async def destroy(self) -> None:
        await self.provider.destroy(self.workspace)

    def watch_destroy(self) -> None:
        """Record, inside the public destroy, whether the resolver was released yet."""
        forget = self.operations.forget_workspace

        async def recorded(workspace_id: str) -> None:
            self.destroy_saw_release.append(self.gate.release.is_set())
            await forget(workspace_id)

        self.monkeypatch.setattr(self.operations, "forget_workspace", recorded)

    def kill_leftovers(self) -> None:
        # Only processes carrying this run's token: the workload and what it forked.
        for pid in self.alive():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


#: Outer per-test bound, above every fixture's summed settlement bounds. It
#: holds when the event loop is blocked or cancellation is swallowed, where
#: `asyncio.timeout` cannot fire.
TEST_DEADLINE_SECONDS = 300


@pytest.fixture(autouse=True)
def _outer_deadline() -> Any:
    """Dump every thread's stack and exit the run if a test outlives its deadline.

    faulthandler's timer is a C thread, independent of the event loop. It
    signals nothing: the process exits, and under `setsid -w` the session it
    leads is the test's own.
    """
    faulthandler.dump_traceback_later(TEST_DEADLINE_SECONDS, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


@pytest.fixture(params=PROVIDERS)
async def subject(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Subject]:
    token = uuid.uuid4().hex
    gate = _Gate()
    if request.param == "docker":
        fake = tmp_path / "docker"
        fake.write_text(FAKE_DOCKER)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
        monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "state"))
        monkeypatch.setenv("FAKE_DOCKER_RUN", token)
        (tmp_path / "repo").mkdir()
        monkeypatch.chdir(tmp_path / "repo")
        provider = WorkspaceDockerProvider()
        operations = DockerOperations(
            credentials=gate, retention_bytes=1 << 20, max_page_bytes=1 << 16, docker=str(fake)
        )
        provider._operations = operations
        _stand_in_container_boundary(provider, tmp_path / "docker.log", monkeypatch)
        # Its own directory: Docker destroy deletes the workspace path.
        (tmp_path / "ws").mkdir()
        workspace = Workspace(
            id="ws1",
            provider="docker",
            path=tmp_path / "ws",
            config=WorkspaceConfig(),
            _handle="c1",
        )
        made = Subject(
            "docker",
            provider,
            operations,
            workspace,
            tmp_path,
            token,
            tmp_path / "launches",
            gate,
            tmp_path / "state",
            monkeypatch,
        )
    else:
        provider = WorkspaceLocalProvider(base_dir=tmp_path, allow_insecure=True)
        operations = LocalOperations(credentials=gate, retention_bytes=1 << 20)
        provider._operations = operations
        workspace = await provider.create(WorkspaceConfig())
        made = Subject(
            "local",
            provider,
            operations,
            workspace,
            workspace.path,
            token,
            tmp_path / "launches",
            gate,
            None,
            monkeypatch,
        )
    try:
        yield made
    finally:
        made.kill_leftovers()


def _stand_in_container_boundary(
    provider: WorkspaceDockerProvider, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Docker SDK calls teardown makes, logged instead of sent to a daemon."""

    def logged(line: str) -> None:
        with log.open("a") as f:
            f.write(line + "\n")

    async def measure_usage(container_name: str) -> TeardownUsage:
        return TeardownUsage()

    async def stop(container_name: str) -> None:
        logged(f"stop {container_name}")

    async def remove(container_name: str) -> bool:
        logged(f"rm {container_name}")
        return True

    monkeypatch.setattr(provider, "_measure_usage", measure_usage)
    monkeypatch.setattr(provider, "_stop", stop)
    monkeypatch.setattr(provider, "_remove", remove)


# --- Given conditions -------------------------------------------------------


def _small_retention(subject: Subject) -> None:
    descriptor = json.loads((FIXTURES / HAZARDS["LH-6"]["provider"]).read_text())
    subject.operations._retention = descriptor["observation"]["retention_bytes"]


async def _settled(subject: Subject, *, within: float = 10.0) -> None:
    """Wait until the started workload has run to the point its hazard needs."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        if subject.launches() and len(subject.alive()) >= 2:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{subject.name}: the LH-1 descendant never appeared")


# --- Calls and matching ----------------------------------------------------


@dataclass
class Outcome:
    """What a call did: a document it returned, or the typed error it raised."""

    document: OperationRef | OperationStatus | ObservationPage | dict[str, Any] | None = None
    kind: str | None = None
    has_ref: bool = False
    pages: list[ObservationPage] = field(default_factory=list)

    def describe(self) -> str:
        if self.kind is not None:
            return f"error kind={self.kind} ref={self.has_ref}"
        doc = self.document
        if isinstance(doc, OperationStatus | ObservationPage | OperationRef):
            return doc.model_dump_json(exclude_none=True)
        return json.dumps(doc)


def _failure(error: OperationFailure) -> Outcome:
    return Outcome(kind=error.error.kind, has_ref=getattr(error.error, "ref", None) is not None)


def _matches(alternative: dict[str, Any], outcome: Outcome, alive: bool) -> bool:
    want = alternative["document"]
    if "descendant_alive" in alternative and alternative["descendant_alive"] != alive:
        return False
    if "kind" in want:
        return outcome.kind == want["kind"] and outcome.has_ref == ("ref" in want)
    if outcome.kind is not None:
        return False
    got = outcome.document
    if isinstance(got, OperationStatus):
        return got.state == want.get("state")
    if isinstance(got, ObservationPage):
        page = got.model_dump(by_alias=True, exclude_none=True)
        return all(
            page.get(key) == want.get(key) for key in ("state", "complete", "gap", "loss_detected")
        )
    if isinstance(got, dict):
        return all(got.get(key) == value for key, value in want.items() if key != "schema")
    # An operationRef expectation: start returned a ref.
    return isinstance(got, OperationRef)


async def _call(
    subject: Subject, step: dict[str, Any], hazard: str, state: dict[str, Any]
) -> Outcome:
    call = step["call"]
    given = set(step.get("given", ()))
    provider = subject.provider
    try:
        if call == "start":
            spec = subject.spec(step["args"]["spec"], hazard)
            if "destroyed_during_credential_resolution" in given:
                subject.gate.armed = True
                subject.watch_destroy()
                starting = asyncio.ensure_future(provider.start(subject.workspace, spec))
                await subject.gate.entered.wait()
                # destroy begins while the start is blocked; the resolver then
                # answers, as a real one eventually would.
                destroying = asyncio.ensure_future(subject.destroy())
                await asyncio.sleep(0.2)
                subject.gate.release.set()
                try:
                    ref = await starting
                finally:
                    await destroying
                    # Not a fixture expectation: the harness's own proof that the
                    # race it claims to drive is the one that ran.
                    assert subject.destroy_saw_release[:1] == [False], (
                        f"{subject.name}: destroy did not reach operation teardown "
                        f"before the resolver was released ({subject.destroy_saw_release})"
                    )
            else:
                if hazard == "LH-3" and subject.name == "docker":
                    subject.monkeypatch.setenv("FAKE_DOCKER_EXEC_DELAY", "300")
                ref = await provider.start(subject.workspace, spec)
            subject.ref = ref
            if hazard == "LH-1":
                await _settled(subject)
            if hazard in ("LH-6", "LH-7"):
                await _drained(subject, ref)
            return Outcome(document=ref)
        assert subject.ref is not None, f"{hazard}: {call} before a successful start"
        ref = subject.ref
        if call == "status":
            return Outcome(document=await provider.status(ref))
        if call == "cancel":
            if "caller_cancelled_during_confirmation" in given:
                cancelling = asyncio.ensure_future(
                    provider.cancel(ref, grace_seconds=GRACE_SECONDS * 3)
                )
                await asyncio.sleep(GRACE_SECONDS / 2)
                cancelling.cancel()
                try:
                    return Outcome(document=await cancelling)
                except asyncio.CancelledError:
                    return Outcome(kind="cancelled", has_ref=True)
            return Outcome(document=await provider.cancel(ref, grace_seconds=GRACE_SECONDS))
        if call == "observe":
            if "backend_stuck_removing" in given and subject.state_file is not None:
                subject.state_file.write_text("removing\n")
            cursor = state.get("cursor")
            if "observation_gap" in given:
                state["max_bytes"] = await _budget_for(provider, ref, cursor, data_bytes=8)
            max_bytes = state.get("max_bytes", 1 << 16)
            pages: list[ObservationPage] = []
            while True:
                page = await provider.observe(
                    ref, cursor=cursor, max_bytes=max_bytes, wait_seconds=1.0
                )
                pages.append(page)
                cursor = state["cursor"] = page.next_cursor
                if page.complete or not step.get("repeat_until_complete"):
                    return Outcome(document=page, pages=pages)
        if call in ("file_exists", "read_file"):
            return await _probe(subject, call, step["args"]["path"], step["settles_within_seconds"])
        if call == "finalize":
            await provider.destroy(subject.workspace)
            return Outcome(document={"state": "completed"})
    except OperationFailure as e:
        return _failure(e)
    raise AssertionError(f"{hazard}: the harness has no driver for call {call!r}")


async def _budget_for(
    provider: Any, ref: OperationRef, cursor: str | None, *, data_bytes: int
) -> int:
    """The smallest `max_bytes` whose page carries `data_bytes`, as the fixture's pages do.

    `max_bytes` bounds the serialised page, envelope included, so the data a
    budget buys is found by asking; observe is a read and moves nothing.
    """
    for budget in range(64, 4096):
        try:
            page = await provider.observe(ref, cursor=cursor, max_bytes=budget)
        except OperationFailure:
            continue
        if len(base64.b64decode(page.data_b64)) >= data_bytes:
            return budget
    raise AssertionError(f"no page budget under 4096 bytes carries {data_bytes} data bytes")


async def _drained(subject: Subject, ref: OperationRef) -> None:
    """Wait for a short workload to finish, without draining its output."""
    async with asyncio.timeout(10):
        while (await subject.provider.status(ref)).state not in ("succeeded", "failed"):
            await asyncio.sleep(0.05)


async def _probe(subject: Subject, call: str, path: str, bound: float) -> Outcome:
    """Probe or read a FIFO, with a watchdog thread that unblocks a stuck open.

    A provider that opens the FIFO on its event loop blocks the loop, so
    `asyncio.timeout` could not fire. The watchdog runs on its own thread: past
    the bound it opens the FIFO's write end once, which ends the open, and
    records that the call overran.
    """
    fifo = subject.root / path
    done = threading.Event()
    overran = threading.Event()

    def watchdog() -> None:
        if done.wait(bound):
            return
        overran.set()
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        if call == "file_exists":
            exists = await subject.provider.file_exists(subject.workspace, path)
            regular = stat.S_ISREG(os.stat(fifo).st_mode) if exists else False
            # Python's file_exists is a bool, so only `exists` is the provider's
            # claim; `regular_file` is read from the filesystem.
            outcome = Outcome(document={"path": path, "exists": exists, "regular_file": regular})
        else:
            data = await subject.provider.read_file(subject.workspace, path)
            outcome = Outcome(document={"path": path, "data": data})
    except OperationFailure as e:
        outcome = _failure(e)
    except OSError as e:
        outcome = Outcome(kind=f"untyped {type(e).__name__}")
    finally:
        done.set()
    if overran.is_set():
        raise FixtureMismatch(f"{call} {path} blocked past its {bound}s bound")
    return outcome


_OUTCOME_FIELDS = ("state", "kind", "complete", "gap", "loss_detected", "exists", "regular_file")


def _summary(alternative: dict[str, Any]) -> str:
    fields = {k: v for k, v in alternative["document"].items() if k in _OUTCOME_FIELDS}
    return f"{json.dumps(fields)} alive={alternative.get('descendant_alive')}"


async def run_hazard(subject: Subject, hazard: str) -> None:
    """Drive every step of `hazard` against `subject`; raise FixtureMismatch on the first miss."""
    fixture = HAZARDS[hazard]
    if "provider" in fixture:
        _small_retention(subject)
    state: dict[str, Any] = {}
    for index, step in enumerate(fixture["steps"]):
        bound = step.get("settles_within_seconds", 30)
        began = time.monotonic()
        try:
            async with asyncio.timeout(bound):
                outcome = await _call(subject, step, hazard, state)
        except TimeoutError as e:
            raise FixtureMismatch(
                f"{hazard} step {index} {step['call']}: did not settle within {bound}s"
            ) from e
        elapsed = time.monotonic() - began
        alive = bool(subject.alive())
        alternatives = step.get("expect_one_of") or [step["expect"]]
        where = f"{subject.name} {hazard} step {index} {step['call']} ({elapsed:.1f}s)"
        if not any(_matches(alt, outcome, alive) for alt in alternatives):
            raise FixtureMismatch(
                f"{where}: got {outcome.describe()} with descendant_alive={alive} "
                f"(measured pids {subject.alive()}); none of "
                + "; ".join(_summary(a) for a in alternatives)
            )
        if (
            "launches" in step
            and (n := await subject.settled_launches(step["launches"])) != step["launches"]
        ):
            raise FixtureMismatch(
                f"{where}: measured {n} launches, fixture says {step['launches']}"
            )
        if "operation_records" in step and subject.records() != step["operation_records"]:
            raise FixtureMismatch(
                f"{where}: provider holds {subject.records()} operation records, "
                f"fixture says {step['operation_records']}"
            )


# Findings: (provider, hazard) -> what the harness measured. Each is a provider
# defect left to a follow-up (this change does not touch provider behaviour).
FINDINGS: dict[tuple[str, str], str] = {
    ("docker", "LH-3"): "cancel waits _LEADER_SECONDS (30s) for the header before stopping, "
    "so it cannot settle within the 30s bound",
    ("docker", "LH-4"): "a start blocked in credential resolution launches after destroy began; "
    "the later start is unknown_operation, not resource_lost",
    ("docker", "LH-6"): "the completing page of a lossy drain carries no loss_detected (0.2.1)",
    ("local", "LH-2"): "after an abandoned cancel, the retried cancel settles failed, "
    "neither confirmed cancelled nor the unconfirmed timeout outcome",
    ("local", "LH-4"): "a start blocked in credential resolution launches after destroy began; "
    "the later start is unknown_operation, not resource_lost",
    ("local", "LH-6"): "the completing page of a lossy drain carries no loss_detected (0.2.1)",
    ("local", "LH-7"): "read_file opens a FIFO on the event loop and blocks until a writer appears",
}
# Cases the harness cannot drive yet; each is reported as not run, never as a pass.
NOT_RUN: dict[tuple[str, str], str] = {
    ("docker", "LH-7"): "file_exists/read_file/destroy go through the Docker SDK, which the "
    "stand-in docker CLI does not serve; needs a real-daemon fixture (follow-up)",
}


@pytest.mark.parametrize("hazard", sorted(HAZARDS))
async def test_provider_meets_lifecycle_hazard(
    subject: Subject, hazard: str, request: pytest.FixtureRequest
) -> None:
    if (subject.name, hazard) in NOT_RUN:
        pytest.skip(f"NOT RUN: {NOT_RUN[subject.name, hazard]}")
    if (subject.name, hazard) in FINDINGS:
        request.applymarker(
            pytest.mark.xfail(
                raises=FixtureMismatch, strict=True, reason=FINDINGS[subject.name, hazard]
            )
        )
    await run_hazard(subject, hazard)


def test_every_hazard_fixture_is_driven() -> None:
    # An empty or partial vendor would otherwise pass the matrix vacuously.
    assert sorted(HAZARDS) == [f"LH-{n}" for n in range(1, 8)]
    calls = {step["call"] for doc in HAZARDS.values() for step in doc["steps"]}
    assert calls == {"start", "status", "cancel", "observe", "file_exists", "read_file", "finalize"}


async def _kill_leader_and_claim_confirmed_local(
    self: LocalOperations, operation: Any, *, grace_seconds: float
) -> None:
    MUTATIONS_APPLIED.append("local")
    # The workload's own group (start_new_session); the detached descendant left it.
    os.killpg(operation.process.pid, signal.SIGKILL)
    await operation.process.wait()


async def _kill_leader_and_claim_confirmed_docker(
    self: DockerOperations, operation: Any, *, grace_seconds: float
) -> None:
    MUTATIONS_APPLIED.append("docker")
    # The leader is a session leader, so its group is the workload's own.
    os.killpg(int(operation.leader[0]), signal.SIGKILL)


MUTATIONS_APPLIED: list[str] = []
CONFIRM_WITHOUT_DESCENDANTS = {
    "local": (LocalOperations, "_signal_and_confirm", _kill_leader_and_claim_confirmed_local),
    "docker": (DockerOperations, "_terminate_group", _kill_leader_and_claim_confirmed_docker),
}


async def test_lh1_fails_a_cancel_confirmed_while_a_descendant_lives(subject: Subject) -> None:
    """No vacuous pass: a provider whose cancel kills only the leader's group and
    reports confirmed must fail LH-1, on the cancel step, on measured liveness."""
    owner, name, mutant = CONFIRM_WITHOUT_DESCENDANTS[subject.name]
    subject.monkeypatch.setattr(owner, name, mutant)
    MUTATIONS_APPLIED.clear()
    with pytest.raises(FixtureMismatch) as caught:
        await run_hazard(subject, "LH-1")
    assert MUTATIONS_APPLIED == [subject.name], "the mutation never ran"
    message = str(caught.value)
    assert "LH-1 step 1 cancel" in message, message
    assert '"state":"cancelled"' in message and "descendant_alive=True" in message, message
