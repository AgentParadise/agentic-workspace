"""Host half of the harness credential round trip's sandbox scan (ADR-042).

The scan runs on the host, not as the sandbox's agent user, so a file the
agent cannot read (the release image ships root-owned 0600 build leftovers)
is still read in full rather than reported as unscanned. Collection goes
through the Docker daemon, which reads as root:

- `docker export` streams the container's whole filesystem as a tar, image
  files included; nothing is skipped for being pre-existing,
- `docker cp CONTAINER:DEST -` streams each writable mount, which export
  leaves out; a tmpfs cannot be read after exit, so it lands in `unscanned`,
- `docker diff` names the paths the run added or changed,
- `docker top` samples every process command line while the CLI runs.

Matching is host-side too. Exact needles (the wire and the host's synthetic
canaries) are searched for in every byte of every regular file. The text of
the files the run wrote, and of every writable mount, is kept for the
caller's credential-shape check; image files are not, because a shape regex
over a whole image matches vendored examples. Symlinks are not followed:
their targets are members of the same filesystem and are scanned where they
live. A stream cut inside a member raises; one cut between members is
caught by the docker command's exit status.
"""

from __future__ import annotations

import json
import subprocess
import tarfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import IO


@dataclass
class Inventory:
    # Text of the files the run wrote, for the caller's shape check.
    files: dict[str, str] = field(default_factory=dict)
    # Path -> names of the exact needles found in it, over every file.
    hits: dict[str, list[str]] = field(default_factory=dict)
    unscanned: dict[str, str] = field(default_factory=dict)
    special: list[str] = field(default_factory=list)

    def as_report(self) -> dict[str, object]:
        return {
            "files": self.files,
            "hits": self.hits,
            "unscanned": self.unscanned,
            "special": self.special,
        }


def scan_tar(
    stream: IO[bytes],
    needles: Mapping[str, str],
    keep: Callable[[str], bool],
    into: Inventory,
    *,
    root: str = "/",
) -> None:
    """Read every regular member of a tar stream in full and match `needles`.

    Member permissions and owners do not matter: the bytes are in the
    stream. `root` is where the archive's top level sits in the sandbox.
    """
    encoded = {name: value.encode() for name, value in needles.items()}
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            path = str(PurePosixPath(root, member.name.removeprefix("./")))
            if member.isdir() or member.issym() or member.islnk():
                # A hard link's bytes are its target's, read at that member.
                continue
            if not member.isreg():
                into.special.append(path)
                continue
            source = archive.extractfile(member)
            if source is None:
                into.unscanned[path] = "tar member without data"
                continue
            data = source.read()
            found = [name for name, needle in encoded.items() if needle in data]
            if found:
                into.hits[path] = found
            if keep(path):
                into.files[path] = data.decode("utf-8", "replace")


def _docker(*argv: str) -> str:
    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=120, check=True
    ).stdout


def _scan_stream(
    argv: list[str],
    needles: Mapping[str, str],
    keep: Callable[[str], bool],
    into: Inventory,
    root: str = "/",
) -> None:
    proc = subprocess.Popen(["docker", *argv], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    try:
        scan_tar(proc.stdout, needles, keep, into, root=root)
    finally:
        proc.stdout.close()
        returncode = proc.wait(timeout=120)
    if returncode != 0:
        raise RuntimeError(f"docker {' '.join(argv)} exited {returncode}")


def scan_container(container: str, needles: Mapping[str, str]) -> dict[str, object]:
    """Scan a stopped container's filesystem and writable mounts."""
    changed = {
        line.split(" ", 1)[1]
        for line in _docker("diff", container).splitlines()
        if line[:2] in ("A ", "C ")
    }
    inventory = Inventory()
    _scan_stream(["export", container], needles, changed.__contains__, inventory)
    (details,) = json.loads(_docker("inspect", container))
    for mount in details["Mounts"]:
        if not mount["RW"]:
            continue
        destination = mount["Destination"]
        _scan_stream(
            ["cp", f"{container}:{destination}", "-"],
            needles,
            lambda _path: True,
            inventory,
            str(PurePosixPath(destination).parent),
        )
    for destination in details["HostConfig"].get("Tmpfs") or {}:
        inventory.unscanned[destination] = "tmpfs is gone once the container exits"
    return inventory.as_report()


class CommandLines:
    """Sample a running container's process command lines through the daemon."""

    def __init__(self, container: str, interval: float = 0.2) -> None:
        self.lines: set[str] = set()
        self._container = container
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> CommandLines:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            # Fails until the container starts and after it stops; the
            # caller asserts the CLI's own line was seen.
            top = subprocess.run(
                ["docker", "top", self._container, "-eww", "-o", "pid,args"],
                capture_output=True,
                text=True,
                check=False,
            )
            if top.returncode == 0:
                for line in top.stdout.splitlines()[1:]:
                    self.lines.add(line.strip().split(None, 1)[-1])
