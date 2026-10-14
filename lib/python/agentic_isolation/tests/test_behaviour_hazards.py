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
`tests/conformance/COVERAGE.md` and pinned in `FINDINGS` by its first miss's
(step, call, outcome fields). The case still runs every time; exactly that
miss is an expected failure, and a fix or any other miss fails.

Run locally as `setsid -w uv run --extra dev pytest`; process cleanup signals
only processes carrying this run's token.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import faulthandler
import json
import os
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentic_isolation.config import SecurityConfig, WorkspaceConfig
from agentic_isolation.contract import (
    CredentialRef,
    ObservationPage,
    OperationRef,
    OperationSpec,
    OperationStatus,
)
from agentic_isolation.providers.base import Workspace
from agentic_isolation.providers.docker import NotARegularFileError, WorkspaceDockerProvider
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
    """A provider's measured response matched none of a step's alternatives.

    A miss on a step's outcome also carries what was measured, so a verdict can
    be classified on fields rather than on the message.
    """

    def __init__(
        self,
        message: str,
        *,
        hazard: str | None = None,
        step: int | None = None,
        call: str | None = None,
        outcome: Outcome | None = None,
        alive: bool | None = None,
        settled: bool = True,
    ) -> None:
        super().__init__(message)
        self.hazard = hazard
        self.step = step
        self.call = call
        self.outcome = outcome
        self.alive = alive
        self.settled = settled

    def fields(self) -> dict[str, Any]:
        """The measured outcome as fields a pin compares: never the message."""
        if not self.settled:
            return {"settled": False}
        return {} if self.outcome is None else self.outcome.fields()


class NotRun(Exception):  # noqa: N818 - a verdict, as in the Rust HazardVerdict::NotRun
    """The step could not be executed as the fixture says: never a pass, never a finding."""


@dataclass(frozen=True)
class Finding:
    """A known provider defect, pinned where and how it first misses.

    A miss at another step or call, or with other outcome fields, is not this
    finding, so it fails rather than staying expected.
    """

    step: int
    call: str
    outcome: dict[str, Any]
    reason: str

    def holds(self, error: FixtureMismatch) -> bool:
        fields = error.fields()
        return (
            error.step == self.step
            and error.call == self.call
            and all(fields.get(key, _ABSENT) == value for key, value in self.outcome.items())
        )


_ABSENT = object()


