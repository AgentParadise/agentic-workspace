"""Which claude-cli image the live harness tests run, and that they never skip.

`tests/integration/release_image.py` resolves the reference (override, else
the release pin) and makes the daemon hold it. Docker is faked here: these
are the failure paths a runner hits only by accident.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.integration.release_image import (
    OVERRIDE_ENV,
    PIN_FILE,
    ReleaseImageError,
    release_image,
)

PINNED = "ghcr.io/example/claude@sha256:" + "ab" * 32


class FakeDocker:
    def __init__(self, *, present: bool, pull_rc: int = 0, pull_stderr: str = "") -> None:
        self.present = present
        self.pull_rc = pull_rc
        self.pull_stderr = pull_stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[0] == "image":
            return subprocess.CompletedProcess(argv, 0 if self.present else 1, "", "")
        return subprocess.CompletedProcess(argv, self.pull_rc, "", self.pull_stderr)


def _pin(tmp_path: Path, body: str) -> Path:
    pin = tmp_path / "claude-cli.release-pin"
    pin.write_text(body)
    return pin


def test_override_wins_and_the_pin_is_not_read(tmp_path: Path) -> None:
    docker = FakeDocker(present=True)
    image = release_image(
        env={OVERRIDE_ENV: "agentic-workspace-claude-cli:dev"},
        pin_file=tmp_path / "absent",
        docker=docker,
    )
    assert image == "agentic-workspace-claude-cli:dev"
    assert docker.calls == [["image", "inspect", "agentic-workspace-claude-cli:dev"]]


def test_pinned_default_is_pulled_when_absent(tmp_path: Path) -> None:
    docker = FakeDocker(present=False)
    pin = _pin(tmp_path, f"# copied from the consumer\n\n{PINNED}\n")
    assert release_image(env={}, pin_file=pin, docker=docker) == PINNED
    assert docker.calls == [["image", "inspect", PINNED], ["pull", "--quiet", PINNED]]


def test_checked_in_pin_is_a_release_digest() -> None:
    docker = FakeDocker(present=True)
    image = release_image(env={}, docker=docker)
    assert image.startswith("ghcr.io/agentparadise/agentic-workspace-claude@sha256:")
    assert docker.calls == [["image", "inspect", image]]
    assert PIN_FILE.name == "claude-cli.release-pin"


def test_missing_pin_file_fails_loudly(tmp_path: Path) -> None:
    missing = tmp_path / "claude-cli.release-pin"
    docker = FakeDocker(present=True)
    with pytest.raises(ReleaseImageError, match=OVERRIDE_ENV) as raised:
        release_image(env={}, pin_file=missing, docker=docker)
    assert str(missing) in str(raised.value)
    assert docker.calls == []


@pytest.mark.parametrize(
    "body",
    [
        "agentic-workspace-claude-cli:latest\n",
        f"{PINNED}\n{PINNED}\n",
        "# only a comment\n",
    ],
)
def test_pin_that_is_not_one_digest_fails_loudly(tmp_path: Path, body: str) -> None:
    with pytest.raises(ReleaseImageError, match="exactly one image@sha256"):
        release_image(env={}, pin_file=_pin(tmp_path, body), docker=FakeDocker(present=True))


def test_pull_failure_fails_loudly_with_reference_and_error(tmp_path: Path) -> None:
    docker = FakeDocker(present=False, pull_rc=1, pull_stderr="manifest unknown\n")
    with pytest.raises(ReleaseImageError) as raised:
        release_image(env={}, pin_file=_pin(tmp_path, PINNED), docker=docker)
    assert str(raised.value) == f"docker pull {PINNED} failed (exit 1): manifest unknown"


def test_missing_docker_fails_loudly(tmp_path: Path) -> None:
    def no_docker(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", "docker")

    with pytest.raises(ReleaseImageError, match=f"cannot run docker for {PINNED}"):
        release_image(env={}, pin_file=_pin(tmp_path, PINNED), docker=no_docker)
