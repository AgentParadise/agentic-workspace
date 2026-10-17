"""Docker implementations of the remote-tier capabilities (#1911).

Everything here goes through the docker CLI, so it works the same whether or
not the caller can see the workspace's host directory, and reaches tmpfs
paths such as /spool that a host mount and `docker cp` cannot.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import io
import json
import signal as signals
import tarfile
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence

from agentic_isolation.providers.capabilities import (
    TRANSFER_ROOTS,
    OwnedWorkspace,
    StreamOutcome,
    StreamReconnectUnsupportedError,
    StreamSession,
    TransferRoot,
)

WORKSPACE_ID_LABEL = "agentic.workspace.id"
PROVIDER_LABEL = "agentic.provider"

# Runs the command as a child with stderr folded into stdout INSIDE the
# sandbox, so the merged order is the order of the writes. The child's pid
# goes to "$0" so a stop reaches it, and its exit status to "$0.rc", so
# completion is read from the sandbox, never inferred from the docker client.
_SUPERVISE = (
    'shift; "$@" 2>&1 & child=$!; echo "$child" > "$0"; '
    'wait "$child"; rc=$?; echo "$rc" > "$0.rc"; exit "$rc"'
)

# Bytes per read of the output pipe. Lines are framed here, not by
# StreamReader.readline, so no line length is too long to deliver.
_READ_CHUNK = 64 * 1024

_STOP_ALREADY_ENDED = 3


async def _docker(*args: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode if proc.returncode is not None else -1, out, err


def _line(raw: bytes | bytearray) -> str:
    return bytes(raw).decode(errors="replace").rstrip("\r")


class DockerStreamSession:
    """A `docker exec` whose sandbox pid is known, so cancel reaches it."""

    def __init__(
        self,
        container: str,
        proc: asyncio.subprocess.Process,
        pid_file: str,
        timeout: float | None,
    ) -> None:
        self._container = container
        self._proc = proc
        self._pid_file = pid_file
        # Serialises stops and status reads, so a stop is published only once
        # confirmed and one observer cannot consume another's evidence.
        self._lock = asyncio.Lock()
        self._stopped_by: StreamOutcome | None = None
        # The completion read from the sandbox. Reading it consumes the
        # status file, so it is kept for every later observation.
        self._completed: StreamOutcome | None = None
        # The client's own stderr carries only docker diagnostics: the
        # sandbox's stderr is already folded into stdout by _SUPERVISE.
        self._client_errors: asyncio.Task[bytes] | None = (
            asyncio.ensure_future(proc.stderr.read()) if proc.stderr is not None else None
        )
        self._timer = (
            asyncio.get_running_loop().call_later(timeout, self._on_timeout)
            if timeout is not None
            else None
        )
        self._timeout_task: asyncio.Task[None] | None = None

    @classmethod
    async def start(
        cls,
        container: str,
        sandbox_argv: Sequence[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        timeout: float | None,
    ) -> DockerStreamSession:
        if not sandbox_argv:
            raise ValueError("sandbox_argv must name a command")
        pid_file = f"/tmp/.agentic-stream-{uuid.uuid4().hex}.pid"
        argv = ["docker", "exec", "-i", "-w", cwd or "/workspace"]
        for key, value in (env or {}).items():
            argv.extend(["-e", f"{key}={value}"])
        argv.extend([container, "sh", "-c", _SUPERVISE, pid_file, "--", *sandbox_argv])
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return cls(container, proc, pid_file, timeout)

    async def lines(self) -> AsyncIterator[str]:
        assert self._proc.stdout is not None  # opened with stdout=PIPE above
        pending = bytearray()
        while chunk := await self._proc.stdout.read(_READ_CHUNK):
            *complete, rest = chunk.split(b"\n")
            for raw in complete:
                pending.extend(raw)
                yield _line(pending)
                pending.clear()
            pending.extend(rest)
        if pending:
            yield _line(pending)

    async def outcome(self) -> StreamOutcome:
        await self._proc.wait()
        if self._timer is not None:
            self._timer.cancel()
        if self._timeout_task is not None:
            await self._timeout_task
        client_errors = (
            (await self._client_errors).decode(errors="replace").strip()
            if (self._client_errors is not None)
            else ""
        )
        async with self._lock:
            if self._stopped_by is not None:
                return self._stopped_by
            if self._completed is not None:
                return self._completed
            exit_code = await self._sandbox_exit_code()
            if exit_code is None:
                # The client ended but the sandbox recorded no exit: whether the
                # process still runs, or ever started, is unknown.
                return StreamOutcome(end="disconnected", detail=client_errors or None)
            self._completed = StreamOutcome(end="completed", exit_code=exit_code)
            return self._completed

    async def cancel(self) -> None:
        await self._stop(StreamOutcome(end="completed", signal=signals.SIGKILL))

    async def reconnect(self) -> StreamSession:
        raise StreamReconnectUnsupportedError(
            "docker exec cannot reattach to a running process's output; "
            f"the process in {self._container} keeps running"
        )

    async def _sandbox_exit_code(self) -> int | None:
        """The status _SUPERVISE recorded, or None when there is none to read."""
        try:
            code, out, _ = await _docker(
                "exec",
                self._container,
                "sh",
                "-c",
                'cat "$0.rc" && rm -f "$0" "$0.rc"',
                self._pid_file,
            )
        except OSError:
            # No docker client to ask: the status is unknown, not absent.
            return None
        if code != 0:
            return None
        try:
            return int(out.decode().strip())
        except ValueError:
            return None

    def _on_timeout(self) -> None:
        self._timeout_task = asyncio.ensure_future(self._stop(StreamOutcome(end="timed_out")))

    async def _stop(self, outcome: StreamOutcome) -> None:
        async with self._lock:
            await self._stop_locked(outcome)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._proc.wait(), timeout=10)
        if self._proc.returncode is None:
            self._proc.kill()

    async def _stop_locked(self, outcome: StreamOutcome) -> None:
        # Only a known sandbox exit or a confirmed stop makes this a no-op. A
        # dead docker client is not one: the sandbox process may still run.
        if self._completed is not None or self._stopped_by is not None:
            return
        # Wait briefly for the announce line, kill the sandbox process, then
        # confirm it is gone (a zombie counts as gone). `kill` is a shell
        # builtin in minimal images, so it cannot be exec'd.
        script = (
            'i=0; while [ ! -s "$0" ]; do [ "$i" -ge 50 ] && exit 1; '
            'i=$((i + 1)); sleep 0.1; done; pid=$(cat "$0"); '
            'alive() { [ -e "/proc/$pid" ] && ! grep -q ") Z " "/proc/$pid/stat"; }; '
            f"alive || exit {_STOP_ALREADY_ENDED}; "
            'kill -KILL "$pid" || exit 1; i=0; '
            'while alive; do [ "$i" -ge 50 ] && exit 1; i=$((i + 1)); sleep 0.1; done'
        )
        code, _, err = await _docker("exec", self._container, "sh", "-c", script, self._pid_file)
        if code == _STOP_ALREADY_ENDED:
            return
        if code != 0:
            # Nothing is published: the outcome stays unknown and a later
            # stop tries again.
            if self._proc.returncode is None:
                self._proc.kill()
            raise RuntimeError(
                f"could not stop the sandbox process in {self._container}: "
                f"{err.decode(errors='replace').strip() or f'exit {code}'}"
            )
        self._stopped_by = outcome


async def read_files(
    container: str, patterns: Sequence[str], base_path: TransferRoot
) -> list[tuple[str, bytes]]:
    """Read matching regular files under `base_path` as a tar from the sandbox."""
    if base_path not in TRANSFER_ROOTS:
        raise ValueError(f"base_path must be one of {', '.join(TRANSFER_ROOTS)}")
    code, out, err = await _docker("exec", container, "tar", "-cf", "-", "-C", base_path, ".")
    if code != 0:
        raise RuntimeError(
            f"could not read {base_path} from {container}: "
            f"{err.decode(errors='replace').strip() or f'tar exited {code}'}"
        )
    found: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(out), mode="r:") as archive:
        for member in archive:
            relative = member.name.removeprefix("./")
            # A hard link is a regular file under a second name; tar stores
            # its bytes once, under whichever name it met first.
            regular = member.isreg() or member.islnk()
            if not regular or not any(fnmatch.fnmatchcase(relative, p) for p in patterns):
                continue
            handle = archive.extractfile(member)
            assert handle is not None  # extractfile resolves regular files and hard links
            found.append((relative, handle.read()))
    return sorted(found)


async def inspect_containers(container_ids: Sequence[str]) -> list[tuple[str, dict[str, str]]]:
    """The name and labels of each container, in order."""
    if not container_ids:
        return []
    code, out, err = await _docker(
        "inspect",
        "--format",
        '{"name": {{json .Name}}, "labels": {{json .Config.Labels}}}',
        *container_ids,
    )
    if code != 0:
        raise RuntimeError(f"docker inspect failed: {err.decode(errors='replace').strip()}")
    containers: list[tuple[str, dict[str, str]]] = []
    for line in out.decode().splitlines():
        parsed: object = json.loads(line)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("name"), str):
            raise RuntimeError(f"docker inspect returned an unexpected container: {line}")
        labels: object = parsed.get("labels") or {}
        if not isinstance(labels, dict):
            raise RuntimeError(f"docker inspect returned labels that are not an object: {line}")
        name = parsed["name"].removeprefix("/")
        containers.append((name, {str(k): str(v) for k, v in labels.items()}))
    return containers


async def find_containers(
    provider_name: str, labels: Mapping[str, str]
) -> list[tuple[str, dict[str, str]]]:
    """The name and labels of every container of this provider carrying all of `labels`."""
    filters = [f"--filter=label={PROVIDER_LABEL}={provider_name}"]
    filters.extend(f"--filter=label={key}={value}" for key, value in labels.items())
    code, out, err = await _docker("ps", "-a", "-q", "--no-trunc", *filters)
    if code != 0:
        raise RuntimeError(f"docker ps failed: {err.decode(errors='replace').strip()}")
    return await inspect_containers(out.decode().split())


async def list_owned(provider_name: str, labels: Mapping[str, str]) -> list[OwnedWorkspace]:
    """Every workspace container of this provider carrying all of `labels`."""
    owned = [
        OwnedWorkspace(workspace_id=found[WORKSPACE_ID_LABEL], labels=found)
        for _, found in await find_containers(provider_name, labels)
        if WORKSPACE_ID_LABEL in found
    ]
    return sorted(owned, key=lambda workspace: workspace.workspace_id)
