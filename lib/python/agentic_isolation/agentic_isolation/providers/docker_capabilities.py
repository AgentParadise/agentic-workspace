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

# Records the sandbox pid, then becomes the command with stderr folded into
# stdout INSIDE the sandbox, so the merged order is the order of the writes.
_ANNOUNCE_AND_EXEC = 'echo $$ > "$0"; shift; exec "$@" 2>&1'


async def _docker(*args: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode if proc.returncode is not None else -1, out, err


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
        self._stopped_by: StreamOutcome | None = None
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
        argv.extend([container, "sh", "-c", _ANNOUNCE_AND_EXEC, pid_file, "--", *sandbox_argv])
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return cls(container, proc, pid_file, timeout)

    async def lines(self) -> AsyncIterator[str]:
        assert self._proc.stdout is not None  # opened with stdout=PIPE above
        async for raw in self._proc.stdout:
            yield raw.decode(errors="replace").rstrip("\r\n")

    async def outcome(self) -> StreamOutcome:
        returncode = await self._proc.wait()
        if self._timer is not None:
            self._timer.cancel()
        if self._timeout_task is not None:
            await self._timeout_task
        if self._stopped_by is not None:
            return self._stopped_by
        if returncode < 0:
            # The local docker client died, not the sandbox process.
            return StreamOutcome(end="disconnected")
        return StreamOutcome(end="completed", exit_code=returncode)

    async def cancel(self) -> None:
        await self._stop(StreamOutcome(end="completed", signal=signals.SIGKILL))

    async def reconnect(self) -> StreamSession:
        raise StreamReconnectUnsupportedError(
            "docker exec cannot reattach to a running process's output; "
            f"the process in {self._container} keeps running"
        )

    def _on_timeout(self) -> None:
        self._timeout_task = asyncio.ensure_future(self._stop(StreamOutcome(end="timed_out")))

    async def _stop(self, outcome: StreamOutcome) -> None:
        if self._proc.returncode is not None or self._stopped_by is not None:
            return
        self._stopped_by = outcome
        # Wait briefly for the announce line; then kill the sandbox process.
        # `kill` is a shell builtin in minimal images, so it cannot be exec'd.
        script = (
            f'i=0; while [ "$i" -lt 50 ]; do if [ -s "{self._pid_file}" ]; then '
            f'kill -KILL "$(cat "{self._pid_file}")"; exit $?; fi; '
            'i=$((i + 1)); sleep 0.1; done; exit 1'
        )
        code, _, err = await _docker("exec", self._container, "sh", "-c", script)
        if code != 0 and self._proc.returncode is None:
            self._proc.kill()
            raise RuntimeError(
                f"could not stop the sandbox process in {self._container}: "
                f"{err.decode(errors='replace').strip() or f'exit {code}'}"
            )
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._proc.wait(), timeout=10)
        if self._proc.returncode is None:
            self._proc.kill()


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
            if not member.isreg() or not any(fnmatch.fnmatchcase(relative, p) for p in patterns):
                continue
            handle = archive.extractfile(member)
            assert handle is not None  # extractfile returns a handle for regular files
            found.append((relative, handle.read()))
    return sorted(found)


async def inspect_labels(container_ids: Sequence[str]) -> list[dict[str, str]]:
    """The labels of each container, in order."""
    if not container_ids:
        return []
    code, out, err = await _docker("inspect", "--format", "{{json .Config.Labels}}", *container_ids)
    if code != 0:
        raise RuntimeError(f"docker inspect failed: {err.decode(errors='replace').strip()}")
    labels: list[dict[str, str]] = []
    for line in out.decode().splitlines():
        parsed: object = json.loads(line)
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise RuntimeError(f"docker inspect returned labels that are not an object: {line}")
        labels.append({str(k): str(v) for k, v in parsed.items()})
    return labels


async def list_owned(provider_name: str, labels: Mapping[str, str]) -> list[OwnedWorkspace]:
    """Every workspace container of this provider carrying all of `labels`."""
    filters = [f"--filter=label={PROVIDER_LABEL}={provider_name}"]
    filters.extend(f"--filter=label={key}={value}" for key, value in labels.items())
    code, out, err = await _docker("ps", "-a", "-q", "--no-trunc", *filters)
    if code != 0:
        raise RuntimeError(f"docker ps failed: {err.decode(errors='replace').strip()}")
    owned = [
        OwnedWorkspace(workspace_id=found[WORKSPACE_ID_LABEL], labels=found)
        for found in await inspect_labels(out.decode().split())
        if WORKSPACE_ID_LABEL in found
    ]
    return sorted(owned, key=lambda workspace: workspace.workspace_id)