class UnconfirmedOrphanCancel(FixtureMismatch):
    """LH-1's cancel failed closed on an orphan it cannot confirm unrelated.

    Correct provider behaviour that the 0.2.1 fixture does not yet accept:
    AgentParadise/agent-paradise-standards-system#146 adds the explicit
    unconfirmed outcome. Drop this, and its xfail, at APSS 0.2.2.
    """


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
    # Where the workload sees `launch_log`, when that is not the host path
    # (a real container sees its workspace mount at /workspace).
    launch_log_in_workload: str | None = None
    # Whether the resolver had been released each time destroy reached the
    # operation teardown: proof destroy began while start was still blocked.
    destroy_saw_release: list[bool] = field(default_factory=list)

    def spec(self, fixture_spec: dict[str, Any], hazard: str) -> OperationSpec:
        fields = dict(fixture_spec)
        fields["argv"] = ["sh", "-c", WORKLOADS[hazard]]
        fields["environment"] = {
            **fields["environment"],
            TOKEN_VARIABLE: self.token,
            "L": self.launch_log_in_workload or str(self.launch_log),
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
        """Live, non-zombie processes carrying this run's token: measured, not declared.

        Fails closed (AgentParadise/agentic-workspace#68): a process that could
        be a workload but whose environment cannot be read makes liveness
        unmeasurable, raised as NotRun, never counted as not ours. A process
        older than this harness cannot be a workload it launched, and one that
        exits during the scan, or is a zombie, is gone; none is a candidate.
        """
        needle = f"{TOKEN_VARIABLE}={self.token}".encode()
        harness_began = _started(Path("/proc/self"))
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                if _started(entry) < harness_began or _zombie(entry):
                    continue
                try:
                    environ = _environ(entry)
                except PermissionError:
                    if _exiting(entry):
                        continue
                    raise
                if needle not in environ.split(b"\0"):
                    continue
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as e:
                raise NotRun(
                    f"descendant liveness is unmeasurable: {entry}/environ is unreadable "
                    f"({e}); AgentParadise/agentic-workspace#68"
                ) from e
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
        # Where liveness is unmeasurable there is nothing this can attribute.
        try:
            pids = self.alive()
        except NotRun:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _started(entry: Path) -> int:
    """When the process began, in clock ticks since boot (proc(5) stat field 22)."""
    return int((entry / "stat").read_text().rsplit(")", 1)[1].split()[19])


def _zombie(entry: Path) -> bool:
    return "\nState:\tZ" in (entry / "status").read_text()


def _exiting(entry: Path) -> bool:
    """Whether a process whose environ is hidden is on its way out.

    An exiting process hides its environ from the moment it drops its memory,
    a little before it becomes a zombie, so it is given a moment to get there.
    """
    for _ in range(5):
        if _zombie(entry):
            return True
        time.sleep(0.02)
    return False


def _environ(entry: Path) -> bytes:
    return (entry / "environ").read_bytes()


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


# `given` conditions only a stand-in docker can hold, and why anything else cannot.
_STAND_IN_GIVENS = {
    "launch_header_not_seen": "Local learns its leader at spawn and has no launch-header "
    "phase; a real daemon's exec cannot be held back",
    "backend_stuck_removing": "Local has no backend to be stuck removing; a real daemon "
    "cannot be made to stick",
}
# `given` conditions every subject establishes: by its workload (detached_descendant,
# fifo_in_workspace), its spec (timeout_elapsed) or the step's driver in `_call`.
_DRIVEN_GIVENS = frozenset(
    {
        "detached_descendant",
        "caller_cancelled_during_confirmation",
        "destroyed_during_credential_resolution",
        "workspace_destroyed",
        "observation_gap",
        "fifo_in_workspace",
        "timeout_elapsed",
    }
)


def _unestablished(subject: Subject, given: str) -> str | None:
    """Why `given` cannot be made true for `subject`, or None when it can."""
    if given in _STAND_IN_GIVENS:
        return None if subject.state_file is not None else _STAND_IN_GIVENS[given]
    if given in _DRIVEN_GIVENS:
        return None
    return "the harness has no way to establish it"


# --- Calls and matching ----------------------------------------------------


@dataclass
class Outcome:
    """What a call did: a document it returned, or the typed error it raised."""

    document: OperationRef | OperationStatus | ObservationPage | dict[str, Any] | None = None
    kind: str | None = None
    has_ref: bool = False
    pages: list[ObservationPage] = field(default_factory=list)

    def fields(self) -> dict[str, Any]:
        """The stable outcome fields a pin names: kind and ref, or the document's outcome."""
        if self.kind is not None:
            return {"kind": self.kind, "ref": self.has_ref}
        doc = self.document
        if isinstance(doc, OperationRef):
            return {"operation_ref": True}
        if isinstance(doc, OperationStatus | ObservationPage):
            dumped = doc.model_dump(by_alias=True, exclude_none=True)
            return {key: dumped[key] for key in _OUTCOME_FIELDS if key in dumped}
        return {key: value for key, value in (doc or {}).items() if key in _OUTCOME_FIELDS}

    def describe(self) -> str:
        if self.kind is not None:
            return f"error kind={self.kind} ref={self.has_ref}"
        doc = self.document
        if isinstance(doc, OperationStatus | ObservationPage | OperationRef):
            return doc.model_dump_json(exclude_none=True)
        return json.dumps(doc)


def _failure(error: OperationFailure) -> Outcome:
    # An absent ref is the MISSING sentinel, not None.
    return Outcome(kind=error.error.kind, has_ref=isinstance(error.error.ref, OperationRef))


def _matches(alternatives: list[dict[str, Any]], outcome: Outcome, alive: bool) -> int | None:
    """The index of the first alternative `outcome` matches, or None."""
    return next(
        (i for i, alternative in enumerate(alternatives) if _match(alternative, outcome, alive)),
        None,
    )


def _match(alternative: dict[str, Any], outcome: Outcome, alive: bool) -> bool:
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
    for condition in sorted(given):
        if (reason := _unestablished(subject, condition)) is not None:
            raise NotRun(f"given {condition} cannot be set up: {reason}")
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
            if "backend_stuck_removing" in given:
                assert subject.state_file is not None
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
            report = await provider.destroy(subject.workspace)
            # Docker returns a TeardownReport; Local returns nothing on success.
            final = "completed" if report is None else report.finalization.state
            return Outcome(document={"state": final})
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
    outcome: Outcome | None
    untyped: Exception | None = None
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
    except NotARegularFileError:
        # Docker's typed refusal to read a non-regular file is the policy refusal.
        outcome = Outcome(kind="rejected_policy")
    except OSError as e:
        outcome = Outcome(kind=f"untyped {type(e).__name__}")
    except Exception as e:  # noqa: BLE001 - any other type is the finding, named below
        untyped = e
        outcome = None
    finally:
        done.set()
    if overran.is_set():
        raise FixtureMismatch(f"{call} {path} blocked past its {bound}s bound", settled=False)
    if outcome is None:
        raise FixtureMismatch(
            f"{call} {path}: untyped error {type(untyped).__name__}: {untyped}"
        ) from untyped
    return outcome


_OUTCOME_FIELDS = ("state", "kind", "complete", "gap", "loss_detected", "exists", "regular_file")


def _summary(alternative: dict[str, Any]) -> str:
    fields = {k: v for k, v in alternative["document"].items() if k in _OUTCOME_FIELDS}
    return f"{json.dumps(fields)} alive={alternative.get('descendant_alive')}"


async def run_hazard(subject: Subject, hazard: str) -> list[int]:
    """Drive every step of `hazard` against `subject`.

    Returns the index of the alternative each step matched. Raises
    FixtureMismatch on the first miss, or NotRun when a step's `given` or its
    liveness cannot be established.
    """
    fixture = HAZARDS[hazard]
    if "provider" in fixture:
        _small_retention(subject)
    state: dict[str, Any] = {}
    matched: list[int] = []
    for index, step in enumerate(fixture["steps"]):
        call = step["call"]
        bound = step.get("settles_within_seconds", 30)
        began = time.monotonic()
        try:
            async with asyncio.timeout(bound):
                outcome = await _call(subject, step, hazard, state)
        except TimeoutError as e:
            raise FixtureMismatch(
                f"{subject.name} {hazard} step {index} {call}: did not settle within {bound}s",
                hazard=hazard,
                step=index,
                call=call,
                settled=False,
            ) from e
        except FixtureMismatch as e:
            if e.step is not None:
                raise
            raise FixtureMismatch(
                f"{subject.name} {hazard} step {index} {call}: {e}",
                hazard=hazard,
                step=index,
                call=call,
                outcome=e.outcome,
                settled=e.settled,
            ) from e
        elapsed = time.monotonic() - began
        alive = bool(subject.alive())
        alternatives = step.get("expect_one_of") or [step["expect"]]
        where = f"{subject.name} {hazard} step {index} {call} ({elapsed:.1f}s)"
        miss = {"hazard": hazard, "step": index, "call": call, "outcome": outcome, "alive": alive}
        if (alternative := _matches(alternatives, outcome, alive)) is None:
            raise FixtureMismatch(
                f"{where}: got {outcome.describe()} with descendant_alive={alive} "
                f"(measured pids {subject.alive()}); none of "
                + "; ".join(_summary(a) for a in alternatives),
                **miss,
            )
        matched.append(alternative)
        if (
            "launches" in step
            and (n := await subject.settled_launches(step["launches"])) != step["launches"]
        ):
            raise FixtureMismatch(
                f"{where}: measured {n} launches, fixture says {step['launches']}", **miss
            )
        if "operation_records" in step and subject.records() != step["operation_records"]:
            raise FixtureMismatch(
                f"{where}: provider holds {subject.records()} operation records, "
                f"fixture says {step['operation_records']}",
                **miss,
            )
    return matched


# Findings: (provider, hazard) -> the first miss the harness measured. Each is a
# provider defect left to a follow-up (this change does not touch provider behaviour).
FINDINGS: dict[tuple[str, str], Finding] = {
    ("docker", "LH-3"): Finding(
        1,
        "cancel",
        {"settled": False},
        "cancel waits _LEADER_SECONDS (30s) for the header before stopping, "
        "so it cannot settle within the 30s bound",
    ),
    ("docker", "LH-4"): Finding(
        0,
        "start",
        {"operation_ref": True},
        "a start blocked in credential resolution launches after destroy began and returns a ref",
    ),
}
# The real-daemon LH-7 run (test_docker_daemon_meets_lh7_fifo).
DOCKER_DAEMON_LH7 = Finding(
    2,
    "file_exists",
    {"exists": False},
    "file_exists reports the FIFO as exists=false; the fixture needs exists=true, "
    "regular_file=false or rejected_policy",
)
# Cells whose outcome is deterministic: step -> the alternative it must match.
# A provider that starts answering a looser alternative here goes red.
CONFIRMED: dict[tuple[str, str], dict[int, int]] = {
    ("docker", "LH-1"): {1: 0},
    ("docker", "LH-2"): {3: 0},
    # The stuck removal settles provider_unavailable, never timed_out.
    ("docker", "LH-5"): {1: 1},
    ("local", "LH-2"): {3: 0},
}


async def _expect_finding(run: Awaitable[Any], finding: Finding) -> None:
    """Strict: exactly `finding`'s miss xfails; a pass or any other miss fails."""
    try:
        await run
    except FixtureMismatch as miss:
        if finding.holds(miss):
            pytest.xfail(f"{finding.reason}: {miss}")
        raise
    pytest.fail(f"[XPASS(strict)] {finding.reason}: the cell now passes; drop its pin")


# Fixture limitations: (provider, hazard) -> the one mismatch the 0.2.1 fixture
# wrongly rejects. Only that mismatch is expected; any other verdict fails.
FIXTURE_GAPS: dict[tuple[str, str], str] = {
    ("local", "LH-1"): "cancel fails closed (state failed, cancel_signal_failure names an orphan "
    "it cannot confirm unrelated, no descendant measured alive), an outcome the 0.2.1 fixture "
    "lacks; AgentParadise/agent-paradise-standards-system#146 adds it in APSS 0.2.2",
}
_UNCONFIRMED_ORPHAN = "cannot be confirmed unrelated"


def _unconfirmed_orphan_cancel(error: FixtureMismatch) -> bool:
    """Whether `error` is exactly the APSS#146 outcome on LH-1's cancel step."""
    status = error.outcome.document if error.outcome is not None else None
    return (
        error.hazard == "LH-1"
        and error.step == 1
        and isinstance(status, OperationStatus)
        and status.state == "failed"
        and _UNCONFIRMED_ORPHAN in status.metadata.get("cancel_signal_failure", "")
        and error.alive is False
    )


async def _run_classified(subject: Subject, hazard: str) -> None:
    """`run_hazard`, with the one mismatch in FIXTURE_GAPS reraised as its own type."""
    try:
        await run_hazard(subject, hazard)
    except FixtureMismatch as error:
        if (subject.name, hazard) in FIXTURE_GAPS and _unconfirmed_orphan_cancel(error):
            raise UnconfirmedOrphanCancel(
                str(error),
                hazard=error.hazard,
                step=error.step,
                outcome=error.outcome,
                alive=error.alive,
            ) from error
        raise


# Cases the harness cannot drive yet; each is reported as not run, never as a pass.
NOT_RUN: dict[tuple[str, str], str] = {
    ("docker", "LH-7"): "the probes read the host workspace mount, which a stand-in container "
    "does not have; run against a real daemon by test_docker_daemon_meets_lh7_fifo",
}


@pytest.mark.parametrize("hazard", sorted(HAZARDS))
async def test_provider_meets_lifecycle_hazard(subject: Subject, hazard: str) -> None:
    cell = (subject.name, hazard)
    if cell in NOT_RUN:
        pytest.skip(f"NOT RUN: {NOT_RUN[cell]}")
    try:
        if cell in FINDINGS:
            await _expect_finding(run_hazard(subject, hazard), FINDINGS[cell])
            return
        if cell in FIXTURE_GAPS:
            # Not a mark: the adoption that triggers it is host-dependent, so a
            # pass is a pass. Only UnconfirmedOrphanCancel is excused; any other
            # mismatch propagates and fails. The strict expected failure is
            # test_local_lh1_unconfirmed_orphan_is_the_apss146_gap.
            try:
                await _run_classified(subject, hazard)
            except UnconfirmedOrphanCancel as gap:
                pytest.xfail(f"APSS#146: {FIXTURE_GAPS[cell]}: {gap}")
            return
        matched = await run_hazard(subject, hazard)
    except NotRun as reason:
        pytest.skip(f"NOT RUN: {reason}")
    for step, alternative in CONFIRMED.get(cell, {}).items():
        assert matched[step] == alternative, (
            f"{cell} step {step} matched alternative {matched[step]}, not the deterministic "
            f"{alternative}: {HAZARDS[hazard]['steps'][step]['expect_one_of'][alternative]}"
        )


def test_every_hazard_fixture_is_driven() -> None:
    # An empty or partial vendor would otherwise pass the matrix vacuously.
    assert sorted(HAZARDS) == [f"LH-{n}" for n in range(1, 8)]
    calls = {step["call"] for doc in HAZARDS.values() for step in doc["steps"]}
    assert calls == {"start", "status", "cancel", "observe", "file_exists", "read_file", "finalize"}


# The cancel status Local emitted in CI run 38603430148 (job 115988139845).
_CI_UNCONFIRMED_ORPHAN = {
    "ref": {
        "workspace": {"provider": "local", "resource_id": "w1"},
        "operation_id": "op1",
        "operation_key": "k1",
    },
    "state": "failed",
    "exit_code": 143,
    "signal": "SIGTERM",
    "execution_id": "e1",
    "metadata": {
        "cancel_signal_failure": "processes [31103] were orphaned to a reaper after the "
        "operation began and may be its descendants; without containment they cannot be "
        "confirmed unrelated, so they were not signalled"
    },
}


def _cancel_miss(*, alive: bool = False, step: int = 1, hazard: str = "LH-1", **status: Any):
    document = OperationStatus.model_validate({**_CI_UNCONFIRMED_ORPHAN, **status})
    return FixtureMismatch(
        "miss", hazard=hazard, step=step, outcome=Outcome(document=document), alive=alive
    )


async def test_the_apss146_gap_is_exactly_the_emitted_unconfirmed_orphan_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    miss = _cancel_miss()

    async def fails(subject: Any, hazard: str) -> None:
        raise miss

    monkeypatch.setattr(f"{__name__}.run_hazard", fails)
    local = Subject.__new__(Subject)
    local.name = "local"
    with pytest.raises(UnconfirmedOrphanCancel) as caught:
        await _run_classified(local, "LH-1")
    assert caught.value.__cause__ is miss
    # Only Local's LH-1 is excused: Docker's identical miss stays a FixtureMismatch.
    local.name = "docker"
    with pytest.raises(FixtureMismatch) as docker:
        await _run_classified(local, "LH-1")
    assert not isinstance(docker.value, UnconfirmedOrphanCancel)


@pytest.mark.parametrize(
    "miss",
    [
        pytest.param(_cancel_miss(alive=True), id="descendant-alive"),
        pytest.param(_cancel_miss(state="cancelled", metadata={}), id="confirmed-cancel"),
        pytest.param(_cancel_miss(metadata={}), id="failed-without-orphan-note"),
        pytest.param(
            _cancel_miss(metadata={"cancel_signal_failure": "owned processes [7] survived"}),
            id="other-signal-failure",
        ),
        pytest.param(_cancel_miss(step=0), id="other-step"),
        pytest.param(_cancel_miss(hazard="LH-2"), id="other-hazard"),
        pytest.param(FixtureMismatch("LH-1 step 1 cancel: did not settle"), id="unsettled"),
    ],
)
async def test_any_other_local_lh1_mismatch_still_fails(
    miss: FixtureMismatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LH-1 expected failure is narrow: a different miss propagates as itself."""

    async def fails(subject: Any, hazard: str) -> None:
        raise miss

    monkeypatch.setattr(f"{__name__}.run_hazard", fails)
    local = Subject.__new__(Subject)
    local.name = "local"
    with pytest.raises(FixtureMismatch) as caught:
        await _run_classified(local, "LH-1")
    assert caught.value is miss and not isinstance(caught.value, UnconfirmedOrphanCancel)


def _unrelated_orphan() -> int:
    """A live process adopted by a reaper, unrelated to any operation: its
    parent `sh` exits at once, and it carries neither a token nor a pipe."""
    parent = subprocess.run(
        ["sh", "-c", "sleep 120 >/dev/null 2>&1 & echo $!"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(parent.stdout)


@pytest.mark.parametrize("subject", ["local"], indirect=True)
@pytest.mark.xfail(
    raises=UnconfirmedOrphanCancel,
    strict=True,
    reason="APSS#146 (AgentParadise/agent-paradise-standards-system#146): "
    + FIXTURE_GAPS["local", "LH-1"],
)
async def test_local_lh1_unconfirmed_orphan_is_the_apss146_gap(subject: Subject) -> None:
    """The host condition CI hit, made deterministic: an unrelated orphan adopted
    after the operation began is alive when LH-1 cancels. Local fails closed,
    which 0.2.1 rejects. Strict: a pass, or any other miss, fails this test;
    at APSS 0.2.2 it XPASSes, and the mark and FIXTURE_GAPS go."""
    original = LocalOperations._signal_and_confirm
    orphans: list[int] = []

    async def with_orphan(self: LocalOperations, operation: Any, *, grace_seconds: float) -> None:
        orphans.append(_unrelated_orphan())
        await original(self, operation, grace_seconds=grace_seconds)

    subject.monkeypatch.setattr(LocalOperations, "_signal_and_confirm", with_orphan)
    try:
        await _run_classified(subject, "LH-1")
    finally:
        for pid in orphans:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    assert orphans, "LH-1 never cancelled, so no orphan was introduced"


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


@pytest.mark.parametrize("subject", ["local"], indirect=True)
async def test_an_unreadable_environ_is_not_run_not_dead(subject: Subject) -> None:
    """Liveness fails closed (#68): a workload whose environ cannot be read is
    unmeasurable, so the cell is NOT RUN rather than judged alive=false."""
    readable = _environ

    def hidden(entry: Path) -> bytes:
        environ = readable(entry)
        if f"{TOKEN_VARIABLE}={subject.token}".encode() in environ.split(b"\0"):
            raise PermissionError(13, "Permission denied", str(entry / "environ"))
        return environ

    subject.monkeypatch.setattr(f"{__name__}._environ", hidden)
    with pytest.raises(NotRun, match="unreadable"):
        await run_hazard(subject, "LH-2")


@pytest.mark.parametrize("subject", ["local"], indirect=True)
async def test_a_probe_raising_an_untyped_error_is_a_fixture_mismatch(subject: Subject) -> None:
    async def untyped(workspace: Workspace, path: str) -> bool:
        raise ValueError("not a typed refusal")

    subject.monkeypatch.setattr(subject.provider, "file_exists", untyped)
    with pytest.raises(FixtureMismatch, match="untyped error ValueError") as caught:
        await run_hazard(subject, "LH-7")
    assert (caught.value.step, caught.value.call) == (2, "file_exists")


@pytest.mark.parametrize("subject", ["local"], indirect=True)
async def test_a_not_a_regular_file_refusal_is_rejected_policy(subject: Subject) -> None:
    async def refuses(workspace: Workspace, path: str) -> bytes:
        raise NotARegularFileError(path)

    subject.monkeypatch.setattr(subject.provider, "read_file", refuses)
    matched = await run_hazard(subject, "LH-7")
    read = HAZARDS["LH-7"]["steps"][3]["expect_one_of"][matched[3]]["document"]
    assert read["kind"] == "rejected_policy"


def _miss(step: int, call: str, **fields: Any) -> FixtureMismatch:
    settled = fields.pop("settled", True)
    outcome = Outcome(document=fields) if fields else None
    return FixtureMismatch("miss", step=step, call=call, outcome=outcome, settled=settled)


@pytest.mark.parametrize(
    "miss",
    [
        pytest.param(_miss(2, "status", settled=False), id="other-step"),
        pytest.param(_miss(1, "status", settled=False), id="other-call"),
        pytest.param(_miss(1, "cancel", state="failed"), id="other-outcome"),
    ],
)
async def test_a_finding_that_misses_elsewhere_fails(miss: FixtureMismatch) -> None:
    """A pin is (step, call, outcome): any other miss propagates as itself."""

    async def fails() -> None:
        raise miss

    # An xfail escaping here would report this test as expected-failed, not
    # failed, so taking the miss for the finding is turned into a failure.
    try:
        await _expect_finding(fails(), FINDINGS["docker", "LH-3"])
    except pytest.xfail.Exception:
        pytest.fail(f"a miss at step {miss.step} {miss.call} was taken for the pinned finding")
    except FixtureMismatch as caught:
        assert caught is miss
    else:
        pytest.fail("the miss produced no verdict")


async def test_a_finding_xfails_only_on_its_pinned_miss() -> None:
    async def fails() -> None:
        raise _miss(1, "cancel", settled=False)

    async def passes() -> None:
        return None

    with pytest.raises(pytest.xfail.Exception):
        await _expect_finding(fails(), FINDINGS["docker", "LH-3"])
    with pytest.raises(pytest.fail.Exception, match="XPASS"):
        await _expect_finding(passes(), FINDINGS["docker", "LH-3"])


async def _daemon_lh7(subject: Subject) -> None:
    """The real-daemon LH-7 cell: its strict finding, or NOT RUN when liveness is
    unmeasurable (#68), as in the matrix; never a failure, xfail or pass."""
    try:
        await _expect_finding(run_hazard(subject, "LH-7"), DOCKER_DAEMON_LH7)
    except NotRun as reason:
        pytest.skip(f"NOT RUN: {reason}")


@pytest.mark.parametrize("subject", ["local"], indirect=True)
async def test_an_unmeasurable_daemon_lh7_is_not_run(subject: Subject) -> None:
    """A process the harness cannot read (on CI, one inside the daemon's
    container) makes the real-daemon cell NOT RUN, through the same boundary."""
    bystander = subprocess.Popen(["sleep", "60"])
    readable = _environ

    def hidden(entry: Path) -> bytes:
        if entry.name == str(bystander.pid):
            raise PermissionError(13, "Permission denied", str(entry / "environ"))
        return readable(entry)

    subject.monkeypatch.setattr(f"{__name__}._environ", hidden)
    try:
        with pytest.raises(pytest.skip.Exception, match="NOT RUN: .*unreadable"):
            await _daemon_lh7(subject)
    finally:
        bystander.kill()
        bystander.wait()


@pytest.mark.integration
@pytest.mark.skipif(not WorkspaceDockerProvider.is_available(), reason="Docker not available")
async def test_docker_daemon_meets_lh7_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LH-7 against a real container: a FIFO made inside it, probed and read
    through the host mount, then a real teardown. Skips without Docker, like
    tests/integration; CI python-compatibility has a daemon and runs it."""
    gate = _Gate()
    provider = WorkspaceDockerProvider(
        default_image="python:3.12-slim", security=SecurityConfig.development(), credentials=gate
    )
    workspace = await provider.create(WorkspaceConfig(provider="docker", image="python:3.12-slim"))
    root = Path(workspace.metadata["workspace_dir"])
    (root / "repo").mkdir(exist_ok=True)
    (root / "repo").chmod(0o777)
    subject = Subject(
        "docker",
        provider,
        provider._operations,
        workspace,
        root,
        uuid.uuid4().hex,
        root / ".launches",
        gate,
        None,
        monkeypatch,
        launch_log_in_workload="/workspace/.launches",
    )
    try:
        await _daemon_lh7(subject)
    finally:
        subject.kill_leftovers()
        if workspace.id in provider._workspaces:
            await provider.destroy(workspace)
