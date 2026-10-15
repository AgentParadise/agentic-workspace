"""The claude-cli image a live harness test runs, present on the local daemon.

The reference is `AGENTIC_CLAUDE_CLI_IMAGE` when set (an explicit override,
e.g. an image built from this tree), otherwise the release pin in
`claude-cli.release-pin`, which must name a digest. A reference the daemon
does not hold is pulled. Every way of not ending up with an image raises
`ReleaseImageError` naming the reference: a live test that skips instead
voids its mutation evidence and hides drift from the release.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

OVERRIDE_ENV = "AGENTIC_CLAUDE_CLI_IMAGE"
PIN_FILE = Path(__file__).with_name("claude-cli.release-pin")
_DIGEST_REFERENCE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")

Docker = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ReleaseImageError(Exception):
    """No usable image; the message names the reference and the cause."""


def _docker(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)


def _read_pin(pin_file: Path) -> str:
    try:
        text = pin_file.read_text()
    except OSError as e:
        raise ReleaseImageError(
            f"release pin {pin_file} unreadable ({e}); restore it or set {OVERRIDE_ENV}"
        ) from e
    lines = [s for line in text.splitlines() if (s := line.strip()) and not s.startswith("#")]
    if len(lines) != 1 or not _DIGEST_REFERENCE.fullmatch(lines[0]):
        raise ReleaseImageError(
            f"release pin {pin_file} must hold exactly one image@sha256:<digest> line, "
            f"found {lines}"
        )
    return lines[0]


def release_image(
    env: Mapping[str, str] = os.environ,
    pin_file: Path = PIN_FILE,
    docker: Docker = _docker,
) -> str:
    """The image reference to run, after making sure the daemon holds it."""
    reference = env.get(OVERRIDE_ENV) or _read_pin(pin_file)
    try:
        if docker(["image", "inspect", reference]).returncode == 0:
            return reference
        pulled = docker(["pull", "--quiet", reference])
    except OSError as e:
        raise ReleaseImageError(f"cannot run docker for {reference}: {e}") from e
    if pulled.returncode != 0:
        raise ReleaseImageError(
            f"docker pull {reference} failed (exit {pulled.returncode}): {pulled.stderr.strip()}"
        )
    return reference
